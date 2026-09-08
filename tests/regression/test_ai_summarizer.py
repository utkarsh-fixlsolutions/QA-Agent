"""AI-generated run summaries (Phase D Part 4): summarizer.py, and
report.py's optional summary rendering.

Pure unit tests, no network and no live LLM - matching test_ai_explainer.py
and the rest of this project's AI test suites. `MockProvider` covers the
simple always-succeed/always-fail cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, compare_golden, normalise, run_agent  # noqa: E402

from qa_agent.ai import MockProvider  # noqa: E402
from qa_agent.ai.summarizer import Summary, summarize_run  # noqa: E402
from qa_agent.report import render, render_markdown  # noqa: E402


class _Finding:
    """A plain stand-in for adapters.Finding, matching prompts.py's own
    documented duck-typed usage.
    """

    def __init__(self, file="a.py", line=3, severity="error",
                 message="sample finding message qzx9", tool="ruff"):
        self.file, self.line, self.severity, self.message, self.tool = (
            file, line, severity, message, tool,
        )


class _Result:
    """A plain stand-in for runner.RunResult, matching prompts.py's own
    documented duck-typed usage.
    """

    def __init__(self, checked=(), tools_used=(), findings=()):
        self.checked, self.tools_used, self.findings = checked, tools_used, findings
        self.filtered = 0
        self.tool_errors = []
        self.skipped = []
        self.missing = []


def _empty_run():
    return _Result(checked=[], tools_used=[], findings=[])


def _run_with_findings():
    findings = [
        _Finding(file="a.py", line=1, severity="error", message="unused import", tool="ruff"),
        _Finding(file="b.py", line=2, severity="warning", message="type mismatch", tool="mypy"),
    ]
    return _Result(checked=["a.py", "b.py"], tools_used=["mypy", "ruff"], findings=findings)


def _multi_analyzer_run():
    findings = [
        _Finding(file="a.js", line=1, severity="error", message="no-unused-vars", tool="eslint"),
        _Finding(file="b.py", line=2, severity="error", message="F401 unused import", tool="ruff"),
        _Finding(file="b.py", line=5, severity="error", message="reportMissingImports",
                 tool="pyright"),
        _Finding(file="b.py", line=5, severity="error", message="import-not-found", tool="mypy"),
        _Finding(file="c.sh", line=1, severity="warning", message="SC2086", tool="shellcheck"),
    ]
    return _Result(checked=["a.js", "b.py", "c.sh"],
                    tools_used=["eslint", "mypy", "pyright", "ruff", "shellcheck"],
                    findings=findings)


# --- summarize_run: success, and every required failure mode ---------------


def test_summarize_run_success(suite):
    provider = MockProvider(
        response_text='{"summary": "Two issues were found across two files."}'
    )
    result = summarize_run(_run_with_findings(), provider)
    suite.check("a Summary is returned", isinstance(result, Summary))
    assert isinstance(result, Summary)
    suite.check("the model's real text is used",
                result.text == "Two issues were found across two files.")


def test_summarize_run_provider_offline(suite):
    provider = MockProvider(fail=True, failure_message="connection refused - offline")
    result = summarize_run(_run_with_findings(), provider)
    suite.check("no summary, no exception", result is None)


def test_summarize_run_provider_timeout(suite):
    provider = MockProvider(fail=True, failure_message="did not respond within 30s")
    result = summarize_run(_run_with_findings(), provider)
    suite.check("no summary on a timeout either", result is None)


def test_summarize_run_malformed_json_response(suite):
    provider = MockProvider(response_text="This run looks pretty clean overall.")
    result = summarize_run(_run_with_findings(), provider)
    suite.check("prose instead of JSON produces no summary", result is None)


def test_summarize_run_invalid_schema_response(suite):
    """Valid JSON, wrong shape - missing the required 'summary' field."""
    provider = MockProvider(response_text='{"comment": "looks fine to me"}')
    result = summarize_run(_run_with_findings(), provider)
    suite.check("a schema-invalid response produces no summary", result is None)


def test_summarize_run_insufficient_context_response(suite):
    provider = MockProvider(
        response_text='{"insufficient_context": true, "reason": "not enough information"}'
    )
    result = summarize_run(_run_with_findings(), provider)
    suite.check("an explicit decline produces no summary, not a fabricated one",
                result is None)


def test_summarize_run_never_invents_a_placeholder(suite):
    """Every failure mode at once: none of them ever produces so much as an
    empty-string Summary - absence, not a placeholder, is the only signal.
    """
    for provider in (
        MockProvider(fail=True),
        MockProvider(response_text="not json"),
        MockProvider(response_text='{"insufficient_context": true}'),
        MockProvider(response_text="{}"),
    ):
        result = summarize_run(_run_with_findings(), provider)
        suite.check("no Summary object at all - not even an empty one", result is None)


def test_summarize_run_survives_a_genuinely_unexpected_exception(suite):
    """The last line of defence: even a completely broken provider must
    not be able to fail the QA run.
    """
    class _BrokenProvider:
        name = "broken"

        def generate(self, prompt):
            raise RuntimeError("something nobody anticipated")

    result = summarize_run(_run_with_findings(), _BrokenProvider())
    suite.check("an exception inside the pipeline still yields None, not a crash", result is None)


# --- run shapes: empty, with findings, multi-analyzer -----------------------


def test_summarize_run_empty_run(suite):
    """A run with zero findings must still build a valid prompt and be
    summarizable - build_summary_prompt() already handles this shape
    (Phase D Part 2); summarize_run() adds nothing special for it.
    """
    provider = MockProvider(response_text='{"summary": "No issues were found."}')
    result = summarize_run(_empty_run(), provider)
    suite.check("an empty run can still be summarized", isinstance(result, Summary))
    assert isinstance(result, Summary)
    suite.check("the summary text is used as given", result.text == "No issues were found.")


def test_summarize_run_with_findings(suite):
    provider = MockProvider(
        response_text='{"summary": "Findings span two tools across two files."}'
    )
    result = summarize_run(_run_with_findings(), provider)
    suite.check("succeeds normally on a run with real findings", isinstance(result, Summary))


def test_summarize_run_multi_analyzer_run(suite):
    """A run involving five different tools at once must summarize just
    like any other - no special-casing by tool count or identity.
    """
    provider = MockProvider(
        response_text='{"summary": "Five tools reported issues across three files."}'
    )
    result = summarize_run(_multi_analyzer_run(), provider)
    suite.check("a multi-analyzer run summarizes successfully", isinstance(result, Summary))


def test_summarize_run_prompt_reflects_the_real_run_data(suite):
    """The prompt actually sent to the provider must be built from this
    run's own real data (tools, checked count, findings) - not generic or
    invented text - proving build_summary_prompt() is genuinely reused,
    not reimplemented.
    """
    captured = {}

    class _CapturingProvider:
        name = "capturing"

        def generate(self, prompt):
            captured["prompt"] = prompt
            return type("R", (), {"ok": True, "text": '{"summary": "ok"}'})()

    summarize_run(_multi_analyzer_run(), _CapturingProvider())
    prompt_text = captured["prompt"]
    suite.check("the real tool names appear in the prompt",
                all(tool in prompt_text for tool in
                    ("eslint", "mypy", "pyright", "ruff", "shellcheck")))
    suite.check("the real checked-file count appears", "3" in prompt_text)
    suite.check("a real finding's own message appears verbatim",
                "no-unused-vars" in prompt_text)


# --- report.py: summary rendering -------------------------------------------


def test_render_shows_summary_before_findings(suite):
    result = _run_with_findings()
    summary = Summary(text="Two issues were found across two files.")
    output = render(result, "app", summary=summary)
    suite.check("clearly labeled", "AI Summary" in output)
    suite.check("the summary text appears", "Two issues were found across two files." in output)
    suite.check("deterministic underline formatting", "----------" in output)
    summary_pos = output.find("AI Summary")
    findings_pos = output.find("Findings (")
    suite.check("the summary appears before the findings, not after",
                0 <= summary_pos < findings_pos)


def test_render_summary_never_replaces_findings(suite):
    result = _run_with_findings()
    summary = Summary(text="Everything looks fine overall.")
    output = render(result, "app", summary=summary)
    suite.check("the real findings are still fully present",
                "a.py:1" in output and "unused import" in output
                and "b.py:2" in output and "type mismatch" in output)
    suite.check("the exact finding count is still shown", "Findings (2):" in output)


def test_render_omits_summary_section_when_none_available(suite):
    result = _run_with_findings()
    output = render(result, "app", summary=None)
    suite.check("no AI Summary section at all", "AI Summary" not in output)


def test_render_multiline_summary_collapses_to_one_line(suite):
    result = _run_with_findings()
    summary = Summary(text="line one\nline two\n\nline three")
    output = render(result, "app", summary=summary)
    ai_summary_lines = output.splitlines()[
        output.splitlines().index("AI Summary") + 2
    ]
    suite.check("the summary body is exactly one line",
                ai_summary_lines == "line one line two line three")


def test_render_markdown_adds_a_summary_section(suite):
    result = _run_with_findings()
    summary = Summary(text="Markdown summary text.")
    output = render_markdown(result, "app", summary=summary)
    suite.check("a distinct AI Summary section exists", "## AI Summary" in output)
    suite.check("the summary text appears in it", "Markdown summary text." in output)
    suite.check("the findings table itself is completely unchanged",
                "| `a.py` | 1 | error | unused import | ruff |" in output)
    summary_pos = output.find("## AI Summary")
    findings_pos = output.find("## Findings")
    suite.check("the summary section appears before the findings table",
                0 <= summary_pos < findings_pos)


def test_render_markdown_omits_summary_when_none_available(suite):
    result = _run_with_findings()
    output = render_markdown(result, "app", summary=None)
    suite.check("no AI Summary section at all", "AI Summary" not in output)


def test_render_byte_identical_with_ai_disabled(suite):
    """Requirement 10, checked directly: omitting the summary (or passing
    None) must produce output byte-for-byte identical to never having
    passed the parameter at all - across both renderers.
    """
    result = _run_with_findings()
    without_param = render(result, "app")
    with_none = render(result, "app", summary=None)
    suite.check("no summary param == summary=None, byte for byte (terminal)",
                without_param == with_none)
    suite.check("no AI Summary text anywhere", "AI Summary" not in without_param)

    md_without = render_markdown(result, "app")
    md_with_none = render_markdown(result, "app", summary=None)
    suite.check("no summary param == summary=None, byte for byte (markdown)",
                md_without == md_with_none)


def test_render_summary_and_explanations_coexist(suite):
    """A summary and per-finding explanations are independent, additive
    features (Parts 3 and 4) - both must be able to render at once without
    interfering with each other.
    """
    from qa_agent.ai.explainer import Explanation

    result = _run_with_findings()
    finding = result.findings[0]
    summary = Summary(text="Run-level summary text.")
    explanations = {finding: Explanation(text="Finding-level explanation text.")}
    output = render(result, "app", explanations=explanations, summary=summary)
    suite.check("both the summary and the explanation are present",
                "Run-level summary text." in output
                and "Finding-level explanation text." in output)
    suite.check("the summary still appears before the findings section",
                output.find("AI Summary") < output.find("Findings ("))


# --- Phase C regression: golden files stay byte-for-byte identical ---------


def test_phase_c_golden_output_unaffected(suite):
    """The real, end-to-end proof: running the actual CLI - which never
    calls summarize_run() at all in this part - must still match every
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
    suite = Suite("AI run summaries (Phase D Part 4)")
    sys.exit(suite.run([
        test_summarize_run_success,
        test_summarize_run_provider_offline,
        test_summarize_run_provider_timeout,
        test_summarize_run_malformed_json_response,
        test_summarize_run_invalid_schema_response,
        test_summarize_run_insufficient_context_response,
        test_summarize_run_never_invents_a_placeholder,
        test_summarize_run_survives_a_genuinely_unexpected_exception,
        test_summarize_run_empty_run,
        test_summarize_run_with_findings,
        test_summarize_run_multi_analyzer_run,
        test_summarize_run_prompt_reflects_the_real_run_data,
        test_render_shows_summary_before_findings,
        test_render_summary_never_replaces_findings,
        test_render_omits_summary_section_when_none_available,
        test_render_multiline_summary_collapses_to_one_line,
        test_render_markdown_adds_a_summary_section,
        test_render_markdown_omits_summary_when_none_available,
        test_render_byte_identical_with_ai_disabled,
        test_render_summary_and_explanations_coexist,
        test_phase_c_golden_output_unaffected,
    ]))
