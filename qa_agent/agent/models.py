"""Data shapes for the G5.1 Autonomous QA Action Selection Controller
(docs/28-g5-action-selection-controller.md).

Deliberately duck-typed against G1-G4's own real result types - the same
zero-import precedent `qa_agent/ai/diagnosis.py` already established for
reading `RuntimeCheckResult`/`RuntimeExecutionResult` as plain input data,
applied here one layer higher: this package reads the *combined* output of
every prior phase (discovery, planning, execution, diagnosis) without
importing a single symbol from `qa_agent.runtime`, `qa_agent.project`, or
`qa_agent.ai.diagnosis`/`qa_agent.ai.runtime_repair`. G5.1 only ever reads
already-produced state; it never calls anything that produces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

DECISION_CONTINUE = "continue"
DECISION_STOP = "stop"
DECISION_ERROR = "error"
DECISIONS = (DECISION_CONTINUE, DECISION_STOP, DECISION_ERROR)

# Stable ControllerDecision.error sentinels (G5.2, docs/29) - never raw,
# ad-hoc strings recomputed at each call site, so a caller (the G5.2 loop)
# can tell "the AI made a well-formed but currently-wrong choice" (safe to
# retry - nothing executed, one iteration consumed) apart from a genuine
# provider/parsing failure (not safe to keep going, see loop.py). Neither
# value is ever influenced by the AI - both are only ever set by
# controller.py itself, after it has already rejected the AI's own output.
ERROR_INELIGIBLE_ACTION = "ineligible_action"
ERROR_INVALID_TARGET = "invalid_target"

SAFETY_READ_ONLY = "READ_ONLY"
SAFETY_ASSISTED = "ASSISTED"
SAFETY_AUTONOMOUS = "AUTONOMOUS"
SAFETY_LEVELS = (SAFETY_READ_ONLY, SAFETY_ASSISTED, SAFETY_AUTONOMOUS)

DEFAULT_MAX_ITERATIONS = 10
DEFAULT_OBJECTIVE = "Reach a fully passing, verified QA state for this repository."


@dataclass(frozen=True)
class ActionDefinition:
    """One entry in the deterministic action registry (`actions.py`) - the
    AI is never trusted to determine any of these properties; every field
    here is fixed, hand-authored data, never AI-generated or AI-editable.

    `id`: the stable action identifier the AI selects by name.
    `category`: `discovery` | `planning` | `static_analysis` |
    `runtime_check` | `diagnosis` | `repair` - grouping only, not itself a
    safety or eligibility signal.
    `safety_level`: one of `SAFETY_LEVELS` - minimal metadata for a *future*
    G5 stage to filter by permission mode; G5.1 itself does not enforce
    mode-based filtering (no mode concept exists yet in this project, and
    building one is explicitly out of this step's scope).
    `modifies_files`: whether the underlying deterministic capability can
    write to the real repository - `True` only for `runtime_repair`, which
    G5.1 deliberately never exposes as eligible (see `implemented` below).
    `requires`: other action ids that must already be completed (per
    `QAState.completed_action_ids`) before this one is even considered.
    `implemented`: whether a real, safe, already-existing deterministic
    capability backs this action id at all. `runtime_repair` is the one
    entry in the registry with `implemented=False` - not because
    `qa_agent.ai.repair_runtime_failure` doesn't exist (it does, and is
    complete, G4) but because this step's own scope explicitly reserves
    repair *selection* for G5.3, not G5.1 - see the module docstring in
    `controller.py`.
    `requires_target`: whether selecting this action also requires naming
    one specific, deterministically-valid target (e.g. `runtime_diagnosis`
    choosing *which* failed check to interpret, when more than one exists).
    `False` for every action except `runtime_diagnosis` (added in G5.2,
    docs/29 - see `actions.valid_targets()`); the AI is never allowed to
    invent a target any more than it can invent an action id - the
    controller validates it the same way, against a deterministically-
    computed set, never trusting the model's own claim about what a valid
    target would be.
    """

    id: str
    description: str
    category: str
    safety_level: str
    modifies_files: bool
    requires: Tuple[str, ...]
    implemented: bool
    requires_target: bool = False


@dataclass(frozen=True)
class QAState:
    """The current, bounded QA state a caller hands to the controller.

    Every field is either `None`/empty (nothing observed yet) or a
    reference to a real, already-produced result from an earlier phase -
    never duplicated, never re-derived, never invented. G5.1 never
    constructs one of these itself (it has no executor); a caller (a test
    today, a future G5.2 loop eventually) builds and updates it externally.

    `repository_context`: a real `RepositoryContext` (`qa_agent.project`),
    or `None` if project discovery has not run yet.
    `runtime_plan`: a real `RuntimeQAPlan` (`qa_agent.runtime`), or `None`.
    `execution_results`: real `RuntimeCheckResult`s accumulated so far, in
    any order - duplicates (the same `.id` appearing twice) are not
    expected from a well-behaved caller and are not deduplicated here.
    `static_analysis_result`: a real, duck-typed `RunResult`-shaped object
    (`.findings`, `.tools_used`, `.checked`) from `qa_agent.runner.run()`,
    or `None` if static analysis has not run yet.
    `diagnoses`: real `RuntimeDiagnosis`es (`qa_agent.ai`) accumulated so
    far.
    `repairs`: real `RuntimeRepairResult`s (`qa_agent.ai`) accumulated so
    far - always empty in G5.1 (nothing in this package ever produces one);
    the field exists now so G5.3 does not need a `QAState` shape change
    later to start populating it.
    `objective`: a short, human-readable QA goal for the prompt - falls
    back to `DEFAULT_OBJECTIVE` when blank.
    `iteration`: how many actions have already been selected-and-executed
    in this session (G5.1 itself never increments this - a future G5.2
    loop would, once each selected action actually runs).
    `max_iterations`: a hard bound on total actions for one session - the
    controller enforces this itself, deterministically, before ever
    consulting the AI (see `controller.select_next_action`).
    """

    repository_context: object = None
    runtime_plan: object = None
    execution_results: Tuple[object, ...] = ()
    static_analysis_result: object = None
    diagnoses: Tuple[object, ...] = ()
    repairs: Tuple[object, ...] = ()
    objective: str = ""
    iteration: int = 0
    max_iterations: int = DEFAULT_MAX_ITERATIONS

    @property
    def remaining_budget(self) -> int:
        return max(0, self.max_iterations - self.iteration)

    @property
    def executed_check_ids(self):
        """The real `.id` of every `RuntimeCheckResult` already in state -
        a plain `set`, computed fresh every time (never stored, never able
        to drift from `execution_results` itself).
        """
        return {getattr(r, "id", None) for r in self.execution_results}

    @property
    def failed_check_ids(self):
        """`.id`s whose real, deterministic `.status` is one of the three
        diagnosable statuses (`fail`/`timeout`/`error`) - the exact same
        string tuple `qa_agent.ai.diagnosis.DIAGNOSABLE_STATUSES` already
        uses, duplicated here rather than imported (this package's own
        zero-import rule, see the module docstring).
        """
        return {
            getattr(r, "id", None) for r in self.execution_results
            if getattr(r, "status", None) in ("fail", "timeout", "error")
        }

    @property
    def diagnosed_check_ids(self):
        return {getattr(d, "check_id", None) for d in self.diagnoses}

    @property
    def completed_action_ids(self):
        """Which registry action ids count as "already done", computed
        fresh from the real state every time - never a separately-tracked
        list that could silently disagree with the data it's supposed to
        summarize.
        """
        from .actions import RUNTIME_CHECK_ACTION_IDS  # local import: actions.py imports this module

        completed = set()
        if self.repository_context is not None:
            completed.add("project_discovery")
        if self.runtime_plan is not None:
            completed.add("runtime_plan")
        if self.static_analysis_result is not None:
            completed.add("static_analysis")
        executed = self.executed_check_ids
        for action_id, check_id in RUNTIME_CHECK_ACTION_IDS.items():
            if check_id in executed:
                completed.add(action_id)
        # runtime_diagnosis is deliberately NOT marked "completed" just
        # because it ran once - it becomes eligible again whenever a new,
        # undiagnosed failure exists (see actions.py's own eligibility
        # rule) - "completed" here only means "nothing left to diagnose
        # right now", the same dynamic sense `completed_action_ids` uses
        # for every other action.
        if not (self.failed_check_ids - self.diagnosed_check_ids):
            completed.add("runtime_diagnosis")
        return frozenset(completed)


@dataclass(frozen=True)
class ControllerDecision:
    """The controller's own structured output - the one thing G5.1 ever
    returns. `decision` is always one of `DECISIONS`. `next_action` is a
    real, currently-eligible action id (only ever set alongside
    `decision == DECISION_CONTINUE`), or `None`. `target` is `None` for
    every action except `next_action == "runtime_diagnosis"` (added in
    G5.2, docs/29), in which case it is a real, currently-valid target id
    (`actions.valid_targets`) - never the AI's own unchecked claim.
    `eligible_actions` is exactly what was actually offered to the AI for
    this decision - kept on the result so a test (or a future caller)
    never has to recompute eligibility separately to understand what the
    controller was even choosing among. `error` is set only for
    `DECISION_ERROR` - `models.ERROR_INELIGIBLE_ACTION`/
    `ERROR_INVALID_TARGET` name the two cases G5.2's own execution loop
    treats as a safe-to-retry rejected decision rather than a fatal one;
    every other `error` value is a genuine provider/parsing failure.

    This dataclass never invents a fallback action - `DECISION_ERROR` with
    a clear `reason`/`error` is always the safe result for anything this
    controller cannot resolve deterministically.
    """

    decision: str
    next_action: Optional[str]
    reason: str
    target: Optional[str] = None
    evidence_needed: Tuple[str, ...] = ()
    eligible_actions: Tuple[str, ...] = ()
    model: str = ""
    error: Optional[str] = None

    def __post_init__(self):
        if self.decision not in DECISIONS:
            raise ValueError("ControllerDecision has an unrecognized decision {!r}".format(self.decision))
