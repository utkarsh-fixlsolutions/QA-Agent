# QA Agent

A minimal, on-premise QA agent. Point it at a repository and it runs local tools (currently `ruff`) and reports **real, tool-verified issues** — file, line, severity, message, source tool. It never invents a finding: if no tool reports something, nothing is reported.

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

Requires Python 3.8+. Pulls in `watchdog` (filesystem events) and `ruff` (the analyzer).

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

Seven suites covering the Phase A CLI against golden files, debounce semantics, the bridge contract, long-running stability, analyzer timeout and recovery, the full watch pipeline as a real process, and filesystem storms.

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
| [step-log](docs/step-log.md) | Every step: goal, decisions, evidence, status |

## Status

**Phase B complete.** The agent is a working one-shot CLI and a stable always-on watcher, verified by 7 automated suites and measured under filesystem storms. Known limitations and deferred work are listed in [docs/12](docs/12-architecture.md).
