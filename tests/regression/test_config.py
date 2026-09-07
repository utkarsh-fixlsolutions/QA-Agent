"""The config system itself: discovery, loading, validation
(docs/16-configuration-system.md). Pure unit tests - no live tool needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.config import DEFAULT_CONFIG, ConfigError, discover, load, resolve  # noqa: E402

KNOWN = ["ruff", "eslint"]


def test_discover_finds_nothing_by_default(suite):
    with TempProject() as root:
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("", encoding="utf-8")
        suite.check("no config anywhere -> None", discover(root / "src") is None)


def test_discover_finds_config_at_the_start_directory(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text("{}", encoding="utf-8")
        found = discover(root)
        suite.check("found at the exact starting directory", found == root / ".qa-agent.json")


def test_discover_walks_upward_and_takes_the_nearest(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text('{"ignore": ["far"]}', encoding="utf-8")
        nested = root / "a" / "b" / "c"
        nested.mkdir(parents=True)
        (root / "a" / ".qa-agent.json").write_text('{"ignore": ["near"]}', encoding="utf-8")
        found = discover(nested)
        suite.check("nearest config wins, not the topmost", found == root / "a" / ".qa-agent.json")


def test_resolve_with_no_config_is_the_defaults(suite):
    with TempProject() as root:
        cfg = resolve(root, known_analyzers=KNOWN)
        suite.check("resolve() with nothing found returns DEFAULT_CONFIG", cfg is DEFAULT_CONFIG)
        suite.check("analyzers is None (= all)", cfg.analyzers is None)
        suite.check("no extra ignore/include", cfg.extra_ignore == frozenset() and
                    cfg.include == frozenset())
        suite.check("no severity floor", cfg.min_severity is None)
        suite.check("no path", cfg.path is None)


def test_resolve_explicit_path_bypasses_discovery(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text('{"ignore": ["nope"]}', encoding="utf-8")
        elsewhere = root / "elsewhere.json"
        elsewhere.write_text('{"ignore": ["yes"]}', encoding="utf-8")
        cfg = resolve(root, explicit_path=elsewhere, known_analyzers=KNOWN)
        suite.check("the explicit file was used, not the discovered one",
                    cfg.extra_ignore == frozenset({"yes"}))


def test_load_valid_config_all_fields(suite):
    with TempProject() as root:
        target = root / "c.json"
        target.write_text(
            '{"analyzers": ["ruff"], "ignore": ["build"], "include": ["node_modules"], '
            '"min_severity": "error"}',
            encoding="utf-8",
        )
        cfg = load(target, KNOWN)
        suite.check("analyzers parsed", cfg.analyzers == frozenset({"ruff"}))
        suite.check("ignore parsed", cfg.extra_ignore == frozenset({"build"}))
        suite.check("include parsed", cfg.include == frozenset({"node_modules"}))
        suite.check("min_severity parsed", cfg.min_severity == "error")
        suite.check("path recorded", cfg.path == target)


def test_load_missing_fields_default_per_field(suite):
    with TempProject() as root:
        target = root / "c.json"
        target.write_text('{"ignore": ["build"]}', encoding="utf-8")
        cfg = load(target, KNOWN)
        suite.check("only ignore was set", cfg.extra_ignore == frozenset({"build"}))
        suite.check("analyzers still defaults to None (all)", cfg.analyzers is None)
        suite.check("include still defaults to empty", cfg.include == frozenset())
        suite.check("min_severity still defaults to None", cfg.min_severity is None)


def test_load_rejects_malformed_json(suite):
    with TempProject() as root:
        target = root / "c.json"
        target.write_text("{not json", encoding="utf-8")
        try:
            load(target, KNOWN)
            suite.check("malformed JSON raises ConfigError", False)
        except ConfigError as exc:
            suite.check("malformed JSON raises ConfigError", True)
            suite.check("names the file", str(target) in str(exc))


def test_load_rejects_non_object_top_level(suite):
    with TempProject() as root:
        target = root / "c.json"
        target.write_text("[1, 2, 3]", encoding="utf-8")
        try:
            load(target, KNOWN)
            suite.check("a JSON array at the top level is rejected", False)
        except ConfigError:
            suite.check("a JSON array at the top level is rejected", True)


def test_load_rejects_unknown_key(suite):
    with TempProject() as root:
        target = root / "c.json"
        target.write_text('{"analyzer": ["ruff"]}', encoding="utf-8")  # typo: no 's'
        try:
            load(target, KNOWN)
            suite.check("an unrecognized key is rejected, not silently ignored", False)
        except ConfigError as exc:
            suite.check("an unrecognized key is rejected, not silently ignored", True)
            suite.check("names the bad key", "analyzer" in str(exc))


def test_load_rejects_wrong_type(suite):
    with TempProject() as root:
        target = root / "c.json"
        target.write_text('{"analyzers": "ruff"}', encoding="utf-8")  # string, not a list
        try:
            load(target, KNOWN)
            suite.check("a non-list value for a list field is rejected", False)
        except ConfigError:
            suite.check("a non-list value for a list field is rejected", True)


def test_load_rejects_unknown_analyzer_name(suite):
    """The single most important validation rule (docs/16 section 9): a typo
    in an analyzer name must never silently result in zero analysis.
    """
    with TempProject() as root:
        target = root / "c.json"
        target.write_text('{"analyzers": ["ruf"]}', encoding="utf-8")  # typo: no 'f'
        try:
            load(target, KNOWN)
            suite.check("an unknown analyzer name is rejected, not silently accepted", False)
        except ConfigError as exc:
            suite.check("an unknown analyzer name is rejected, not silently accepted", True)
            suite.check("names the bad analyzer", "ruf" in str(exc))


def test_load_rejects_invalid_min_severity(suite):
    with TempProject() as root:
        target = root / "c.json"
        target.write_text('{"min_severity": "critical"}', encoding="utf-8")  # not a real level
        try:
            load(target, KNOWN)
            suite.check("an unrecognized severity level is rejected", False)
        except ConfigError:
            suite.check("an unrecognized severity level is rejected", True)


def test_load_missing_file_is_a_config_error(suite):
    with TempProject() as root:
        try:
            load(root / "does_not_exist.json", KNOWN)
            suite.check("a genuinely missing file (e.g. race after discovery) raises", False)
        except ConfigError:
            suite.check("a genuinely missing file (e.g. race after discovery) raises", True)


if __name__ == "__main__":
    suite = Suite("Config system: discovery, loading, validation")
    sys.exit(suite.run([
        test_discover_finds_nothing_by_default,
        test_discover_finds_config_at_the_start_directory,
        test_discover_walks_upward_and_takes_the_nearest,
        test_resolve_with_no_config_is_the_defaults,
        test_resolve_explicit_path_bypasses_discovery,
        test_load_valid_config_all_fields,
        test_load_missing_fields_default_per_field,
        test_load_rejects_malformed_json,
        test_load_rejects_non_object_top_level,
        test_load_rejects_unknown_key,
        test_load_rejects_wrong_type,
        test_load_rejects_unknown_analyzer_name,
        test_load_rejects_invalid_min_severity,
        test_load_missing_file_is_a_config_error,
    ]))
