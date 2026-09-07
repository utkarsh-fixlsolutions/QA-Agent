# Phase B Part 6 — Long-Running Stability

**Status:** Confirmed
**Date:** 2026-09-05

No new capabilities. This part audited watch mode for continuous execution, found four real defects by reproducing them, and fixed them.

## Defects found and fixed

Each was **reproduced before being fixed**, and the reproduction re-run afterwards.

### 1. One bad event permanently deafened the watcher — critical
An exception in the event callback escaped into watchdog's dispatch thread and killed it.

| | Before | After |
|---|---|---|
| Observer alive after a raising event | **False** | True |
| Events delivered afterwards | **0** | 3 |

The process stayed alive and the banner still claimed to be watching. **A silently deaf watcher is the worst failure mode for a process meant to run for weeks** — it fails invisibly. `_Handler._emit` now contains the exception, reports it, and keeps dispatching.

### 2. `stop()` returned while an analysis was still running
Measured order was `flush-start → stop-returned → flush-end`, so *"Nothing was left running"* was false, output could follow the shutdown message, and interpreter exit killed the analysis mid-flight.

`Debouncer.stop()` now waits on the flush lock. Re-measured: `flush-start → flush-end → stop-returned`.

The wait is **bounded** (`SHUTDOWN_WAIT_SECONDS = 10`) so a pathological analysis cannot make Ctrl+C hang. On overrun the session says which component did not finish and exits 2, rather than passing silently.

### 3. Shutdown order was inverted from its own documented intent
`components=[monitor, debouncer]` stopped in reverse meant `stop:debouncer → stop:monitor` — the **event source outliving its consumer**, the opposite of the Part 4 comment claiming "events stop before the debouncer does."

Components are now listed source-first and stopped in that order: `stop:monitor → stop:debouncer`.

### 4. A failing `stop()` orphaned everything after it
When one component's `stop()` raised, later components were never stopped and the exception escaped `run()` — leaving an observer thread alive and exiting on a traceback. Each stop is now isolated and reported; all components stop regardless.

## Storm behaviour, measured

**The `threading.Timer` design was challenged with data before being kept.**

| Measurement | Result |
|---|---|
| watchdog delivery rate (real storm) | 2,470 events/sec |
| `Debouncer.submit()` throughput | 6,000–8,000 events/sec |
| **Headroom** | **~2.5×** |
| Peak live threads at 50,000 events | **6** |
| `Timer.start()` failures | 0 |
| Files lost, 3,000-file storm | **0** |
| Files lost, sustained load with slow analyses | **0** |
| Memory over 1,200 events / 200 batches | +3.4 KB, +11 objects |

**Conclusion: `threading.Timer` is not a bottleneck** — watchdog's own dispatch is the ceiling, and cancelled timers do not accumulate. A scheduler-thread redesign was proposed, then **withdrawn for lack of evidence**. The simpler implementation stands.

Answering the storm questions directly:
- **Can watchdog's queue bottleneck?** It is the slower stage, but it kept up: 3,000 files produced 6,000 events with none lost.
- **Can our batching bottleneck?** No — 2.5× headroom over the delivery rate.
- **Events faster than analyses complete?** They coalesce into the pending map and are analyzed in the next batch. Nothing is lost; a slow analysis delays work rather than dropping it.
- **Lost, delayed, or mis-merged?** None lost in any test. Merging follows `merge_events()`. Delay is real — see below.
- **Correct under sustained load?** Yes. Backlog is bounded by the number of *distinct* changed files (peak 2,041 entries), then drains to zero.

### The one real limitation this uncovered
Under **25 seconds of continuous writing** (~100 files/sec), the trailing-edge window never expired: **1 batch, zero analyses until writing stopped**. Nothing was lost — all 2,057 files were analyzed once quiet returned — but the watcher gives **no feedback during sustained activity**. A long `npm install` or file sync would look dead.

The fix is a max-wait cap ("flush at least every N seconds"). That is a behaviour change, not a reliability fix, so it is **deferred to Part 7** rather than slipped in here.

## Object ownership
| Object | Created by | Owned by | Shut down by | Unreachable when |
|---|---|---|---|---|
| `WatchSession` | `_watch_main` | its frame | returns from `run()` | `main()` returns |
| `FileSystemMonitor` | `_watch_main` | `WatchSession.components` | `WatchSession._stop_all` | after `run()` |
| watchdog `Observer` | `monitor.start()` | the monitor | `stop()` + `join()`, then set to `None` | on that assignment |
| `Debouncer` | `_watch_main` | `WatchSession.components` | `WatchSession._stop_all` | after `run()` |
| `Timer` (per event) | `_restart_timer` | the debouncer | `cancel()` on replacement or stop | when its thread exits |
| `AnalysisBridge`, `LiveReporter` | `_watch_main` | the `analyze_batch` closure | nothing to stop | with the closure |
| `EventBatch`, `RunResult` | per flush | the flush frame | — | when the flush returns |

Nothing relies on "hopefully collected": both threaded resources are explicitly stopped, and the observer reference is cleared.

## Thread lifecycle
| Thread | Created | Stopped | Joined | Daemon |
|---|---|---|---|---|
| watchdog observer + emitter | `monitor.start()` | `observer.stop()` | **yes** | watchdog's own |
| `Timer` (one per event) | `submit()` | `cancel()` on replacement/stop | no — `stop()` waits on the flush lock instead | yes |

The Timer stays a daemon deliberately: `stop()` performs a real, bounded wait for in-flight work *first*, and reports an overrun. The flag is a documented last-resort guarantee that Ctrl+C exits — **not a way to hide a shutdown bug**, which was defect 2 and is now fixed.

## Deterministic shutdown
```
Ctrl+C -> KeyboardInterrupt in the main thread
1. monitor.stop()     observer.stop() + join()      no new events, ever
2. debouncer.stop()   mark stopped, cancel window
3.                    wait (bounded) for an in-flight flush
4. report anything that did not finish
5. print the shutdown message
6. return 0  (2 if a component died or overran)
```
Source-first is what makes this deterministic: once the observer is joined nothing can enqueue work, so step 2 races nothing.

## Shutdown races
| Race | Behaviour |
|---|---|
| Event arrives during shutdown | Observer already joined; a straggler hits the `_stopped` guard and is dropped |
| Window expires while stopping | The flush takes the flush lock, re-checks `_stopped`, returns without running |
| Flush snapshotted but not started | `stop()` holds the flush lock; that flush sees `_stopped` and does nothing |
| Observer mid-dispatch | `join()` waits for the in-flight dispatch |
| Analysis finishing at Ctrl+C | Waited for; its report prints **before** the shutdown message |

## Exception containment
| Failure | Behaviour | Visible? |
|---|---|---|
| Bad filesystem event | Reported; dispatch continues | Yes, full traceback |
| `ToolError` in analysis | Returned as an outcome, rendered | Yes |
| Unexpected error in analysis | Returned as an outcome, rendered with traceback | Yes |
| Exception in the flush callback | Reported; debouncer keeps running | Yes |
| Component `stop()` raises | Reported; remaining components still stop | Yes |
| Component dies silently | Liveness check reports it and exits 2 | Yes |

**Nothing is swallowed.** Every containment point reports before continuing.

## Architecture review
No module is overloaded. `watch.py` is one class over stdlib; `debouncer.py` is timing only; `fsmonitor.py` is watchdog isolation; the bridge is orchestration; `live_report.py` is presentation. The dependency graph remains a DAG and the pipeline is unchanged.

`_watch_main` now does parsing, registry translation, and wiring of five objects. It is still readable and it is the composition root — the right place for wiring. **Named honestly rather than refactored for appearances.**

**No rewrite was performed.** Adapters, runner, finding model, report model, incremental pipeline and filesystem abstraction are untouched; Phase A output remains byte-identical.

## Verification
**Part 6 suite (7/7):** monitor liveness honest · double `start()` refused · dead component detected → exit 2 · failing batch reported, debouncer survives · `stop()` bounded, reports overrun, safe twice · events after `stop()` ignored · 10 start/stop cycles leak no threads.

**Defect reproductions re-run:** all four now show the opposite result.

**No regressions:** debouncer semantics 9/9 · bridge contract 8/8 · live reporting 12/12 · shutdown clean with no leak · growth still flat · Phase A output byte-identical · all exit codes unchanged · dogfood clean.

---

# Production Readiness Review

## ✓ Production ready
- **No memory growth** — +3.4 KB / +11 objects over 1,200 events, measured
- **No thread leaks** — flat across 200 batches and 10 start/stop cycles
- **No event loss** — zero under a 3,000-file storm and under sustained load with slow analyses
- **Backlog bounded** by distinct changed files; drains to zero
- **Deterministic shutdown** — source-first, waits for in-flight work, reports overruns
- **Exception containment at every boundary**, always visible
- **A dead observer is detected and reported** instead of silently watching nothing
- Watch output is ASCII — cannot crash on a cp1252 console

## ⚠ Acceptable, with limitations
- **No feedback during sustained activity** — measured: 25 s of continuous writes produced zero analyses until quiet. Nothing lost, but the watcher looks idle. Needs a max-wait cap (Part 7).
- ~~Very large batches are slow~~ — **corrected in Part 7 by measurement:** 3,000 files analyse in **2.0s** (600 files in 0.53s). This was asserted here without being measured; it is not a limitation.
- **watchdog's internal queue is unbounded** — safe in testing, not hard-capped.
- **Ctrl+C pressed twice** interrupts the shutdown itself.
- **A deleted or disconnected watch root** kills the observer; detected and reported, not recovered from.
- **Timer threads remain daemons** — justified above, but the guarantee rests on the bounded wait rather than the flag.

## ✗ Deliberately left for Part 7
- ~~No `subprocess` timeout on ruff~~ — **implemented in Part 7** (`ANALYSIS_TIMEOUT_SECONDS = 60`), with a test proving timeout, clear reporting, and recovery on the next analysis.
- **Max-wait cap** for the starvation limitation above.
- **No self-healing** after a dead observer — it reports and exits rather than re-establishing the watch.
- No structured logging, rotation, metrics, health endpoint, or supervision integration.
- No backpressure or bounded work queue.
