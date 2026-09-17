#!/usr/bin/env python
"""Standalone local-model benchmark for QA-Agent's AI layer.

    python tools/model_bench.py --models qwen2.5:0.5b
    python tools/model_bench.py --models modelA modelB modelC --repeats 5

Sends real prompts (built by the real `qa_agent.ai` prompt builders) to one
or more real Ollama models via the real, unmodified `OllamaProvider`, and
scores every response with the real, unmodified `qa_agent.ai` validators
(`validate_diagnosis_response`, `validate_repair_response`,
`validate_explanation_response`, `validate_summary_response`,
`response_is_grounded`). See docs/26-model-benchmark.md for the full
design, the task list, and how to read the output.

Lives entirely outside `qa_agent/` - this tool evaluates the production
system, it is not part of it. It never writes to any `qa_agent/` path,
never executes anything the model generates, never gives the model shell
or filesystem-write access, and never changes the production AI
provider/model default (that remains `qa_agent/config.py`'s own
`AIConfig`/CLI flags, untouched by this file). See `tests/regression/
test_model_bench.py` for a source-level and execution-level proof of all
of that.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
_REPO_ROOT = _TOOLS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import bench_fixtures  # noqa: E402
from bench_tasks import BenchTask, build_all_tasks  # noqa: E402

from qa_agent.ai.ollama import DEFAULT_ENDPOINT, DEFAULT_TIMEOUT_SECONDS, OllamaProvider  # noqa: E402
from qa_agent.ai.schemas import STATUS_INSUFFICIENT_CONTEXT, STATUS_SUCCESS  # noqa: E402

DEFAULT_REPEATS = 5
DEFAULT_RESULTS_DIR = _REPO_ROOT / "benchmark_results"


# --- one recorded attempt ---------------------------------------------------

@dataclass
class TaskAttemptResult:
    """Every dimension is recorded separately - see the module/task
    docstrings for why (`Do not collapse these into one score`). `grounded`
    and `correctness` are `None` whenever they genuinely do not apply
    (a Category F probe, or a response that never reached a stage where
    grounding/correctness is even meaningful) - never coerced into a false
    0 or a misleading average.
    """

    model: str
    task_id: str
    category: str
    kind: str
    repetition: int
    started_at: str
    finished_at: str
    latency_seconds: float
    provider_ok: bool
    provider_error: Optional[str]
    schema_status: str  # "success" | "insufficient_context" | "invalid" | "provider_error" | "not_applicable"
    schema_reason: Optional[str]
    grounded: Optional[bool]
    constraints: Dict[str, bool]
    correctness: Optional[int]
    correctness_source: str  # "mechanical" | "manual_pending" | "not_applicable"
    raw_response: str


def _prompt_text(prompt) -> str:
    return "{}\n\n{}".format(prompt.system, prompt.user)


def _strict_json_only(raw: str) -> bool:
    try:
        json.loads(raw.strip())
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _score_correctness(task: BenchTask, result, grounded, raw, json_only):
    """The one shared correctness policy every task is scored under - see
    `bench_tasks.py`'s own module docstring for the full explanation.
    """
    if result is not None and result.status == STATUS_SUCCESS and grounded is False:
        return 0, "mechanical"
    if task.mechanical_score is not None:
        score = task.mechanical_score(result, grounded, raw, json_only)
        if score is not None:
            return score, "mechanical"
    if result is not None and result.status == STATUS_SUCCESS:
        return None, "manual_pending"
    return None, "not_applicable"


def run_one_attempt(model: str, task: BenchTask, provider_factory, repetition: int) -> TaskAttemptResult:
    """One real (or injected, for tests) provider call, scored against one
    task. Never raises - every failure mode becomes a structured result
    with `provider_ok=False` or `schema_status` naming what went wrong,
    exactly like every "did it work" boundary elsewhere in this project.
    """
    provider = provider_factory(model)
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    response = provider.generate(_prompt_text(task.prompt))
    latency = getattr(response, "latency_seconds", None)
    if latency is None:
        latency = time.perf_counter() - t0
    finished = datetime.now(timezone.utc).isoformat()

    if not response.ok:
        return TaskAttemptResult(
            model=model, task_id=task.id, category=task.category, kind=task.kind, repetition=repetition,
            started_at=started, finished_at=finished, latency_seconds=latency,
            provider_ok=False, provider_error=response.error,
            schema_status="provider_error", schema_reason=response.error,
            grounded=None, constraints={}, correctness=None, correctness_source="not_applicable", raw_response="",
        )

    if task.validate is None:
        # Category F probe - no schema exists to validate against (see
        # bench_tasks.py's module docstring); informational only.
        return TaskAttemptResult(
            model=model, task_id=task.id, category=task.category, kind=task.kind, repetition=repetition,
            started_at=started, finished_at=finished, latency_seconds=latency,
            provider_ok=True, provider_error=None,
            schema_status="not_applicable", schema_reason=None,
            grounded=None, constraints={}, correctness=None, correctness_source="not_applicable",
            raw_response=response.text,
        )

    result = task.validate(response.text)
    if result.status == STATUS_SUCCESS:
        schema_status = "success"
    elif result.status == STATUS_INSUFFICIENT_CONTEXT:
        schema_status = "insufficient_context"
    else:
        schema_status = "invalid"

    grounded = None
    if schema_status == "success" and task.ground is not None:
        try:
            grounded = bool(task.ground(result.value))
        except Exception:  # noqa: BLE001 - a grounding check must never abort the benchmark
            grounded = False

    json_only = _strict_json_only(response.text)
    constraints = {"json_only": json_only}
    for name, checker in task.constraint_checks:
        try:
            constraints[name] = bool(checker(response.text, result))
        except Exception:  # noqa: BLE001
            constraints[name] = False

    correctness, source = _score_correctness(task, result, grounded, response.text, json_only)

    return TaskAttemptResult(
        model=model, task_id=task.id, category=task.category, kind=task.kind, repetition=repetition,
        started_at=started, finished_at=finished, latency_seconds=latency,
        provider_ok=True, provider_error=None,
        schema_status=schema_status, schema_reason=getattr(result, "reason", None),
        grounded=grounded, constraints=constraints, correctness=correctness, correctness_source=source,
        raw_response=response.text,
    )


def run_benchmark(models: List[str], tasks: List[BenchTask], repeats: int, provider_factory,
                   on_attempt: Optional[Callable[[TaskAttemptResult], None]] = None) -> List[TaskAttemptResult]:
    """Every (model, task, repetition) combination, in a fixed order, each
    fully isolated - one failing attempt (a provider timeout, a malformed
    response, an unexpected exception anywhere in scoring) never stops the
    next attempt, the next task, or the next model.
    """
    results: List[TaskAttemptResult] = []
    for model in models:
        for task in tasks:
            for repetition in range(1, repeats + 1):
                try:
                    attempt = run_one_attempt(model, task, provider_factory, repetition)
                except Exception as exc:  # noqa: BLE001 - the benchmark itself must never crash mid-run
                    attempt = TaskAttemptResult(
                        model=model, task_id=task.id, category=task.category, kind=task.kind,
                        repetition=repetition, started_at="", finished_at="", latency_seconds=0.0,
                        provider_ok=False,
                        provider_error="benchmark harness error: {}: {}".format(type(exc).__name__, exc),
                        schema_status="provider_error", schema_reason=None, grounded=None,
                        constraints={}, correctness=None, correctness_source="not_applicable", raw_response="",
                    )
                results.append(attempt)
                if on_attempt is not None:
                    on_attempt(attempt)
    return results


# --- model availability (never auto-pulled, never substituted) -------------

def default_list_models(endpoint: str) -> List[str]:
    import urllib.request

    request = urllib.request.Request(endpoint.rstrip("/") + "/api/tags", method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [m.get("name") for m in data.get("models", []) if isinstance(m, dict) and m.get("name")]


def check_models_available(models: List[str], endpoint: str, list_models_fn=default_list_models) -> Dict[str, tuple]:
    """`{model: (ok, reason_or_None)}` for every requested model. Never
    pulls a model, never substitutes one for another - a model this
    reports unavailable is simply excluded by the caller.
    """
    try:
        installed = set(list_models_fn(endpoint))
    except Exception as exc:  # noqa: BLE001 - reachability failure must not crash the CLI
        reason = "could not reach Ollama at '{}' to check installed models: {}".format(endpoint, exc)
        return {m: (False, reason) for m in models}

    result = {}
    for m in models:
        # An exact "name:tag" match, or a bare name matching any installed
        # "name:*" tag - never a fuzzy/partial match beyond that, so a
        # genuine typo is still reported, not silently "close enough".
        ok = m in installed or any(i.split(":")[0] == m for i in installed)
        reason = None if ok else "model '{}' is not installed/available at '{}' (not pulling automatically)".format(m, endpoint)
        result[m] = (ok, reason)
    return result


def default_provider_factory(endpoint: str, timeout: float):
    def factory(model_name: str):
        return OllamaProvider(endpoint=endpoint, model=model_name, timeout=timeout)
    return factory


# --- persistence -------------------------------------------------------------

def save_results(results: List[TaskAttemptResult], tasks: List[BenchTask], models: List[str], repeats: int,
                  output_dir=None) -> Path:
    output_dir = Path(output_dir) if output_dir else DEFAULT_RESULTS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / "{}.json".format(timestamp)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "models": models,
        "repeats": repeats,
        "tasks": [
            {"id": t.id, "category": t.category, "kind": t.kind, "description": t.description,
             "scoring_guidance": t.scoring_guidance}
            for t in tasks
        ],
        "attempts": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# --- human-readable summary -------------------------------------------------

def _fmt_pct(n, d):
    return "n/a" if d == 0 else "{:.0f}%".format(100 * n / d)


def print_summary(results: List[TaskAttemptResult], tasks: List[BenchTask]) -> None:
    tasks_by_id = {t.id: t for t in tasks}
    by_model: Dict[str, List[TaskAttemptResult]] = {}
    for r in results:
        by_model.setdefault(r.model, []).append(r)

    for model in by_model:
        attempts = by_model[model]
        print("=" * 78)
        print("MODEL: {}".format(model))
        print("=" * 78)

        by_category: Dict[str, List[TaskAttemptResult]] = {}
        for a in attempts:
            by_category.setdefault(a.category, []).append(a)

        header = "{:<9}{:>14}{:>11}{:>18}{:>14}{:>12}".format(
            "CATEGORY", "SCHEMA VALID%", "GROUNDED%", "AVG CORRECTNESS", "CONSTRAINT%", "AVG LAT(s)")
        print(header)
        print("-" * len(header))

        category_consistency: Dict[str, float] = {}
        for category in sorted(c for c in by_category if c != "F"):
            cat = by_category[category]
            total = len(cat)
            # "Schema valid" means the model followed the response contract -
            # either a full structured answer or the sanctioned
            # insufficient_context decline (both are schema-compliant, see
            # response_parser.py's own three-state ValidationResult). Only
            # "invalid" (malformed JSON, missing fields, ...) counts against
            # this. Consistency (below) is the stricter, separate metric for
            # "produced a genuinely usable, grounded answer".
            schema_valid = sum(1 for a in cat if a.schema_status in ("success", "insufficient_context"))
            grounded_applicable = [a for a in cat if a.grounded is not None]
            grounded_ok = sum(1 for a in grounded_applicable if a.grounded)
            constraint_applicable = [a for a in cat if a.constraints]
            constraint_ok = sum(1 for a in constraint_applicable if all(a.constraints.values()))
            scored = [a for a in cat if a.correctness is not None]
            avg_correctness = "{:.2f}/2 ({}/{})".format(
                sum(a.correctness for a in scored) / len(scored), len(scored), total,
            ) if scored else "manual review"
            avg_latency = sum(a.latency_seconds for a in cat) / total if total else 0.0
            consistency = sum(
                1 for a in cat if a.schema_status == "success" and a.grounded is not False
            ) / total if total else 0.0
            category_consistency[category] = consistency

            print("{:<9}{:>14}{:>11}{:>18}{:>14}{:>12.2f}".format(
                category, _fmt_pct(schema_valid, total), _fmt_pct(grounded_ok, len(grounded_applicable)),
                avg_correctness, _fmt_pct(constraint_ok, len(constraint_applicable)), avg_latency,
            ))
        print()

        if category_consistency:
            weakest = min(category_consistency, key=category_consistency.get)
            print("  Weakest category (by valid+grounded consistency): {} ({:.0%})".format(
                weakest, category_consistency[weakest]))

        consistency_by_task: Dict[str, float] = {}
        for a in attempts:
            if a.category == "F":
                continue
            key = a.task_id
            consistency_by_task.setdefault(key, []).append(1 if a.schema_status == "success" and a.grounded is not False else 0)
        for task_id, hits in sorted(consistency_by_task.items()):
            rate = sum(hits) / len(hits)
            if rate < 1.0:
                print("  consistency  {:<45} {}/{}".format(task_id, sum(hits), len(hits)))

        provider_errors = [a for a in attempts if not a.provider_ok]
        invalid = [a for a in attempts if a.provider_ok and a.schema_status == "invalid"]
        if provider_errors:
            print()
            print("  Provider errors ({}):".format(len(provider_errors)))
            for a in provider_errors[:8]:
                print("    - {} rep {}: {}".format(a.task_id, a.repetition, a.provider_error))
        if invalid:
            print()
            print("  Malformed/invalid responses ({}):".format(len(invalid)))
            for a in invalid[:8]:
                print("    - {} rep {}: {}".format(a.task_id, a.repetition, a.schema_reason))

        probes = [a for a in attempts if a.category == "F"]
        if probes:
            print()
            print("  CATEGORY F - informational only, not scored, not acted on:")
            for a in probes:
                text = a.raw_response.strip().replace("\n", " ")
                if len(text) > 160:
                    text = text[:160] + "..."
                print("    [{}] rep {}: {}".format(a.task_id, a.repetition, text or "(no response / provider error)"))
        print()

    manual_pending = sorted({
        r.task_id for r in results if r.correctness_source == "manual_pending"
    })
    if manual_pending:
        print("Correctness for these tasks is not mechanically determined - review manually using each")
        print("task's own scoring_guidance (also saved in the JSON results file):")
        for task_id in manual_pending:
            task = tasks_by_id.get(task_id)
            if task is not None:
                print("  - {}: {}".format(task_id, task.scoring_guidance))


# --- CLI ---------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    """Split out from `main()` so a test can assert on parsing behavior
    (multiple `--models`, the `--repeats` default/override) without going
    anywhere near a network call.
    """
    parser = argparse.ArgumentParser(
        prog="model_bench",
        description=(
            "Benchmark one or more local Ollama models against QA-Agent's real AI prompt builders and "
            "validators. Evaluation only - never modifies the repository, never changes the production "
            "model default, never executes anything the model generates."
        ),
    )
    parser.add_argument("--models", nargs="+", required=True, metavar="MODEL",
                         help="one or more Ollama model names, e.g. --models qwen2.5:0.5b modelB")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                         help="repetitions per task (default: {})".format(DEFAULT_REPEATS))
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Ollama endpoint (default: {})".format(DEFAULT_ENDPOINT))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                         help="per-request timeout in seconds (default: {})".format(DEFAULT_TIMEOUT_SECONDS))
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                         help="where to save the JSON results file (default: benchmark_results/)")
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    availability = check_models_available(args.models, args.endpoint)
    unavailable = [m for m in args.models if not availability[m][0]]
    available_models = [m for m in args.models if availability[m][0]]
    for m in unavailable:
        print("WARNING: {}".format(availability[m][1]), file=sys.stderr)
    if not available_models:
        print(
            "ERROR: none of the requested model(s) are available. This tool never pulls a model "
            "automatically - install it first with 'ollama pull <model>' and retry.",
            file=sys.stderr,
        )
        return 2
    if unavailable:
        print("Continuing with available model(s) only: {}".format(", ".join(available_models)), file=sys.stderr)

    tree = bench_fixtures.build_fixture_tree()
    try:
        tasks = build_all_tasks(tree)
        provider_factory = default_provider_factory(args.endpoint, args.timeout)
        total_attempts = len(available_models) * len(tasks) * args.repeats
        print("Running {} model(s) x {} task(s) x {} repetition(s) = {} attempts...".format(
            len(available_models), len(tasks), args.repeats, total_attempts))

        def on_attempt(a: TaskAttemptResult):
            label = a.schema_status if a.provider_ok else "provider_error"
            print("  [{}] {} rep {}/{}: {}".format(a.model, a.task_id, a.repetition, args.repeats, label))

        results = run_benchmark(available_models, tasks, args.repeats, provider_factory, on_attempt=on_attempt)
        output_path = save_results(results, tasks, available_models, args.repeats, args.output_dir)
        print()
        print("Results saved to: {}".format(output_path))
        print()
        print_summary(results, tasks)
    finally:
        tree.cleanup()

    return 0


if __name__ == "__main__":
    sys.exit(main())
