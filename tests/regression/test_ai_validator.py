"""Repair validation (Phase E Part 3): validator.py.

Pure unit tests, no network, no live LLM, no real analyzer subprocess -
`validate_repair`'s own `run` parameter is injected with a fake, matching
this project's established dependency-injection convention for testing AI
modules (`extract=...` in repair.py, `extract=...` in fixer.py). Real
`Finding`, `RepairProposal`, and `AppliedRepair` instances are used
throughout so these tests exercise the real production types.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite, TempProject  # noqa: E402

from qa_agent.adapters import Finding, ToolError  # noqa: E402
from qa_agent.ai.repair import RepairProposal  # noqa: E402
from qa_agent.ai.workspace import AppliedRepair  # noqa: E402
from qa_agent.ai.validator import (  # noqa: E402
    STATUS_IMPROVED,
    STATUS_UNCHANGED,
    STATUS_VALIDATION_FAILED,
    STATUS_WORSENED,
    RepairValidationResult,
    ValidationComparison,
    compare_results,
    validate_repair,
)


class _Result:
    """A plain RunResult-shaped stand-in - only `.findings`/`.tool_errors`
    are ever read by validator.py.
    """

    def __init__(self, findings=(), tool_errors=()):
        self.findings = list(findings)
        self.tool_errors = list(tool_errors)


def _finding(file="a.py", line=1, severity="error", message="F401: unused", tool="ruff"):
    return Finding(file=file, line=line, severity=severity, message=message, tool=tool)


def _proposal(finding, file=None):
    return RepairProposal(
        finding=finding, explanation="why", replacement="fixed", confidence=0.9,
        model="mock", file=file if file is not None else finding.file,
        start_line=finding.line, end_line=finding.line,
    )


def _real_workspace_file(tmp_path_holder):
    """A real file on disk - validate_repair checks Path(workspace_file).is_file()."""
    d = Path(tempfile.mkdtemp(prefix="qa-validator-test-"))
    tmp_path_holder.append(d)
    f = d / "copy.py"
    f.write_text("x = 1\n", encoding="utf-8", newline="")
    return f


def _fake_run(after_findings=(), after_tool_errors=(), capture=None):
    def run(inputs, config=None):
        if capture is not None:
            capture.append({"inputs": list(inputs), "config": config})
        return _Result(findings=after_findings, tool_errors=after_tool_errors)
    return run


def _cleanup(dirs):
    import shutil
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


# --- compare_results: pure comparison math ----------------------------------


def test_compare_results_finding_removed_is_counted(suite):
    before = [_finding(line=1, message="F401: unused")]
    comparison = compare_results(before, [])
    suite.check("a ValidationComparison is returned", isinstance(comparison, ValidationComparison))
    suite.check("removed_count is 1", comparison.removed_count == 1)
    suite.check("introduced_count is 0", comparison.introduced_count == 0)
    suite.check("the removed finding itself is included", comparison.removed_findings == tuple(before))


def test_compare_results_finding_unchanged_is_not_counted_as_removed_or_introduced(suite):
    finding = _finding(line=1, message="F401: unused")
    comparison = compare_results([finding], [finding])
    suite.check("nothing removed", comparison.removed_count == 0)
    suite.check("nothing introduced", comparison.introduced_count == 0)
    suite.check("one unchanged", comparison.unchanged_count == 1)


def test_compare_results_new_finding_is_introduced(suite):
    new_finding = _finding(line=2, message="F841: unused variable")
    comparison = compare_results([], [new_finding])
    suite.check("introduced_count is 1", comparison.introduced_count == 1)
    suite.check("the introduced finding itself is included",
                comparison.introduced_findings == (new_finding,))


def test_compare_results_matches_across_different_file_paths_by_basename(suite):
    """The documented match rule: file is normalized to its basename, since
    "before" (the real project) and "after" (a workspace copy) are always
    in different locations for the same logical file.
    """
    before_finding = _finding(file=r"C:\real\project\a.py", line=3, tool="ruff", message="F401: unused")
    after_finding = _finding(file=r"C:\Temp\workspace\f0\a.py", line=3, tool="ruff", message="F401: unused")
    comparison = compare_results([before_finding], [after_finding])
    suite.check("matched despite different directories", comparison.unchanged_count == 1)
    suite.check("not counted as removed", comparison.removed_count == 0)
    suite.check("not counted as introduced", comparison.introduced_count == 0)


def test_compare_results_does_not_match_a_different_line(suite):
    before_finding = _finding(line=5, message="F401: unused")
    after_finding = _finding(line=6, message="F401: unused")
    comparison = compare_results([before_finding], [after_finding])
    suite.check("a different line is a genuinely different finding, not a match",
                comparison.removed_count == 1 and comparison.introduced_count == 1)


def test_compare_results_does_not_match_a_different_tool(suite):
    before_finding = _finding(line=1, tool="ruff", message="same message")
    after_finding = _finding(line=1, tool="pyright", message="same message")
    comparison = compare_results([before_finding], [after_finding])
    suite.check("a different tool is a genuinely different finding, not a match",
                comparison.removed_count == 1 and comparison.introduced_count == 1)


def test_compare_results_does_not_match_a_different_message(suite):
    before_finding = _finding(line=1, message="F401: json unused")
    after_finding = _finding(line=1, message="F401: os unused")
    comparison = compare_results([before_finding], [after_finding])
    suite.check("a different message is a genuinely different finding, not a match",
                comparison.removed_count == 1 and comparison.introduced_count == 1)


def test_compare_results_severity_change_alone_still_matches(suite):
    """Documented deliberate exclusion: severity is not part of the match
    key, so a severity-only change is still treated as the same finding.
    """
    before_finding = _finding(line=1, severity="warning", message="same")
    after_finding = _finding(line=1, severity="error", message="same")
    comparison = compare_results([before_finding], [after_finding])
    suite.check("matched despite a different severity", comparison.unchanged_count == 1)


def test_compare_results_target_resolved_true_when_gone(suite):
    target = _finding(line=1, message="F401: unused")
    comparison = compare_results([target], [], target_finding=target)
    suite.check("target_resolved is True", comparison.target_resolved is True)


def test_compare_results_target_resolved_false_when_still_present(suite):
    target = _finding(line=1, message="F401: unused")
    comparison = compare_results([target], [target], target_finding=target)
    suite.check("target_resolved is False", comparison.target_resolved is False)


def test_compare_results_target_resolved_none_when_no_target_given(suite):
    comparison = compare_results([], [])
    suite.check("target_resolved is None when no target was given",
                comparison.target_resolved is None)


def test_compare_results_by_tool_and_by_severity_counts(suite):
    before = [_finding(line=1, tool="ruff", severity="error"),
              _finding(line=2, tool="pyright", severity="warning")]
    after = [_finding(line=1, tool="ruff", severity="error")]
    comparison = compare_results(before, after)
    suite.check("before_by_tool counts both tools", comparison.before_by_tool == {"ruff": 1, "pyright": 1})
    suite.check("after_by_tool counts only the survivor", comparison.after_by_tool == {"ruff": 1})
    suite.check("before_by_severity counts both severities",
                comparison.before_by_severity == {"error": 1, "warning": 1})
    suite.check("after_by_severity counts only the survivor", comparison.after_by_severity == {"error": 1})


def test_compare_results_tool_error_names_are_captured(suite):
    comparison = compare_results(
        [], [], before_tool_errors=[("eslint", ToolError("boom"))],
        after_tool_errors=[("mypy", ToolError("bang"))],
    )
    suite.check("before tool error names captured", comparison.before_tool_errors == ("eslint",))
    suite.check("after tool error names captured", comparison.after_tool_errors == ("mypy",))


def test_compare_results_ordering_is_deterministic(suite):
    before = [_finding(line=1, message="m1"), _finding(line=2, message="m2")]
    after = [_finding(line=2, message="m2"), _finding(line=3, message="m3")]
    first = compare_results(before, after)
    second = compare_results(before, after)
    suite.check("removed_findings order is identical across calls",
                first.removed_findings == second.removed_findings)
    suite.check("introduced_findings order is identical across calls",
                first.introduced_findings == second.introduced_findings)


# --- validate_repair: classification -----------------------------------------


def test_validate_repair_improved_when_finding_removed(suite):
    dirs = []
    try:
        target = _finding(file="a.py", line=1, message="F401: unused")
        before_result = _Result(findings=[target])
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=_real_workspace_file(dirs))
        result = validate_repair(before_result, applied, run=_fake_run(after_findings=[]))
        suite.check("a RepairValidationResult is returned", isinstance(result, RepairValidationResult))
        suite.check("status is improved", result.status == STATUS_IMPROVED)
        assert result.comparison is not None
        suite.check("target_resolved is True", result.comparison.target_resolved is True)
    finally:
        _cleanup(dirs)


def test_validate_repair_unchanged_when_finding_still_present(suite):
    dirs = []
    try:
        target = _finding(file="a.py", line=1, message="F401: unused")
        before_result = _Result(findings=[target])
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=_real_workspace_file(dirs))
        result = validate_repair(before_result, applied, run=_fake_run(after_findings=[target]))
        suite.check("status is unchanged", result.status == STATUS_UNCHANGED)
        assert result.comparison is not None
        suite.check("target_resolved is False", result.comparison.target_resolved is False)
    finally:
        _cleanup(dirs)


def test_validate_repair_worsened_when_new_finding_introduced(suite):
    dirs = []
    try:
        target = _finding(file="a.py", line=1, message="F401: unused")
        new_finding = _finding(file="a.py", line=2, message="E999: syntax error")
        before_result = _Result(findings=[target])
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=_real_workspace_file(dirs))
        result = validate_repair(before_result, applied, run=_fake_run(after_findings=[new_finding]))
        suite.check("status is worsened", result.status == STATUS_WORSENED)
    finally:
        _cleanup(dirs)


def test_validate_repair_worsened_outranks_an_unrelated_improvement(suite):
    """The documented, deliberate rule: introducing anything new wins over
    an unrelated removal in the same comparison.
    """
    dirs = []
    try:
        target = _finding(file="a.py", line=1, message="F401: unused")
        other_removed = _finding(file="a.py", line=5, message="F841: unused var")
        new_finding = _finding(file="a.py", line=2, message="E999: syntax error")
        before_result = _Result(findings=[target, other_removed])
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=_real_workspace_file(dirs))
        result = validate_repair(before_result, applied, run=_fake_run(after_findings=[new_finding]))
        suite.check("worsened, even though something else also improved",
                     result.status == STATUS_WORSENED)
    finally:
        _cleanup(dirs)


# --- validate_repair: baseline scoping and analyzer run scope --------------


def test_validate_repair_baseline_is_scoped_to_the_repaired_file_only(suite):
    """The frozen 'before' snapshot may contain findings from many files -
    only the ones for the repaired file are used as the baseline.
    """
    dirs = []
    try:
        target = _finding(file="a.py", line=1, message="F401: unused")
        unrelated = _finding(file="b.py", line=9, message="F841: unused var")
        before_result = _Result(findings=[target, unrelated])
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=_real_workspace_file(dirs))
        result = validate_repair(before_result, applied, run=_fake_run(after_findings=[]))
        assert result.comparison is not None
        suite.check("before_count excludes the unrelated file's finding",
                     result.comparison.before_count == 1)
    finally:
        _cleanup(dirs)


def test_validate_repair_run_scope_is_exactly_the_workspace_file(suite):
    """Analyzer run scope: exactly the one patched file, not the whole
    workspace directory - proven behaviorally, not just by reading the code.
    """
    dirs = []
    try:
        target = _finding(file="a.py", line=1, message="F401: unused")
        before_result = _Result(findings=[target])
        workspace_file = _real_workspace_file(dirs)
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=workspace_file)
        calls = []
        validate_repair(before_result, applied, run=_fake_run(after_findings=[], capture=calls))
        suite.check("run() was called exactly once", len(calls) == 1)
        suite.check("run() was given exactly one input: the workspace file",
                     calls[0]["inputs"] == [workspace_file])
    finally:
        _cleanup(dirs)


def test_validate_repair_config_is_passed_through_unchanged(suite):
    dirs = []
    try:
        target = _finding(file="a.py", line=1, message="F401: unused")
        before_result = _Result(findings=[target])
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=_real_workspace_file(dirs))
        calls = []
        sentinel = object()
        validate_repair(before_result, applied, config=sentinel,
                         run=_fake_run(after_findings=[], capture=calls))
        suite.check("the given config object is forwarded to run() unchanged",
                     calls[0]["config"] is sentinel)
    finally:
        _cleanup(dirs)


# --- validate_repair: graceful failure modes --------------------------------


def test_validate_repair_none_applied_repair(suite):
    result = validate_repair(_Result(), None, run=_fake_run())
    suite.check("validation_failed on a None applied repair", result.status == STATUS_VALIDATION_FAILED)
    suite.check("a clear error is given", bool(result.error))
    suite.check("no comparison on failure", result.comparison is None)


def test_validate_repair_unsuccessful_applied_repair(suite):
    proposal = _proposal(_finding())
    applied = AppliedRepair(ok=False, proposal=proposal, error="line range invalid")
    result = validate_repair(_Result(), applied, run=_fake_run())
    suite.check("validation_failed when the repair itself was never applied",
                 result.status == STATUS_VALIDATION_FAILED)


def test_validate_repair_missing_workspace_file(suite):
    proposal = _proposal(_finding())
    applied = AppliedRepair(ok=True, proposal=proposal,
                             workspace_file=Path(tempfile.gettempdir()) / "does-not-exist-qa.py")
    result = validate_repair(_Result(), applied, run=_fake_run())
    suite.check("validation_failed when the workspace file is missing",
                 result.status == STATUS_VALIDATION_FAILED)
    suite.check("the missing path is named", "does-not-exist-qa.py" in (result.error or ""))


def test_validate_repair_missing_baseline(suite):
    dirs = []
    try:
        proposal = _proposal(_finding())
        applied = AppliedRepair(ok=True, proposal=proposal, workspace_file=_real_workspace_file(dirs))
        result = validate_repair(None, applied, run=_fake_run())
        suite.check("validation_failed when no baseline is given",
                     result.status == STATUS_VALIDATION_FAILED)
    finally:
        _cleanup(dirs)


def test_validate_repair_malformed_applied_repair_missing_proposal(suite):
    class _Bare:
        ok = True

    result = validate_repair(_Result(), _Bare(), run=_fake_run())
    suite.check("validation_failed on an object missing proposal/workspace_file",
                 result.status == STATUS_VALIDATION_FAILED)


def test_validate_repair_run_raises_is_caught(suite):
    dirs = []
    try:
        proposal = _proposal(_finding())
        applied = AppliedRepair(ok=True, proposal=proposal, workspace_file=_real_workspace_file(dirs))

        def _broken_run(inputs, config=None):
            raise RuntimeError("analyzer subprocess exploded")

        result = validate_repair(_Result(findings=[proposal.finding]), applied, run=_broken_run)
        suite.check("validation_failed, not a crash, when run() itself raises",
                     result.status == STATUS_VALIDATION_FAILED)
        suite.check("the underlying error is named", "exploded" in (result.error or ""))
    finally:
        _cleanup(dirs)


def test_validate_repair_target_tool_failure_during_rerun_is_validation_failed(suite):
    """The real scenario found via dogfooding: if the specific tool that
    produced the target finding fails during the rerun, "no finding" and
    "the tool never ran" are indistinguishable - this must not silently
    read as improved.
    """
    dirs = []
    try:
        target = _finding(file="app.js", line=1, tool="eslint", message="no-unused-vars: x")
        before_result = _Result(findings=[target])
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=_real_workspace_file(dirs))
        result = validate_repair(
            before_result, applied,
            run=_fake_run(after_findings=[], after_tool_errors=[("eslint", ToolError("no config found"))]),
        )
        suite.check("validation_failed, not improved, when the target's own tool errored",
                     result.status == STATUS_VALIDATION_FAILED)
        suite.check("eslint is named in the error", "eslint" in (result.error or ""))
    finally:
        _cleanup(dirs)


def test_validate_repair_unrelated_tool_error_does_not_block_validation(suite):
    """Tool-error isolation, carried over from Phase C: a tool error for a
    *different* tool than the one being validated must not stop validation.
    """
    dirs = []
    try:
        target = _finding(file="a.py", line=1, tool="ruff", message="F401: unused")
        before_result = _Result(findings=[target])
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=_real_workspace_file(dirs))
        result = validate_repair(
            before_result, applied,
            run=_fake_run(after_findings=[], after_tool_errors=[("mypy", ToolError("unrelated failure"))]),
        )
        suite.check("validation still completes despite an unrelated tool error",
                     result.status == STATUS_IMPROVED)
        assert result.comparison is not None
        suite.check("the unrelated tool error is still recorded in the comparison",
                     result.comparison.after_tool_errors == ("mypy",))
    finally:
        _cleanup(dirs)


# --- multiple analyzers / mixed-language projects ---------------------------


def test_validate_repair_multiple_analyzers_on_one_file(suite):
    dirs = []
    try:
        target = _finding(file="a.py", line=1, tool="ruff", message="F401: unused")
        pyright_finding = _finding(file="a.py", line=3, tool="pyright", message="reportMissingImports")
        before_result = _Result(findings=[target, pyright_finding])
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=_real_workspace_file(dirs))
        # ruff's finding is gone, pyright's survives unchanged.
        result = validate_repair(before_result, applied, run=_fake_run(after_findings=[pyright_finding]))
        assert result.comparison is not None
        suite.check("before_by_tool counts both ruff and pyright",
                     result.comparison.before_by_tool == {"ruff": 1, "pyright": 1})
        suite.check("after_by_tool counts only pyright's survivor",
                     result.comparison.after_by_tool == {"pyright": 1})
        suite.check("status is improved (one real removal, nothing new)",
                     result.status == STATUS_IMPROVED)
    finally:
        _cleanup(dirs)


def test_validate_repair_mixed_language_python_and_javascript(suite):
    dirs = []
    try:
        py_target = _finding(file="a.py", line=1, tool="ruff", message="F401: unused")
        js_target = _finding(file="app.js", line=1, tool="eslint", message="no-unused-vars: x")

        py_result = validate_repair(
            _Result(findings=[py_target]),
            AppliedRepair(ok=True, proposal=_proposal(py_target), workspace_file=_real_workspace_file(dirs)),
            run=_fake_run(after_findings=[]),
        )
        js_result = validate_repair(
            _Result(findings=[js_target]),
            AppliedRepair(ok=True, proposal=_proposal(js_target), workspace_file=_real_workspace_file(dirs)),
            run=_fake_run(after_findings=[]),
        )
        suite.check("a Python repair validates correctly", py_result.status == STATUS_IMPROVED)
        suite.check("a JavaScript repair validates correctly through the same function",
                     js_result.status == STATUS_IMPROVED)
    finally:
        _cleanup(dirs)


# --- isolation / no modification of the original project --------------------


def test_validate_repair_never_touches_the_real_original_file(suite):
    with TempProject() as project:
        original = project / "a.py"
        original_bytes = b"import json\n"
        original.write_bytes(original_bytes)
        target = _finding(file=str(original), line=1, tool="ruff", message="F401: unused")
        before_result = _Result(findings=[target])
        dirs = []
        try:
            applied = AppliedRepair(ok=True, proposal=_proposal(target, file=str(original)),
                                     workspace_file=_real_workspace_file(dirs))
            validate_repair(before_result, applied, run=_fake_run(after_findings=[]))
            suite.check("the real original file is byte-for-byte unchanged",
                        original.read_bytes() == original_bytes)
        finally:
            _cleanup(dirs)


def test_validator_module_never_writes_or_touches_subprocess_directly(suite):
    """Checked directly against the real source: validator.py must never
    write to disk or shell out itself - it only ever calls the injected
    `run` callable, which is the real runner.run() by default.
    """
    source = (REPO_ROOT / "qa_agent" / "ai" / "validator.py").read_text(encoding="utf-8")
    forbidden = ["write_text", "write_bytes", "import subprocess", "os.system"]
    found = [token for token in forbidden if token in source]
    suite.check("no write/subprocess tokens appear in validator.py's real source",
                not found, "  [{}]".format(found))


def test_validator_is_not_invoked_by_the_normal_analyze_path(suite):
    """No CLI integration in this part: nothing in the deterministic
    pipeline (including __main__.py, which already legitimately imports
    qa_agent.ai as of Phase D Part 6) references validate_repair.
    """
    pipeline_modules = [
        "runner.py", "adapters.py", "report.py", "config.py",
        "analysis_bridge.py", "watch.py", "debouncer.py",
        "fsmonitor.py", "live_report.py", "gitdiff.py", "__main__.py",
    ]
    offenders = []
    for name in pipeline_modules:
        text = (REPO_ROOT / "qa_agent" / name).read_text(encoding="utf-8")
        if "validate_repair" in text or "RepairValidationResult" in text:
            offenders.append(name)
    suite.check("no pipeline module references validate_repair/RepairValidationResult",
                not offenders, "  [{}]".format(offenders))


def test_validate_repair_deterministic_across_repeated_calls(suite):
    dirs = []
    try:
        target = _finding(file="a.py", line=1, tool="ruff", message="F401: unused")
        survivor = _finding(file="a.py", line=2, tool="ruff", message="F841: unused var")
        new_finding = _finding(file="a.py", line=3, tool="ruff", message="E999: syntax error")
        before_result = _Result(findings=[target, survivor])
        workspace_file = _real_workspace_file(dirs)
        applied = AppliedRepair(ok=True, proposal=_proposal(target), workspace_file=workspace_file)

        first = validate_repair(before_result, applied,
                                 run=_fake_run(after_findings=[survivor, new_finding]))
        second = validate_repair(before_result, applied,
                                  run=_fake_run(after_findings=[survivor, new_finding]))
        suite.check("identical status across repeated calls", first.status == second.status)
        assert first.comparison is not None and second.comparison is not None
        suite.check("identical removed/introduced findings across repeated calls",
                     first.comparison.removed_findings == second.comparison.removed_findings
                     and first.comparison.introduced_findings == second.comparison.introduced_findings)
    finally:
        _cleanup(dirs)


if __name__ == "__main__":
    suite = Suite("Repair validation engine (Phase E Part 3)")
    sys.exit(suite.run([
        test_compare_results_finding_removed_is_counted,
        test_compare_results_finding_unchanged_is_not_counted_as_removed_or_introduced,
        test_compare_results_new_finding_is_introduced,
        test_compare_results_matches_across_different_file_paths_by_basename,
        test_compare_results_does_not_match_a_different_line,
        test_compare_results_does_not_match_a_different_tool,
        test_compare_results_does_not_match_a_different_message,
        test_compare_results_severity_change_alone_still_matches,
        test_compare_results_target_resolved_true_when_gone,
        test_compare_results_target_resolved_false_when_still_present,
        test_compare_results_target_resolved_none_when_no_target_given,
        test_compare_results_by_tool_and_by_severity_counts,
        test_compare_results_tool_error_names_are_captured,
        test_compare_results_ordering_is_deterministic,
        test_validate_repair_improved_when_finding_removed,
        test_validate_repair_unchanged_when_finding_still_present,
        test_validate_repair_worsened_when_new_finding_introduced,
        test_validate_repair_worsened_outranks_an_unrelated_improvement,
        test_validate_repair_baseline_is_scoped_to_the_repaired_file_only,
        test_validate_repair_run_scope_is_exactly_the_workspace_file,
        test_validate_repair_config_is_passed_through_unchanged,
        test_validate_repair_none_applied_repair,
        test_validate_repair_unsuccessful_applied_repair,
        test_validate_repair_missing_workspace_file,
        test_validate_repair_missing_baseline,
        test_validate_repair_malformed_applied_repair_missing_proposal,
        test_validate_repair_run_raises_is_caught,
        test_validate_repair_target_tool_failure_during_rerun_is_validation_failed,
        test_validate_repair_unrelated_tool_error_does_not_block_validation,
        test_validate_repair_multiple_analyzers_on_one_file,
        test_validate_repair_mixed_language_python_and_javascript,
        test_validate_repair_never_touches_the_real_original_file,
        test_validator_module_never_writes_or_touches_subprocess_directly,
        test_validator_is_not_invoked_by_the_normal_analyze_path,
        test_validate_repair_deterministic_across_repeated_calls,
    ]))
