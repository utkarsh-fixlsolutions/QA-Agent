"""Repair decision engine: a deterministic policy over Phase E Part 3's
`RepairValidationResult`, deciding whether a repair is an eligible
*candidate* - never whether it gets applied (Phase E Part 4).

"Accepted" in this part means only: `accept_candidate` - eligible to be
shown for human approval or later application. It does not mean the real
project was modified; nothing in this module writes anywhere, reruns an
analyzer, or calls an AI provider. Writing to the real project, an
approval workflow, and any CLI surface are all explicitly later Phase E
parts.

Pipeline:

    RepairProposal -> AppliedRepair -> RepairValidationResult (Part 3)
        -> decide_repair() -> RepairDecision

Hard rule this whole module exists to enforce: the AI never makes this
decision. `decide_repair()` reads only `RepairValidationResult.status` and
`ValidationComparison`'s plain counts/flags - never `RepairProposal.
confidence` or `.explanation`, never a provider, never a re-run analyzer.
`proposal` and `applied_repair` are accepted only as references carried
through onto the returned `RepairDecision` for a caller's convenience; the
decision logic itself never reads anything off them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .validator import (
    STATUS_IMPROVED,
    STATUS_UNCHANGED,
    STATUS_WORSENED,
)

ACTION_ACCEPT_CANDIDATE = "accept_candidate"
ACTION_REJECT = "reject"
ACTION_HOLD = "hold"
ACTION_VALIDATION_FAILED = "validation_failed"

# The only statuses this policy trusts as "validation genuinely completed" -
# anything else (validator.py's own STATUS_VALIDATION_FAILED, a missing
# ValidationResult, or an unrecognized status string from some other
# producer) fails closed to ACTION_VALIDATION_FAILED, never guessed past.
_KNOWN_STATUSES = (STATUS_IMPROVED, STATUS_UNCHANGED, STATUS_WORSENED)


@dataclass(frozen=True)
class RepairDecision:
    """A deterministic verdict on one repair candidate.

    `action` is one of the four module-level constants:
    - `accept_candidate` - eligible to be shown for human approval or later
      application (E5+). Never means the real project was modified.
    - `reject` - measurably worse, or an improvement that also introduced
      new findings; not eligible.
    - `hold` - no measurable change, or the outcome could not be confirmed
      confidently enough to accept or reject (e.g. the target finding's own
      resolution is unknown) - worth a human look, not auto-decided either
      way.
    - `validation_failed` - no trustworthy validation data exists to decide
      from at all (Part 3's own check failed, or the given `ValidationResult`
      is missing/malformed) - never a judgment about the repair itself.

    `reason`: a short, fixed, deterministic explanation - built from the
    same counts `action` was derived from, never from AI-generated text.
    `before_count`/`after_count`/`removed_count`/`introduced_count`: copied
    from `ValidationComparison` when available, `None` when not (either
    because validation failed, or because a count was itself missing -
    see `decide_repair`'s docstring for which fields cause which fallback).
    `proposal`/`applied_repair`/`validation_result`: the three inputs this
    decision was made from, carried through unmodified for a caller's
    convenience - never read by the decision logic itself for anything
    beyond `validation_result.status`/`.comparison`.
    """

    action: str
    reason: str
    before_count: object = None  # int | None
    after_count: object = None
    removed_count: object = None
    introduced_count: object = None
    proposal: object = None
    applied_repair: object = None
    validation_result: object = None


def _is_plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _validation_failed_reason(validation_result, status):
    if validation_result is None:
        return "no ValidationResult was given"
    if status not in _KNOWN_STATUSES:
        error = getattr(validation_result, "error", None)
        if error:
            return "validation did not complete: {}".format(error)
        return "validation result has no usable status ({!r})".format(status)
    return "validation result is missing its comparison data"


def decide_repair(proposal, applied_repair, validation_result):
    """Decide whether one repair is an eligible candidate, from
    `validation_result` alone - the decision matrix, in the exact order
    specified (Phase E Part 4):

    1. `validation_result` is missing, its `status` is not one of Part 3's
       three completed statuses (`improved`/`unchanged`/`worsened`), or it
       has no `comparison` -> `validation_failed`. This is the only branch
       that can fire without real counts, since none of the others can be
       decided without them.
    2. `status == "worsened"` OR `after_count > before_count` -> `reject`.
       The count check is independent of `status` deliberately: this
       policy trusts the raw counts as much as the label, not one alone.
    3. `status == "unchanged"` -> `hold`.
    4. `status == "improved"` AND `comparison.target_resolved is True` AND
       `introduced_count == 0` -> `accept_candidate`.
    5. `status == "improved"` AND `introduced_count > 0` -> `reject` (a
       safer default: an improvement that also introduces something new is
       not auto-eligible).
    6. Anything else (in practice: `improved`, nothing introduced, but the
       target finding's own resolution is `False` or unconfirmed/`None`)
       -> `hold`.

    Two distinct "fail closed" outcomes, not one, chosen deliberately:
    - Steps/branch 1 above (no `ValidationResult`, an unrecognized status,
      or no `comparison` at all) -> `validation_failed`: there is no
      trustworthy data to decide from.
    - `comparison` exists and `status` is recognized, but one of its own
      count fields (`before_count`/`after_count`/`removed_count`/
      `introduced_count`) is missing or not a real integer -> `hold`, not
      `validation_failed`: Part 3's own check did complete, so this is a
      "worth a human look" situation, not "the check itself failed".

    Never raises: any unexpected exception while reading `validation_result`
    (a malformed object whose attribute access itself misbehaves, say)
    still returns `RepairDecision(action="validation_failed", ...)`.
    """
    try:
        return _decide(proposal, applied_repair, validation_result)
    except Exception as exc:  # noqa: BLE001 - a decision must never crash a caller
        return RepairDecision(
            action=ACTION_VALIDATION_FAILED,
            reason="decision could not be made: {}: {}".format(type(exc).__name__, exc),
            proposal=proposal, applied_repair=applied_repair, validation_result=validation_result,
        )


def _decide(proposal, applied_repair, validation_result):
    status = getattr(validation_result, "status", None) if validation_result is not None else None
    comparison = getattr(validation_result, "comparison", None) if validation_result is not None else None

    # Matrix step 1.
    if validation_result is None or status not in _KNOWN_STATUSES or comparison is None:
        return RepairDecision(
            action=ACTION_VALIDATION_FAILED,
            reason=_validation_failed_reason(validation_result, status),
            proposal=proposal, applied_repair=applied_repair, validation_result=validation_result,
        )

    before_count = getattr(comparison, "before_count", None)
    after_count = getattr(comparison, "after_count", None)
    removed_count = getattr(comparison, "removed_count", None)
    introduced_count = getattr(comparison, "introduced_count", None)
    target_resolved = getattr(comparison, "target_resolved", None)

    if not all(_is_plain_int(v) for v in (before_count, after_count, removed_count, introduced_count)):
        # A recognized status but incomplete counts: validation itself
        # completed, so this is "hold for a human look", not
        # "validation_failed" - see decide_repair's own docstring.
        return RepairDecision(
            action=ACTION_HOLD,
            reason="validation comparison is missing required count(s); holding rather than guessing",
            before_count=before_count, after_count=after_count,
            removed_count=removed_count, introduced_count=introduced_count,
            proposal=proposal, applied_repair=applied_repair, validation_result=validation_result,
        )

    # before_count/after_count/removed_count/introduced_count are typed as
    # plain `object` (they come from an untyped getattr()); the `all(...)`
    # check just above is this function's own guarantee that all four are
    # real ints from this point on - narrowed explicitly since pyright
    # cannot infer that through a generator expression.
    assert isinstance(before_count, int) and isinstance(after_count, int)
    assert isinstance(removed_count, int) and isinstance(introduced_count, int)

    counts = dict(before_count=before_count, after_count=after_count,
                  removed_count=removed_count, introduced_count=introduced_count)
    refs = dict(proposal=proposal, applied_repair=applied_repair, validation_result=validation_result)

    # Matrix step 2.
    if status == STATUS_WORSENED or after_count > before_count:
        return RepairDecision(
            action=ACTION_REJECT,
            reason="analyzer output worsened: {} new finding(s), {} more finding(s) overall than before"
                   .format(introduced_count, after_count - before_count),
            **counts, **refs,
        )

    # Matrix step 3.
    if status == STATUS_UNCHANGED:
        return RepairDecision(
            action=ACTION_HOLD,
            reason="no measurable change: findings before and after are identical",
            **counts, **refs,
        )

    # From here status == STATUS_IMPROVED (worsened/unchanged handled above,
    # and step 1 already excluded anything outside the three known statuses).

    # Matrix step 4.
    if target_resolved is True and introduced_count == 0:
        return RepairDecision(
            action=ACTION_ACCEPT_CANDIDATE,
            reason="target finding resolved, {} finding(s) removed, no new findings introduced"
                   .format(removed_count),
            **counts, **refs,
        )

    # Matrix step 5.
    if introduced_count > 0:
        return RepairDecision(
            action=ACTION_REJECT,
            reason="improvement introduced {} new finding(s); not auto-eligible".format(introduced_count),
            **counts, **refs,
        )

    # Matrix step 6: improved, nothing introduced, but the target finding's
    # own resolution is False or unconfirmed (target_resolved is not True).
    return RepairDecision(
        action=ACTION_HOLD,
        reason="improved overall, but the target finding's own resolution could not be confirmed",
        **counts, **refs,
    )
