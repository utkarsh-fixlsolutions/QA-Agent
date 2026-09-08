"""AI-generated suggested fixes (Phase D Part 5): fixer.py, and report.py's
optional suggested-fix rendering.

Pure unit tests, no network and no live LLM - matching test_ai_explainer.py
and the rest of this project's AI test suites. `MockProvider` covers the
simple always-succeed/always-fail cases; a small local `_ScriptedProvider`
(the same shape test_ai_explainer.py already uses) covers per-call-distinct
responses, needed to prove per-finding independence and deterministic
ordering across several findings at once.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, compare_golden, normalise, run_agent  # noqa: E402

from qa_agent.ai import Explanation, LLMResponse, MockProvider  # noqa: E402
from qa_agent.ai.context import CodeContext  # noqa: E402
from qa_agent.ai.fixer import SuggestedFix, suggest_fix, suggest_fixes  # noqa: E402
from qa_agent.report import render, render_findings, render_markdown  # noqa: E402


class _Finding:
    """A plain stand-in for adapters.Finding, matching prompts.py's own
    documented duck-typed usage (test_ai_explainer.py uses the same
    pattern).
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
    """Returns one fully-controlled LLMResponse per call, in order - the
    same shape test_ai_explainer.py already established, needed here to
    prove per-finding independence without relying on MockProvider's
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
        raise AssertionError("suggest_fix must never call test_connection")


def _fixed_context(file_path, line, **kwargs):
    """A context-extraction stand-in that never touches the real
    filesystem - the finding's file/line are irrelevant to these tests,
    only the provider's response matters.
    """
    return CodeContext(ok=True, lines=((line, "x = 1"),), start_line=line, end_line=line,
                        target_line=line)


def _fix_response(explanation="an explanation", suggested_fix="a suggested fix"):
    return (
        '{{"explanation": "{}", "suggested_fix": "{}"}}'
        .format(explanation, suggested_fix)
    )


# --- suggest_fix: success, and every required failure mode -----------------


def test_suggest_fix_success(suite):
    provider = MockProvider(response_text=_fix_response(
        "Comparing to None with == can invoke overloaded equality.",
        "if value is None:",
    ))
    result = suggest_fix(_Finding(), provider, extract=_fixed_context)
    suite.check("a SuggestedFix is returned", isinstance(result, SuggestedFix))
    assert isinstance(result, SuggestedFix)
    suite.check("the model's own explanation is used",
                result.explanation == "Comparing to None with == can invoke overloaded equality.")
    suite.check("the model's own replacement is used", result.replacement == "if value is None:")


def test_suggest_fix_title_comes_from_the_finding_not_the_model(suite):
    """The real, deliberate design choice this part made: `title` is
    grounded in already-trusted deterministic data (the finding's own
    message), not something the model has to restate - so it can never be
    a source of invented content.
    """
    finding = _Finding(message="a genuinely specific finding message")
    provider = MockProvider(response_text=_fix_response())
    result = suggest_fix(finding, provider, extract=_fixed_context)
    assert isinstance(result, SuggestedFix)
    suite.check("title is exactly the finding's own message",
                result.title == "a genuinely specific finding message")


def test_suggest_fix_provider_offline(suite):
    provider = MockProvider(fail=True, failure_message="connection refused - offline")
    result = suggest_fix(_Finding(), provider, extract=_fixed_context)
    suite.check("no fix, no exception", result is None)


def test_suggest_fix_provider_timeout(suite):
    provider = MockProvider(fail=True, failure_message="did not respond within 30s")
    result = suggest_fix(_Finding(), provider, extract=_fixed_context)
    suite.check("no fix on a timeout either", result is None)


def test_suggest_fix_malformed_json_response(suite):
    provider = MockProvider(response_text="You could just fix the comparison operator.")
    result = suggest_fix(_Finding(), provider, extract=_fixed_context)
    suite.check("prose instead of JSON produces no fix", result is None)


def test_suggest_fix_invalid_schema_response(suite):
    """Valid JSON, wrong shape - missing the required 'suggested_fix' field."""
    provider = MockProvider(response_text='{"explanation": "only half the schema"}')
    result = suggest_fix(_Finding(), provider, extract=_fixed_context)
    suite.check("a schema-invalid response produces no fix", result is None)


def test_suggest_fix_insufficient_context_response(suite):
    provider = MockProvider(
        response_text='{"insufficient_context": true, "reason": "no code was supplied"}'
    )
    result = suggest_fix(_Finding(), provider, extract=_fixed_context)
    suite.check("an explicit decline produces no fix, not a fabricated one", result is None)


def test_suggest_fix_never_invents_a_placeholder(suite):
    """Every failure mode at once: none of them ever produces even an
    empty-field SuggestedFix - absence, not a placeholder, is the only
    signal of failure.
    """
    for provider in (
        MockProvider(fail=True),
        MockProvider(response_text="not json"),
        MockProvider(response_text='{"insufficient_context": true}'),
        MockProvider(response_text="{}"),
    ):
        result = suggest_fix(_Finding(), provider, extract=_fixed_context)
        suite.check("no SuggestedFix object at all - not even an empty one", result is None)


def test_suggest_fix_survives_a_genuinely_unexpected_exception(suite):
    """The last line of defence: even a completely broken provider must
    not be able to fail the QA run.
    """
    class _BrokenProvider:
        name = "broken"

        def generate(self, prompt):
            raise RuntimeError("something nobody anticipated")

    result = suggest_fix(_Finding(), _BrokenProvider(), extract=_fixed_context)
    suite.check("an exception inside the pipeline still yields None, not a crash", result is None)


# --- suggest_fixes: multiple findings, independence, ordering --------------


def test_suggest_fixes_multiple_all_succeed(suite):
    findings = [_Finding(file="a.py", line=1, message="issue a"),
                _Finding(file="b.py", line=2, message="issue b"),
                _Finding(file="c.py", line=3, message="issue c")]
    provider = _ScriptedProvider([
        LLMResponse(text=_fix_response("why a", "fix a")),
        LLMResponse(text=_fix_response("why b", "fix b")),
        LLMResponse(text=_fix_response("why c", "fix c")),
    ])
    fixes = suggest_fixes(findings, provider, extract=_fixed_context)
    suite.check("all three findings got their own fix", len(fixes) == 3)
    suite.check("each finding maps to its own correct replacement",
                fixes[findings[0]].replacement == "fix a"
                and fixes[findings[1]].replacement == "fix b"
                and fixes[findings[2]].replacement == "fix c")
    suite.check("the provider was called once per finding, in order", len(provider.calls) == 3)


def test_suggest_fixes_one_failure_does_not_affect_the_others(suite):
    """Execution isolation, the same principle the deterministic engine
    already uses for tools (docs/13): one finding's fix failing must never
    affect another's.
    """
    findings = [_Finding(file="a.py", line=1, message="issue a"),
                _Finding(file="b.py", line=2, message="issue b"),
                _Finding(file="c.py", line=3, message="issue c")]
    provider = _ScriptedProvider([
        LLMResponse(text=_fix_response("why a", "fix a")),
        LLMResponse(error="offline for this one call"),
        LLMResponse(text=_fix_response("why c", "fix c")),
    ])
    fixes = suggest_fixes(findings, provider, extract=_fixed_context)
    suite.check("exactly two of three succeeded", len(fixes) == 2)
    suite.check("the failing middle finding is simply absent, not present-with-None",
                findings[1] not in fixes)
    suite.check("the two that succeeded still have the right replacement",
                fixes[findings[0]].replacement == "fix a"
                and fixes[findings[2]].replacement == "fix c")


def test_suggest_fixes_returns_empty_mapping_when_all_fail(suite):
    findings = [_Finding(file="a.py"), _Finding(file="b.py")]
    fixes = suggest_fixes(findings, MockProvider(fail=True), extract=_fixed_context)
    suite.check("an empty dict, not None and not a crash", fixes == {})


def test_suggest_fixes_ordering_is_deterministic(suite):
    """Explaining the same findings twice, in the same order, must always
    produce the same result - no hidden randomness or ordering dependence.
    """
    findings = [_Finding(file="a.py", line=1, message="m1"),
                _Finding(file="b.py", line=2, message="m2")]

    def make_provider():
        return _ScriptedProvider([
            LLMResponse(text=_fix_response("why 1", "fix 1")),
            LLMResponse(text=_fix_response("why 2", "fix 2")),
        ])

    first = suggest_fixes(findings, make_provider(), extract=_fixed_context)
    second = suggest_fixes(findings, make_provider(), extract=_fixed_context)
    suite.check("identical findings/provider sequence produces identical fixes",
                {f: fx.replacement for f, fx in first.items()}
                == {f: fx.replacement for f, fx in second.items()})


# --- report.py: suggested-fix rendering -------------------------------------


def test_render_shows_fix_beneath_its_finding(suite):
    finding = _Finding(file="x.py", line=5, severity="error", message="a real issue", tool="ruff")
    result = _Result(checked=["x.py"], tools_used=["ruff"], findings=[finding])
    fixes = {finding: SuggestedFix(title="a real issue", explanation="why", replacement="fix")}
    output = render(result, "x.py", suggested_fixes=fixes)
    suite.check("the finding line is present, unchanged",
                "x.py:5  [error] a real issue  (ruff)" in output)
    suite.check("clearly labeled", "[AI Suggested Fix]" in output)
    suite.check("advisory wording is present", "advisory" in output and "review" in output)
    suite.check("the explanation appears", "why" in output)
    suite.check("the replacement appears", "fix" in output)
    finding_pos = output.find("x.py:5")
    fix_pos = output.find("[AI Suggested Fix]")
    suite.check("the fix appears beneath (after) its finding, not before",
                0 <= finding_pos < fix_pos)


def test_render_fix_appears_beneath_explanation_not_in_place_of_it(suite):
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    explanations = {finding: Explanation(text="the explanation text")}
    fixes = {finding: SuggestedFix(title="t", explanation="why", replacement="the fix text")}
    output = render(result, "a.py", explanations=explanations, suggested_fixes=fixes)
    suite.check("the explanation is still present, not replaced",
                "[AI Explanation] the explanation text" in output)
    suite.check("the fix is also present", "[AI Suggested Fix]" in output)
    explanation_pos = output.find("[AI Explanation]")
    fix_pos = output.find("[AI Suggested Fix]")
    suite.check("the fix renders after (beneath) the explanation",
                0 <= explanation_pos < fix_pos)


def test_render_fix_never_claims_certainty(suite):
    """Requirement 7: every suggestion must be presented as requiring
    developer review, never as a guaranteed-correct fact.
    """
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    fixes = {finding: SuggestedFix(title="t", explanation="why", replacement="fix")}
    output = render(result, "a.py", suggested_fixes=fixes)
    suite.check("the wording explicitly says review before applying",
                "review before applying" in output)


def test_render_fix_preserves_multiline_replacement(suite):
    """Unlike an AI explanation (collapsed to one line), a suggested code
    fix legitimately needs its own line structure preserved to stay
    readable - checked directly, not assumed.
    """
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    fixes = {finding: SuggestedFix(
        title="t", explanation="why",
        replacement="if value is None:\n    return True\nreturn False",
    )}
    output = render(result, "a.py", suggested_fixes=fixes)
    suite.check("all three replacement lines are present",
                "if value is None:" in output and "return True" in output
                and "return False" in output)
    suite.check("the replacement's own line breaks survive (not collapsed to one line)",
                output.count("\n") > 5)


def test_render_omits_fix_when_none_available(suite):
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    output = render(result, "a.py", suggested_fixes={})
    suite.check("no [AI Suggested Fix] block when the mapping has nothing for this finding",
                "[AI Suggested Fix]" not in output)


def test_render_findings_also_supports_suggested_fixes(suite):
    """Watch mode's own renderer (report.render_findings) must support the
    same optional suggested_fixes parameter, not just the one-shot
    render() - matching explanations' own precedent from Part 3.
    """
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    fixes = {finding: SuggestedFix(title="t", explanation="why",
                                    replacement="watch-mode fix text")}
    output = render_findings(result, suggested_fixes=fixes)
    suite.check("the fix renders in the watch-mode findings view too",
                "watch-mode fix text" in output)


def test_render_markdown_adds_a_separate_suggested_fixes_section(suite):
    finding = _Finding(file="x.py", line=5, tool="ruff", message="a real issue")
    result = _Result(checked=["x.py"], tools_used=["ruff"], findings=[finding])
    fixes = {finding: SuggestedFix(title="a real issue", explanation="why this helps",
                                    replacement="value is None")}
    output = render_markdown(result, "x.py", suggested_fixes=fixes)
    suite.check("a distinct AI Suggested Fixes section exists", "## AI Suggested Fixes" in output)
    suite.check("advisory wording is present in the section", "advisory" in output)
    suite.check("the explanation appears", "why this helps" in output)
    suite.check("the replacement appears inside a fenced code block",
                "```\nvalue is None\n```" in output)
    suite.check("the findings table itself is completely unchanged",
                "| `x.py` | 5 | error | a real issue | ruff |" in output)


def test_render_markdown_omits_the_section_when_no_fixes(suite):
    finding = _Finding()
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[finding])
    output = render_markdown(result, "a.py", suggested_fixes=None)
    suite.check("no AI Suggested Fixes section at all", "AI Suggested Fixes" not in output)


def test_render_byte_identical_with_ai_disabled(suite):
    """Requirement 11, checked directly: omitting suggested fixes (or
    passing None) must produce output byte-for-byte identical to never
    having passed the parameter at all - across both renderers.
    """
    finding = _Finding(file="x.py", line=5, severity="error", message="a real issue", tool="ruff")
    result = _Result(checked=["x.py"], tools_used=["ruff"], findings=[finding])
    without_param = render(result, "x.py")
    with_none = render(result, "x.py", suggested_fixes=None)
    with_empty = render(result, "x.py", suggested_fixes={})
    suite.check("no suggested_fixes param == suggested_fixes=None, byte for byte",
                without_param == with_none)
    suite.check("suggested_fixes=None == suggested_fixes={}, byte for byte",
                with_none == with_empty)
    suite.check("no [AI Suggested Fix] text anywhere", "[AI Suggested Fix]" not in without_param)

    md_without = render_markdown(result, "x.py")
    md_with_none = render_markdown(result, "x.py", suggested_fixes=None)
    suite.check("markdown: no param == None, byte for byte", md_without == md_with_none)


# --- Phase C regression: golden files stay byte-for-byte identical ---------


def test_phase_c_golden_output_unaffected(suite):
    """The real, end-to-end proof: running the actual CLI - which never
    calls suggest_fixes() at all in this part - must still match every
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
    suite = Suite("AI suggested fixes (Phase D Part 5)")
    sys.exit(suite.run([
        test_suggest_fix_success,
        test_suggest_fix_title_comes_from_the_finding_not_the_model,
        test_suggest_fix_provider_offline,
        test_suggest_fix_provider_timeout,
        test_suggest_fix_malformed_json_response,
        test_suggest_fix_invalid_schema_response,
        test_suggest_fix_insufficient_context_response,
        test_suggest_fix_never_invents_a_placeholder,
        test_suggest_fix_survives_a_genuinely_unexpected_exception,
        test_suggest_fixes_multiple_all_succeed,
        test_suggest_fixes_one_failure_does_not_affect_the_others,
        test_suggest_fixes_returns_empty_mapping_when_all_fail,
        test_suggest_fixes_ordering_is_deterministic,
        test_render_shows_fix_beneath_its_finding,
        test_render_fix_appears_beneath_explanation_not_in_place_of_it,
        test_render_fix_never_claims_certainty,
        test_render_fix_preserves_multiline_replacement,
        test_render_omits_fix_when_none_available,
        test_render_findings_also_supports_suggested_fixes,
        test_render_markdown_adds_a_separate_suggested_fixes_section,
        test_render_markdown_omits_the_section_when_no_fixes,
        test_render_byte_identical_with_ai_disabled,
        test_phase_c_golden_output_unaffected,
    ]))
