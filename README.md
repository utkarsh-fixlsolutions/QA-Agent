# QA Agent

A minimal, on-premise QA agent. Point it at a repository and it runs local tools (`ruff` + `pyright` + `mypy` for `.py`, `eslint` for `.js`, `shellcheck` for `.sh`) and reports **real, tool-verified issues** — file, line, severity, message, source tool. It never invents a finding: if no tool reports something, nothing is reported.

Runs entirely on your own machine. No paid APIs, no cloud services, no network access at any point.

**Two ways to use it:**
- **One-shot** — check paths, or just what git says you changed.
- **Watch mode** — leave it running while you work; it analyzes each file as you save it.

## Install

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Requires Python 3.8+. Pulls in `watchdog` (filesystem events), `ruff`, `pyright`, and `mypy` (the three Python analyzers).

**For `.js` files:** requires Node.js/npm and a project-local ESLint install with a flat `eslint.config.(js|mjs|cjs)` — this is a prerequisite of the *project being analyzed*, not of qa_agent itself, so ESLint is never pinned in `requirements.txt`. Without it, `.js` files report a clear `ToolError` rather than being silently skipped or falsely marked clean; `.py` files are unaffected either way. See [docs/14](docs/14-first-multi-language-analyzer.md) for the full set of trade-offs (local-vs-global ESLint, ESLint 8 vs. 9 config compatibility).

**For `.sh` files:** requires [ShellCheck](https://www.shellcheck.net/) on `PATH` — no pip/npm distribution, install it yourself (or use `tests/fixtures/shellcheck/fetch.ps1` for the test suite only). Without it, `.sh` files report a clear `ToolError`, exactly like a missing ruff or eslint.

## Configuration

Optional `.qa-agent.json` in the project root (or an ancestor of it) - none needed for the defaults above. See [docs/16](docs/16-configuration-system.md).

```json
{
  "analyzers": ["ruff", "eslint"],
  "ignore": ["build", "vendor"],
  "min_severity": "error"
}
```

## Usage

```
python -m qa_agent <path> [<path> ...]     check given files/directories
python -m qa_agent --git-diff              check only files git reports as changed
python -m qa_agent --git-diff <REF>        ...compared against a ref
python -m qa_agent <input> --output r.md   also save the report as Markdown
python -m qa_agent watch <dir>             analyze files as you save them (Ctrl+C to stop)
```

Exit codes: `0` no findings / clean shutdown · `1` findings reported · `2` tool, input, or write error.

### AI enrichment (optional)

Findings stay 100% tool-generated always; AI, when turned on, only explains, summarizes, or suggests advisory fixes for findings that already exist - it can never invent, suppress, or change one. Off by default; output is unaffected unless you opt in.

```
--ai                 enable AI (all of --ai-explain/--ai-summary/--ai-fix, unless given individually)
--ai-explain          explain each finding
--ai-summary          an AI-written run summary
--ai-fix               a suggested fix per finding - advisory only, never applied
--ai-model <model>     override the configured model
--ai-provider <name>   "ollama" (default, needs a local Ollama server) or "mock" (tests)
```

Or via `.qa-agent.json` (CLI flags win over this when both are given):

```json
{
  "ai": {
    "enabled": true,
    "provider": "ollama",
    "model": "qwen2.5-coder:latest",
    "explain": true,
    "summary": true,
    "suggest_fixes": false
  }
}
```

If the provider is unreachable, times out, or replies with something unusable, the AI section is silently omitted - the deterministic report always completes.

### Watch mode output

```
--------------------------------------------------
Batch #2   15:40:51

Files changed:
  - src/model.py (created)
  - src/recognize.py (created)

Findings (1):
  src/recognize.py:1  [error] F401: `os` imported but unused  (ruff)
```

One report per burst of changes: rapid saves collapse into a single analysis, and only changed files are analyzed — never the whole repository.

## What it does not do

No automatic code edits · no CI integration · no dashboard · no cloud · no continuous full-repo scanning. AI (above) is optional, local-only (Ollama), and advisory-only - it never edits a file or applies a fix itself.

## Testing

```
python tests/run_all.py            everything, about a minute
python tests/run_all.py --quick    skip the stress suites
```

Eighteen suites covering the Phase A CLI against golden files, debounce semantics, every adapter's parsing and the generic runner mechanisms, the config system (including the optional AI section), the bridge contract, long-running stability, analyzer timeout and recovery, the full watch pipeline as a real process, multi-tool projects against real installs, filesystem storms, and the AI provider/prompt/explanation/summary/fix/CLI-integration layers.

**Some integration suites need real tool installs, once:** `npm install` inside `tests/fixtures/eslint`, and `powershell -File tests/fixtures/shellcheck/fetch.ps1` (both gitignored, same as this project's own `.venv` for Python and pyright). Tests that need them skip cleanly and visibly if this hasn't been done — the rest of the suite runs regardless.

## Documentation

**Start here:** [docs/12-architecture.md](docs/12-architecture.md) — pipeline diagram, module map, event flow, shutdown sequence, testing methodology, known limitations, troubleshooting.

The numbered documents are a build log: each records what was decided at that step and why.

| Documents | Covering |
|---|---|
| [01](docs/01-definition.md) definition · [02](docs/02-tool-selection.md) tool selection | Phase A: scope, and choosing ruff |
| [03](docs/03-minimal-runner.md) runner · [04](docs/04-git-diff-input.md) git-diff · [05](docs/05-report-file.md) report file | Phase A: the working CLI |
| [06](docs/06-watch-mode.md) watch mode · [07](docs/07-filesystem-events.md) events · [08](docs/08-incremental-analysis.md) incremental | Phase B: the watcher |
| [09](docs/09-debouncing.md) debouncing · [10](docs/10-live-reporting.md) live reporting | Phase B: batching and output |
| [11](docs/11-long-running-stability.md) stability · [12](docs/12-architecture.md) architecture | Phase B: hardening and overview |
| [13](docs/13-multi-analyzer-foundation.md) multi-analyzer foundation · [14](docs/14-first-multi-language-analyzer.md) ESLint · [15](docs/15-unified-reporting.md) unified reporting · [16](docs/16-configuration-system.md) configuration | Phase C: the multi-language engine, its tools, merging their output into one report, and configuring all of it per project |
| [17](docs/17-hardening-and-validation.md) hardening & validation | Phase C: production validation, a real bug found and fixed, current capabilities and limitations |
| [step-log](docs/step-log.md) | Every step: goal, decisions, evidence, status |

## Status

**Phase C complete (Parts 1-7):** five real analyzers — `ruff` + `pyright` + `mypy` (`.py`), `eslint` (`.js`), `shellcheck` (`.sh`) — run through one unified, configurable engine, merging every tool's findings into one deterministically-ordered, conservatively-deduplicated report that never hides a succeeding tool's real findings behind a failing one's error, in both one-shot and watch mode. Validated against real external repositories and every configuration option; two real, silent bugs found and fixed along the way (docs/step-log.md).

**Phase D complete (Parts 1-6):** an optional, local-only AI layer (Ollama) built alongside the deterministic engine without changing it - a provider abstraction, prompt/context/response-validation building blocks, per-finding explanations, a run summary, advisory-only suggested fixes, and the CLI flags and `.qa-agent.json` section above that wire it all in. With AI disabled - the default - every report is still byte-for-byte identical to Phase C. Known limitations, deferred decisions, and trade-offs are listed in [docs/12](docs/12-architecture.md), [docs/17](docs/17-hardening-and-validation.md), and [docs/step-log.md](docs/step-log.md).

**Phase E complete (Parts 1-6):** a structured, advisory-only `RepairProposal` engine (`qa_agent/ai/repair.py`) that turns one existing analyzer Finding plus code context into a proposed replacement, explanation, and self-reported confidence, reusing Phase D's provider/prompt/validation building blocks; (`qa_agent/ai/workspace.py`) safe application of that proposal to an isolated temporary copy of its file - encoding and newline style preserved, the user's real project never touched; (`qa_agent/ai/validator.py`) reruns the real analyzers against that workspace copy to measure - never let the AI judge - whether a repair actually helped, classifying the result as improved/unchanged/worsened/validation_failed from analyzer output alone; (`qa_agent/ai/decision.py`) a pure, deterministic policy over that validation result deciding whether a repair is an eligible *candidate* (accept_candidate/reject/hold/validation_failed) - "accepted" never means the real project was modified; (`qa_agent/ai/apply.py`) the one module that finally writes an accepted repair to the real project, only after re-verifying every precondition against the file's current state, via backup-then-atomic-replace with automatic restore on failure; and (`qa_agent/ai/repair_loop.py`) the orchestrator that runs all of the above, once per finding, over a bounded iteration limit, with structured statistics and graceful continuation past any one finding's failure. Still no CLI or config surface, no interactive approval, and nothing reachable from the normal analyze or watch path. See [docs/step-log.md](docs/step-log.md).
