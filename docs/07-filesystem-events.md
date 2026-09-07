# Phase B Part 2 — Filesystem Event Detection

**Status:** Confirmed
**Date:** 2026-09-05

Watch mode now detects filesystem changes. **It still analyzes nothing** — no ruff, no `run()`, no debouncing, no queue, no initial scan. Strictly an event detector.

## Usage
```
python -m qa_agent watch D:\Working\Facial-Emotions-Recognition
```
```
QA Agent - watch mode
  Watching:  ...\fsproject
  Analyzers: ruff (.py)
  Status:    watch mode started successfully

  Detecting changes only - files are not analyzed yet.
  Press Ctrl+C to stop.
Created: src\utils.py
Modified: src\utils.py
Renamed: src\utils.py -> src\helpers.py
Deleted: src\helpers.py
```

## The dependency: watchdog 6.0.0
Assessed against the project's five dependency conditions:

| Condition | How it is met |
|---|---|
| Integrates cleanly | Its observer runs on its own thread; `WatchSession` was already built around a `threading.Event` for exactly this |
| Isolated behind our abstraction | **`fsmonitor.py` is the only module that imports watchdog** — verified by grep |
| Replaceable via integration layer only | Consumers receive our `FileEvent`, never a watchdog class. Swapping backends means rewriting one file |
| Supports long-term direction | Native OS events (`ReadDirectoryChangesW` on Windows) — instant, near-zero idle CPU, suits an always-on PC |
| Stable and widely adopted | The de-facto standard Python library for this, MIT, used across the ecosystem |

The alternative — stdlib polling — was rejected because it **cannot detect renames**; it would have had to *guess* by pairing a delete with a create, which is the sort of inference this project has refused since Step 1. Pinned in `requirements.txt` alongside ruff.

## Design
**`FileEvent`** — a frozen dataclass (`kind`, `path`, `old_path`), deliberately independent of watchdog's event classes. This is the contract Part 3 consumes, and the same boundary `Finding` draws around ruff's JSON.

**`FileSystemMonitor`** — wraps the observer, watches recursively, and hands each translated event to an **injected callback**. It does not print, and it does not decide what matters.

**Filter order:** unsupported extension → outside tree → ignored directory → emit.

### How purity and the registry requirement coexist
Extensions must come from the adapter registry, yet the monitor must know nothing about analyzers. Resolved the way Part 1 handled analyzer names:

> **The monitor never imports `ADAPTERS`.** `__main__` — the composition root — builds `set(ADAPTERS)` and injects it.

Verified: `fsmonitor.py` imports only `dataclasses`, `pathlib`, and watchdog. Nothing from `adapters`, `runner`, `report`, or `gitdiff`.

Printing lives in `watch.py` as a module-level `print_file_event()` — outside `WatchSession`, which stays lifecycle-only.

### Lifecycle
`WatchSession` accepts an optional `component` with `start()`/`stop()` and runs it around the idle loop, stopping it in a `finally`. The session never learns what the component does. `stop()` joins the observer thread, so the banner's "Nothing was left running" is literally true.

| File | Change |
|---|---|
| `qa_agent/fsmonitor.py` | new — the watchdog integration layer |
| `qa_agent/watch.py` | optional `component`; module-level `print_file_event()`; banner line updated |
| `qa_agent/__main__.py` | builds extensions from the registry, wires monitor + callback |
| `requirements.txt` | new — pinned `watchdog==6.0.0`, `ruff==0.15.14` |
| `adapters.py`, `runner.py`, `report.py`, `gitdiff.py` | **untouched** |

## Ignored directories
`.git`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `node_modules` — a `DEFAULT_IGNORED_DIRS` frozenset, injectable per monitor so extending it needs no edit to the module.

**Known duplication, flagged deliberately:** `runner.py` has a similar list for directory walks. They are *not* shared — the monitor stays dependency-free, and the two are allowed to diverge (one filters events, the other filters scans). Noted so it stays a known choice rather than silent drift.

## Behaviour worth knowing
- **One save can produce several `Modified` events.** Windows reports content and metadata writes separately; the test run showed three for a single write. That is real OS behaviour, not a bug — Part 3's debouncing is where it collapses to one.
- **Editors save atomically** (write temp, rename over), so a save may surface as `Created` or `Renamed`.
- **Moves across the watch boundary** appear as `Deleted` (out) or `Created` (in) — there is no other side to report.
- **Mixed-extension renames**: reported on the destination. `notes.txt` → `notes.py` is a `Created`; `notes.py` → `notes.txt` is a `Deleted`.

## What success looks like — verified 2026-09-05
End-to-end against a live process performing real file operations:

| Check | Result |
|---|---|
| Created / Modified / Deleted | all reported |
| Renamed | reported as `src\utils.py -> src\helpers.py` |
| `.png`, `.mp4`, `.pptx` (created *and* modified) | **zero output** |
| `.py` files inside `.git`, `node_modules`, `__pycache__`, `.ruff_cache` | **zero output** — ignored even though the extension is supported |
| Survives every event | process alive throughout; still reporting afterwards |
| **Ctrl+C with a live observer** | event caught mid-run, interrupt at 2.0s → exit at 2.05s, exit 0, **thread count back to baseline (observer joined, nothing leaked)** |
| **Future adapter picked up automatically** | registering a fake `.ts` adapter made `component.ts` events appear **with no change to `fsmonitor.py`**, while `.css` stayed ignored |
| Phase A regression | all 7 pre-existing invocations return identical exit codes |
| watchdog isolation | grep confirms no module outside `fsmonitor.py` imports it |
