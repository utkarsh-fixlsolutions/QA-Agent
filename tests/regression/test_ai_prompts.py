"""Prompt architecture & context extraction (Phase D Part 2): context.py,
prompts.py, schemas.py, response_parser.py.

Pure unit tests, no network and no live LLM - matching test_ai_provider.py's
own style. Prompt builders take plain duck-typed objects rather than real
Finding/RunResult instances, exactly as prompts.py's own docstring
documents, so a fake here is not a shortcut - it is the intended, supported
usage.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite, TempProject  # noqa: E402

from qa_agent.ai.context import extract_context  # noqa: E402
from qa_agent.ai.prompts import (  # noqa: E402
    GUARDRAILS,
    MAX_SUMMARY_FINDINGS,
    build_explanation_prompt,
    build_fix_prompt,
    build_summary_prompt,
)
from qa_agent.ai.response_parser import (  # noqa: E402
    parse_json_response,
    strip_markdown_fence,
    validate_explanation_response,
    validate_fix_response,
    validate_summary_response,
)
from qa_agent.ai.schemas import (  # noqa: E402
    STATUS_INSUFFICIENT_CONTEXT,
    STATUS_INVALID,
    STATUS_SUCCESS,
    ExplanationResponse,
    FixResponse,
    SummaryResponse,
)


def _text(value):
    """Narrow an Optional[str] a test already knows must be set (via a
    status/ok check just made) - an assertion, not a silent fallback, so a
    genuine None where one isn't expected fails loudly rather than being
    masked.
    """
    assert value is not None
    return value


def _explanation(result):
    assert isinstance(result.value, ExplanationResponse)
    return result.value


def _summary(result):
    assert isinstance(result.value, SummaryResponse)
    return result.value


def _fix(result):
    assert isinstance(result.value, FixResponse)
    return result.value


class _Finding:
    """A plain stand-in for adapters.Finding - proves the builders are
    genuinely duck-typed, not secretly coupled to the real class.
    """

    def __init__(self, file="a.py", line=3, severity="error",
                 message="sample finding message qzx9", tool="ruff"):
        self.file, self.line, self.severity, self.message, self.tool = (
            file, line, severity, message, tool,
        )


class _Result:
    """A plain stand-in for runner.RunResult - same reasoning as _Finding."""

    def __init__(self, checked=(), tools_used=(), findings=()):
        self.checked, self.tools_used, self.findings = checked, tools_used, findings


# --- context extraction: the basic window, exactly as requested ------------


def test_extract_context_basic_window(suite):
    with TempProject() as root:
        target = root / "sample.py"
        target.write_text("\n".join("line{}".format(i) for i in range(1, 21)), encoding="utf-8")
        context = extract_context(target, 10, lines_before=2, lines_after=2)
    suite.check("ok", context.ok)
    suite.check("start/end reflect the requested window",
                context.start_line == 8 and context.end_line == 12)
    suite.check("target line is included, correctly", (10, "line10") in context.lines)
    suite.check("exactly 5 lines (2 before + target + 2 after)", len(context.lines) == 5)
    suite.check("not truncated", context.truncated is False)


def test_extract_context_clips_at_start_of_file(suite):
    """Requesting more lines-before than exist must not go negative or error -
    it clips to line 1."""
    with TempProject() as root:
        target = root / "sample.py"
        target.write_text("\n".join("line{}".format(i) for i in range(1, 21)), encoding="utf-8")
        context = extract_context(target, 2, lines_before=10, lines_after=1)
    suite.check("ok", context.ok)
    suite.check("start clips to line 1, not a negative number", context.start_line == 1)
    suite.check("end is still exactly what was requested", context.end_line == 3)


def test_extract_context_clips_at_end_of_file(suite):
    with TempProject() as root:
        target = root / "sample.py"
        target.write_text("\n".join("line{}".format(i) for i in range(1, 11)), encoding="utf-8")
        context = extract_context(target, 9, lines_before=1, lines_after=10)
    suite.check("ok", context.ok)
    suite.check("end clips to the real last line (10), not the requested 19",
                context.end_line == 10)
    suite.check("start is still exactly what was requested", context.start_line == 8)


def test_extract_context_missing_file(suite):
    context = extract_context(REPO_ROOT / "definitely_does_not_exist.py", 1)
    suite.check("not ok, did not raise", not context.ok)
    suite.check("a clear reason is given", "not found" in _text(context.error))
    suite.check("no lines on a failure", context.lines == ())


def test_extract_context_line_beyond_end_of_file(suite):
    with TempProject() as root:
        target = root / "short.py"
        target.write_text("only one line", encoding="utf-8")
        context = extract_context(target, 500)
    suite.check("not ok - the target line does not exist", not context.ok)
    suite.check("the reason names the problem", "fewer than" in _text(context.error))


def test_extract_context_rejects_a_non_positive_line_number(suite):
    with TempProject() as root:
        target = root / "sample.py"
        target.write_text("x = 1\n", encoding="utf-8")
        zero = extract_context(target, 0)
        negative = extract_context(target, -3)
    suite.check("line 0 is rejected, not silently treated as line 1", not zero.ok)
    suite.check("a negative line is rejected the same way", not negative.ok)


def test_extract_context_respects_max_lines_cap(suite):
    """A caller asking for an enormous window must still be bounded by
    max_lines - the token/line limit this part explicitly requires.
    """
    with TempProject() as root:
        target = root / "big.py"
        target.write_text("\n".join("line{}".format(i) for i in range(1, 2001)), encoding="utf-8")
        context = extract_context(target, 1000, lines_before=900, lines_after=900, max_lines=20)
    suite.check("ok", context.ok)
    suite.check("the window never exceeds max_lines", len(context.lines) <= 20)
    suite.check("the target line still survives the cap",
                any(number == 1000 for number, _ in context.lines))


def test_extract_context_respects_max_chars_cap(suite):
    with TempProject() as root:
        target = root / "wide.py"
        # Each line is long enough that a handful of them exceed a small
        # max_chars budget, forcing real truncation.
        target.write_text(
            "\n".join("x" * 200 + str(i) for i in range(1, 21)), encoding="utf-8"
        )
        context = extract_context(target, 10, lines_before=5, lines_after=5, max_chars=500)
    suite.check("ok", context.ok)
    suite.check("truncated is reported, not silently shrunk", context.truncated is True)
    suite.check("the joined text fits the budget",
                sum(len(text) for _, text in context.lines) <= 500 + len(context.lines))
    suite.check("the target line always survives truncation",
                any(number == 10 for number, _ in context.lines))


def test_extract_context_never_drops_the_target_line_even_when_it_alone_is_huge(suite):
    """A single line longer than max_chars must still be returned whole -
    better a real, over-budget line than a fabricated partial one.
    """
    with TempProject() as root:
        target = root / "onehuge.py"
        target.write_text("short\n" + "y" * 5000 + "\nshort2\n", encoding="utf-8")
        context = extract_context(target, 2, lines_before=1, lines_after=1, max_chars=100)
    suite.check("ok", context.ok)
    suite.check("the oversized target line is present in full, not truncated mid-line",
                any(number == 2 and len(text) == 5000 for number, text in context.lines))


def test_extract_context_on_a_large_file_stays_fast(suite):
    """Not a strict proof of "never reads the whole file", but a real,
    measurable check that a window near the start of a large file does not
    become proportionally slow - consistent with "avoid loading entire
    files unnecessarily".
    """
    with TempProject() as root:
        target = root / "huge.py"
        target.write_text("\n".join("line{}".format(i) for i in range(1, 200001)), encoding="utf-8")
        start = time.perf_counter()
        context = extract_context(target, 5, lines_before=2, lines_after=2)
        elapsed = time.perf_counter() - start
    suite.check("ok", context.ok)
    suite.check("correct window near the start of a 200,000-line file",
                context.start_line == 3 and context.end_line == 7)
    suite.check("fast - a small window near the start, not a full-file load",
                elapsed < 1.0, "  [{:.3f}s]".format(elapsed))


# --- prompt builders ---------------------------------------------------


def test_build_explanation_prompt_includes_finding_fields(suite):
    finding = _Finding(file="app/util.py", line=5, severity="warning",
                        message="unused variable 'x'", tool="ruff")
    with TempProject() as root:
        target = root / "util.py"
        target.write_text("\n".join("l{}".format(i) for i in range(1, 11)), encoding="utf-8")
        context = extract_context(target, 5)
        prompt = build_explanation_prompt(finding, context)
    suite.check("file is present in the user prompt", "app/util.py" in prompt.user)
    suite.check("line is present", "5" in prompt.user)
    suite.check("severity is present", "warning" in prompt.user)
    suite.check("message is present verbatim", "unused variable 'x'" in prompt.user)
    suite.check("tool is present", "ruff" in prompt.user)


def test_build_explanation_prompt_includes_code_context_and_marks_target_line(suite):
    finding = _Finding(line=5)
    with TempProject() as root:
        target = root / "util.py"
        target.write_text("\n".join("l{}".format(i) for i in range(1, 11)), encoding="utf-8")
        context = extract_context(target, 5, lines_before=2, lines_after=2)
        prompt = build_explanation_prompt(finding, context)
    suite.check("surrounding lines appear in the prompt", "l3" in prompt.user and "l7" in prompt.user)
    suite.check("the target line is visually marked", ">>" in prompt.user and "l5" in prompt.user)


def test_build_explanation_prompt_handles_unavailable_context_gracefully(suite):
    """context.ok=False must be shown honestly, not silently produce an
    empty or misleading code block.
    """
    finding = _Finding()
    bad_context = extract_context(REPO_ROOT / "nope.py", 1)
    prompt = build_explanation_prompt(finding, bad_context)
    suite.check("the failure reason is surfaced in the prompt, not hidden",
                "not found" in prompt.user)
    suite.check("no code fence is opened when there is no code to show",
                "```" not in prompt.user)


def test_build_fix_prompt_asks_for_explanation_and_suggested_fix(suite):
    finding = _Finding()
    context = extract_context(REPO_ROOT / "nope.py", 1)  # ok=False is fine here
    prompt = build_fix_prompt(finding, context)
    suite.check("asks for 'explanation'", '"explanation"' in prompt.user)
    suite.check("asks for 'suggested_fix'", '"suggested_fix"' in prompt.user)


def test_build_summary_prompt_lists_findings(suite):
    findings = [
        _Finding(file="a.py", line=1, severity="error", message="m1", tool="ruff"),
        _Finding(file="b.py", line=2, severity="warning", message="m2", tool="pyright"),
    ]
    result = _Result(checked=["a.py", "b.py"], tools_used=["pyright", "ruff"], findings=findings)
    prompt = build_summary_prompt(result)
    suite.check("checked count present", "2" in prompt.user)
    suite.check("both tools listed", "pyright" in prompt.user and "ruff" in prompt.user)
    suite.check("both findings' messages present", "m1" in prompt.user and "m2" in prompt.user)
    suite.check("asks for 'summary'", '"summary"' in prompt.user)


def test_build_summary_prompt_handles_zero_findings(suite):
    result = _Result(checked=["a.py"], tools_used=["ruff"], findings=[])
    prompt = build_summary_prompt(result)
    suite.check("states plainly that nothing was found",
                "No findings were reported." in prompt.user)


def test_build_summary_prompt_caps_long_finding_lists(suite):
    """More findings than MAX_SUMMARY_FINDINGS must not all be dumped into
    the prompt - the same "cap it, count the remainder" precedent
    live_report.py already established for a different long list.
    """
    findings = [
        _Finding(file="f{}.py".format(i), line=1, message="m{}".format(i))
        for i in range(MAX_SUMMARY_FINDINGS + 15)
    ]
    result = _Result(checked=["x"], tools_used=["ruff"], findings=findings)
    prompt = build_summary_prompt(result)
    suite.check("the true total is still reported",
                str(MAX_SUMMARY_FINDINGS + 15) in prompt.user)
    suite.check("the overflow is counted, not silently dropped", "...and 15 more" in prompt.user)
    suite.check("only the capped number of individual finding lines appear",
                prompt.user.count(" - [") == MAX_SUMMARY_FINDINGS)


def test_all_builders_include_the_shared_guardrails(suite):
    """Consistency: every builder's system prompt carries the exact same
    anti-hallucination guardrail text - never respelled per builder.
    """
    finding = _Finding()
    context = extract_context(REPO_ROOT / "nope.py", 1)
    result = _Result(checked=[], tools_used=[], findings=[])
    prompts = [
        build_explanation_prompt(finding, context),
        build_fix_prompt(finding, context),
        build_summary_prompt(result),
    ]
    suite.check("all three system prompts contain the exact shared GUARDRAILS text",
                all(GUARDRAILS in p.system for p in prompts))


def test_all_builders_separate_system_and_user_text(suite):
    finding = _Finding()
    context = extract_context(REPO_ROOT / "nope.py", 1)
    prompt = build_explanation_prompt(finding, context)
    suite.check("system and user are genuinely different strings",
                prompt.system != prompt.user)
    suite.check("the finding's own data lives in user, not system",
                finding.message not in prompt.system and finding.message in prompt.user)
    suite.check("the guardrails live in system, not user",
                GUARDRAILS in prompt.system and GUARDRAILS not in prompt.user)


def test_all_builders_instruct_json_only_response(suite):
    finding = _Finding()
    context = extract_context(REPO_ROOT / "nope.py", 1)
    result = _Result(checked=[], tools_used=[], findings=[])
    for prompt in (
        build_explanation_prompt(finding, context),
        build_fix_prompt(finding, context),
        build_summary_prompt(result),
    ):
        suite.check("system prompt instructs JSON-only responses",
                     "JSON only" in prompt.system)


def test_prompt_builders_never_import_the_provider_layer(suite):
    """The real, source-level proof this part requires: prompt construction
    only ever constructs prompts - it must never call a provider.
    """
    source = (REPO_ROOT / "qa_agent" / "ai" / "prompts.py").read_text(encoding="utf-8")
    suite.check("prompts.py never imports the provider protocol", "provider" not in source.lower()
                or "from .provider" not in source)
    suite.check("prompts.py never imports OllamaProvider", "OllamaProvider" not in source)
    suite.check("prompts.py never imports MockProvider", "MockProvider" not in source)


def test_context_module_has_no_qa_agent_imports(suite):
    """context.py's own claim, checked directly: independent of everything
    else in this package, pure stdlib.
    """
    source = (REPO_ROOT / "qa_agent" / "ai" / "context.py").read_text(encoding="utf-8")
    suite.check("no relative or qa_agent import anywhere in context.py",
                "import qa_agent" not in source and "from ." not in source)


# --- response parsing: fences, malformed JSON, required fields -------------


def test_strip_markdown_fence_json_tagged(suite):
    text = '```json\n{"a": 1}\n```'
    suite.check("the fence and language tag are removed", strip_markdown_fence(text) == '{"a": 1}')


def test_strip_markdown_fence_bare_fence(suite):
    text = '```\n{"a": 1}\n```'
    suite.check("a bare fence (no 'json' tag) is removed too",
                strip_markdown_fence(text) == '{"a": 1}')


def test_strip_markdown_fence_no_fence_present(suite):
    text = '{"a": 1}'
    suite.check("bare JSON with no fence is returned unchanged",
                strip_markdown_fence(text) == '{"a": 1}')


def test_strip_markdown_fence_with_surrounding_prose(suite):
    """A model told "JSON only" that still adds a sentence around the fence
    - realistic, not hypothetical, so this must still work.
    """
    text = 'Sure, here you go:\n```json\n{"a": 1}\n```\nLet me know if you need more.'
    suite.check("the fenced JSON is extracted even with prose around it",
                strip_markdown_fence(text) == '{"a": 1}')


def test_parse_json_response_valid(suite):
    data, error = parse_json_response('{"a": 1}')
    suite.check("parses correctly", data == {"a": 1})
    suite.check("no error on success", error is None)


def test_parse_json_response_malformed(suite):
    data, error = parse_json_response("not json at all")
    suite.check("data is None on failure", data is None)
    suite.check("a clear error is given, not a raised exception", "not valid JSON" in _text(error))


def test_parse_json_response_empty_string(suite):
    data, error = parse_json_response("   ")
    suite.check("data is None", data is None)
    suite.check("a clear 'empty' error is given", "empty" in _text(error))


# --- validators: success, missing fields, wrong types, malformed JSON ------


def test_validate_explanation_response_success(suite):
    result = validate_explanation_response('{"explanation": "it is unused"}')
    suite.check("status is success", result.status == STATUS_SUCCESS)
    suite.check("ok property matches", result.ok is True)
    suite.check("the value is the validated schema object",
                _explanation(result).explanation == "it is unused")


def test_validate_explanation_response_accepts_a_markdown_fence(suite):
    result = validate_explanation_response('```json\n{"explanation": "fenced"}\n```')
    suite.check("fenced JSON still validates successfully", result.ok)
    suite.check("value extracted correctly", _explanation(result).explanation == "fenced")


def test_validate_explanation_response_missing_field(suite):
    result = validate_explanation_response('{"something_else": "x"}')
    suite.check("status is invalid, not success", result.status == STATUS_INVALID)
    suite.check("the missing field is named", "explanation" in _text(result.reason))
    suite.check("no value on an invalid result", result.value is None)


def test_validate_explanation_response_wrong_field_type(suite):
    """A field present but the wrong type (a number, not a string) must be
    rejected exactly like a missing one - a real "malformed or incomplete
    response" case, not just absence.
    """
    result = validate_explanation_response('{"explanation": 12345}')
    suite.check("rejected as invalid", result.status == STATUS_INVALID)


def test_validate_explanation_response_blank_field(suite):
    result = validate_explanation_response('{"explanation": "   "}')
    suite.check("a blank string does not count as a real explanation",
                result.status == STATUS_INVALID)


def test_validate_explanation_response_malformed_json(suite):
    result = validate_explanation_response("this is not json")
    suite.check("status is invalid", result.status == STATUS_INVALID)
    suite.check("did not raise - a structured failure came back instead", result is not None)


def test_validate_explanation_response_not_a_json_object(suite):
    result = validate_explanation_response('["just", "an", "array"]')
    suite.check("a JSON array is rejected, not silently accepted", result.status == STATUS_INVALID)
    suite.check("the reason names the real problem", "object" in _text(result.reason))


def test_validate_summary_response_success(suite):
    result = validate_summary_response('{"summary": "3 issues found"}')
    suite.check("ok", result.ok)
    suite.check("value is a SummaryResponse with the right text",
                _summary(result).summary == "3 issues found")


def test_validate_summary_response_missing_field(suite):
    result = validate_summary_response("{}")
    suite.check("status is invalid", result.status == STATUS_INVALID)
    suite.check("names the missing field", "summary" in _text(result.reason))


def test_validate_fix_response_success(suite):
    result = validate_fix_response('{"explanation": "e", "suggested_fix": "f"}')
    suite.check("ok", result.ok)
    fix = _fix(result)
    suite.check("both fields present in the validated value",
                fix.explanation == "e" and fix.suggested_fix == "f")


def test_validate_fix_response_missing_one_field(suite):
    result = validate_fix_response('{"explanation": "e"}')
    suite.check("status is invalid", result.status == STATUS_INVALID)
    reason = _text(result.reason)
    suite.check("names exactly the missing field, not the present one",
                "suggested_fix" in reason and "explanation" not in reason)


def test_validate_fix_response_missing_both_fields(suite):
    result = validate_fix_response("{}")
    suite.check("status is invalid", result.status == STATUS_INVALID)
    reason = _text(result.reason)
    suite.check("both missing fields are named",
                "explanation" in reason and "suggested_fix" in reason)


# --- anti-hallucination: the insufficient_context contract ------------------


def test_insufficient_context_recognized_by_every_validator(suite):
    payload = '{"insufficient_context": true, "reason": "no code was supplied"}'
    for validate in (validate_explanation_response, validate_summary_response,
                      validate_fix_response):
        result = validate(payload)
        suite.check("{} recognizes the decline".format(validate.__name__),
                     result.status == STATUS_INSUFFICIENT_CONTEXT)
        suite.check("{} carries the model's own reason".format(validate.__name__),
                     result.reason == "no code was supplied")


def test_insufficient_context_gets_a_default_reason_when_none_given(suite):
    result = validate_explanation_response('{"insufficient_context": true}')
    suite.check("status recognized even with no reason field",
                result.status == STATUS_INSUFFICIENT_CONTEXT)
    suite.check("a sensible default reason is used, not a blank one", bool(result.reason))


def test_insufficient_context_false_is_not_treated_as_a_decline(suite):
    """A literal `"insufficient_context": false` must be ignored, not
    misread as a decline - only `true` is the sanctioned signal.
    """
    result = validate_explanation_response(
        '{"insufficient_context": false, "explanation": "real answer"}'
    )
    suite.check("treated as a normal successful response", result.status == STATUS_SUCCESS)
    suite.check("the real explanation is used", _explanation(result).explanation == "real answer")


def test_insufficient_context_never_fabricates_a_value(suite):
    result = validate_summary_response('{"insufficient_context": true, "reason": "x"}')
    suite.check("no schema value is produced for a decline", result.value is None)


if __name__ == "__main__":
    suite = Suite("AI prompt architecture & context extraction (Phase D Part 2)")
    sys.exit(suite.run([
        test_extract_context_basic_window,
        test_extract_context_clips_at_start_of_file,
        test_extract_context_clips_at_end_of_file,
        test_extract_context_missing_file,
        test_extract_context_line_beyond_end_of_file,
        test_extract_context_rejects_a_non_positive_line_number,
        test_extract_context_respects_max_lines_cap,
        test_extract_context_respects_max_chars_cap,
        test_extract_context_never_drops_the_target_line_even_when_it_alone_is_huge,
        test_extract_context_on_a_large_file_stays_fast,
        test_build_explanation_prompt_includes_finding_fields,
        test_build_explanation_prompt_includes_code_context_and_marks_target_line,
        test_build_explanation_prompt_handles_unavailable_context_gracefully,
        test_build_fix_prompt_asks_for_explanation_and_suggested_fix,
        test_build_summary_prompt_lists_findings,
        test_build_summary_prompt_handles_zero_findings,
        test_build_summary_prompt_caps_long_finding_lists,
        test_all_builders_include_the_shared_guardrails,
        test_all_builders_separate_system_and_user_text,
        test_all_builders_instruct_json_only_response,
        test_prompt_builders_never_import_the_provider_layer,
        test_context_module_has_no_qa_agent_imports,
        test_strip_markdown_fence_json_tagged,
        test_strip_markdown_fence_bare_fence,
        test_strip_markdown_fence_no_fence_present,
        test_strip_markdown_fence_with_surrounding_prose,
        test_parse_json_response_valid,
        test_parse_json_response_malformed,
        test_parse_json_response_empty_string,
        test_validate_explanation_response_success,
        test_validate_explanation_response_accepts_a_markdown_fence,
        test_validate_explanation_response_missing_field,
        test_validate_explanation_response_wrong_field_type,
        test_validate_explanation_response_blank_field,
        test_validate_explanation_response_malformed_json,
        test_validate_explanation_response_not_a_json_object,
        test_validate_summary_response_success,
        test_validate_summary_response_missing_field,
        test_validate_fix_response_success,
        test_validate_fix_response_missing_one_field,
        test_validate_fix_response_missing_both_fields,
        test_insufficient_context_recognized_by_every_validator,
        test_insufficient_context_gets_a_default_reason_when_none_given,
        test_insufficient_context_false_is_not_treated_as_a_decline,
        test_insufficient_context_never_fabricates_a_value,
    ]))
