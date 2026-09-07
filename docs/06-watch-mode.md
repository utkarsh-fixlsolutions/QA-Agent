# Phase B Part 1 — Watch Mode Skeleton

**Status:** Confirmed
**Date:** 2026-09-05

Turns the agent into a process that can stay running while you work. **This part starts and stops cleanly and nothing more** — no file watching, no events, no automatic analysis.

## Usage
```
python -m qa_agent watch <project_path>
python -m qa_agent watch D:\Working\Facial-Emotions-Recognition
```
Runs until Ctrl+C. Exit codes follow the existing convention: `0` after a clean shutdown, `2` for an unusable path.

```
QA Agent - watch mode
  Watching:  D:\Working\Qa-Agent
  Analyzers: ruff (.py)
  Status:    watch mode started successfully

  Files are not being analyzed yet - this part starts the process only.
  Press Ctrl+C to stop.

QA Agent: watch mode stopped. Nothing was left running.
```

## How it dispatches, and why existing behaviour is safe
`paths` is a `nargs="*"` positional. Converting the CLI to argparse subparsers would have **broken** `python -m qa_agent <path>`, because argparse would then require a subcommand name.

So dispatch happens *before* argparse: if the first argument is exactly `watch`, the watch CLI handles it; otherwise the original parser runs, entered unchanged. The existing parser never learns watch mode exists — which is what makes "unchanged" checkable rather than hoped for.

**Known ambiguity:** a directory literally named `watch`. A bare `python -m qa_agent watch` selects watch mode, so pass it explicitly instead — `python -m qa_agent ./watch`, or an absolute path. Verified working.

## Responsibilities
`WatchSession` in `qa_agent/watch.py` owns the process **lifecycle only**: validate the path, print the banner, idle, shut down cleanly. It deliberately holds no analyzer or filesystem knowledge — the analyzer names it prints are passed in by the caller, and it imports nothing from `adapters`, `runner`, `report`, or `gitdiff`.

`__main__.py` is the composition root: it builds the analyzer list from the `ADAPTERS` registry and hands it over. The registry stays the single source of truth, so the banner cannot drift when a second adapter is registered.

| File | Change |
|---|---|
| `qa_agent/watch.py` | new — `WatchSession` |
| `qa_agent/__main__.py` | pre-dispatch + `_watch_main()`; existing `main()` body unchanged |
| `adapters.py` | read-only (banner); **not modified** |
| `runner.py`, `report.py`, `gitdiff.py` | **untouched** |

## Two implementation details worth knowing
**The idle loop uses a `threading.Event`, not `time.sleep`.** `while not self._stop.wait(0.5)` blocks efficiently, stays interruptible so Ctrl+C lands promptly, and can be ended from *another thread* via `session.stop()` — which is exactly what a filesystem observer will need. It costs nothing today and avoids a rewrite later.

**The banner is flushed explicitly.** Found during testing: with output redirected to a file, stdout is block-buffered, so the banner stayed invisible while the process ran. For a long-running process that is a real defect, not cosmetics.

## Where later parts attach
Two seams, neither requiring changes to this file's responsibility or to the Phase A pipeline:
- **Filesystem monitoring** — an observer thread collects changed paths and calls `session.stop()` to end the session.
- **Analysis / debouncing / live reporting** — debounce between the event source and the call, then hand the file list to the existing `run()` → `render()` path, exactly as `--git-diff` does.

This works because Phase A separated *which files* from *how they are analyzed*. Step 4 already proved it: `--git-diff` added an input mode without touching a single adapter. Watch mode is the same move one level up — it changes *when* selection happens, not how analysis works.

## What success looks like — verified 2026-09-05
| Check | Result |
|---|---|
| **All 8 pre-existing invocations** (path/dir/`--output`/`--git-diff`/`--git-diff REF`/no-args/missing-path) | exit codes identical to before |
| Starts and stays running | still alive after 3s as a real background process |
| Banner correct and immediate | shows directory, `ruff (.py)` from the registry, success line — visible while running, even redirected |
| **Ctrl+C on the real idle loop** | `KeyboardInterrupt` via `_thread.interrupt_main()` (the same mechanism Ctrl+C uses) — noticed in 0.03s, friendly message, exit 0, no traceback |
| `stop()` from another thread | graceful, exit 0 — the seam future parts will use |
| Does not exit on its own | confirmed it idles rather than falling through |
| Missing path / file-not-directory / no path given | exit 2 with a clear message each |
| Directory named `watch` | `./watch` scans it correctly; ambiguity documented |

**One limitation, stated plainly:** a synthetic console Ctrl+C could not be delivered in this sandbox — Git Bash's `kill -INT` does not reach native Windows processes, and `GenerateConsoleCtrlEvent` did not cross into the child's console. The interrupt path is verified through the real loop by the mechanism Python itself uses for Ctrl+C, but pressing Ctrl+C in a live terminal is worth one manual confirmation on your side.
