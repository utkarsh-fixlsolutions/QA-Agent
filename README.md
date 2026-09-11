# QA Agent

A minimal, on-premise QA agent. Point it at a repository and it runs local tools (`ruff` + `pyright` + `mypy` for `.py`, `eslint` for `.js`/`.jsx`/`.ts`/`.tsx`, `shellcheck` for `.sh`) and reports **real, tool-verified issues** — file, line, severity, message, source tool. It never invents a finding: if no tool reports something, nothing is reported.

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

**For `.js`/`.jsx`/`.ts`/`.tsx` files:** requires Node.js/npm and a project-local ESLint install with a flat `eslint.config.(js|mjs|cjs)` — this is a prerequisite of the *project being analyzed*, not of qa_agent itself, so ESLint is never pinned in `requirements.txt`. Without it, these files report a clear `ToolError` rather than being silently skipped or falsely marked clean; `.py` files are unaffected either way. qa_agent only routes `.jsx`/`.ts`/`.tsx` files to ESLint the same way it already does `.js` — it does not bundle or configure a TypeScript/JSX parser itself, so meaningful `.ts`/`.tsx`/`.jsx` checking depends on the analyzed project's own eslint config having the parser/plugin it needs (e.g. `@typescript-eslint`); without one, ESLint reports its own honest parse error rather than qa_agent inventing or suppressing anything. See [docs/14](docs/14-first-multi-language-analyzer.md) for the original `.js` trade-offs (local-vs-global ESLint, ESLint 8 vs. 9 config compatibility) and [docs/18](docs/18-eslint-jsx-tsx-extension.md) for the extension to `.jsx`/`.ts`/`.tsx`.

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
python -m qa_agent discover <dir>          print a deterministic project profile (no AI, no analyzers)
python -m qa_agent discover <dir> --context  ...plus a derived repository-context summary (Phase F Part 2)
python -m qa_agent discover <dir> --runtime-plan  ...plus what should be runtime-tested and why (Phase G Part 1, never executed)
python -m qa_agent discover <dir> --execute-runtime-plan  ...actually runs the checks it knows how to (Phase G Part 2 - real subprocesses)
python -m qa_agent discover <dir> --diagnose  ...plus AI interprets why each failed check failed (Phase G Part 3 - opt-in AI)
python -m qa_agent discover <dir> --repair-runtime  ...attempts a verified repair for each diagnosed failure (Phase G Part 4 - the only flag that writes)
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

### Project discovery (`discover`)

```
$ python -m qa_agent discover .
Project Discovery Report

  Status:  success
  Elapsed: 0.033s
  Scanned: 93 file(s), 80 ignored

  Root:             D:\Major projects\lms-ai
  Repository type:  single-package
  Application type: full-stack

  Languages:
    - JavaScript  (evidence: eslint.config.mjs, jest.config.js, postcss.config.mjs)
    - TypeScript  (evidence: app/api/keep-alive/route.ts, ...)
  Frameworks:
    - Next.js  (evidence: next.config.ts, package.json)
    - React  (evidence: package.json)
  Package managers:
    - npm  (evidence: package-lock.json)
  ...
```

A read-only, deterministic pass over the repository's own files — languages, frameworks, package managers, build systems, and important files/directories, each with the real file(s) that prove it. No AI, no analyzers, no network, no subprocess; nothing is ever inferred without a file on disk backing it. See [docs/19](docs/19-project-discovery-engine.md).

Add `--context` for a derived, still fully deterministic repository summary built on top - an ordered architecture summary, a repository-layout description, factual constraints ("TypeScript project", "Requires Node", "Dockerized"), and a curated list of known gaps ("No CI configuration detected"). Not wired into the AI layer - produced and printable only, for now. See [docs/20](docs/20-repository-context-engine.md).

Add `--runtime-plan` for a deterministic, evidence-based list of *what should be tested and why* - server startup, API endpoints, middleware, Docker, CI, and more, each with a reason, its supporting evidence, a priority, a dependency order, and an expected result. Nothing is ever executed - this plans, it does not run anything. Only about half the categories a full runtime-QA vision would eventually cover are implemented yet (no database/auth/session/queue/cache detection exists in the deterministic layer today) - named honestly rather than faked. See [docs/21](docs/21-runtime-qa-planning-engine.md).

Add `--execute-runtime-plan` to actually run the checks this engine currently knows how to - server startup, build verification, test suite verification, environment validation, static asset verification. This is the one flag in this whole project that launches real subprocesses: a real build/test command run to completion, a real dev-server process started and always cleanly terminated afterward (never left running, on every exit path - ready, crash, or timeout). Every other planned check reports `not_implemented`, honestly, rather than being skipped silently or faked. See [docs/22](docs/22-runtime-execution-engine.md).

Add `--diagnose` (plus `--ai-provider ollama|mock`, `--ai-model`) for AI to interpret *why* each failed check failed - what happened, the most likely cause, and what to investigate next, clearly labeled "AI Diagnosis" and never presented as deterministic fact. Off by default: without this flag, zero AI calls are made. The deterministic result stays authoritative - AI is only ever asked about a check that already, genuinely failed/timed out/errored, and every claim it makes about a specific file or component is checked against the real evidence it was given before being accepted. See [docs/23](docs/23-runtime-failure-diagnosis-engine.md).

Add `--repair-runtime` to let a `DIAGNOSED` runtime failure enter the existing Phase D/E verified-repair pipeline, unmodified: an AI-proposed patch is applied only inside a temporary workspace, validated (both statically and, whenever the check type supports it, by actually re-running the real check itself against a materialized candidate copy - never just a linter), and written to the real repository only if a deterministic decision accepts it *and* the original runtime check is confirmed passing again afterward with a fresh, real re-run. A `DIAGNOSED` diagnosis is treated as a hypothesis, never as proof - an ambiguous, unresolvable, or unsafe repair target (a missing file, a `node_modules`/lockfile path, more than one named file) is refused rather than guessed. At most one repair attempt per failure; `--diagnose` alone never modifies a file - this is the only flag in the whole project that can. See [docs/24](docs/24-runtime-repair-integration.md).

## What it does not do

No automatic code edits · no CI integration · no dashboard · no cloud · no continuous full-repo scanning. AI (above) is optional, local-only (Ollama), and advisory-only - it never edits a file or applies a fix itself.

## Testing

```
python tests/run_all.py            everything, about a minute
python tests/run_all.py --quick    skip the stress suites
```

Twenty-nine suites covering the Phase A CLI against golden files, debounce semantics, every adapter's parsing and the generic runner mechanisms, the config system (including the optional AI section), the bridge contract, long-running stability, analyzer timeout and recovery, the full watch pipeline as a real process, multi-tool projects against real installs, filesystem storms, the AI provider/prompt/explanation/summary/fix/repair/CLI-integration layers, the deterministic Project Discovery Engine (languages, frameworks, package managers, repository/application type, ignore policy, symlinks, permissions, determinism), the Repository Context Engine built on top of it (architecture/layout summaries, constraints, known limitations, serialization), the Runtime QA Planning Engine built on top of that (evidence-gated checks, dependency ordering, serialization, never executes anything), the Runtime Execution Engine that actually runs the checks it can (real subprocess management, timeouts, guaranteed process-tree cleanup on every platform), and the AI Runtime Failure Diagnosis Engine built on top of that (grounded interpretation of a real failure, never a pass/fail authority, structured validation, secret redaction).

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
| [18](docs/18-eslint-jsx-tsx-extension.md) ESLint .jsx/.ts/.tsx extension | Phase C Part 2 addendum: widening ESLint's claimed extensions beyond `.js` |
| [19](docs/19-project-discovery-engine.md) project discovery | Phase F Part 1: a deterministic project-structure model - languages, frameworks, package managers - for future runtime-QA phases to consume |
| [20](docs/20-repository-context-engine.md) repository context | Phase F Part 2: a derived, still-deterministic repository summary (architecture, layout, constraints, known limitations) built on top of Part 1 - produced, not yet consumed by the AI layer |
| [21](docs/21-runtime-qa-planning-engine.md) runtime QA planning | Phase G Part 1: a deterministic plan of what should be runtime-tested and why, built on top of Part 2 - never executes anything; honestly scoped to only the evidence Parts 1-2 can actually detect today |
| [22](docs/22-runtime-execution-engine.md) runtime execution | Phase G Part 2: actually runs the checks it can (server startup, build/test, environment/static-asset verification) - real subprocesses, always cleaned up; everything else reports honestly as not-yet-implemented |
| [23](docs/23-runtime-failure-diagnosis-engine.md) runtime failure diagnosis | Phase G Part 3: the first real AI reasoning layer for runtime QA - interprets why a check failed, grounded in the real evidence, never the pass/fail authority; lives in `qa_agent/ai/`, not `qa_agent/runtime/` |
| [24](docs/24-runtime-repair-integration.md) runtime repair integration | Phase G Part 4: lets a diagnosed runtime failure enter the existing, unmodified Phase D/E verified-repair pipeline - a deterministic eligibility gate and file/line localizer, a real candidate runtime check re-run before any write, only ever `VERIFIED` after a real re-run of the real check passes |
| [step-log](docs/step-log.md) | Every step: goal, decisions, evidence, status |

## Status

**Phase C complete (Parts 1-7):** five real analyzers — `ruff` + `pyright` + `mypy` (`.py`), `eslint` (`.js`/`.jsx`/`.ts`/`.tsx` — extended from `.js`-only, see [docs/18](docs/18-eslint-jsx-tsx-extension.md)), `shellcheck` (`.sh`) — run through one unified, configurable engine, merging every tool's findings into one deterministically-ordered, conservatively-deduplicated report that never hides a succeeding tool's real findings behind a failing one's error, in both one-shot and watch mode. Validated against real external repositories and every configuration option; two real, silent bugs found and fixed along the way (docs/step-log.md).

**Phase D complete (Parts 1-6):** an optional, local-only AI layer (Ollama) built alongside the deterministic engine without changing it - a provider abstraction, prompt/context/response-validation building blocks, per-finding explanations, a run summary, advisory-only suggested fixes, and the CLI flags and `.qa-agent.json` section above that wire it all in. With AI disabled - the default - every report is still byte-for-byte identical to Phase C. Known limitations, deferred decisions, and trade-offs are listed in [docs/12](docs/12-architecture.md), [docs/17](docs/17-hardening-and-validation.md), and [docs/step-log.md](docs/step-log.md).

**Phase E complete (Parts 1-6):** a structured, advisory-only `RepairProposal` engine (`qa_agent/ai/repair.py`) that turns one existing analyzer Finding plus code context into a proposed replacement, explanation, and self-reported confidence, reusing Phase D's provider/prompt/validation building blocks; (`qa_agent/ai/workspace.py`) safe application of that proposal to an isolated temporary copy of its file - encoding and newline style preserved, the user's real project never touched; (`qa_agent/ai/validator.py`) reruns the real analyzers against that workspace copy to measure - never let the AI judge - whether a repair actually helped, classifying the result as improved/unchanged/worsened/validation_failed from analyzer output alone; (`qa_agent/ai/decision.py`) a pure, deterministic policy over that validation result deciding whether a repair is an eligible *candidate* (accept_candidate/reject/hold/validation_failed) - "accepted" never means the real project was modified; (`qa_agent/ai/apply.py`) the one module that finally writes an accepted repair to the real project, only after re-verifying every precondition against the file's current state, via backup-then-atomic-replace with automatic restore on failure; and (`qa_agent/ai/repair_loop.py`) the orchestrator that runs all of the above, once per finding, over a bounded iteration limit, with structured statistics and graceful continuation past any one finding's failure. Still no CLI or config surface, no interactive approval, and nothing reachable from the normal analyze or watch path. See [docs/step-log.md](docs/step-log.md).

**Phase F Part 1 complete:** a deterministic Project Discovery Engine (`qa_agent/project/`, `discover_project(root)`) that builds a structured, evidence-backed profile of a repository - languages, frameworks, package managers, build systems, repository/application type, and every important file/directory - each fact traceable to a real file on disk, nothing invented or inferred. No AI, no subprocess, no network; a single filesystem walk, reusing and extending the existing ignore policy. Read-only via `python -m qa_agent discover <path>`. Not consumed by anything yet - this is the foundation later runtime-QA phases will build on, not runtime testing itself. See [docs/19](docs/19-project-discovery-engine.md).

**Phase F Part 2 complete:** a Repository Context Engine (`qa_agent/project/context.py`/`context_builder.py`, `build_repository_context(project)`) that turns Part 1's `ProjectKnowledge` into a richer, still entirely deterministic `RepositoryContext` - an ordered architecture summary, a repository-layout description, factual constraints, and a curated, finite list of known limitations, plus lossless dict/JSON serialization. References `ProjectKnowledge` rather than duplicating it. Deliberately **not** wired into any AI prompt builder - `qa_agent/ai/` is entirely untouched by this part, confirmed by a direct source-grep test; that integration is an explicit, separate decision for a later phase. Read-only via `python -m qa_agent discover <path> --context`. See [docs/20](docs/20-repository-context-engine.md).

**Phase G Part 1 complete:** a Runtime QA Planning Engine (`qa_agent/runtime/`, `plan_runtime_qa(context)`) that converts a `RepositoryContext` into a deterministic, dependency-ordered `RuntimeQAPlan` - what should be tested, why, in what order, with what dependencies - never executing anything. 19 check types across server startup, API endpoints, middleware, frontend routes, Docker, CI, build verification, and framework-specific clusters for Next.js App Router and FastAPI, each backed by real evidence. Honestly scoped: roughly half the categories a full runtime-QA vision would eventually need (database/ORM, authentication, WebSockets, caching, queues, and more) have no evidence detector yet and are explicitly not faked - named as deferred work, not silently dropped. Read-only via `python -m qa_agent discover <path> --runtime-plan`. See [docs/21](docs/21-runtime-qa-planning-engine.md).

**Phase G Part 2 complete:** a Runtime Execution Engine (`qa_agent/runtime/executor.py`, `run_runtime_plan(plan, root, configuration=None)`) that actually runs the checks it knows how to - Server Startup, Build Verification, Test Suite Verification, Environment Validation, Static Asset Verification - and honestly reports every other planned check as `not_implemented`. The first part of this whole project that launches real subprocesses: real build/test commands run to completion, a real dev-server process started and always cleanly terminated afterward on every exit path (ready, crash, or timeout - including a real, Windows-specific process-tree-cleanup fix, since a shell wrapper's own `terminate()` alone leaves its real child process orphaned). Command discovery never guesses - only real, authored evidence (an npm `package.json` script, a real detected entry point) is ever run; anything else is skipped with the reason why. "Server Startup passing" means the process stayed alive without crashing, never that it answered a real request - this engine still makes no HTTP call of any kind. Read-only otherwise; opt-in via `python -m qa_agent discover <path> --execute-runtime-plan`. See [docs/22](docs/22-runtime-execution-engine.md).

**Phase G Part 3 complete:** an AI Runtime Failure Diagnosis Engine (`qa_agent/ai/diagnosis.py`, `diagnose_runtime_failure(s)`) - the first real AI reasoning layer for runtime QA. Interprets *why* a check that already, genuinely failed/timed out/errored did so - never whether it did; Phase G Part 2's own `RuntimeCheckResult.status` remains the sole authority, verified by a call-counting test that a PASS/SKIPPED/NOT_IMPLEMENTED check triggers zero AI calls. Every claim about a specific file or component is checked against the real evidence it was actually given before being accepted - an unsupported claim is rejected outright, not silently kept. Lives in `qa_agent/ai/`, not `qa_agent/runtime/` (which stays exactly as deterministic as it's always been) - reading `RuntimeExecutionResult`/`RepositoryContext` as input data, the same one-directional exception `validator.py` already established in Phase E. Opt-in via `python -m qa_agent discover <path> --diagnose`; zero AI calls without it. See [docs/23](docs/23-runtime-failure-diagnosis-engine.md).

**Phase G Part 4 complete:** Runtime Failure -> Verified Repair Integration (`qa_agent/ai/runtime_repair.py`, `repair_runtime_failure(s)`) - a thin layer that lets a `DIAGNOSED` runtime failure enter the existing, completely unmodified Phase D/E verified-repair pipeline; no second repair engine, workspace, validator, decision policy, or apply mechanism exists anywhere in this phase. A `DIAGNOSED` diagnosis is treated as a hypothesis, never proof, demonstrated concretely during dogfooding: a real, well-grounded, high-confidence diagnosis was still correctly refused by the deterministic eligibility gate for naming the same real file two different ways. `decide_repair()`'s own static-analysis verdict is used only as a regression guard; the real signal for a runtime repair is a genuine re-run of the actual runtime check (`npm run build`, not a linter) against a candidate copy materialized inside the same temporary workspace via cheap directory-entry links, never a slow full-tree copy. `ACCEPTED` and `VERIFIED` are never conflated - only a real, final re-run of the real, original check against the real repository can ever produce `VERIFIED`. At most one repair attempt per failure; opt-in via `python -m qa_agent discover <path> --repair-runtime` (implies `--diagnose`) - `--diagnose` alone never writes a file. See [docs/24](docs/24-runtime-repair-integration.md).
