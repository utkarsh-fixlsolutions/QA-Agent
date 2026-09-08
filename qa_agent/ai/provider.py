"""The provider contract every AI backend satisfies (Phase D Part 1).

A `Protocol`, not an ABC - the same duck-typed spirit as `adapters.py`'s
tool contract (`name`, `extensions`, `build_command`, `parse`), just made
structurally checkable since this project already dogfoods a static type
checker (pyright, mypy) on its own source. A provider does not need to
inherit `AIProvider`; it only needs to have the right shape.

`LLMResponse` mirrors `AnalysisOutcome`'s own ok/error shape
(analysis_bridge.py) - the same "did it work, and what happened" contract
already used for the deterministic pipeline, reused here rather than
inventing a new one. A provider method never raises: every real-world
failure - offline, timeout, a malformed response - becomes a failed
`LLMResponse` or `ConnectionResult` instead, so a provider's own failure can
never crash the (still entirely AI-unaware) analyzer pipeline.

Nothing in this package is imported by, or imports, `runner.py`,
`adapters.py`, `report.py`, `config.py`, `analysis_bridge.py`, `__main__.py`,
or watch mode - wiring an AI provider into the CLI is explicitly a later
Phase D part, not this one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMResponse:
    """What one generation call produced: text, or the error that prevented it.

    `error=None` means success - the same convention `ToolError`-free results
    already use throughout this project. A response is never partially valid:
    either `text` is real model output, or `error` explains why there is none.
    """

    text: str = ""
    error: str | None = None
    provider: str = ""
    model: str = ""
    latency_seconds: float = 0.0

    @property
    def ok(self):
        return self.error is None


@dataclass(frozen=True)
class ConnectionResult:
    """Whether a provider is currently reachable, and why not if it isn't.

    Deliberately separate from `LLMResponse`: checking reachability should
    never require invoking a model (docs/step-log.md, Phase D Part 1 -
    "lightweight connection testing").
    """

    ok: bool
    detail: str = ""


@runtime_checkable
class AIProvider(Protocol):
    """The shape every provider - Ollama, Mock, and whatever comes later -
    satisfies. Two methods, both total (they never raise): `generate` for
    one prompt-in/text-out call, `test_connection` for a lightweight
    reachability check that never invokes a model.
    """

    name: str

    def generate(self, prompt: str) -> LLMResponse: ...

    def test_connection(self) -> ConnectionResult: ...
