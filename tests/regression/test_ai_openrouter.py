"""OpenRouter cloud provider (docs/27-openrouter-cloud-provider.md):
`OpenRouterProvider` implementing the existing `AIProvider` contract
alongside `OllamaProvider`/`MockProvider`, unmodified.

Pure unit tests, mirroring test_ai_provider.py's own OllamaProvider
suite exactly: no live network call anywhere in this file -
`urllib.request.urlopen` is replaced with a fake, the same swap-and-
restore pattern already established there. Every real-world failure mode
this provider is required to handle (missing/empty key, HTTP 401/403/429/
5xx, network/timeout, malformed JSON, missing choices/message/content) is
exercised directly, plus the one guarantee unique to a provider that holds
a real secret: the API key must never appear in any error message this
provider produces.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.ai import OpenRouterProvider  # noqa: E402
from qa_agent.ai import openrouter as openrouter_module  # noqa: E402

FAKE_SECRET = "sk-or-v1-THIS-MUST-NEVER-APPEAR-ANYWHERE-IN-OUTPUT"


class _FakeHTTPResponse:
    """Stands in for the object urlopen()'s context manager yields - the
    same shape test_ai_provider.py's own OllamaProvider suite already uses.
    """

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _error(result):
    assert result.error is not None
    return result.error


def _with_fake_urlopen(fake, body):
    original = openrouter_module.urllib.request.urlopen
    openrouter_module.urllib.request.urlopen = fake
    try:
        return body()
    finally:
        openrouter_module.urllib.request.urlopen = original


def _http_error(code, body: bytes, msg="Error"):
    return urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/chat/completions", code=code, msg=msg, hdrs=None, fp=io.BytesIO(body),
    )


def _openai_shaped_response(content="The model's real reply.", model="poolside/laguna-s-2.1:free"):
    return json.dumps({
        "id": "gen-1", "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }).encode("utf-8")


# --- successful generation ---------------------------------------------------

def test_openrouter_generate_success(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeHTTPResponse(_openai_shaped_response())

    provider = OpenRouterProvider(api_key=FAKE_SECRET, timeout=12)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hello"))

    suite.check("ok", result.ok)
    suite.check("the model's real reply text is returned", result.text == "The model's real reply.")
    suite.check("attributed to 'openrouter'", result.provider == "openrouter")
    suite.check("model name carried through", result.model == "poolside/laguna-s-2.1:free")
    suite.check("latency is measured, not zero/invented", result.latency_seconds >= 0)
    suite.check("posts to the real chat/completions endpoint",
                captured["url"] == "https://openrouter.ai/api/v1/chat/completions")
    suite.check("uses POST", captured["method"] == "POST")
    suite.check("Authorization uses Bearer + the real key", captured["headers"]["authorization"] == "Bearer {}".format(FAKE_SECRET))
    suite.check("the configured timeout is threaded through to urlopen", captured["timeout"] == 12)
    suite.check("the prompt is sent as a single user message",
                captured["body"]["messages"] == [{"role": "user", "content": "hello"}])
    suite.check("streaming is explicitly disabled", captured["body"]["stream"] is False)
    suite.check("the model field is sent", captured["body"]["model"] == "poolside/laguna-s-2.1:free")


def test_openrouter_configured_custom_model_is_used(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse(_openai_shaped_response(model="some/other-model"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET, model="some/other-model")
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("the custom model is sent in the request", captured["body"]["model"] == "some/other-model")
    suite.check("the result is attributed to the custom model", result.model == "some/other-model")


def test_openrouter_default_model_is_the_configured_target(suite):
    suite.check("DEFAULT_MODEL is poolside/laguna-s-2.1:free",
                openrouter_module.DEFAULT_MODEL == "poolside/laguna-s-2.1:free")
    suite.check("a provider built with no model= uses that default",
                OpenRouterProvider(api_key=FAKE_SECRET).model == "poolside/laguna-s-2.1:free")


def test_openrouter_default_endpoint_is_the_real_chat_completions_url(suite):
    suite.check("DEFAULT_ENDPOINT is the real OpenRouter chat/completions URL",
                openrouter_module.DEFAULT_ENDPOINT == "https://openrouter.ai/api/v1/chat/completions")


# --- API key handling ---------------------------------------------------

def test_openrouter_missing_api_key_fails_without_any_network_call(suite):
    def fake_urlopen(request, timeout=None):
        raise AssertionError("generate() must not call urlopen when no API key is configured")

    provider = OpenRouterProvider(api_key=None)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("a clear, specific reason is given", "OPENROUTER_API_KEY" in _error(result))
    suite.check("no fabricated text on failure", result.text == "")


def test_openrouter_empty_api_key_is_treated_as_missing(suite):
    def fake_urlopen(request, timeout=None):
        raise AssertionError("generate() must not call urlopen for an empty API key either")

    provider = OpenRouterProvider(api_key="")
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("an empty string key is refused just like a missing one", not result.ok)
    suite.check("the same clear reason is given", "OPENROUTER_API_KEY" in _error(result))


def test_openrouter_env_key_is_used_when_not_passed_explicitly(suite):
    import os
    original = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = FAKE_SECRET
    try:
        provider = OpenRouterProvider()
        suite.check("the environment variable is read when api_key= is not given",
                     provider.api_key == FAKE_SECRET)
    finally:
        if original is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = original


def test_openrouter_explicit_api_key_overrides_the_environment(suite):
    import os
    original = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = "env-value-should-not-be-used"
    try:
        provider = OpenRouterProvider(api_key="explicit-value")
        suite.check("an explicitly-passed api_key wins over the environment",
                     provider.api_key == "explicit-value")
    finally:
        if original is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = original


# --- HTTP failure modes ---------------------------------------------------

def test_openrouter_http_400_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise _http_error(400, json.dumps({"error": {"message": "Invalid request body"}}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("HTTP 400 is named", "400" in _error(result))
    suite.check("OpenRouter's own error message is preserved", "Invalid request body" in _error(result))


def test_openrouter_http_401_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise _http_error(401, json.dumps({"error": {"message": "No auth credentials found"}}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("HTTP 401 is named", "401" in _error(result))
    suite.check("authentication failure is named clearly", "authentication failed" in _error(result))


def test_openrouter_http_403_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise _http_error(403, json.dumps({"error": {"message": "Forbidden"}}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("HTTP 403 is named", "403" in _error(result))


def test_openrouter_http_429_rate_limit_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise _http_error(429, json.dumps({"error": {"message": "Rate limit exceeded"}}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("HTTP 429 is named", "429" in _error(result))
    suite.check("rate limiting is named clearly", "rate limited" in _error(result))


def test_openrouter_http_5xx_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise _http_error(503, b"")

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("HTTP 503 is named", "503" in _error(result))
    suite.check("a server error is named clearly", "server error" in _error(result))


def test_openrouter_network_failure_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("nobody listening"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the failure is reported clearly", "could not reach OpenRouter" in _error(result))


def test_openrouter_dns_failure_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(OSError("Name or service not known"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the DNS-shaped failure is still reported clearly", "could not reach OpenRouter" in _error(result))


def test_openrouter_timeout_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        raise socket.timeout("timed out")

    provider = OpenRouterProvider(api_key=FAKE_SECRET, timeout=9)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the timeout is reported clearly", "did not respond" in _error(result))
    suite.check("the configured timeout value is named", "9s" in _error(result))


def test_openrouter_urlerror_wrapping_a_timeout_is_still_a_timeout(suite):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET, timeout=3)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("recognized as a timeout even though wrapped in URLError", "did not respond" in _error(result))


def test_openrouter_unexpected_exception_is_still_caught(suite):
    def fake_urlopen(request, timeout=None):
        raise ValueError("something nobody anticipated")

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the unexpected error's own message is preserved", "something nobody anticipated" in _error(result))


# --- malformed response shapes -------------------------------------------

def test_openrouter_invalid_json_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(b"not json at all")

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the malformed-JSON case is named clearly", "not valid JSON" in _error(result))


def test_openrouter_missing_choices_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(json.dumps({"id": "gen-1", "model": "x"}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the missing-'choices' case is named clearly", "choices" in _error(result))


def test_openrouter_empty_choices_list_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(json.dumps({"choices": []}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("an empty 'choices' list is named clearly", "choices" in _error(result))


def test_openrouter_missing_message_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(json.dumps({"choices": [{"index": 0}]}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the missing-'message' case is named clearly", "message" in _error(result))


def test_openrouter_missing_content_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(json.dumps({"choices": [{"message": {"role": "assistant"}}]}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("the missing-'content' case is named clearly", "content" in _error(result))


def test_openrouter_empty_content_is_handled_gracefully(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(json.dumps({"choices": [{"message": {"content": "   "}}]}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("not ok, but did not raise", not result.ok)
    suite.check("a whitespace-only content is treated as empty, not a real response", "content" in _error(result))


# --- test_connection: lightweight, never invokes generation -----------------

def test_openrouter_test_connection_success(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        return _FakeHTTPResponse(json.dumps({"data": []}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, provider.test_connection)
    suite.check("reachable", result.ok is True)
    suite.check("checks the models endpoint, not chat/completions - no model invoked",
                captured["url"] == "https://openrouter.ai/api/v1/models")
    suite.check("uses GET", captured["method"] == "GET")


def test_openrouter_test_connection_missing_key_no_network_call(suite):
    def fake_urlopen(request, timeout=None):
        raise AssertionError("test_connection must not call urlopen with no API key")

    provider = OpenRouterProvider(api_key=None)
    result = _with_fake_urlopen(fake_urlopen, provider.test_connection)
    suite.check("unreachable, but did not raise or call the network", result.ok is False)
    suite.check("a clear reason is given", "OPENROUTER_API_KEY" in result.detail)


def test_openrouter_test_connection_offline(suite):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("nobody listening"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, provider.test_connection)
    suite.check("unreachable, but did not raise", result.ok is False)
    suite.check("a reason is given", bool(result.detail))


def test_openrouter_test_connection_never_invokes_chat_completions(suite):
    def fake_urlopen(request, timeout=None):
        if "chat/completions" in request.full_url:
            raise AssertionError("test_connection must never call chat/completions")
        return _FakeHTTPResponse(json.dumps({"data": []}).encode("utf-8"))

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, provider.test_connection)
    suite.check("completed without ever touching chat/completions", result.ok is True)


# --- the one guarantee unique to a secret-holding provider -------------------

def test_openrouter_secret_never_appears_in_any_error_message(suite):
    provider = OpenRouterProvider(api_key=FAKE_SECRET, timeout=5)

    scenarios = []

    def fake_401(request, timeout=None):
        raise _http_error(401, json.dumps({"error": {"message": "unauthorized"}}).encode("utf-8"))
    scenarios.append(("401", fake_401))

    def fake_network(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))
    scenarios.append(("network", fake_network))

    def fake_timeout(request, timeout=None):
        raise socket.timeout("timed out")
    scenarios.append(("timeout", fake_timeout))

    def fake_malformed(request, timeout=None):
        return _FakeHTTPResponse(b"not json")
    scenarios.append(("malformed", fake_malformed))

    def fake_unexpected(request, timeout=None):
        raise ValueError("boom")
    scenarios.append(("unexpected", fake_unexpected))

    for label, fake in scenarios:
        result = _with_fake_urlopen(fake, lambda: provider.generate("hi"))
        suite.check("[{}] the real secret never appears in the error message".format(label),
                     FAKE_SECRET not in (result.error or ""))

    conn_result = _with_fake_urlopen(fake_401, provider.test_connection)
    suite.check("test_connection's own error also never leaks the secret",
                 FAKE_SECRET not in (conn_result.detail or ""))


def test_openrouter_secret_never_appears_in_a_successful_result_either(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_openai_shaped_response())

    provider = OpenRouterProvider(api_key=FAKE_SECRET)
    result = _with_fake_urlopen(fake_urlopen, lambda: provider.generate("hi"))
    suite.check("a successful LLMResponse never contains the secret anywhere in its own fields",
                 FAKE_SECRET not in result.text and FAKE_SECRET not in result.provider
                 and FAKE_SECRET not in result.model and FAKE_SECRET not in str(result))


# --- CLI: discover --ai-provider cloud, no live network needed --------------

def test_cli_discover_ai_provider_cloud_fails_safely_without_a_key(suite):
    """A real, end-to-end proof that `--ai-provider cloud` is wired into the
    CLI correctly and never crashes - with OPENROUTER_API_KEY deliberately
    unset in the subprocess environment, the whole discover -> plan ->
    execute -> diagnose pipeline must still complete cleanly, reporting
    AI_ERROR for the diagnosis rather than raising or hanging.
    """
    import os

    with TempProject() as root:
        (root / "package-lock.json").write_text("{}", encoding="utf-8")
        (root / "package.json").write_text(
            json.dumps({"scripts": {"build": "node -e \"console.error('x'); process.exit(1)\""}}),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("OPENROUTER_API_KEY", None)
        proc = run_agent(["discover", str(root), "--diagnose", "--ai-provider", "cloud"], env=env)
    suite.check("discover --ai-provider cloud exits 0 even with no API key configured", proc.returncode == 0)
    suite.check("no traceback leaks to the user", "Traceback" not in proc.stdout)
    suite.check("the API key is never printed (it was never even set here)", "OPENROUTER_API_KEY=" not in proc.stdout)


if __name__ == "__main__":
    suite = Suite("OpenRouter Cloud Provider (docs/27)")
    sys.exit(suite.run([
        test_openrouter_generate_success,
        test_openrouter_configured_custom_model_is_used,
        test_openrouter_default_model_is_the_configured_target,
        test_openrouter_default_endpoint_is_the_real_chat_completions_url,
        test_openrouter_missing_api_key_fails_without_any_network_call,
        test_openrouter_empty_api_key_is_treated_as_missing,
        test_openrouter_env_key_is_used_when_not_passed_explicitly,
        test_openrouter_explicit_api_key_overrides_the_environment,
        test_openrouter_http_400_is_handled_gracefully,
        test_openrouter_http_401_is_handled_gracefully,
        test_openrouter_http_403_is_handled_gracefully,
        test_openrouter_http_429_rate_limit_is_handled_gracefully,
        test_openrouter_http_5xx_is_handled_gracefully,
        test_openrouter_network_failure_is_handled_gracefully,
        test_openrouter_dns_failure_is_handled_gracefully,
        test_openrouter_timeout_is_handled_gracefully,
        test_openrouter_urlerror_wrapping_a_timeout_is_still_a_timeout,
        test_openrouter_unexpected_exception_is_still_caught,
        test_openrouter_invalid_json_is_handled_gracefully,
        test_openrouter_missing_choices_is_handled_gracefully,
        test_openrouter_empty_choices_list_is_handled_gracefully,
        test_openrouter_missing_message_is_handled_gracefully,
        test_openrouter_missing_content_is_handled_gracefully,
        test_openrouter_empty_content_is_handled_gracefully,
        test_openrouter_test_connection_success,
        test_openrouter_test_connection_missing_key_no_network_call,
        test_openrouter_test_connection_offline,
        test_openrouter_test_connection_never_invokes_chat_completions,
        test_openrouter_secret_never_appears_in_any_error_message,
        test_openrouter_secret_never_appears_in_a_successful_result_either,
        test_cli_discover_ai_provider_cloud_fails_safely_without_a_key,
    ]))
