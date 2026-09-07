# QA Agent — Architecture Overview

**Status:** Phase B complete
**Date:** 2026-09-05

The single page to read before touching the code. Individual step documents (01–11) record how each decision was reached; this one describes what exists now.

## The pipeline

```
                 you save a file
                        |
                        v
              +-------------------+
              |     watchdog      |   OS filesystem events (the only dependency)
              +-------------------+
                        |
                        v
              +-------------------+
              | FileSystemMonitor |   filters: extension, ignored dirs, files only
              |   fsmonitor.py    |   translates to our own FileEvent
              +-------------------+
                        |  FileEvent
                        v
              +-------------------+
              |     Debouncer     |   collects a 300 ms quiet window,
              |    debouncer.py   |   dedupes by path, coalesces bursts
              +-------------------+
                        |  EventBatch(events, paths)
                        v
              +-------------------+
              |  AnalysisBridge   |   orchestration only; returns an
              | analysis_bridge.py|   AnalysisOutcome, prints nothing
              +-------------------+
                        |  paths
                        v
              +-------------------+
              |      Runner       |   dispatches by extension to an adapter,
              |     runner.py     |   invokes ruff, parses its JSON
              +-------------------+
                        |  RunResult (findings)
                        v
              +-------------------+
              |   LiveReporter    |   batch number, timestamp, file list,
              |  live_report.py   |   then findings rendered by report.py
              +-------------------+
                        |
                        v
                    your terminal

  WatchSession (watch.py) owns the lifecycle around all of this:
  validate the path, start components, idle, shut down deterministically.
```

Each link knows only the next one's interface. The monitor has never heard of ruff; the debouncer has never heard of findings; the bridge has never heard of timers.

## Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `adapters.py` | Extension → tool registry; `Finding`; `ToolError` | — |
| `runner.py` | Dispatch, invoke the tool, normalise output | `adapters` |
| `report.py` | All rendering: terminal, Markdown, errors | — |
| `gitdiff.py` | Ask git which files changed | `adapters` |
| `fsmonitor.py` | watchdog isolation; `FileEvent`; event semantics | watchdog |
| `debouncer.py` | Timing, collection, coalescing, dedup | `fsmonitor` |
| `analysis_bridge.py` | Orchestration: paths → `AnalysisOutcome` | `adapters`, `runner` |
| `live_report.py` | Watch-stream presentation | `fsmonitor`, `report` |
| `watch.py` | Process lifecycle only | stdlib only |
| `__main__.py` | CLI parsing and composition root | everything |

`watch.py` imports nothing from the package — the clearest sign the lifecycle stayed separate from the work.

## Event flow, step by step

1. **watchdog** reports a raw OS event on its own thread.
2. **`FileSystemMonitor`** drops directories, unsupported extensions, and ignored directories (`.git`, `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `venv`), then emits a `FileEvent`. Exceptions here are contained — an escaping one would kill watchdog's dispatch thread and leave a **silently deaf watcher**.
3. **`Debouncer.submit()`** stores the event under its path (`merge_events` keeps the most descriptive one) and restarts a 300 ms timer.
4. When the window expires, one **`EventBatch`** is produced: every affected file once, deletions kept for reporting but excluded from analysis.
5. **`AnalysisBridge.analyze_paths()`** runs the pipeline and returns an `AnalysisOutcome`.
6. **`LiveReporter.report()`** prints the batch header and the findings.

## Shutdown sequence

```
Ctrl+C -> KeyboardInterrupt in the main thread
  1. monitor.stop()      observer.stop() + join()      no new events, ever
  2. debouncer.stop()    mark stopped, cancel window
  3.                     wait (bounded, 10s) for an in-flight analysis
  4.                     report anything that did not finish
  5. print the shutdown message
  6. exit 0   (2 if a component died or overran)
```

Components are listed **source-first** and stopped in that order. Once the observer is joined nothing can enqueue work, so every later step races nothing.

## Dependencies

| Package | Why | Isolated behind |
|---|---|---|
| `watchdog==6.0.0` | OS filesystem events | `fsmonitor.py` — the only module that imports it |
| `ruff==0.15.14` | The analyzer, invoked as a CLI binary | `adapters.py` — never imported as a library |

Nothing else. No network access at any point.

## Installation

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Testing methodology

```
python tests/run_all.py            everything, about a minute
python tests/run_all.py --quick    skip the stress suites
```

| Suite | Covers |
|---|---|
| `regression/test_phase_a_cli.py` | Phase A CLI vs golden files, exit codes, `--output` |
| `regression/test_debouncer.py` | Coalescing, deletions, renames, trailing edge, serialisation |
| `integration/test_reporting.py` | Bridge returns outcomes and prints nothing; reporter renders all cases |
| `integration/test_stability.py` | The four Part 6 defects, leaks, lifecycle |
| `integration/test_timeout_recovery.py` | Hung analyzer times out, is reported, and the next analysis works |
| `integration/test_watch_pipeline.py` | The whole pipeline as a real process with real edits |
| `stress/test_storm.py` | Throughput, storms, latency, memory, the known limitation |

Golden files normalise absolute paths and timestamps, so they compare meaningfully on any machine. Delete a golden file to re-record it.

## Dogfooding: measurable success criteria

Running it for a while is not evidence. These are:

| Criterion | Target | How to check |
|---|---|---|
| Crashes | **0** | The process is still running; exit code 0 on Ctrl+C |
| Missed changes | **0** | Every save of a supported file produces a batch |
| Duplicate analyses | **0** | One save produces exactly one report |
| Thread leaks | **0** | Thread count in Task Manager stable from hour 1 to hour N |
| Memory growth | **< 5 MB drift** | Private bytes in Task Manager, hour 1 vs hour N |
| Idle CPU | **< 0.5%** | Task Manager while you are not editing (measured: ~0%) |
| Feedback latency | **< 1s** | From save to report on a single file (measured: ~0.36s) |
| Regressions afterwards | **none** | `python tests/run_all.py` still passes |

**Duration: several days of ordinary development**, including at least one `git checkout` between branches, one dependency install, and one long session left running overnight.

**Classifying what you see:**

| Observation | Verdict |
|---|---|
| Missed change to a `.py` file | **Bug** |
| Two reports for one save | **Bug** |
| Crash, traceback, or silent death | **Bug** |
| Report naming the wrong file or line | **Bug** |
| Nothing during a long `npm install` | **Known limitation** (see below) |
| One batch for a whole `git checkout` | **Expected** — measured at ~0.5s for 600 files |
| Editor temp files reported | Investigate — may need an ignore entry |

## Known limitations

- **No feedback during sustained activity.** Trailing-edge debouncing means continuous writes keep deferring the window. Measured: 25s of continuous writing produced zero analyses until it stopped. Nothing is lost — everything is analyzed once activity pauses — but the watcher looks idle. A max-wait cap would fix it; deferred as a behaviour change.
- **A dead observer is reported, not repaired.** If the watch root is deleted or a network drive disconnects, the session says so and exits 2 rather than re-establishing the watch.
- **Ctrl+C pressed twice** interrupts the shutdown itself.
- **watchdog's internal queue is unbounded** — safe in every test run, but not hard-capped.
- **Severity is whatever ruff reports** (currently always `error`). `SEVERITY_POLICY` in `adapters.py` lets you reclassify specific rules.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `'ruff' is not installed or not on PATH` | `pip install -r requirements.txt`, with the venv active |
| `not inside a git repository` | `--git-diff` must run inside a repo; use explicit paths instead |
| Watch mode reports nothing when you save | Check the file's extension is registered in `ADAPTERS`, and that it is not inside an ignored directory |
| A directory named `watch` is not scanned | A bare `watch` selects watch mode — pass `./watch` or an absolute path |
| `did not finish within 60s` | The analyzer hung and was stopped; the watcher continues normally |
| `<component> stopped unexpectedly` | The observer died (deleted root, disconnected drive). Restart the watcher |
| Output looks buffered in a log file | It is flushed per batch; check the redirect, not the agent |
