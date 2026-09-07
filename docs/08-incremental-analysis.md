# Phase B Part 3 — Incremental Analysis on Change

**Status:** Confirmed
**Date:** 2026-09-05

Watch mode now analyzes files as they change — one file per event, through the existing pipeline. No debouncing, batching, queues, suppression, caching, or parallelism; that is Part 4.

## What it looks like
```
python -m qa_agent watch D:\Working\Facial-Emotions-Recognition
```
```
Created: src\bad.py
QA Agent report
  Run at:  2026-09-05 14:54:38
  Input:   created: src\bad.py
  Checked: 1 file(s)
  Tools:   ruff

Findings (2):
  ...\src\bad.py:1  [error] F401: `json` imported but unused  (ruff)
  ...\src\bad.py:5  [error] E711: Comparison to `None` should be `cond is None`  (ruff)
```
The event echo comes first, so it is always clear which change produced which report.

## Event handling
| Event | Action |
|---|---|
| `created` | analyze that file |
| `modified` | analyze that file |
| `renamed` | analyze the **destination** only — the source path is never analyzed |
| `deleted` | echoed, never analyzed — there is nothing left to check |

A file that vanishes between the event and the analysis is skipped silently: that is a race, not a finding.

## The bridge
`qa_agent/analysis_bridge.py` holds `AnalysisBridge` — a callable that receives a `FileEvent` and decides which path it implies. **Orchestration only.** It performs no analysis, opens no second ruff path, and formats nothing: every line it emits was rendered by `report.py`, which stays the owner of presentation.

```
__call__(event)              echo -> pick path -> analyze_paths([path], label)
analyze_paths(paths, label)  run(paths) -> render(result, label) -> print
```

**Incremental by construction:** `run()` is handed exactly the changed file, never the project. Verified — every report says `Checked: 1 file(s)`.

## Errors never stop the watcher
A long-running watcher that dies on one bad analysis is worse than useless, so `analyze_paths` contains failures instead of propagating them:

- **`ToolError`** (ruff missing, a file that vanished mid-run) → rendered by the existing `render_tool_error()`, watching continues.
- **Anything else** → `render_unexpected_error()`, new in `report.py`, which prints the **full traceback**. A silently swallowed error on an always-on process is the worse bug, so nothing is hidden.

Both renderers live in `report.py`, keeping the bridge free of presentation.

## Why no pipeline refactoring was needed
The extension points already existed and had already been exercised:
- **`run()` has accepted a list of paths since Step 3**, and `--git-diff` proved in Step 4 that a second caller can supply its own file list without touching the runner or any adapter. The bridge is simply a third caller of that contract.
- **`render()` takes a free-form `source` label**, added in Step 5 so callers could describe their own input. `"created: src\bad.py"` needed no renderer change.

| File | Change |
|---|---|
| `qa_agent/analysis_bridge.py` | new — the bridge |
| `qa_agent/report.py` | added `render_unexpected_error()` |
| `qa_agent/watch.py` | extracted `display_path()` (used by both the echo and the bridge label) |
| `qa_agent/__main__.py` | one line: callback is now `AnalysisBridge(root=root)` |
| `fsmonitor.py`, `runner.py`, `adapters.py`, `gitdiff.py` | **untouched** |

The monitor still imports only `dataclasses`, `pathlib`, and watchdog — it knows nothing about ruff, adapters, findings, or reports.

## Known behaviour, and why Part 4 exists
**Analysis runs on watchdog's observer thread**, inline and synchronous. Consequences, both acceptable for now and both stated deliberately:
- While ruff runs, further events wait their turn. They are queued by watchdog, not lost.
- **One save produces several analyses.** Windows reports content and metadata writes separately, so a single edit fires multiple `Modified` events, each triggering its own run — visible in the test output as repeated identical reports.

This is exactly the behaviour Part 4's debouncing will collapse. `analyze_paths(paths, label)` already takes a list, so the debouncer will call it once with a batch — wrapping the bridge rather than rewriting it.

## What success looks like — verified 2026-09-05
End-to-end against a live watcher performing real edits:

| Check | Result |
|---|---|
| Created file analyzed | real findings reported (`F401`, `E711`) |
| Modified file re-analyzed | previously-failing file reports clean after the fix |
| **Every analysis checked exactly 1 file** | `Checked: 1 file(s)` on every single report |
| **No full rescan** | two pre-existing files with real `F401` bugs sat in the project and were **never analyzed** |
| Rename | analyzed `renamed: src\renamed.py` — the destination; source never analyzed |
| Deleted | echoed as `Deleted:`, no report produced |
| Unsupported `.txt` | no echo, no analysis |
| Echo precedes each report | confirmed by output ordering |
| **ToolError containment** | ruff removed from PATH → error reported, exception not propagated |
| **Unexpected error containment** | injected `ValueError` → full traceback logged, watcher survived |
| Recovery | the analysis immediately after two failures worked normally |
| Race: destination already gone | no analysis, no crash |
| **Ctrl+C during live analysis** | exit 0, observer joined, thread count back to baseline |
| Phase A regression | all 6 pre-existing invocations, identical exit codes |
