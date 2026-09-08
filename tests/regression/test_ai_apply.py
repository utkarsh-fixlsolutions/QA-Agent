"""Verified repair application (Phase E Part 5): apply.py, and report.py's
optional `repair_result` rendering.

Pure filesystem tests, no network, no live LLM - `apply_verified_repair`
only ever consumes a `RepairDecision`-shaped object as plain data. Real
`RepairProposal`, `AppliedRepair`, `RepairValidationResult`, and
`RepairDecision` instances are used throughout so these tests exercise the
real production types.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite, TempProject  # noqa: E402

from qa_agent.adapters import Finding  # noqa: E402
from qa_agent.ai.repair import RepairProposal  # noqa: E402
from qa_agent.ai.workspace import AppliedRepair  # noqa: E402
from qa_agent.ai.validator import STATUS_IMPROVED, RepairValidationResult, ValidationComparison  # noqa: E402
from qa_agent.ai.decision import (  # noqa: E402
    ACTION_ACCEPT_CANDIDATE,
    ACTION_HOLD,
    ACTION_REJECT,
    ACTION_VALIDATION_FAILED,
    RepairDecision,
)
from qa_agent.ai.apply import (  # noqa: E402
    RepairApplicationResult,
    apply_verified_repair,
    approve_repair,
    reject_repair,
)
from qa_agent.report import render, render_markdown, render_findings  # noqa: E402


def _finding(file, line=1, severity="error", message="F401: unused", tool="ruff"):
    return Finding(file=file, line=line, severity=severity, message=message, tool=tool)


def _proposal(finding, start_line=None, end_line=None):
    return RepairProposal(
        finding=finding, explanation="why", replacement="fixed", confidence=0.9,
        model="mock", file=finding.file,
        start_line=start_line if start_line is not None else finding.line,
        end_line=end_line if end_line is not None else finding.line,
    )


def _applied(proposal, workspace_file, original_text, patched_text, ok=True):
    return AppliedRepair(
        ok=ok, proposal=proposal, workspace_file=workspace_file,
        original_text=original_text, patched_text=patched_text,
    )


def _accept_decision(proposal, applied):
    comparison = ValidationComparison(before_count=1, after_count=0, removed_count=1,
                                       introduced_count=0, unchanged_count=0, target_resolved=True)
    validation = RepairValidationResult(status=STATUS_IMPROVED, comparison=comparison)
    return RepairDecision(
        action=ACTION_ACCEPT_CANDIDATE, reason="target finding resolved, 1 finding(s) removed",
        before_count=1, after_count=0, removed_count=1, introduced_count=0,
        proposal=proposal, applied_repair=applied, validation_result=validation,
    )


def _setup_repair(root, original_text="import os\nprint('hi')\n",
                   patched_text="print('hi')\n", filename="a.py"):
    """A real original file plus a real workspace copy, matching what
    Parts 1-2 would actually have produced - returns (original, proposal,
    applied).
    """
    original = root / filename
    original.write_text(original_text, encoding="utf-8", newline="")
    finding = _finding(str(original), line=1)
    proposal = _proposal(finding, start_line=1, end_line=1)
    workspace_dir = root / "_workspace"
    workspace_dir.mkdir(exist_ok=True)
    workspace_file = workspace_dir / filename
    workspace_file.write_text(patched_text, encoding="utf-8", newline="")
    applied = _applied(proposal, workspace_file, original_text, patched_text)
    return original, proposal, applied


# --- accepted repair writes successfully -------------------------------------


def test_accepted_repair_writes_successfully(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        decision = _accept_decision(proposal, applied)

        result = apply_verified_repair(decision)
        suite.check("a RepairApplicationResult is returned", isinstance(result, RepairApplicationResult))
        suite.check("success is True", result.success)
        suite.check("the real file now has the patched content",
                     original.read_text(encoding="utf-8") == "print('hi')\n")
        suite.check("file is reported", result.file == str(original))
        suite.check("bytes_written matches the patched content's size",
                     result.bytes_written == len("print('hi')\n".encode("utf-8")))
        suite.check("a timestamp is given", bool(result.timestamp))
        suite.check("decision_used is the real decision object", result.decision_used is decision)


def test_accepted_repair_creates_a_real_backup_with_the_original_content(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        decision = _accept_decision(proposal, applied)

        result = apply_verified_repair(decision)
        assert result.success and isinstance(result.backup_file, Path)
        suite.check("a backup_file is reported", result.backup_file is not None)
        backup = result.backup_file
        suite.check("the backup file really exists", backup.is_file())
        suite.check("the backup holds the ORIGINAL content, not the patched one",
                     backup.read_text(encoding="utf-8") == "import os\nprint('hi')\n")


def test_accepted_repair_atomic_replace_leaves_no_leftover_temp_file(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        decision = _accept_decision(proposal, applied)
        apply_verified_repair(decision)
        leftovers = list(root.glob("*.qa-agent-tmp"))
        suite.check("no .qa-agent-tmp file survives a successful write", leftovers == [])


# --- decisions that must never write -----------------------------------------


def test_review_hold_repair_never_writes(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        decision = RepairDecision(action=ACTION_HOLD, reason="no measurable change",
                                   proposal=proposal, applied_repair=applied)
        before = original.read_bytes()
        result = apply_verified_repair(decision)
        suite.check("success is False for a hold decision", not result.success)
        suite.check("the original file is completely untouched", original.read_bytes() == before)


def test_rejected_repair_never_writes(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        decision = RepairDecision(action=ACTION_REJECT, reason="worsened",
                                   proposal=proposal, applied_repair=applied)
        before = original.read_bytes()
        result = apply_verified_repair(decision)
        suite.check("success is False for a reject decision", not result.success)
        suite.check("the reason names the real action", "reject" in result.reason.lower()
                     or "accept_candidate" in result.reason)
        suite.check("the original file is completely untouched", original.read_bytes() == before)


def test_validation_failed_repair_never_writes(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        decision = RepairDecision(action=ACTION_VALIDATION_FAILED, reason="workspace file missing",
                                   proposal=proposal, applied_repair=applied)
        before = original.read_bytes()
        result = apply_verified_repair(decision)
        suite.check("success is False for a validation_failed decision", not result.success)
        suite.check("the original file is completely untouched", original.read_bytes() == before)


def test_none_decision_never_writes(suite):
    result = apply_verified_repair(None)
    suite.check("success is False on a None decision", not result.success)
    suite.check("a clear reason is given", bool(result.reason))


# --- precondition checks: workspace mismatch, modified source, missing files, line mismatch


def test_workspace_repair_not_ok_is_rejected(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        broken_applied = AppliedRepair(ok=False, proposal=proposal, error="line range invalid")
        decision = RepairDecision(action=ACTION_ACCEPT_CANDIDATE, reason="target resolved",
                                   proposal=proposal, applied_repair=broken_applied)
        before = original.read_bytes()
        result = apply_verified_repair(decision)
        suite.check("an unsuccessful AppliedRepair is rejected", not result.success)
        suite.check("original untouched", original.read_bytes() == before)


def test_missing_workspace_file_is_rejected(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        # Simulate the workspace having been cleaned up already.
        assert applied.workspace_file is not None
        Path(applied.workspace_file).unlink()
        decision = _accept_decision(proposal, applied)
        before = original.read_bytes()
        result = apply_verified_repair(decision)
        suite.check("a missing workspace file is rejected", not result.success)
        suite.check("the reason mentions the workspace", "workspace" in result.reason)
        suite.check("original untouched", original.read_bytes() == before)


def test_missing_source_file_is_rejected(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        original.unlink()
        decision = _accept_decision(proposal, applied)
        result = apply_verified_repair(decision)
        suite.check("a missing original file is rejected", not result.success)
        suite.check("the reason mentions the original file", "original file" in result.reason)


def test_modified_source_is_rejected(suite):
    """The real project file changed since the repair was generated - must
    never be overwritten regardless of how good the repair looked.
    """
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        original.write_text("import os\nprint('a completely different edit')\n",
                             encoding="utf-8", newline="")
        modified_bytes = original.read_bytes()
        decision = _accept_decision(proposal, applied)
        result = apply_verified_repair(decision)
        suite.check("a modified source file is rejected", not result.success)
        suite.check("the reason says the file changed", "changed" in result.reason)
        suite.check("the file's own (differently-modified) content is untouched",
                     original.read_bytes() == modified_bytes)


def test_line_mismatch_is_rejected(suite):
    """The repair's own line range no longer fits the (unchanged) file -
    a malformed/stale proposal must not be trusted blindly.
    """
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        bad_proposal = RepairProposal(
            finding=proposal.finding, explanation="why", replacement="fixed",
            confidence=0.9, model="mock", file=proposal.file,
            start_line=1, end_line=99,  # far beyond the real file's line count
        )
        bad_applied = _applied(bad_proposal, applied.workspace_file,
                                applied.original_text, applied.patched_text)
        decision = _accept_decision(bad_proposal, bad_applied)
        before = original.read_bytes()
        result = apply_verified_repair(decision)
        suite.check("an out-of-range line repair is rejected", not result.success)
        suite.check("the reason mentions the line range", "line range" in result.reason)
        suite.check("original untouched", original.read_bytes() == before)


def test_target_file_mismatch_is_rejected(suite):
    """decision.proposal and decision.applied_repair.proposal disagree
    about which file is being repaired - a genuine consistency check.
    """
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        other_finding = _finding(str(root / "other.py"), line=1)
        mismatched_top_level_proposal = _proposal(other_finding)
        decision = RepairDecision(
            action=ACTION_ACCEPT_CANDIDATE, reason="target resolved",
            proposal=mismatched_top_level_proposal, applied_repair=applied,
        )
        before = original.read_bytes()
        result = apply_verified_repair(decision)
        suite.check("a mismatched proposal/applied_repair pair is rejected", not result.success)
        suite.check("the reason names the mismatch", "same file" in result.reason)
        suite.check("original untouched", original.read_bytes() == before)


def test_missing_original_text_fails_closed(suite):
    """AppliedRepair without original_text: cannot verify "unchanged", so
    this must fail closed rather than silently skip the check.
    """
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        no_original_text = AppliedRepair(
            ok=True, proposal=proposal, workspace_file=applied.workspace_file,
            original_text=None, patched_text=applied.patched_text,
        )
        decision = _accept_decision(proposal, no_original_text)
        before = original.read_bytes()
        result = apply_verified_repair(decision)
        suite.check("missing original_text fails closed, not open", not result.success)
        suite.check("original untouched", original.read_bytes() == before)


# --- write failure handling ---------------------------------------------------


def test_write_failure_is_handled_cleanly(suite):
    """A destination that cannot be written to (a directory in its place)
    must produce a clean, structured failure - not a crash, and the real
    original file (whatever it actually is) must be provably unaffected.
    """
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        # Replace the "original" with a directory of the same name so the
        # write step itself fails in a controlled, reproducible way.
        original.unlink()
        original.mkdir()
        decision = _accept_decision(proposal, applied)
        result = apply_verified_repair(decision)
        suite.check("a write onto a directory fails cleanly, not with a crash", not result.success)
        suite.check("the directory itself was not deleted or replaced", original.is_dir())


def test_apply_verified_repair_never_raises_on_a_broken_decision(suite):
    class _Explosive:
        @property
        def action(self):
            raise RuntimeError("something nobody anticipated")

    result = apply_verified_repair(_Explosive())
    suite.check("a decision whose own attribute access raises still yields success=False",
                not result.success)


# --- approve_repair / reject_repair -------------------------------------------


def test_approve_repair_applies_an_accepted_decision(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        decision = _accept_decision(proposal, applied)
        result = approve_repair(decision)
        suite.check("approve_repair writes an accepted repair, same as apply_verified_repair",
                     result.success and original.read_text(encoding="utf-8") == "print('hi')\n")


def test_reject_repair_never_writes_even_for_an_accepted_decision(suite):
    with TempProject() as root:
        original, proposal, applied = _setup_repair(root)
        decision = _accept_decision(proposal, applied)
        before = original.read_bytes()
        result = reject_repair(decision)
        suite.check("reject_repair reports failure", not result.success)
        suite.check("reject_repair never writes, even for an otherwise-accepted decision",
                     original.read_bytes() == before)
        suite.check("the reason is explicit about rejection", "rejected" in result.reason)


# --- report rendering ----------------------------------------------------------


class _Result:
    def __init__(self, checked=(), tools_used=(), findings=()):
        self.checked, self.tools_used, self.findings = checked, tools_used, findings
        self.filtered = 0
        self.tool_errors = []
        self.skipped = []
        self.missing = []


class _AppResult:
    def __init__(self, success, file=None, backup_file=None, reason=""):
        self.success, self.file, self.backup_file, self.reason = success, file, backup_file, reason


def test_render_shows_verified_repair_applied_block(suite):
    result = _Result(checked=["a.py"], tools_used=["ruff"])
    repair_result = _AppResult(success=True, file="a.py", backup_file="a.py.bak-1")
    output = render(result, "a.py", repair_result=repair_result)
    suite.check("the block header is present", "Verified Repair Applied" in output)
    suite.check("the file is named", "File:   a.py" in output)
    suite.check("the backup is named", "Backup: a.py.bak-1" in output)


def test_render_shows_repair_skipped_block(suite):
    result = _Result(checked=["a.py"], tools_used=["ruff"])
    repair_result = _AppResult(success=False, reason="repair was not accepted")
    output = render(result, "a.py", repair_result=repair_result)
    suite.check("the skipped header is present", "Repair Skipped" in output)
    suite.check("the reason is shown", "repair was not accepted" in output)


def test_render_byte_identical_without_repair_result(suite):
    result = _Result(checked=["a.py"], tools_used=["ruff"])
    without_param = render(result, "a.py")
    with_none = render(result, "a.py", repair_result=None)
    suite.check("no repair_result param == repair_result=None, byte for byte",
                without_param == with_none)
    suite.check("no repair-application text anywhere",
                "Verified Repair Applied" not in without_param
                and "Repair Skipped" not in without_param)


def test_render_markdown_repair_application_section(suite):
    result = _Result(checked=["a.py"], tools_used=["ruff"])
    applied_result = _AppResult(success=True, file="a.py", backup_file="a.py.bak-1")
    skipped_result = _AppResult(success=False, reason="repair was not accepted")

    applied_output = render_markdown(result, "a.py", repair_result=applied_result)
    suite.check("markdown shows the applied section", "## Verified Repair Applied" in applied_output)
    suite.check("markdown names the file", "a.py" in applied_output)

    skipped_output = render_markdown(result, "a.py", repair_result=skipped_result)
    suite.check("markdown shows the skipped section", "## Repair Skipped" in skipped_output)
    suite.check("markdown shows the reason", "repair was not accepted" in skipped_output)


def test_render_markdown_byte_identical_without_repair_result(suite):
    result = _Result(checked=["a.py"], tools_used=["ruff"])
    without_param = render_markdown(result, "a.py")
    with_none = render_markdown(result, "a.py", repair_result=None)
    suite.check("markdown: no param == None, byte for byte", without_param == with_none)


def test_render_findings_signature_unaffected(suite):
    """render_findings() (watch mode) deliberately does not accept
    repair_result - a single applied-repair outcome has no natural meaning
    per incremental batch, the same reasoning summary already followed
    (Phase D Part 4).
    """
    result = _Result(checked=["a.py"], tools_used=["ruff"])
    output = render_findings(result)
    suite.check("render_findings still works exactly as before", isinstance(output, str))


# --- isolation ------------------------------------------------------------------


def test_apply_module_never_imports_provider_or_prompt_infrastructure(suite):
    source = (REPO_ROOT / "qa_agent" / "ai" / "apply.py").read_text(encoding="utf-8")
    forbidden_imports = [
        "from .provider", "from .ollama", "from .mock", "from .prompts",
        "from .schemas", "from .response_parser", "from .repair", "from .validator",
        "from ..runner", "from .context",
    ]
    found = [token for token in forbidden_imports if token in source]
    suite.check("no provider/prompt/schema/runner imports appear in apply.py's real source",
                not found, "  [{}]".format(found))


def test_apply_module_is_not_invoked_by_the_normal_analyze_path(suite):
    pipeline_modules = [
        "runner.py", "adapters.py", "config.py",
        "analysis_bridge.py", "watch.py", "debouncer.py",
        "fsmonitor.py", "live_report.py", "gitdiff.py", "__main__.py",
    ]
    offenders = []
    for name in pipeline_modules:
        text = (REPO_ROOT / "qa_agent" / name).read_text(encoding="utf-8")
        if "apply_verified_repair" in text or "approve_repair" in text or "RepairApplicationResult" in text:
            offenders.append(name)
    suite.check("no pipeline module references the apply.py public API",
                not offenders, "  [{}]".format(offenders))


if __name__ == "__main__":
    suite = Suite("Verified repair application (Phase E Part 5)")
    sys.exit(suite.run([
        test_accepted_repair_writes_successfully,
        test_accepted_repair_creates_a_real_backup_with_the_original_content,
        test_accepted_repair_atomic_replace_leaves_no_leftover_temp_file,
        test_review_hold_repair_never_writes,
        test_rejected_repair_never_writes,
        test_validation_failed_repair_never_writes,
        test_none_decision_never_writes,
        test_workspace_repair_not_ok_is_rejected,
        test_missing_workspace_file_is_rejected,
        test_missing_source_file_is_rejected,
        test_modified_source_is_rejected,
        test_line_mismatch_is_rejected,
        test_target_file_mismatch_is_rejected,
        test_missing_original_text_fails_closed,
        test_write_failure_is_handled_cleanly,
        test_apply_verified_repair_never_raises_on_a_broken_decision,
        test_approve_repair_applies_an_accepted_decision,
        test_reject_repair_never_writes_even_for_an_accepted_decision,
        test_render_shows_verified_repair_applied_block,
        test_render_shows_repair_skipped_block,
        test_render_byte_identical_without_repair_result,
        test_render_markdown_repair_application_section,
        test_render_markdown_byte_identical_without_repair_result,
        test_render_findings_signature_unaffected,
        test_apply_module_never_imports_provider_or_prompt_infrastructure,
        test_apply_module_is_not_invoked_by_the_normal_analyze_path,
    ]))
