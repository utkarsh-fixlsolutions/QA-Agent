"""Phase G Part 3: the AI Runtime Failure Diagnosis Engine (docs/23-runtime
-failure-diagnosis-engine.md) - `qa_agent.ai.diagnose_runtime_failure(s)()`,
its prompt/grounding/validation, and the `discover --diagnose` CLI flag.

Every fixture goes through the real pipeline (`discover_project` ->
`build_repository_context` -> `plan_runtime_qa` -> `run_runtime_plan`) to
produce a real `RuntimeExecutionResult` before diagnosis ever runs - the
same "exercise the real thing, not an invented input" discipline every
prior Phase F/G suite in this project already follows. `MockProvider` (and
a small call-counting variant) stands in for the LLM everywhere except the
one guarded, optional live-Ollama dogfooding check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.ai import (  # noqa: E402
    DIAGNOSIS_AI_ERROR,
    DIAGNOSIS_DIAGNOSED,
    DIAGNOSIS_INSUFFICIENT_CONTEXT,
    DIAGNOSIS_INVALID_RESPONSE,
    DIAGNOSIS_NOT_APPLICABLE,
    ConnectionResult,
    LLMResponse,
    MockProvider,
    RuntimeDiagnosis,
    build_diagnosis_prompt,
    diagnose_runtime_failure,
    diagnose_runtime_failures,
    diagnosis_to_dict,
    diagnoses_to_json,
    render_diagnosis,
    response_is_grounded,
    validate_diagnosis_response,
)
from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.runtime import ExecutionConfig, plan_runtime_qa, run_runtime_plan  # noqa: E402


class _Writer:
    def __init__(self, path):
        self.path = path

    write = TempProject.write


class _CountingProvider:
    """Same shape as MockProvider, but counts real `generate()` calls - the
    one way to actually prove "PASS/SKIPPED/NOT_IMPLEMENTED never reaches
    the AI" rather than merely asserting on the output.
    """

    name = "counting"

    def __init__(self, response_text="mock response"):
        self.calls = 0
        self._response_text = response_text

    def generate(self, prompt):
        self.calls += 1
        return LLMResponse(text=self._response_text, provider=self.name, model="counting-model")

    def test_connection(self):
        return ConnectionResult(ok=True, detail="always reachable")


def _valid_json(**overrides):
    payload = {
        "summary": "The build script exited with a non-zero status.",
        "severity": "error",
        "confidence": 0.8,
        "root_cause": "The build process could not find the referenced module.",
        "evidence": ["Module not found"],
        "affected_files": [],
        "affected_components": [],
        "recommended_action": "Verify the referenced module exists.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _execute(files, config=None):
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        for rel, text in files.items():
            writer.write(rel, text)
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        plan = plan_runtime_qa(context)
        execution = run_runtime_plan(plan, proj.path, config)
        return execution, context, proj.path
    finally:
        proj.__exit__(None, None, None)


def _failing_build_fixture():
    return {
        "package-lock.json": "{}",
        "package.json": json.dumps({
            "scripts": {"build": "node -e \"console.error('Error: Cannot find module xyz'); process.exit(1)\""}
        }),
    }


def _passing_env_fixture():
    return {".env.example": "K=\n", ".env": "K=1\n"}


# --- RuntimeDiagnosis model contract --------------------------------------

def test_runtime_diagnosis_rejects_an_unknown_diagnosis_status(suite):
    try:
        RuntimeDiagnosis(check_id="x", check_name="X", execution_status="fail", diagnosis_status="weird")
        raised = False
    except ValueError:
        raised = True
    suite.check("an unrecognized diagnosis_status raises ValueError", raised)


def test_runtime_diagnosis_rejects_an_unknown_severity(suite):
    try:
        RuntimeDiagnosis(
            check_id="x", check_name="X", execution_status="fail",
            diagnosis_status=DIAGNOSIS_DIAGNOSED, severity="catastrophic",
        )
        raised = False
    except ValueError:
        raised = True
    suite.check("an unrecognized severity raises ValueError", raised)


# --- successful diagnosis --------------------------------------------------

def test_valid_diagnosis_of_a_real_failure(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    provider = MockProvider(response_text=_valid_json(affected_components=["xyz"]))
    diagnoses = diagnose_runtime_failures(execution, context, provider)
    build_diagnosis = next((d for d in diagnoses if d.check_id == "build-verification"), None)
    suite.check("a diagnosis was produced for the real build failure", build_diagnosis is not None)
    if build_diagnosis is not None:
        suite.check("status is DIAGNOSED", build_diagnosis.diagnosis_status == DIAGNOSIS_DIAGNOSED)
        suite.check("execution_status mirrors the real result", build_diagnosis.execution_status == "fail")
        suite.check("summary is populated", build_diagnosis.summary != "")
        suite.check("confidence is the real parsed value", build_diagnosis.confidence == 0.8)
        suite.check("model name is recorded", build_diagnosis.model == "mock-model")


def test_diagnoses_preserve_order_and_identity(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    provider = MockProvider(response_text=_valid_json())
    diagnoses = diagnose_runtime_failures(execution, context, provider)
    suite.check(
        "one diagnosis per execution result, in the same order",
        [d.check_id for d in diagnoses] == [r.id for r in execution.results],
    )


# --- structured failure modes ----------------------------------------------

def test_insufficient_context_decline(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    provider = MockProvider(response_text=json.dumps({"insufficient_context": True, "reason": "no stack trace"}))
    diagnoses = diagnose_runtime_failures(execution, context, provider)
    result = next(d for d in diagnoses if d.check_id == "build-verification")
    suite.check("the sanctioned decline shape -> INSUFFICIENT_CONTEXT", result.diagnosis_status == DIAGNOSIS_INSUFFICIENT_CONTEXT)
    suite.check("this is not treated as an AI error", result.diagnosis_status != DIAGNOSIS_AI_ERROR)
    suite.check("the model's own reason is preserved", result.error == "no stack trace")


def test_malformed_json_is_invalid_response(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    provider = MockProvider(response_text="not json at all")
    diagnoses = diagnose_runtime_failures(execution, context, provider)
    result = next(d for d in diagnoses if d.check_id == "build-verification")
    suite.check("malformed JSON -> INVALID_RESPONSE", result.diagnosis_status == DIAGNOSIS_INVALID_RESPONSE)


def test_markdown_fenced_valid_json_is_still_diagnosed(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    fenced = "```json\n{}\n```".format(_valid_json())
    diagnoses = diagnose_runtime_failures(execution, context, MockProvider(response_text=fenced))
    result = next(d for d in diagnoses if d.check_id == "build-verification")
    suite.check("valid JSON inside a markdown fence is still parsed and diagnosed", result.diagnosis_status == DIAGNOSIS_DIAGNOSED)


def test_missing_required_field_is_invalid_response(suite):
    payload = json.loads(_valid_json())
    del payload["root_cause"]
    result = validate_diagnosis_response(json.dumps(payload))
    suite.check("a missing required field is rejected", not result.ok)


def test_provider_failure_is_ai_error(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    provider = MockProvider(fail=True, failure_message="offline")
    diagnoses = diagnose_runtime_failures(execution, context, provider)
    result = next(d for d in diagnoses if d.check_id == "build-verification")
    suite.check("a failed provider call -> AI_ERROR", result.diagnosis_status == DIAGNOSIS_AI_ERROR)
    suite.check("the provider's real error is preserved", result.error == "offline")


# --- confidence validation --------------------------------------------------

def test_confidence_boundary_values_are_accepted(suite):
    for value in (0.0, 1.0, 0.5):
        result = validate_diagnosis_response(_valid_json(confidence=value))
        suite.check("confidence {} is accepted".format(value), result.ok)


def test_confidence_out_of_range_is_rejected(suite):
    for value in (-0.1, 1.1, 2, -5):
        result = validate_diagnosis_response(_valid_json(confidence=value))
        suite.check("confidence {} is rejected".format(value), not result.ok)


def test_confidence_boolean_is_rejected(suite):
    # bool is an int subclass in Python - json.dumps(True) -> "true", which
    # json.loads parses back to the Python bool True, not 1.
    raw = _valid_json().replace('"confidence": 0.8', '"confidence": true')
    result = validate_diagnosis_response(raw)
    suite.check("confidence: true (a JSON boolean) is rejected, not accepted as 1", not result.ok)


def test_confidence_string_is_rejected(suite):
    result = validate_diagnosis_response(_valid_json(confidence="0.8"))
    suite.check("a string confidence is rejected", not result.ok)


def test_confidence_nan_and_infinity_are_rejected(suite):
    for literal in ("NaN", "Infinity", "-Infinity"):
        raw = _valid_json().replace('"confidence": 0.8', '"confidence": {}'.format(literal))
        result = validate_diagnosis_response(raw)
        suite.check("confidence: {} is rejected".format(literal), not result.ok)


def test_invalid_severity_is_rejected(suite):
    result = validate_diagnosis_response(_valid_json(severity="catastrophic"))
    suite.check("an unrecognized severity value is rejected", not result.ok)


def test_evidence_must_be_a_list_of_strings(suite):
    result = validate_diagnosis_response(_valid_json(evidence="not a list"))
    suite.check("a bare string instead of a list is rejected", not result.ok)
    result2 = validate_diagnosis_response(_valid_json(evidence=[1, 2, 3]))
    suite.check("a list of non-strings is rejected", not result2.ok)


# --- PASS/SKIPPED/NOT_IMPLEMENTED never reach the AI ------------------------

def test_pass_result_does_not_trigger_ai(suite):
    execution, context, root = _execute(_passing_env_fixture())
    provider = _CountingProvider()
    diagnoses = diagnose_runtime_failures(execution, context, provider)
    result = next(d for d in diagnoses if d.check_id == "environment-configuration")
    suite.check("a PASS result -> NOT_APPLICABLE", result.diagnosis_status == DIAGNOSIS_NOT_APPLICABLE)
    suite.check("zero real provider calls were made for it", provider.calls == 0)


def test_skipped_result_does_not_trigger_ai(suite):
    # requirements.txt alone (no app.py/main.py entry point) plans both
    # build-verification (pip isn't a JS manager -> no build command
    # evidence) and server-startup (Flask's own framework evidence plans
    # it, but with no real entry point to launch, _discover_start_command
    # itself raises CheckSkipped) - both genuinely SKIPPED by the real
    # executor, neither diagnosable. Deliberately no app.py here: an
    # earlier version of this fixture included one, which gave
    # server-startup a real entry point to actually launch - it then
    # legitimately failed and was legitimately (correctly) diagnosed,
    # which is what first caught this fixture design mistake.
    execution, context, root = _execute({"requirements.txt": "flask\n"})
    provider = _CountingProvider()
    diagnoses = diagnose_runtime_failures(execution, context, provider)
    build_result = next((r for r in execution.results if r.id == "build-verification"), None)
    suite.check("build-verification was really SKIPPED by the executor", build_result is not None and build_result.status == "skipped")
    diagnosis = next(d for d in diagnoses if d.check_id == "build-verification")
    suite.check("a SKIPPED result -> NOT_APPLICABLE", diagnosis.diagnosis_status == DIAGNOSIS_NOT_APPLICABLE)
    suite.check("zero real provider calls were made across the whole run", provider.calls == 0)


def test_not_implemented_result_does_not_trigger_ai(suite):
    execution, context, root = _execute({"Dockerfile": "FROM node:20\n", "package.json": "{}"})
    provider = _CountingProvider()
    diagnoses = diagnose_runtime_failures(execution, context, provider)
    docker_diagnoses = [d for d in diagnoses if d.check_id.startswith("container-")]
    suite.check("at least one NOT_IMPLEMENTED docker check exists", len(docker_diagnoses) > 0)
    for diagnosis in docker_diagnoses:
        suite.check("'{}' -> NOT_APPLICABLE, not diagnosed".format(diagnosis.check_id), diagnosis.diagnosis_status == DIAGNOSIS_NOT_APPLICABLE)
    suite.check("zero provider calls for any not_implemented/pass check", provider.calls == 0)


# --- multiple failures, independence ---------------------------------------

def test_multiple_failed_checks_are_diagnosed_independently(suite):
    execution, context, root = _execute({
        "package-lock.json": "{}",
        "package.json": json.dumps({
            "scripts": {
                "build": "node -e \"console.error('Error: Cannot find module xyz'); process.exit(1)\"",
                "test": "node -e \"console.error('assertion failed'); process.exit(1)\"",
            }
        }),
        "tests/x.test.js": "test('x', () => {})\n",
    }, ExecutionConfig(build_timeout=15, test_timeout=15))
    calls = {"n": 0}

    class _AlternatingProvider:
        name = "alt"

        def generate(self, prompt):
            calls["n"] += 1
            if "Build Verification" in prompt:
                return LLMResponse(text=_valid_json(affected_components=["xyz"]), provider="alt", model="alt-model")
            return LLMResponse(text=json.dumps({"insufficient_context": True, "reason": "ambiguous"}), provider="alt", model="alt-model")

        def test_connection(self):
            return ConnectionResult(ok=True)

    diagnoses = diagnose_runtime_failures(execution, context, _AlternatingProvider())
    by_id = {d.check_id: d for d in diagnoses}
    suite.check("both real failures were attempted", calls["n"] == 2)
    suite.check("build-verification got a real DIAGNOSED result", by_id["build-verification"].diagnosis_status == DIAGNOSIS_DIAGNOSED)
    suite.check(
        "test-suite-verification independently got its own, different result (INSUFFICIENT_CONTEXT)",
        by_id["test-suite-verification"].diagnosis_status == DIAGNOSIS_INSUFFICIENT_CONTEXT,
    )


# --- evidence grounding: no invented findings -------------------------------

def test_ungrounded_affected_files_are_rejected(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    fabricated = _valid_json(affected_files=["src/db/connection.ts"], affected_components=["PostgreSQL"])
    diagnoses = diagnose_runtime_failures(execution, context, MockProvider(response_text=fabricated))
    result = next(d for d in diagnoses if d.check_id == "build-verification")
    suite.check(
        "a claim naming a file/component never present in the real evidence is rejected",
        result.diagnosis_status == DIAGNOSIS_INVALID_RESPONSE,
    )


def test_grounded_affected_component_is_accepted(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    # "xyz" genuinely appears in the real captured build output (see the fixture).
    grounded = _valid_json(affected_components=["xyz"])
    diagnoses = diagnose_runtime_failures(execution, context, MockProvider(response_text=grounded))
    result = next(d for d in diagnoses if d.check_id == "build-verification")
    suite.check("a claim that genuinely appears in the real captured output is accepted", result.diagnosis_status == DIAGNOSIS_DIAGNOSED)


def test_response_is_grounded_direct_unit(suite):
    from qa_agent.ai.diagnosis_parser import DiagnosisResponse

    class _FakeResult:
        logs = ("Error: Cannot find module xyz",)
        reason = "build failed"
        details = ""
        exception = None

    class _FakeProject:
        languages = ()
        frameworks = ()
        important_files = ()
        important_directories = ()
        runtime_files = ()

    class _FakeContext:
        project = _FakeProject()

    grounded_response = DiagnosisResponse(
        summary="x", severity="error", confidence=0.5, root_cause="x",
        evidence=("x",), affected_files=(), affected_components=("xyz",),
        recommended_action="x",
    )
    ungrounded_response = DiagnosisResponse(
        summary="x", severity="error", confidence=0.5, root_cause="x",
        evidence=("x",), affected_files=("nonexistent/path.py",), affected_components=(),
        recommended_action="x",
    )
    suite.check("a real substring match is grounded", response_is_grounded(grounded_response, _FakeResult(), _FakeContext()))
    suite.check("an invented file path is not grounded", not response_is_grounded(ungrounded_response, _FakeResult(), _FakeContext()))


# --- log handling: truncation, redaction, empty output ---------------------

def test_large_output_is_truncated_with_an_explicit_marker(suite):
    huge_line = "x" * 20000
    prompt = build_diagnosis_prompt_with_logs((huge_line,))
    suite.check("the prompt does not contain the full 20000-char line untruncated", huge_line not in prompt.user)
    suite.check("truncation is stated explicitly, never silent", "truncated" in prompt.user.lower())


def test_empty_output_is_stated_explicitly_not_left_blank(suite):
    prompt = build_diagnosis_prompt_with_logs(())
    suite.check("an empty log is stated explicitly", "no output was captured" in prompt.user.lower())


def test_secret_shaped_values_are_redacted_before_reaching_the_prompt(suite):
    secret = "sk_live_abcdef1234567890"
    prompt = build_diagnosis_prompt_with_logs(("API_KEY={}".format(secret),))
    suite.check("the raw secret value never appears in the built prompt", secret not in prompt.user)
    suite.check("a redaction marker appears in its place", "[REDACTED]" in prompt.user)


def build_diagnosis_prompt_with_logs(logs):
    """A minimal real `RuntimeCheckResult`/`RepositoryContext` pair, just to
    exercise `build_diagnosis_prompt`'s own log handling directly and
    quickly, without a full discover/plan/execute round trip.
    """
    from qa_agent.project.models import ProjectKnowledge
    from qa_agent.project.context import RepositoryContext
    from qa_agent.runtime.execution_models import STATUS_FAIL, RuntimeCheckResult

    check_result = RuntimeCheckResult(
        id="build-verification", name="Build Verification", status=STATUS_FAIL,
        start_time="", end_time="", duration=1.0, reason="build failed", logs=logs,
    )
    project = ProjectKnowledge(root_path="C:/fake", repository_type="single-package", application_type="backend")
    context = RepositoryContext(project=project)
    return build_diagnosis_prompt(check_result, context)


# --- serialization / rendering ---------------------------------------------

def test_diagnosis_to_dict_has_expected_shape(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    diagnoses = diagnose_runtime_failures(execution, context, MockProvider(response_text=_valid_json(affected_components=["xyz"])))
    data = diagnosis_to_dict(diagnoses[0])
    for field in ("check_id", "diagnosis_status", "summary", "confidence", "affected_files", "recommended_action"):
        suite.check("serialized diagnosis has '{}'".format(field), field in data)


def test_diagnoses_to_json_round_trips(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    diagnoses = diagnose_runtime_failures(execution, context, MockProvider(response_text=_valid_json(affected_components=["xyz"])))
    raw = diagnoses_to_json(diagnoses)
    try:
        parsed = json.loads(raw)
        ok = True
    except json.JSONDecodeError:
        parsed, ok = None, False
    suite.check("diagnoses_to_json produces valid JSON", ok)
    if ok:
        suite.check("round-tripped JSON matches diagnosis_to_dict exactly", parsed == [diagnosis_to_dict(d) for d in diagnoses])


def test_render_diagnosis_labels_output_as_ai_diagnosis(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    diagnoses = diagnose_runtime_failures(execution, context, MockProvider(response_text=_valid_json(affected_components=["xyz"])))
    rendered = render_diagnosis(diagnoses[0])
    suite.check("rendered output is explicitly labeled 'AI Diagnosis'", "AI Diagnosis" in rendered)
    suite.check("rendered output names the real root cause", "referenced module" in rendered)


def test_render_diagnosis_not_applicable_is_empty(suite):
    execution, context, root = _execute(_passing_env_fixture())
    diagnoses = diagnose_runtime_failures(execution, context, MockProvider())
    result = next(d for d in diagnoses if d.check_id == "environment-configuration")
    suite.check("a NOT_APPLICABLE diagnosis renders to nothing", render_diagnosis(result) == "")


# --- never raises, works without a runtime_check ---------------------------

def test_diagnose_runtime_failure_never_raises_on_a_broken_provider(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    check_result = next(r for r in execution.results if r.id == "build-verification")

    class _ExplodingProvider:
        name = "exploding"

        def generate(self, prompt):
            raise RuntimeError("boom")

        def test_connection(self):
            return ConnectionResult(ok=False)

    try:
        diagnosis = diagnose_runtime_failure(check_result, context, _ExplodingProvider())
        raised = False
    except Exception:
        raised = True
    suite.check("a provider that raises never crashes diagnose_runtime_failure", not raised)
    if not raised:
        suite.check("the result is AI_ERROR with the real exception text preserved", diagnosis.diagnosis_status == DIAGNOSIS_AI_ERROR and "boom" in diagnosis.error)


def test_diagnose_runtime_failure_works_without_a_known_runtime_check(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    check_result = next(r for r in execution.results if r.id == "build-verification")
    diagnosis = diagnose_runtime_failure(check_result, context, MockProvider(response_text=_valid_json(affected_components=["xyz"])), runtime_check=None)
    suite.check("diagnosis works fine with runtime_check=None", diagnosis.diagnosis_status == DIAGNOSIS_DIAGNOSED)


# --- backward compatibility / isolation -------------------------------------

def test_deterministic_execution_result_is_never_mutated(suite):
    execution, context, root = _execute(_failing_build_fixture(), ExecutionConfig(build_timeout=15))
    before = [(r.id, r.status, r.reason) for r in execution.results]
    diagnose_runtime_failures(execution, context, MockProvider(response_text=_valid_json(affected_components=["xyz"])))
    after = [(r.id, r.status, r.reason) for r in execution.results]
    suite.check("the deterministic RuntimeExecutionResult is byte-identical before and after diagnosis", before == after)


def test_runtime_package_still_never_imports_qa_agent_ai(suite):
    import re
    pattern = re.compile(r"^\s*(import qa_agent\.ai|from \.\.ai\b|from \.ai\b|from qa_agent\.ai\b)", re.MULTILINE)
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "runtime"
    offending = []
    for path in package_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offending.append(path.name)
    suite.check(
        "no file in qa_agent/runtime/ imports qa_agent.ai, even after Phase G Part 3",
        offending == [], " ({})".format(offending),
    )


def test_diagnosis_modules_never_execute_subprocesses_or_touch_the_filesystem(suite):
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "ai"
    offending = []
    for name in ("diagnosis.py", "diagnosis_models.py", "diagnosis_prompts.py", "diagnosis_parser.py"):
        text = (package_dir / name).read_text(encoding="utf-8")
        if "import subprocess" in text or "os.remove" in text or "unlink(" in text or "shutil." in text:
            offending.append(name)
    suite.check("no diagnosis module executes subprocesses or modifies files", offending == [], " ({})".format(offending))


# --- CLI ---------------------------------------------------------------

def test_cli_diagnose_flag_implies_execution_and_plan(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package-lock.json", "{}")
        writer.write("package.json", json.dumps({"scripts": {"build": "node -e \"console.error('x'); process.exit(1)\""}}))
        proc = run_agent(["discover", str(root), "--diagnose", "--ai-provider", "mock"])
    suite.check("discover --diagnose exits 0", proc.returncode == 0)
    suite.check("output includes the Runtime QA Plan (implied)", "Runtime QA Plan" in proc.stdout)
    suite.check("output includes Execution Results (implied)", "Execution Results" in proc.stdout)
    suite.check("output includes an AI Diagnosis section", "AI Diagnosis" in proc.stdout)


def test_cli_without_diagnose_flag_makes_zero_ai_calls(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write(".env.example", "K=\n")
        writer.write(".env", "K=1\n")
        proc = run_agent(["discover", str(root), "--execute-runtime-plan"])
    suite.check("no --diagnose -> no AI Diagnosis section at all", "AI Diagnosis" not in proc.stdout)


def test_cli_diagnose_with_mock_provider_never_needs_ollama(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write(".env.example", "K=\n")
        proc = run_agent(["discover", str(root), "--diagnose", "--ai-provider", "mock"])
    suite.check("discover --diagnose --ai-provider mock exits 0 without any real Ollama server", proc.returncode == 0)
    suite.check("no traceback leaks to the user", "Traceback" not in proc.stdout)


if __name__ == "__main__":
    suite = Suite("Phase G Part 3: AI Runtime Failure Diagnosis Engine")
    sys.exit(suite.run([
        test_runtime_diagnosis_rejects_an_unknown_diagnosis_status,
        test_runtime_diagnosis_rejects_an_unknown_severity,
        test_valid_diagnosis_of_a_real_failure,
        test_diagnoses_preserve_order_and_identity,
        test_insufficient_context_decline,
        test_malformed_json_is_invalid_response,
        test_markdown_fenced_valid_json_is_still_diagnosed,
        test_missing_required_field_is_invalid_response,
        test_provider_failure_is_ai_error,
        test_confidence_boundary_values_are_accepted,
        test_confidence_out_of_range_is_rejected,
        test_confidence_boolean_is_rejected,
        test_confidence_string_is_rejected,
        test_confidence_nan_and_infinity_are_rejected,
        test_invalid_severity_is_rejected,
        test_evidence_must_be_a_list_of_strings,
        test_pass_result_does_not_trigger_ai,
        test_skipped_result_does_not_trigger_ai,
        test_not_implemented_result_does_not_trigger_ai,
        test_multiple_failed_checks_are_diagnosed_independently,
        test_ungrounded_affected_files_are_rejected,
        test_grounded_affected_component_is_accepted,
        test_response_is_grounded_direct_unit,
        test_large_output_is_truncated_with_an_explicit_marker,
        test_empty_output_is_stated_explicitly_not_left_blank,
        test_secret_shaped_values_are_redacted_before_reaching_the_prompt,
        test_diagnosis_to_dict_has_expected_shape,
        test_diagnoses_to_json_round_trips,
        test_render_diagnosis_labels_output_as_ai_diagnosis,
        test_render_diagnosis_not_applicable_is_empty,
        test_diagnose_runtime_failure_never_raises_on_a_broken_provider,
        test_diagnose_runtime_failure_works_without_a_known_runtime_check,
        test_deterministic_execution_result_is_never_mutated,
        test_runtime_package_still_never_imports_qa_agent_ai,
        test_diagnosis_modules_never_execute_subprocesses_or_touch_the_filesystem,
        test_cli_diagnose_flag_implies_execution_and_plan,
        test_cli_without_diagnose_flag_makes_zero_ai_calls,
        test_cli_diagnose_with_mock_provider_never_needs_ollama,
    ]))
