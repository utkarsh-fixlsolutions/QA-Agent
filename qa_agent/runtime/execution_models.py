"""Immutable data shapes for one `RuntimeCheck`'s real execution (Phase G
Part 2, docs/22-runtime-execution-engine.md).

`RuntimeCheckResult` never claims more than what was actually observed:
`status` is always one of the six closed values below, `reason` always
names *why* (a real log line matched, a real exit code, a real timeout),
and `details`/`logs` hold real captured output - never invented text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_NOT_IMPLEMENTED = "not_implemented"

STATUSES = (
    STATUS_PASS,
    STATUS_FAIL,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    STATUS_ERROR,
    STATUS_NOT_IMPLEMENTED,
)


@dataclass(frozen=True)
class RuntimeCheckResult:
    """One `RuntimeCheck`'s real outcome. `id`/`name` mirror the
    `RuntimeCheck` this result belongs to (not a reference to it - a plain
    string copy, since a result must remain meaningful even if rendered
    long after the originating plan object is gone).

    `logs` is a tuple of captured output lines (stdout/stderr, merged) -
    real subprocess output, never fabricated, and never present at all for
    a check that never launched a process (`SKIPPED`/`NOT_IMPLEMENTED`).
    `artifacts` names any real file this execution produced or referenced
    (empty in Part 2 - no executor here writes a file) - left as a tuple
    now so a later phase that does produce one (a screenshot, a saved
    response body) needs no shape change to this model.
    """

    id: str
    name: str
    status: str
    start_time: str
    end_time: str
    duration: float
    reason: str
    details: str = ""
    logs: Tuple[str, ...] = ()
    artifacts: Tuple[str, ...] = ()
    exception: Optional[str] = None
    retryable: bool = False

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(
                "RuntimeCheckResult({!r}) has an unrecognized status {!r}".format(self.id, self.status)
            )
