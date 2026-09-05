# Step 2 — Tool Selection (Python, v1)

**Status:** Confirmed
**Date:** 2026-09-05

## 0. How the agent decides which tool to run (extensibility approach)
The agent is not hardcoded to "always run ruff." It uses a small, general mechanism so a second language can be added later as "one new entry," not a redesign:

- **A file-extension → tool-adapter registry.** Conceptually:
  ```
  ".py"  -> ruff adapter
  ".js"  -> (unmapped in v1)
  ".ts"  -> (unmapped in v1)
  ```
- Each **adapter** bundles exactly two things: (1) the CLI command shape to invoke its tool, and (2) a parser that normalizes that tool's raw output into one shared shape: `{file, line, severity, message, tool}`. Every future adapter (Python, JS/TS, Go, …) must produce this same shape — the rest of the agent (collecting results, building the report) never needs to know which language it's looking at.
- **v1 registers exactly one adapter: Python → ruff.** Nothing else is wired up yet.
- **A file with no registered adapter is not silently dropped, and not flagged with a fake issue.** It appears in the report under an explicit "skipped — no tool configured for this file type" note, so it stays visible without inventing a finding for it.
- This registry/adapter shape is the only piece of "architecture" introduced in this step, and exists solely to keep the tool choice below from being a dead end. The runner, CLI entrypoint, and report writer are still undesigned — that's later steps.

## 1. Tool choice: ruff (confirmed)
- Single free, MIT-licensed, standalone binary (Rust-based) — no separate runtime dependency once installed, works fully offline.
- Combines what would otherwise be multiple tools (pyflakes-, pycodestyle-equivalent checks, and more) behind one command and one output format.
- Native JSON output — no fragile text-scraping/regex needed to parse results.
- No config file required to get useful default output (default rule set: `E4`, `E7`, `E9`, `F`).
- **Alternative considered and rejected for v1:** `flake8` (+ optionally `mypy`) — two tools instead of one, plain-text output only (needs custom regex parsing), slower. Ruled out purely on the "as minimal as possible" constraint, not capability.
- **Explicitly out of scope for v1:** type checking (`mypy`) — ruff doesn't type-check. Possible future addition as a second Python adapter; not decided now.

## 2. CLI invocation shape
```
ruff check <path-or-files...> --output-format=json --exit-zero
```
- `<path-or-files...>` — the file list or path the agent was given (matches Step 1's "targeted mode" / "path mode").
- `--output-format=json` — machine-readable output. **Verified** against the actually-installed version (`ruff 0.15.14`) — this is the current flag name, not assumed from memory.
- `--exit-zero` — ruff normally exits with code 1 when it finds issues; this keeps exit code 0 so "issues found" isn't confused with "the tool itself failed to run." The agent should still separately detect real tool failures (e.g. a nonzero exit even with `--exit-zero` set, or stderr output).
- No `--fix` is ever passed — v1 is check-only, per Step 1 ("no auto-fixing").

## 3. JSON → report field mapping
Verified by actually running `ruff check --output-format=json --exit-zero` against a scratch file with three deliberate, distinct issues (unused import, `== None` comparison, unused variable) — not assumed from documentation or memory. Real sample output:

```json
{
  "code": "F401",
  "location": { "row": 1, "column": 8 },
  "end_location": { "row": 1, "column": 10 },
  "filename": "C:\\...\\ruff_verify_sample.py",
  "message": "`os` imported but unused",
  "severity": "error",
  "url": "https://docs.astral.sh/ruff/rules/unused-import"
}
```

| Report field | Source |
|---|---|
| file | `filename` |
| line | `location.row` (column available at `location.column` if needed later) |
| message | `message` (optionally prefixed with `code`, e.g. `F401: ...`) |
| severity | `severity` — **this field genuinely exists** in the installed ruff version (0.15.14); no severity is being invented. Tested against the default rule set and against `--select ALL`: every finding returned `"error"` in both cases. If a real `"warning"` (or other value) shows up once the agent is actually run against varied real-world code, that's just passed through as-is — nothing here manufactures a fake split. |
| source tool | constant `"ruff"` |

## 4. Install requirements
- Python already assumed present (target codebases are Python; also needed to eventually run the agent itself).
- `ruff` installed and on PATH — via `pip install ruff` (or `pipx install ruff`, or Astral's standalone installer). Verify with `ruff --version`. (Confirmed already installed on this machine: `ruff 0.15.14`.)
- No network access needed at run time, no per-project config file needed for v1.

## 5. "Done when" checklist for Step 2
- [x] Extension→adapter registry approach documented, with the shared `{file, line, severity, message, tool}` contract and the "unsupported file → explicit skip note, not silence, not a fake finding" rule
- [x] ruff choice justified in docs (with the one alternative considered)
- [x] Exact CLI command shape verified against a real installed ruff version, not assumed
- [x] JSON field names in the mapping table verified against real output
- [x] Install requirements documented
- [x] `docs/02-tool-selection.md` written
- [x] `docs/step-log.md` updated with a Step 2 entry
- [x] `README.md` status line bumped
- [x] No agent code, no project scaffolding created — docs-only (verification used a scratch file outside this repo)
