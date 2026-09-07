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

No auto-fixing · no LLM · no CI integration · no dashboard · no cloud · no continuous full-repo scanning.

## Testing

```
python tests/run_all.py            everything, about a minute
python tests/run_all.py --quick    skip the stress suites
```

Twelve suites covering the Phase A CLI against golden files, debounce semantics, every adapter's parsing and the generic runner mechanisms, the config system, the bridge contract, long-running stability, analyzer timeout and recovery, the full watch pipeline as a real process, multi-tool projects against real installs, and filesystem storms.

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

**Phase C complete (Parts 1-7).** Five real analyzers — `ruff` + `pyright` + `mypy` (`.py`), `eslint` (`.js`), `shellcheck` (`.sh`) — run through one unified, configurable engine, merging every tool's findings into one deterministically-ordered, conservatively-deduplicated report that never hides a succeeding tool's real findings behind a failing one's error, in both one-shot and watch mode. Validated in Part 6 against six real external repositories (Python, JavaScript, and mixed), every configuration option, and workloads from a handful of files to 2,000 - which found and fixed one real, silent bug (a large batch could exceed `cmd.exe`'s command-line limit and lose findings with no error reported). Part 7 added mypy as a third independent `.py` analyzer - proving three tools can share one extension, not just two - and found and fixed a second real, silent bug the same way, before it ever reached a test (mypy's own path formatting was inconsistent within a single run, which would have broken deterministic file ordering). Verified by 12 automated suites (107 checks in the multi-analyzer suite alone). Known limitations, deferred decisions, and trade-offs are listed in [docs/12](docs/12-architecture.md), [docs/17](docs/17-hardening-and-validation.md), and [docs/step-log.md](docs/step-log.md).
