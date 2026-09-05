# Step 3 — Minimal Runner

**Status:** Confirmed
**Date:** 2026-09-05

The smallest working implementation of what Steps 1 and 2 defined: take paths in, dispatch `.py` files to ruff, parse its JSON, print a structured report.

## Layout
```
qa_agent/
├── __init__.py
├── __main__.py    CLI entrypoint (argparse -> run -> render -> exit code)
├── adapters.py    Finding shape, ToolError, SEVERITY_POLICY, RuffAdapter, ADAPTERS registry
├── runner.py      input handling -> adapter dispatch -> subprocess call -> findings
└── report.py      renders the report text
```
Four small modules, each with one job. `adapters.py` is the only place that knows a tool exists; adding a language later means registering one more adapter, nothing else changes.

## How to run
```
cd D:\Working\Qa-Agent
python -m qa_agent <path> [<path> ...]
```
Paths may be individual files, directories (walked recursively), or a mix — matching the "targeted mode" / "path mode" from [01-definition.md](./01-definition.md) section 4. Scope always comes from the caller; the agent never widens it on its own.

Directory walks skip `.git`, `.venv`, `venv`, `__pycache__`, `.mypy_cache`, `.ruff_cache`, `node_modules` — noise, not findings.

**Exit codes:** `0` ran cleanly, no findings · `1` ran cleanly, findings reported · `2` a tool failed to run, or no usable input.

## Behaviour
- **Dispatch:** `.py` → ruff. Any other extension is listed under "Skipped — unsupported file type, not checked", never checked silently and never given a fake finding.
- **Invocation:** exactly the Step 2 command, `ruff check <files> --output-format=json --exit-zero`, batched at 200 files per call (Windows caps a command line near 32k characters).
- **Severity:** ruff's own reported `severity` is passed through verbatim. `SEVERITY_POLICY` in `adapters.py` is an empty, explicit `{rule code: level}` map you can add to if you want to reclassify specific rules yourself — deliberately empty by default so nothing is invented, and prefix matching is not attempted so every override stays auditable.
- **Tool failure ≠ findings:** `--exit-zero` means findings alone never cause a nonzero exit, so any nonzero exit, a missing `ruff` binary, or unparseable output raises `ToolError` and prints "tool error — no code issues were reported" to stderr with exit 2. A broken tool can never masquerade as a clean run or as issues.
- **Offline:** the only outbound call is `subprocess` to the local ruff binary. No networking imports exist anywhere in `qa_agent/` (verified by grep).

## What success looks like — verified 2026-09-05
Run against fixtures in a scratch directory (not committed to this repo):

| Check | Result |
|---|---|
| `.py` file with 3 known issues | 3 findings, exit 1 — `F401` line 1, `E711` line 5, `F841` line 7 |
| Cross-check vs raw `ruff check --output-format=json` | identical file/line/code/severity for all 3 — no drift, no invention |
| Clean `.py` file | `Findings: no issues found.`, exit 0 |
| Directory with `.py` + `.txt` | `.txt` under Skipped with reason `no tool configured for '.txt'`, exit 1 |
| Nonexistent path | listed under "Not found", exit 2 |
| `ruff` unreachable on PATH | `tool error - no code issues were reported`, exit 2, zero findings printed |

Sample output:
```
QA Agent report
  Run at:  2026-09-05 11:24:44
  Input:   ...\has_issues.py
  Checked: 1 file(s)
  Tools:   ruff

Findings (3):
  ...\has_issues.py:1  [error] F401: `os` imported but unused  (ruff)
  ...\has_issues.py:5  [error] E711: Comparison to `None` should be `cond is None`  (ruff)
  ...\has_issues.py:7  [error] F841: Local variable `unused` is assigned to but never used  (ruff)
```

## Requirements
Python 3.7+ and `ruff` on PATH (`pip install ruff`; verified against ruff 0.15.14). No other dependencies, no config file, no network access.
