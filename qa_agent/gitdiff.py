"""Git-diff input mode: ask git which files changed, hand them to the same runner.

This module only decides *which files* to check. Dispatch, tool invocation, and
reporting stay exactly as Step 3 built them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .adapters import ToolError


def _git(args, cwd=None):
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    except FileNotFoundError as exc:
        raise ToolError("'git' is not installed or not on PATH") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise ToolError("git {} failed: {}".format(" ".join(args), detail))
    return proc.stdout


def _git_succeeds(args, cwd=None):
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    except FileNotFoundError as exc:
        raise ToolError("'git' is not installed or not on PATH") from exc
    return proc.returncode == 0


def repo_root():
    """Absolute path of the enclosing git repository, or a clear ToolError."""
    if not _git_succeeds(["rev-parse", "--is-inside-work-tree"]):
        raise ToolError(
            "not inside a git repository - run --git-diff from within a repo, "
            "or pass explicit paths instead"
        )
    return Path(_git(["rev-parse", "--show-toplevel"]).strip())


def _names(args, cwd):
    # -z gives NUL-separated, unquoted paths, so filenames with spaces or
    # non-ASCII characters survive intact.
    return [n for n in _git([*args, "-z"], cwd=cwd).split("\0") if n]


def changed_files(ref=None):
    """Return (existing, deleted) absolute paths for files git reports as changed.

    Default (no ref): everything not yet committed - staged and unstaged changes
    against HEAD, plus untracked files git is not ignoring. A newly written file
    is a change in every sense that matters for QA, so it is included.

    With a ref: what the working tree changed relative to that ref.
    """
    root = repo_root()
    cwd = str(root)

    if ref:
        # Check the ref first: otherwise a mistyped ref (or a path passed where a
        # ref was expected) surfaces as raw git usage text instead of a clear error.
        if not _git_succeeds(["rev-parse", "--verify", "--quiet", "{}^{{commit}}".format(ref)], cwd=cwd):
            raise ToolError("'{}' is not a valid git ref in this repository".format(ref))
        names = _names(["diff", "--name-only", ref], cwd)
    elif _git_succeeds(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=cwd):
        names = _names(["diff", "--name-only", "HEAD"], cwd)
        names += _names(["ls-files", "--others", "--exclude-standard"], cwd)
    else:
        # A repo with no commits yet: there is no HEAD to diff against.
        names = _names(["diff", "--name-only", "--cached"], cwd)
        names += _names(["ls-files", "--others", "--exclude-standard"], cwd)

    seen, existing, deleted = set(), [], []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        path = root / name
        # A deleted path cannot be checked; it is reported as skipped, not lost.
        (existing if path.is_file() else deleted).append(path)
    return sorted(existing), sorted(deleted)


def describe(ref=None):
    if ref:
        return "git changed files (working tree vs {})".format(ref)
    return "git changed files (staged + unstaged vs HEAD, plus untracked)"
