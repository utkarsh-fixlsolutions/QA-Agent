"""Verified repair application: the final, controlled write of an ACCEPTED
repair to the user's real project (Phase E Part 5).

Everything up to this point is advisory-only and confined to a Phase E Part
2 temporary workspace - Parts 1-4 never touch a real project file. This
module is the one place in the whole Phase E pipeline that does, and only
after every prior stage (a real Finding, a validated repair, a deterministic
`accept_candidate` decision) has already succeeded, and only after
re-checking - right before writing - that nothing has changed since.

Pipeline:

    RepairDecision (Part 4, action == "accept_candidate")
        -> re-verify every precondition against the *current* real file
        -> safe write: backup -> temp file -> flush -> atomic replace
        -> RepairApplicationResult

Hard rules:
- Only a decision whose `action` is exactly `ACTION_ACCEPT_CANDIDATE` may
  ever reach a write. A `reject`, `hold`, or `validation_failed` decision
  is refused immediately, before any file is touched.
- The original file is never edited in place - a temporary file is written
  and flushed in the same directory, then swapped in with a single atomic
  `os.replace()`, so the real file is at every instant either fully the old
  content or fully the new content, never partially written.
- A backup of the original is always created before any write attempt, and
  restored automatically if anything after that point fails.
- This module never re-runs an analyzer, never calls an AI provider, and
  never invents replacement text of its own - the content written is
  exactly `AppliedRepair.patched_text`, already produced by Part 2 and
  already judged by Parts 3-4. Recomputing the patch here would duplicate
  workspace.py's own logic, which this part deliberately reuses instead.
- No CLI, no prompts, no Y/N - `approve_repair()`/`reject_repair()` are
  plain service functions for a later interactive layer (Phase E Part 6)
  to call, not anything that asks a user a question itself.

A known, deliberate consequence of "the original file must be unchanged
since the repair was generated": if `applied_repair` is the *second* of two
sequential repairs applied to the same file within one Part 2 workspace,
its own `original_text` reflects the workspace copy's state *after* the
first repair, not the real project file's true original content - which
this module has never touched. Validating against the real file then
correctly and safely refuses it (a real mismatch, not a bug) - applying
anything but the first/only repair for a given file is out of this part's
scope, consistent with "no repair loop" being explicitly deferred.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .decision import ACTION_ACCEPT_CANDIDATE
from .workspace import _detect_encoding

BACKUP_SUFFIX_FORMAT = ".bak-{}"


@dataclass(frozen=True)
class RepairApplicationResult:
    """The outcome of trying to apply one accepted repair to a real file.

    `success=True`: `file` was written, `backup_file` names the real
    backup created before the write, `bytes_written` is the size (in
    bytes, in the file's own detected encoding) of the content actually
    written, `timestamp` is an ISO-8601 string for when the write
    completed.
    `success=False`: `reason` is a short, fixed phrase explaining why
    nothing was written - never accepted, a precondition failed, or the
    write itself failed. `error` carries the underlying exception text,
    only ever set alongside a write-stage failure.
    `decision_used`: the `RepairDecision` this outcome was decided from,
    carried through for a caller's reference - never re-interpreted here.
    """

    success: bool
    reason: str
    file: object = None  # str | None
    backup_file: object = None  # Path | None
    bytes_written: object = None  # int | None
    timestamp: object = None  # str | None
    decision_used: object = None
    error: object = None  # str | None


class _VerificationFailed(Exception):
    """Internal control flow only - never escapes this module. Carries the
    short, fixed reason one precondition failed, so `_verify()` reads
    top-to-bottom as a plain sequence of checks rather than a nested
    if/elif chain.
    """


def _verify(decision):
    """Every precondition this part requires, checked against the
    *current* state of the real project - never assumed still true from
    whenever the repair was proposed. Returns
    `(original_file, patched_text, encoding)` on success; raises
    `_VerificationFailed` with a short, fixed reason on the first check
    that fails. Never modifies anything - purely reads.
    """
    if decision is None or getattr(decision, "action", None) != ACTION_ACCEPT_CANDIDATE:
        raise _VerificationFailed(
            "repair was not accepted (action={!r})".format(
                getattr(decision, "action", None) if decision is not None else None
            )
        )

    applied_repair = getattr(decision, "applied_repair", None)
    if applied_repair is None or not getattr(applied_repair, "ok", False):
        raise _VerificationFailed("workspace repair did not complete successfully")

    proposal = getattr(applied_repair, "proposal", None)
    workspace_file = getattr(applied_repair, "workspace_file", None)
    patched_text = getattr(applied_repair, "patched_text", None)
    original_text_at_copy = getattr(applied_repair, "original_text", None)
    if proposal is None or workspace_file is None or patched_text is None:
        raise _VerificationFailed("applied repair is missing required data")

    original_file = getattr(proposal, "file", None)
    if not original_file:
        raise _VerificationFailed("repair has no target file")

    # "repair still targets the same file": the top-level proposal a
    # caller passed to decide_repair() must agree with the one embedded in
    # applied_repair - a mismatch means decide_repair() was given a
    # proposal/applied_repair pair that do not actually belong together.
    top_level_proposal = getattr(decision, "proposal", None)
    if top_level_proposal is not None and getattr(top_level_proposal, "file", None) != original_file:
        raise _VerificationFailed("repair no longer targets the same file")

    if not Path(workspace_file).is_file():
        raise _VerificationFailed("workspace repair file no longer exists")

    if not Path(original_file).is_file():
        raise _VerificationFailed("original file no longer exists")

    try:
        current_raw = Path(original_file).read_bytes()
        encoding = _detect_encoding(current_raw)
        current_text = current_raw.decode(encoding)
    except OSError as exc:
        raise _VerificationFailed("could not read the original file: {}".format(exc)) from exc
    except UnicodeDecodeError as exc:
        raise _VerificationFailed("original file could not be decoded: {}".format(exc)) from exc

    # "original file has not changed since proposal generation": compared
    # against AppliedRepair.original_text using the exact same encoding-
    # detection rule workspace.py itself used to produce it
    # (_detect_encoding, reused directly rather than re-implemented).
    # Missing entirely fails closed - "unverifiable" is not "unchanged".
    if original_text_at_copy is None:
        raise _VerificationFailed(
            "applied repair has no recorded original_text; cannot verify the file is unchanged"
        )
    if current_text != original_text_at_copy:
        raise _VerificationFailed("original file has changed since the repair was generated")

    start_line = getattr(proposal, "start_line", None)
    end_line = getattr(proposal, "end_line", None)
    line_count = len(current_text.splitlines())
    if (not isinstance(start_line, int) or isinstance(start_line, bool)
            or not isinstance(end_line, int) or isinstance(end_line, bool)
            or start_line < 1 or end_line < start_line or end_line > line_count):
        raise _VerificationFailed("repair line range no longer fits the file")

    return original_file, patched_text, encoding


def _safe_write(path, text, encoding):
    """Backup -> temp file (created in the *same* directory as `path`, so
    the final replace is guaranteed atomic on one filesystem) -> flush ->
    `os.fsync` -> atomic `os.replace`. If anything fails after the backup
    exists, the original is restored from it before the exception
    propagates - `os.replace` is itself atomic (it cannot leave `path`
    partially written), so this is defense in depth, not a gap it closes.

    Returns `(backup_path, bytes_written)`. Raises on failure - the caller
    (`apply_verified_repair`) is the one place that turns that into a
    structured result.
    """
    path = Path(path)
    backup_path = path.with_name(
        path.name + BACKUP_SUFFIX_FORMAT.format(datetime.now().strftime("%Y%m%d%H%M%S%f"))
    )
    # Nothing has been written yet at this point - a failure here (e.g. no
    # permission to create the backup) leaves the original completely
    # untouched, with nothing to restore.
    shutil.copy2(path, backup_path)

    payload = text.encode(encoding)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".qa-agent-tmp"
    )
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            shutil.copy2(backup_path, path)
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        raise

    return backup_path, len(payload)


def apply_verified_repair(decision):
    """Apply one accepted repair to the real project, or refuse safely.

    Only ever writes when `decision.action == "accept_candidate"` and
    every precondition in `_verify()` still holds against the file's
    *current* state. Every other decision, and every failed precondition,
    returns `RepairApplicationResult(success=False, ...)` without touching
    anything.

    Never raises: a genuinely unexpected exception anywhere in this
    process - including a write that fails after a backup was already
    made, which is restored automatically before this returns - still
    comes back as a structured, `success=False` result.
    """
    try:
        original_file, patched_text, encoding = _verify(decision)
    except _VerificationFailed as exc:
        return RepairApplicationResult(success=False, reason=str(exc), decision_used=decision)
    except Exception as exc:  # noqa: BLE001 - this function must never crash a caller
        return RepairApplicationResult(
            success=False, reason="unexpected error during verification",
            decision_used=decision, error="{}: {}".format(type(exc).__name__, exc),
        )

    try:
        backup_path, bytes_written = _safe_write(original_file, patched_text, encoding)
    except Exception as exc:  # noqa: BLE001 - a write failure must not crash a caller
        return RepairApplicationResult(
            success=False, reason="write failed; original file was not modified",
            file=original_file, decision_used=decision,
            error="{}: {}".format(type(exc).__name__, exc),
        )

    return RepairApplicationResult(
        success=True, reason="repair applied successfully",
        file=original_file, backup_file=backup_path, bytes_written=bytes_written,
        timestamp=datetime.now().isoformat(), decision_used=decision,
    )


def approve_repair(decision):
    """The explicit "this repair is approved" entry point for a later
    interactive layer (Phase E Part 6) to call once a human (or some other
    policy) has said yes - not itself an approval UI, just the service
    call an approval UI would make. Identical to `apply_verified_repair()`
    in every respect; kept as its own named function so a caller's intent
    reads clearly at the call site, separate from the write mechanics.
    """
    return apply_verified_repair(decision)


def reject_repair(decision):
    """The explicit "this repair is declined" entry point - guaranteed
    never to write, regardless of what `decision.action` was. Gives a
    later interactive layer a symmetric pair with `approve_repair()`: a
    clean, structured "not applied, by explicit choice" result to report
    or log, rather than simply doing nothing.
    """
    return RepairApplicationResult(
        success=False, reason="repair rejected; not applied", decision_used=decision,
    )
