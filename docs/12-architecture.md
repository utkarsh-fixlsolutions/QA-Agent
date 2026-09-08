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
| `adapters.py` | Tool registry (ruff, pyright, eslint, shellcheck); `Finding`; `ToolError` | — |
| `runner.py` | Dispatch, invoke each tool independently, merge/sort/dedupe findings into one unified, deterministic result (docs/15) | `adapters` |
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
| `ruff==0.15.14` | A Python analyzer (lint/style), invoked as a CLI binary | `adapters.py` — never imported as a library |
| `pyright==1.1.411` | A second Python analyzer (type checking), pinned like ruff (docs/step-log.md, Phase C Part 5) | `adapters.py`, invoked the same way as ruff |
| `eslint` (external, Node/npm) | The JavaScript analyzer (docs/14) | `adapters.py`, invoked the same way as ruff — but not a qa_agent-owned dependency; must be installed by whatever project is being analyzed, not pinned in `requirements.txt` |
| `shellcheck` (external, no pip/npm distribution) | The shell-script analyzer (docs/step-log.md, Phase C Part 5) | `adapters.py`; a standalone binary the user installs themselves, exactly like eslint |
| `mypy==2.3.1` | A third Python analyzer (a second, independent type checker), pinned like ruff and pyright (docs/step-log.md, Phase C Part 7) | `adapters.py`, invoked the same way as ruff/pyright |

No network access at any point during analysis. Phase C, Part 1 generalised `runner.py`'s invocation step to be adapter-declared rather than ruff-shaped — see docs/13 and docs/14 for what that means and why. Ruff and pyright both claim `.py`, the first real (not test-only) case of two adapters sharing an extension.

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
| `regression/test_multi_analyzer.py` | Every adapter's parsing (ruff, eslint, pyright, shellcheck); the generic `ok_exit_codes`/cwd/`use_shell` mechanisms (docs/14); deterministic ordering, dedup, and result isolation (docs/15), all adapter-blind |
| `regression/test_config.py` | Config discovery, loading, and validation (docs/16) |
| `integration/test_reporting.py` | Bridge returns outcomes and prints nothing; reporter renders all cases |
| `integration/test_stability.py` | The four Part 6 defects, leaks, lifecycle |
| `integration/test_timeout_recovery.py` | Hung analyzer times out, is reported, and the next analysis works |
| `integration/test_watch_pipeline.py` | The whole pipeline as a real process with real edits |
| `integration/test_multi_language.py` | ESLint + ruff together, real installs (`tests/fixtures/eslint`, `npm install` once) - mixed projects, cwd anchoring, config errors, watch mode |
| `integration/test_config_cli.py` | The config system through the real CLI (docs/16) |
| `integration/test_additional_analyzers.py` | Pyright + ShellCheck through the real CLI (`tests/fixtures/shellcheck/fetch.ps1` once) - real findings, config enable/disable, a four-language mixed project, tool isolation with a genuinely missing tool |
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
- **Ruff's own JSON severity is always `error`** - true across every dogfooding run to date. `SEVERITY_POLICY` in `adapters.py` lets you reclassify specific rules if that granularity matters. Pyright (`error`/`warning`/`information`), ShellCheck (`error`/`warning`/`info`/`style`), and Mypy (`error`/`note`) report genuinely varied severities, and `min_severity` (docs/16) filters across all five tools using each tool's own words - validated end-to-end against real mixed-severity output (docs/step-log.md, Phase C Parts 6-7).
- **Pyright's per-invocation cost is large (~15s measured, dominated by its own startup, not file count) and paid once per chunk.** `MAX_FILES_PER_CALL=200` (`runner.py`) exists for Windows' command-line length limit and is fine for ruff/eslint/shellcheck's near-zero per-invocation cost, but a large `.py`-heavy project spanning several chunks pays pyright's ~15s startup multiple times. Measured on a real 661-file repository (docs/step-log.md, Phase C Part 5): 37.7s total vs. 15.0s for pyright alone on one subset in one invocation. A per-adapter batch size (or a single unchunked pyright call when the command line allows it) would fix this - still deferred (Phase C Part 6 measured it again on medium/large corpora and found nothing beyond what Part 5 already quantified, and the fix would mean adapter-specific special-casing in an otherwise adapter-blind engine).
- **Git's Windows line-ending conversion (`core.autocrlf`) can introduce CRLF into checked-out shell scripts**, which ShellCheck correctly flags (`SC1017`) - observed on a real repository clone, not a false positive in the adapter.
- **Pyright and mypy both run from qa_agent's own `.venv`, with no visibility into a target project's actual dependencies.** Dogfooding a genuinely external, unrelated Python project (flask, Phase C Part 6) produced 234 "missing import" findings from pyright alone - real (the import genuinely isn't installed in *this* venv) but almost entirely unactionable noise, versus ruff's 1 genuine style finding on the same repository. Mypy independently confirmed the same trade-off on a second external project (Phase C Part 7): its own `import-not-found` findings for the same reason. Not a bug - neither tool has a way to know which environment to check a target project against unless told - but a real trade-off worth knowing before pointing this at someone else's codebase.
- **Batches are chunked by both file count and estimated command length** (`MAX_FILES_PER_CALL`, `MAX_COMMAND_LENGTH_SHELL`/`MAX_COMMAND_LENGTH_DIRECT` in `runner.py`) - added in Phase C Part 6 after a real, silent failure: a large batch of long, deeply-nested paths launched through `cmd.exe` (`use_shell=True`, eslint) exceeded cmd.exe's own ~8191-character line limit, which is far smaller than the ~32k `CreateProcess` limit the file-count cap alone was calibrated for. The command was rejected with an ordinary exit code and empty output, previously misread as "ran cleanly, no findings" rather than a tool failure.
- **Mypy exits 2 - the same code it uses for a genuine crash - both for a real fatal failure and for a batch containing a file it cannot parse or a genuine module-naming conflict** (e.g. two same-named `conftest.py` files in different package-less directories, confirmed on a real repository). `MypyAdapter.ok_exit_codes` deliberately excludes 2, so either case is conservatively reported as a tool error rather than guessed at - other files in the same batch may have real findings mypy itself declined to report because of the one problem file; ruff and pyright are unaffected and still report normally on the same files (docs/step-log.md, Phase C Part 7).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `'ruff' is not installed or not on PATH` (or `pyright`) | `pip install -r requirements.txt`, with the venv active |
| `'shellcheck' is not installed or not on PATH` | Install ShellCheck yourself (no pip/npm distribution) and put it on PATH - `tests/fixtures/shellcheck/fetch.ps1` does this for the test suite only |
| `not inside a git repository` | `--git-diff` must run inside a repo; use explicit paths instead |
| Watch mode reports nothing when you save | Check the file's extension is registered in `ADAPTERS`, and that it is not inside an ignored directory |
| A directory named `watch` is not scanned | A bare `watch` selects watch mode — pass `./watch` or an absolute path |
| `did not finish within 60s` | The analyzer hung and was stopped; the watcher continues normally |
| `<component> stopped unexpectedly` | The observer died (deleted root, disconnected drive). Restart the watcher |
| Output looks buffered in a log file | It is flushed per batch; check the redirect, not the agent |
