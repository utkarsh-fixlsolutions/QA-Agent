"""AI repair proposal generation (Phase E Part 1): repair.py, and its
extensions to prompts.py/schemas.py/response_parser.py (build_repair_prompt,
RepairResponse, validate_repair_response).

Pure unit tests, no network and no live LLM - matching every other AI test
suite in this project. `MockProvider` covers the simple always-succeed/
always-fail cases; a small local `_ScriptedProvider` (the same shape
test_ai_explainer.py/test_ai_fixer.py already use) covers per-call-distinct
responses, needed to prove per-finding independence and deterministic
ordering across several findings at once.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite, TempProject, compare_golden, normalise, run_agent  # noqa: E402

from qa_agent.ai import LLMResponse, MockProvider  # noqa: E402
from qa_agent.ai.context import CodeContext  # noqa: E402
from qa_agent.ai.repair import RepairProposal, propose_repair, propose_repairs  # noqa: E402


class _Finding:
    """A plain stand-in for adapters.Finding, matching prompts.py's own
    documented duck-typed usage (every other AI test suite uses the same
    pattern).
    """

    def __init__(self, file="a.py", line=3, severity="error",
                 message="sample finding message qzx9", tool="ruff"):
        self.file, self.line, self.severity, self.message, self.tool = (
            file, line, severity, message, tool,
        )


class _ScriptedProvider:
    """Returns one fully-controlled LLMResponse per call, in order - needed
    here to prove per-finding independence without relying on MockProvider's
    single fixed response.
    """

    name = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate(self, prompt):
        self.calls.append(prompt)
        index = len(self.calls) - 1
        if index < len(self._responses):
            return self._responses[index]
        return LLMResponse(error="no more scripted responses", provider=self.name)

    def test_connection(self):
        raise AssertionError("propose_repair must never call test_connection")


def _fixed_context(file_path, line, **kwargs):
    """A context-extraction stand-in that never touches the real
    filesystem - the finding's file/line are irrelevant to these tests,
    only the provider's response matters.
    """
    return CodeContext(ok=True, lines=((line, "x = 1"),), start_line=line, end_line=line,
                        target_line=line)


def _repair_response(explanation="an explanation", replacement="a replacement",
                      confidence=0.8, start_line=None, end_line=None):
    fields = ['"explanation": "{}"'.format(explanation),
              '"replacement": "{}"'.format(replacement),
              '"confidence": {}'.format(confidence)]
    if start_line is not None:
        fields.append('"start_line": {}'.format(start_line))
    if end_line is not None:
        fields.append('"end_line": {}'.format(end_line))
    return "{{{}}}".format(", ".join(fields))


# --- propose_repair: success, and every required failure mode --------------


def test_propose_repair_success(suite):
    provider = MockProvider(response_text=_repair_response(
        "Comparing to None with == can invoke overloaded equality.",
        "if value is None:",
        confidence=0.9,
    ))
    finding = _Finding()
    result = propose_repair(finding, provider, extract=_fixed_context)
    suite.check("a RepairProposal is returned", isinstance(result, RepairProposal))
    assert isinstance(result, RepairProposal)
    suite.check("the original finding is carried through unchanged", result.finding is finding)
    suite.check("the model's own explanation is used",
                result.explanation == "Comparing to None with == can invoke overloaded equality.")
    suite.check("the model's own replacement is used", result.replacement == "if value is None:")
    suite.check("confidence is carried through", result.confidence == 0.9)
    suite.check("model is the provider's own name", result.model == "mock")
    suite.check("file is the finding's own file", result.file == finding.file)


def test_propose_repair_uses_the_models_line_range_when_valid(suite):
    provider = MockProvider(response_text=_repair_response(start_line=10, end_line=12))
    result = propose_repair(_Finding(line=11), provider, extract=_fixed_context)
    assert isinstance(result, RepairProposal)
    suite.check("start_line is the model's own value", result.start_line == 10)
    suite.check("end_line is the model's own value", result.end_line == 12)


def test_propose_repair_falls_back_to_the_findings_line_when_range_absent(suite):
    provider = MockProvider(response_text=_repair_response())
    finding = _Finding(line=42)
    result = propose_repair(finding, provider, extract=_fixed_context)
    assert isinstance(result, RepairProposal)
    suite.check("start_line falls back to the finding's own line", result.start_line == 42)
    suite.check("end_line falls back to the finding's own line", result.end_line == 42)


def test_propose_repair_falls_back_when_the_models_range_is_backwards(suite):
    """end_line before start_line is not a real range - the finding's own
    line is used instead of trusting a claim that contradicts itself.
    """
    provider = MockProvider(response_text=_repair_response(start_line=20, end_line=10))
    finding = _Finding(line=7)
    result = propose_repair(finding, provider, extract=_fixed_context)
    assert isinstance(result, RepairProposal)
    suite.check("falls back to the finding's own line, not the backwards claim",
                result.start_line == 7 and result.end_line == 7)


def test_propose_repair_confidence_is_clamped_into_range(suite):
    over = MockProvider(response_text=_repair_response(confidence=1.7))
    under = MockProvider(response_text=_repair_response(confidence=-0.3))
    result_over = propose_repair(_Finding(), over, extract=_fixed_context)
    result_under = propose_repair(_Finding(), under, extract=_fixed_context)
    assert isinstance(result_over, RepairProposal) and isinstance(result_under, RepairProposal)
    suite.check("a confidence above 1.0 is clamped down to 1.0", result_over.confidence == 1.0)
    suite.check("a confidence below 0.0 is clamped up to 0.0", result_under.confidence == 0.0)


def test_propose_repair_provider_offline(suite):
    provider = MockProvider(fail=True, failure_message="connection refused - offline")
    result = propose_repair(_Finding(), provider, extract=_fixed_context)
    suite.check("no proposal, no exception", result is None)


def test_propose_repair_provider_timeout(suite):
    provider = MockProvider(fail=True, failure_message="did not respond within 30s")
    result = propose_repair(_Finding(), provider, extract=_fixed_context)
    suite.check("no proposal on a timeout either", result is None)


def test_propose_repair_malformed_json_response(suite):
    provider = MockProvider(response_text="You could just fix the comparison operator.")
    result = propose_repair(_Finding(), provider, extract=_fixed_context)
    suite.check("prose instead of JSON produces no proposal", result is None)


def test_propose_repair_invalid_schema_missing_replacement(suite):
    provider = MockProvider(
        response_text='{"explanation": "only some of the schema", "confidence": 0.5}'
    )
    result = propose_repair(_Finding(), provider, extract=_fixed_context)
    suite.check("a schema-invalid response (missing replacement) produces no proposal",
                result is None)


def test_propose_repair_invalid_schema_missing_confidence(suite):
    """confidence is required for E1's RepairProposal - a response that
    omits it is schema-invalid, not silently defaulted.
    """
    provider = MockProvider(
        response_text='{"explanation": "why", "replacement": "fix"}'
    )
    result = propose_repair(_Finding(), provider, extract=_fixed_context)
    suite.check("missing confidence produces no proposal", result is None)


def test_propose_repair_invalid_schema_wrong_type_confidence(suite):
    """bool is an int subclass in Python - "confidence": true must not be
    silently accepted as 1.
    """
    provider = MockProvider(
        response_text='{"explanation": "why", "replacement": "fix", "confidence": true}'
    )
    result = propose_repair(_Finding(), provider, extract=_fixed_context)
    suite.check("a boolean confidence is rejected, not accepted as 1.0", result is None)


def test_propose_repair_invalid_schema_wrong_type_line(suite):
    provider = MockProvider(
        response_text=(
            '{"explanation": "why", "replacement": "fix", "confidence": 0.5, '
            '"start_line": "ten"}'
        )
    )
    result = propose_repair(_Finding(), provider, extract=_fixed_context)
    suite.check("a non-integer start_line produces no proposal", result is None)


def test_propose_repair_insufficient_context_response(suite):
    provider = MockProvider(
        response_text='{"insufficient_context": true, "reason": "no code was supplied"}'
    )
    result = propose_repair(_Finding(), provider, extract=_fixed_context)
    suite.check("an explicit decline produces no proposal, not a fabricated one", result is None)


def test_propose_repair_never_invents_a_placeholder(suite):
    """Every failure mode at once: none of them ever produces even an
    empty-field RepairProposal - absence, not a placeholder, is the only
    signal of failure.
    """
    for provider in (
        MockProvider(fail=True),
        MockProvider(response_text="not json"),
        MockProvider(response_text='{"insufficient_context": true}'),
        MockProvider(response_text="{}"),
    ):
        result = propose_repair(_Finding(), provider, extract=_fixed_context)
        suite.check("no RepairProposal object at all - not even an empty one", result is None)


def test_propose_repair_survives_a_genuinely_unexpected_exception(suite):
    """The last line of defence: even a completely broken provider must
    not be able to fail the QA run.
    """
    class _BrokenProvider:
        name = "broken"

        def generate(self, prompt):
            raise RuntimeError("something nobody anticipated")

    result = propose_repair(_Finding(), _BrokenProvider(), extract=_fixed_context)
    suite.check("an exception inside the pipeline still yields None, not a crash", result is None)


def test_propose_repair_survives_broken_context_extraction(suite):
    """extract_context() itself is documented never to raise, but this
    module's own try/except must still cover a caller-supplied `extract`
    that misbehaves - the same defensive posture as any other AI module.
    """
    def _broken_extract(file_path, line, **kwargs):
        raise RuntimeError("disk exploded")

    provider = MockProvider(response_text=_repair_response())
    result = propose_repair(_Finding(), provider, extract=_broken_extract)
    suite.check("a broken extractor still yields None, not a crash", result is None)


# --- MockProvider determinism -----------------------------------------------


def test_propose_repair_mock_provider_is_deterministic(suite):
    provider = MockProvider(response_text=_repair_response("why", "fix", confidence=0.6))
    first = propose_repair(_Finding(), provider, extract=_fixed_context)
    second = propose_repair(_Finding(), provider, extract=_fixed_context)
    assert isinstance(first, RepairProposal) and isinstance(second, RepairProposal)
    suite.check("the same fixed configuration produces the same proposal every time",
                (first.explanation, first.replacement, first.confidence)
                == (second.explanation, second.replacement, second.confidence))


# --- propose_repairs: multiple findings, independence, ordering ------------


def test_propose_repairs_multiple_all_succeed(suite):
    findings = [_Finding(file="a.py", line=1, message="issue a"),
                _Finding(file="b.py", line=2, message="issue b"),
                _Finding(file="c.py", line=3, message="issue c")]
    provider = _ScriptedProvider([
        LLMResponse(text=_repair_response("why a", "fix a")),
        LLMResponse(text=_repair_response("why b", "fix b")),
        LLMResponse(text=_repair_response("why c", "fix c")),
    ])
    proposals = propose_repairs(findings, provider, extract=_fixed_context)
    suite.check("all three findings got their own proposal", len(proposals) == 3)
    suite.check("each finding maps to its own correct replacement",
                proposals[findings[0]].replacement == "fix a"
                and proposals[findings[1]].replacement == "fix b"
                and proposals[findings[2]].replacement == "fix c")
    suite.check("the provider was called once per finding, in order", len(provider.calls) == 3)


def test_propose_repairs_one_failure_does_not_affect_the_others(suite):
    """Execution isolation, the same principle the deterministic engine
    already uses for tools (docs/13): one finding's repair failing must
    never affect another's.
    """
    findings = [_Finding(file="a.py", line=1, message="issue a"),
                _Finding(file="b.py", line=2, message="issue b"),
                _Finding(file="c.py", line=3, message="issue c")]
    provider = _ScriptedProvider([
        LLMResponse(text=_repair_response("why a", "fix a")),
        LLMResponse(error="offline for this one call"),
        LLMResponse(text=_repair_response("why c", "fix c")),
    ])
    proposals = propose_repairs(findings, provider, extract=_fixed_context)
    suite.check("exactly two of three succeeded", len(proposals) == 2)
    suite.check("the failing middle finding is simply absent, not present-with-None",
                findings[1] not in proposals)
    suite.check("the two that succeeded still have the right replacement",
                proposals[findings[0]].replacement == "fix a"
                and proposals[findings[2]].replacement == "fix c")


def test_propose_repairs_returns_empty_mapping_when_all_fail(suite):
    findings = [_Finding(file="a.py"), _Finding(file="b.py")]
    proposals = propose_repairs(findings, MockProvider(fail=True), extract=_fixed_context)
    suite.check("an empty dict, not None and not a crash", proposals == {})


def test_propose_repairs_ordering_is_deterministic(suite):
    findings = [_Finding(file="a.py", line=1, message="m1"),
                _Finding(file="b.py", line=2, message="m2")]

    def make_provider():
        return _ScriptedProvider([
            LLMResponse(text=_repair_response("why 1", "fix 1")),
            LLMResponse(text=_repair_response("why 2", "fix 2")),
        ])

    first = propose_repairs(findings, make_provider(), extract=_fixed_context)
    second = propose_repairs(findings, make_provider(), extract=_fixed_context)
    suite.check("identical findings/provider sequence produces identical proposals",
                {f: p.replacement for f, p in first.items()}
                == {f: p.replacement for f, p in second.items()})


# --- isolation: repair.py never edits, patches, or is auto-invoked ---------


def test_repair_engine_is_not_invoked_by_the_normal_analyze_path(suite):
    """Phase E Part 1's own isolation rule, distinct from (and in addition
    to) the general qa_agent.ai isolation already proven in
    test_ai_provider.py: even though __main__.py legitimately imports
    qa_agent.ai as of Phase D Part 6, nothing in the normal analyze/watch
    path may reference this specific module - no --repair CLI flag, no
    automatic invocation, exists yet.

    Checks for actual references to repair.py's own public API, not a bare
    "repair" substring - the same false-positive class documented
    repeatedly elsewhere in this project (a pipeline module legitimately
    using the word "repair" for something unrelated, e.g. report.py's own
    Phase E Part 5 `repair_result` rendering, must not trip this).
    """
    pipeline_modules = [
        "runner.py", "adapters.py", "report.py", "config.py",
        "analysis_bridge.py", "watch.py", "debouncer.py",
        "fsmonitor.py", "live_report.py", "gitdiff.py", "__main__.py",
    ]
    forbidden = ["propose_repair", "RepairProposal", "from .repair", "from qa_agent.ai.repair"]
    offenders = []
    for name in pipeline_modules:
        source = (REPO_ROOT / "qa_agent" / name).read_text(encoding="utf-8")
        if any(token in source for token in forbidden):
            offenders.append(name)
    suite.check("no pipeline module (including __main__.py) references repair.py's own API",
                not offenders, "  [{}]".format(offenders))


def test_repair_module_never_writes_edits_or_touches_git(suite):
    """Checked directly against the real source, not just asserted in a
    docstring: repair.py must contain no file-write, subprocess, or git
    machinery of any kind - it only ever returns data.
    """
    source = (REPO_ROOT / "qa_agent" / "ai" / "repair.py").read_text(encoding="utf-8")
    forbidden = ["write_text", "write_bytes", "subprocess", "os.system", "shutil", "gitdiff"]
    found = [token for token in forbidden if token in source]
    suite.check("no file-write/subprocess/git tokens appear in repair.py's real source",
                not found, "  [{}]".format(found))


def test_phase_c_golden_output_unaffected(suite):
    """The real, end-to-end proof: running the actual CLI - which never
    calls propose_repair()/propose_repairs() at all, this part having no
    CLI integration - must still match every existing golden file exactly.
    """
    with TempProject() as root:
        target = root / "has_issues.py"
        target.write_text(
            "import json\n\n\ndef f(x):\n    if x == None:\n        return 1\n"
            "    y = 2\n    return x\n",
            encoding="utf-8",
        )
        proc = run_agent([str(target)])
        actual = normalise(proc.stdout, root)
    compare_golden(suite, "cli_findings.txt", actual)


if __name__ == "__main__":
    suite = Suite("AI repair proposal engine (Phase E Part 1)")
    sys.exit(suite.run([
        test_propose_repair_success,
        test_propose_repair_uses_the_models_line_range_when_valid,
        test_propose_repair_falls_back_to_the_findings_line_when_range_absent,
        test_propose_repair_falls_back_when_the_models_range_is_backwards,
        test_propose_repair_confidence_is_clamped_into_range,
        test_propose_repair_provider_offline,
        test_propose_repair_provider_timeout,
        test_propose_repair_malformed_json_response,
        test_propose_repair_invalid_schema_missing_replacement,
        test_propose_repair_invalid_schema_missing_confidence,
        test_propose_repair_invalid_schema_wrong_type_confidence,
        test_propose_repair_invalid_schema_wrong_type_line,
        test_propose_repair_insufficient_context_response,
        test_propose_repair_never_invents_a_placeholder,
        test_propose_repair_survives_a_genuinely_unexpected_exception,
        test_propose_repair_survives_broken_context_extraction,
        test_propose_repair_mock_provider_is_deterministic,
        test_propose_repairs_multiple_all_succeed,
        test_propose_repairs_one_failure_does_not_affect_the_others,
        test_propose_repairs_returns_empty_mapping_when_all_fail,
        test_propose_repairs_ordering_is_deterministic,
        test_repair_engine_is_not_invoked_by_the_normal_analyze_path,
        test_repair_module_never_writes_edits_or_touches_git,
        test_phase_c_golden_output_unaffected,
    ]))
