"""A deterministic provider for tests and for exercising the AI layer without
a real model - never touches a network. Configured entirely at construction:
what `generate()`/`test_connection()` return is fixed at creation time, not
computed, so a test asserting on it never has to guess (Phase D Part 1).
"""

from __future__ import annotations

from .provider import ConnectionResult, LLMResponse

DEFAULT_MODEL = "mock-model"
DEFAULT_RESPONSE_TEXT = "mock response"


class MockProvider:
    """Returns exactly what it was configured to return - a fixed success,
    or a fixed failure - every single call. No randomness, no state that
    changes between calls, no network.
    """

    name = "mock"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        response_text: str = DEFAULT_RESPONSE_TEXT,
        fail: bool = False,
        failure_message: str = "mock provider configured to fail",
    ):
        self.model = model
        self._response_text = response_text
        self._fail = fail
        self._failure_message = failure_message

    def generate(self, prompt: str) -> LLMResponse:
        if self._fail:
            return LLMResponse(
                error=self._failure_message, provider=self.name, model=self.model
            )
        return LLMResponse(
            text=self._response_text, provider=self.name, model=self.model
        )

    def test_connection(self) -> ConnectionResult:
        if self._fail:
            return ConnectionResult(ok=False, detail=self._failure_message)
        return ConnectionResult(ok=True, detail="mock provider is always reachable")
