# Step 51 — Monorepo Package-Manager Ancestor Detection

**Status:** IMPLEMENTED (2026-09-17).
**Goal:** the user reported the agent "broken" against a real pnpm-workspace project (`apps/web` inside a monorepo) - discovery found all 26 real API endpoints correctly, but every single one was skipped: `server status: skipped (no JS package manager detected for this project)`. Reproduced directly (not guessed) by running `python -m qa_agent discover <path> --api-test` against the real project.

## Root cause (found by reading the code, not speculation)

`detect_package_managers` (`project/detectors.py`) only ever scans **downward** from whatever root it is given, matching lockfile basenames anywhere under it. In a real pnpm/yarn/npm workspace, a sub-package (`apps/web`) commonly has **no lockfile of its own at all** - the one real lockfile (`pnpm-lock.yaml`) lives at the workspace root, one or more directories *above* the package being tested. Pointing the agent directly at `apps/web` (a completely normal thing to do - it's the real, independently-runnable app) therefore produced zero package-manager evidence, even though a real lockfile genuinely existed just outside the scanned root - `discover_server_start_command` (`api_qa/server.py`) had no fallback for this and skipped the server entirely.

A second, related bug found while fixing the first and verifying end-to-end: `web/server.py`'s `_npm_install` was hardcoded to run literal `npm install` regardless of which package manager was actually evidenced - wrong for any pnpm/yarn project (plain npm does not understand pnpm's workspace linking and can corrupt an already-working install).

A third, found only once the first two were fixed and the server could actually attempt to start: the evidenced manager (`pnpm`) was not actually installed on the machine's own PATH at all (confirmed via `where.exe pnpm` in the real shell the server runs in) - a real, previously-unreachable failure mode, since nothing ever got far enough to hit it before.

## Design

**`qa_agent/api_qa/server.py`**

- `_ancestor_js_package_manager(root)` (new): walks upward from `root`, checking each ancestor's own immediate directory entries (never recursive) for one of the same real lockfile basenames `detect_package_managers` already trusts. Stops at the first match, at the first ancestor that itself contains a `.git` directory (the real repository boundary - never searched past it, so an unrelated lockfile from some enclosing directory outside the actual repo is never mistaken for real evidence), or after 6 levels.
- `_js_package_manager(project, root=None)`: unchanged behavior when `root` is omitted; when given, falls back to `_ancestor_js_package_manager(root)` only after the existing downward-scanned evidence (`project.package_managers`) comes up empty.
- `discover_server_start_command` now passes `root` through to `_js_package_manager`.
- `_prefer_installed_manager_binary(command)` (new): a package manager's binary is only strictly required for its own `install` - *running* an already-defined `package.json` script against `node_modules` that already exists on disk works identically through any of them. If the evidenced manager's own binary is not actually on PATH, substitutes `npm` (which ships with any Node.js install) to run the same script, rather than reporting a real, already-discovered, already-runnable server as unstartable. Never invents a command when no fallback binary exists either - the original, honest failure still surfaces downstream exactly as before.

**`web/server.py`**

- `_npm_install(pkg_dir, manager="npm")`: now runs `<manager> install`, not a hardcoded `npm install`.
- Its one call site now passes `_cmd[0]` (the manager `discover_server_start_command` itself already found real evidence for) instead of always defaulting to npm.

## Verified

`tests/regression/test_api_qa.py` (+5 new checks, part of the file's existing 166/166): ancestor lockfile detection finding a real pnpm workspace root two directories up; the search correctly refusing to cross a `.git` boundary into an unrelated enclosing directory; `_prefer_installed_manager_binary` falling back to npm when the evidenced manager isn't on PATH, keeping the evidenced manager when it is, and never inventing a command when neither is available.

**Real end-to-end, against the actual project that surfaced this** (`D:\Working\Testing-Purpose\qaai\apps\web` - a real pnpm-workspace Next.js/Supabase app, 26 real API routes, GET/POST/PATCH/DELETE, dynamic `[id]` segments):

- Before: `26 skipped (real evidence)` - `no JS package manager detected for this project`, every single call.
- After: server started, all 26 endpoints actually called - `3 Working, 2 Failing, 10 Not Working, 11 Skipped` (skips now for real, individually-explained reasons: ambiguous multi-segment paths, no constructable body even after an empty-body probe). Caught **real bugs in the target project itself**: `GET /api/issues`/`GET /api/plans` returning a real 400 body with a genuinely useful message (`"Missing required parameter: project_id"`), and a real `Module not found: Can't resolve '../../../../../services/runner/lib/test-data-seeder.js'` import error (visible in the captured server log) breaking nearly every POST/DELETE route's compilation.

`python tests/run_all.py --quick` run after this change; see the session's own follow-up report for its result.

## Explicitly not done

- No attempt to fix the target `qaai` project's own broken import or missing-required-param validation - those are real findings *for* that project, not this tool.
- No change to how `detect_package_managers` itself works for the general discovery report (`qa_agent discover <path>` without `--api-test`) - the ancestor walk is scoped specifically to `discover_server_start_command`'s own "can a server actually be started" question, not to the broader project-profile evidence.
- Still no support for a workspace-aware install (e.g. running `pnpm install` from the real workspace root rather than the package directory) when `node_modules` does not already exist - left as a known next step if a fresh, never-installed pnpm-workspace upload turns out to need it.
