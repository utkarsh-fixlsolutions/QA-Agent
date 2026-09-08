# Step 18 — ESLint Extension: `.jsx`/`.ts`/`.tsx` Routing

**Status:** IMPLEMENTED (2026-09-08).
**Phase:** C, Part 2 addendum — widens the scope [docs/14](14-first-multi-language-analyzer.md) deliberately deferred, on branch `feature/eslint-jsx-tsx-support`.
**Scope:** `ESLintAdapter.extensions` gains `.jsx`, `.ts`, `.tsx` alongside the existing `.js`. Routing only — no new dependency, no bundled TypeScript/JSX parser, no engine change. Real linting quality for the three new extensions is exactly as dependent on the *analyzed project's own* eslint config as `.js` linting already was.

## Why this was deferred, and why it's safe to do now

[docs/14 §5](14-first-multi-language-analyzer.md) scoped Part 2 to `.js` only, explicitly naming `.jsx`/`.ts`/`.tsx` as "natural, likely next steps, deliberately deferred" — TypeScript needing its own parser/plugin was the stated reason, and §13 ("Future scalability") predicted this exact follow-up. This step takes only the routing half of that: `ESLintAdapter` still does not parse or lint anything itself (`build_command()`/`parse()` are already extension-agnostic — neither needed a code change), it only decides which files get handed to the target project's own `eslint`. Whether that `eslint` can actually make sense of `.ts`/`.tsx` syntax is entirely up to that project's own `eslint.config` — the same boundary that already existed for `.js` (README's ".js files" prerequisite: Node/npm and a project-local ESLint install are the *analyzed project's* responsibility, never qa_agent's).

## What was measured (real ESLint 9.39.5, this machine, `tests/fixtures/eslint`'s own install)

A minimal flat config with no `files` key (the same shape `ESLINT_CONFIG` already uses across the test suite) was run directly against `.jsx`/`.ts` files:

| Input | Target project's config | Real ESLint output |
|---|---|---|
| Plain JS-compatible code, `.jsx` extension | No `files` pattern naming `.jsx` | `severity: 1`, `"File ignored because no matching configuration was supplied."` — a warning-level advisory, not silence, not a crash |
| Same, `.ts` extension | No `files` pattern naming `.ts` | Identical advisory |
| Plain JS-compatible code, `.jsx`/`.ts` extension | `files: ["**/*.js", "**/*.jsx", "**/*.ts", "**/*.tsx"]` added | Lints normally — real `no-unused-vars` findings, identical shape to a `.js` finding |
| Real JSX syntax (`<div>...</div>`), opted in, no JSX-aware parser | same opt-in config | `fatal: true`, `"Parsing error: Unexpected token <"` |
| Real TypeScript syntax (`interface`), opted in, no TS parser | same opt-in config | `fatal: true`, `"Parsing error: The keyword 'interface' is reserved"` |

Every one of these five shapes was already handled, unmodified, by `ESLintAdapter.parse()` — the "file ignored" advisory and the "fatal parse error mid-batch" cases are the exact two already proven in [docs/14 §5](14-first-multi-language-analyzer.md) and its own fixtures (`node_modules`-ignored files, a fatal syntax error not aborting a batch). Nothing new needed to be taught to `parse()` to handle `.jsx`/`.ts`/`.tsx` correctly.

**The practical consequence, stated plainly:** simply pointing qa_agent at a JSX/TypeScript project whose `eslint.config` was written before this extension (i.e. it never expected ESLint to be asked about `.jsx`/`.ts`/`.tsx` at all) will surface a `File ignored because no matching configuration was supplied` warning per such file, not real findings — an honest signal that the *target project's own config* isn't opted in yet, not a qa_agent bug. A project whose config already lints `.jsx`/`.ts`/`.tsx` (with or without `@typescript-eslint`) gets real findings immediately, with zero qa_agent-side configuration.

## End-to-end verification (real CLI, real files, no fakes)

Ran the actual built CLI (`python -m qa_agent <dir>`) against a real scratch project containing `eslint.config.js` (opted in to all four extensions), `plain.jsx`, `plain.ts` (JS-compatible content) and `real.jsx`, `real.ts` (genuine JSX/TypeScript syntax):

```
Checked: 5 file(s)
Tools:   eslint

Findings (6):
  ...plain.jsx:1  [error] no-unused-vars: 'unusedThing' is assigned a value but never used.  (eslint)
  ...plain.jsx:2  [error] no-unused-vars: 'greet' is defined but never used.  (eslint)
  ...plain.ts:1   [error] no-unused-vars: 'unusedThing' is assigned a value but never used.  (eslint)
  ...plain.ts:2   [error] no-unused-vars: 'greet' is defined but never used.  (eslint)
  ...real.jsx:2   [error] Parsing error: Unexpected token <  (eslint)
  ...real.ts:1    [error] Parsing error: The keyword 'interface' is reserved  (eslint)
```

All 5 files routed to `eslint` and checked (previously, `.jsx`/`.ts` files in this same project would have been silently skipped — not even attempted — since no adapter claimed them). Exit code 1 (findings present), matching every other real-findings run.

## Files changed

- `qa_agent/adapters.py` — `ESLintAdapter.extensions` widened to `frozenset({".js", ".jsx", ".ts", ".tsx"})`; class and module docstrings updated to state the routing-only boundary. `build_command()`, `parse()`, `ok_exit_codes`, `use_shell` — all untouched.
- `tests/regression/test_multi_analyzer.py` — the adapter-contract test's `eslint.extensions` assertion updated to the new set.
- `tests/integration/test_multi_language.py` — the live watch-mode banner assertion updated (`eslint (.js)` → `eslint (.js, .jsx, .ts, .tsx)`, sorted order confirmed against real output).
- `README.md` — the tool summary line, the `.js`-files prerequisite paragraph, the docs table, and the Phase C status line all updated to name the four extensions and this document.
- **Untouched:** `qa_agent/runner.py`, `qa_agent/report.py`, `qa_agent/__main__.py`, `qa_agent/watch.py`, every other adapter, and every other test suite. The watch-mode banner and `watched_extensions` set are both already built generically from `adapter.extensions` ([docs/14 §6](14-first-multi-language-analyzer.md)) — this is the same "zero-`__main__.py`-change" property Part 2 first proved, holding again for a third and fourth extension on an already-registered adapter.

## Verified

`python tests/run_all.py` — **23/24 suites** (the one failure is `test_watch_pipeline.py`'s pre-existing, unrelated timing flake, documented in every phase since Part 4 and unrelated to this change).

## Deferred

Bundling `@typescript-eslint`'s parser/plugin (or any JSX/TypeScript-aware parser) as a qa_agent-side default, auto-detecting or scaffolding a target project's `eslint.config` for these extensions, and any `.mjs`/`.cjs` extension (a separate, already-named-but-still-deferred gap from docs/14 §"Discoveries") — none of these were needed to make routing correct and honest, and adding any of them now would be exactly the scope creep docs/14 §5 originally drew the line against.

**Status: Phase C Part 2 addendum CLOSED.**
