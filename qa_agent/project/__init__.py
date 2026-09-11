"""Project Discovery & Knowledge Engine (Phase F Parts 1-2). See
docs/19-project-discovery-engine.md, docs/20-repository-context-engine.md,
and docs/step-log.md.

Deterministic only - no AI, no subprocess, no network, filesystem
inspection alone. `qa_agent.ai` and the deterministic analyzer pipeline
(`runner.py`, `adapters.py`) never import this package; this package never
imports them (`discovery.py`'s single reuse of `runner.IGNORED_DIRS` is a
read of one plain constant, not a dependency on any of that pipeline's
behavior). `qa_agent.__main__` is the only place that wires this package
in, as the `discover` subcommand - the same "AI features are opt-in at the
one composition root" shape Phase D Part 6 already established for
`qa_agent.ai`, applied here to project discovery instead.

Public API: `discover_project(root)` -> `ProjectDiscoveryResult` (Part 1);
`build_repository_context(project)` -> `RepositoryContext` (Part 2), a
derived, still-deterministic description built from an already-discovered
`ProjectKnowledge` - produced and printable, not yet consumed by
`qa_agent.ai` or anything else (Part 2's own explicit scope boundary).
"""

from .context import RepositoryContext
from .context_builder import build_repository_context
from .context_builder import render as render_context
from .context_builder import to_dict as context_to_dict
from .context_builder import to_json as context_to_json
from .discovery import discover_project
from .knowledge import build_knowledge, render
from .models import (
    STATUS_DISCOVERY_FAILED,
    STATUS_INVALID_ROOT,
    STATUS_PARTIAL_SUCCESS,
    STATUS_PERMISSION_DENIED,
    STATUS_SUCCESS,
    STATUSES,
    DetectedItem,
    ProjectDiscoveryResult,
    ProjectKnowledge,
)

__all__ = [
    "STATUS_DISCOVERY_FAILED",
    "STATUS_INVALID_ROOT",
    "STATUS_PARTIAL_SUCCESS",
    "STATUS_PERMISSION_DENIED",
    "STATUS_SUCCESS",
    "STATUSES",
    "DetectedItem",
    "ProjectDiscoveryResult",
    "ProjectKnowledge",
    "RepositoryContext",
    "build_knowledge",
    "build_repository_context",
    "context_to_dict",
    "context_to_json",
    "discover_project",
    "render",
    "render_context",
]
