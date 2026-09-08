"""Surrounding code context for a single finding (Phase D Part 2).

Independent of everything else in this package - no import of provider.py,
prompts.py, or any qa_agent module outside the stdlib. A small, pure
file-reading utility: given a file and a line, return the lines around it,
or a clear reason why not.

Reads line-by-line rather than loading a file whole, and stops as soon as
the requested window is read - "avoid loading entire files unnecessarily"
(docs/step-log.md, Phase D Part 2). A missing, unreadable, or too-short
file is a normal, expected outcome here, not an exception: every caller
gets back a CodeContext either way, never a raised error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Defaults, all overridable per call - deliberately small: this context
# feeds a prompt, not a human reading the file directly, and every extra
# line is tokens spent on every future call.
DEFAULT_LINES_BEFORE = 5
DEFAULT_LINES_AFTER = 5
MAX_LINES = 200   # hard cap on the window regardless of what was requested
MAX_CHARS = 8000  # hard cap on the joined context text, to bound token usage


@dataclass(frozen=True)
class CodeContext:
    """The lines around one finding, or the reason there are none.

    `lines` holds (line_number, text) pairs so a caller never has to
    recompute which physical line a given entry is - necessary once
    file-start/end clipping or char-limit truncation means `start_line`
    is not simply `target_line - lines_before`.
    """

    ok: bool
    lines: tuple = ()  # tuple[tuple[int, str], ...]
    start_line: int = 0
    end_line: int = 0
    target_line: int = 0
    truncated: bool = False
    error: str | None = None


def extract_context(
    file_path,
    line: int,
    lines_before: int = DEFAULT_LINES_BEFORE,
    lines_after: int = DEFAULT_LINES_AFTER,
    max_lines: int = MAX_LINES,
    max_chars: int = MAX_CHARS,
) -> CodeContext:
    """The lines from `line - lines_before` to `line + lines_after`
    (1-indexed, inclusive), clipped to the file's real start/end and to
    `max_lines`/`max_chars`. Never raises.
    """
    if line < 1:
        return CodeContext(ok=False, error="line must be 1 or greater, got {}".format(line))

    # A hard line-count cap, split evenly - simpler and easier to reason
    # about than proportional trimming, and max_chars is the real defense
    # against excessive token usage regardless. -1 reserves room for the
    # target line itself, so lines_before + 1 (target) + lines_after never
    # exceeds max_lines.
    half_cap = max(0, (max_lines - 1) // 2)
    lines_before = max(0, min(lines_before, half_cap))
    lines_after = max(0, min(lines_after, half_cap))

    start_line = max(1, line - lines_before)
    end_line = line + lines_after
    path = Path(file_path)

    collected = []
    saw_target = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, raw in enumerate(handle, start=1):
                if number < start_line:
                    continue
                if number > end_line:
                    break
                if number == line:
                    saw_target = True
                collected.append((number, raw.rstrip("\n").rstrip("\r")))
    except FileNotFoundError:
        return CodeContext(ok=False, error="file not found: '{}'".format(path))
    except IsADirectoryError:
        return CodeContext(ok=False, error="'{}' is a directory, not a file".format(path))
    except OSError as exc:
        return CodeContext(ok=False, error="could not read '{}': {}".format(path, exc))

    if not saw_target:
        return CodeContext(
            ok=False,
            error="'{}' has fewer than {} line(s); line {} does not exist".format(
                path, line, line
            ),
        )

    collected, truncated = _fit_to_char_budget(collected, line, max_chars)

    return CodeContext(
        ok=True,
        lines=tuple(collected),
        start_line=collected[0][0],
        end_line=collected[-1][0],
        target_line=line,
        truncated=truncated,
    )


def _fit_to_char_budget(collected, target_line, max_chars):
    """Trim from whichever end is farther from the target line until the
    joined text fits max_chars - the target line itself is never dropped,
    even if it alone exceeds the budget (better an over-budget real line
    than a fabricated partial one).
    """
    truncated = False

    def _size(rows):
        return sum(len(text) + 1 for _, text in rows)

    while len(collected) > 1 and _size(collected) > max_chars:
        truncated = True
        first_number = collected[0][0]
        last_number = collected[-1][0]
        if first_number == target_line:
            collected = collected[:-1]
        elif last_number == target_line:
            collected = collected[1:]
        elif (target_line - first_number) >= (last_number - target_line):
            collected = collected[1:]
        else:
            collected = collected[:-1]

    return collected, truncated
