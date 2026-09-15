"""Groq provider: an optional cloud AI backend, reached over Groq's own
OpenAI-compatible `/chat/completions` HTTP API (docs/42-groq-cloud-provider
.md) - the same wire shape OpenRouter already speaks.

Stdlib only (`urllib.request`), mirroring `openrouter.py`'s own isolation
and failure-handling shape exactly - the same "every failure mode is caught
here and returned as a failed `LLMResponse`/`ConnectionResult`, never
raised" contract, so this provider is a drop-in `AIProvider` alongside
`OllamaProvider`/`OpenRouterProvider`/`MockProvider`: G3/G4 and every other
caller in this project already only ever depend on that shape, never on
which provider produced it. AI remains entirely optional either way -
nothing in this project's deterministic core requires a cloud provider,
and this module is never imported unless `--ai-provider groq` (or
`ai.provider: "groq"` in `.qa-agent.json`) is explicitly chosen.

The API key is read only from the `GROQ_API_KEY` environment variable -
never hardcoded, never read from a project config file or a `.env` file,
never logged, and never present in any error message this module produces
(every failure message is built from the server's own response and/or the
exception text, never from `self.api_key` or the request's own headers) -
the same guarantee `openrouter.py` already makes for `OPENROUTER_API_KEY`.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request

from .. import __version__ as _QA_AGENT_VERSION
from .provider import ConnectionResult, LLMResponse

DEFAULT_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
# Verified directly against Groq's own real, currently-active model list
# (GET /openai/v1/models) - Groq's model catalog changes over time (an
# earlier, plausible-looking default, "llama-3.3-70b-versatile", was
# confirmed to no longer exist via a real HTTP 404 from the real API
# before this one was chosen and confirmed working end-to-end).
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_TIMEOUT_SECONDS = 30.0

# Groq's own models-list endpoint - used only for a lightweight
# reachability check (test_connection), never for generation.
_MODELS_ENDPOINT = "https://api.groq.com/openai/v1/models"

# TimeoutError and socket.timeout are the same class from Python 3.10
# onward, and socket.timeout is a subclass of TimeoutError before that -
# the same pair openrouter.py/ollama.py already check, for the same reason
# (this project's full 3.8+ range, not just the interpreter it's written on).
_TIMEOUT_EXCEPTIONS = (socket.timeout, TimeoutError)

# A short, bounded snippet of Groq's own error body - never the full
# response, and never the request we sent (which could contain repository
# content via the prompt).
_MAX_ERROR_BODY_CHARS = 300

_HTTP_STATUS_LABELS = {
    400: "bad request",
    401: "authentication failed (check GROQ_API_KEY)",
    403: "forbidden",
    429: "rate limited",
}


class GroqProvider:
    """Talks to Groq's `/chat/completions` endpoint. Endpoint, model,
    timeout, and the API key are all configurable at construction - the API
    key defaults to `GROQ_API_KEY` from the environment when not given
    explicitly (production code, the CLI included, never passes
    `api_key=`; only a test injects one directly, to avoid mutating
    `os.environ`).
    """

    name = "groq"

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        api_key: str | None = None,
    ):
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.api_key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY")

    def _headers(self) -> dict:
        # Groq's own edge (Cloudflare) blocks requests carrying urllib's
        # default `Python-urllib/x.y` User-Agent as a bot signature (a real
        # HTTP 403 "error code: 1010", confirmed directly against the real
        # API - not an authentication failure, easy to mistake for one). A
        # real, explicit User-Agent avoids it; OpenRouter has no equivalent
        # requirement, which is why `openrouter.py` never needed this.
        return {
            "Content-Type": "application/json", "Authorization": "Bearer {}".format(self.api_key),
            "User-Agent": "qa-agent/{}".format(_QA_AGENT_VERSION),
        }

    def generate(self, prompt: str) -> LLMResponse:
        """Send `prompt` as a single user message to `/chat/completions`
        (non-streaming) and return the model's reply. Never raises - see
        the module docstring.

        `AIProvider.generate(prompt: str)` only ever hands a provider one
        already-joined string - every caller in this project (explainer.py,
        repair.py, diagnosis.py, runtime_repair.py, ...) already merges a
        `Prompt`'s `system`/`user` text before calling a provider, via each
        module's own `_prompt_text()` helper. This is the one faithful way
        to forward that here: a single `user`-role message, never a
        reinvented system/user split this layer has no way to recover
        correctly from an already-flattened string.
        """
        start = time.perf_counter()
        if not self.api_key:
            return self._failure("GROQ_API_KEY is not set", start)

        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=body, headers=self._headers(), method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except _TIMEOUT_EXCEPTIONS:
            return self._failure("Groq did not respond within {:.0f}s".format(self.timeout), start)
        except urllib.error.HTTPError as exc:
            return self._failure(self._describe_http_error(exc), start)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, _TIMEOUT_EXCEPTIONS):
                return self._failure("Groq did not respond within {:.0f}s".format(self.timeout), start)
            return self._failure("could not reach Groq: {}".format(exc.reason), start)
        except OSError as exc:
            return self._failure("connection to Groq failed: {}".format(exc), start)
        except Exception as exc:  # noqa: BLE001 - AI must never crash the pipeline
            return self._failure("unexpected error talking to Groq: {}".format(exc), start)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._failure("malformed response (not valid JSON): {}".format(exc), start)

        if not isinstance(parsed, dict):
            return self._failure("malformed response (not a JSON object)", start)

        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            return self._failure("malformed response (missing or empty 'choices')", start)

        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict):
            return self._failure("malformed response (missing 'message')", start)

        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return self._failure("malformed response (missing or empty 'content')", start)

        return LLMResponse(
            text=content, provider=self.name, model=self.model,
            latency_seconds=time.perf_counter() - start,
        )

    def test_connection(self) -> ConnectionResult:
        """A lightweight reachability check - GET the models list, never
        invokes generation. Fails fast, with no network call at all, when
        the API key is simply not set - matching `generate()`'s own
        fast-fail for the same case.
        """
        if not self.api_key:
            return ConnectionResult(ok=False, detail="GROQ_API_KEY is not set")
        request = urllib.request.Request(
            _MODELS_ENDPOINT, headers=self._headers(), method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except _TIMEOUT_EXCEPTIONS:
            return ConnectionResult(ok=False, detail="Groq did not respond within {:.0f}s".format(self.timeout))
        except urllib.error.HTTPError as exc:
            return ConnectionResult(ok=False, detail=self._describe_http_error(exc))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, _TIMEOUT_EXCEPTIONS):
                return ConnectionResult(
                    ok=False, detail="Groq did not respond within {:.0f}s".format(self.timeout)
                )
            return ConnectionResult(ok=False, detail="could not reach Groq: {}".format(exc.reason))
        except OSError as exc:
            return ConnectionResult(ok=False, detail="connection to Groq failed: {}".format(exc))
        except Exception as exc:  # noqa: BLE001 - AI must never crash the pipeline
            return ConnectionResult(ok=False, detail="unexpected error reaching Groq: {}".format(exc))
        return ConnectionResult(ok=True, detail="Groq is reachable")

    def _describe_http_error(self, exc: urllib.error.HTTPError) -> str:
        """A clear, bounded error message built only from the HTTP status
        and Groq's own JSON error body (`{"error": {"message": ...}}` when
        present) - never from the request we sent or its headers, so the
        API key can never appear here regardless of what Groq itself
        echoes back.
        """
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw = ""
        detail = raw
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                message = parsed["error"].get("message")
                if isinstance(message, str) and message.strip():
                    detail = message
        except (json.JSONDecodeError, ValueError):
            pass
        detail = detail.strip()[:_MAX_ERROR_BODY_CHARS]
        label = _HTTP_STATUS_LABELS.get(exc.code, "server error" if exc.code >= 500 else "request failed")
        return "Groq returned HTTP {} ({}){}".format(
            exc.code, label, ": {}".format(detail) if detail else "",
        )

    def _failure(self, message: str, start: float) -> LLMResponse:
        return LLMResponse(
            error=message, provider=self.name, model=self.model,
            latency_seconds=time.perf_counter() - start,
        )
