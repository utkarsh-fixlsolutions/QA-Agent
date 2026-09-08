"""Isolated temporary workspaces for applying AI repair proposals (Phase E
Part 2).

Takes Part 1's advisory-only `RepairProposal` one step further: applying it
somewhere real enough to later be validated (a later Phase E part), while
guaranteeing the user's actual project is never touched. Everything here is
stdlib only (`tempfile`, `shutil`, `pathlib`) - no provider, no prompt, no
schema, nothing from the rest of `qa_agent.ai` is imported. A `RepairProposal`
is consumed purely as data (`.file`, `.start_line`, `.end_line`,
`.replacement`); this module has no idea which provider or model produced it,
and does not care.

Workflow (exactly the one this part specifies):

    Original Project
            |
    Create Temporary Workspace   (create_workspace)
            |
    Copy only required files      (apply_repair, on first use of a file)
            |
    Apply RepairProposal           (apply_repair)
            |
    Return AppliedRepair
            |
    Leave original project untouched

Hard rules carried over from repair.py and restated here because this
module's whole purpose is writing bytes to disk, where the temptation to go
further is highest: this module NEVER opens an original source file for
writing, NEVER writes outside a workspace it created, NEVER runs an analyzer,
NEVER performs a git operation, and has no notion of "correct" - it only
copies and replaces text exactly as instructed. Validating a repair (running
analyzers before/after, comparing findings) and writing back to the real
project are both explicitly later Phase E parts.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PREFIX = "qa-agent-repair-"

# The only encodings this module actively recognizes and preserves: plain
# UTF-8, and UTF-8 with a leading BOM. Every adapter, fixture, and prompt in
# this project already commits to UTF-8 throughout (docs/step-log.md); a
# source file in some other encoding is rare enough for a Python/JS/shell
# codebase that failing clearly rather than guessing (and risking silent
# corruption on write-back) is the right trade-off here.
_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class AppliedRepair:
    """The outcome of applying one `RepairProposal` inside a
    `TemporaryWorkspace` - never applied to the user's real project, which is
    guaranteed untouched whether this succeeds or not.

    `ok=True`: `workspace_file` is where the patched copy lives,
    `original_text`/`patched_text` are the workspace copy's full content
    before and after this specific repair (not the pristine original, if an
    earlier repair already patched this same file in this same workspace -
    see `apply_repair`).
    `ok=False`: `error` explains why - a malformed proposal, a missing
    source file, or a line range that does not fit the file. Never raised,
    always returned.
    """

    ok: bool
    proposal: object = None
    workspace_file: Path | None = None
    original_text: str | None = None
    patched_text: str | None = None
    error: str | None = None


class TemporaryWorkspace:
    """An isolated temporary directory holding copies of whichever specific
    files a repair has been requested against - never the whole project,
    and never the user's real files themselves.

    Stateful by design (matching `Debouncer`'s own plain-class-with-mutable-
    state shape elsewhere in this project) rather than an immutable value:
    `files` grows as `apply_repair()` copies in files it has not seen yet,
    and reuses an existing copy - already-patched content included - for one
    it has.

    `root`: the real temporary directory on disk.
    `files`: original file path (as given to `apply_repair`, resolved to an
    absolute string) -> the corresponding path of its copy inside `root`.
    Public so a caller (or a test) can inspect what has been copied in
    without re-deriving it.
    """

    def __init__(self, root: Path):
        self.root = root
        self.files: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        cleanup_workspace(self)
        return False


def create_workspace(prefix: str = DEFAULT_PREFIX):
    """Create a new, empty, isolated temporary directory. Returns a
    `TemporaryWorkspace`, or `None` if the directory itself could not be
    created (a genuine environment failure - a full disk, no permission) -
    never raises, matching every other "did it work" boundary in this
    project (`LLMResponse`, `ValidationResult`, `AnalysisOutcome`).
    """
    try:
        root = Path(tempfile.mkdtemp(prefix=prefix))
    except OSError:
        return None
    return TemporaryWorkspace(root)


def cleanup_workspace(workspace) -> None:
    """Remove a workspace's temporary directory and everything copied into
    it. Safe to call on `None`, safe to call more than once, safe to call on
    a workspace whose directory is already gone by some other means -
    cleanup itself must never be what crashes a caller. Never raises.
    """
    if workspace is None or getattr(workspace, "root", None) is None:
        return
    shutil.rmtree(workspace.root, ignore_errors=True)


def _detect_encoding(raw: bytes) -> str:
    return "utf-8-sig" if raw.startswith(_UTF8_BOM) else "utf-8"


def _detect_newline(text: str) -> str:
    """The file's dominant line ending, so a patched file stays consistent
    with the original rather than silently switching styles. `\\r\\n` is
    counted first and subtracted out before counting bare `\\n`/`\\r`, so a
    "\\r\\n" pair is never double-counted as an extra bare "\\n". Ties and
    line-ending-free files default to "\\n" - "whenever practical", not a
    guarantee for a pathologically mixed file.
    """
    crlf = text.count("\r\n")
    remainder = text.replace("\r\n", "")
    lf = remainder.count("\n")
    cr = remainder.count("\r")
    counts = {"\r\n": crlf, "\n": lf, "\r": cr}
    # dict.get's overloaded signature (an optional default) confuses both
    # mypy and pyright when passed directly as max()'s key - a lambda
    # sidesteps the ambiguity entirely (found via this project's own
    # dogfooding, Phase E Part 2).
    best = max(counts, key=lambda style: counts[style])
    return best if counts[best] > 0 else "\n"


def _read_source(path: Path):
    """Read `path` for the first time a workspace copy is needed: raw bytes
    (for a byte-exact copy, never re-encoded), the decoded text, its
    detected encoding, and its detected newline style. Raises the same
    exceptions `pathlib`/`open` would (`FileNotFoundError`, `OSError`,
    `UnicodeDecodeError`) - the caller (`apply_repair`) is the one place
    that turns those into a structured `AppliedRepair`, matching this
    project's usual "catch at the boundary, not deep inside" shape.
    """
    raw = path.read_bytes()
    encoding = _detect_encoding(raw)
    text = raw.decode(encoding)
    newline = _detect_newline(text)
    return raw, text, encoding, newline


def _copy_into_workspace(workspace: TemporaryWorkspace, path: Path) -> Path:
    """Copy `path` into its own fresh subdirectory of `workspace` - one per
    distinct source file, so two files that happen to share a basename
    (e.g. `src/a.py` and `other/a.py`) can never collide. A byte-for-byte
    copy (`shutil.copy2`, opening the source read-only): re-encoding only
    ever happens later, in `apply_repair`, on the workspace's own copy.
    """
    index = len(workspace.files)
    target_dir = workspace.root / "f{}".format(index)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    shutil.copy2(path, target)
    return target


def _proposal_shape_error(proposal):
    """The names among a `RepairProposal`'s required attributes that are
    missing or the wrong type - duck-typed, matching how `Finding`-shaped
    objects are already validated loosely elsewhere in this project
    (prompts.py's own documented convention), so a plain test double works
    exactly like the real dataclass.
    """
    file = getattr(proposal, "file", None)
    if not isinstance(file, str) or not file.strip():
        return "proposal.file must be a non-empty string"
    start = getattr(proposal, "start_line", None)
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        return "proposal.start_line must be a positive integer"
    end = getattr(proposal, "end_line", None)
    if not isinstance(end, int) or isinstance(end, bool) or end < 1:
        return "proposal.end_line must be a positive integer"
    if end < start:
        return "proposal.end_line ({}) is before proposal.start_line ({})".format(end, start)
    replacement = getattr(proposal, "replacement", None)
    if not isinstance(replacement, str):
        return "proposal.replacement must be a string"
    return None


def apply_repair(workspace, proposal, source_file=None) -> AppliedRepair:
    """Apply one `RepairProposal` inside `workspace`, never touching the
    real file it came from.

    `source_file` overrides `proposal.file` for locating the real file to
    copy in (defaults to `proposal.file`); either way, only the workspace's
    own copy is ever opened for writing.

    The first `apply_repair` call for a given file copies it into
    `workspace` and patches that copy. A later call naming the same file
    reuses the existing (possibly already-patched) workspace copy rather
    than re-copying the pristine original - this is what makes "multiple
    repairs applied sequentially within the same temporary workspace" work:
    each call's `start_line`/`end_line` is interpreted against whatever that
    file currently contains in the workspace at the time of the call. A
    caller applying two repairs to the same file is responsible for giving
    each proposal line numbers that are correct for the state it is applied
    against - recomputing that automatically would mean re-analyzing, which
    is explicitly a later phase (out of scope here).

    Returns an `AppliedRepair` - never raises, for every required failure
    mode: a malformed proposal, a missing/unreadable source file, or a line
    range that does not fit the file's real line count.
    """
    try:
        if workspace is None or getattr(workspace, "root", None) is None:
            return AppliedRepair(ok=False, proposal=proposal, error="no workspace available")

        shape_error = _proposal_shape_error(proposal)
        if shape_error is not None:
            return AppliedRepair(ok=False, proposal=proposal, error=shape_error)

        original_path = Path(source_file if source_file is not None else proposal.file)
        key = str(original_path.resolve())

        if key in workspace.files:
            workspace_path = workspace.files[key]
            raw = workspace_path.read_bytes()
            encoding = _detect_encoding(raw)
            text = raw.decode(encoding)
            newline = _detect_newline(text)
        else:
            if not original_path.is_file():
                return AppliedRepair(
                    ok=False, proposal=proposal,
                    error="source file not found: '{}'".format(original_path),
                )
            _raw, text, encoding, newline = _read_source(original_path)
            workspace_path = _copy_into_workspace(workspace, original_path)
            workspace.files[key] = workspace_path

        lines = text.splitlines()
        start_line, end_line = proposal.start_line, proposal.end_line
        if end_line > len(lines):
            return AppliedRepair(
                ok=False, proposal=proposal,
                error="line range {}-{} is outside the file's {} line(s)".format(
                    start_line, end_line, len(lines)),
            )

        replacement_lines = proposal.replacement.splitlines()
        new_lines = lines[:start_line - 1] + replacement_lines + lines[end_line:]
        patched_text = newline.join(new_lines)
        if text.endswith(("\n", "\r")) and new_lines:
            patched_text += newline

        workspace_path.write_bytes(patched_text.encode(encoding))

        return AppliedRepair(
            ok=True, proposal=proposal, workspace_file=workspace_path,
            original_text=text, patched_text=patched_text,
        )
    except Exception as exc:  # noqa: BLE001 - patch application must never crash a caller
        return AppliedRepair(ok=False, proposal=proposal, error="{}: {}".format(type(exc).__name__, exc))
