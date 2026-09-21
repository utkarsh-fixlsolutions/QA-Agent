"""The ~15 benchmark tasks (tools/model_bench.py), grouped into categories
A-F exactly as specified for this benchmark. Every task builds its prompt
with a real `qa_agent.ai` prompt builder and validates a response with a
real `qa_agent.ai` validator - no duplicate parsing/validation logic exists
anywhere in this file. Category F ("informational only" probes) is the one
deliberate exception: no next-action schema exists in `qa_agent.ai` (the AI
Behavior Contract work explicitly declined to create one before G5 is
designed), so those two tasks send a plain, clearly-labeled exploratory
prompt and record only the raw text - never validated, never scored,
never treated as a decision.

Correctness scoring policy, applied uniformly by the benchmark runner, not
repeated per task: a response that fails schema validation gets no
correctness verdict (its failure is already fully captured by the
schema-validity dimension); a schema-valid response whose grounded claims
are false gets a hard 0 (an ungrounded claim is never "correct" regardless
of how the rest of the answer reads); everything else is either resolved by
a task's own `mechanical_score` (defined only where the *correct* answer is
itself mechanically checkable - a decline, a grounded name, a constraint)
or is left `None` - explicitly manual, never faked as automatic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from qa_agent.adapters import Finding
from qa_agent.ai import (
    Prompt,
    build_diagnosis_prompt,
    build_explanation_prompt,
    build_runtime_repair_prompt,
    build_summary_prompt,
    extract_context,
    response_is_grounded,
    validate_diagnosis_response,
    validate_explanation_response,
    validate_repair_response,
    validate_summary_response,
)
from qa_agent.ai.schemas import STATUS_INSUFFICIENT_CONTEXT, STATUS_SUCCESS
from qa_agent.ai.workspace import apply_repair, cleanup_workspace, create_workspace
from qa_agent.project.models import DetectedItem

import bench_fixtures as fx

_FILE_TOKEN_RE = re.compile(r"\b[\w./-]+\.(?:py|js|ts|jsx|tsx|json|java|go|rb)\b")


@dataclass(frozen=True)
class BenchTask:
    """One benchmark task. `validate=None` only for a Category F probe (no
    schema exists to validate against - see module docstring). `ground` and
    `mechanical_score`, when present, are called only after a response has
    already validated as `STATUS_SUCCESS` - see the module docstring for the
    exact scoring policy this leaves to the runner itself.
    """

    id: str
    category: str
    kind: str  # "diagnosis" | "repair" | "explain" | "summary" | "probe"
    description: str
    scoring_guidance: str
    prompt: Prompt
    validate: Optional[Callable[[str], object]] = None
    ground: Optional[Callable[[object], bool]] = None
    mechanical_score: Optional[Callable[[object, Optional[bool], str, bool], Optional[int]]] = None
    constraint_checks: Tuple[Tuple[str, Callable[[str, object], bool]], ...] = ()


def _line_range(value, fallback_line):
    """The same small, deliberately duplicated fallback `repair.py`'s own
    `_line_range`/`runtime_repair.py`'s own `_line_range` already use - a
    sane model-given range is trusted, an insane or missing one falls back
    to the real target line, never invented.
    """
    start, end = getattr(value, "start_line", None), getattr(value, "end_line", None)
    if isinstance(start, int) and isinstance(end, int) and 1 <= start <= end:
        return start, end
    return fallback_line, fallback_line


def _repair_applies_cleanly(value, target_file, fallback_line) -> bool:
    """This task's "grounding" analog for a repair proposal: does it
    actually apply against the real target file, using the real, unmodified
    Phase E Part 2 `apply_repair()` - never a reimplementation of its own
    line-range/shape checks.
    """
    start, end = _line_range(value, fallback_line)

    class _Proposal:
        file = target_file
        start_line = start
        end_line = end
        replacement = value.replacement

    workspace = create_workspace()
    if workspace is None:
        return False
    try:
        applied = apply_repair(workspace, _Proposal())
        return bool(applied.ok)
    finally:
        cleanup_workspace(workspace)


def _mentions_other_file(text: str, exclude_basename: str) -> bool:
    """A loose, explicitly-heuristic signal (documented as such wherever it
    is used) for whether a response's own text names a file other than the
    one it was actually shown - used only as one input to a task's own
    `mechanical_score`, never as a standalone pass/fail gate.
    """
    for token in _FILE_TOKEN_RE.findall(text):
        if token.split("/")[-1] != exclude_basename:
            return True
    return False


def _strict_json_only(raw: str) -> bool:
    """Stricter than the real parser: `response_parser.parse_json_response`
    deliberately tolerates a markdown fence and surrounding whitespace (a
    real, sanctioned leniency). This checks the thing Category D actually
    wants to know - did the model comply with "JSON only, no markdown, no
    commentary" *literally* - so it is intentionally not reused from
    `response_parser.py`; it is asking a different, stricter question.
    """
    import json
    try:
        json.loads(raw.strip())
        return True
    except (json.JSONDecodeError, ValueError):
        return False


# --- Category A: runtime diagnosis ------------------------------------------

def _task_a1() -> BenchTask:
    cr = fx.check_result(
        reason="build exited 1 (package.json scripts.build (via npm))",
        logs=("Error: Cannot find module 'lodash'", "    at Function.Module._resolveFilename (node:internal)"),
    )
    ctx = fx.repository_context()
    rc = fx.runtime_check()
    prompt = build_diagnosis_prompt(cr, ctx, runtime_check=rc)
    return BenchTask(
        id="A1-build-failure-clear-cause", category="A", kind="diagnosis",
        description="A real build failure whose root cause (a missing module) is directly stated in the logs.",
        scoring_guidance="2 if root_cause correctly identifies the missing 'lodash' module and it appears in "
                          "affected_components/evidence; 1 if the general direction is right but vague; 0 if it "
                          "names an unrelated cause.",
        prompt=prompt, validate=validate_diagnosis_response,
        ground=lambda v: response_is_grounded(v, cr, ctx, runtime_check=rc),
    )


def _task_a2() -> BenchTask:
    cr = fx.check_result(
        check_id="server-startup", name="Server Startup", status=fx.STATUS_ERROR,
        reason="unexpected error: TypeError: Cannot read properties of undefined (reading 'listen')",
        logs=(
            "TypeError: Cannot read properties of undefined (reading 'listen')",
            "    at Object.<anonymous> (server.js:12:5)",
        ),
        exception="TypeError: Cannot read properties of undefined (reading 'listen')",
    )
    ctx = fx.repository_context()
    rc = fx.runtime_check(check_id="server-startup", name="Server Startup", required_evidence=("server.js",))
    prompt = build_diagnosis_prompt(cr, ctx, runtime_check=rc)
    return BenchTask(
        id="A2-server-crash-stack-trace", category="A", kind="diagnosis",
        description="A server crash with a real, specific stack trace naming server.js:12.",
        scoring_guidance="2 if it correctly identifies server.js as affected and the 'listen on undefined' shape "
                          "of the error; 1 if it identifies the file but not the specific issue; 0 otherwise.",
        prompt=prompt, validate=validate_diagnosis_response,
        ground=lambda v: response_is_grounded(v, cr, ctx, runtime_check=rc),
    )


def _task_a3() -> BenchTask:
    cr = fx.check_result(
        check_id="test-suite-verification", name="Test Suite Verification",
        reason="test command exited 1 (pytest)",
        logs=(
            "FAILED tests/test_orders.py::test_total - AssertionError: 105 != 100",
            "FAILED tests/test_billing.py::test_charge - AssertionError: 105 != 100",
            "Both failures assert on calculate_total()'s return value",
        ),
    )
    ctx = fx.repository_context(project=fx.project(
        languages=(DetectedItem(name="Python", evidence=("tests/",)),),
    ))
    rc = fx.runtime_check(check_id="test-suite-verification", name="Test Suite Verification",
                           required_evidence=("pytest dependency",))
    prompt = build_diagnosis_prompt(cr, ctx, runtime_check=rc)
    return BenchTask(
        id="A3-test-failure-multiple-candidate-files", category="A", kind="diagnosis",
        description="Evidence implicates two files roughly equally (both fail the same way, on the same function).",
        scoring_guidance="2 if both test_orders.py and test_billing.py are named, or the ambiguity is stated "
                          "explicitly; 1 if only one is named without acknowledging the other; 0 if a third, "
                          "unsupported file is invented instead.",
        prompt=prompt, validate=validate_diagnosis_response,
        ground=lambda v: response_is_grounded(v, cr, ctx, runtime_check=rc),
    )


# --- Category B: adversarial / grounding ------------------------------------

def _task_b4() -> BenchTask:
    cr = fx.check_result(reason="process exited with code 1", logs=("Process exited unexpectedly.",))
    ctx = fx.repository_context()
    rc = fx.runtime_check()
    prompt = build_diagnosis_prompt(cr, ctx, runtime_check=rc)

    def mech(result, grounded, raw, json_only):
        if result.status == STATUS_INSUFFICIENT_CONTEXT:
            return 2
        if result.status == STATUS_SUCCESS:
            return 1 if not result.value.affected_files else 0
        return None

    return BenchTask(
        id="B4-no-identifiable-file", category="B", kind="diagnosis",
        description="No file, module, or component is named anywhere in the evidence.",
        scoring_guidance="Mechanically scored: insufficient_context=2, DIAGNOSED with no affected_files=1, "
                          "DIAGNOSED naming any affected_files=0 (nothing supports naming one).",
        prompt=prompt, validate=validate_diagnosis_response,
        ground=lambda v: response_is_grounded(v, cr, ctx, runtime_check=rc),
        mechanical_score=mech,
    )


def _task_b5() -> BenchTask:
    cr = fx.check_result(
        reason="build exited 1", logs=("Buildin", "[truncated - log stream cut off]"),
    )
    ctx = fx.repository_context()
    rc = fx.runtime_check()
    prompt = build_diagnosis_prompt(cr, ctx, runtime_check=rc)

    def mech(result, grounded, raw, json_only):
        if result.status == STATUS_INSUFFICIENT_CONTEXT:
            return 2
        if result.status == STATUS_SUCCESS:
            confidence = result.value.confidence
            if confidence <= 0.5:
                return 2
            return 1 if confidence <= 0.8 else 0
        return None

    return BenchTask(
        id="B5-truncated-logs", category="B", kind="diagnosis",
        description="Logs are visibly cut off mid-line; nothing conclusive is present.",
        scoring_guidance="Mechanically scored: insufficient_context or confidence<=0.5 = 2, "
                          "confidence 0.5-0.8 = 1, confidence>0.8 despite thin evidence = 0.",
        prompt=prompt, validate=validate_diagnosis_response,
        ground=lambda v: response_is_grounded(v, cr, ctx, runtime_check=rc),
        mechanical_score=mech,
    )


def _task_b6() -> BenchTask:
    cr = fx.check_result(
        check_id="test-suite-verification", name="Test Suite Verification",
        reason="test command exited 1 (pytest)",
        logs=("FAILED tests/test_payment.py - KeyError: 'amount' raised in payment.py, line 2",),
    )
    ctx = fx.repository_context(project=fx.project(
        languages=(DetectedItem(name="Python", evidence=("payment.py",)),),
        important_files=("payment.py", "billing.py"),  # billing.py is the distractor: present in context, never in evidence
    ))
    rc = fx.runtime_check(check_id="test-suite-verification", name="Test Suite Verification",
                           required_evidence=("pytest dependency",))
    prompt = build_diagnosis_prompt(cr, ctx, runtime_check=rc)

    def mech(result, grounded, raw, json_only):
        if result.status != STATUS_SUCCESS:
            return None
        value = result.value
        named_distractor = any("billing.py" in f for f in value.affected_files) or \
            any("billing.py" in c for c in value.affected_components)
        named_real = any("payment.py" in f for f in value.affected_files)
        if named_distractor or grounded is False:
            return 0
        return 2 if named_real else 1

    return BenchTask(
        id="B6-evidence-vs-distraction", category="B", kind="diagnosis",
        description="Evidence clearly implicates payment.py; repository context also lists billing.py, a "
                     "plausible-sounding file never actually mentioned in the evidence.",
        scoring_guidance="Mechanically scored: names payment.py (never billing.py) = 2, names neither = 1, "
                          "names billing.py = 0.",
        prompt=prompt, validate=validate_diagnosis_response,
        ground=lambda v: response_is_grounded(v, cr, ctx, runtime_check=rc),
        mechanical_score=mech,
    )


# --- Category C: repair ------------------------------------------------------

def _task_c7(tree: fx.FixtureTree) -> BenchTask:
    target = tree.root / "build_simple.js"
    cr = fx.check_result(
        reason="build exited 1 (package.json scripts.build (via npm))",
        logs=("Error: intentional failure",),
    )
    ctx = fx.repository_context()
    from qa_agent.ai import RuntimeDiagnosis, DIAGNOSIS_DIAGNOSED
    diagnosis = RuntimeDiagnosis(
        check_id=cr.id, check_name=cr.name, execution_status=cr.status, diagnosis_status=DIAGNOSIS_DIAGNOSED,
        summary="Build script fails.", severity="error", confidence=0.9,
        observed_evidence=("Error: intentional failure",), likely_root_cause="build.js exits with a failure",
        affected_files=("build_simple.js",), affected_components=(), recommended_action="fix build_simple.js",
    )
    code_context = extract_context(str(target), 1)
    prompt = build_runtime_repair_prompt(cr, diagnosis, ctx, code_context, str(target))
    return BenchTask(
        id="C7-simple-single-line-repair", category="C", kind="repair",
        description="A trivially fixable, single-line-scale bug: the script always exits 1.",
        scoring_guidance="2 if the replacement plausibly makes the script exit 0; 1 if plausible but incomplete; "
                          "0 if it wouldn't work or targets something else.",
        prompt=prompt, validate=validate_repair_response,
        ground=lambda v: _repair_applies_cleanly(v, str(target), 1),
    )


def _task_c8(tree: fx.FixtureTree) -> BenchTask:
    target = tree.root / "build_multiline.js"
    cr = fx.check_result(
        reason="build exited 1 (package.json scripts.build (via npm))",
        logs=("ReferenceError: undefinedVariable is not defined", "    at calculateDiscount (build_multiline.js:4:12)"),
    )
    ctx = fx.repository_context()
    from qa_agent.ai import RuntimeDiagnosis, DIAGNOSIS_DIAGNOSED
    diagnosis = RuntimeDiagnosis(
        check_id=cr.id, check_name=cr.name, execution_status=cr.status, diagnosis_status=DIAGNOSIS_DIAGNOSED,
        summary="calculateDiscount references an undefined variable in one branch.", severity="error", confidence=0.85,
        observed_evidence=("ReferenceError: undefinedVariable is not defined",),
        likely_root_cause="the discount>price branch returns a variable that was never defined",
        affected_files=("build_multiline.js",), affected_components=("undefinedVariable",),
        recommended_action="return a real value from the discount>price branch",
    )
    code_context = extract_context(str(target), 4)
    prompt = build_runtime_repair_prompt(cr, diagnosis, ctx, code_context, str(target))
    return BenchTask(
        id="C8-multiline-repair", category="C", kind="repair",
        description="A logic bug spanning a small multi-line block (an if-branch returning an undefined name).",
        scoring_guidance="2 if the replacement removes the undefined-variable reference and returns something "
                          "sane; 1 if it addresses the symptom incompletely; 0 if it doesn't address it.",
        prompt=prompt, validate=validate_repair_response,
        ground=lambda v: _repair_applies_cleanly(v, str(target), 2),
    )


def _task_c9(tree: fx.FixtureTree) -> BenchTask:
    target = tree.root / "app_thin_evidence.js"
    cr = fx.check_result(
        reason="build exited 1", logs=("Process exited with code 1.",),
    )
    ctx = fx.repository_context()
    from qa_agent.ai import RuntimeDiagnosis, DIAGNOSIS_DIAGNOSED
    diagnosis = RuntimeDiagnosis(
        check_id=cr.id, check_name=cr.name, execution_status=cr.status, diagnosis_status=DIAGNOSIS_DIAGNOSED,
        summary="The process exits with no further detail.", severity="warning", confidence=0.4,
        observed_evidence=("Process exited with code 1.",), likely_root_cause="unclear from the evidence available",
        affected_files=("app_thin_evidence.js",), affected_components=(),
        recommended_action="investigate further before attempting a fix",
    )
    code_context = extract_context(str(target), 1)
    prompt = build_runtime_repair_prompt(cr, diagnosis, ctx, code_context, str(target))

    def mech(result, grounded, raw, json_only):
        if result.status == STATUS_INSUFFICIENT_CONTEXT:
            return 2
        if result.status == STATUS_SUCCESS:
            return 0
        return None

    return BenchTask(
        id="C9-insufficient-evidence-repair", category="C", kind="repair",
        description="The target file has no visible problem, and the evidence gives no specific symptom to fix.",
        scoring_guidance="Mechanically scored: insufficient_context=2, any proposed replacement=0 (fabricated "
                          "given no real signal to act on).",
        prompt=prompt, validate=validate_repair_response, mechanical_score=mech,
    )


# --- Category D: constraint following ---------------------------------------

def _task_d10() -> BenchTask:
    cr = fx.check_result(
        reason="build exited 1 (package.json scripts.build (via npm))",
        logs=("Error: Cannot find module 'axios'",),
    )
    ctx = fx.repository_context()
    rc = fx.runtime_check()
    prompt = build_diagnosis_prompt(cr, ctx, runtime_check=rc)

    def mech(result, grounded, raw, json_only):
        if result.status not in (STATUS_SUCCESS, STATUS_INSUFFICIENT_CONTEXT):
            return None
        return 2 if json_only else 0

    return BenchTask(
        id="D10-strict-json-only", category="D", kind="diagnosis",
        description="Same diagnosis shape as A1 - scored specifically for literal 'JSON only, no commentary' "
                     "compliance, not diagnosis quality.",
        scoring_guidance="Mechanically scored purely on constraint_json_only: strictly bare JSON=2, anything "
                          "wrapped in prose/markdown that still parses via the lenient real parser=0.",
        prompt=prompt, validate=validate_diagnosis_response,
        ground=lambda v: response_is_grounded(v, cr, ctx, runtime_check=rc),
        mechanical_score=mech,
    )


def _task_d11(tree: fx.FixtureTree) -> BenchTask:
    target = tree.root / "build_simple.js"
    cr = fx.check_result(
        reason="build exited 1 (package.json scripts.build (via npm))", logs=("Error: intentional failure",),
    )
    ctx = fx.repository_context()
    from qa_agent.ai import RuntimeDiagnosis, DIAGNOSIS_DIAGNOSED
    diagnosis = RuntimeDiagnosis(
        check_id=cr.id, check_name=cr.name, execution_status=cr.status, diagnosis_status=DIAGNOSIS_DIAGNOSED,
        summary="Build script fails.", severity="error", confidence=0.9,
        observed_evidence=("Error: intentional failure",), likely_root_cause="build_simple.js exits with a failure",
        affected_files=("build_simple.js",), affected_components=(), recommended_action="fix build_simple.js",
    )
    code_context = extract_context(str(target), 1)
    prompt = build_runtime_repair_prompt(cr, diagnosis, ctx, code_context, str(target))

    def mech(result, grounded, raw, json_only):
        if result.status != STATUS_SUCCESS:
            return None
        other_file_explanation = _mentions_other_file(result.value.explanation, "build_simple.js")
        other_file_replacement = _mentions_other_file(result.value.replacement, "build_simple.js")
        if other_file_explanation or other_file_replacement:
            return 0
        return 2 if grounded else 1

    return BenchTask(
        id="D11-single-file-restriction", category="D", kind="repair",
        description="The repair prompt (unmodified, existing text) already restricts the model to the one file "
                     "shown - scored for whether it actually stays inside that restriction.",
        scoring_guidance="Mechanically scored: mentions no other file and applies cleanly=2, stays on-file but "
                          "doesn't apply cleanly=1, mentions a second file=0.",
        prompt=prompt, validate=validate_repair_response,
        ground=lambda v: _repair_applies_cleanly(v, str(target), 1), mechanical_score=mech,
    )


# --- Category E: Phase D AI capabilities -------------------------------------

def _task_e12(tree: fx.FixtureTree) -> BenchTask:
    target = tree.root / "finding_example.py"
    finding = Finding(file=str(target), line=1, severity="warning",
                       message="F401 'os' imported but unused", tool="ruff")
    context = extract_context(str(target), 1)
    prompt = build_explanation_prompt(finding, context)
    return BenchTask(
        id="E12-explain-real-finding", category="E", kind="explain",
        description="A real, specific ruff finding (an unused import) against a real 5-line file.",
        scoring_guidance="2 if the explanation is specific to the unused 'os' import shown; 1 if generic but not "
                          "wrong; 0 if it invents a different/unrelated issue.",
        prompt=prompt, validate=validate_explanation_response,
    )


def _task_e13() -> BenchTask:
    findings = (
        Finding(file="a.py", line=1, severity="warning", message="F401 'os' imported but unused", tool="ruff"),
        Finding(file="a.py", line=5, severity="error", message="F821 undefined name 'foo'", tool="ruff"),
        Finding(file="b.js", line=2, severity="warning", message="no-unused-vars: 'x' is defined but never used", tool="eslint"),
        Finding(file="b.js", line=10, severity="error", message="no-undef: 'bar' is not defined", tool="eslint"),
        Finding(file="c.py", line=3, severity="warning", message="E501 line too long (90 > 88 characters)", tool="ruff"),
    )
    result = fx.run_result(checked=("a.py", "b.js", "c.py"), tools_used=("ruff", "eslint"), findings=findings)
    prompt = build_summary_prompt(result)

    def _mentions_wildly_wrong_count(raw, real_count=len(findings)):
        numbers = [int(n) for n in re.findall(r"\b\d+\b", raw)]
        return any(n > real_count * 3 for n in numbers)

    return BenchTask(
        id="E13-summarize-multi-finding-run", category="E", kind="summary",
        description="A real 5-finding run across 2 tools and 3 files - must not invent extra findings or counts.",
        scoring_guidance="Manual: 2 if the summary accurately reflects the real 5 findings/2 tools without "
                          "inventing extra ones; 1 if vague but not wrong; 0 if it states a count/issue not "
                          "present. The 'plausible_count' constraint flags an obviously inflated number "
                          "automatically as one input to that judgment, not a full substitute for it.",
        prompt=prompt, validate=validate_summary_response,
        constraint_checks=(("plausible_count", lambda raw, result: not _mentions_wildly_wrong_count(raw)),),
    )


# --- Category F: informational-only G5 probes (never scored, never acted on) -

_PROBE_ROLE = (
    "You are helping a QA system think through what to investigate next. This "
    "is an exploratory research question only - no automated system acts on "
    "your answer, and no such system exists yet. Answer in 2-4 concise "
    "sentences of plain text; JSON is not required for this question."
)


def _task_f14() -> BenchTask:
    user = (
        "Current QA evidence:\n"
        "- Build Verification: FAILED (missing module 'lodash')\n"
        "- Server Startup: not yet run\n"
        "- Test Suite Verification: not yet run\n\n"
        "Which deterministic QA operation should run next, and why?"
    )
    return BenchTask(
        id="F14-recommend-next-operation", category="F", kind="probe",
        description="INFORMATIONAL ONLY. No schema, no scoring - read manually. Not part of any decision.",
        scoring_guidance="Not scored. Read the raw response for plausibility only.",
        prompt=Prompt(system=_PROBE_ROLE, user=user),
    )


def _task_f15() -> BenchTask:
    user = (
        "Two independent QA failures were observed:\n"
        "1. Build Verification: FAILED (missing module 'lodash')\n"
        "2. Test Suite Verification: FAILED (2 tests fail on calculate_total())\n\n"
        "Which should be investigated first, and why?"
    )
    return BenchTask(
        id="F15-prioritize-two-failures", category="F", kind="probe",
        description="INFORMATIONAL ONLY. No schema, no scoring - read manually. Not part of any decision.",
        scoring_guidance="Not scored. Read the raw response for plausibility only.",
        prompt=Prompt(system=_PROBE_ROLE, user=user),
    )


def build_all_tasks(tree: fx.FixtureTree):
    """Every task, in a fixed, deterministic order - the same order every
    model/run sees, so results are directly comparable across models.
    """
    return [
        _task_a1(), _task_a2(), _task_a3(),
        _task_b4(), _task_b5(), _task_b6(),
        _task_c7(tree), _task_c8(tree), _task_c9(tree),
        _task_d10(), _task_d11(tree),
        _task_e12(tree), _task_e13(),
        _task_f14(), _task_f15(),
    ]
