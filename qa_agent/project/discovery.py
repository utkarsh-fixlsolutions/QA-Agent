"""The Project Discovery Engine's one public entry point (Phase F Part 1,
docs/19-project-discovery-engine.md): `discover_project(root)`.

This is a deterministic foundation for future AI reasoning (Phase G onward),
not reasoning itself. Nothing in this package - this module included -
imports anything from `qa_agent.ai`, calls a subprocess, opens a network
connection, or drives a browser. A fact only ever enters `ProjectKnowledge`
if a real file on disk proves it (`detectors.py`); if it cannot be proven,
it does not appear.

The filesystem is walked exactly once, top-down, with every entry in
`IGNORED_DIR_NAMES` pruned from `os.walk`'s own `dirnames` list *before* it
descends - so an ignored directory's contents are never even listed, not
merely discarded afterward (the actual performance requirement behind
"walk the filesystem only once" and "scale to hundreds of thousands of
files": pruning happens at the `os.walk` level itself, not in a second pass
over its output).

`discover_project()` never raises to its caller. Every failure mode -
a root that does not exist, a root that cannot even be listed, or a
genuinely unexpected internal error - becomes a `ProjectDiscoveryResult`
with a `status` naming exactly what happened, matching this project's
established "never invent success, never crash a caller, always say
honestly what went wrong" discipline (runner.py's `ToolError` isolation,
qa_agent.ai's uniform "return None/empty on failure" contract).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ..runner import IGNORED_DIRS as _ANALYZER_IGNORED_DIRS
from .knowledge import build_knowledge
from .models import (
    STATUS_DISCOVERY_FAILED,
    STATUS_INVALID_ROOT,
    STATUS_PARTIAL_SUCCESS,
    STATUS_PERMISSION_DENIED,
    STATUS_SUCCESS,
    ProjectDiscoveryResult,
)

# Reuses and extends runner.py's own ignore set (docs/19's explicit
# instruction) rather than maintaining a second, silently-drifting copy -
# .next is the one directory this project has already been bitten by once
# for real (a live scan of an external Next.js project chose to feed
# hundreds of compiled build-output files to eslint and the AI layer before
# .next was ever excluded anywhere). Extended here with the other common
# build-output/cache/dependency directories the same failure mode could
# recur for in other ecosystems.
IGNORED_DIR_NAMES = _ANALYZER_IGNORED_DIRS | {
    ".next",
    ".nuxt",
    "dist",
    "build",
    "coverage",
    "target",
    "out",
    "bin",
    "obj",
    ".cache",
    ".pytest_cache",
    ".eslintcache",
    ".idea",
    ".vscode",
}


def _prune(dirnames):
    dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIR_NAMES)


def _walk_once(resolved_root, warnings):
    """One `os.walk` pass. Returns `(files, dirs)` - sorted tuples of
    POSIX-style paths relative to `resolved_root`. `followlinks=False`
    (the default, passed explicitly here for clarity) means a symlinked
    directory is listed but never descended into, which is also exactly
    what stops a circular symlink from ever causing an infinite walk - no
    separate cycle-detection is needed. A broken symlink (its target does
    not exist) is excluded explicitly, once, below - `os.walk` itself would
    otherwise report it as an ordinary filename with no way to tell it apart
    from a real file without an extra check.
    """
    files, dirs = [], []

    def _on_error(exc):
        # A permission failure (or similar) partway through the walk -
        # os.walk continues with whatever siblings it can still reach when
        # onerror does not re-raise, so one unreadable subtree never stops
        # discovery of the rest of the repository.
        warnings.append("could not list '{}': {}".format(getattr(exc, "filename", "?"), exc))

    for dirpath, dirnames, filenames in os.walk(
        resolved_root, topdown=True, onerror=_on_error, followlinks=False
    ):
        _prune(dirnames)
        dir_path = Path(dirpath)
        if dir_path != resolved_root:
            dirs.append(dir_path.relative_to(resolved_root).as_posix())
        for fname in sorted(filenames):
            candidate = dir_path / fname
            if not candidate.exists():
                # Broken symlink (or a race where the file vanished mid-walk)
                # - docs/19's explicit "ignore broken symlinks" rule.
                continue
            files.append(candidate.relative_to(resolved_root).as_posix())

    files.sort()
    dirs.sort()
    return tuple(files), tuple(dirs)


def discover_project(root):
    """Inspect `root` and return a `ProjectDiscoveryResult`. Never raises.

    `root` may be a `str` or `os.PathLike`; anything else, or a path that
    does not resolve to a real directory, yields `STATUS_INVALID_ROOT` -
    never an exception out of this function.
    """
    started = time.perf_counter()

    try:
        candidate = Path(root)
    except TypeError as exc:
        return ProjectDiscoveryResult(
            status=STATUS_INVALID_ROOT,
            project=None,
            errors=("'{}' is not a valid path: {}".format(root, exc),),
            elapsed_time=time.perf_counter() - started,
        )

    try:
        resolved_root = candidate.resolve()
    except OSError as exc:
        return ProjectDiscoveryResult(
            status=STATUS_INVALID_ROOT,
            project=None,
            errors=("could not resolve '{}': {}".format(root, exc),),
            elapsed_time=time.perf_counter() - started,
        )

    if not resolved_root.exists():
        return ProjectDiscoveryResult(
            status=STATUS_INVALID_ROOT,
            project=None,
            errors=("'{}' does not exist".format(resolved_root),),
            elapsed_time=time.perf_counter() - started,
        )
    if not resolved_root.is_dir():
        return ProjectDiscoveryResult(
            status=STATUS_INVALID_ROOT,
            project=None,
            errors=("'{}' is not a directory".format(resolved_root),),
            elapsed_time=time.perf_counter() - started,
        )

    # Checked once, explicitly, before the real walk - so "the root itself
    # cannot be read at all" is reported as PERMISSION_DENIED, distinct from
    # a permission failure on some deeper subtree (which is recoverable and
    # becomes a warning + PARTIAL_SUCCESS below, not a hard stop).
    try:
        os.listdir(resolved_root)
    except PermissionError as exc:
        return ProjectDiscoveryResult(
            status=STATUS_PERMISSION_DENIED,
            project=None,
            errors=("permission denied listing '{}': {}".format(resolved_root, exc),),
            elapsed_time=time.perf_counter() - started,
        )
    except OSError as exc:
        return ProjectDiscoveryResult(
            status=STATUS_DISCOVERY_FAILED,
            project=None,
            errors=("could not list '{}': {}".format(resolved_root, exc),),
            elapsed_time=time.perf_counter() - started,
        )

    warnings = []
    try:
        files, dirs = _walk_once(resolved_root, warnings)
        project = build_knowledge(resolved_root, files, dirs)
    except Exception as exc:  # noqa: BLE001 - discovery must never crash a caller
        return ProjectDiscoveryResult(
            status=STATUS_DISCOVERY_FAILED,
            project=None,
            errors=("unexpected internal error: {!r}".format(exc),),
            warnings=tuple(warnings),
            elapsed_time=time.perf_counter() - started,
        )

    # ignored_files counts files that *were* visited (inside a non-pruned
    # directory) but matched no recognized category - deliberately not an
    # attempt to count every file inside a *pruned* directory, which would
    # require walking those directories after all and defeat the entire
    # point of pruning them (the "walk once, scale to hundreds of thousands
    # of files" requirement this module leads with).
    ignored_files = len(files) - len(project.important_files)
    status = STATUS_PARTIAL_SUCCESS if warnings else STATUS_SUCCESS

    return ProjectDiscoveryResult(
        status=status,
        project=project,
        warnings=tuple(warnings),
        elapsed_time=time.perf_counter() - started,
        scanned_files=len(files),
        ignored_files=ignored_files,
    )
