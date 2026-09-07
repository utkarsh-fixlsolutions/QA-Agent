"""Input handling -> adapter dispatch -> tool invocation -> findings.

Scope is always given by the caller (docs/01-definition.md section 4): explicit
files, or a directory to walk. The agent never decides on its own to scan more.

Each registered adapter is invoked independently (docs/13-multi-analyzer-
foundation.md): one adapter failing never stops another adapter's files from
being analyzed (execution isolation), and never discards another adapter's
already-gathered findings on the way out - a failure is recorded in the
returned result, never raised (result isolation, docs/15-unified-reporting.md).
Findings are merged, deduplicated, and sorted into one deterministic order
regardless of which adapter produced them or ran first.

Invocation is otherwise generic across every adapter (docs/14-first-multi-
language-analyzer.md): an adapter declares which exit codes mean "parse my
output" versus "I failed" (ok_exit_codes - ruff's --exit-zero means only 0
ever appears, ESLint's own convention allows 1 too), every subprocess is run
with its cwd anchored to the batch's own files rather than the invoking
process's cwd (harmless for a tool like ruff that never consults it,
load-bearing for a tool whose config discovery searches upward from cwd the
way git finds .git), and an adapter declares whether it needs a shell to
launch at all (use_shell - true for eslint, whose npm .cmd shim on Windows
cannot be launched directly).

An optional Config (docs/16-configuration-system.md) narrows the inputs to
all of the above - which adapters are in play, which paths are ignored, a
severity floor on the merged findings - without this module ever importing
config.py: run() only ever reads plain attributes off whatever it's given,
defaulting to today's exact behavior when config is None.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import ADAPTERS, ToolError

# Never worth checking; skipped during directory walks without reporting each one.
IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
}

# Windows caps a command line near 32k characters, so send files in batches.
MAX_FILES_PER_CALL = 200

# A fixed file count alone does not bound the actual command-line length -
# discovered by dogfooding a real, deeply-nested repository (Phase C Part 6):
# 166 files with long absolute paths built a ~14,300 character command line.
# That is fine for a direct CreateProcess launch (shell=False - ruff,
# pyright, shellcheck; ~32k limit) but not for eslint's use_shell=True path,
# where the command is actually handed to cmd.exe, whose own line buffer is
# only ~8191 characters. Over that limit, cmd.exe rejected the command
# ("The command line is too long") with exit code 1 and empty stdout -
# indistinguishable, to the old count-only chunker, from ESLint's own normal
# "ran cleanly, found errors" exit 1, so the failure was silently read as
# zero findings instead of a tool error. Two budgets, not one, because the
# two launch paths have genuinely different limits.
MAX_COMMAND_LENGTH_SHELL = 6000
MAX_COMMAND_LENGTH_DIRECT = 30000

# A tool that never returns would otherwise block every later analysis in watch
# mode, with no diagnosis. Measured worst case is 2.0s for 3,000 files, so this
# leaves roughly 30x headroom over anything observed while still bounding a hang.
ANALYSIS_TIMEOUT_SECONDS = 60.0

# Basic severity filtering (Part 4, docs/16-configuration-system.md).
# Duplicated, not imported, from config.py's own copy - see that module's
# comment for why. Extended in Part 5 for shellcheck's four-level scale
# (style < info < warning < error) and pyright's own "information" word for
# the same concept shellcheck calls "info" - each tool's own severity string
# is passed through unchanged (no cross-tool renaming), so both spellings
# are mapped, at the same rank, rather than invented into one canonical
# name. warning/error's relative order is unchanged from Part 4 - only
# tools genuinely using ruff's or eslint's own filter settings could notice,
# and >= comparisons only ever care about relative order, not the numbers.
# Extended again in Part 7 for mypy's own "note" word (e.g. reveal_type
# output, or a hint attached as its own diagnostic) - supplementary, not a
# real problem, so ranked alongside "style" rather than invented a new tier.
SEVERITY_LEVELS = {"style": 1, "note": 1, "info": 2, "information": 2, "warning": 3, "error": 4}


@dataclass
class RunResult:
    checked: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # (path, reason)
    missing: list = field(default_factory=list)  # input paths that do not exist
    findings: list = field(default_factory=list)
    tools_used: list = field(default_factory=list)
    tool_errors: list = field(default_factory=list)  # (tool_name, ToolError)
    filtered: int = 0  # findings hidden by a config severity floor, not lost


def collect_paths(inputs, ignored_dirs=None):
    """Expand caller-given inputs into a concrete file list."""
    files, missing = [], []
    for raw in inputs:
        path = Path(raw)
        if not path.exists():
            missing.append(raw)
        elif path.is_dir():
            files.extend(_walk(path, ignored_dirs))
        else:
            files.append(path)
    return files, missing


def _walk(root, ignored_dirs=None):
    effective = IGNORED_DIRS if ignored_dirs is None else ignored_dirs
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in effective for part in path.relative_to(root).parts):
            continue
        yield path


def _chunks(files, use_shell):
    """Split files into batches bounded by both MAX_FILES_PER_CALL and an
    estimated real command-line length (see the budgets above) - a file
    count alone does not bound the length once paths get long enough.

    Length is measured on each file's *resolved* absolute form, matching
    what _invoke() actually launches - a relative path here would
    understate the real command line. A single file whose own resolved
    path already exceeds the budget still gets its own one-file batch
    (the length check only ever flushes a non-empty batch), so an
    oversized path is sent alone rather than silently dropped.
    """
    budget = MAX_COMMAND_LENGTH_SHELL if use_shell else MAX_COMMAND_LENGTH_DIRECT
    batch, length = [], 0
    for path in files:
        added = len(str(Path(path).resolve())) + 1  # +1 for the joining space
        if batch and (len(batch) >= MAX_FILES_PER_CALL or length + added > budget):
            yield batch
            batch, length = [], 0
        batch.append(path)
        length += added
    if batch:
        yield batch


def _extension_index(adapters):
    """extension -> adapters (from the given set) that claim it.

    A list rather than a single adapter: nothing here assumes only one tool
    can claim a given extension, so a second one (e.g. a type checker
    alongside ruff on .py) needs no change to this function. `adapters` is a
    parameter, not a reference to the module-level ADAPTERS, so a
    config-filtered subset (Part 4) needs no change here either.
    """
    index = {}
    for adapter in adapters:
        for extension in adapter.extensions:
            index.setdefault(extension, []).append(adapter)
    return index


def _finding_sort_key(finding):
    """(file, line, severity, message, tool): every field but the last is
    what actually distinguishes one real issue from another, so the merged
    list reads top-to-bottom by file exactly as a single-tool report always
    has - `tool` only breaks ties, deterministically, when two different
    tools genuinely report the identical issue on the identical line.
    """
    return (finding.file, finding.line, finding.severity, finding.message, finding.tool)


def _dedupe(sorted_findings):
    """Drop exact duplicates: the same file, line, severity, and message,
    reported by more than one tool (docs/15-unified-reporting.md).

    Deliberately conservative - every field but `tool` must match exactly.
    Two findings that are merely similar (a different message, a different
    line, a different severity from a reclassification) are two different
    findings and both survive. Call this on an already-sorted list: which
    copy of a genuine duplicate survives is then a deterministic function of
    the sort key, not of adapter registration or execution order.
    """
    seen = set()
    unique = []
    for finding in sorted_findings:
        key = (finding.file, finding.line, finding.severity, finding.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def _batch_cwd(resolved_files):
    """Subprocess working directory for one invocation: the batch's common
    ancestor directory (docs/14-first-multi-language-analyzer.md).

    Falls back to the inherited process cwd (None) when the batch has no
    single common ancestor - e.g. files on different drives - rather than
    anchoring it somewhere arbitrary. `resolved_files` must already be
    absolute, so this is never sensitive to what cwd the caller started with.
    """
    directories = {path.parent for path in resolved_files}
    try:
        return Path(os.path.commonpath([str(d) for d in directories]))
    except ValueError:
        return None


def _invoke(adapter, files):
    # Resolved to absolute first: once the subprocess cwd below is anchored
    # to the batch rather than inherited, a relative argument would resolve
    # against the wrong directory. Ruff already reports an absolute filename
    # for a relative input (verified), so this is byte-identical for ruff.
    resolved = [Path(f).resolve() for f in files]
    command = adapter.build_command([str(f) for f in resolved])

    # Checked explicitly rather than relying solely on subprocess.run's own
    # FileNotFoundError: an adapter that needs use_shell=True (eslint's npm
    # .cmd shim can only be launched through a shell on Windows - see
    # adapters.py) never raises that exception at all when the command isn't
    # found - the shell itself reports "not recognized" as an ordinary
    # nonzero exit with empty stdout, which would otherwise be misread as
    # "ran cleanly, no findings" (verified: exit 1 is already a normal,
    # findings-present exit for eslint). This check is adapter-blind and
    # runs the same way regardless of use_shell.
    if shutil.which(command[0]) is None:
        raise ToolError("'{}' is not installed or not on PATH".format(adapter.name))

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=ANALYSIS_TIMEOUT_SECONDS,
            cwd=_batch_cwd(resolved),
            shell=adapter.use_shell,
        )
    except FileNotFoundError as exc:
        # Defensive fallback (e.g. a race between the which() check above and
        # the actual launch) - shutil.which() is the primary detection path.
        raise ToolError("'{}' is not installed or not on PATH".format(adapter.name)) from exc
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills the child before re-raising, so nothing is left
        # behind. Raised as a ToolError because every layer above already knows
        # how to report one without ending a watch session.
        raise ToolError(
            "'{}' did not finish within {:.0f}s and was stopped ({} file(s))".format(
                adapter.name, ANALYSIS_TIMEOUT_SECONDS, len(files)
            )
        ) from exc

    # Each adapter declares which exit codes are normal for it - ruff's
    # --exit-zero means only 0 ever appears; ESLint's own convention allows 1
    # (findings present) too. Anything else is a genuine tool failure.
    if proc.returncode not in adapter.ok_exit_codes:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise ToolError(
            "'{}' exited with code {}: {}".format(adapter.name, proc.returncode, detail)
        )
    return adapter.parse(proc.stdout)


def run(inputs, extra_skipped=None, config=None):
    """Check the given paths and return everything the report needs.

    extra_skipped: (path, reason) pairs the caller already knows cannot be
    checked - e.g. files git reports as deleted. They are reported, not dropped.

    config: an optional Config (docs/16-configuration-system.md). None means
    exactly today's behavior - every registered adapter, the hardcoded ignore
    set, no severity floor. This module never imports config.py; it only
    reads plain attributes off whatever it's given.

    An adapter that fails is never raised out of this function - it is
    recorded in the returned result's `tool_errors`, so a failing tool can
    never hide another tool's real findings (docs/15-unified-reporting.md).
    The caller decides how to present that; run() itself only ever returns.
    """
    effective_adapters = ADAPTERS
    ignored_dirs = IGNORED_DIRS
    min_severity = None
    if config is not None:
        if config.analyzers is not None:
            effective_adapters = tuple(a for a in ADAPTERS if a.name in config.analyzers)
        ignored_dirs = (IGNORED_DIRS | config.extra_ignore) - config.include
        min_severity = config.min_severity

    files, missing = collect_paths(inputs, ignored_dirs=ignored_dirs)
    index = _extension_index(effective_adapters)
    # Only needed to tell "genuinely unsupported" apart from "disabled by
    # config" below - and only worth building when something was actually
    # filtered out, so the common (no config) case does no extra work.
    full_index = index if effective_adapters is ADAPTERS else _extension_index(ADAPTERS)

    batches, skipped = {}, []
    for path in files:
        matching = index.get(path.suffix.lower())
        if not matching:
            if full_index.get(path.suffix.lower()):
                reason = "disabled by config"
            else:
                reason = "no tool configured for '{}'".format(path.suffix or "no extension")
            skipped.append((path, reason))
        else:
            for adapter in matching:
                batches.setdefault(adapter.name, (adapter, []))[1].append(path)

    result = RunResult(skipped=list(extra_skipped or []) + skipped, missing=missing)
    for name, (adapter, adapter_files) in sorted(batches.items()):
        result.tools_used.append(name)
        result.checked.extend(adapter_files)
        # Isolated per adapter, in both directions: one tool failing never
        # stops another tool's files from being analyzed (execution
        # isolation, Part 1), and never discards another tool's already-
        # gathered findings on the way out (result isolation, Part 3). The
        # failure is recorded here, not raised.
        try:
            for chunk in _chunks(adapter_files, adapter.use_shell):
                result.findings.extend(_invoke(adapter, chunk))
        except ToolError as exc:
            result.tool_errors.append((name, exc))

    # Deterministic regardless of adapter registration or execution order:
    # sorted first, so which copy of an exact duplicate survives dedup below
    # is a function of the sort key, never of dict/loop iteration order.
    result.findings.sort(key=_finding_sort_key)
    result.findings = _dedupe(result.findings)

    if min_severity is not None:
        threshold = SEVERITY_LEVELS[min_severity]
        # A severity this project has never seen (a rogue adapter, or a
        # SEVERITY_POLICY reclassification to something unrecognized) always
        # survives the filter - the same "never silently hide what we can't
        # classify" rule already used for skip reasons and tool errors.
        kept = [
            f for f in result.findings
            if SEVERITY_LEVELS.get(f.severity, threshold) >= threshold
        ]
        result.filtered = len(result.findings) - len(kept)
        result.findings = kept

    return result
