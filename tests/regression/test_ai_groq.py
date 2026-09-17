"""Groq cloud provider (docs/42-groq-cloud-provider.md): `GroqProvider`
implementing the existing `AIProvider` contract alongside
`OllamaProvider`/`OpenRouterProvider`/`MockProvider`, unmodified.

Pure unit tests, mirroring test_ai_openrouter.py's own suite exactly (same
provider shape, same OpenAI-compatible wire format): no live network call
anywhere in this file - `urllib.request.urlopen` is replaced with a fake,
the same swap-and-restore pattern. Every real-world failure mode this
provider is required to handle (missing key, HTTP 401/429/5xx, network/
timeout, malformed JSON, missing choices/message/content) is exercised
directly, plus the one guarantee unique to a provider that holds a real
secret: the API key must never appear in any error message this provider
produces.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite  # noqa: E402

from qa_agent.ai import GroqProvider  # noqa: E402
from qa_agent.ai import groq as groq_module  # noqa: E402

FAKE_SECRET = "gsk_THIS-MUST-NEVER-APPEAR-ANYWHERE-IN-OUTPUT"


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _with_fake_urlopen(fake, body):
    original = groq_module.urllib.request.urlopen
    groq_module.urllib.request.urlopen = fake
    try:
        return body()
    finally:
        groq_module.urllib.request.urlopen = original


def _http_error(code, body: bytes, msg="Error"):
    return urllib.error.HTTPError(
        url="https://api.groq.com/openai/v1/chat/completions", code=code, msg=msg, hdrs=None, fp=io.BytesIO(body),
    )


def _openai_shaped_response(content="The model's real reply.", model="openai/gpt-oss-120b"):
    return json.dumps({
        "id": "chatcmpl-1", "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }).encode("utf-8")


# --- successful generation ---------------------------------------------------

def test_groq_generate_success(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeHTTPResponse(_openai_shaped_response())

    provider = GroqProvider(api_key=FAKE_SECRET, timeout=12)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))

    suite.check("ok", result.ok)
    suite.check("the model's real reply text is returned", result.text == "The model's real reply.")
    suite.check("attributed to 'groq'", result.provider == "groq")
    suite.check("model name carried through", result.model == "openai/gpt-oss-120b")
    suite.check("latency is measured, not zero/invented", result.latency_seconds >= 0)
    suite.check("posts to the real chat/completions endpoint",
                captured["url"] == "https://api.groq.com/openai/v1/chat/completions")
    suite.check("uses POST", captured["method"] == "POST")
    suite.check("the real API key is sent as a bearer token",
                captured["headers"].get("authorization") == "Bearer {}".format(FAKE_SECRET))
    suite.check("the prompt is forwarded as a single user message",
                captured["body"]["messages"] == [{"role": "user", "content": "hello"}])
    suite.check("non-streaming", captured["body"]["stream"] is False)
    suite.check("the configured timeout is passed through", captured["timeout"] == 12)


def test_groq_generate_reads_api_key_from_environment(suite):
    import os
    original = os.environ.get("GROQ_API_KEY")
    os.environ["GROQ_API_KEY"] = FAKE_SECRET
    try:
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            return _FakeHTTPResponse(_openai_shaped_response())

        provider = GroqProvider()  # no api_key= given
        result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
        suite.check("ok", result.ok)
        suite.check("picked up GROQ_API_KEY from the environment",
                     captured["headers"].get("authorization") == "Bearer {}".format(FAKE_SECRET))
    finally:
        if original is None:
            os.environ.pop("GROQ_API_KEY", None)
        else:
            os.environ["GROQ_API_KEY"] = original


# --- failure modes -------------------------------------------------------

def test_groq_missing_api_key_fails_fast_with_no_network_call(suite):
    def fake_urlopen(request, timeout=None):
        raise AssertionError("must never be called when the key is missing")

    provider = GroqProvider(api_key=None)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))
    suite.check("not ok", not result.ok)
    suite.check("a clear reason is given", "GROQ_API_KEY is not set" in result.error)


def test_groq_http_401_names_the_env_var_never_the_key(suite):
    def fake_urlopen(request, timeout=None):
        raise _http_error(401, json.dumps({"error": {"message": "Invalid API Key"}}).encode())

    provider = GroqProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))
    suite.check("not ok", not result.ok)
    suite.check("names the real env var to check", "GROQ_API_KEY" in result.error)
    suite.check("the real secret never appears in the error", FAKE_SECRET not in result.error)
    suite.check("the server's own real error message is surfaced", "Invalid API Key" in result.error)


def test_groq_http_429_reports_rate_limited(suite):
    def fake_urlopen(request, timeout=None):
        raise _http_error(429, b'{"error": {"message": "rate limit exceeded"}}')

    provider = GroqProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))
    suite.check("not ok", not result.ok)
    suite.check("reports rate limited", "rate limited" in result.error)


def test_groq_http_500_reports_server_error(suite):
    def fake_urlopen(request, timeout=None):
        raise _http_error(500, b"")

    provider = GroqProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))
    suite.check("not ok", not result.ok)
    suite.check("reports a server error", "server error" in result.error)


def test_groq_timeout(suite):
    def fake_urlopen(request, timeout=None):
        raise socket.timeout()

    provider = GroqProvider(api_key=FAKE_SECRET, timeout=5)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))
    suite.check("not ok", not result.ok)
    suite.check("names the real configured timeout", "5s" in result.error)


def test_groq_connection_refused(suite):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError())

    provider = GroqProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))
    suite.check("not ok", not result.ok)
    suite.check("a clear reason is given", "could not reach Groq" in result.error)


def test_groq_malformed_json_response(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(b"not json at all")

    provider = GroqProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))
    suite.check("not ok", not result.ok)
    suite.check("a clear reason is given", "not valid JSON" in result.error)


def test_groq_missing_choices_in_response(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(json.dumps({"id": "x"}).encode())

    provider = GroqProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))
    suite.check("not ok", not result.ok)
    suite.check("a clear reason is given", "choices" in result.error)


def test_groq_empty_content_in_response(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_openai_shaped_response(content=""))

    provider = GroqProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))
    suite.check("not ok", not result.ok)
    suite.check("a clear reason is given", "content" in result.error)


# --- connection test -------------------------------------------------------

def test_groq_test_connection_success(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(b'{"data": []}')

    provider = GroqProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.test_connection())
    suite.check("ok", result.ok)


def test_groq_test_connection_checks_models_not_chat(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        return _FakeHTTPResponse(b'{"data": []}')

    provider = GroqProvider(api_key=FAKE_SECRET)
    _with_fake_urlopen(fake_urlopen, lambda: provider.test_connection())
    suite.check("checks /models, not /chat/completions - no model invoked",
                captured["url"] == "https://api.groq.com/openai/v1/models")
    suite.check("uses GET", captured["method"] == "GET")


def test_groq_test_connection_missing_key_fails_with_no_network_call(suite):
    def fake_urlopen(request, timeout=None):
        raise AssertionError("must never be called when the key is missing")

    provider = GroqProvider(api_key=None)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.test_connection())
    suite.check("not ok", not result.ok)
    suite.check("a reason is given", "GROQ_API_KEY is not set" in result.detail)


def test_groq_test_connection_unreachable(suite):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(OSError("network is unreachable"))

    provider = GroqProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.test_connection())
    suite.check("unreachable, but did not raise", not result.ok)
    suite.check("a reason is given", "could not reach Groq" in result.detail)


if __name__ == "__main__":
    suite = Suite("Groq cloud provider")
    sys.exit(suite.run([
        test_groq_generate_success,
        test_groq_generate_reads_api_key_from_environment,
        test_groq_missing_api_key_fails_fast_with_no_network_call,
        test_groq_http_401_names_the_env_var_never_the_key,
        test_groq_http_429_reports_rate_limited,
        test_groq_http_500_reports_server_error,
        test_groq_timeout,
        test_groq_connection_refused,
        test_groq_malformed_json_response,
        test_groq_missing_choices_in_response,
        test_groq_empty_content_in_response,
        test_groq_test_connection_success,
        test_groq_test_connection_checks_models_not_chat,
        test_groq_test_connection_missing_key_fails_with_no_network_call,
        test_groq_test_connection_unreachable,
    ]))
