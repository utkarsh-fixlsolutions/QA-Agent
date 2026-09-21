"""`RepositoryContext`: the canonical repository-level description built
from a `ProjectKnowledge` (Phase F Part 2, docs/20-repository-context-
engine.md).

**Not to be confused with `qa_agent/ai/context.py`'s `CodeContext`** - that
is the surrounding source lines around *one finding* (Phase D Part 2), used
to build a per-finding AI prompt. `RepositoryContext` here describes the
*whole repository*, has nothing to do with any one finding, lives in the
deterministic project layer rather than the AI layer, and - per this
phase's own explicit rule - is not wired into any AI prompt builder. Two
similarly-named "context" concepts, in two different packages, for two
genuinely different purposes; this docstring exists specifically so that
distinction is never assumed away.

`RepositoryContext` references the `ProjectKnowledge` it was built from
rather than duplicating any of its fields - every raw fact (languages,
frameworks, package managers, evidence, ...) is reached via `.project`,
never re-declared here. Only genuinely new, *derived* information -
computed once, deterministically, from `ProjectKnowledge`'s own already-
verified facts - lives on this object. See context_builder.py for how each
field is actually assembled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .models import ProjectKnowledge


@dataclass(frozen=True)
class RepositoryContext:
    """`generated_at` is the one field that is not itself derived from
    `ProjectKnowledge`'s evidence - it is a plain timestamp of when this
    context was built, the same role `report.py`'s own "Run at:" line
    already plays for a `RunResult`. Like that line, it is expected to
    differ between two calls even against the identical `ProjectKnowledge`
    - tests that check repeatability compare every other field and treat
    this one specially, not as a defect.
    """

    project: ProjectKnowledge
    architecture_summary: Tuple[str, ...] = ()
    repository_layout: Tuple[str, ...] = ()
    constraints: Tuple[str, ...] = ()
    known_limitations: Tuple[str, ...] = ()
    generated_at: str = ""
