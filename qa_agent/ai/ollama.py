"""Ollama provider: talks to a local Ollama server over its plain HTTP API.

Stdlib only (`urllib.request`) - no new dependency for a foundation this
small, isolated to this one module exactly the way every subprocess-based
adapter isolates its own tool (adapters.py) or `fsmonitor.py` isolates
`watchdog`. `requests` or similar would be reasonable too, but nothing here
needs more than urlopen + json, so the smaller footprint wins (docs/step-log
.md, Dependency philosophy).

Every failure mode this part is required to handle - an offline server, a
timeout, a connection failure, a malformed response - is caught here and
returned as a failed `LLMResponse`/`ConnectionResult`, never raised. AI is
optional (docs/step-log.md, Phase D Part 1): a provider failing must never
be able to crash, or even be visible to, the deterministic analyzer
pipeline, which does not import this module at all.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from .provider import ConnectionResult, LLMResponse

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "llama3"
DEFAULT_TIMEOUT_SECONDS = 30.0

# TimeoutError and socket.timeout are the same class from Python 3.10
# onward, and socket.timeout is a subclass of TimeoutError before that -
# checked as a pair so this is correct on this project's full 3.8+ range,
# not just the interpreter it happens to be written on.
_TIMEOUT_EXCEPTIONS = (socket.timeout, TimeoutError)


class OllamaProvider:
    """Talks to a local (or remote) Ollama server via `/api/generate`.

    Endpoint, model, and timeout are all configurable at construction - no
    other configuration surface exists yet; CLI flags and `.qa-agent.json`
    integration are later Phase D parts, not this one.
    """

    name = "ollama"

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str) -> LLMResponse:
        """Send `prompt` to `/api/generate` (non-streaming) and return the
        model's reply. Never raises - see the module docstring.
        """
        start = time.perf_counter()
        body = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + "/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except _TIMEOUT_EXCEPTIONS:
            return self._failure(
                "'{}' did not respond within {:.0f}s".format(self.endpoint, self.timeout), start
            )
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, _TIMEOUT_EXCEPTIONS):
                return self._failure(
                    "'{}' did not respond within {:.0f}s".format(self.endpoint, self.timeout),
                    start,
                )
            return self._failure(
                "could not reach '{}': {}".format(self.endpoint, exc.reason), start
            )
        except OSError as exc:
            return self._failure("connection to '{}' failed: {}".format(self.endpoint, exc), start)
        except Exception as exc:  # noqa: BLE001 - AI must never crash the pipeline
            return self._failure(
                "unexpected error talking to '{}': {}".format(self.endpoint, exc), start
            )

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._failure("malformed response (not valid JSON): {}".format(exc), start)

        if not isinstance(parsed, dict) or not isinstance(parsed.get("response"), str):
            return self._failure(
                "malformed response (missing 'response' field): {}".format(raw[:200]), start
            )

        return LLMResponse(
            text=parsed["response"],
            provider=self.name,
            model=self.model,
            latency_seconds=time.perf_counter() - start,
        )

    def test_connection(self) -> ConnectionResult:
        """A lightweight reachability check - GET `/api/tags`, no model
        invoked, matching this part's "lightweight connection testing"
        requirement.
        """
        request = urllib.request.Request(self.endpoint + "/api/tags", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except _TIMEOUT_EXCEPTIONS:
            return ConnectionResult(
                ok=False,
                detail="'{}' did not respond within {:.0f}s".format(self.endpoint, self.timeout),
            )
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, _TIMEOUT_EXCEPTIONS):
                return ConnectionResult(
                    ok=False,
                    detail="'{}' did not respond within {:.0f}s".format(
                        self.endpoint, self.timeout
                    ),
                )
            return ConnectionResult(
                ok=False, detail="could not reach '{}': {}".format(self.endpoint, exc.reason)
            )
        except OSError as exc:
            return ConnectionResult(
                ok=False, detail="connection to '{}' failed: {}".format(self.endpoint, exc)
            )
        except Exception as exc:  # noqa: BLE001 - AI must never crash the pipeline
            return ConnectionResult(
                ok=False, detail="unexpected error reaching '{}': {}".format(self.endpoint, exc)
            )
        return ConnectionResult(ok=True, detail="'{}' is reachable".format(self.endpoint))

    def _failure(self, message: str, start: float) -> LLMResponse:
        return LLMResponse(
            error=message,
            provider=self.name,
            model=self.model,
            latency_seconds=time.perf_counter() - start,
        )
