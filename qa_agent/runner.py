"""Input handling -> adapter dispatch -> tool invocation -> findings.

Scope is always given by the caller (docs/01-definition.md section 4): explicit
files, or a directory to walk. The agent never decides on its own to scan more.
"""

from __future__ import annotations

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


@dataclass
class RunResult:
    checked: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # (path, reason)
    missing: list = field(default_factory=list)  # input paths that do not exist
    findings: list = field(default_factory=list)
    tools_used: list = field(default_factory=list)


def collect_paths(inputs):
    """Expand caller-given inputs into a concrete file list."""
    files, missing = [], []
    for raw in inputs:
        path = Path(raw)
        if not path.exists():
            missing.append(raw)
        elif path.is_dir():
            files.extend(_walk(path))
        else:
            files.append(path)
    return files, missing


def _walk(root):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _invoke(adapter, files):
    command = adapter.build_command([str(f) for f in files])
    try:
        proc = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ToolError("'{}' is not installed or not on PATH".format(adapter.name)) from exc

    # --exit-zero means findings alone do not produce a nonzero exit, so a
    # nonzero code here is a genuine tool failure, not code issues.
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise ToolError(
            "'{}' exited with code {}: {}".format(adapter.name, proc.returncode, detail)
        )
    return adapter.parse(proc.stdout)


def run(inputs, extra_skipped=None):
    """Check the given paths and return everything the report needs.

    extra_skipped: (path, reason) pairs the caller already knows cannot be
    checked - e.g. files git reports as deleted. They are reported, not dropped.
    """
    files, missing = collect_paths(inputs)

    batches, skipped = {}, []
    for path in files:
        adapter = ADAPTERS.get(path.suffix.lower())
        if adapter is None:
            reason = "no tool configured for '{}'".format(path.suffix or "no extension")
            skipped.append((path, reason))
        else:
            batches.setdefault(adapter.name, (adapter, []))[1].append(path)

    result = RunResult(skipped=list(extra_skipped or []) + skipped, missing=missing)
    for name, (adapter, adapter_files) in sorted(batches.items()):
        result.tools_used.append(name)
        result.checked.extend(adapter_files)
        for chunk in _chunks(adapter_files, MAX_FILES_PER_CALL):
            result.findings.extend(_invoke(adapter, chunk))
    return result
