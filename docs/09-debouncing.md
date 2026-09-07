# Phase B Part 4 — Debouncing

**Status:** Confirmed
**Date:** 2026-09-05

Bursts of filesystem events now coalesce into a single analysis pass. Event scheduling only — the analysis pipeline and reporting are unchanged.

## The chain
```
WatchSession → FileSystemMonitor → Debouncer → AnalysisBridge → existing pipeline
```
Linear, with each link knowing only the next one's interface. The monitor has never heard of debouncing; the Debouncer has never heard of ruff.

## Before and after
A single save on Windows emits several events. Previously each triggered its own analysis:

```
Modified: src\one.py        Created: src\one.py
QA Agent report                 QA Agent report
Modified: src\one.py    →         Checked: 1 file(s)
QA Agent report                   Findings (1): F401 ...
```
Three files saved together now produce **one** report covering all three, instead of three separate runs:
```
Created: src\a.py
Created: src\b.py
Created: src\c.py
QA Agent report
  Input:   watch: 3 changed files
  Checked: 3 file(s)
```

## The interval: 300 ms, chosen from measurement
Real gaps were measured before picking a number:

| Burst | Events | Gaps |
|---|---|---|
| simple write | 2 | 0.3 ms |
| rewrite | 2 | 0.2 ms |
| atomic save (temp + replace) | 2 | 0.1 ms |
| deliberately chunked write | 2 | 50.8 ms |
| three different files | 6 | 0.2–0.3 ms |

Real bursts arrive **0.1–0.3 ms** apart; only an artificially chunked write reached 50 ms. A 100 ms window would already have coalesced every observed gap, so **300 ms carries roughly a 6× margin** for slower editors while staying under the ~400 ms threshold where feedback stops feeling immediate. Configurable per instance (`Debouncer(on_flush, interval=...)`), with `DEBOUNCE_SECONDS` as the default.

## Coalescing without losing changes
The Debouncer keeps a `dict` keyed by path — the latest meaningful event per file — not a bare set, so deletions stay visible in the summary while being excluded from analysis.

- **Same file, many events** → one entry → one analysis.
- **Different files** → one entry each → all forwarded in a single `analyze_paths()` call. Nothing is dropped, because `run()` has always taken a list.
- **Deleted** → kept in the batch for reporting, excluded from the analysis paths.
- **Renamed** → destination analyzed, source never.
- Every event **restarts** the window (trailing edge); the batch flushes once things go quiet.
- The pending map is swapped out under a lock, so events arriving *during* an analysis land in the next batch rather than being lost.

**Two event-semantics rules live beside `FileEvent` in `fsmonitor.py`**, so the bridge and the Debouncer share one definition instead of each keeping a copy:
- `target_path(event)` — what a file event leaves to examine (`None` for deletions, destination for renames).
- `merge_events(existing, incoming)` — which event best describes a path within one burst. A rename or creation is normally followed by a `Modified` for the same file from the same action, so the **more specific event wins** and a rename is not downgraded to a modification. A deletion is the file's final state and always wins.

That second rule was added after testing showed a rename being reported as `Modified: src\renamed.py`, losing where the file came from. It now reads `Renamed: src\a.py -> src\renamed.py`.

## Why the pipeline needed no changes
`AnalysisBridge.analyze_paths(paths, label)` already took a list and never asked where the paths came from — the seam built in Part 3. The Debouncer simply calls it with more than one path. No timers, intervals, or buffering reach the bridge.

| File | Change |
|---|---|
| `qa_agent/debouncer.py` | new — `Debouncer`, `EventBatch` |
| `qa_agent/fsmonitor.py` | added shared `target_path()` and `merge_events()` |
| `qa_agent/analysis_bridge.py` | uses the shared `target_path()` instead of its own copy |
| `qa_agent/watch.py` | `components` sequence; `print_change_summary()`, `describe_batch()`; banner text |
| `qa_agent/__main__.py` | wires the linear chain |
| `runner.py`, `report.py`, `adapters.py` | **untouched** |

## Threading, stated plainly
A debounce cannot exist without something firing after silence, so a `threading.Timer` is used — the stdlib idiom, alive only while a window is pending, with no pool and no concurrency. **A separate lock serializes flushes**, so if a burst's window expires while an analysis is still running, the next batch waits rather than running alongside it. Verified by test.

Analysis now runs on the timer thread rather than the observer thread, so the observer is no longer blocked while ruff runs.

**Known property, deliberately not addressed:** trailing-edge debouncing means continuous writes (a code generator saving every 100 ms) would keep deferring analysis. A max-wait cap would solve it; that is scheduling policy beyond this part's scope.

**Shutdown:** components stop in reverse order — the monitor is silenced before the debouncer — and a pending window is **cancelled**, not flushed. Ctrl+C means stop.

## What success looks like — verified 2026-09-05
**Debouncer unit tests (9/9):** duplicates collapse to one analysis · three distinct files (each duplicated) yield one analysis of three paths · deletions excluded from analysis but kept in the batch · modified-then-deleted is not analyzed · rename analyzes the destination only · trailing edge re-arms and fires once · **events during an analysis land in the next batch** · **flushes never overlap** · `stop()` cancels the pending window and ignores later events.

**End-to-end against a live watcher (10/10):** one save → one report (was two) · six rapid saves → one analysis · three files saved together → **one report, `Checked: 3 file(s)`** · no file lost · findings still real · a pre-existing buggy file never analyzed (no rescan) · deletion reported but not analyzed · rename analyzed the destination only · four reports for five user actions.

**Also verified:** Part 3's error-containment tests still pass after the bridge change · Ctrl+C with a pending debounce window exits 0 with no thread leak and no late report · all 8 Phase A invocations return identical exit codes · the agent passes its own dogfood check.
