# QA Agent

A minimal, on-premise QA agent: given a repo path or a set of changed files, it runs local free tools (linters, type checkers, static rules) and reports real, tool-verified issues in a structured report.

No paid APIs. No cloud LLMs. No continuous background scanning — it runs on demand, on a schedule, or reacts to changes.

This project is being built **one small, testable step at a time**. Nothing beyond the current step is implemented or designed yet.

## Docs
- [docs/01-definition.md](docs/01-definition.md) — Step 1: mission, scope, and success criteria for v1.
- [docs/02-tool-selection.md](docs/02-tool-selection.md) — Step 2: tool choice (ruff), CLI shape, JSON→report field mapping, extensibility approach.
- [docs/03-minimal-runner.md](docs/03-minimal-runner.md) — Step 3: the working runner — how to run it, behaviour, verified results.
- [docs/04-git-diff-input.md](docs/04-git-diff-input.md) — Step 4: git-diff mode — both input modes, git commands used, failure cases.
- [docs/05-report-file.md](docs/05-report-file.md) — Step 5: `--output` — saving the report as Markdown.
- [docs/step-log.md](docs/step-log.md) — running log of every step, decisions made, and open questions.

## Status
**Step 5 (Report file) — closed.** The agent checks explicit paths or git-changed files, and can save the report as Markdown. Next step not yet defined.

## Usage
```
python -m qa_agent <path> [<path> ...]     check given files/directories
python -m qa_agent --git-diff              check only files git reports as changed
python -m qa_agent --git-diff <REF>        ...compared against a ref
python -m qa_agent <input> --output r.md   also save the report as Markdown
```
Run from the project root. Requires `ruff` on PATH (`pip install ruff`); `--git-diff` also requires `git` and a repository. Exit codes: `0` no findings, `1` findings, `2` tool/input/write error.

Step docs: [03 runner](docs/03-minimal-runner.md) · [04 git-diff](docs/04-git-diff-input.md) · [05 report file](docs/05-report-file.md).
