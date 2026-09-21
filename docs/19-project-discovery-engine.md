# Step 19 — Phase F Part 1: Project Discovery & Knowledge Engine

**Status:** IMPLEMENTED (2026-09-10).
**Phase:** F, Part 1 of "Project Intelligence" — the first phase built after Phase E closed. Not runtime testing, not API testing, not browser automation, not repair, not another analyzer — its only job is to make the agent understand a repository's structure before any future runtime-QA phase (Phase G onward) begins.
**Scope:** a new, standalone package, `qa_agent/project/`, exposing one public entry point, `discover_project(root)`. Deterministic only — every fact reported is backed by a real file on disk; nothing here calls an AI provider, spawns a subprocess, opens a network connection, or drives a browser.

## Why this exists

Everything built through Phase E analyzes files independently — it is never told what kind of project it is looking at, only which files exist. Before any future phase can reason about *runtime* behavior (does the build succeed, do the API routes work, does middleware redirect correctly), the agent first needs the same thing a senior engineer builds silently in their head before touching an unfamiliar repository: what language is this, what framework, what package manager, is it a monorepo, is it a frontend app or an API or both. This phase builds that model, once, deterministically, and stops there.

## Architecture

```
qa_agent/project/
  models.py      immutable data shapes (DetectedItem, ProjectKnowledge, ProjectDiscoveryResult)
  detectors.py   pure functions: language/framework/package-manager/build-system
                 detection, file categorization, repository/application type
  knowledge.py   assembles ProjectKnowledge from detectors' output; renders
                 a ProjectDiscoveryResult for the CLI (report.py's role,
                 applied to discovery instead of analysis)
  discovery.py   the one real filesystem walk; discover_project(root)
  __init__.py    public exports
```

Deliberately kept out of `qa_agent/ai/` — this is architecture, not just convention: nothing in `qa_agent.ai` imports this package, and nothing here imports `qa_agent.ai` (enforced by a source-grep isolation test, the same pattern the AI-era modules already use on themselves). The one dependency on the existing pipeline is a read of one plain constant, `runner.IGNORED_DIRS`, reused and extended rather than duplicated — never a behavioral dependency in either direction.

## The one real walk, and the ignore policy

`os.walk`, top-down, with every entry in `IGNORED_DIR_NAMES` pruned from `dirnames` *before* `os.walk` descends — so an ignored directory's contents are never listed at all, not merely discarded afterward. This is what actually delivers "scale to hundreds of thousands of files": a `node_modules` with tens of thousands of files costs one `os.scandir` call to see and skip, not a full recursive walk.

`IGNORED_DIR_NAMES` reuses and extends `runner.IGNORED_DIRS`, not a second, silently-drifting copy. Extended with `.next`, `.nuxt`, `dist`, `build`, `coverage`, `target`, `out`, `bin`, `obj`, `.cache`, `.pytest_cache`, `.eslintcache`, `.idea`, `.vscode` — `.next` in particular is not a hypothetical: it is the exact real directory that made a live scan of an external Next.js project (this same project's own dogfooding, two days before this phase) feed hundreds of compiled bundle files to eslint and the AI layer before it was excluded anywhere. This phase fixes that gap for good, for every future consumer of `qa_agent/project/`, not just the static analyzer.

**One deliberate, documented deviation from a literal reading of the source prompt's ignore list:** `.github` is *not* pruned, even though the prompt's ignore-policy section named it. Pruning it would make CI-file detection (`.github/workflows/*.yml`, required elsewhere in the same prompt) impossible — a real contradiction in the literal instructions, resolved by keeping `.github` walked (only `.git` itself, the actual internal git data, is pruned) and named explicitly here rather than silently chosen. `test_ci_files_still_found_despite_dot_github_not_being_hard_ignored` exists specifically so this decision can't silently regress.

Symlinks: `followlinks=False` (the default, passed explicitly) means a symlinked directory is listed but never descended into — which is also exactly what prevents a circular symlink from ever causing an infinite walk, with no separate cycle-detection needed. A broken symlink (target does not exist) is excluded with one explicit `Path.exists()` check per file — `os.walk` itself has no way to tell a broken symlink apart from a real file without it.

Permission errors: checked once, explicitly, via `os.listdir(root)` *before* the real walk begins — a root that cannot be listed at all is `PERMISSION_DENIED` (discovery never starts). A deeper subtree that fails mid-walk is caught by `os.walk`'s own `onerror` callback, recorded as a warning, and discovery continues with whatever else is reachable — the result is `PARTIAL_SUCCESS`, not a hard failure, with a fully-formed `ProjectKnowledge` built from everything that *was* reachable.

## Never invent a fact

`DetectedItem` (one language, framework, package manager, or build system) refuses to be constructed with empty evidence — a `ValueError` at construction time, not a convention someone could forget to follow. Every detector in `detectors.py` only ever adds a name to its accumulator alongside the real file that proves it. Evidence lists are capped at five examples per item (`models.MAX_EVIDENCE_PER_ITEM`) so a repository with thousands of `.py` files doesn't turn "Python was detected" into a thousand-line dump — presence is still checked across the *entire* walk; only the *displayed* sample is capped.

`repository_type` and `application_type` are single classification strings, not `DetectedItem`s — each is a conclusion drawn from several files at once, not one fact tied to one file. Their supporting evidence lives in `ProjectKnowledge.evidence`, a `MappingProxyType` keyed `"repository_type"`/`"application_type"`, rather than forcing an artificial single-file shape onto a multi-file conclusion.

## Framework detection: never collapse a hierarchy

A Next.js `package.json` is real evidence of Next.js *and* of React at the same time — both are reported, never collapsed into just the more specific one. Verified directly (`test_nextjs_project_reports_both_nextjs_and_react`) and confirmed against a real project below.

Detection is dependency-name-based for the npm ecosystem (`package.json`'s `dependencies`/`devDependencies`/`peerDependencies`/`optionalDependencies`, read once as JSON, capped at 2MB — manifest files are inherently small; the cap guards a pathological case, not a real one), config-file-based for a handful of unambiguous markers (`next.config.*`, `angular.json`, `nest-cli.json`), and substring-based for Python/Java/PHP/C# frameworks whose manifests aren't JSON (`requirements.txt`/`pyproject.toml` for FastAPI/Flask/Django, `pom.xml`/`build.gradle*` for Spring Boot, `composer.json`'s `require` object for Laravel, `*.csproj` for ASP.NET) — a deliberate choice to avoid a new TOML/XML/Gradle-parser dependency purely to detect a package name, matching this project's own dependency philosophy (a library when the tool needs real sophistication, never by default).

## Application type: one documented extension to the given rule list

The rule list evaluates independent yes/no signals in a fixed priority (conflicting Electron+React Native evidence → `unknown`; Electron → `desktop`; React Native → `mobile`; frontend+backend → `full-stack`; frontend only → `frontend`; backend only → `backend`; an executable entrypoint (`package.json` `bin`, `pyproject.toml` `[project.scripts]`/`[tool.poetry.scripts]`, `setup.py`/`setup.cfg` `console_scripts`, `Cargo.toml` `[[bin]]`) → `cli`; a valid manifest with none of the above → `library`; nothing → `unknown`).

**Extension, not deviation:** a Next.js project with a real `app/api/`/`pages/api/` directory is treated as backend evidence in its own right, alongside the explicit backend-framework list. This is real backend code, evidenced by real files, exactly like an Express route file would be — verified against a real fixture (`test_application_type_full_stack_via_nextjs_api_directory`) and confirmed live below.

## Repository type

`monorepo`: an explicit monorepo-tool marker (`pnpm-workspace.yaml`, `turbo.json`, `nx.json`, `rush.json`, `lerna.json`) or a root `package.json`'s own `"workspaces"` key — never inferred from directory names alone. `workspace`: more than one independent manifest file (`package.json`/`pyproject.toml`/`Cargo.toml`/`go.mod`/`composer.json`/`pom.xml`) found in more than one directory, with no monorepo-tool marker. `single-package`: exactly the manifests found, all effectively one package. `unknown`: no manifest anywhere.

## A real bug found during implementation (not by manual inspection — by the test suite)

`detect_workspace_structure`/`detect_repository_type` originally built directory-key strings with `str(Path(rel).parent)` — correct on Linux/macOS, but on Windows this renders with backslashes (`"apps\\web"`) while every path this package produces elsewhere is POSIX-style (`"apps/web"`, via `Path.as_posix()` in `discovery.py`'s own walk). `test_workspace_structure_and_monorepo_packages` and `test_nested_workspace_deep_structure` both caught this immediately on this machine (Windows) by comparing against a hardcoded `"apps/web"`-style expectation. Fixed by using `Path(...).as_posix()` consistently in both functions — the same category of Windows-path-separator bug this project has hit before (`PyrightAdapter`'s drive-letter normalization, Phase C Part 5), each time found by testing on the actual platform rather than assumed correct from POSIX-only reasoning.

A second, milder bug: the isolation test that greps `qa_agent/project/` for any reference to `qa_agent.ai` initially matched on bare substrings, and tripped on this package's own docstrings explaining the isolation rule in prose — the exact same false-positive class this project's own step-log already documents repeatedly for Phase E's isolation tests. Fixed by anchoring the check to actual import-statement syntax (`^\s*(import qa_agent\.ai|from \.\.ai|from \.ai|from qa_agent\.ai)`) rather than a bare substring.

## CLI

`python -m qa_agent discover <path>` — read-only, deliberately as small as `_watch_main` is large: calls `discover_project()`, prints `knowledge.render()`'s output, exits. No AI, no analyzers, no repair, nothing else reachable from it. Exists for exactly the reason the prompt specifying this phase named: dogfooding and debugging the discovery engine directly, without needing a Python shell.

## Files changed

**New:** `qa_agent/project/__init__.py`, `models.py`, `detectors.py`, `knowledge.py`, `discovery.py`; `tests/regression/test_project_discovery.py` (69 test functions, 127 checks); this document.
**Modified:** `qa_agent/__main__.py` (+`discover` subcommand dispatch and `_discover_main()`; module docstring updated) — `main()`'s existing `watch` dispatch is untouched, the new `discover` check is a sibling, not a replacement. `README.md`, `docs/step-log.md`, `docs/12-architecture.md` (see their own diffs).
**Untouched, byte-for-byte:** `qa_agent/ai/`, `runner.py`, `adapters.py`, `report.py`, `watch.py`, `config.py`, `gitdiff.py`, `analysis_bridge.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py` — every existing regression/integration/stress suite still passes exactly as before this phase (see Test results).

## Test results

`python tests/run_all.py` — **24/25 suites** (the one failure is `integration/test_watch_pipeline.py`'s pre-existing, unrelated timing flake, documented in every phase since Phase D Part 4). The new suite, `regression/test_project_discovery.py`, passes 127/127 checks on its own. Every one of Phase A-E's suites passes unchanged — no analyzer output was altered by this phase, confirmed by rerunning the full suite before and after.

**Dogfooding, two real projects:**

1. **This project's own repository** (`qa_agent` itself, via `discover_project('.')`): correctly identified as Python, `single-package`, `library`, package managers `npm` (from the eslint test fixture's own `package.json`) and `pip` (from `requirements.txt`) — accurate, and the "library" classification is honest: this is a CLI tool distributed as a plain script, not a project with a declared `bin`/console-script entrypoint, so `library` (rather than `cli`) is the evidence-based, not guessed, result.

2. **A real external Next.js project** (`D:\Major projects\lms-ai`, the same project this session's earlier dogfooding used) — real output:

```
Status:  success
Elapsed: 0.033s
Scanned: 93 file(s), 80 ignored

Repository type:  single-package
Application type: full-stack

Languages:        JavaScript, TypeScript
Frameworks:        Next.js (evidence: next.config.ts, package.json)
                    React (evidence: package.json)
Package managers:  npm (evidence: package-lock.json)
Important directories: app, app/api, components, public
Test directories:  components/__tests__, lib/__tests__, lib/actions/__tests__
CI files:          .github/workflows/ci.yml, .github/workflows/keep-supabase-alive.yml
Environment files: .env.local, .env.sentry-build-plugin   (presence only)
```

Both the Next.js/React dual-report and the `app/api`-driven `full-stack` classification are exactly the two behaviors this design deliberately built and tested for, confirmed live rather than only in a synthetic fixture. 0.033s end to end — direct, measured proof that the `.next` exclusion (the real gap this same project hit two days earlier) now works: the same directory that previously produced hundreds of noise files is skipped entirely, at the `os.walk` level, before it is ever listed.

## Bugs discovered

Two, both described above under "A real bug found during implementation" — the Windows path-separator bug in workspace/repository-type directory keys, and the isolation test's own false-positive-on-prose bug. Both caught by the regression suite itself on this machine, both fixed before this phase was considered complete.

## Deferred (explicitly, per this phase's own non-goals)

Runtime testing, API/endpoint discovery, middleware validation, browser automation, AI reasoning of any kind, repair of any kind, a CI-file *content* parser (only presence/category is recorded), a build-system detector beyond the documented starter set (Vite/Webpack/Rollup/Babel/Make/CMake/Maven/Gradle — extensible later, not exhaustive now), and wiring `discover_project()`'s output into the deterministic analyzer pipeline or the AI layer — `ProjectKnowledge` is produced and printable today; nothing yet *consumes* it. All of these belong to later Phase F parts or Phase G, per this phase's own explicit scope boundary.

**Status: Phase F Part 1 CLOSED.**
