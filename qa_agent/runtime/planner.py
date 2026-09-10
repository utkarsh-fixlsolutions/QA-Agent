"""The Runtime QA Planning Engine's one public entry point (Phase G Part 1,
docs/21-runtime-qa-planning-engine.md): `plan_runtime_qa(context)`.

Input is a `RepositoryContext` only - `context.project` already holds the
`ProjectKnowledge` every rule needs, so nothing here re-walks a filesystem,
calls an AI provider, or invokes a subprocess. Every rule in `rules.py` is
a pure function; this module's only job is to call all of them, dedupe by
id defensively, and compute a deterministic `recommended_order` from each
check's declared `dependencies` - never to invent a check of its own.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from .models import PRIORITIES, RuntimeQAPlan
from .rules import ALL_RULES


def _dependency_depth(checks_by_id):
    """Each check's depth = 0 if it has no dependencies present in this
    plan, else 1 + the maximum depth of its dependencies. A dependency id
    that isn't actually part of this plan (should not happen - every
    `dependencies` tuple in rules.py only ever names another rule's own
    fixed id - but defended against anyway, matching this project's "never
    trust an input to be as clean as expected" discipline) is simply
    ignored rather than raising.
    """
    depth = {}

    def _depth_of(check_id, seen):
        if check_id in depth:
            return depth[check_id]
        if check_id in seen:
            # A dependency cycle - cannot happen from rules.py's own fixed,
            # hand-written dependency graph, but guarded against rather
            # than assumed impossible: treat as depth 0 rather than
            # recursing forever.
            return 0
        check = checks_by_id.get(check_id)
        if check is None:
            return 0
        seen = seen | {check_id}
        deps = [d for d in check.dependencies if d in checks_by_id]
        result = 0 if not deps else 1 + max(_depth_of(d, seen) for d in deps)
        depth[check_id] = result
        return result

    for check_id in checks_by_id:
        _depth_of(check_id, frozenset())
    return depth


_PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}  # critical=0 ... low=3


def _assign_order(checks):
    """Deterministic order: dependency depth first (a check never appears
    before anything it depends on), then priority, then `id` alphabetically
    as the final, always-available tie-break - never insertion order, which
    would make the plan depend on which rule happened to run first.
    """
    checks_by_id = {c.id: c for c in checks}
    depth = _dependency_depth(checks_by_id)
    ordered = sorted(checks, key=lambda c: (depth[c.id], _PRIORITY_RANK[c.priority], c.id))
    return tuple(replace(check, recommended_order=position) for position, check in enumerate(ordered, start=1))


def plan_runtime_qa(context):
    """Never raises: every rule function is pure and, given a real
    `RepositoryContext`, cannot itself fail - a defensive `try/except`
    still wraps the whole body, matching every other builder in this
    project (`discover_project()`, `build_repository_context()`), so a
    genuinely unexpected failure still returns a structured, empty plan
    rather than propagating.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        collected = []
        seen_ids = set()
        for rule in ALL_RULES:
            for check in rule(context):
                if check.id in seen_ids:
                    # Defensive only - every rule above uses its own fixed,
                    # distinct id; this guards against a future rule
                    # accidentally colliding rather than silently producing
                    # two checks that claim the same id.
                    continue
                seen_ids.add(check.id)
                collected.append(check)
        ordered = _assign_order(collected)
        return RuntimeQAPlan(context=context, checks=ordered, generated_at=generated_at)
    except Exception:  # noqa: BLE001 - must never crash a caller
        return RuntimeQAPlan(context=context, checks=(), generated_at=generated_at)
