"""Tool adapters: each one builds a command and parses that tool's output.

docs/02-tool-selection.md, docs/13-multi-analyzer-foundation.md (the registry),
and docs/14-first-multi-language-analyzer.md (exit codes and working
directory) define the contract every adapter satisfies: a `name`, the file
`extensions` it claims, the `ok_exit_codes` that mean "the tool ran, parse its
output" rather than "the tool failed", a `build_command` for a batch of
files, and a `parse` of that tool's raw output into the shared finding shape
{file, line, severity, message, tool}. Nothing outside this module needs to
know which language or tool produced a finding, which extensions belong to
which tool, or which exit codes are normal for it.

`ADAPTERS` is a tuple of adapters rather than an extension-keyed dict, so more
than one adapter can claim the same extension without changing anything
outside this file. Registers five adapters: ruff, pyright, and mypy on .py,
eslint on .js, shellcheck on .sh (docs/step-log.md, Phase C Parts 5 and 7) -
ruff+pyright was the first real (not fake-adapter-only) exercise of two tools
sharing an extension, mypy is the first exercise of three, and shellcheck is
the first adapter for a language none of the others touch at all. None of
them needed any change to this module's contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """One real issue reported by a tool. Never constructed from anything else."""

    file: str
    line: int
    severity: str
    message: str
    tool: str


class ToolError(Exception):
    """The tool itself failed to run. Reported as a tool error, never as a finding."""


# Optional severity policy, owned by us rather than by the tool.
#
# Empty by default, which means every finding keeps the severity the tool itself
# reported (docs/02-tool-selection.md section 3 - no invented severities). Add
# entries here to classify specific ruff rules yourself, e.g.:
#     SEVERITY_POLICY = {"E711": "warning", "F821": "error"}
# Keys are exact ruff rule codes; prefix matching is deliberately not attempted,
# so any reclassification stays explicit and auditable.
SEVERITY_POLICY = {}  # type: dict


class RuffAdapter:
    """Adapter for ruff (docs/02-tool-selection.md sections 2-3)."""

    name = "ruff"
    extensions = frozenset({".py"})
    # --exit-zero (below) already guarantees ruff only ever exits 0; this just
    # states that fact for the runner's generic exit-code check to consume.
    ok_exit_codes = frozenset({0})
    # ruff.exe is a real Windows executable; no shell needed to launch it.
    use_shell = False

    def build_command(self, files):
        # Exactly the command shape documented in Step 2.
        return ["ruff", "check", *files, "--output-format=json", "--exit-zero"]

    def parse(self, stdout):
        if not stdout.strip():
            return []
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ToolError("ruff produced output that is not valid JSON: {}".format(exc)) from exc

        findings = []
        for item in raw:
            code = item.get("code") or ""
            message = "{}: {}".format(code, item["message"]) if code else item["message"]
            findings.append(
                Finding(
                    file=item["filename"],
                    line=item["location"]["row"],
                    severity=SEVERITY_POLICY.get(code, item.get("severity", "unknown")),
                    message=message,
                    tool=self.name,
                )
            )
        return findings


# ESLint's own severity scale, decoded into words - not a reclassification
# (that's what SEVERITY_POLICY is for), just spelling out what the tool's
# integers already mean, the same way ruff's string severity is used as-is.
_ESLINT_SEVERITY_NAMES = {1: "warning", 2: "error"}


class ESLintAdapter:
    """Adapter for ESLint (docs/14-first-multi-language-analyzer.md).

    .js only for now - TypeScript needs its own parser/plugin and its own
    due diligence, deliberately deferred rather than bundled in here.

    Unlike ruff, a nonzero exit does not mean the tool failed: exit 1 is
    ESLint's normal way of saying "there are errors in the code" (it has no
    --exit-zero equivalent), so ok_exit_codes includes it. Exit 2 (a missing
    config, a fatal crash) is a real failure and still raises a ToolError
    through the runner's generic handling.

    npm installs eslint as a `.cmd` shim on Windows, not a real executable -
    confirmed by testing directly: subprocess.run(["eslint", ...]) raises
    FileNotFoundError even when eslint.cmd genuinely resolves on PATH,
    because Windows can only launch a .cmd/.bat file through a shell. Hence
    use_shell = True, unlike ruff's real .exe.
    """

    name = "eslint"
    extensions = frozenset({".js"})
    ok_exit_codes = frozenset({0, 1})
    use_shell = True

    def build_command(self, files):
        return ["eslint", "--format=json", *files]

    def parse(self, stdout):
        if not stdout.strip():
            return []
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ToolError("eslint produced output that is not valid JSON: {}".format(exc)) from exc

        findings = []
        for file_result in raw:
            filename = file_result["filePath"]
            for item in file_result["messages"]:
                # ruleId is null for a fatal parse error and for ESLint's own
                # "file ignored" advisories - both fall back to the bare
                # message, exactly like ruff's adapter does for a missing code.
                rule_id = item.get("ruleId") or ""
                message = "{}: {}".format(rule_id, item["message"]) if rule_id else item["message"]
                findings.append(
                    Finding(
                        file=filename,
                        # A file-level advisory (e.g. "ignored because it's
                        # under node_modules") carries no line at all -
                        # verified against real eslint output. Line 1 is a
                        # harmless, honest placeholder for "the whole file",
                        # not a fabricated location.
                        line=item.get("line", 1),
                        severity=_ESLINT_SEVERITY_NAMES.get(item["severity"], "unknown"),
                        message=message,
                        tool=self.name,
                    )
                )
        return findings


def _normalize_drive_letter(path):
    """Windows only: pyright reports its own "file" paths with a lowercase
    drive letter ("c:\\Users\\...") - a Language Server Protocol/VSCode URI
    convention leaking into its plain-path JSON, confirmed by its own
    "missing file" error naming the path as a file:// URI with %3A-encoded
    lowercase c. Every other adapter (ruff, eslint, shellcheck) echoes back
    whatever case it was given. Left alone, this silently breaks Part 3's
    "sorted by file" guarantee - found by testing a real mixed-language
    project, not by inspection: pyright's own findings sorted after every
    other tool's, on every file, because "c" > "C" in a plain string
    comparison, regardless of the actual filename that followed.
    """
    if len(path) >= 2 and path[1] == ":" and path[0].islower():
        return path[0].upper() + path[1:]
    return path


class PyrightAdapter:
    """Adapter for Pyright, a Python type checker (Phase C Part 5).

    A second .py analyzer alongside ruff - the first real exercise (Part 1's
    fake-adapter tests aside) of more than one adapter claiming the same
    extension, and of Part 3's merge/sort/dedup across two tools that look
    at the same files.

    Installed into qa_agent's own venv and pinned in requirements.txt, like
    ruff - not treated as a target-project dependency like eslint. Verified
    directly: importing a real third-party package qa_agent's own venv does
    not have (requests) produced no false diagnostic at all (pyright ships
    typeshed stubs for well-known packages), while a genuinely nonexistent
    import correctly did - real signal either way, not cross-venv noise.

    No --exit-zero equivalent (confirmed via --help): exit 0 clean, 1 an
    error was found, 4 (and others) a genuine tool failure - the same "0 and
    1 both mean parse my output" shape ESLint already established.
    """

    name = "pyright"
    extensions = frozenset({".py"})
    ok_exit_codes = frozenset({0, 1})
    use_shell = False

    def build_command(self, files):
        return ["pyright", "--outputjson", *files]

    def parse(self, stdout):
        if not stdout.strip():
            return []
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ToolError(
                "pyright produced output that is not valid JSON: {}".format(exc)
            ) from exc

        findings = []
        for item in raw.get("generalDiagnostics", []):
            rule = item.get("rule") or ""
            message = "{}: {}".format(rule, item["message"]) if rule else item["message"]
            findings.append(
                Finding(
                    file=_normalize_drive_letter(item["file"]),
                    # Pyright's own line numbers are 0-indexed - verified
                    # against real output (a known line-2 error reported as
                    # "line": 1). +1 to match every other adapter's 1-indexed
                    # Finding.line.
                    line=item["range"]["start"]["line"] + 1,
                    severity=item.get("severity", "unknown"),
                    message=message,
                    tool=self.name,
                )
            )
        return findings


class ShellCheckAdapter:
    """Adapter for ShellCheck, a shell script analyzer (Phase C Part 5).

    Claims .sh - a genuinely new extension none of the other three adapters
    touch, unlike pyright's shared .py.

    Not pip/npm-installable - a standalone Haskell binary. Fetched directly
    from its GitHub release for testing (tests/fixtures/shellcheck) since no
    non-interactive, admin-free package manager path was available on this
    machine; a real user's PATH is their own responsibility, exactly like
    ruff's and eslint's already are.

    No --exit-zero equivalent (confirmed via --help): exit 0 clean, 1
    findings present (verified: even a lone lowest-severity "info" finding
    still causes exit 1), 2 a genuine tool failure (e.g. a missing file).
    """

    name = "shellcheck"
    extensions = frozenset({".sh"})
    ok_exit_codes = frozenset({0, 1})
    use_shell = False

    def build_command(self, files):
        return ["shellcheck", "--format=json", *files]

    def parse(self, stdout):
        if not stdout.strip():
            return []
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ToolError(
                "shellcheck produced output that is not valid JSON: {}".format(exc)
            ) from exc

        # Unlike ruff/eslint/pyright, shellcheck's JSON is already a flat
        # list of findings - no per-file grouping to unwrap, and a clean
        # file simply contributes no entries at all.
        findings = []
        for item in raw:
            code = item.get("code")
            rule = "SC{}".format(code) if code is not None else ""
            message = "{}: {}".format(rule, item["message"]) if rule else item["message"]
            findings.append(
                Finding(
                    file=item["file"],
                    line=item["line"],
                    severity=item.get("level", "unknown"),
                    message=message,
                    tool=self.name,
                )
            )
        return findings


class MypyAdapter:
    """Adapter for Mypy, a second Python type checker (Phase C Part 7).

    A third .py adapter alongside ruff and pyright - proves the "N adapters
    share one extension" path (built in Part 1, first exercised for real in
    Part 5) scales past two without any further engine change.

    Installed into qa_agent's own venv and pinned in requirements.txt, like
    ruff and pyright - not a target-project dependency like eslint.

    `-O json` prints one JSON object **per line** (JSON Lines), unlike every
    other adapter here's single JSON array or object - verified directly,
    not assumed from documentation.

    Exit codes, verified directly: 0 clean, 1 real findings present - both
    handled exactly like every other adapter. Exit 2 is genuinely ambiguous
    for mypy in a way no other adapter here has: it means EITHER a real tool
    failure (a missing file, a crash - stdout empty) OR a batch containing a
    file mypy cannot even parse (a syntax error - real JSON still printed,
    but mypy stops checking every *other* file in that batch and reports
    only the syntax error, confirmed order-independent). Both look identical
    by exit code alone, so - rather than guess, or change the adapter
    contract to let `parse()` see the exit code - exit 2 is conservatively
    treated as a tool failure either way, exactly like any other adapter's
    undeclared exit code: never risking a silently invented or silently
    dropped finding. In practice a syntax error is also caught by ruff
    independently (verified), so the merged report still carries real signal
    for that file even when mypy's own contribution degrades to a tool error
    for the whole batch.
    """

    name = "mypy"
    extensions = frozenset({".py"})
    ok_exit_codes = frozenset({0, 1})
    use_shell = False

    def build_command(self, files):
        # --show-absolute-path: without it, mypy's own "file" field is
        # inconsistent even within one invocation - verified directly, not
        # assumed. Given several already-absolute input files, mypy reports
        # some of them relative to its own cwd (whichever it judges
        # reachable that way) and others absolute (whichever it does not) -
        # confirmed with a real multi-file batch and again on a real
        # external repository (flask, Phase C Part 7). Left alone, this
        # would silently break Part 3's "sorted by file" guarantee exactly
        # like Part 5's pyright lowercase-drive-letter bug did: the same
        # file's findings from mypy and from ruff/pyright would sort into
        # different positions in the merged report. The flag makes mypy's
        # own output consistent, so no adapter-side post-processing (e.g. a
        # normalizer like PyrightAdapter's) is needed at all.
        return ["mypy", "-O", "json", "--show-absolute-path", *files]

    def parse(self, stdout):
        if not stdout.strip():
            return []
        findings = []
        for line_number, line in enumerate(stdout.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ToolError(
                    "mypy produced a line that is not valid JSON (line {}): {}".format(
                        line_number, exc
                    )
                ) from exc
            code = item.get("code") or ""
            message = "{}: {}".format(code, item["message"]) if code else item["message"]
            hint = item.get("hint")
            if hint:
                # Mypy's own supplementary suggestion (e.g. narrowing advice)
                # - genuinely useful, not invented, so it is kept rather than
                # dropped like every other adapter's optional fields.
                message = "{} (hint: {})".format(message, hint)
            findings.append(
                Finding(
                    file=item["file"],
                    line=item["line"],
                    severity=item.get("severity", "unknown"),
                    message=message,
                    tool=self.name,
                )
            )
        return findings


# Registered adapters. Each declares its own `extensions`, so registering a
# second adapter for an extension already claimed (e.g. a type checker
# alongside ruff on .py) needs no change outside this file.
ADAPTERS = (RuffAdapter(), ESLintAdapter(), PyrightAdapter(), ShellCheckAdapter(), MypyAdapter())
