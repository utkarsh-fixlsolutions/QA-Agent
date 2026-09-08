"""AI provider foundation (Phase D Part 1) - the provider protocol, the Mock
and Ollama providers, and graceful handling of every real-world failure mode:
offline, timeout, connection failure, malformed response.

Pure unit tests: no live Ollama process anywhere in this file, matching
test_multi_analyzer.py's own "real captured shapes, mocked transport" style -
`urllib.request.urlopen` is replaced with a fake exactly the way
runner.subprocess.run is replaced elsewhere in this suite.

Entirely independent of the deterministic pipeline: nothing here imports
runner.py, adapters.py, report.py, config.py, or analysis_bridge.py, and
nothing there imports qa_agent.ai - see test_ai_isolation below for the
proof.
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite  # noqa: E402

from qa_agent.ai import AIProvider, ConnectionResult, LLMResponse, MockProvider, OllamaProvider  # noqa: E402
from qa_agent.ai import ollama as ollama_module  # noqa: E402

# --- LLMResponse / ConnectionResult: the shared result shapes --------------


def test_llm_response_ok_means_no_error(suite):
    success = LLMResponse(text="hi")
    failure = LLMResponse(error="boom")
    suite.check("a response with no error is ok", success.ok is True)
    suite.check("a response with an error is not ok", failure.ok is False)


def test_llm_response_defaults_are_a_clean_empty_success(suite):
    """Constructing one with no arguments must not accidentally look like a
    failure - error=None is the default, matching every ToolError-free
    result already used throughout this project.
    """
    response = LLMResponse()
    suite.check("default is ok (error defaults to None)", response.ok is True)
    suite.check("default text is empty, not invented", response.text == "")


def test_connection_result_is_a_plain_ok_plus_detail(suite):
    result = ConnectionResult(ok=False, detail="offline")
    suite.check("ok is whatever was given", result.ok is False)
    suite.check("detail carries the reason", result.detail == "offline")


# --- the protocol: both real providers satisfy it, structurally ------------


def test_both_providers_satisfy_the_ai_provider_protocol(suite):
    suite.check("MockProvider satisfies AIProvider", isinstance(MockProvider(), AIProvider))
    suite.check("OllamaProvider satisfies AIProvider", isinstance(OllamaProvider(), AIProvider))


def test_providers_declare_their_own_name(suite):
    suite.check("mock's name is 'mock'", MockProvider().name == "mock")
    suite.check("ollama's name is 'ollama'", OllamaProvider().name == "ollama")


# --- MockProvider: deterministic, no network, exactly what it was told -----


def test_mock_provider_returns_its_configured_success(suite):
    provider = MockProvider(model="m", response_text="configured text")
    result = provider.generate("any prompt")
    suite.check("ok", result.ok)
    suite.check("returns exactly the configured text", result.text == "configured text")
    suite.check("attributed to 'mock'", result.provider == "mock")
    suite.check("model is what was configured", result.model == "m")


def test_mock_provider_is_deterministic_across_calls(suite):
    provider = MockProvider(response_text="always the same")
    first = provider.generate("prompt one")
    second = provider.generate("a completely different prompt")
    suite.check("the same fixed response regardless of prompt content",
                first.text == second.text == "always the same")


def test_mock_provider_can_be_configured_to_fail(suite):
    provider = MockProvider(fail=True, failure_message="simulated failure")
    result = provider.generate("prompt")
    suite.check("not ok", not result.ok)
    suite.check("the configured failure message is used", result.error == "simulated failure")
    suite.check("no text on a failure", result.text == "")


def test_mock_provider_test_connection(suite):
    ok_provider = MockProvider()
    failing_provider = MockProvider(fail=True, failure_message="down")
    suite.check("a normally-configured mock reports reachable",
                ok_provider.test_connection().ok is True)
    connection = failing_provider.test_connection()
    suite.check("a fail-configured mock reports unreachable", connection.ok is False)
    suite.check("...with the configured reason", connection.detail == "down")


# --- OllamaProvider: real captured shapes, mocked transport ----------------


class _FakeHTTPResponse:
    """Stands in for the object urlopen()'s context manager yields."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _error(result):
    """Narrows LLMResponse.error from str | None to str for a test that
    already knows, via `not result.ok`, that it must be set - an assertion,
    not a silent fallback, so a genuine None-on-failure would fail loudly
    rather than being masked.
    """
    assert result.error is not None
    return result.error


def _with_fake_urlopen(fake, body):
    """Run body() with urllib.request.urlopen replaced, restoring it after -
    the same swap-and-restore pattern runner.subprocess.run uses elsewhere
    in this suite.
    """
    original = ollama_module.urllib.request.urlopen
    ollama_module.urllib.request.urlopen = fake
    try:
        return body()
    finally:
        ollama_module.urllib.request.urlopen = original


# Real captured shape: `ollama run llama3` via /api/generate, non-streaming.
OLLAMA_REAL_RESPONSE = json.dumps({
    "model": "llama3",
    "created_at": "2026-09-07T12:00:00Z",
    "response": "This is the model's real reply.",
    "done": True,
}).encode("utf-8")


def test_ollama_generate_success(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeHTTPResponse(OLLAMA_REAL_RESPONSE)

    provider = OllamaProvider(endpoint="http://localhost:11434", model="llama3", timeout=5)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))

    suite.check("ok", result.ok)
    suite.check("the model's real reply text is returned",
                result.text == "This is the model's real reply.")
    suite.check("attributed to 'ollama'", result.provider == "ollama")
    suite.check("model name carried through", result.model == "llama3")
    suite.check("latency is measured, not zero/invented", result.latency_seconds >= 0)
    suite.check("posts to /api/generate", captured["url"] == "http://localhost:11434/api/generate")
    suite.check("uses POST", captured["method"] == "POST")
    suite.check("the prompt is sent verbatim", captured["body"]["prompt"] == "hello")
    suite.check("streaming is explicitly disabled (out of scope this part)",
                captured["body"]["stream"] is False)
    suite.check("the configured timeout is threaded through to urlopen",
                captured["timeout"] == 5)


def test_ollama_endpoint_trailing_slash_is_normalized(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeHTTPResponse(OLLAMA_REAL_RESPONSE)

    provider = OllamaProvider(endpoint="http://localhost:11434/")
    _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("no double slash even when the endpoint has a trailing one",
                captured["url"] == "http://localhost:11434/api/generate")


def test_ollama_offline_is_handled_gracefully(suite):
    """The exact scenario this part requires: an offline provider must never
    raise out of generate() - it must come back as a failed LLMResponse.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("nobody listening"))

    provider = OllamaProvider(endpoint="http://localhost:11434")
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))

    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the endpoint is named in the error", "11434" in _error(result))
    suite.check("no fabricated text on a failure", result.text == "")
    suite.check("still attributed to 'ollama' even on failure", result.provider == "ollama")


def test_ollama_timeout_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise socket.timeout("timed out")

    provider = OllamaProvider(endpoint="http://localhost:11434", timeout=7)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))

    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the timeout is reported clearly", "did not respond" in _error(result))
    suite.check("the configured timeout value is named", "7s" in _error(result))


def test_ollama_urlerror_wrapping_a_timeout_is_still_a_timeout(suite):
    """Verified directly against a real urllib call (docs/step-log.md, Phase
    D Part 1): urlopen can wrap a timeout inside URLError rather than
    raising socket.timeout/TimeoutError directly - both shapes must be
    recognized as a timeout, not a generic connection failure.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    provider = OllamaProvider(timeout=3)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("recognized as a timeout even though wrapped in URLError",
                "did not respond" in _error(result))


def test_ollama_connection_failure_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise ConnectionResetError("connection reset")

    provider = OllamaProvider()
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the failure is reported clearly", "connection" in _error(result).lower())


def test_ollama_malformed_json_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(b"not json at all")

    provider = OllamaProvider()
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the malformed-JSON case is named clearly", "not valid JSON" in _error(result))


def test_ollama_missing_response_field_is_handled_gracefully(suite):
    """Valid JSON, wrong shape - a different kind of malformed response than
    invalid JSON, and one a naive .loads()-then-trust would miss.
    """
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(json.dumps({"done": True}).encode("utf-8"))

    provider = OllamaProvider()
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the missing-field case is named clearly", "missing" in _error(result))


def test_ollama_unexpected_exception_is_still_caught(suite):
    """The last line of defence (matching analysis_bridge.py's own pattern):
    even a completely unforeseen exception must not escape generate().
    """
    def fake_urlopen(request, timeout=None):
        raise ValueError("something nobody anticipated")

    provider = OllamaProvider()
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the unexpected error's own message is preserved",
                "something nobody anticipated" in _error(result))


def test_ollama_test_connection_success(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        return _FakeHTTPResponse(json.dumps({"models": []}).encode("utf-8"))

    provider = OllamaProvider(endpoint="http://localhost:11434")
    result = _with_fake_urlopen(fake_urlopen, provider.test_connection)

    suite.check("reachable", result.ok is True)
    suite.check("checks /api/tags, not /api/generate - no model invoked",
                captured["url"] == "http://localhost:11434/api/tags")
    suite.check("uses GET", captured["method"] == "GET")


def test_ollama_test_connection_offline(suite):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("nobody listening"))

    provider = OllamaProvider()
    result = _with_fake_urlopen(fake_urlopen, provider.test_connection)
    suite.check("unreachable, but did not raise", result.ok is False)
    suite.check("a reason is given", bool(result.detail))


def test_ollama_test_connection_never_invokes_generate(suite):
    """The literal proof of "lightweight" - test_connection must never hit
    /api/generate even by accident.
    """
    def fake_urlopen(request, timeout=None):
        if "generate" in request.full_url:
            raise AssertionError("test_connection must never call /api/generate")
        return _FakeHTTPResponse(json.dumps({"models": []}).encode("utf-8"))

    provider = OllamaProvider()
    result = _with_fake_urlopen(fake_urlopen, provider.test_connection)
    suite.check("completed without ever touching /api/generate", result.ok is True)


# --- isolation from the deterministic pipeline ------------------------------


def test_ai_package_is_not_imported_by_the_deterministic_pipeline(suite):
    """The architectural rule this whole part exists to satisfy: the
    analyzer engine must never depend on AI. Checked directly against the
    real source, not just asserted in a docstring.

    `__main__.py` is deliberately excluded from this list as of Phase D
    Part 6: its entire job from that part on is to be the one composition
    root that imports the AI package and wires it into the CLI and watch
    mode - the isolation rule was always about the *engine*, one-directional
    (engine never depends on AI), never about no code anywhere ever
    importing it. Every module actually in the deterministic pipeline -
    dispatch, adapters, config parsing, reporting, watch-mode plumbing -
    stays checked here and still must not import it.
    """
    pipeline_modules = [
        "runner.py", "adapters.py", "report.py", "config.py",
        "analysis_bridge.py", "watch.py", "debouncer.py",
        "fsmonitor.py", "live_report.py", "gitdiff.py",
    ]
    offenders = []
    for name in pipeline_modules:
        source = (REPO_ROOT / "qa_agent" / name).read_text(encoding="utf-8")
        if "qa_agent.ai" in source or "from .ai" in source or "from . import ai" in source:
            offenders.append(name)
    suite.check("no deterministic-pipeline module imports qa_agent.ai",
                not offenders, "  [{}]".format(offenders))


if __name__ == "__main__":
    suite = Suite("AI provider foundation: protocol, Mock, and Ollama (Phase D Part 1)")
    sys.exit(suite.run([
        test_llm_response_ok_means_no_error,
        test_llm_response_defaults_are_a_clean_empty_success,
        test_connection_result_is_a_plain_ok_plus_detail,
        test_both_providers_satisfy_the_ai_provider_protocol,
        test_providers_declare_their_own_name,
        test_mock_provider_returns_its_configured_success,
        test_mock_provider_is_deterministic_across_calls,
        test_mock_provider_can_be_configured_to_fail,
        test_mock_provider_test_connection,
        test_ollama_generate_success,
        test_ollama_endpoint_trailing_slash_is_normalized,
        test_ollama_offline_is_handled_gracefully,
        test_ollama_timeout_is_handled_gracefully,
        test_ollama_urlerror_wrapping_a_timeout_is_still_a_timeout,
        test_ollama_connection_failure_is_handled_gracefully,
        test_ollama_malformed_json_is_handled_gracefully,
        test_ollama_missing_response_field_is_handled_gracefully,
        test_ollama_unexpected_exception_is_still_caught,
        test_ollama_test_connection_success,
        test_ollama_test_connection_offline,
        test_ollama_test_connection_never_invokes_generate,
        test_ai_package_is_not_imported_by_the_deterministic_pipeline,
    ]))
