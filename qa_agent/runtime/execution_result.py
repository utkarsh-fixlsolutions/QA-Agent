"""`RuntimeExecutionResult`: the whole-plan outcome of running a
`RuntimeQAPlan` for real (Phase G Part 2, docs/22-runtime-execution-engine
.md). References the `RuntimeQAPlan` it executed rather than duplicating
its checks - the same "reference, don't duplicate" pattern `RepositoryContext`
established for `ProjectKnowledge` and `RuntimeQAPlan` established for
`RepositoryContext`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .execution_models import RuntimeCheckResult
from .models import RuntimeQAPlan


@dataclass(frozen=True)
class RuntimeExecutionResult:
    plan: RuntimeQAPlan
    results: Tuple[RuntimeCheckResult, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    total_duration: float = 0.0
