# QA Agent — Step Log

Running record of each step in the build: what was decided, what was produced, and any open questions carried forward. One entry per step, newest at the bottom.

---

## Step 1 — Definition (2026-09-05)

**Goal:** Write a one-page definition of the QA agent's mission, scope, and success criteria. No code, no architecture, no tool selection yet.

**Constraints given:**
- On-premise only, no paid APIs, minimal (one step at a time), must work on real code/diffs, prefer local free tools, optional local LLM later, react-on-demand not continuous scanning, do not invent findings.

**Produced:** [docs/01-definition.md](./01-definition.md)

**Decisions made:**
- v1 scope = single repo, single run, single report. No LLM, no auto-fix, no CI/dashboard.
- Input can be either a specific changed-file list (diff-driven) or an explicit path — caller always decides scope, agent never self-triggers a full scan.
- Output is a structured report (file, location, message, severity, source tool); exact format (text/Markdown/JSON) deferred to a later step.

**Open questions — resolved same day:**
- **Language target → Python.** Rationale: ruff is a single, free, config-free, JSON-output static binary — fewer moving parts than a JS/TS lint+typecheck stack, best fit for the "as minimal as possible" constraint. Not a permanent lock-in; the design stays language-agnostic and other languages can be added as tool adapters later.
- **Target repo → no existing repo yet.** User has real projects on GitHub to point this at once it's built. Until then, a small scaffolded test repo (with a few deliberate, known issues) will be used to verify the agent as it's built.

**Status: Step 1 CLOSED.**

---

## Step 2 — Tool Selection (2026-09-05)

**Goal:** Pick the exact local tool(s) for Python v1, define the CLI invocation shape and the JSON→report field mapping, and record install requirements. Documentation only — no agent code, no project scaffolding.

**Constraints given:** On-premise, no paid APIs, minimal, Python first, local free tools only, no LLM, no full agent implementation, no architecture beyond what's needed for tool choice.

**Feedback on first draft of this step's plan:** it picked ruff but didn't say how the agent would ever handle another language — would have been a dead end requiring a redesign at the next language. Revised to add one deliberate, minimal piece of architecture: a file-extension → tool-adapter registry (see doc §0), so v1 still only wires up Python/ruff but the shape doesn't need reworking later.

**Produced:** [docs/02-tool-selection.md](./02-tool-selection.md)

**Decisions made:**
- Extension→adapter registry is the dispatch mechanism: each adapter = (CLI command shape) + (parser to shared `{file, line, severity, message, tool}` shape). v1 registers only `.py` → ruff. Unmapped files are explicitly marked "skipped — no tool configured," never silently dropped or fake-flagged.
- Tool: **ruff**, chosen over `flake8`(+`mypy`) purely on minimalism (one binary, one JSON format, no config needed) — not a capability gap. Type checking (`mypy`) explicitly deferred, not decided.
- CLI shape: `ruff check <files> --output-format=json --exit-zero`. Verified live against the actually-installed `ruff 0.15.14` — not assumed from memory.
- JSON mapping verified against real output from a scratch file with 3 deliberate issues (unused import, `== None`, unused variable) in the session scratchpad — not written into the repo. Notable finding: this ruff version genuinely emits a `severity` field per finding (observed as `"error"` across the default rule set and `--select ALL`); no severity value is invented — it's passed through as-is, whatever ruff reports.
- Install requirement: Python + `ruff` on PATH (`pip install ruff`), no network access or per-project config needed at run time.

**Status: Step 2 CLOSED.**

**Next step:** Step 3 — minimal runner.

---

## Step 3 — Minimal Runner (2026-09-05)

**Goal:** Smallest working runner: accept file/directory paths, dispatch `.py` to the ruff adapter from Step 2, parse its JSON into `{file, line, severity, message, tool}`, print a structured report with findings, skipped files, and tool errors.

**Constraints given:** On-premise, no paid APIs/cloud LLMs, minimal, Python-only (ruff adapter only), no invented findings, no git hooks/watcher/other languages/LLM/auto-fix/UI, keep the project linear on top of Steps 1-2.

**Conflict raised and resolved before coding:** the step spec asked for a flat `"warning"` severity, citing "the rule from Step 2" — but that rule only existed in a superseded plan draft. Step 2's live verification found ruff genuinely emits a real per-finding `severity`, and flattening it would have contradicted the step's own "only what ruff actually reports" constraint. User asked whether we could classify severities ourselves; resolved as: pass ruff's value through verbatim by default, plus an explicit, empty-by-default `SEVERITY_POLICY` map for opting into our own per-rule classification later.

**Produced:** [docs/03-minimal-runner.md](./03-minimal-runner.md) and the `qa_agent/` package (`__main__.py`, `adapters.py`, `runner.py`, `report.py`).

**Decisions made:**
- Four small modules, one job each; `adapters.py` is the only module aware that a specific tool exists, keeping the Step 2 registry contract real rather than notional.
- Entrypoint `python -m qa_agent <paths...>`. Exit codes: 0 clean, 1 findings, 2 tool error / no usable input.
- Directory walks skip `.git`, `.venv`, `venv`, `__pycache__`, `.mypy_cache`, `.ruff_cache`, `node_modules`.
- Ruff calls batched at 200 files each — Windows caps a command line near 32k characters.
- Tool failure is structurally separated from findings: `--exit-zero` means findings never cause a nonzero exit, so nonzero exit / missing binary / unparseable JSON all raise `ToolError` → stderr message + exit 2, never a finding.

**Verified (all six checks passed, 2026-09-05):** file with 3 known issues → 3 findings matching raw ruff output exactly (F401 L1, E711 L5, F841 L7, all severity `error`); clean file → "no issues found" exit 0; mixed directory → `.txt` under Skipped, not a fake finding; nonexistent path → "Not found", exit 2; ruff removed from PATH → tool error, exit 2, zero findings printed; no networking imports anywhere in `qa_agent/`. Fixtures lived in the session scratchpad, not this repo.

**Status: Step 3 CLOSED.**

**Next step:** Step 4 — git-diff / changed-files input mode.

---

## Step 4 — Git-diff / Changed-files Input (2026-09-05)

**Goal:** Optional mode so the agent checks only files git reports as changed, without replacing path mode. Reuse the Step 3 pipeline; only file *selection* is new.

**Constraints given:** On-premise, no paid APIs/cloud LLMs, minimal, build on existing code, keep the project linear, no watcher/hooks/other languages/LLM/auto-fix/UI, no invented findings.

**Produced:** [docs/04-git-diff-input.md](./04-git-diff-input.md) and `qa_agent/gitdiff.py`.

**Decisions made:**
- **What "changed" means (default):** `git diff --name-only HEAD` (staged + unstaged in one command) **plus** `git ls-files --others --exclude-standard` (untracked). Untracked files are included deliberately — a newly written `.py` file is absent from `git diff` entirely, yet is exactly what most needs checking. Ignored files stay excluded.
- Optional `--git-diff <REF>` for working tree vs a ref; the ref is verified with `rev-parse --verify` first, so a mistyped ref or a path passed in the ref slot gives a clear message instead of leaking git's raw usage text (found and fixed during testing).
- Repo root resolved via `git rev-parse --show-toplevel`, so the mode works from any subdirectory; `-z` everywhere so odd filenames survive.
- No-commits-yet repos fall back to `--cached` plus untracked, since `HEAD` does not exist.
- **Deleted files** are filtered before dispatch and surfaced under Skipped as `deleted in working tree` — passing one to ruff would otherwise fail the whole run with a tool error.
- Modes are mutually exclusive and both are required-one-of; argparse's `error()` already exits 2, matching the existing input-failure code.
- Reuse over rewrite: `gitdiff.py` is the only git-aware module. `runner.run()` gained one optional `extra_skipped` argument; `report.render()` now takes a source label and a reason-agnostic Skipped header; `adapters.py` untouched.

**Verified (2026-09-05, scratch git repo):** path mode regression unchanged; modified + untracked + staged `.py` all checked (3 findings, exit 1); changed `.txt` skipped; **a committed file with a real `F401` bug left untouched was correctly not reported** (proven ruff would flag it if asked — scope discipline holds); deleted file skipped without a tool error; nothing-changed → clean message, exit 0; outside a repo → clear error, exit 2; `--git-diff HEAD~1` → 5 files, exit 1; invalid ref → clear error, exit 2. Agent still passes its own dogfood check.

**Status: Step 4 CLOSED.**

**Next step:** Step 5 — save report to a file.

---

## Step 5 — Save Report to a File (2026-09-05)

**Goal:** Optional `--output <path>` writing the same report to a file, so results can be kept or shared. Terminal behaviour unchanged.

**Constraints given:** On-premise, no paid APIs/cloud LLMs, minimal, reuse runner/adapters/report, one linear agent, no watcher/hooks/other languages/LLM/auto-fix/UI, terminal output must still work, no changes to finding/git/adapter logic.

**Produced:** [docs/05-report-file.md](./05-report-file.md); changes confined to `report.py` and `__main__.py`.

**Decisions made:**
- **Markdown only; JSON deliberately not built.** The step permitted optional JSON, but nothing consumes it yet, so building it now would mean maintaining a second format with no reader. Left as a small later addition (`render_json()` beside `render_markdown()`, same data) if a real need appears.
- **One data model, two renderers.** `render()` (terminal) and `render_markdown()` (file) are two presentations of the same `RunResult` — not a second report pipeline. `runner.py`, `adapters.py`, `gitdiff.py` are untouched by this step.
- **Print first, then write.** A failed write never costs the results: findings still go to stdout, the write error goes to stderr.
- **Write failure exits 2, outranking the `1` for findings** — the requested output was not produced, so a script must not see the run as successful.
- Parent directories are not auto-created; a missing directory is reported rather than silently made. Files written as UTF-8.
- `|` inside a message is escaped so it cannot silently corrupt the Markdown table.

**Verified (2026-09-05):** no-`--output` behaviour identical to Step 4; path mode and `--git-diff` both write correct files; clean run writes an honest `No issues found.`; **terminal-vs-file parity checked by parsing findings and skipped entries out of both and comparing as sets — identical**; missing directory, directory-as-path, and findings-plus-unwritable-path all give clear errors at exit 2 with results preserved; pipe escaping keeps table rows valid. Agent still passes its own dogfood check.

**Incidental:** a `SyntaxWarning` (invalid escape sequence) introduced while writing `_cell()` was caught and fixed — the agent's own check on `report.py` is clean.

**Status: Step 5 CLOSED.**

**Next step:** not yet defined — awaiting user direction.
