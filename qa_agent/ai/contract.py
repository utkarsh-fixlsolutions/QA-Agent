"""Shared AI behavior-contract text (docs/25-ai-behavior-contract.md).

Small, additive prompt-text constants meant to be appended onto - never to
replace - an existing prompt's own guardrails (`diagnosis_prompts.
DIAGNOSIS_GUARDRAILS`, `runtime_repair_prompts.REPAIR_GUARDRAILS`, and any
future runtime-facing prompt's own guardrail constant). Nothing here is
read by any deterministic module; this is prompt text only, exactly like
`prompts.GUARDRAILS`/`DIAGNOSIS_GUARDRAILS`/`REPAIR_GUARDRAILS` already are.

Kept intentionally short and narrow. The per-prompt guardrails this project
already has cover most of the FACT/HYPOTHESIS/UNKNOWN ground already
(observed-evidence-vs-inference, the insufficient-context decline, "you are
not the authority on pass/fail"). These constants add only the pieces a
direct audit found genuinely missing - see docs/25 for the full contract
these are one small part of.
"""

from __future__ import annotations

# Missing from both DIAGNOSIS_GUARDRAILS and REPAIR_GUARDRAILS: neither
# told the model what to do when the evidence itself points more than one
# way. Without this, a model under pressure to produce a confident answer
# has no instruction *not* to just pick one arbitrarily.
CONTRADICTORY_EVIDENCE_CLAUSE = (
    "If the evidence conflicts with itself or supports more than one "
    "equally plausible explanation, do not resolve that conflict by "
    "arbitrarily picking one - say so explicitly (name the conflicting "
    "possibilities, or use the insufficient-context decline) and lower "
    "your confidence accordingly."
)

# Both prompts already say "you are not the authority on whether it passed
# or failed" (in their own _ROLE text) - but that only covers a second-
# guess of the status field. Nothing stopped a model from writing a
# summary/explanation that quietly implies a different outcome in prose.
DETERMINISTIC_AUTHORITY_CLAUSE = (
    "The deterministic result above is the only authority on what "
    "actually happened - not just its status field, but every fact it "
    "reports. Never write a summary, explanation, or any other text that "
    "implies a different outcome than the deterministic result actually "
    "recorded."
)

# Repair-specific: nothing in REPAIR_GUARDRAILS previously said the model
# has no say in whether its own proposal is correct - repair.py's own
# docstring already makes this a hard rule for the deterministic system
# ("the AI never grades its own repair"), but the prompt itself never told
# the model that.
CANDIDATE_ONLY_CLAUSE = (
    "Your proposal is a candidate only. You do not decide whether it is "
    "correct or whether it fixes the problem - a separate, deterministic "
    "process will validate that before anything is ever applied. Do not "
    "claim, in `explanation` or anywhere else, that this change is "
    "verified, tested, or guaranteed to work."
)
