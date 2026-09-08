"""AI-generated finding explanations (Phase D Part 3): explainer.py, and
report.py's optional explanation rendering.

Pure unit tests, no network and no live LLM - matching test_ai_provider.py
and test_ai_prompts.py's own style. `MockProvider` covers the simple
always-succeed/always-fail cases; a small local `_ScriptedProvider` covers
per-call-distinct responses, needed to prove per-finding independence and
deterministic ordering across several findings at once.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, compare_golden, normalise, run_agent  # noqa: E402

from qa_agent.ai import LLMResponse, MockProvider  # noqa: E402
from qa_agent.ai.context import CodeContext  # noqa: E402
from qa_agent.ai.explainer import Explanation, explain_finding, explain_findings  # noqa: E402
from qa_agent.report import render, render_findings, render_markdown  # noqa: E402


class _Finding:
    """A plain stand-in for adapters.Finding, matching prompts.py's own
    documented duck-typed usage (test_ai_prompts.py uses the same pattern).
    """

    def __init__(self, file="a.py", line=3, severity="error",
                 message="sample finding message qzx9", tool="ruff"):
        self.file, self.line, self.severity, self.message, self.tool = (
            file, line, severity, message, tool,
        )


class _Result:
    def __init__(self, checked=(), tools_used=(), findings=()):
        self.checked, self.tools_used, self.findings = checked, tools_used, findings
        self.filtered = 0
        self.tool_errors = []
        self.skipped = []
        self.missing = []


class _ScriptedProvider:
    """Returns one fully-controlled LLMResponse per call, in order - lets a
    test script exactly what happens on the 1st, 2nd, 3rd... call, proving
    per-finding independence without relying on MockProvider's single fixed
    response.
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
        raise AssertionError("explain_finding must never call test_connection")


def _fixed_context(file_path, line, **kwargs):
    """A context-extraction stand-in that never touches the real
    filesystem - the finding's file/line are irrelevant to these tests,
    only the provider's response matters.
    """
    return CodeContext(ok=True, lines=((line, "x = 1"),), start_line=line, end_line=line,
                        target_line=line)


# --- explain_finding: success, and every required failure mode -------------


def test_explain_finding_success(suite):
    provider = MockProvider(response_text='{"explanation": "This is unused, so it can be removed."}')
    result = explain_finding(_Finding(), provider, extract=_fixed_context)
    suite.check("an Explanation is returned", isinstance(result, Explanation))
    assert isinstance(result, Explanation)
    suite.check("the model's real text is used",
                result.text == "This is unused, so it can be removed.")


def test_explain_finding_provider_offline(suite):
    """The exact scenario this part requires: an offline provider must
    never raise, and must simply produce no explanation.
    """
    provider = MockProvider(fail=True, failure_message="connection refused - offline")
    result = explain_finding(_Finding(), provider, extract=_fixed_context)
    suite.check("no explanation, no exception", result is None)


def test_explain_finding_provider_timeout(suite):
    provider = MockProvider(fail=True, failure_message="did not respond within 30s")
    result = explain_finding(_Finding(), provider, extract=_fixed_context)
    suite.check("no explanation on a timeout either", result is None)


def test_explain_finding_malformed_json_response(suite):
    provider = MockProvider(response_text="I think this is because the variable is unused.")
    result = explain_finding(_Finding(), provider, extract=_fixed_context)
    suite.check("prose instead of JSON produces no explanation", result is None)


def test_explain_finding_insufficient_context_response(suite):
    provider = MockProvider(
        response_text='{"insufficient_context": true, "reason": "not enough code shown"}'
    )
    result = explain_finding(_Finding(), provider, extract=_fixed_context)
    suite.check("an explicit decline produces no explanation, not a fabricated one",
                result is None)


def test_explain_finding_invalid_schema_response(suite):
    """Valid JSON, wrong shape - missing the required 'explanation' field."""
    provider = MockProvider(response_text='{"comment": "looks fine to me"}')
    result = explain_finding(_Finding(), provider, extract=_fixed_context)
    suite.check("a schema-invalid response produces no explanation", result is None)


def test_explain_finding_never_invents_a_placeholder(suite):
    """Every failure mode at once, checked together: none of them ever
    produces so much as an empty-string Explanation - absence, not a
    placeholder, is the only signal of failure.
    """
    for provider in (
        MockProvider(fail=True),
        MockProvider(response_text="not json"),
        MockProvider(response_text='{"insufficient_context": true}'),
        MockProvider(response_text="{}"),
    ):
        result = explain_finding(_Finding(), provider, extract=_fixed_context)
        suite.check("no Explanation object at all - not even an empty one", result is None)


def test_explain_finding_survives_a_genuinely_unexpected_exception(suite):
    """The last line of defence: even a completely broken provider must
    not be able to fail the QA run.
    """
    class _BrokenProvider:
        name = "broken"

        def generate(self, prompt):
            raise RuntimeError("something nobody anticipated")

    result = explain_finding(_Finding(), _BrokenProvider(), extract=_fixed_context)
    suite.check("an exception inside the pipeline still yields None, not a crash", result is None)


# --- explain_findings: multiple findings, independence, ordering -----------


def test_explain_findings_multiple_all_succeed(suite):
    findings = [_Finding(file="a.py", line=1, message="issue a"),
                _Finding(file="b.py", line=2, message="issue b"),
                _Finding(file="c.py", line=3, message="issue c")]
    provider = _ScriptedProvider([
        LLMResponse(text='{"explanation": "explains a"}'),
        LLMResponse(text='{"explanation": "explains b"}'),
        LLMResponse(text='{"explanation": "explains c"}'),
    ])
    explanations = explain_findings(findings, provider, extract=_fixed_context)
    suite.check("all three findings got their own explanation", len(explanations) == 3)
    suite.check("each finding maps to its own correct text",
                explanations[findings[0]].text == "explains a"
                and explanations[findings[1]].text == "explains b"
                and explanations[findings[2]].text == "explains c")
    suite.check("the provider was called once per finding, in order", len(provider.calls) == 3)


def test_explain_findings_one_failure_does_not_affect_the_others(suite):
    """Execution isolation, the same principle the deterministic engine
    already uses for tools (docs/13): one finding's explanation failing
    must never affect another's.
    """
    findings = [_Finding(file="a.py", line=1, message="issue a"),
                _Finding(file="b.py", line=2, message="issue b"),
                _Finding(file="c.py", line=3, message="issue c")]
    provider = _ScriptedProvider([
        LLMResponse(text='{"explanation": "explains a"}'),
        LLMResponse(error="offline for this one call"),
        LLMResponse(text='{"explanation": "explains c"}'),
    ])
    explanations = explain_findings(findings, provider, extract=_fixed_context)
    suite.check("exactly two of three succeeded", len(explanations) == 2)
    suite.check("the failing middle finding is simply absent, not present-with-None",
                findings[1] not in explanations)
    suite.check("the two that succeeded still have the right text",
                explanations[findings[0]].text == "explains a"
                and explanations[findings[2]].text == "explains c")


def test_explain_findings_returns_empty_mapping_when_all_fail(suite):
    findings = [_Finding(file="a.py"), _Finding(file="b.py")]
    provider = MockProvider(fail=True)
    explanations = explain_findings(findings, provider, extract=_fixed_context)
    suite.check("an empty dict, not None and not a crash", explanations == {})


def test_explain_findings_ordering_is_deterministic(suite):
    """Explaining the same findings twice, in the same order, must always
    produce the same result - no hidden randomness or ordering dependence.
    """
    findings = [_Finding(file="a.py", line=1, message="m1"),
                _Finding(file="b.py", line=2, message="m2")]

    def make_provider():
        return _ScriptedProvider([
            LLMResponse(text='{"explanation": "e1"}'),
            LLMResponse(text='{"explanation": "e2"}'),
        ])

    first = explain_findings(findings, make_provider(), extract=_fixed_context)
    second = explain_findings(findings, make_provider(), extract=_fixed_context)
    suite.check("identical findings/provider sequence produces identical explanations",
                {f: e.text for f, e in first.items()} == {f: e.text for f, e in second.items()})


# --- report.py: explanation rendering ---------------------------------------


def test_render_shows_explanation_beneath_its_finding(suite):
    finding = _Finding(file="x.py", line=5, severity="error", message="a real issue", tool="ruff")
    result = _Result(checked=["x.py"], tools_used=["ruff"], findings=[finding])
    explanations = {finding: Explanation(text="This is why it matters.")}
    output = render(result, "x.py", explanations=explanations)
    suite.check("the finding line is present, unchanged",
                "x.py:5  [error] a real issue  (ruff)" in output)
    suite.check("the explanation is clearly labeled", "[AI Explanation]" in output)
    suite.check("the explanation text appears", "This is why it matters." in output)
    finding_pos = output.find("x.py:5")
    explanation_pos = output.find("[AI Explanation]")
    suite.check("the explanation appears beneath (after) its finding, not before",
                0 <= finding_pos < explanation_pos)


def test_render_never_mixes_explanation_into_the_tool_message(suite):
    finding = _Finding(message="the tool's own message")
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    explanations = {finding: Explanation(text="the AI's own words")}
    output = render(result, "a.py", explanations=explanations)
    finding_line = next(line for line in output.splitlines() if "the tool's own message" in line)
    suite.check("the AI text never appears on the finding's own line",
                "the AI's own words" not in finding_line)


def test_render_omits_explanation_line_when_none_available(suite):
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    output = render(result, "a.py", explanations={})
    suite.check("no [AI Explanation] line when the mapping has nothing for this finding",
                "[AI Explanation]" not in output)


def test_render_multiline_explanation_collapses_to_one_line(suite):
    """Deterministic formatting: whatever the model returned, the rendered
    explanation is always exactly one line.
    """
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    explanations = {finding: Explanation(text="line one\nline two\n\nline three")}
    output = render(result, "a.py", explanations=explanations)
    ai_lines = [line for line in output.splitlines() if "[AI Explanation]" in line]
    suite.check("exactly one rendered line for the explanation", len(ai_lines) == 1)
    suite.check("all three fragments still present, just joined",
                "line one line two line three" in ai_lines[0])


def test_render_findings_also_supports_explanations(suite):
    """Watch mode's own renderer (report.render_findings) must support the
    same optional explanations parameter, not just the one-shot render().
    """
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    explanations = {finding: Explanation(text="watch-mode explanation")}
    output = render_findings(result, explanations=explanations)
    suite.check("explanation renders in the watch-mode findings view too",
                "watch-mode explanation" in output)


def test_render_markdown_adds_a_separate_explanations_section(suite):
    finding = _Finding(file="x.py", line=5, tool="ruff", message="a real issue")
    result = _Result(checked=["x.py"], tools_used=["ruff"], findings=[finding])
    explanations = {finding: Explanation(text="markdown explanation text")}
    output = render_markdown(result, "x.py", explanations=explanations)
    suite.check("a distinct AI Explanations section exists", "## AI Explanations" in output)
    suite.check("the explanation text appears in it", "markdown explanation text" in output)
    suite.check("the findings table itself is completely unchanged",
                "| `x.py` | 5 | error | a real issue | ruff |" in output)


def test_render_markdown_omits_the_section_when_no_explanations(suite):
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    output = render_markdown(result, "a.py", explanations=None)
    suite.check("no AI Explanations section at all", "AI Explanations" not in output)


def test_render_byte_identical_with_ai_disabled(suite):
    """Requirement 10, checked directly: omitting explanations (or passing
    None) must produce output byte-for-byte identical to never having
    passed the parameter at all.
    """
    finding = _Finding(file="x.py", line=5, severity="error", message="a real issue", tool="ruff")
    result = _Result(checked=["x.py"], tools_used=["ruff"], findings=[finding])
    without_param = render(result, "x.py")
    with_none = render(result, "x.py", explanations=None)
    with_empty = render(result, "x.py", explanations={})
    suite.check("no explanations param == explanations=None, byte for byte",
                without_param == with_none)
    suite.check("explanations=None == explanations={}, byte for byte",
                with_none == with_empty)
    suite.check("no [AI Explanation] text anywhere", "[AI Explanation]" not in without_param)


# --- Phase C regression: golden files stay byte-for-byte identical ---------


def test_phase_c_golden_output_unaffected(suite):
    """The real, end-to-end proof: running the actual CLI - which never
    calls explain_findings() at all in this part - must still match every
    existing golden file exactly.
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
    suite = Suite("AI finding explanations (Phase D Part 3)")
    sys.exit(suite.run([
        test_explain_finding_success,
        test_explain_finding_provider_offline,
        test_explain_finding_provider_timeout,
        test_explain_finding_malformed_json_response,
        test_explain_finding_insufficient_context_response,
        test_explain_finding_invalid_schema_response,
        test_explain_finding_never_invents_a_placeholder,
        test_explain_finding_survives_a_genuinely_unexpected_exception,
        test_explain_findings_multiple_all_succeed,
        test_explain_findings_one_failure_does_not_affect_the_others,
        test_explain_findings_returns_empty_mapping_when_all_fail,
        test_explain_findings_ordering_is_deterministic,
        test_render_shows_explanation_beneath_its_finding,
        test_render_never_mixes_explanation_into_the_tool_message,
        test_render_omits_explanation_line_when_none_available,
        test_render_multiline_explanation_collapses_to_one_line,
        test_render_findings_also_supports_explanations,
        test_render_markdown_adds_a_separate_explanations_section,
        test_render_markdown_omits_the_section_when_no_explanations,
        test_render_byte_identical_with_ai_disabled,
        test_phase_c_golden_output_unaffected,
    ]))
