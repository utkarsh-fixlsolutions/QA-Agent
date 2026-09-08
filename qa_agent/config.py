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

_ALLOWED_KEYS = {"analyzers", "ignore", "include", "min_severity", "ai"}

# Phase D Part 6: the only providers the AI package actually implements
# (docs/step-log.md) - Ollama for real use, Mock for tests. A name outside
# this set is rejected the same way an unknown analyzer name already is:
# a typo must never silently mean "AI quietly does nothing".
_KNOWN_AI_PROVIDERS = {"ollama", "mock"}

_ALLOWED_AI_KEYS = {
    "enabled", "provider", "model", "endpoint", "timeout", "explain", "summary", "suggest_fixes",
}


class ConfigError(Exception):
    """The config file itself is unusable. Reported clearly, never guessed past."""


@dataclass(frozen=True)
class AIConfig:
    """Optional AI enrichment settings (Phase D Part 6).

    `enabled` is a master switch: `explain`/`summary`/`suggest_fixes` only
    ever run when it is also true - the same shape the example config in
    docs/step-log.md's Part 6 entry uses (all three could be `true` while
    `enabled` is `false`, and nothing would run). `model`/`endpoint`/
    `timeout` of `None` mean "use the provider's own default" - not
    duplicated here; the AI package's own provider classes already own
    those defaults, this module only ever overrides them when a project
    asks to. This module never imports that package at all - it only ever
    produces this plain data value, which `__main__.py` later reads.
    """

    enabled: bool = False
    provider: str = "ollama"
    model: object = None  # str | None
    endpoint: object = None  # str | None
    timeout: object = None  # float | None
    explain: bool = False
    summary: bool = False
    suggest_fixes: bool = False


DEFAULT_AI_CONFIG = AIConfig()


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
    ai: AIConfig = field(default_factory=AIConfig)  # Phase D Part 6, defaults to all-disabled


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
        ai=_load_ai_config(raw, path),
    )


def _string_list(raw, key, path):
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("'{}' setting '{}' must be a list of strings".format(path, key))
    return value


def _load_ai_config(raw, path):
    """Validate and parse the optional "ai" section (Phase D Part 6) - the
    same strict style as every other setting: an unrecognized key or a
    wrong-typed value is a ConfigError, never silently ignored or guessed
    past. Absent entirely, every field keeps AIConfig's own default (AI
    off) - zero-config behavior is unaffected either way.
    """
    if "ai" not in raw:
        return DEFAULT_AI_CONFIG

    ai_raw = raw["ai"]
    if not isinstance(ai_raw, dict):
        raise ConfigError("'{}' setting 'ai' must be a JSON object".format(path))

    unknown = set(ai_raw) - _ALLOWED_AI_KEYS
    if unknown:
        raise ConfigError(
            "'{}' has unrecognized 'ai' setting(s): {}".format(path, ", ".join(sorted(unknown)))
        )

    provider = ai_raw.get("provider", DEFAULT_AI_CONFIG.provider)
    if not isinstance(provider, str) or provider not in _KNOWN_AI_PROVIDERS:
        raise ConfigError(
            "'{}' has an invalid 'ai.provider' {!r} (must be one of: {})".format(
                path, provider, ", ".join(sorted(_KNOWN_AI_PROVIDERS))
            )
        )

    timeout = ai_raw.get("timeout")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
    ):
        raise ConfigError(
            "'{}' has an invalid 'ai.timeout' {!r} (must be a positive number)".format(
                path, timeout
            )
        )

    return AIConfig(
        enabled=_ai_bool(ai_raw, "enabled", path),
        provider=provider,
        model=_ai_string(ai_raw, "model", path),
        endpoint=_ai_string(ai_raw, "endpoint", path),
        timeout=timeout,
        explain=_ai_bool(ai_raw, "explain", path),
        summary=_ai_bool(ai_raw, "summary", path),
        suggest_fixes=_ai_bool(ai_raw, "suggest_fixes", path),
    )


def _ai_bool(raw, key, path):
    if key not in raw:
        return False
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError("'{}' setting 'ai.{}' must be true or false".format(path, key))
    return value


def _ai_string(raw, key, path):
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str):
        raise ConfigError("'{}' setting 'ai.{}' must be a string".format(path, key))
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
