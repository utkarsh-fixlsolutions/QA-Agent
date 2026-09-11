"""Phase G Part 4: Runtime Failure -> Verified Repair Integration
(docs/24-runtime-repair-integration.md) - `qa_agent.ai.repair_runtime_failure(s)()`,
its deterministic eligibility gate, target localization, candidate
materialization, and the `discover --repair-runtime` CLI flag.

Every fixture that needs a real runtime check goes through the real
discover -> context -> plan -> execute pipeline first (the same discipline
`test_runtime_diagnosis.py` already established), then a `RuntimeDiagnosis`
is hand-built directly (rather than routed through the real diagnosis
engine, already exhaustively tested in that other suite) so each test can
control exactly what the diagnosis claims. `MockProvider` stands in for the
LLM everywhere; the static analyzer rerun is a fake `run` callable (the
same convention `test_ai_repair_loop.py` already established) so these
tests never depend on a real linter/eslint install; the *runtime* rerun
(candidate and final) is real by default (real `npm`/`node`, matching this
project's own real-subprocess dogfooding discipline elsewhere), with a few
tests injecting `execute_check` to force a specific outcome deterministically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.ai import (  # noqa: E402
    ACTION_REJECT,
    APPLY_NOT_ATTEMPTED,
    ConnectionResult,
    DIAGNOSIS_AI_ERROR,
    DIAGNOSIS_DIAGNOSED,
    DIAGNOSIS_INSUFFICIENT_CONTEXT,
    DIAGNOSIS_INVALID_RESPONSE,
    DIAGNOSIS_NOT_APPLICABLE,
    LLMResponse,
    MockProvider,
    OUTCOME_APPLIED_BUT_STILL_FAILING,
    OUTCOME_ERROR,
    OUTCOME_HELD,
    OUTCOME_NOT_ELIGIBLE,
    OUTCOME_PROPOSAL_FAILED,
    OUTCOME_REJECTED,
    OUTCOME_VERIFIED,
    RuntimeDiagnosis,
    VERIFICATION_STILL_FAILING,
    VERIFICATION_VERIFIED,
    check_repair_eligibility,
    propose_runtime_repair,
    render_runtime_repair_result,
    repair_runtime_failure,
    repair_runtime_failures,
    resolve_repair_target,
    runtime_repair_result_to_dict,
)
from qa_agent.ai import runtime_repair  # noqa: E402 - private helpers (_mirror_with_patch)
from qa_agent.ai.apply import RepairApplicationResult  # noqa: E402
from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.runtime import (  # noqa: E402
    STATUS_FAIL,
    STATUS_PASS,
    ExecutionConfig,
    RuntimeCheckResult,
    plan_runtime_qa,
    run_runtime_plan,
)


class _Writer:
    def __init__(self, path):
        self.path = path

    write = TempProject.write


def _execute_in(root, files, config=None):
    """Like test_runtime_diagnosis.py's own `_execute`, but never cleans up
    `root` - G4 tests need continued real file access after execution
    (to patch, re-run, and re-verify), unlike G3's own diagnosis-only tests.
    """
    writer = _Writer(root)
    for rel, text in files.items():
        writer.write(rel, text)
    result = discover_project(root)
    context = build_repository_context(result.project)
    plan = plan_runtime_qa(context)
    execution = run_runtime_plan(plan, root, config)
    return execution, context


BROKEN_BUILD_JS = "console.error('Error: intentional failure');\nprocess.exit(1);\n"
STILL_BROKEN_JS = "console.error('Error: still broken');\nprocess.exit(1);\n"
FIXED_BUILD_JS = "console.log('build ok');\nprocess.exit(0);"


def _broken_build_fixture():
    return {
        "package-lock.json": "{}",
        "package.json": json.dumps({"scripts": {"build": "node build.js"}}),
        "build.js": BROKEN_BUILD_JS,
    }


def _repair_response(replacement, confidence=0.9, start_line=1, end_line=2, explanation="fixes the build"):
    return json.dumps({
        "explanation": explanation, "replacement": replacement,
        "confidence": confidence, "start_line": start_line, "end_line": end_line,
    })


def _make_diagnosis(check_result, affected_files=("build.js",), affected_components=("intentional failure",),
                     evidence=("Error: intentional failure",), status=DIAGNOSIS_DIAGNOSED, error=None):
    return RuntimeDiagnosis(
        check_id=check_result.id, check_name=check_result.name,
        execution_status=check_result.status, diagnosis_status=status,
        summary="the build script exits with a non-zero status", severity="error", confidence=0.9,
        observed_evidence=evidence, likely_root_cause="build.js intentionally exits with a failure",
        affected_files=affected_files, affected_components=affected_components,
        recommended_action="fix build.js so it exits 0", model="mock-model", error=error,
    )


def _raw_diagnosis(affected_files, affected_components=(), evidence=()):
    return RuntimeDiagnosis(
        check_id="x", check_name="X", execution_status="fail", diagnosis_status=DIAGNOSIS_DIAGNOSED,
        summary="s", severity="error", confidence=0.9, observed_evidence=evidence,
        likely_root_cause="r", affected_files=affected_files, affected_components=affected_components,
        recommended_action="a",
    )


class _FakeCheckResult:
    reason = ""
    exception = None
    logs = ()


class _RunResult:
    def __init__(self, findings=(), tool_errors=()):
        self.findings = list(findings)
        self.tool_errors = list(tool_errors)


class _Finding:
    def __init__(self, file, line=1, tool="ruff", message="issue", severity="error"):
        self.file, self.line, self.tool, self.message, self.severity = file, line, tool, message, severity


def _fake_run(findings=()):
    def run(inputs, config=None):
        return _RunResult(findings=findings)
    return run


def _sequenced_run(*results):
    calls = {"n": 0}

    def run(inputs, config=None):
        index = calls["n"]
        calls["n"] += 1
        return results[index] if index < len(results) else results[-1]

    return run


def _check_and_runtime_check(execution, check_id):
    check_result = next(r for r in execution.results if r.id == check_id)
    runtime_check = next(c for c in execution.plan.checks if c.id == check_id)
    return check_result, runtime_check


# --- eligibility gate: diagnosis status ------------------------------------

def test_insufficient_context_diagnosis_is_not_eligible(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result, status=DIAGNOSIS_INSUFFICIENT_CONTEXT)
        result = repair_runtime_failure(check_result, diagnosis, execution, context, MockProvider(), root)
        suite.check("INSUFFICIENT_CONTEXT diagnosis -> NOT_ELIGIBLE", result.outcome == OUTCOME_NOT_ELIGIBLE)
        suite.check("no real write attempted", result.apply_status == APPLY_NOT_ATTEMPTED)


def test_invalid_response_diagnosis_is_not_eligible(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result, status=DIAGNOSIS_INVALID_RESPONSE)
        result = repair_runtime_failure(check_result, diagnosis, execution, context, MockProvider(), root)
        suite.check("INVALID_RESPONSE diagnosis -> NOT_ELIGIBLE", result.outcome == OUTCOME_NOT_ELIGIBLE)


def test_ai_error_diagnosis_is_not_eligible(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result, status=DIAGNOSIS_AI_ERROR)
        result = repair_runtime_failure(check_result, diagnosis, execution, context, MockProvider(), root)
        suite.check("AI_ERROR diagnosis -> NOT_ELIGIBLE", result.outcome == OUTCOME_NOT_ELIGIBLE)


def test_not_applicable_diagnosis_is_not_eligible_and_skipped_in_plural(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result, status=DIAGNOSIS_NOT_APPLICABLE)
        result = repair_runtime_failure(check_result, diagnosis, execution, context, MockProvider(), root)
        suite.check("NOT_APPLICABLE diagnosis -> NOT_ELIGIBLE", result.outcome == OUTCOME_NOT_ELIGIBLE)
        results = repair_runtime_failures(execution, [diagnosis], context, MockProvider(), root)
        suite.check("the plural entry point skips a NOT_APPLICABLE diagnosis entirely", results == ())


# --- eligibility gate: target resolution -----------------------------------

def test_missing_affected_file_is_not_eligible(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result, affected_files=())
        result = repair_runtime_failure(check_result, diagnosis, execution, context, MockProvider(), root)
        suite.check("no affected file -> NOT_ELIGIBLE", result.outcome == OUTCOME_NOT_ELIGIBLE)
        suite.check("reason names the real cause", "no affected file" in result.eligibility.reason)


def test_nonexistent_affected_file_is_not_eligible(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result, affected_files=("does_not_exist.js",))
        result = repair_runtime_failure(check_result, diagnosis, execution, context, MockProvider(), root)
        suite.check("a nonexistent file -> NOT_ELIGIBLE, never guessed", result.outcome == OUTCOME_NOT_ELIGIBLE)
        suite.check("reason states it does not exist", "does not exist" in result.eligibility.reason)


def test_ambiguous_affected_files_is_not_eligible(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result, affected_files=("build.js", "package.json"))
        result = repair_runtime_failure(check_result, diagnosis, execution, context, MockProvider(), root)
        suite.check("multiple affected files -> NOT_ELIGIBLE, never arbitrarily chosen", result.outcome == OUTCOME_NOT_ELIGIBLE)
        suite.check("reason states ambiguity", "ambiguous" in result.eligibility.reason)


def test_environment_configuration_failure_with_missing_file_is_not_eligible(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, {".env.example": "K=\n"})
        check_result, _ = _check_and_runtime_check(execution, "environment-configuration")
        suite.check("fixture genuinely fails environment-configuration", check_result.status == STATUS_FAIL)
        diagnosis = _make_diagnosis(check_result, affected_files=(".env",), affected_components=())
        result = repair_runtime_failure(check_result, diagnosis, execution, context, MockProvider(), root)
        suite.check("a missing config file is never fabricated as a repair target", result.outcome == OUTCOME_NOT_ELIGIBLE)


def test_generated_or_dependency_path_is_rejected_as_repair_target(suite):
    with TempProject() as root:
        (root / "node_modules").mkdir()
        (root / "node_modules" / "pkg.js").write_text("module.exports = {};\n", encoding="utf-8")
        diagnosis = _raw_diagnosis(affected_files=("node_modules/pkg.js",))
        result = resolve_repair_target(diagnosis, _FakeCheckResult(), root)
        suite.check("a node_modules path is rejected as a repair target", not result.ok)
        suite.check("reason explains it is generated/dependency", "generated/dependency" in result.reason)


def test_lock_file_is_rejected_as_repair_target(suite):
    with TempProject() as root:
        (root / "package-lock.json").write_text("{}\n", encoding="utf-8")
        diagnosis = _raw_diagnosis(affected_files=("package-lock.json",))
        result = resolve_repair_target(diagnosis, _FakeCheckResult(), root)
        suite.check("a lockfile is rejected as a repair target", not result.ok)


# --- deterministic target/line localization --------------------------------

def test_deterministic_target_resolution_matches_real_evidence(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result)
        target = resolve_repair_target(diagnosis, check_result, root)
        suite.check("target resolved ok", target.ok)
        suite.check("target file is the real build.js", target.file == str((root / "build.js").resolve()))
        suite.check("localized to a real matching line, not merely anchored", target.localized)
        matched_line = BROKEN_BUILD_JS.splitlines()[target.line - 1]
        suite.check("the matched line genuinely contains the evidence token", "intentional failure" in matched_line)


def test_no_evidence_match_anchors_at_line_one_not_invented(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(
            check_result, affected_components=("totally-unrelated-token",), evidence=("nothing matching here",),
        )
        target = resolve_repair_target(diagnosis, check_result, root)
        suite.check("target file still resolves (it really exists)", target.ok)
        suite.check("not localized - no real evidence token matched", not target.localized)
        suite.check("anchored at line 1, never a fabricated line", target.line == 1)


# --- repair proposal (Phase E Part 1's own RepairProposal/validator) -------

def test_repair_proposal_succeeds_and_targets_the_resolved_file(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result)
        target = resolve_repair_target(diagnosis, check_result, root)
        provider = MockProvider(response_text=_repair_response(FIXED_BUILD_JS))
        proposal = propose_runtime_repair(check_result, diagnosis, context, target, provider)
        suite.check("a RepairProposal was produced", proposal is not None)
        if proposal is not None:
            suite.check("proposal targets the resolved file", proposal.file == target.file)
            suite.check("proposal carries the model's own replacement", "build ok" in proposal.replacement)
            suite.check("the finding shape is never a real adapters.Finding", proposal.finding.tool == "runtime-diagnosis")


def test_repair_proposal_failure_is_proposal_failed(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, runtime_check = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result)
        provider = MockProvider(fail=True, failure_message="offline")
        result = repair_runtime_failure(check_result, diagnosis, execution, context, provider, root,
                                         runtime_check=runtime_check)
        suite.check("an offline provider -> PROPOSAL_FAILED", result.outcome == OUTCOME_PROPOSAL_FAILED)
        suite.check("the real file was never touched", (root / "build.js").read_text(encoding="utf-8") == BROKEN_BUILD_JS)


# --- full pipeline: static guard + real runtime candidate + real apply -----

def test_eligible_diagnosis_full_happy_path_is_verified(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture(), ExecutionConfig(build_timeout=20))
        check_result, runtime_check = _check_and_runtime_check(execution, "build-verification")
        suite.check("fixture really fails build-verification", check_result.status == STATUS_FAIL)
        diagnosis = _make_diagnosis(check_result)
        provider = MockProvider(response_text=_repair_response(FIXED_BUILD_JS))

        result = repair_runtime_failure(
            check_result, diagnosis, execution, context, provider, root,
            runtime_check=runtime_check, run=_fake_run(findings=[]),
        )

        suite.check("outcome is VERIFIED", result.outcome == OUTCOME_VERIFIED)
        suite.check("verification_status is VERIFICATION_VERIFIED", result.verification_status == VERIFICATION_VERIFIED)
        suite.check("final_runtime_status is a real pass", result.final_runtime_status == STATUS_PASS)
        suite.check("the real build.js file was actually patched", "build ok" in (root / "build.js").read_text(encoding="utf-8"))
        suite.check("a deterministic decision is recorded", result.deterministic_decision is not None)
        suite.check("apply recorded a real backup file on disk",
                     result.apply_result is not None and result.apply_result.backup_file is not None
                     and Path(result.apply_result.backup_file).is_file())


def test_runtime_candidate_check_fails_rejects_without_touching_real_repository(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture(), ExecutionConfig(build_timeout=20))
        check_result, runtime_check = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result)
        # the model's own replacement is still broken - the candidate's own
        # real runtime check (re-run against a materialized patched copy)
        # will still fail.
        provider = MockProvider(response_text=_repair_response(STILL_BROKEN_JS))

        result = repair_runtime_failure(
            check_result, diagnosis, execution, context, provider, root,
            runtime_check=runtime_check, run=_fake_run(findings=[]),
        )

        suite.check("outcome is REJECTED", result.outcome == OUTCOME_REJECTED)
        suite.check("the candidate's own real runtime check genuinely failed", result.runtime_candidate_status == STATUS_FAIL)
        suite.check("no real apply was ever attempted", result.apply_status == APPLY_NOT_ATTEMPTED)
        suite.check(
            "the real repository file is byte-identical to before the attempt",
            (root / "build.js").read_text(encoding="utf-8") == BROKEN_BUILD_JS,
        )


def test_static_worsened_rejects_before_runtime_candidate_is_attempted(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, runtime_check = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result)
        provider = MockProvider(response_text=_repair_response(FIXED_BUILD_JS))
        target_file = str((root / "build.js").resolve())
        before = _RunResult(findings=[])
        after = _RunResult(findings=[_Finding(target_file)])  # a newly-introduced static finding

        spy = {"executed": False}

        def spy_execute_check(*a, **kw):
            spy["executed"] = True
            return None

        result = repair_runtime_failure(
            check_result, diagnosis, execution, context, provider, root,
            runtime_check=runtime_check, run=_sequenced_run(before, after), execute_check=spy_execute_check,
        )
        suite.check("outcome is REJECTED", result.outcome == OUTCOME_REJECTED)
        suite.check("a real static regression alone was enough to reject", result.deterministic_decision.action == ACTION_REJECT)
        suite.check("the runtime candidate check was never even attempted after a static reject", not spy["executed"])


def test_static_hold_without_known_runtime_check_stays_held(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result)
        provider = MockProvider(response_text=_repair_response(FIXED_BUILD_JS))
        result = repair_runtime_failure(
            check_result, diagnosis, execution, context, provider, root,
            runtime_check=None, run=_fake_run(findings=[]),  # unchanged -> static HOLD
        )
        suite.check("outcome is HELD", result.outcome == OUTCOME_HELD)
        suite.check("no runtime candidate check was possible without a known RuntimeCheck", result.runtime_candidate_status is None)
        suite.check("no real write happened", result.apply_status == APPLY_NOT_ATTEMPTED)


def test_real_post_apply_verification_still_failing_is_reported_honestly(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture(), ExecutionConfig(build_timeout=20))
        check_result, runtime_check = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result)
        provider = MockProvider(response_text=_repair_response(FIXED_BUILD_JS))

        def fake_execute_check(plan, check, root_arg, config=None):
            # the candidate (a materialized copy, never equal to the real
            # root) reports PASS; the *real* post-apply re-run (root_arg is
            # the real root) reports FAIL - a deliberate divergence, to
            # prove this module never reports VERIFIED off anything but a
            # real check against the real repository.
            status = STATUS_FAIL if str(root_arg) == str(root) else STATUS_PASS
            return RuntimeCheckResult(id=check.id, name=check.name, status=status,
                                       start_time="", end_time="", duration=0.1, reason="test double")

        result = repair_runtime_failure(
            check_result, diagnosis, execution, context, provider, root,
            runtime_check=runtime_check, run=_fake_run(findings=[]), execute_check=fake_execute_check,
        )
        suite.check("outcome is APPLIED_BUT_STILL_FAILING", result.outcome == OUTCOME_APPLIED_BUT_STILL_FAILING)
        suite.check("verification_status is VERIFICATION_STILL_FAILING", result.verification_status == VERIFICATION_STILL_FAILING)
        suite.check("the real file WAS written (the accepted candidate was applied)",
                     "build ok" in (root / "build.js").read_text(encoding="utf-8"))
        suite.check("this is never reported as VERIFIED", result.outcome != OUTCOME_VERIFIED)


def test_real_apply_failure_is_handled_safely(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, runtime_check = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result)
        provider = MockProvider(response_text=_repair_response(FIXED_BUILD_JS))

        def failing_apply(decision):
            return RepairApplicationResult(success=False, reason="disk full (simulated)", decision_used=decision)

        def fake_execute_check(plan, check, root_arg, config=None):
            return RuntimeCheckResult(id=check.id, name=check.name, status=STATUS_PASS,
                                       start_time="", end_time="", duration=0.1, reason="test double")

        result = repair_runtime_failure(
            check_result, diagnosis, execution, context, provider, root,
            runtime_check=runtime_check, run=_fake_run(findings=[]),
            execute_check=fake_execute_check, apply=failing_apply,
        )
        suite.check("outcome is ERROR, never converted into a false success", result.outcome == OUTCOME_ERROR)
        suite.check("apply_status carries the real failure reason", result.apply_status == "disk full (simulated)")
        suite.check("the real file was never actually written", (root / "build.js").read_text(encoding="utf-8") == BROKEN_BUILD_JS)


def test_max_one_repair_attempt_per_check_no_retry_loop(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture(), ExecutionConfig(build_timeout=20))
        check_result, runtime_check = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result)

        class _CountingProvider:
            name = "counting"

            def __init__(self):
                self.calls = 0

            def generate(self, prompt):
                self.calls += 1
                return LLMResponse(text=_repair_response(FIXED_BUILD_JS), provider=self.name, model="counting-model")

            def test_connection(self):
                return ConnectionResult(ok=True)

        provider = _CountingProvider()
        repair_runtime_failure(check_result, diagnosis, execution, context, provider, root,
                                runtime_check=runtime_check, run=_fake_run(findings=[]))
        suite.check("exactly one AI call was made - no retry, no repair loop", provider.calls == 1)


# --- candidate materialization ----------------------------------------------

def test_mirror_with_patch_links_siblings_and_writes_only_the_target(suite):
    with TempProject() as source_root:
        writer = _Writer(source_root)
        writer.write("keep/a.txt", "original a\n")
        writer.write("target.txt", "original target\n")
        with TempProject() as dest_parent:
            dest = dest_parent / "mirror"
            ok = runtime_repair._mirror_with_patch(source_root, dest, ("target.txt",), b"patched target\n")
            suite.check("mirror succeeded", ok)
            suite.check("the patched file has the new content", (dest / "target.txt").read_text(encoding="utf-8") == "patched target\n")
            suite.check("a sibling directory is still reachable with its original content",
                         (dest / "keep" / "a.txt").read_text(encoding="utf-8") == "original a\n")
            suite.check("the real source target file is completely untouched",
                         (source_root / "target.txt").read_text(encoding="utf-8") == "original target\n")


# --- AI cannot directly modify files -----------------------------------------

def test_ai_provider_output_is_never_written_directly_to_a_real_file(suite):
    source = Path(runtime_repair.__file__).read_text(encoding="utf-8")
    suite.check(
        "this module's only real byte-write is inside _mirror_with_patch (a workspace copy, never the real repo)",
        source.count(".write_bytes(") == 1,
    )
    suite.check("this module never calls .write_text on any path", ".write_text(" not in source)


# --- rendering / serialization -----------------------------------------------

def test_runtime_repair_result_to_dict_has_expected_shape(suite):
    with TempProject() as root:
        execution, context = _execute_in(root, _broken_build_fixture())
        check_result, _ = _check_and_runtime_check(execution, "build-verification")
        diagnosis = _make_diagnosis(check_result, affected_files=())
        result = repair_runtime_failure(check_result, diagnosis, execution, context, MockProvider(), root)
        data = runtime_repair_result_to_dict(result)
        for field in ("check_id", "outcome", "eligible", "verification_status", "explanation"):
            suite.check("serialized result has '{}'".format(field), field in data)
        rendered = render_runtime_repair_result(result)
        suite.check("rendered output states repair was not attempted", "not attempted" in rendered.lower())


# --- CLI ---------------------------------------------------------------

def test_cli_repair_runtime_flag_implies_diagnose(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package-lock.json", "{}")
        writer.write("package.json", json.dumps({"scripts": {"build": "node -e \"console.error('x'); process.exit(1)\""}}))
        proc = run_agent(["discover", str(root), "--repair-runtime", "--ai-provider", "mock"])
    suite.check("discover --repair-runtime exits 0", proc.returncode == 0)
    suite.check("output includes an AI Diagnosis section (implied)", "AI Diagnosis" in proc.stdout)
    suite.check("output includes a Runtime Repair section", "Runtime Repair" in proc.stdout)
    suite.check("no traceback leaks to the user", "Traceback" not in proc.stdout)


def test_cli_diagnose_alone_never_writes_any_file(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package-lock.json", "{}")
        writer.write("package.json", json.dumps({"scripts": {"build": "node -e \"console.error('x'); process.exit(1)\""}}))
        before = sorted(p.name for p in root.iterdir())
        proc = run_agent(["discover", str(root), "--diagnose", "--ai-provider", "mock"])
        after = sorted(p.name for p in root.iterdir())
    suite.check("discover --diagnose alone exits 0", proc.returncode == 0)
    suite.check("no Runtime Repair section without --repair-runtime", "Runtime Repair" not in proc.stdout)
    suite.check("--diagnose alone never writes any file", before == after)


if __name__ == "__main__":
    suite = Suite("Phase G Part 4: Runtime Failure -> Verified Repair Integration")
    sys.exit(suite.run([
        test_insufficient_context_diagnosis_is_not_eligible,
        test_invalid_response_diagnosis_is_not_eligible,
        test_ai_error_diagnosis_is_not_eligible,
        test_not_applicable_diagnosis_is_not_eligible_and_skipped_in_plural,
        test_missing_affected_file_is_not_eligible,
        test_nonexistent_affected_file_is_not_eligible,
        test_ambiguous_affected_files_is_not_eligible,
        test_environment_configuration_failure_with_missing_file_is_not_eligible,
        test_generated_or_dependency_path_is_rejected_as_repair_target,
        test_lock_file_is_rejected_as_repair_target,
        test_deterministic_target_resolution_matches_real_evidence,
        test_no_evidence_match_anchors_at_line_one_not_invented,
        test_repair_proposal_succeeds_and_targets_the_resolved_file,
        test_repair_proposal_failure_is_proposal_failed,
        test_eligible_diagnosis_full_happy_path_is_verified,
        test_runtime_candidate_check_fails_rejects_without_touching_real_repository,
        test_static_worsened_rejects_before_runtime_candidate_is_attempted,
        test_static_hold_without_known_runtime_check_stays_held,
        test_real_post_apply_verification_still_failing_is_reported_honestly,
        test_real_apply_failure_is_handled_safely,
        test_max_one_repair_attempt_per_check_no_retry_loop,
        test_mirror_with_patch_links_siblings_and_writes_only_the_target,
        test_ai_provider_output_is_never_written_directly_to_a_real_file,
        test_runtime_repair_result_to_dict_has_expected_shape,
        test_cli_repair_runtime_flag_implies_diagnose,
        test_cli_diagnose_alone_never_writes_any_file,
    ]))
