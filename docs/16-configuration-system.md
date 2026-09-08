# Step 16 — Configuration System (Design)

**Status:** IMPLEMENTED (2026-09-07). One deliberate scope change from this
document at implementation time: "severity filtering" (section 17), argued
above as not-yet-justified and deferred, was explicitly requested by name
when implementation was commissioned and so was built - kept as small as the
rest of this design (a single `min_severity` floor over the two severities
this project's adapters actually produce, applied once after merge/dedupe,
its effect always shown in the report, never silent). Full details in the
step-log's Part 4 implementation entry.
**Phase:** C, Part 4 of "Multi-Language Intelligence."
**Scope:** a per-project configuration file that can disable specific analyzers and extend which paths are ignored/included, discovered automatically, with zero configuration required for every existing behavior to keep working exactly as it does today.

Every claim below is checked against the actual current code (`qa_agent/adapters.py`, `runner.py`, `report.py`, `__main__.py`, `analysis_bridge.py`, `gitdiff.py` — reread in full immediately before writing this document, not recalled from memory), not assumed from what earlier docs said, since three parts of real changes have landed since docs/13 was written.

---

## 1. Current architecture after Part 3

```
Project
  |
  v
__main__.py (composition root)
  |
  +-- one-shot: run(inputs, extra_skipped=None) -----------------+
  |                                                                |
  +-- watch: AnalysisBridge().analyze_paths(paths) -- run() ------+
                                                                    |
                                                                    v
                                                    runner.run()
                                                      |
                                                      +-- collect_paths() / _walk()
                                                      |     filtered by hardcoded IGNORED_DIRS
                                                      |
                                                      +-- _extension_index()
                                                      |     built from the full, hardcoded ADAPTERS tuple
                                                      |
                                                      +-- per adapter: _invoke() (isolated, Part 1/3)
                                                      |
                                                      +-- sort + dedupe (Part 3)
                                                      |
                                                      v
                                                RunResult (findings, tool_errors, skipped, missing)
                                                      |
                                                      v
                                                report.py -> one unified report
```

**What's genuinely hardcoded today — verified by rereading the source, not assumed:**

1. **Which adapters run.** `adapters.py` line 159: `ADAPTERS = (RuffAdapter(), ESLintAdapter())` — a literal tuple. `runner._extension_index()` builds its dispatch table directly from this module-level constant. There is no way to run qa_agent against a project and have it skip ESLint (or ruff) without editing `adapters.py`'s source or removing the tool from `PATH` entirely — a blunt instrument that also breaks every *other* project you'd want to analyze with that tool.
2. **Which paths are ignored.** `runner.py` line 37: `IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", "node_modules"}` — a module-level frozenset, consulted by `_walk()`'s `any(part in IGNORED_DIRS for part in path.relative_to(root).parts)`. A project with a `build/`, `dist/`, or `vendor/` directory it wants excluded has no way to say so.
3. **Severity reclassification.** `adapters.py` line 47: `SEVERITY_POLICY = {}` — empty since Step 3, editable only by editing `adapters.py`'s own source. Never exercised by any dogfooding run across three parts (verified: every dogfooding transcript in docs/14 and docs/15 shows an empty policy in effect).

**What already varies per-call, and is the precedent this design follows:** `run(inputs, extra_skipped=None)` already takes an optional parameter with a safe default (Step 4, for `--git-diff`'s deleted-file list); `RunResult` already carries structured, honest "why wasn't this analyzed" information (`skipped`, `missing`, and — since Part 3 — `tool_errors`); `AnalysisBridge` is already a thin, stateful-at-construction wrapper that exists specifically so watch mode doesn't repeat one-shot mode's logic. Every one of these is reused below rather than replaced.

---

## 2. Why a configuration system is now needed

Not speculative — three separate, already-documented real pain points, each surfaced by actual dogfooding in Parts 2 and 3, each currently unsolvable without editing this project's own source:

1. **docs/14's `npm/cli` dogfood run** (654 real `.js` files) failed outright: the repo ships `.eslintrc.js` (legacy config), ESLint 9 refuses to run without a flat config, and the *only* current recourse is a global one — uninstall ESLint from `PATH` entirely, which would also disable it for every *other*, unrelated project. There is no way to say "for this one project, don't even try ESLint."
2. **docs/15's own worked example** (the "failing analyzer never hides findings" dogfood) leaves a project that will *always* show an Analyzer errors section and exit 2, forever, until its ESLint config is fixed — even for a user who is fully aware of this and, for now, only cares about the Python side.
3. **`IGNORED_DIRS` is a reasonable default, not a complete one.** It already covers the common cases this project's own history needed (`.venv`, `node_modules`, tool caches). A real project's `build/`, generated-code, or vendored-third-party directory is exactly as likely to contain files this project's own philosophy says should never be silently analyzed *or* silently mis-flagged — but today the only fix is editing `runner.py`.

The common thread: everything that varies is currently **global, hardcoded Python module state**, when what actually varies is **per-project** — exactly the same category of gap `SEVERITY_POLICY`'s own docstring already anticipated in Step 3 ("Add entries here to classify specific ruff rules yourself") without ever providing a way to do that anywhere but inside this tool's own source. This phase is that gap, closed for the two settings real evidence has already justified.

---

## 3. Design goals

1. **Zero configuration required.** No config file present → every existing behavior, byte-identical, forever. This is the single non-negotiable invariant, checked against every existing golden file (§28).
2. **Per-project, not global.** Config lives with the project being analyzed, the same way `eslint.config.js` and `pyproject.toml` already do for the tools this project wraps — never a qa_agent-wide settings file (explicitly excluded by the brief: no user/global configuration).
3. **Explicit, not inferred.** A setting only ever takes effect because a user wrote it. No auto-detection of project type, no guessing.
4. **Config selects inputs; it never touches execution.** Which adapters run and which paths are walked are config's entire domain. Dispatch, per-adapter error isolation, merging, sorting, and deduplication (Parts 1 and 3) stay completely config-blind — they already don't care *how* the set of files-per-adapter was decided, only that it was.
5. **Every effect config has must be visible in the report.** Nothing this project has ever built silently changes what's shown (`Skipped`, `Missing`, and `tool_errors` all exist for exactly this reason) — a disabled analyzer or an extra ignore rule must be just as honestly reported as a missing tool already is.
6. **Extend existing mechanisms before inventing new ones.** Filtering the existing `ADAPTERS` tuple, extending the existing `IGNORED_DIRS` set-membership check, and reusing the existing `Skipped` reporting path are all preferred, and used below, over new machinery that would do the same job a second way.

---

## 4. Proposed architecture after Part 4

```
Project
  |
  v
__main__.py (composition root)
  |
  +-- config.find_and_load(start_dir) -> Config | None      (NEW, once per invocation)
  |
  +-- one-shot: run(inputs, extra_skipped=None, config=None) -----+
  |                                                                 |
  +-- watch: AnalysisBridge(config=None).analyze_paths(paths) -----+
                                                                     |
                                                                     v
                                                     runner.run(..., config=None)
                                                       |
                                                       +-- collect_paths() / _walk()
                                                       |     filtered by IGNORED_DIRS
                                                       |     UNION config.ignore
                                                       |     MINUS config.include        (NEW)
                                                       |
                                                       +-- _extension_index()
                                                       |     built from ADAPTERS filtered
                                                       |     by config.analyzers          (NEW)
                                                       |
                                                       +-- per adapter: _invoke()          UNCHANGED
                                                       |
                                                       +-- sort + dedupe                   UNCHANGED
                                                       |
                                                       v
                                                 RunResult                                UNCHANGED SHAPE
                                                       |
                                                       v
                                     report.py -> render(result, source, config_path=None)  (NEW param)
```

Everything below the `_extension_index()`/`_walk()` line is **completely unchanged code** — the diagram makes this visible on purpose. Config is entirely an input-selection concern, resolved once, at the top, before dispatch begins.

---

## 5. Configuration lifecycle

One full pass, in order, for a single invocation of the CLI (one-shot) or once at the start of a watch session:

1. **Discovery** (§7): walk upward from a computed starting directory, looking for `.qa-agent.json`. Stops at the first one found, or at the filesystem root with nothing found.
2. **Not found** → `Config` is the empty/default value — semantically "no restrictions beyond today's hardcoded defaults." Nothing else in this list happens; go straight to dispatch.
3. **Found** → **loading** (§6): read the file, parse it as JSON, **validate** it (§9). Any problem at this stage — bad JSON, an unrecognized key, a wrong type, an analyzer name that doesn't match a registered adapter — raises `ConfigError` immediately. This is a whole-run precondition failure, not a per-adapter one (§26), so it is not isolated the way an adapter failure is: it aborts before any adapter is ever dispatched.
4. **Handoff**: the composition root passes the loaded (or default) `Config` into `run()` (one-shot) or into `AnalysisBridge`'s constructor (watch — loaded once, held for the whole session, §22).
5. **Consumption**: `run()` filters `ADAPTERS` and extends its ignore set from `Config`, then proceeds exactly as it does today, with zero further awareness of config.
6. **Reporting**: the config file's path (if one was used) is shown once — in the one-shot report's summary header, or once in the watch-mode startup banner — never repeated per-batch, and its effects on which files/adapters were involved are visible through the existing `Skipped` mechanism (§18).

---

## 6. Configuration loading

A new, small module, `qa_agent/config.py` — reasoned about in §11 (why a new module, not a bolt-on to an existing one).

`load_config(path)`: reads the file as UTF-8, `json.loads()`s it (the same stdlib call `adapters.py` already uses to parse tool output — no new parsing dependency), validates the result (§9), and returns a `Config` — a small `@dataclass(frozen=True)`, matching the existing style of `Finding` and `RunResult`:

- `analyzers: frozenset | None` — `None` means "no restriction, run everything registered" (today's default); a frozenset of names means "only these."
- `extra_ignore: frozenset` — additional directory/file name segments, empty by default.
- `include: frozenset` — name segments that override the *effective* ignore set (hardcoded ∪ `extra_ignore`), empty by default.
- `path: Path` — where this config was actually loaded from, carried along purely so the report can say so (§18).

No config file present is handled by discovery (§7) returning `None` directly — `load_config()` is only ever called on a path that discovery already confirmed exists, so it never needs a "file missing" branch of its own; a file that *disappears* between discovery and loading (a real if rare race, e.g. a concurrent `git checkout` in watch mode) surfaces as an ordinary `OSError` from the `read_text()` call, which is treated as a `ConfigError` too (§9) — a genuine failure to load, not silently treated as "no config."

---

## 7. Configuration discovery

**Where the search starts:** the common ancestor directory of the resolved input paths — reusing `runner._batch_cwd()`'s exact already-tested logic (Part 2), not a second implementation of the same idea. For watch mode, there is only one input directory (`args.project_path`), so the "common ancestor" is trivially that directory itself.

**Deliberately the same algorithm for every entry point** — one-shot explicit paths, `--git-diff`, and `watch` all discover config the same way. `--git-diff` was considered for a special case (starting from `gitdiff.repo_root()`, which is already computed for that mode) but rejected: it would converge on the same result whenever the config lives at the repository root (the common case) while adding a second discovery algorithm to reason about and test — not worth it without a concrete case where the two would actually disagree.

**How the search proceeds:** exactly ESLint's own precedent, already measured and cited in docs/14 — walk upward from the starting directory, checking each directory for `.qa-agent.json`, stopping at the **first** one found (nearest wins), or at the filesystem root with nothing found. No merging of multiple configs found at different levels — that is configuration *inheritance*, explicitly excluded by the brief.

**Filename:** `.qa-agent.json` — dotfile-hidden, matching the convention of `.eslintrc`/`.prettierrc`-style per-project tool config this project already sits alongside. Chosen over a bare `qa-agent.json` specifically to signal "tool configuration, not a data file" the same way its siblings do.

**Explicit override — `--config <path>`:** a new, optional CLI flag (`-o`/`--output`'s exact existing pattern: automatic by default, one flag to override) that skips discovery entirely and loads the named file directly, still through the same `load_config()`/validation path. Justified concretely: it is the natural escape hatch when discovery doesn't find what a user wants (an unusual project layout, or testing a specific config file), and this project's own test suite benefits from being able to point at an explicit fixture file rather than depending on directory-structure-based discovery in every test. Added to both the one-shot parser and the `watch` subparser, for the same reason both already show the same `ADAPTERS`-driven banner logic — one consistent surface, not two. **This is the one piece of this design with a plausible smaller alternative** (discovery-only, no flag) — flagged here rather than silently assumed, since it's the one place this document adds CLI surface beyond what was asked for by name.

---

## 8. Configuration precedence

Deliberately the simplest precedence scheme that solves the stated problem, because the brief explicitly excludes the two things that would make it more complex (inheritance, and a global/user layer):

**Two tiers only: the discovered (or `--config`-given) project file, or the hardcoded defaults.** No merging of multiple files, no environment variables, no CLI flags that override individual *values* (only `--config`, which overrides *which file* is loaded — a different thing, §7).

**Within one config file, per-field defaults apply.** A config that sets only `"ignore"` does not need to also repeat `"analyzers"` — every field not present in the file keeps its default (`analyzers: None` = everything enabled). This is deliberately field-level, not all-or-nothing, because a config that had to restate every setting just to change one would be exactly the kind of friction this phase exists to remove.

**Two different merge rules for the two list fields, each justified on its own:**
- `ignore` is **additive** — config-supplied names are unioned with `IGNORED_DIRS`, never replace it. A config file can never accidentally weaken the safety-relevant defaults (re-including `.git` by a badly written config, for instance) — it can only add.
- `analyzers`, when present, is **exclusive** — it names the complete set that should run, not an addition to some other set. This is the correct semantics for "enable/disable" specifically: the whole point of the field is to name a proper subset, and there is (today) no disabled-by-default adapter to "enable" — every registered adapter runs unless named out, so "enable" and "disable" are really the same subtractive operation (§20 returns to this asymmetry).

---

## 9. Validation strategy

Checked in this order, each failure raising `ConfigError` immediately (no partial application of a half-valid config):

1. **Is it valid JSON?** `json.JSONDecodeError` → `ConfigError`, message includes the parser's own location detail — the same pattern `RuffAdapter.parse()`/`ESLintAdapter.parse()` already use for the tools' own output.
2. **Is the top level a JSON object?** A list or scalar at the top level → `ConfigError`.
3. **Are all keys recognized?** `analyzers`, `ignore`, `include` are the only valid top-level keys for this phase. An unrecognized key (a typo — `"analyzer"` for `"analyzers"`, say) is rejected outright, not silently ignored. This matches this project's consistent "never silently do the wrong thing" ethos (the same reasoning that made `Skipped`/`tool_errors` explicit sections rather than swallowed information) applied to the user's own config authoring: a silently-ignored typo would mean the user believes a setting is active when it never was.
4. **Is each present key's value the right shape?** `analyzers`/`ignore`/`include` must each be a JSON array of strings (not a single string, not a number, not nested objects).
5. **Does every named analyzer actually exist?** Each entry in `analyzers` is checked against `{a.name for a in ADAPTERS}` (the real, currently-registered set) — `"analyzers": ["ruf"]` is rejected, not silently resulting in zero adapters running. This is the single most important validation rule in the whole design: a name-matching mistake here has exactly the "invented clean bill of health" failure shape this project refused starting in Part 2's `use_shell` fix — a project that intended to keep ruff running but misspelled its name must never silently end up with *no* analysis at all.

`ignore`/`include` entries are **not** validated against the filesystem (a name that doesn't currently match any real directory is not an error — it might start matching after the project adds one, and requiring it to already exist would make the config file's own git history noisier than the setting is worth).

---

## 10. Failure behavior

A `ConfigError` is a **precondition failure**, not an analyzer failure — reasoned about precisely in §26. It is raised and caught exactly where `gitdiff.py`'s own `ToolError` (a structurally identical kind of "we can't even start" failure — "not inside a git repository," "invalid ref") is already caught today: at the composition root, in `__main__.py`'s existing `try/except` around the analysis call, printed via a new `render_config_error()` (`render_tool_error()`'s exact existing one-line pattern, reused, not reinvented), exit 2. Watch mode's equivalent: `_watch_main()` raises before `WatchSession.run()` is ever entered, so a bad config means the process never starts watching at all rather than starting with a silently-wrong configuration — consistent with how a bad `project_path` already exits 2 before the session begins today.

**Nothing about a `ConfigError` is isolated the way an adapter's `ToolError` is.** A broken adapter still lets every *other* adapter run, because we know exactly what each adapter was supposed to do. A broken config means we genuinely do not know what the user wanted enabled or ignored — proceeding with "some best guess" would be exactly the kind of invented behavior this project has refused since Step 1.

---

## 11. Component responsibilities

| Component | Responsibility | New/changed? |
|---|---|---|
| **`config.py`** (new) | Discover, load, validate a `.qa-agent.json`; define `Config` and `ConfigError`. Knows nothing about adapters' internals, the runner's dispatch loop, or reporting. | New module. |
| **`runner.py`** | Unchanged responsibility (dispatch, invoke, merge). Gains: an optional `config` parameter to `run()`, consumed only to filter `ADAPTERS` and extend the ignore set before today's existing logic runs unmodified. | Small, additive change. |
| **`adapters.py`** | Unchanged responsibility, **and unchanged code** (§16). Still the sole source of truth for which tools qa_agent *knows how to run*; config only ever selects a subset of that, never adds to or edits it. | Untouched. |
| **`report.py`** | Unchanged responsibility. Gains: an optional `config_path` parameter to `render()`/`render_markdown()`, shown once in the summary header when present. | Small, additive change. |
| **`__main__.py`** | Composition root — already the place that decides `--git-diff` vs. explicit paths, already the place that writes `--output`. Gains: discover/load config once, pass it to `run()`, pass its path to `render()`; a new `--config` flag on both subcommands; catches `ConfigError` alongside the existing `ToolError`. | Additive, no restructuring. |
| **`analysis_bridge.py`** | Unchanged responsibility. Gains: an optional `config` constructor parameter, held for the session, threaded into every `run()` call it makes. `analyze_paths(paths)`'s own signature does not change. | Small, additive change. |

**Why `config.py` is a new module, not folded into an existing one** — the same reasoning that justified `gitdiff.py` as its own module in Step 4 rather than living inside `runner.py`: discovery, parsing, and validation of a *user-authored* file is a genuinely distinct responsibility from dispatching to tools (`runner.py`) or defining tool contracts (`adapters.py`). Cramming it into either would mix "what does this file mean" with "how do we invoke a tool," the exact kind of blending Part 1's whole redesign existed to undo.

---

## 12. Dependency graph

```
__main__.py -----> config.py
     |
     +-----------> runner.py -----> adapters.py
     |                  ^
     +-----------> gitdiff.py --------|  (ToolError only, unchanged)
     |
     +-----------> analysis_bridge.py --> runner.py --> adapters.py
     |
     +-----------> watch.py, fsmonitor.py, debouncer.py, live_report.py, report.py
```

One new node (`config.py`), two new edges (`__main__.py -> config.py`, implicitly `runner.py`'s new optional parameter — `runner.py` does **not** import `config.py`; it only receives an already-built `Config` object as a plain parameter, the same way it already receives `extra_skipped` as plain data without importing `gitdiff.py`). This is a deliberate choice: `runner.py` stays ignorant of *how* a `Config` came to exist, exactly as it has always been ignorant of *how* a changed-file list came to exist. `adapters.py` gains no new edge at all — nothing about it changes, so nothing points at `config.py` from there.

---

## 13. Configuration format

**JSON**, using the stdlib `json` module `adapters.py` already imports and uses for exactly this kind of "parse structured data" job — zero new dependency, works on every Python version this project already claims to support (README: "Requires Python 3.8+").

**TOML was considered and rejected for now.** It is the ecosystem-conventional choice for Python tool config (ruff, black, mypy, pytest all support it) and reads more nicely by hand (comments, less punctuation). But `tomllib` is stdlib only from Python 3.11 — supporting it on 3.8–3.10 would mean either a new third-party dependency (`tomli`) needing the same five-point justification this project's own recorded dependency philosophy requires, or quietly raising this project's minimum Python version as a side effect of a phase that was never asked to do that. Revisit if the project's own floor ever moves to 3.11+.

**YAML was rejected outright** — needs a third-party parser (`PyYAML`), and nothing else in this project has ever needed one; the same justification bar applies and isn't met.

**JSON's real cost, named honestly:** no comments, and stricter syntax (no trailing commas) than a human-hand-edited file ideally wants. Judged acceptable — this project's own README already documents raw JSON output shapes (`--output-format=json` for ruff, `--format=json` for eslint) as a completely normal thing to hand-inspect, and the config file itself is expected to be small (three optional keys, all in this phase).

---

## 14. Default behavior when no config exists

**Byte-identical to today, in every observable way — the single hardest invariant this design has to hold, and the one checked most directly in testing (§28).**

- `ADAPTERS` is filtered by `config.analyzers`; when `config` is the default `Config` (`analyzers=None`), the filter is a no-op — every registered adapter runs, exactly as `ADAPTERS` alone already produces today.
- The ignore set is `IGNORED_DIRS | config.extra_ignore - config.include`; with the default `Config` (`extra_ignore=frozenset()`, `include=frozenset()`), this reduces to exactly `IGNORED_DIRS`, untouched.
- `render()`/`render_markdown()`'s new `config_path` parameter defaults to `None`, and the new summary line is only emitted when it is not `None` — so a no-config run's report has **no new line at all**, not an empty or "none" line. This is the detail that keeps every existing golden file matching byte-for-byte.

---

## 15. Enable/disable analyzers

`Config.analyzers`: a JSON array of adapter names (`"analyzers": ["ruff"]`), validated against the real registered set (§9). `runner.run()` computes the *effective* adapter tuple once, at the top: `ADAPTERS` filtered to those whose `.name` is in `config.analyzers`, or the full `ADAPTERS` unchanged when `config.analyzers` is `None`. Everything downstream — `_extension_index()`, dispatch, `_invoke()`, sort/dedupe — consumes only this already-filtered set and needs no further awareness that filtering happened at all.

**A disabled adapter's files are not silently absent — they are reported, honestly, through the existing `Skipped` mechanism** (§18), with a distinct, accurate reason ("eslint disabled by config") rather than the generic "no tool configured" — because there *is* a tool configured; it has just been turned off for this project.

---

## 16. Integration with Adapters

**`adapters.py` is completely untouched — zero lines changed.** This is deliberate and worth justifying precisely, not just asserted:

- The *full* set of tools qa_agent knows how to run does not change based on config — `ADAPTERS` remains the single source of truth for "what qa_agent supports," exactly as it has been since Part 1.
- Config only ever *selects a subset* of that known set for one project; it never adds a tool qa_agent doesn't already know about (that would be a plugin system, explicitly excluded) and never changes how a known tool is invoked (that would be per-adapter settings beyond enable/disable — explicitly deferred, §31).
- No adapter's `build_command()` or `parse()` signature changes. `SEVERITY_POLICY` stays exactly where and what it is today — a hardcoded, empty-by-default, source-edited constant. Moving it into the config system was considered (§17) and explicitly deferred: doing so would require touching every adapter's `parse()` signature, a materially bigger and differently-shaped change than filtering an already-existing tuple, and — unlike enable/disable and ignore/include — has no concrete dogfooding evidence behind it yet (§2 lists three real pain points; severity reclassification is not one of them).

---

## 17. Severity filtering (only if justified) — not justified; deferred

Two different things could be meant by "severity filtering," and it's worth being precise about which one this section is turning down:

1. **Threshold-based suppression** — "only show findings at or above `error`, hide `warning`." This is a genuinely new feature: nothing in this project has ever hidden a real, tool-verified finding from the report (the entire philosophy since Step 1 has been the opposite — report everything real, invent nothing). No dogfooding run across four parts has surfaced a concrete case where this was actually needed; introducing it now would be exactly the "speculative abstraction" the brief asks to avoid. **Deferred because solving it now would expand Phase C Part 4.**
2. **Per-rule reclassification** — moving the *already-existing* `SEVERITY_POLICY` concept from hardcoded-in-source to config-loadable. This one has a real existing mechanism behind it (Step 3), but as reasoned in §16, folding it into config in this phase would touch every adapter's `parse()` signature — a bigger, differently-shaped change than anything else in this design, for a setting with zero observed real-world use across three parts of dogfooding. **Deferred because solving it now would expand Phase C Part 4** — a natural next addition to `Config`'s shape once there's a concrete case, following exactly the same "adapter declares/consumes one more fact" pattern `ok_exit_codes` and `use_shell` already proved out in Part 2.

---

## 18. Include/exclude path handling

**Exclude (`ignore`):** additive to `IGNORED_DIRS`, same matching mechanism `_walk()` already uses — exact directory/file *name* membership against each path component, not a glob or pattern engine (§30 names this limitation explicitly). `"ignore": ["build", "dist"]` means any path with a component literally named `build` or `dist`, anywhere in the tree, is skipped — identical semantics to how `.venv` is already skipped today, just user-extensible.

**Include (`include`):** subtracted from the *effective* ignore set (`IGNORED_DIRS | extra_ignore`) before matching — an override for a hardcoded or config-added default a particular project genuinely wants analyzed despite the general rule. Concrete, realistic example: a project that vendors one small, first-party package inside `node_modules` and *does* want it linted, unlike the third-party code alongside it:

```json
{
  "ignore": ["build"],
  "include": ["node_modules"]
}
```

**Deliberately the same name-component matching for both fields, not path-based matching.** A finer-grained "un-ignore this one specific file inside an otherwise-ignored directory" was considered and rejected for this phase — it would need a different (path-based, not component-based) matching rule than `ignore` uses, adding real complexity for a case with no concrete evidence behind it yet. Noted honestly as a real limitation of this design, not hidden.

**Reporting:** a config-added ignore behaves exactly like a hardcoded one already does — invisible, never listed under `Skipped`, for consistency with how `.git`/`.venv`/`node_modules` are already silently absent from every existing report. A config-added *include* likewise just results in the file being analyzed normally, with no special annotation — it looks exactly like any other checked file, because from the engine's point of view, after the ignore-set computation, it is.

---

## 19. Per-adapter configuration

Scoped, in this phase, **specifically and only to enable/disable** (§15) — not a general per-adapter settings object (extra CLI flags, a custom tool-config-file path, per-adapter severity). Each of those was considered individually and deferred (§31) for the same reason: real evidence exists for *which tools run* and *which paths are walked*; none yet exists for *how a specific tool is invoked* beyond what it already does. Extending `Config`'s shape with a genuine per-adapter settings block, once a concrete need exists, is a small, well-precedented future addition (§32) — not something this phase needs to build ahead of that need.

---

## 20. Integration with Runner

`run(inputs, extra_skipped=None, config=None)` — one new, optional, default-`None` parameter, following the exact precedent `extra_skipped` itself set in Step 4. `config=None` is treated identically to "the default `Config`" (§14) — every existing call site (including every existing test that doesn't know this parameter exists) keeps working unmodified.

Two small additions at the top of `run()`, before today's existing logic:

- **Effective adapters**: `ADAPTERS` filtered to `config.analyzers` if given, else `ADAPTERS` unchanged — computed once, used in place of the bare `ADAPTERS` reference everywhere `_extension_index()` and the disabled-adapter skip-reason (§15) need it.
- **Effective ignore set**: `IGNORED_DIRS | config.extra_ignore - config.include`, computed once, passed into `_walk()` in place of the bare module constant.

**One asymmetry worth naming, not hiding:** because every registered adapter runs by default, `config.analyzers` is *only ever* subtractive in practice today — there is no disabled-by-default adapter for a config to "enable." The field is still named `analyzers` (a selection), not `disabled_analyzers` (explicitly subtractive), because that's the more natural shape once a future phase *does* register a disabled-by-default tool (§32) — but it's worth being honest that, right now, "enable" and "disable" are the same operation wearing two names.

**Everything after this point in `run()` — dispatch, `_invoke()`, per-adapter error isolation, sorting, deduplication — is unmodified code.** This is the concrete meaning of "integrate without redesigning the engine": config affects exactly two inputs to a function that already existed, and touches nothing about what that function does with them.

---

## 21. Integration with Unified Reporting

Two small, additive touches to `report.py` (§14 already established that both are no-ops in the no-config case):

- `render(result, source, config_path=None)` and `render_markdown(result, source, config_path=None)` each gain one line in their existing summary header, shown only when `config_path` is not `None`: `"  Config:  {}".format(config_path)`. This answers "was a config file even picked up" transparently and independent of whether this particular run's file set happens to make config's effects visible any other way.
- **No change at all** to `_findings_lines()`, `render_findings()`, or anything watch mode's `LiveReporter` calls per-batch. Config's effects that vary *per file* (a disabled adapter's files, §15) are already fully carried by the *existing* `Skipped` list and its reason string — which `_findings_lines()` already renders, unmodified, for both the one-shot report and every watch-mode batch. Config's effects that are *session-wide, not per-batch* (which config file, which adapters are active) belong in the *startup* banner, not repeated in every live batch — exactly matching how "Analyzers:" itself already works today (§22).

---

## 22. Interaction with watch mode

Config is discovered and loaded **once**, at `_watch_main()` startup, before `WatchSession.run()` begins — not re-discovered per batch, and not hot-reloaded if the file changes mid-session (§29 names this explicitly). It is handed to `AnalysisBridge` at construction (`AnalysisBridge(config=loaded_config)`), which is already instantiated once and reused for the whole session; `analyze_paths(paths)`'s own call signature does not change, since the bridge already holds everything it needs from construction time.

The startup banner — which already dynamically lists `"eslint (.js), ruff (.py)"` built from `ADAPTERS` (Part 1/2's own zero-`__main__.py`-change success story) — is extended to reflect the *effective*, config-filtered set instead of the raw registry, so a session with ESLint disabled shows `Analyzers: ruff (.py)` from the first line, not a surprise 20 minutes later. The config file's own path is shown alongside it, mirroring §21's one-shot header line.

---

## 23. Interaction with `--git-diff`

No special case. `changed_files(ref)` still produces exactly the file list it always has; that list still becomes `run()`'s `inputs`. Config discovery still starts from the common ancestor of those files (§7), which converges on the repository root in the overwhelmingly common case where a config file lives there — `gitdiff.py` gains no new responsibility and no new import. `ToolError`s that `changed_files()` itself can raise ("not inside a git repository," a bad ref) are completely unrelated to config and continue to work exactly as they do today.

---

## 24. Interaction with `--output`

Symmetric, no special interaction: `render_markdown()` gains the exact same optional `config_path` parameter `render()` does (§21), for the same reason `render_markdown()` has always mirrored `render()` — "the same run... in Markdown. Same data, different presentation," per its own existing docstring. The file written by `--output` shows the same "Config:" line the terminal does, or none at all in the no-config case.

---

## 25. Interaction with multi-analyzer execution

Config resolves to exactly two plain values — a set of adapter names and a set of ignore/include names — **before** `run()`'s dispatch loop begins, and neither value is consulted again after that point. The per-adapter execution isolation (Part 1) and result isolation (Part 3), the deterministic sort key, and the conservative dedup logic (Part 3) are all defined over "whatever ended up in `batches`" — they have never needed to know *why* a particular adapter is or isn't in that dict, whether because its extension was never present in the input files, or now, because config filtered it out beforehand. No changes to any of that machinery are needed, and none are made.

---

## 26. Error isolation

Two genuinely different failure categories, kept genuinely separate — the key design distinction of this whole document:

| | Adapter failure (`ToolError`) | Config failure (`ConfigError`) |
|---|---|---|
| **When it can happen** | Mid-run, per adapter, after dispatch has begun | Before dispatch begins at all |
| **What we still know** | Exactly what every *other* adapter was supposed to do | Nothing reliable — we don't know what the user wanted enabled or ignored |
| **Isolation** | Yes — recorded in `RunResult.tool_errors`, every other adapter still runs (Part 1/3) | No — the whole run aborts; proceeding on a guess would be inventing behavior |
| **Where it's caught** | Inside `run()`'s per-adapter loop | At the composition root, before `run()` is ever called |
| **Exit code** | 2 (alongside any findings that did succeed) | 2 (nothing has run yet) |

A config error is structurally identical in *shape* to `gitdiff.py`'s existing `ToolError` for "not inside a git repository" — a precondition failure the whole run depends on — which is exactly why it's caught at the same point in `__main__.py`, not folded into `RunResult.tool_errors` alongside adapter failures it is not actually one of.

---

## 27. Performance impact

Discovery is a bounded upward filesystem walk (one `Path.exists()` check per directory level, the same order of cost as `_batch_cwd()`'s own `os.path.commonpath()` call already in the hot path) plus, at most, one small JSON file read and parse — in-process, stdlib-only, no subprocess spawned. This happens exactly **once** per one-shot invocation, or once at watch-session startup — never per file, never per batch, never inside `_invoke()`'s loop.

Reasoned from first principles rather than freshly benchmarked, unlike docs/14's performance section, because the operation category is different in kind: every measured cost in this project so far (docs/12, docs/14) is dominated by *spawning an external process* (ruff, eslint, git) — 50–500ms per invocation. A handful of in-process `Path.exists()` calls and one small `json.loads()` are multiple orders of magnitude below that, on a part of the pipeline that runs once per session rather than once per file. Worth confirming with a real timer once implemented, but not worth gating this design on a measurement whose answer isn't in doubt.

---

## 28. Testing strategy

Same structure and harness Parts 1–3 already established (`tests/harness.py`'s `Suite`/`TempProject`, no pytest, `tests/run_all.py` auto-discovers `test_*.py`):

**Unit (`config.py` directly, no CLI, no real adapters needed):**
- Valid config: correct `analyzers`/`ignore`/`include` parsed into the right `Config` fields.
- No file present → discovery returns `None`, `run()` behaves with the default `Config`.
- Malformed JSON → `ConfigError`, verified via message content.
- Unrecognized top-level key → `ConfigError`.
- Wrong-typed value (`"analyzers": "ruff"` instead of a list) → `ConfigError`.
- Unrecognized analyzer name → `ConfigError` (the typo-protection case, §9's most important rule).
- Upward search finds the *nearest* of two configs at different directory levels.
- `--config <path>` bypasses discovery and loads the named file directly.

**Integration (real CLI, real ruff, real fixture-installed eslint — reusing `tests/fixtures/eslint` from Part 2):**
- A config disabling `eslint` on a real mixed project → `.js` files show under `Skipped` with the new, accurate reason; ruff's real findings are unaffected; the summary/banner reflects only `ruff`.
- A config adding an ignored directory containing a deliberately broken `.py`/`.js` file → genuinely never analyzed, exactly like `.venv` already isn't.
- A config `include`-ing a normally-hardcoded-ignored directory → that directory's files *are* analyzed.
- `--config` pointed at an explicit file in an unrelated directory → loads correctly regardless of cwd (mirrors docs/14's cwd-anchoring test discipline).
- Watch mode: banner reflects config-filtered adapters from the first line; a live save of a file matching a disabled adapter's extension produces no analysis for it (falls through to the existing "not configured/disabled" skip path).

**Regression — the hard requirement (§14):** every existing golden CLI fixture (`cli_findings.txt`, `cli_clean.txt`, `cli_directory.txt`, `cli_markdown.md`) is run with **no config file present** in its `TempProject` fixture and must still match byte-for-byte. This is the test that actually proves §14's claim rather than just asserting it.

---

## 29. Dogfooding strategy

Real dogfooding against real projects, matching every previous phase's "measured, not asserted" discipline — and, concretely, closing the loop on a real, already-documented pain point rather than inventing a new scenario:

1. **Re-run `npm/cli`** (docs/14's original dogfood target, which failed outright on ESLint's legacy config) — this time *with* a `.qa-agent.json` disabling `eslint` for that repo. Expected and to be verified: no Analyzer errors section, no forced exit 2, ruff's real Python-side findings (if any — `npm/cli` is a JS project, so likely none, which is itself a valid, honest result to observe) shown cleanly. This is the single most direct proof this phase was worth building.
2. **The Part 3 mixed-project fixture** (real ruff + real eslint, genuine findings in both) — add a config that ignores one of its two directories entirely, confirm the ignored file's real, injected bug is never reported, exactly as `.venv`'s bugs already aren't today.
3. **A config with a deliberate typo** in an analyzer name, run against a real project — confirm the process fails loudly and immediately with a clear `ConfigError`, not a silent zero-analyzer run (§9's core justification, checked against real process behavior, not just a unit test).

---

## 30. Trade-offs

- **Name-component matching only, no glob/gitignore-style patterns** for `ignore`/`include` (§18). A real, named limitation — many users will reach for `*.generated.py` instinctively. Deferred (§31) because a real pattern-matching engine (recursive `**`, negation, precedence between overlapping patterns) is a materially bigger feature than extending the exact-membership check `_walk()` already has, and this phase has no evidence yet that the simpler mechanism is insufficient.
- **JSON over TOML** (§13) — friendlier ecosystem convention traded for zero new dependency and no Python-version-floor change; revisit if the project's floor ever moves to 3.11+.
- **`--config` as a CLI escape hatch** (§7) is the one place this design adds surface beyond the brief's named list — justified by direct precedent (`--output`) and by this project's own test suite needing it, but flagged, not assumed, as the one decision most reasonable to trim if a smaller Part 4 is preferred.
- **Severity reclassification left entirely out of config** (§16, §17) — the existing `SEVERITY_POLICY` mechanism stays hardcoded, unmoved, for lack of any concrete need across three parts of dogfooding; a real but currently-unjustified gap, not an oversight.
- **No fine-grained, path-based include** (§18) — only directory/file-name-level override, not "un-ignore this one file inside an ignored directory." Simpler, consistent with `ignore`'s own mechanism, at the cost of that one specific (currently unevidenced) use case.

---

## 31. Explicitly deferred work

Every deferral in this document, collected in one place:

- **Severity filtering (threshold-based suppression of findings).** Deferred because solving it now would expand Phase C Part 4 — no observed real need, and it would be the first case of this project ever hiding a real, tool-verified finding.
- **Moving `SEVERITY_POLICY` into config (per-rule reclassification).** Deferred because solving it now would expand Phase C Part 4 — requires touching every adapter's `parse()` signature, with zero dogfooding evidence of need.
- **Per-adapter settings beyond enable/disable** (extra CLI flags, a custom tool-config-file path passthrough). Deferred because solving it now would expand Phase C Part 4.
- **Glob/gitignore-style pattern matching for `ignore`/`include`.** Deferred because solving it now would expand Phase C Part 4 — a materially bigger feature than the exact-name-membership extension this phase makes.
- **Fine-grained, path-based `include`** (un-ignoring one specific file inside an ignored directory). Deferred because solving it now would expand Phase C Part 4.
- **Config file hot-reload during a watch session.** Deferred because solving it now would expand Phase C Part 4 — and because watch-mode redesign is explicitly excluded by the brief; a config change requires restarting the session, exactly as a change to the registered-adapter set already would.
- **Configuration inheritance, remote configuration, user/global configuration.** Explicitly excluded by the brief; restated here for completeness rather than silently omitted.
- **CLI flags that override individual config values** (as opposed to `--config`, which overrides *which file* is loaded). Deferred because solving it now would expand Phase C Part 4 — a third precedence tier beyond "file vs. hardcoded default" that nothing in this phase's justification calls for.
- **Caching, concurrency, scheduling, plugins, dashboards, policy engines, LLM features, additional analyzers.** Explicitly excluded by the brief; restated for completeness.

---

## 32. Future scalability

- **A global/user-level config layer**, if ever added, slots in *above* the per-project layer as one more precedence tier and one more merge step before `Config` is finalized — it does not require changing `Config`'s shape, `load_config()`'s validation, or anything downstream of it.
- **A real pattern-matching ignore engine**, if ever justified, replaces the simple set-membership check inside `_walk()`'s filter alone — discovery, validation, and `Config`'s field names are unaffected.
- **Per-adapter settings**, if ever justified, add one more field to `Config` (e.g. a `dict[str, dict]` keyed by adapter name) and are threaded into `build_command()`/`parse()` the same way `ok_exit_codes` and `use_shell` were added to the adapter contract in Part 2 — a pattern already proven twice to absorb a new adapter-facing fact without an engine redesign.
- **A future analyzer** (Pyright, Mypy, ShellCheck — Part 2's own named future scalability list) is automatically eligible for enable/disable the moment it's registered in `ADAPTERS`, with zero changes to `config.py` — the validation step already checks names against the live registry, not a hardcoded list of expected tools.

---

## Constraints checked against this design

No plugins (config only selects among what `adapters.py` already knows). No configuration inheritance (single file, nearest-wins, no merging). No remote configuration (local filesystem only). No user/global configuration (per-project discovery only). No caching, concurrency, or scheduling (one discovery pass, in-process, once per invocation). No watch-mode redesign (config loaded once at startup, exactly like the adapter registry already is; hot-reload explicitly deferred). No LLM features, dashboards, or policy engines. No additional analyzers registered. Every abstraction introduced (`Config`, `ConfigError`, the discovery walk) exists because a concrete, already-documented dogfooding failure needed it (§2) — not speculatively.
