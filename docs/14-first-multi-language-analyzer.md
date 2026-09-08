# Step 14 — First Multi-Language Analyzer: ESLint

**Addendum (2026-09-08):** the `.js`-only scope below (§5) was widened to also claim `.jsx`/`.ts`/`.tsx` — routing only, no engine change, no bundled TypeScript/JSX parser. See [docs/18](18-eslint-jsx-tsx-extension.md). Everything below is left exactly as it was decided and verified at the time; §5's own "natural, likely next steps, deliberately deferred" line was the plan this addendum carried out.

**Status:** IMPLEMENTED (2026-09-07). Design approved with the two amendments below adopted; implementation, tests, and dogfooding complete.
**Phase:** C, Part 2 of "Multi-Language Intelligence."
**Scope:** register exactly one new adapter, ESLint, for `.js` only. The point of this part is not "support JavaScript" — it is to prove, with a real second tool, that Part 1's architecture ([docs/13](./13-multi-analyzer-foundation.md)) scales the way it was designed to. Pyright, Mypy, and ShellCheck are discussed only in §12 (future scalability), never implemented here.

All empirical claims below were measured today (2026-09-07) against the actually-installed toolchain on this machine — Node v26.5.0, npm 11.17.0, ESLint 9.39.5 — not assumed from memory or documentation. Every command is reproducible; several are quoted verbatim where the exact output matters.

## Amendments adopted at implementation time

1. **No `RunResult.tool_errors` / partial-result-propagation change (§7).** Explicitly deferred, using the design's own stated off-ramp: execution isolation (every adapter genuinely attempted independently) is real and tested; result propagation (a failing tool still discards an already-succeeded tool's findings on the way out of `run()`) is unchanged from Part 1, and is a real, still-open, explicitly tracked known limitation — see `test_deferred_known_limitation_failure_hides_other_tools_findings` in `tests/integration/test_multi_language.py`, which exists specifically so this can't silently rot into "forgotten."
2. **A third generalization, not anticipated by the design, was required and added: `use_shell`.** Found only by actually trying to launch a real, locally-installed ESLint on Windows — see "Discoveries made during implementation" below. Not a redesign of anything described above; it follows the exact same adapter-declares-a-fact-the-runner-consumes-generically pattern as `ok_exit_codes` and the cwd anchoring.

## Discoveries made during implementation (not anticipated by the design above)

Two real, load-bearing facts surfaced only by actually building and dogfooding this, not by the design review:

**1. npm installs ESLint as a `.cmd` shim on Windows, not a real executable — `subprocess.run(shell=False)` cannot launch it at all, regardless of PATH correctness.** Confirmed directly: even given ESLint's exact, verified-correct, resolved path, `subprocess.run(["eslint.cmd", ...])` raises `FileNotFoundError [WinError 2]` — Windows' `CreateProcess` can only directly launch a real PE executable (ruff.exe qualifies; a `.cmd` batch script does not, `shell=True` or an explicit `cmd.exe /c` wrapper is required). Fixed with a third adapter-declared attribute, `use_shell` (`False` for ruff's real `.exe`, `True` for eslint), consumed generically by `_invoke()` exactly like `ok_exit_codes`.

That fix immediately caused a second, worse bug, also found only by testing the actual failure path: **with `shell=True`, a genuinely-missing `eslint` never raises `FileNotFoundError` at all** — `cmd.exe` itself reports `"'eslint' is not recognized..."` as an ordinary exit code `1` with empty stdout. Since ESLint's own `ok_exit_codes` already includes `1` (findings present is normal), this would have been silently misread as "ran cleanly, no findings" — inventing a false clean bill of health for code that was never actually checked, exactly the failure mode this project has refused since Step 1. Fixed by checking `shutil.which(command[0])` explicitly before ever invoking the subprocess, for every adapter uniformly (not eslint-specific) — deterministic and locale-independent, unlike parsing `cmd.exe`'s own (localizable) error text would have been.

**2. `.js`-only scope, while correctly excluding TypeScript, also excludes `.mjs`/`.cjs`.** Found while dogfooding against `unjs/ofetch`, a real, modern, ES-module JavaScript project — its actual `.js`-equivalent source files are `.mjs`. `.mjs`/`.cjs` are plain JavaScript; ESLint lints them exactly like `.js`, with no extra parser or plugin, so the design's own stated reason for deferring TypeScript (needs `@typescript-eslint`'s parser and plugin) does not apply to them. **Deliberately left as `.js`-only for this part** rather than silently widened — it's a small, low-risk, easily-justified follow-up (`extensions = frozenset({".js", ".mjs", ".cjs"})`, nothing else changes), but it's still a scope change to a document that was implemented as approved; flagged for a quick explicit decision rather than folded in unasked. See the final implementation report for the concrete recommendation.

---

## 1. Current architecture (after Part 1)

```
Project
  |
  v
runner.run()  ---"which adapters claim this file?"--->  ADAPTERS (tuple)
  |                                                            |
  |                                                each: RuffAdapter
  |                                                            |
  +--------- every matching adapter invoked, failures isolated per adapter ---<---+
  |
  v
RunResult (unified findings)
```

**Responsibilities, as Part 1 left them** ([runner.py](../qa_agent/runner.py), [adapters.py](../qa_agent/adapters.py)):

- **`runner.py`** collects paths, builds `extension -> [adapters]` fresh each call (`_extension_index()`), groups matching files per adapter, invokes each adapter's command as a subprocess (batched, 60s-timeout, per-adapter `try/except ToolError` so one adapter's failure cannot stop another's files from being analyzed), and aggregates everything into one `RunResult`.
- **`adapters.py`** holds the contract: each adapter declares a `name`, the `extensions` it claims, `build_command(files)`, and `parse(stdout)`. `ADAPTERS` is a tuple; more than one adapter may claim the same extension.
- **`report.py`, `watch.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py`, `analysis_bridge.py`** — all already tool-agnostic, unaffected by Part 1, unaffected here.

**Why the engine already supports multiple analyzers:** this is not aspirational — it was built and tested in Part 1. `_extension_index()` already produces a list per extension, not a single tool. The invocation loop already iterates `sorted(batches.items())` and already wraps each adapter's call in its own `try/except`. Registering a second adapter today would already dispatch to it, batch its files separately, and invoke it independently. That part of the design holds up unchanged.

**What still needs to change before ESLint can actually be added — found by testing ESLint directly, not by inspection:**

### Finding 1: `_invoke()`'s exit-code check is quietly ruff-shaped

Today: `if proc.returncode != 0: raise ToolError(...)`. This assumes exit code 0 means success and anything else means the tool itself failed. That's only true for ruff because ruff's adapter passes `--exit-zero` — a deliberate flag chosen in Step 2 specifically so exit code carries no information about findings, only about tool health.

ESLint has no equivalent flag (confirmed against its own `--help`):

```
$ eslint --help | grep -iE "exit|warning|zero"
  --max-warnings Int               Number of warnings to trigger nonzero exit code - default: -1
  --exit-on-fatal-error            Exit with exit code 2 in case of fatal error - default: false
  --pass-on-no-patterns            Exit with exit code 0 in case no file patterns are passed
```

Measured directly:

| Scenario | Exit code | stdout |
|---|---|---|
| Clean file | `0` | valid JSON, `errorCount: 0` |
| File with only warnings (`eqeqeq`) | `0` | valid JSON, `errorCount: 0, warningCount: 1` |
| File with a real error (`no-unused-vars`) | `1` | valid JSON, `errorCount: 2` |
| Batch with a fatal syntax error in one file | `1` | valid JSON for **every** file, including the broken one (`fatal: true` folded in as a message) |
| No `eslint.config.js` findable | `2` | empty; error on stderr |

So `0` and `1` both mean "the tool ran correctly, parse the output" — `1` is not a failure, it's ESLint's normal way of saying "there are errors in the code," exactly the information ruff already carries in its JSON. Only `2` (and anything else) is a genuine tool failure. Today's engine cannot tell the difference; it would misreport every ESLint run that finds anything as a **tool error**, discarding real findings.

### Finding 2: `_invoke()` never sets a subprocess working directory — ESLint needs one anchored inside the project

`subprocess.run()` in `_invoke()` inherits whatever directory the whole `qa_agent` process happens to be running in. This has never mattered for ruff, which this project deliberately chose as a **zero-config** tool (docs/02, section 3) — ruff never needs to discover a config file relative to any particular directory.

ESLint's flat config (`eslint.config.js`) is different: it is resolved by searching **upward from the process's current working directory**, the same way git finds `.git`. Measured directly:

```
# cwd = project root containing eslint.config.js -> works
$ eslint bad.js --format json          # succeeds

# cwd = a subdirectory of the project -> works (upward search, like git)
$ cd nested/deeper && eslint <absolute path to bad.js> --format json   # succeeds

# cwd = an unrelated directory, even with a fully-qualified absolute file path -> fails
$ cd / && eslint <absolute path to bad.js> --format json
Oops! Something went wrong! :(
ESLint couldn't find an eslint.config.(js|mjs|cjs) file.
exit: 2
```

Passing `--config <path>` explicitly does not fully fix this either — ESLint still evaluates file paths against a "base path" derived from cwd, and a mismatch produces `"File ignored because outside of base path."` (also measured).

This matters concretely for this project: **watch mode's process is not chdir'd into the watched directory.** `python -m qa_agent watch D:\Working\flask`, launched from `D:\Working\Qa-Agent`, leaves the process's cwd at `D:\Working\Qa-Agent` — exactly the "unrelated directory" failure case above. `--git-diff`, which this project explicitly supports running from any repo subdirectory (docs/04), lands in the *working* case (upward search from a subdirectory succeeds) — but only because it happens to.

Both findings point to the same conclusion: **the invocation contract in `_invoke()` has two hidden ruff-specific assumptions that Part 1 had no way to discover with only one tool registered.** This is the same category of gap Part 1 itself predicted (docs/13 §10: "deferred until a second real analyzer exists to design against") — now there is one.

---

## 2. Proposed architecture (after Part 2)

```
Project
  |
  v
runner.run()
  |
  +-- dispatch: extension -> [adapters]                (unchanged, Part 1)
  |
  +-- per adapter, per chunk:
  |     subprocess cwd = common ancestor of this chunk's files   (NEW, generic)
  |     success = proc.returncode in adapter.ok_exit_codes       (NEW, adapter-declared)
  |     on success -> adapter.parse(stdout)
  |     on failure -> ToolError, isolated to this adapter        (unchanged, Part 1)
  |
  v
RunResult (unified findings from RuffAdapter + ESLintAdapter)
```

Both new mechanisms are **generalizations of the existing invocation step**, not new pipeline stages. The loop shape, the dispatch mechanism, the chunking, the timeout, and the per-adapter isolation are exactly what Part 1 built. Nothing here adds a stage to the pipeline in docs/12's diagram.

---

## 3. Component responsibilities

| Component | Responsibility | Change from Part 1 |
|---|---|---|
| **Runner** (`runner.py`) | Collect paths, dispatch by extension, invoke each adapter (now: with a computed `cwd` and an adapter-declared success-code set), isolate failures per adapter, aggregate. | `_invoke()` gains two generalizations (§1, findings 1 and 2). Loop structure, chunking, timeout, dispatch: unchanged. |
| **RuffAdapter** (`adapters.py`) | Unchanged responsibility. | Gains one declared fact about itself: `ok_exit_codes = frozenset({0})` — formalizes what `--exit-zero` already guarantees today. No behavior change. |
| **ESLintAdapter** (`adapters.py`, new) | Declares `.js`, builds the `eslint --format=json` command for a batch, parses ESLint's JSON into `Finding` objects. Knows nothing about the runner, cwd computation, or watch mode. | New class, same shape as `RuffAdapter`. |
| **Reporting** (`report.py`) | Render `RunResult`. | See §7 — one additive decision (a `tool_errors` field) is proposed and needs explicit sign-off; everything else is unchanged. |
| **Watch mode, `__main__.py` composition** | Unchanged responsibility. | **Zero code changes required** — see §6. This is the direct, measurable proof of Part 1's stated success criterion. |

---

## 4. Dependency graph

```
__main__.py -----> runner.py -----> adapters.py
     |                                   ^
     +----> gitdiff.py -------------------|  (ToolError only)
     |
     +----> analysis_bridge.py --> runner.py --> adapters.py
     |
     +----> watch.py, fsmonitor.py, debouncer.py, live_report.py, report.py
```

Unchanged from Part 1 — same nodes, same edges. `ESLintAdapter` is new *content* inside `adapters.py`, not a new node. No file outside `runner.py` and `adapters.py` gains an import.

One structural asymmetry worth naming here rather than burying in trade-offs: ruff is a **qa_agent-owned dependency** — pinned in `requirements.txt`, installed into qa_agent's own `.venv`. ESLint cannot be pinned the same way. It must be whatever the **analyzed project** has installed (almost always a local `devDependency` in that project's own `node_modules`), because its config format, plugin set, and rule versions are specific to that project. This is a new *kind* of dependency for this project — external to qa_agent's own install, resolved per-target-project — not a gap in this design, but a real property of what "supporting a second language" means that's worth stating plainly (expanded in §11).

---

## 5. The ESLint analyzer

**Scope decision: `.js` only, not `.jsx`/`.ts`/`.tsx`.** TypeScript needs `@typescript-eslint`'s parser and plugin to lint meaningfully — a second package, a second config shape, and its own due-diligence pass this document hasn't done. Adding it now would blur Part 2's actual goal (prove the architecture with one real second tool) into "build out JS/TS tooling." `.jsx`/`.ts`/`.tsx` are natural, likely next steps, deliberately deferred — mirroring exactly how Part 1 deferred a second Python tool until there was a real reason to build one.

**Responsibilities:** build the exact CLI command for a batch of `.js` files; parse ESLint's JSON into `Finding` objects. Nothing else — no cwd computation (that's the runner's job now, §1 finding 2), no subprocess invocation, no error classification beyond what `parse()` already does for malformed output.

**Public interface** (same four members `RuffAdapter` has, plus the one new declared fact every adapter now provides):

- `name = "eslint"`
- `extensions = frozenset({".js"})`
- `ok_exit_codes = frozenset({0, 1})` — 0 clean, 1 findings present; both are successful runs whose output should be parsed.
- `use_shell = True` — found necessary during implementation, not anticipated here; see "Discoveries made during implementation" above. `RuffAdapter` declares `False` (a real `.exe`, unchanged behavior).
- `build_command(files)` — `["eslint", "--format=json", *files]`.
- `parse(stdout)` — see below.

**Command construction:** `eslint` is invoked assumed to be on `PATH`, exactly like ruff. No `npx eslint` fallback, no `node_modules/.bin/eslint` path discovery. This is a deliberate, evidence-based choice, not an oversight — see the performance measurements in §9: direct invocation measured **0.40s**, `npx eslint` measured **1.09s** (2.7x slower) for the identical single-version-check call, even with no network access needed (local install resolved). Given Part 1's constraint against unjustified abstraction, and that ruff already carries the identical "must be on PATH" assumption, ESLint gets the same one. The real-world cost of this choice (most JS projects don't put ESLint on global PATH) is named honestly in §11, not hidden.

**Parsing strategy (prose — this is exactly what `parse()` should do, not code):**

ESLint's `--format=json` output is a JSON array with one object per file: `{filePath, messages: [...], errorCount, warningCount, ...}`. Each entry in `messages` becomes one `Finding`:

- `Finding.file` = the message's file's `filePath` (already an absolute path, matching how ruff's `filename` field is also absolute in this project's usage — no cross-tool path reconciliation needed).
- `Finding.line` = the message's `line`. (Column is available but dropped — `Finding` has never carried a column, for either tool; not adding one now, matching Part 1's "don't redesign `Finding` unless justified" — it isn't, here.)
- `Finding.severity` — ESLint reports `severity` as an integer, `1` (warning) or `2` (error), per its own documented scale. Mapped to the words `"warning"`/`"error"` via a small fixed lookup. This is **decoding** the tool's own vocabulary into text, not inventing a value — the same distinction docs/02 already draws for ruff's `SEVERITY_POLICY` (reclassifying is opt-in and explicit; passing through what the tool actually said is the default). No `SEVERITY_POLICY`-equivalent is needed for ESLint in Part 2 — deferred until there's a real reason to reclassify a specific rule.
- `Finding.message` = `"{ruleId}: {message}"` when `ruleId` is present, else the bare `message` — literally the same conditional `RuffAdapter.parse()` already uses for ruff's `code`, reused for ESLint's `ruleId`. This same fallback naturally and correctly covers two real cases seen in testing: a fatal syntax error (`ruleId: null, message: "Parsing error: ..."`) and ESLint's own "file ignored" advisory (`ruleId: null`, §5 "skipped files" below) — both become ordinary, honest findings with no special-casing.
- `Finding.tool` = `"eslint"`.

A **fatal parse error in one file of a batch does not abort the batch** — measured directly: a batch of three files where one had a syntax error still returned valid JSON for all three, with the broken file's entry carrying one message with `fatal: true, ruleId: null, severity: 2`. `parse()` treats it exactly like any other message — it's real, tool-verified information ("this file could not be parsed"), not an invented finding.

**Error handling:**

- **Missing executable** (`eslint` not on PATH) — already fully handled by the existing generic `FileNotFoundError` catch in `_invoke()`. Zero ESLint-specific code needed.
- **Timeout** — already fully handled by the existing generic 60s-timeout catch. ESLint's measured latency (well under a second even for 100 files, §9) leaves the same wide margin ruff has; no per-adapter override is proposed (Part 1 explicitly deferred this until evidence demands it — this measurement doesn't).
- **Non-zero exit not in `ok_exit_codes`** (i.e., `2` — config missing, fatal crash, unmatched pattern) — routes through the existing generic `ToolError` formatting (`proc.stderr.strip() or proc.stdout.strip() or "no output"`). Verified the "no config" message lands on **stderr** (502 bytes, stdout empty), so the existing generic formatting surfaces ESLint's own real error text verbatim — genuinely useful, not generic. No ESLint-specific error-message code needed.
- **Invalid/unparseable output despite an "ok" exit code** — `ESLintAdapter.parse()` wraps its `json.loads()` in the same defensive `try/except json.JSONDecodeError -> raise ToolError` pattern `RuffAdapter.parse()` already uses. This `ToolError` is caught by the **same per-adapter `try/except` Part 1 already built** — no engine change needed for this case at all. Worth noting explicitly: Part 1's error isolation was built with no second tool to test it against; this is the first real case it protects against, and it already works.
- **Parser failure beyond a JSON-decode error** (e.g., a message dict missing an expected key) — `RuffAdapter.parse()` today does not defensively validate every field; it trusts ruff's documented JSON shape and only guards the outer `json.loads()` call. `ESLintAdapter.parse()` follows the same precedent, for consistency, not as a new risk. If ESLint's shape genuinely changed underneath us, a raw `KeyError` (not a `ToolError`) would propagate out of `run()` uncaught — this is an **existing, already-latent property of the architecture**, identical for ruff today, not introduced by this part. Named honestly here because the review explicitly asked about "parser failure" as a category — flagged as a candidate for a future hardening step, not something Part 2 needs to fix.
- **Configuration errors** (`eslint.config.js` not found) — this is the exit-`2` case above.

**Skipped files:** an extension `neither` adapter claims is already fully handled by the existing generic "no tool configured for X" skip path — unchanged. Files under `node_modules/` are **already filtered before either tool ever sees them** — confirmed both `IGNORED_DIRS` in `runner.py` and the default ignore set in `fsmonitor.py` already include `"node_modules"` (present since Phase A/B, unrelated to this part). The only way ESLint sees a `node_modules` file is if a caller explicitly names one (e.g. an un-gitignored file surfaced by `--git-diff`) — measured: ESLint does not error, it reports a low-severity advisory message (`"File ignored by default because it is located under the node_modules directory..."`, `severity: 1`), which `parse()` turns into an ordinary warning-level `Finding` — honest, not silent, not a crash.

**Working directory assumptions:** covered in §1 finding 2 and resolved generically in the runner (§6) — the adapter itself makes no cwd assumption; it receives absolute file paths and builds a command exactly like `RuffAdapter` does.

**`node_modules` interaction:** covered above (skipped files).

**Exit code behavior:** covered in §1 finding 1 and §7.

---

## 6. Integration — files changed

| File | Why it changes | What's added | What does *not* change |
|---|---|---|---|
| `qa_agent/adapters.py` | New tool to register; existing tool's contract formalized. | `RuffAdapter` gains `ok_exit_codes = frozenset({0})` (formalizes existing truth, zero behavior change). New `ESLintAdapter` class (§5). `ADAPTERS` becomes `(RuffAdapter(), ESLintAdapter())`. | `Finding`, `ToolError`, `SEVERITY_POLICY`, `RuffAdapter.build_command`/`parse` — all untouched. |
| `qa_agent/runner.py` | The two generalizations from §1. | `_invoke()`'s exit check becomes `if proc.returncode not in adapter.ok_exit_codes`. A new small, adapter-blind helper computes `subprocess.run(..., cwd=...)` from the common ancestor of the chunk's files (§1 finding 2), applied uniformly to every adapter's invocation — not ESLint-specific logic living in the runner. | `_extension_index()`, the dispatch loop, chunking, per-adapter error isolation, `RunResult`'s existing fields, `ANALYSIS_TIMEOUT_SECONDS` — all unchanged (pending the §7 decision, which is a separate, explicitly-flagged addition). |
| `qa_agent/__main__.py` | **Nothing required.** | — | The watch-mode banner builder already iterates `ADAPTERS` generically and groups by `adapter.name`/`adapter.extensions` (Part 1) — registering `ESLintAdapter` makes it print `"eslint (.js), ruff (.py)"` with **zero code change**. The `watched_extensions` set comprehension is likewise already generic and picks up `.js` automatically. This is the direct, literal proof of the success criterion: implement the contract, register the instance, done. |
| `README.md` | New external prerequisite. | A short paragraph: Node.js + a project-local ESLint install (with a flat `eslint.config.js`) are needed for `.js` files to be checked; documented as a prerequisite of the *target project*, not of qa_agent's own install. | The existing "Requires Python 3.8+... pulls in `watchdog` and `ruff`" line stays accurate for qa_agent's own dependencies — ESLint is never added to `requirements.txt` (it isn't a pip package). |

## Files that remain fully untouched

`report.py` (pending §7's one flagged decision), `watch.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py`, `analysis_bridge.py`, `gitdiff.py`, `__init__.py` — identical list to Part 1, still true. `tests/**`'s *existing* suites are untouched (new suites are additive, §10) — confirmed by the same grep-before-writing discipline used in Part 1: no existing test imports `ADAPTERS` by shape or asserts on `_invoke()`'s exit-code check internals.

---

## 7. Error isolation — and the one real decision this part surfaces

Walking through every category the review asked about, with the measured behavior behind each:

| Failure | Where it's caught | Isolated per adapter? |
|---|---|---|
| Missing executable | Existing generic `FileNotFoundError` catch | Yes (Part 1, unchanged) |
| Tool timeout | Existing generic `TimeoutExpired` catch | Yes (Part 1, unchanged) |
| Non-zero exit that's legitimate (ESLint `1`) | No longer an error — `ok_exit_codes` (§1 finding 1, new) | N/A — not a failure |
| Non-zero exit that isn't legitimate (ESLint `2`) | Existing generic `ToolError` formatting, now correctly gated by `ok_exit_codes` | Yes (Part 1, unchanged) |
| Invalid JSON despite an "ok" exit code | Adapter's own `parse()` raises `ToolError`, caught by Part 1's per-adapter `try/except` | Yes — first real case this protects, unmodified from Part 1 |
| Parser failure beyond bad JSON (unexpected shape) | **Not** a `ToolError` — an uncaught exception, same as it already is for ruff today | Not currently isolated (pre-existing property, not introduced here) |
| Config error (`eslint.config.js` missing) | Same as "non-zero exit that isn't legitimate" | Yes |

**Should Ruff's findings still appear if ESLint fails? Yes — and this is the part of the design Part 1 explicitly deferred, that now needs a real answer.**

Trace it precisely: `batches` is keyed by adapter name, sorted alphabetically — `"eslint"` runs before `"ruff"`. If ESLint's batch raises `ToolError`, Part 1's loop *does* continue on to ruff's batch (execution isolation already works — ruff is genuinely attempted, its findings genuinely computed). But at the end of `run()`, `if errors: raise errors[0]` — the function **raises**, and the `raise` discards the entire `RunResult`, including ruff's now-real findings, before it ever reaches the caller. **Execution isolation and result propagation are two different things, and only the first one is solved today.**

This is exactly the situation Part 1's design doc named and deferred (docs/13 §10: "No policy for presenting multiple simultaneous analyzer failures... deferred until a second real analyzer exists to design against") and exactly what its step-log entry predicted as Part 2's forcing function. It's real now, and it needs a decision:

**Recommended change (needs your sign-off before implementation):** `RunResult` gains one new field, `tool_errors` (a list of `(tool_name, ToolError)` pairs). Instead of raising on the first failure, `run()` returns normally with `findings` containing whatever *did* succeed and `tool_errors` naming whatever didn't. `report.py` gains one small additive rendering block — the same pattern `Skipped` and `Missing` already use today (a labeled section, only shown when non-empty) — so a run with real ruff findings and a broken ESLint install shows both: the findings, and an honest "eslint: 'eslint' is not installed or not on PATH" line, rather than the CLI's current behavior of hiding the findings entirely behind a bare tool-error message and exit code 2.

This directly answers your other two questions:

- **Should one `ToolError` represent multiple analyzer failures?** No — keep them separate, one entry per failing tool in `tool_errors`, each with its own message. Merging them into one exception would lose exactly the attribution (`finding.tool` already exists for the same reason) that makes a failure actionable ("which tool, why").
- **Should failures remain isolated?** Yes, and — to be precise about what's already true vs. proposed — **execution** isolation already is (Part 1, unchanged). What's proposed here is **result** isolation: a failing tool's failure should not erase a succeeding tool's output on the way out of `run()`.

**Trade-off of the recommended change:** it's a real, visible behavior change for the one-shot CLI's tool-failure exit path (today: exit 2, no findings shown, ever, on any tool error; proposed: findings shown when any succeeded, exit code becomes "1 if findings, else 2 if any tool_errors" — still never silently exit 0 with a hidden failure). It also touches `report.py`, which every other part of this document keeps off the "files changed" list — the one deliberate exception, flagged here rather than smuggled into §6's table. If you'd rather keep today's "any failure aborts the whole report" behavior for Part 2 and revisit propagation policy separately, that's a smaller, purely additive change (`ok_exit_codes` + cwd anchoring only) — say so and this section's recommendation is dropped from scope.

---

## 8. Normalization

| | Ruff | ESLint | `Finding` field |
|---|---|---|---|
| Severity | Own string (`"error"`), passed through | Integer `1`/`2`, decoded to `"warning"`/`"error"` | `severity` |
| Location | `location.row` | `line` | `line` (column dropped for both — `Finding` never had one) |
| Code/rule id | `code` (e.g. `F401`) | `ruleId` (e.g. `no-unused-vars`, may be `null`) | folded into `message` as `"{code}: ..."`, same conditional pattern for both |
| Message | `message` | `message` | `message` |
| Tool | `self.name` | `self.name` | `tool` |
| Path | absolute (verified) | absolute (verified, Windows-style backslashes) | `file` — a plain string, never re-parsed |

**Why `report.py` needs no changes:** it already only ever touches `finding.file/line/severity/message/tool` and `result.checked/skipped/missing/findings/tools_used` generically (verified by reading it in full) — every one of those is already populated identically regardless of which adapter produced it. The **one** exception is the proposed `result.tool_errors` field from §7, which is a genuinely new thing to render, not a change to how existing fields are rendered — flagged there, not hidden here.

---

## 9. Performance — measured, not speculated

All measured 2026-09-07, this machine, files in the session scratchpad (not the repo).

| Scenario | Result |
|---|---|
| Ruff, 1 file (docs/12, prior measurement) | 57 ms |
| ESLint, 1 file, direct local binary (3 runs) | 489 ms, 440 ms, 451 ms |
| Ruff, 100 `.py` files | 152 ms, 20 findings |
| ESLint, 100 `.js` files, direct local binary | 636 ms, 40 findings |
| Sequential combined (ruff 100 `.py` + eslint 100 `.js`, one after another — exactly how `run()` would execute them) | 567 ms total |
| ESLint via `npx eslint` (local install resolved, no network) | 1089 ms |
| ESLint via direct local binary | 404 ms — **2.7x faster than npx** |

**Discussion — is sequential execution still appropriate? Yes.** The mixed 100+100-file batch completed in ~0.57s — orders of magnitude under the 60s tool timeout and nowhere near a real problem. More importantly: **watch mode's real per-edit path only ever invokes the one adapter matching the saved file's extension** — a `.js` save never triggers ruff, a `.py` save never triggers eslint, because dispatch is per-extension (§1). The "two analyzers in one batch" scenario only arises in bulk scans (a one-shot CLI run, or an initial large `--git-diff`), not in the steady-state watch-mode loop that Part 1's stability work centered on. Parallelizing would only help the bulk-scan case, and even there, 0.57s for 200 files gives no evidence a problem exists to solve. This mirrors Part 6's own precedent exactly (the scheduler-thread proposal withdrawn for lack of evidence) — **parallel execution is explicitly rejected for Part 2**, not deferred for lack of time.

**One honest caveat, named rather than buried:** ESLint's Node.js cold-start (~450ms) is real and roughly **8x** ruff's single-file latency (57ms). A single `.js` save in watch mode would take roughly 300ms (debounce window) + ~450ms (ESLint) ≈ 750ms before a report appears — noticeably slower than ruff's documented ~360ms end-to-end for a single `.py` save. This is not an architecture problem to fix here; it's Node's process-startup cost, inherent to any Node-based tool this project ever adds. Named as a known, tool-inherent limitation, not something the engine should try to hide or work around with added complexity.

---

## 10. Real repositories

| Repository shape | Behavior | Why |
|---|---|---|
| Python-only | Identical to today. `.js` files never occur; `ADAPTERS`'s second entry is simply never matched. | `_extension_index()` only activates entries whose extension is present in the file list. |
| JavaScript-only | Every `.js` file dispatched to `ESLintAdapter`; `ruff` never invoked (no `.py` files to batch). `Tools:` line reads `eslint`. | Same dispatch mechanism, mirrored. |
| Mixed | Both adapters run, independently, each on its own file subset; findings from both appear in one unified list, each tagged with its own `tool`. | Exactly what §2's diagram shows. |
| ESLint not installed | `.py` files checked normally by ruff; `.js` files produce a `ToolError` (`"'eslint' is not installed or not on PATH"`) via the existing generic `FileNotFoundError` path — surfaced per §7's recommendation (or, if that recommendation isn't adopted, the whole run aborts with that message, as it does for ruff today). | Nothing invents a clean bill of health for code that was never actually checked. |
| Ruff not installed | Symmetric to the above, roles reversed. | Same mechanism, no ruff-specific code path. |
| Unsupported files present (`.css`, `.md`, binary assets, …) | Already-existing "no tool configured for X" skip path, unchanged — listed under `Skipped`, never silently dropped, never treated as a finding. | Fully generic since Phase A; nothing about ESLint changes this. |

Nothing in this matrix has a "silently nothing happens" outcome — every file lands in exactly one of `checked` (with real findings or a clean bill), `skipped` (named, with a reason), `missing` (named), or is behind a named `tool_errors`/`ToolError` entry.

---

## 11. Testing strategy

Following the existing `tests/{regression,integration,stress}` structure and golden-file discipline exactly (docs/12 §"Testing methodology"):

- **Unit — `ESLintAdapter.parse()`** against real captured ESLint JSON fixtures (not synthesized by hand): a clean file, a file with findings, a batch containing one fatal syntax error, and a `node_modules`-ignored-file advisory — the four real shapes measured while writing this document. Golden-style, matching how ruff's fixtures already work.
- **Unit — `ok_exit_codes` generalization** tested directly in `runner.py`'s tests against `RuffAdapter` (unchanged behavior, regression-proof) and a minimal fake adapter with a nonstandard `ok_exit_codes` set, to prove the mechanism is generic and not eslint-special-cased.
- **Unit — the common-ancestor cwd helper** tested directly: single file, multiple files in one directory, files across nested subdirectories (should still resolve upward correctly, per the measured behavior), and files with no common ancestor (two drives/unrelated roots — should fall back to no override rather than raising).
- **Integration — mixed-language project.** A scratch fixture with one `.py` file carrying a known ruff issue and one `.js` file carrying a known ESLint issue (plus a committed `eslint.config.js`); asserts both findings appear in one run, `Tools:` lists both names.
- **Integration — one tool failing, the other succeeding.** ESLint pointed at a project with no `eslint.config.js` while a real ruff issue exists elsewhere in the same run; asserts ruff's finding still surfaces (this test's *pass condition* depends on the §7 decision being adopted — noted as a dependency, not assumed).
- **Integration — watch-mode banner.** Asserts the banner reads `"eslint (.js), ruff (.py)"` once `ESLintAdapter` is registered, with **no change to the assertion's target file** (`__main__.py`) — the literal, automated proof of this part's stated goal.
- **Golden CLI output** — new golden fixtures for a JS-only run and a mixed run, compared byte-for-byte exactly as Phase A's four goldens already are.
- **Timeout** — the existing generic timeout test (already adapter-agnostic, monkeypatches `subprocess.run`) reused with `ESLintAdapter` as a second parametrization, not a new mechanism.
- **Real-world / dogfooding** — before Part 2 is marked complete: run the finished tool against a real small JS repository (or a deliberately scaffolded one with a handful of genuine ESLint-flagged issues) and a real mixed Python+JS repository, and report what was actually observed — matching this project's established "measured, not asserted" dogfooding discipline (docs/12) rather than treating passing unit tests alone as proof.

---

## 12. Trade-offs

- **Node.js/npm as a hard external prerequisite.** Unlike ruff (a qa_agent-owned, pip-pinned, `.venv`-resident dependency), ESLint is invoked as whatever `eslint` resolves to on `PATH` — the user's responsibility, not qa_agent's, and not something `requirements.txt` can express. Named plainly in §6/§4, not softened.
- **Local vs. global vs. `npx`.** Chosen: assume `eslint` is on `PATH`, matching ruff's own assumption and measured **2.7x faster** than `npx`. Real cost: most JavaScript projects intentionally do *not* put ESLint on global `PATH` — it's a pinned `devDependency` almost everywhere, specifically so a project's ESLint version doesn't drift with whatever happens to be installed globally on a given machine. A real user pointing qa_agent at a typical JS project would need to either install ESLint globally (works, but reintroduces exactly the version-drift risk the ecosystem convention exists to avoid) or add `node_modules/.bin` to `PATH` themselves. This is a genuine Part 2 usability gap, named as deferred, not solved — automatic local-install discovery is a reasonable Part 3-or-later addition once there's a real user hitting this, not something to build speculatively now.
- **Version differences are not resolvable by qa_agent.** ESLint 8 (`.eslintrc*`) and ESLint 9 (`eslint.config.js`, flat config) are mutually incompatible — measured directly: ESLint 9 refuses to run at all without a flat config file. Whichever version the *target project* has installed dictates behavior entirely; qa_agent has no way to normalize this from the outside, since it just invokes whatever `eslint` resolves to. This risk is inherent to depending on an externally-versioned tool, not a design flaw here.
- **Configuration discovery is CWD-anchored, not file-anchored** — a genuine philosophical difference from ruff's zero-config design, bridged generically in §1/§6 (common-ancestor cwd), with one honestly-named remaining gap: a single tool-batch spanning two unrelated project roots has no single correct cwd and falls back to none, surfacing as ESLint's own honest "no config found" error rather than silently picking the wrong one.
- **Performance:** ESLint's ~450ms Node cold-start vs. ruff's ~57ms native-binary cold-start is an ~8x per-invocation gap, real and measured, inherent to Node rather than to this design (§9).

---

## 13. Future scalability

**Pyright, Mypy** would both claim `.py` *alongside* ruff — this is precisely the "more than one adapter per extension" capability Part 1 built for exactly this future case (docs/13 §1, §9). A `.py` file would be checked by ruff *and* mypy independently, each producing its own `Finding` entries tagged with its own `tool` name, aggregated into the same `RunResult`, through the same `_extension_index()` mechanism this part uses unchanged. Both are pip-installable, like ruff — likely simpler to integrate than ESLint (no Node dependency, no cwd-anchoring problem, though `ok_exit_codes` would need verifying per-tool, not assumed).

**ShellCheck** claims `.sh` — a genuinely new extension, structurally identical in shape to what this part already proves for `.js`: a new adapter, a new extension, the same four-member contract, plus whatever `ok_exit_codes` its own exit-code convention turns out to need (to be measured when it's actually built, not guessed now).

**Why no further engine modification is likely needed:** between Part 1 (dispatch, per-adapter error isolation) and this part (`ok_exit_codes`, generic cwd anchoring), every axis of variation actually encountered across two structurally different real tools — a Python-native binary with an explicit `--exit-zero` flag, and a Node-based CLI with CWD-anchored config discovery and exit-code-encodes-findings semantics — has been absorbed into the runner exactly once, generically, without adapter-specific branching. A third and fourth tool are more likely to fit these same two knobs than to demand a third engine generalization. This is inductive, not proven — worth verifying again with whichever real tool comes next, the same way this document verified it against ESLint rather than assuming Part 1's design would "obviously" work.

---

## Constraints checked against this design

No plugins. No engine redesign beyond the two generalizations in §1/§6, both justified by direct measurement against a real second tool. No `Finding` redesign (§8). No reporting redesign beyond the one explicitly-flagged, explicitly-optional `tool_errors` addition in §7, which needs your sign-off before it's in scope. No watch-mode redesign (§6 — zero changes). No configuration system. No parallel execution (§9, explicitly rejected with evidence). No caching. No LLM integration. No abstraction introduced without a measured problem behind it — `ok_exit_codes` and the cwd helper both exist because a real command actually failed without them, not speculatively.
