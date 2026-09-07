"""Per-project configuration: .qa-agent.json (docs/16-configuration-system.md).

Zero configuration required: when no file is found, resolve() returns
DEFAULT_CONFIG and every existing behavior is unchanged. Discovery walks
upward from a starting directory, nearest file wins - the same precedent
ESLint's own config discovery already established (docs/14). A config file
that can't be trusted is never partially applied: any problem raises
ConfigError immediately, the same "give one clear error, never guess"
precedent gitdiff.py's ToolError already set for a broken input source.

This module knows nothing about the runner's dispatch loop or reporting -
callers get back a plain Config value and decide what to do with it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = ".qa-agent.json"

# Basic severity filtering (Part 4). Extended in Part 5 for shellcheck's
# four-level scale and pyright's "information" spelling of the same concept
# shellcheck calls "info", and again in Part 7 for mypy's "note" - see
# runner.py's own copy for the full comment. Duplicated, not imported, in
# runner.py - the same small, explicitly-flagged duplication already
# accepted for fsmonitor.py's ignore list vs. runner.py's IGNORED_DIRS
# (docs/step-log.md, Phase B Part 2): a handful of entries is cheaper to
# keep in sync by hand than to justify a cross-module import for.
SEVERITY_LEVELS = {"style": 1, "note": 1, "info": 2, "information": 2, "warning": 3, "error": 4}

_ALLOWED_KEYS = {"analyzers", "ignore", "include", "min_severity"}


class ConfigError(Exception):
    """The config file itself is unusable. Reported clearly, never guessed past."""


@dataclass(frozen=True)
class Config:
    """What one project's config resolved to - or the defaults, when there was
    no file at all. `analyzers=None` means "every registered adapter", not
    "none" - the only way to genuinely disable one is to name the rest.
    """

    analyzers: object = None  # frozenset of adapter names, or None = all of them
    extra_ignore: frozenset = field(default_factory=frozenset)
    include: frozenset = field(default_factory=frozenset)
    min_severity: object = None  # "warning" | "error" | None
    path: object = None  # Path this was loaded from, or None for the defaults


DEFAULT_CONFIG = Config()


def discover(start_dir):
    """The nearest .qa-agent.json found walking upward from start_dir, or None."""
    current = Path(start_dir).resolve()
    if current.is_file():
        current = current.parent
    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load(path, known_analyzers):
    """Read, parse, and validate one config file into a Config.

    known_analyzers: the real, currently-registered adapter names - an
    `analyzers` entry that doesn't match one of these is rejected outright
    (a typo must never silently result in zero analysis, docs/16 section 9).
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("could not read '{}': {}".format(path, exc)) from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("'{}' is not valid JSON: {}".format(path, exc)) from exc

    if not isinstance(raw, dict):
        raise ConfigError("'{}' must be a JSON object".format(path))

    unknown_keys = set(raw) - _ALLOWED_KEYS
    if unknown_keys:
        raise ConfigError(
            "'{}' has unrecognized setting(s): {}".format(path, ", ".join(sorted(unknown_keys)))
        )

    analyzers = _string_list(raw, "analyzers", path)
    if analyzers is not None:
        unknown_analyzers = set(analyzers) - set(known_analyzers)
        if unknown_analyzers:
            raise ConfigError(
                "'{}' names unknown analyzer(s): {} (registered: {})".format(
                    path,
                    ", ".join(sorted(unknown_analyzers)),
                    ", ".join(sorted(known_analyzers)) or "none",
                )
            )
        analyzers = frozenset(analyzers)

    ignore = frozenset(_string_list(raw, "ignore", path) or [])
    include = frozenset(_string_list(raw, "include", path) or [])

    min_severity = raw.get("min_severity")
    if min_severity is not None and (
        not isinstance(min_severity, str) or min_severity not in SEVERITY_LEVELS
    ):
        raise ConfigError(
            "'{}' has an invalid min_severity {!r} (must be one of: {})".format(
                path, min_severity, ", ".join(sorted(SEVERITY_LEVELS))
            )
        )

    return Config(
        analyzers=analyzers,
        extra_ignore=ignore,
        include=include,
        min_severity=min_severity,
        path=path,
    )


def _string_list(raw, key, path):
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("'{}' setting '{}' must be a list of strings".format(path, key))
    return value


def resolve(start_dir, explicit_path=None, known_analyzers=()):
    """The whole lifecycle in one call.

    An explicit path always wins over discovery. Failing that, the nearest
    .qa-agent.json found walking upward from start_dir. Failing that,
    DEFAULT_CONFIG - every existing behavior, unchanged (docs/16 section 14).
    """
    if explicit_path is not None:
        return load(explicit_path, known_analyzers)
    found = discover(start_dir)
    if found is None:
        return DEFAULT_CONFIG
    return load(found, known_analyzers)
