"""tools/model_bench.py - the standalone local-model benchmark harness
(docs/26-model-benchmark.md). Tests the harness itself, not any model's
answers - a fake, deterministic provider drives every test here, so this
suite runs offline and needs no live Ollama server, matching this
project's own "MockProvider stands in for the LLM everywhere except an
explicit, guarded live check" convention. Deliberately does not duplicate
G3/G4's own validator/grounding tests - this suite only proves the
benchmark harness calls that real, unmodified machinery correctly, never
that the machinery itself is correct (already proven elsewhere).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
from harness import Suite  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tools"))
import bench_fixtures  # noqa: E402
from bench_tasks import build_all_tasks  # noqa: E402
import model_bench as mb  # noqa: E402

from qa_agent.ai import ConnectionResult, LLMResponse  # noqa: E402


class _FixedProvider:
    """Always returns the same, fixed `LLMResponse` - the same role
    `MockProvider` already plays elsewhere, kept local so this suite has no
    dependency on `qa_agent.ai.MockProvider`'s own specific shape.
    """

    name = "fixed"

    def __init__(self, model, text="", ok=True, error="boom"):
        self.model = model
        self._text = text
        self._ok = ok
        self._error = error
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        if not self._ok:
            return LLMResponse(error=self._error, provider=self.name, model=self.model)
        return LLMResponse(text=self._text, provider=self.name, model=self.model, latency_seconds=0.001)

    def test_connection(self):
        return ConnectionResult(ok=self._ok)


def _valid_diagnosis_json():
    return json.dumps({
        "summary": "The build failed because lodash could not be found.",
        "severity": "error", "confidence": 0.9,
        "root_cause": "The lodash module is missing.",
        "evidence": ["Cannot find module 'lodash'"],
        "affected_files": [], "affected_components": ["lodash"],
        "recommended_action": "Install lodash.",
    })


def _decline_json():
    return json.dumps({"insufficient_context": True, "reason": "not enough evidence"})


# --- CLI parsing --------------------------------------------------------

def test_multiple_models_are_accepted(suite):
    parser = mb.build_arg_parser()
    args = parser.parse_args(["--models", "modelA", "modelB", "modelC"])
    suite.check("all three models are parsed, in order", args.models == ["modelA", "modelB", "modelC"])


def test_single_model_is_also_accepted(suite):
    parser = mb.build_arg_parser()
    args = parser.parse_args(["--models", "qwen2.5:0.5b"])
    suite.check("a single model is parsed as a one-element list", args.models == ["qwen2.5:0.5b"])


def test_repeats_defaults_to_five_and_can_be_overridden(suite):
    parser = mb.build_arg_parser()
    default_args = parser.parse_args(["--models", "m"])
    override_args = parser.parse_args(["--models", "m", "--repeats", "3"])
    suite.check("--repeats defaults to 5", default_args.repeats == 5)
    suite.check("--repeats can be overridden", override_args.repeats == 3)


# --- repetition count is honored by the runner, not just parsed --------

def test_repetition_count_produces_that_many_attempts_per_task(suite):
    tree = bench_fixtures.build_fixture_tree()
    try:
        tasks = build_all_tasks(tree)[:2]
        provider = _FixedProvider("m", text=_decline_json())
        results = mb.run_benchmark(["m"], tasks, 3, lambda name: provider)
        suite.check("2 tasks x 3 repeats = 6 attempts", len(results) == 6)
        for task in tasks:
            reps = sorted(a.repetition for a in results if a.task_id == task.id)
            suite.check("task '{}' got reps 1,2,3 in order".format(task.id), reps == [1, 2, 3])
    finally:
        tree.cleanup()


# --- failure isolation ---------------------------------------------------

def test_one_failing_task_does_not_stop_the_benchmark(suite):
    tree = bench_fixtures.build_fixture_tree()
    try:
        tasks = build_all_tasks(tree)[:3]

        # Force the crash deterministically on the 2nd attempt overall,
        # regardless of which task it lands on - simpler and just as
        # meaningful as targeting one specific task by id.
        calls = {"n": 0}

        class _CountingFlakyProvider:
            name = "flaky"

            def __init__(self, model):
                self.model = model

            def generate(self, prompt):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("simulated crash")
                return LLMResponse(text=_decline_json(), provider=self.name, model=self.model)

            def test_connection(self):
                return ConnectionResult(ok=True)

        results = mb.run_benchmark(["m"], tasks, 1, lambda name: _CountingFlakyProvider(name))
        suite.check("all 3 attempts are still recorded despite one crashing", len(results) == 3)
        crashed = [r for r in results if not r.provider_ok and r.provider_error and "simulated crash" in r.provider_error]
        suite.check("exactly one attempt recorded the crash, not raised", len(crashed) == 1)
        others_ok = [r for r in results if r.provider_ok]
        suite.check("the other two attempts completed normally", len(others_ok) == 2)
    finally:
        tree.cleanup()


def test_a_provider_error_never_raises_out_of_run_one_attempt(suite):
    tree = bench_fixtures.build_fixture_tree()
    try:
        task = build_all_tasks(tree)[0]
        provider = _FixedProvider("m", ok=False, error="offline")
        attempt = mb.run_one_attempt("m", task, lambda name: provider, 1)
        suite.check("a provider failure is recorded, not raised", attempt.provider_ok is False)
        suite.check("the real error is preserved", attempt.provider_error == "offline")
        suite.check("schema_status reflects the provider error", attempt.schema_status == "provider_error")
    finally:
        tree.cleanup()


# --- results are persisted correctly ------------------------------------

def test_results_are_persisted_as_valid_json_with_the_expected_shape(suite):
    tree = bench_fixtures.build_fixture_tree()
    try:
        tasks = build_all_tasks(tree)[:2]
        provider = _FixedProvider("m", text=_decline_json())
        results = mb.run_benchmark(["m"], tasks, 2, lambda name: provider)
        with_temp = REPO_ROOT / "tests" / "_bench_tmp_output"
        path = mb.save_results(results, tasks, ["m"], 2, output_dir=with_temp)
        try:
            suite.check("a results file was created", path.is_file())
            data = json.loads(path.read_text(encoding="utf-8"))
            suite.check("top-level 'models' matches", data["models"] == ["m"])
            suite.check("top-level 'repeats' matches", data["repeats"] == 2)
            suite.check("'tasks' lists both task ids", {t["id"] for t in data["tasks"]} == {t.id for t in tasks})
            suite.check("'attempts' has 4 entries (2 tasks x 2 reps)", len(data["attempts"]) == 4)
            suite.check("each attempt has the expected fields", all(
                "model" in a and "task_id" in a and "schema_status" in a and "latency_seconds" in a
                and "started_at" in a and "finished_at" in a
                for a in data["attempts"]
            ))
        finally:
            if path.is_file():
                path.unlink()
            if with_temp.is_dir() and not any(with_temp.iterdir()):
                with_temp.rmdir()
    finally:
        tree.cleanup()


# --- real validators are actually invoked, not bypassed -------------------

def test_a_genuinely_valid_response_is_scored_success_by_the_real_validator(suite):
    tree = bench_fixtures.build_fixture_tree()
    try:
        task = next(t for t in build_all_tasks(tree) if t.id == "A1-build-failure-clear-cause")
        provider = _FixedProvider("m", text=_valid_diagnosis_json())
        attempt = mb.run_one_attempt("m", task, lambda name: provider, 1)
        suite.check("a genuinely valid, grounded response validates as success", attempt.schema_status == "success")
        suite.check("grounding was actually checked and passed", attempt.grounded is True)
    finally:
        tree.cleanup()


def test_a_malformed_response_is_scored_invalid_by_the_real_validator(suite):
    tree = bench_fixtures.build_fixture_tree()
    try:
        task = next(t for t in build_all_tasks(tree) if t.id == "A1-build-failure-clear-cause")
        provider = _FixedProvider("m", text="not json at all")
        attempt = mb.run_one_attempt("m", task, lambda name: provider, 1)
        suite.check("malformed JSON is scored invalid, not silently accepted", attempt.schema_status == "invalid")
        suite.check("no correctness verdict is fabricated for an invalid response",
                     attempt.correctness is None and attempt.correctness_source == "not_applicable")
    finally:
        tree.cleanup()


def test_an_ungrounded_claim_is_rejected_by_the_real_grounding_check(suite):
    tree = bench_fixtures.build_fixture_tree()
    try:
        task = next(t for t in build_all_tasks(tree) if t.id == "A1-build-failure-clear-cause")
        fabricated = json.dumps({
            "summary": "x", "severity": "error", "confidence": 0.9, "root_cause": "x",
            "evidence": ["x"], "affected_files": ["src/db/connection.ts"], "affected_components": [],
            "recommended_action": "x",
        })
        provider = _FixedProvider("m", text=fabricated)
        attempt = mb.run_one_attempt("m", task, lambda name: provider, 1)
        suite.check("schema itself is valid", attempt.schema_status == "success")
        suite.check("a claim naming something never in the evidence is rejected by real grounding",
                     attempt.grounded is False)
        suite.check("an ungrounded claim is mechanically scored 0, never left ambiguous",
                     attempt.correctness == 0 and attempt.correctness_source == "mechanical")
    finally:
        tree.cleanup()


def test_a_decline_is_recognized_and_mechanically_scored_where_expected(suite):
    tree = bench_fixtures.build_fixture_tree()
    try:
        task = next(t for t in build_all_tasks(tree) if t.id == "B4-no-identifiable-file")
        provider = _FixedProvider("m", text=_decline_json())
        attempt = mb.run_one_attempt("m", task, lambda name: provider, 1)
        suite.check("the real validator recognizes the decline shape", attempt.schema_status == "insufficient_context")
        suite.check("declining when no file is identifiable is mechanically scored correct (2)",
                     attempt.correctness == 2 and attempt.correctness_source == "mechanical")
    finally:
        tree.cleanup()


# --- missing model handling ------------------------------------------------

def test_missing_model_is_reported_clearly_without_pulling_or_substituting(suite):
    def fake_list_models(endpoint):
        return ["some-other-model:latest"]

    availability = mb.check_models_available(["missing-model"], "http://fake:1", list_models_fn=fake_list_models)
    ok, reason = availability["missing-model"]
    suite.check("a genuinely absent model is reported unavailable", ok is False)
    suite.check("the reason is a clear, specific message", "missing-model" in reason and "not installed" in reason)
    suite.check("the reason explicitly states it will not auto-pull", "not pulling automatically" in reason)


def test_an_available_model_matches_by_exact_or_bare_name(suite):
    def fake_list_models(endpoint):
        return ["qwen2.5:0.5b", "llama3:latest"]

    availability = mb.check_models_available(["qwen2.5:0.5b", "llama3", "nonexistent"], "http://fake:1",
                                               list_models_fn=fake_list_models)
    suite.check("an exact 'name:tag' match is available", availability["qwen2.5:0.5b"][0] is True)
    suite.check("a bare name matches an installed 'name:tag'", availability["llama3"][0] is True)
    suite.check("a genuinely absent model is not", availability["nonexistent"][0] is False)


def test_unreachable_ollama_is_reported_clearly_not_raised(suite):
    def failing_list_models(endpoint):
        raise OSError("connection refused")

    availability = mb.check_models_available(["m"], "http://fake:1", list_models_fn=failing_list_models)
    ok, reason = availability["m"]
    suite.check("an unreachable server is reported, not raised", ok is False)
    suite.check("the reason names the real problem", "connection refused" in reason or "could not reach" in reason)


# --- production qa_agent code is never modified by running the benchmark --

def _hash_qa_agent_tree():
    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / "qa_agent").rglob("*.py")):
        digest.update(str(path.relative_to(REPO_ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_production_qa_agent_source_is_byte_identical_after_a_full_benchmark_run(suite):
    before = _hash_qa_agent_tree()
    tree = bench_fixtures.build_fixture_tree()
    try:
        tasks = build_all_tasks(tree)
        provider = _FixedProvider("m", text=_decline_json())
        mb.run_benchmark(["m"], tasks, 1, lambda name: provider)
    finally:
        tree.cleanup()
    after = _hash_qa_agent_tree()
    suite.check("every file under qa_agent/ is byte-identical before and after a full benchmark run", before == after)


def test_benchmark_source_never_writes_to_a_qa_agent_path(suite):
    """Precise, line-level check (not "the file mentions qa_agent
    somewhere") - every real write call in tools/*.py is a one-liner
    against a local variable (`target.write_text(...)`,
    `path.write_text(...)`); this fails only if a write call's own line
    literally names a qa_agent path.
    """
    tools_dir = REPO_ROOT / "tools"
    write_call_re = re.compile(r"\.write_(?:text|bytes)\(")
    offending = []
    for path in tools_dir.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if write_call_re.search(line) and "qa_agent" in line:
                offending.append("{}: {}".format(path.name, line.strip()))
    suite.check("no write call in tools/*.py targets a path built from 'qa_agent'", offending == [], " ({})".format(offending))


if __name__ == "__main__":
    suite = Suite("Model Benchmark Harness (tools/model_bench.py)")
    sys.exit(suite.run([
        test_multiple_models_are_accepted,
        test_single_model_is_also_accepted,
        test_repeats_defaults_to_five_and_can_be_overridden,
        test_repetition_count_produces_that_many_attempts_per_task,
        test_one_failing_task_does_not_stop_the_benchmark,
        test_a_provider_error_never_raises_out_of_run_one_attempt,
        test_results_are_persisted_as_valid_json_with_the_expected_shape,
        test_a_genuinely_valid_response_is_scored_success_by_the_real_validator,
        test_a_malformed_response_is_scored_invalid_by_the_real_validator,
        test_an_ungrounded_claim_is_rejected_by_the_real_grounding_check,
        test_a_decline_is_recognized_and_mechanically_scored_where_expected,
        test_missing_model_is_reported_clearly_without_pulling_or_substituting,
        test_an_available_model_matches_by_exact_or_bare_name,
        test_unreachable_ollama_is_reported_clearly_not_raised,
        test_production_qa_agent_source_is_byte_identical_after_a_full_benchmark_run,
        test_benchmark_source_never_writes_to_a_qa_agent_path,
    ]))
