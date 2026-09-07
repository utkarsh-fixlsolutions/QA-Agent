# QA Agent — Step Log

Running record of each step in the build: what was decided, what was produced, and any open questions carried forward. One entry per step, newest at the bottom.

---

## Step 1 — Definition (2026-09-05)

**Goal:** Write a one-page definition of the QA agent's mission, scope, and success criteria. No code, no architecture, no tool selection yet.

**Constraints given:**
- On-premise only, no paid APIs, minimal (one step at a time), must work on real code/diffs, prefer local free tools, optional local LLM later, react-on-demand not continuous scanning, do not invent findings.

**Produced:** [docs/01-definition.md](./01-definition.md)

**Decisions made:**
- v1 scope = single repo, single run, single report. No LLM, no auto-fix, no CI/dashboard.
- Input can be either a specific changed-file list (diff-driven) or an explicit path — caller always decides scope, agent never self-triggers a full scan.
- Output is a structured report (file, location, message, severity, source tool); exact format (text/Markdown/JSON) deferred to a later step.

**Open questions — resolved same day:**
- **Language target → Python.** Rationale: ruff is a single, free, config-free, JSON-output static binary — fewer moving parts than a JS/TS lint+typecheck stack, best fit for the "as minimal as possible" constraint. Not a permanent lock-in; the design stays language-agnostic and other languages can be added as tool adapters later.
- **Target repo → no existing repo yet.** User has real projects on GitHub to point this at once it's built. Until then, a small scaffolded test repo (with a few deliberate, known issues) will be used to verify the agent as it's built.

**Status: Step 1 CLOSED.**

---

## Step 2 — Tool Selection (2026-09-05)

**Goal:** Pick the exact local tool(s) for Python v1, define the CLI invocation shape and the JSON→report field mapping, and record install requirements. Documentation only — no agent code, no project scaffolding.

**Constraints given:** On-premise, no paid APIs, minimal, Python first, local free tools only, no LLM, no full agent implementation, no architecture beyond what's needed for tool choice.

**Feedback on first draft of this step's plan:** it picked ruff but didn't say how the agent would ever handle another language — would have been a dead end requiring a redesign at the next language. Revised to add one deliberate, minimal piece of architecture: a file-extension → tool-adapter registry (see doc §0), so v1 still only wires up Python/ruff but the shape doesn't need reworking later.

**Produced:** [docs/02-tool-selection.md](./02-tool-selection.md)

**Decisions made:**
- Extension→adapter registry is the dispatch mechanism: each adapter = (CLI command shape) + (parser to shared `{file, line, severity, message, tool}` shape). v1 registers only `.py` → ruff. Unmapped files are explicitly marked "skipped — no tool configured," never silently dropped or fake-flagged.
- Tool: **ruff**, chosen over `flake8`(+`mypy`) purely on minimalism (one binary, one JSON format, no config needed) — not a capability gap. Type checking (`mypy`) explicitly deferred, not decided.
- CLI shape: `ruff check <files> --output-format=json --exit-zero`. Verified live against the actually-installed `ruff 0.15.14` — not assumed from memory.
- JSON mapping verified against real output from a scratch file with 3 deliberate issues (unused import, `== None`, unused variable) in the session scratchpad — not written into the repo. Notable finding: this ruff version genuinely emits a `severity` field per finding (observed as `"error"` across the default rule set and `--select ALL`); no severity value is invented — it's passed through as-is, whatever ruff reports.
- Install requirement: Python + `ruff` on PATH (`pip install ruff`), no network access or per-project config needed at run time.

**Status: Step 2 CLOSED.**

**Next step:** Step 3 — minimal runner.

---

## Step 3 — Minimal Runner (2026-09-05)

**Goal:** Smallest working runner: accept file/directory paths, dispatch `.py` to the ruff adapter from Step 2, parse its JSON into `{file, line, severity, message, tool}`, print a structured report with findings, skipped files, and tool errors.

**Constraints given:** On-premise, no paid APIs/cloud LLMs, minimal, Python-only (ruff adapter only), no invented findings, no git hooks/watcher/other languages/LLM/auto-fix/UI, keep the project linear on top of Steps 1-2.

**Conflict raised and resolved before coding:** the step spec asked for a flat `"warning"` severity, citing "the rule from Step 2" — but that rule only existed in a superseded plan draft. Step 2's live verification found ruff genuinely emits a real per-finding `severity`, and flattening it would have contradicted the step's own "only what ruff actually reports" constraint. User asked whether we could classify severities ourselves; resolved as: pass ruff's value through verbatim by default, plus an explicit, empty-by-default `SEVERITY_POLICY` map for opting into our own per-rule classification later.

**Produced:** [docs/03-minimal-runner.md](./03-minimal-runner.md) and the `qa_agent/` package (`__main__.py`, `adapters.py`, `runner.py`, `report.py`).

**Decisions made:**
- Four small modules, one job each; `adapters.py` is the only module aware that a specific tool exists, keeping the Step 2 registry contract real rather than notional.
- Entrypoint `python -m qa_agent <paths...>`. Exit codes: 0 clean, 1 findings, 2 tool error / no usable input.
- Directory walks skip `.git`, `.venv`, `venv`, `__pycache__`, `.mypy_cache`, `.ruff_cache`, `node_modules`.
- Ruff calls batched at 200 files each — Windows caps a command line near 32k characters.
- Tool failure is structurally separated from findings: `--exit-zero` means findings never cause a nonzero exit, so nonzero exit / missing binary / unparseable JSON all raise `ToolError` → stderr message + exit 2, never a finding.

**Verified (all six checks passed, 2026-09-05):** file with 3 known issues → 3 findings matching raw ruff output exactly (F401 L1, E711 L5, F841 L7, all severity `error`); clean file → "no issues found" exit 0; mixed directory → `.txt` under Skipped, not a fake finding; nonexistent path → "Not found", exit 2; ruff removed from PATH → tool error, exit 2, zero findings printed; no networking imports anywhere in `qa_agent/`. Fixtures lived in the session scratchpad, not this repo.

**Status: Step 3 CLOSED.**

**Next step:** Step 4 — git-diff / changed-files input mode.

---

## Step 4 — Git-diff / Changed-files Input (2026-09-05)

**Goal:** Optional mode so the agent checks only files git reports as changed, without replacing path mode. Reuse the Step 3 pipeline; only file *selection* is new.

**Constraints given:** On-premise, no paid APIs/cloud LLMs, minimal, build on existing code, keep the project linear, no watcher/hooks/other languages/LLM/auto-fix/UI, no invented findings.

**Produced:** [docs/04-git-diff-input.md](./04-git-diff-input.md) and `qa_agent/gitdiff.py`.

**Decisions made:**
- **What "changed" means (default):** `git diff --name-only HEAD` (staged + unstaged in one command) **plus** `git ls-files --others --exclude-standard` (untracked). Untracked files are included deliberately — a newly written `.py` file is absent from `git diff` entirely, yet is exactly what most needs checking. Ignored files stay excluded.
- Optional `--git-diff <REF>` for working tree vs a ref; the ref is verified with `rev-parse --verify` first, so a mistyped ref or a path passed in the ref slot gives a clear message instead of leaking git's raw usage text (found and fixed during testing).
- Repo root resolved via `git rev-parse --show-toplevel`, so the mode works from any subdirectory; `-z` everywhere so odd filenames survive.
- No-commits-yet repos fall back to `--cached` plus untracked, since `HEAD` does not exist.
- **Deleted files** are filtered before dispatch and surfaced under Skipped as `deleted in working tree` — passing one to ruff would otherwise fail the whole run with a tool error.
- Modes are mutually exclusive and both are required-one-of; argparse's `error()` already exits 2, matching the existing input-failure code.
- Reuse over rewrite: `gitdiff.py` is the only git-aware module. `runner.run()` gained one optional `extra_skipped` argument; `report.render()` now takes a source label and a reason-agnostic Skipped header; `adapters.py` untouched.

**Verified (2026-09-05, scratch git repo):** path mode regression unchanged; modified + untracked + staged `.py` all checked (3 findings, exit 1); changed `.txt` skipped; **a committed file with a real `F401` bug left untouched was correctly not reported** (proven ruff would flag it if asked — scope discipline holds); deleted file skipped without a tool error; nothing-changed → clean message, exit 0; outside a repo → clear error, exit 2; `--git-diff HEAD~1` → 5 files, exit 1; invalid ref → clear error, exit 2. Agent still passes its own dogfood check.

**Status: Step 4 CLOSED.**

**Next step:** Step 5 — save report to a file.

---

## Step 5 — Save Report to a File (2026-09-05)

**Goal:** Optional `--output <path>` writing the same report to a file, so results can be kept or shared. Terminal behaviour unchanged.

**Constraints given:** On-premise, no paid APIs/cloud LLMs, minimal, reuse runner/adapters/report, one linear agent, no watcher/hooks/other languages/LLM/auto-fix/UI, terminal output must still work, no changes to finding/git/adapter logic.

**Produced:** [docs/05-report-file.md](./05-report-file.md); changes confined to `report.py` and `__main__.py`.

**Decisions made:**
- **Markdown only; JSON deliberately not built.** The step permitted optional JSON, but nothing consumes it yet, so building it now would mean maintaining a second format with no reader. Left as a small later addition (`render_json()` beside `render_markdown()`, same data) if a real need appears.
- **One data model, two renderers.** `render()` (terminal) and `render_markdown()` (file) are two presentations of the same `RunResult` — not a second report pipeline. `runner.py`, `adapters.py`, `gitdiff.py` are untouched by this step.
- **Print first, then write.** A failed write never costs the results: findings still go to stdout, the write error goes to stderr.
- **Write failure exits 2, outranking the `1` for findings** — the requested output was not produced, so a script must not see the run as successful.
- Parent directories are not auto-created; a missing directory is reported rather than silently made. Files written as UTF-8.
- `|` inside a message is escaped so it cannot silently corrupt the Markdown table.

**Verified (2026-09-05):** no-`--output` behaviour identical to Step 4; path mode and `--git-diff` both write correct files; clean run writes an honest `No issues found.`; **terminal-vs-file parity checked by parsing findings and skipped entries out of both and comparing as sets — identical**; missing directory, directory-as-path, and findings-plus-unwritable-path all give clear errors at exit 2 with results preserved; pipe escaping keeps table rows valid. Agent still passes its own dogfood check.

**Incidental:** a `SyntaxWarning` (invalid escape sequence) introduced while writing `_cell()` was caught and fixed — the agent's own check on `report.py` is clean.

**Status: Step 5 CLOSED.**

**Phase A complete.** Next: Phase B — long-running / watch mode.

---

## Phase B, Part 1 — Watch Mode Skeleton (2026-09-05)

**Goal:** Add a `watch` CLI mode that validates a project path, prints a startup banner, stays running, and shuts down gracefully on Ctrl+C. Infrastructure only — explicitly no file watching, no events, no automatic analysis.

**Constraints given:** Existing CLI behaviour must continue working exactly; `WatchSession` limited to lifecycle management (no analyzer or filesystem logic); no changes to the adapter architecture or analysis pipeline; keep it extensible for later monitoring/incremental analysis/debouncing/live reporting.

**Produced:** [docs/06-watch-mode.md](./06-watch-mode.md) and `qa_agent/watch.py`.

**Decisions made:**
- **Pre-dispatch instead of argparse subparsers.** `paths` is a `nargs="*"` positional, so subparsers would have broken `python -m qa_agent <path>` by requiring a subcommand name. Dispatch happens before argparse: first argument exactly `watch` routes to the watch CLI, otherwise the original parser is entered untouched. This makes "existing behaviour unchanged" verifiable rather than hoped for.
- **Known ambiguity, documented:** a directory literally named `watch` needs an explicit path (`./watch` or absolute). Verified both branches.
- **`WatchSession` holds lifecycle only** — it imports nothing from `adapters`, `runner`, `report`, or `gitdiff`. Analyzer names are passed in by `__main__` (the composition root), built from the `ADAPTERS` registry so the banner cannot drift from what is actually enabled.
- **Idle loop uses `threading.Event`, not `time.sleep`** — efficient, interruptible so Ctrl+C lands promptly, and stoppable from another thread via `stop()`, which is precisely the seam a future filesystem observer needs.
- Exit codes reuse the existing convention: 0 clean shutdown, 2 unusable path.

**Found during testing:** with output redirected, the banner was invisible because stdout is block-buffered — a real defect for a long-running process, fixed with an explicit flush.

**Verified (2026-09-05):** all 8 pre-existing invocations return identical exit codes; process stays alive as a real background process; banner correct and immediate even when redirected; **the real idle loop interrupted by `KeyboardInterrupt` (via `_thread.interrupt_main()`, the same mechanism Ctrl+C uses) shuts down in 0.03s with a friendly message, exit 0, no traceback**; `stop()` from another thread works; session does not exit on its own; missing path / file-not-directory / missing argument each exit 2 with clear messages.

**Limitation stated honestly:** a synthetic console Ctrl+C could not be delivered in this sandbox (Git Bash `kill -INT` does not reach native Windows processes; `GenerateConsoleCtrlEvent` did not cross consoles). The interrupt path is verified through the real loop, but one manual Ctrl+C in a live terminal is worth confirming.

**Status: Phase B Part 1 CLOSED.**

**Next step:** Phase B Part 2 — filesystem event detection.

---

## Phase B, Part 2 — Filesystem Event Detection (2026-09-05)

**Goal:** Detect create/modify/delete/rename events recursively while watch mode runs. Strictly an event detector — no ruff, no `run()`, no debouncing, no queue, no cache, no initial scan.

**Constraints given:** Filter by the adapter registry (never hardcode `.py`); extensible ignore list; unsupported extensions produce no output; never exit on an event; Ctrl+C still graceful; the monitor must know nothing about analyzers, findings, reports, or ruff.

**New architectural principle adopted (user, 2026-09-05):** external libraries are welcome when they are the right engineering choice, provided they integrate cleanly, stay isolated behind our own abstractions, are replaceable via their integration layer alone, support the long-term direction, and are stable/widely adopted. Future library choices must be argued against these five points before implementation. Recorded to memory.

**Produced:** [docs/07-filesystem-events.md](./07-filesystem-events.md), `qa_agent/fsmonitor.py`, `requirements.txt`.

**Decisions made:**
- **watchdog 6.0.0 over stdlib polling.** Polling cannot detect renames — it would have to guess by pairing a delete with a create, exactly the kind of inference this project has refused since Step 1. watchdog also gives native OS events (`WindowsApiObserver` / `ReadDirectoryChangesW`): instant, near-zero idle CPU, right for an always-on PC. Installed into the project `.venv` and pinned in a new `requirements.txt` (with ruff).
- **`fsmonitor.py` is the sole importer of watchdog** (grep-verified). Consumers receive our own `FileEvent` dataclass, never a watchdog class — the same boundary `Finding` draws around ruff's JSON, so replacing the backend touches one file.
- **Registry-as-source-of-truth without importing it:** the monitor takes an injected extension set; `__main__` (composition root) builds it from `ADAPTERS`. Satisfies "no hardcoded `.py`" and "monitor knows nothing about analyzers" simultaneously.
- **Printing lives outside the monitor** — module-level `print_file_event()` in `watch.py`, so Part 3 swaps one callback for a debouncer and the monitor is untouched. `WatchSession` stays lifecycle-only.
- **`WatchSession` gained an optional `component`** with `start()`/`stop()`, run around the idle loop and stopped in a `finally`. The session never learns what it does. This is why Part 1 chose `threading.Event` over `time.sleep`.
- **Ignore list** is a `DEFAULT_IGNORED_DIRS` frozenset, injectable per monitor. Deliberately *not* shared with `runner.py`'s similar list: the monitor stays dependency-free and the two may legitimately diverge. Flagged as a known duplication rather than silent drift.

**Verified (2026-09-05, live process + real file operations):** create/modify/delete reported; rename reported as `old -> new`; `.png`/`.mp4`/`.pptx` produced **zero** output on both create and modify; `.py` files inside `.git`, `node_modules`, `__pycache__`, `.ruff_cache` produced **zero** output despite a supported extension; process survived every event and kept reporting; **Ctrl+C with a live observer exited in 0.05s with exit 0 and thread count back to baseline (observer joined, nothing leaked)**; **registering a fake `.ts` adapter made its events appear with no change to `fsmonitor.py`** while `.css` stayed ignored; all 7 Phase A invocations return identical exit codes.

**Noted for Part 3:** one save can emit several `Modified` events (Windows reports content and metadata writes separately — three were observed for a single write), and editors that save atomically surface as `Created`/`Renamed`. This is real OS behaviour and precisely what debouncing exists to collapse.

**Status: Phase B Part 2 CLOSED.**

**Next step:** Part 3 — incremental analysis on change.

---

## Phase B, Part 3 — Incremental Analysis on Change (2026-09-05)

**Goal:** Connect filesystem events to the existing analysis pipeline. Analyze only the changed file, reuse `run()` and `render()`, return to waiting afterwards.

**Constraints given:** created/modified → analyze that file only; deleted → never analyzed; renamed → destination only; no duplicate analysis logic or second ruff path; no watch-specific report implementation; no debouncing, batching, queues, suppression, caching, aggregation, parallelism, or worker pools. Multiple rapid analyses are acceptable for now.

**User direction during review:** keep the event echo before analysis (observability); catch `ToolError` **and** add a final `except Exception` guard since watch mode runs continuously, but log unexpected errors clearly rather than swallowing them; keep `AnalysisBridge` focused on orchestration and leave presentation to the reporting layer.

**Produced:** [docs/08-incremental-analysis.md](./08-incremental-analysis.md) and `qa_agent/analysis_bridge.py`.

**Decisions made:**
- **`AnalysisBridge` formats nothing.** Acting on the presentation-ownership note, error text moved into the reporting layer too: `ToolError` reuses the existing `render_tool_error()`, and a new `render_unexpected_error()` in `report.py` prints the full traceback. The bridge holds zero presentation beyond calling the echo.
- **Incremental by construction:** `run([path])` is handed exactly the changed file. Every report shows `Checked: 1 file(s)`.
- **Two entry points** — `__call__(event)` decides the path and delegates to `analyze_paths(paths, label)`. Part 4's debouncer calls the latter with a batch, so batching needs no new analysis code. Not speculative machinery: it is one internal split that costs nothing now.
- **Existence check before analysis** handles the created-then-deleted race quietly — a race is not a finding.
- **`display_path()` extracted** into `watch.py` so the echo and the report label share one implementation instead of duplicating the relative-path fallback.
- Output goes through a flushing print, carrying forward the buffering lesson from Part 1: reports must appear when they happen, not when the process ends.

**Why no pipeline refactoring was needed:** `run()` has taken a path list since Step 3 (proved by `--git-diff` in Step 4 adding a second caller without touching the runner or any adapter), and `render()` has taken a free-form `source` label since Step 5. The bridge is simply a third caller of contracts that already existed.

**Verified (2026-09-05, live watcher + real edits, 10/10 E2E checks):** created → real findings; modified → re-analyzed, reports clean after a fix; **every analysis checked exactly 1 file**; **two pre-existing buggy files were never analyzed, proving no full rescan**; rename analyzed the destination only; delete echoed but never analyzed; unsupported `.txt` silent; echo precedes each report. Error containment (in-process): ruff removed from PATH → `ToolError` reported without propagating; injected `ValueError` → full traceback logged and watcher survived; the next analysis after two failures worked normally; vanished rename destination → no analysis, no crash. Ctrl+C during live analysis → exit 0, observer joined, no thread leaked. All 6 Phase A invocations unchanged.

**Known behaviour, deliberately left for Part 4:** analysis runs inline on watchdog's observer thread, so events wait during a run (queued, not lost), and a single save fires several `Modified` events on Windows — each triggering its own analysis. Visible as repeated identical reports in the test output; precisely what debouncing will collapse.

**Status: Phase B Part 3 CLOSED.**

**Next step:** Part 4 — debouncing.

---

## Phase B, Part 4 — Debouncing (2026-09-05)

**Goal:** Coalesce bursts of filesystem events into a single analysis pass. Event scheduling only; pipeline and reporting untouched.

**Constraints given:** exactly one new component (Debouncer); monitor and bridge must both remain unaware of debouncing; same-file bursts → one analysis; different files → all analyzed, none lost; deletions never analyzed; renames analyze the destination; reuse `analyze_paths()`; no worker threads, pools, async, hashing, caching, persistence, prioritization, or parallel analysis.

**User direction during review:** use option (a) — extract the event-to-analysis decision into a shared helper rather than duplicating it; 300 ms initial interval, configurable, validated against real measurements; and prefer a single linear pipeline over branching the composition root into separate print and debounce paths, *if* it can be done without noticeably complicating the design.

**Produced:** [docs/09-debouncing.md](./09-debouncing.md), `qa_agent/debouncer.py`.

**Decisions made:**
- **Linear pipeline adopted.** The Debouncer keeps a `dict[path, FileEvent]` rather than a bare set, so it can forward both the deduplicated paths *and* the events behind them in one `EventBatch`. `__main__` then prints the summary and analyzes in one callback — no fan-out. This also improved output: duplicate echoes collapse too, so a burst is one summary line plus one report instead of three echoes plus three reports.
- **Interval validated by measurement, not folklore.** Measured intra-burst gaps: 0.1-0.3 ms for real saves (simple write, rewrite, atomic temp+replace, three-file save); only a deliberately chunked write reached 50.8 ms. 100 ms would have coalesced every observed gap, so 300 ms keeps a ~6x margin while staying under the ~400 ms "feels immediate" threshold.
- **Two event-semantics rules now live beside `FileEvent`** in `fsmonitor.py`: `target_path()` (what an event leaves to examine) and `merge_events()` (which event best describes a path in a burst). Both bridge and debouncer read them from one place.
- **`merge_events()` added after a test finding:** a rename was being reported as `Modified: renamed.py` because Windows fires a follow-up Modified for the destination, and last-event-wins discarded the rename context. The more specific event (created/renamed) now wins over a following modification, while a deletion always wins as the final state.
- **Flushes are serialized by a second lock**, guaranteeing "no parallel analysis" even when a window expires during a long run. A `threading.Timer` is unavoidable for a debounce — stdlib idiom, alive only while a window is pending, no pool, no concurrency. Analysis now runs on the timer thread, so the observer thread is no longer blocked during ruff.
- **`WatchSession` takes a `components` sequence** (started in order, stopped in reverse) rather than a single component, avoiding a Composite class that would have been a second new component. On shutdown a pending window is cancelled, not flushed.

**Verified (2026-09-05):** debouncer unit tests 9/9 — duplicates collapse; three distinct files all survive; deletions excluded from analysis but kept in the batch; modified-then-deleted not analyzed; rename → destination only; trailing edge re-arms and fires once; **events during an analysis land in the next batch**; **flushes never overlap**; `stop()` cancels the pending window. End-to-end 10/10 — one save → one report (was two); six rapid saves → one analysis; three files together → one report with `Checked: 3 file(s)`; no file lost; pre-existing buggy file never analyzed; deletion reported not analyzed; rename destination only. Part 3 error-containment tests still pass; Ctrl+C with a pending window → exit 0, no thread leak, no late report; all 8 Phase A invocations unchanged; dogfood clean.

**Incidental:** the watch banner still read "files are not analyzed yet" from Part 1 — stale since Part 3, now corrected to "Changed files are analyzed as you save them."

**Status: Phase B Part 4 CLOSED.**

**Next step:** Part 5 — live reporting.

---

## Phase B, Part 5 — Live Reporting (2026-09-05)

**Goal:** Make watch output read as a continuous monitoring stream: one clearly separated report per batch, each with an identity, a timestamp, and the files that triggered it. Presentation only.

**Constraints given:** reuse the existing renderer (a very small extension allowed if justified first); do not redesign the pipeline; keep responsibilities where they are; readable after hours — avoid verbosity and duplicate information; no colors, notifications, statistics, timings, persistence, JSON, config, or cross-batch summaries.

**User direction during review:** Option A — a dedicated `live_report.py`, keeping `watch.py` focused on lifecycle; extract `render_findings()` rather than duplicating formatting, provided CLI output is unchanged; cap long file lists as presentation only; and **evaluate whether `AnalysisBridge` can stay purely responsible for orchestration (producing results) with `LiveReporter` owning presentation**, in preference to injecting renderers.

**Produced:** [docs/10-live-reporting.md](./10-live-reporting.md), `qa_agent/live_report.py`.

**Decisions made:**
- **The bridge now returns an `AnalysisOutcome` and prints nothing** — the user's suggestion, evaluated and adopted over renderer injection. It proved simpler, not more complex: the bridge's imports collapsed to `run` and `ToolError`, dropping `report`, `watch` and `fsmonitor` entirely. The outcome carries `expected` so the reporting layer can distinguish an anticipated `ToolError` from something unforeseen — classifying the failure is orchestration's job (it caught it), formatting it is not.
- **`AnalysisBridge.__call__(event)` removed.** Dead since Part 4 (the debouncer calls `analyze_paths` directly) and it printed an echo plus a report — precisely the presentation being moved out. Keeping it would have left two contradictory contracts in one class.
- **Part 5 removed as much as it added.** The header supersedes `Run at:`, `Input:`, `Checked:` and `Tools:`, all of which were duplicated per batch; `Tools: ruff` was constant noise forever. Watch mode now prints a header plus findings, nothing twice.
- **`render_findings()` extracted** via a shared `_findings_lines()`, so there is one implementation of findings formatting. **CLI output byte-identity was proven, not asserted:** goldens captured before the refactor, diffed after — 5/5 IDENTICAL including the Markdown file.
- **`watch.py` is finally lifecycle-only** — `print_file_event`, `print_change_summary`, `describe_batch` and `display_path` left it. It now imports only stdlib and defines one class, which is what Part 1 asked for.
- **File lists capped at 10** with `...and N more`; presentation only, every changed file is still analyzed.
- **Output is pure ASCII.** A `•` bullet or `…` would raise `UnicodeEncodeError` when watch output is redirected on a cp1252 Windows console — the same class of bug hit earlier in this project. A crash in a process meant to run for hours is not worth a prettier bullet; verified by asserting every output byte is ASCII.

**Verified (2026-09-05):** end-to-end 12/12 — batch identity, sequential numbering, separators, per-batch file lists, multi-file batches, clean batches, deletions annotated and unanalyzed, a 14-file batch capped at 10, **no `Run at:`/`Input:`/`Checked:` duplication**, `Tools:` never repeated, real rule codes, ASCII-only output. Bridge contract tests — success returns a result and prints nothing; `ToolError` classified expected; unforeseen errors classified unexpected; the reporter renders all four cases. Unchanged elsewhere — CLI goldens 5/5 identical, debouncer tests still 9/9, Ctrl+C exits 0 with no thread leak, all 6 Phase A invocations identical, dogfood clean.

**Observation for a future part (not implemented):** findings still print absolute file paths, which after hours is the longest remaining source of noise in the stream — the batch header already shows the relative path. Relativising them would mean giving `render_findings()` an optional base path; flagged rather than done, since it was not in the approved design.

**Status: Phase B Part 5 CLOSED.**

**Next step:** Part 6 — long-running stability.

---

## Phase B, Part 6 — Long-Running Stability (2026-09-05)

**Goal:** No new capabilities. Audit watch mode for continuous execution over days or weeks: object ownership, thread lifecycles, deterministic shutdown, leaks, shutdown races, exception containment.

**User direction during review:** make the `threading.Timer` decision **evidence-driven** — prove a real correctness or performance limitation before replacing it with a permanent scheduler thread; keep the simpler design if there is no data. Also document behaviour under filesystem event storms.

**Produced:** [docs/11-long-running-stability.md](./11-long-running-stability.md).

**Four defects found by reproduction, then fixed:**
1. **One bad event permanently deafened the watcher (critical).** An exception in the event callback escaped into watchdog's dispatch thread and killed it: measured observer alive `True -> False`, **0 events delivered afterwards**, process still running and banner still claiming to watch. Contained in `_Handler._emit`; re-measured as alive with 3 further events delivered.
2. **`stop()` returned while an analysis was running** — measured `flush-start -> stop-returned -> flush-end`, making "Nothing was left running" false. `Debouncer.stop()` now waits on the flush lock, bounded by `SHUTDOWN_WAIT_SECONDS = 10`, and reports an overrun instead of hiding it.
3. **Shutdown order was inverted from its own documented intent** — `reversed([monitor, debouncer])` stopped the consumer before the source, contradicting the Part 4 comment. Components are now listed source-first and stopped in that order.
4. **A failing `stop()` orphaned the components after it** and escaped `run()`. Each stop is now isolated and reported.

**The scheduler-thread proposal was withdrawn for lack of evidence.** Measured: watchdog delivers 2,470 events/sec while `submit()` sustains 6,000-8,000/sec (**~2.5x headroom**); peak live threads at 50,000 events was **6** with zero `Timer.start()` failures; zero files lost in a 3,000-file storm or under sustained load. `threading.Timer` is not the bottleneck — watchdog's dispatch is. The earlier "100k events/day" argument was speculation, and the user was right to challenge it.

**New limitation discovered by the storm testing:** under 25 s of continuous writing (~100 files/sec) the trailing-edge window never expired — **1 batch, zero analyses until writing stopped**. Nothing lost (all 2,057 files analyzed once quiet), but the watcher gives no feedback during sustained activity. The fix is a max-wait cap, which is a behaviour change, so it is deferred to Part 7 rather than slipped into a reliability part.

**Also added:** a liveness check in the idle loop (a component that dies is reported, exit 2, instead of the session watching nothing for days); `FileSystemMonitor.is_alive()`; a guard against double `start()` stranding an observer; containment around the flush callback; error sinks injected from the composition root so failures render through the reporting layer.

**Verified:** Part 6 suite 7/7; all four defect reproductions now show the opposite result; no regressions — debouncer 9/9, bridge contract 8/8, live reporting 12/12, growth still flat (+3.4 KB / 1,200 events), Phase A output byte-identical, all exit codes unchanged, dogfood clean.

**Status: Phase B Part 6 CLOSED.**

**Next step:** Part 7 — dogfooding and polish.

---

## Phase B, Part 7 — Dogfooding & Polish (2026-09-05)

**Goal:** Final part of Phase B. No new user-facing functionality — validate, harden, polish, and make production readiness provable rather than asserted.

**User direction during review:** organise `tests/` into `regression/ integration/ stress/ golden/` with a single `run_all.py`; keep the 60s timeout but **test recovery specifically** (hang times out, reported clearly, watcher survives, next analysis succeeds); define **measurable** dogfooding criteria rather than "run it for a while"; include an architecture diagram; and **keep `__version__`** — a conventional public attribute is not worth removing for no benefit.

**Produced:** [docs/12-architecture.md](./12-architecture.md), a rewritten README, and `tests/` (7 suites, ~106 checks).

**Measured before deciding anything:**
- **Idle CPU: 0.000s over 20s** watching a 300-file tree — effectively free on an always-on PC.
- **Analysis latency: 57 ms for one file** (≈ 0.36s end-to-end with the debounce window); **0.53s for 600 files**; 2.0s for 3,000.
- **Dead code: none.** A symbol-by-symbol audit found every Part 5 removal was genuinely deleted, not orphaned.
- **Hung analyzer: 0 later analyses completed**, shutdown waited out the full bound, no diagnosis — the demonstrated problem justifying the timeout.

**Two earlier claims corrected by measurement, not defended:**
- Part 6 listed "very large batches are slow" as a limitation. **3,000 files analyse in 2.0s.** It was asserted without measuring; docs/11 now records the correction.
- I expected dead code to have accumulated over 11 parts. There was none.

**Changes made — each solving a demonstrated problem:**
- **`ANALYSIS_TIMEOUT_SECONDS = 60`** in `runner.py`; expiry raises the existing `ToolError`, so every layer already knows how to report it without ending a watch session. Normal runs are unaffected — Phase A output is still byte-identical against golden files.
- **`tests/`** — previously every test lived in an ephemeral scratchpad, so none of the project's claims could be re-verified. Now `python tests/run_all.py` runs everything in ~52s, including regressions for all four Part 6 defects so they cannot silently return.

**A test found a real flaw in my own harness:** the golden normaliser missed the Markdown timestamp format (`- **Run at:** ...`), so that golden could never have matched twice. Caught on the second run and fixed.

**Deliberately NOT implemented:** the **max-wait cap** for the sustained-activity limitation. It was raised as a decision in the design and never approved; it changes when reports appear, which is user-facing behaviour, and the instruction was to add nothing speculative. It remains documented as a known limitation and a Phase C candidate.

**Verified:** 7/7 suites, ~106 checks, 52.3s — Phase A CLI vs goldens (19), debounce semantics (17), reporting and bridge contract (16), stability incl. all four Part 6 defects (15), timeout and recovery (12), full watch pipeline as a real process (15), storms and load (11). Phase A output byte-identical; agent still passes its own dogfood check.

**Status: Phase B Part 7 CLOSED. Phase B COMPLETE.**

**Remaining for Phase C:** max-wait cap for sustained activity; self-healing after observer death; a second adapter (JS/TS) to exercise the registry with more than one tool; structured logging and metrics; the `__pycache__` files still tracked in git from before `.gitignore` existed.

---

## Phase C, Part 1 — Multi-Analyzer Foundation (Design) (2026-09-07)

**Goal:** Design review only, no code. Restructure the pipeline from a single-analyzer shape (`Runner -> Ruff`) to a genuine multi-analyzer engine (`Engine -> Multiple Analysis Tools -> Unified Findings`), registering only ruff. Every existing command must remain byte-identical; adding a future analyzer must require implementing a contract and registering it, with no engine change.

**Constraints given:** no new analyzers this part; no CLI/watch/reporting/behavior changes; engine must stay language-agnostic; `Finding` kept as-is unless justified otherwise; error isolation must naturally support multiple tools without redesigning the engine later; avoid speculative abstraction — every abstraction must solve a problem that exists today; dependency graph must not grow; design must be reviewed and approved before any implementation.

**Produced:** [docs/13-multi-analyzer-foundation.md](./13-multi-analyzer-foundation.md).

**Findings from reading the current code before designing:** the invocation/aggregation mechanics in `runner.py` were already tool-agnostic (batches by tool, invokes generically, merges into one `RunResult`) — the real gaps were narrower than the brief's diagram implied: (1) the registry (`ADAPTERS`) maps one extension to exactly one tool, which cannot hold two Python analyzers (ruff + Mypy + Pyright, all named as later additions) without an engine-level rework; (2) one tool's `ToolError` propagates out of the whole run and silently discards findings already gathered from tools processed earlier in the same call — invisible today with one tool, a real bug the moment a second exists; (3) `report.py` was already fully tool-agnostic and needs nothing.

**Design proposed:**
- `adapters.py` -> `analyzers.py`: `RuffAdapter` -> `RuffAnalyzer` gains an explicit `extensions` attribute (today only implied by the external dict); `ADAPTERS` dict -> `ANALYZERS` registration tuple. `Finding`, `ToolError`, `SEVERITY_POLICY` unchanged in shape.
- `runner.py` -> `engine.py`: dispatch changes from an extension dict-lookup to "which registered analyzers claim this extension," so a second tool on an existing extension needs no engine change. Per-analyzer error isolation added to the invocation loop: every matching analyzer is attempted regardless of an earlier one's failure; the (currently sole) failure is still raised after every analyzer has run, keeping `run()`'s external contract byte-identical today.
- `analysis_bridge.py`, `gitdiff.py`, `__main__.py`: import-path updates only; the watch-mode banner and the `FileSystemMonitor` extension set are recomputed from `ANALYZERS` instead of `ADAPTERS`, same output for the current single-analyzer case.
- No formal `Protocol`/ABC contract — kept as a documented, duck-typed contract, matching existing house style (no ABCs anywhere else in the codebase).
- Explicitly deferred: any new analyzer, a config/CLI toggle for enabling analyzers, per-analyzer timeout override, concurrent analyzer execution, and the presentation policy for **multiple simultaneous** tool failures in one run (only decidable once a second real analyzer exists to design against).

**One open point left for approval rather than silently decided:** whether the `adapters.py`/`runner.py` file renames are worth the mechanical ripple through five files and the test suite for a naming-consistency benefit, versus keeping today's filenames and making only the structural changes (list-based registry, per-analyzer error isolation).

**Design approved (2026-09-07) with two amendments:**
1. **No file/symbol renames.** `adapters.py`, `runner.py`, `RuffAdapter`, and `ADAPTERS` all keep their names — only `ADAPTERS`'s shape changes (dict → tuple). Naming deferred to a later cleanup phase.
2. **Scope narrowed on error isolation.** Part 1 builds only the *execution* isolation (every registered adapter attempted independently, one's failure never starves another's), not a presentation policy for showing multiple simultaneous failures at once — that's explicitly Part 2's problem, once a real second adapter exists to design it against.

[docs/13-multi-analyzer-foundation.md](./13-multi-analyzer-foundation.md) updated to match both amendments before implementation began. Because nothing renames, the actual file-touch list shrank to just `adapters.py`, `runner.py`, and `__main__.py` — `analysis_bridge.py` and `gitdiff.py` need no changes at all (both only import `ToolError`/`run`, neither name moving), confirmed by grepping every `ADAPTERS`/`RuffAdapter`/import-path reference in the repo (including `tests/`) before writing code.

**Status: Phase C Part 1 design CLOSED.**

**Implemented, per the approved (amended) design:**
- `adapters.py`: `RuffAdapter` gained `extensions = frozenset({".py"})`; `ADAPTERS` changed from `{".py": RuffAdapter()}` to a tuple `(RuffAdapter(),)`. `Finding`, `ToolError`, `SEVERITY_POLICY` untouched.
- `runner.py`: new `_extension_index()` builds `extension -> [adapters]` fresh each run from `ADAPTERS`, replacing the old `ADAPTERS.get(suffix)` dict lookup — a file can now be claimed by more than one adapter with zero further change here. The invocation loop wraps each adapter's chunk loop in its own `try/except ToolError`, collecting failures into a list rather than letting the first one abort the whole call; the (still sole) error is raised after every adapter has been attempted, once, exactly reproducing today's raise.
- `__main__.py`: the watch-mode banner builder and the `FileSystemMonitor` extension set both now derive from iterating `ADAPTERS` and each adapter's `.extensions`, instead of treating `ADAPTERS` as an extension-keyed dict.
- `analysis_bridge.py`, `gitdiff.py`, `report.py`, `watch.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py`, `tests/**` — **zero changes**, confirmed by grep before writing any code (neither renamed per amendment 1, and none of them reference `ADAPTERS`'s internal shape).

**Verified (2026-09-07):** `python tests/run_all.py` — **7/7 suites, every check, 52.7s**, including the four golden-file CLI comparisons (byte-identical) and the watch-mode banner text (`Analyzers: ruff`) inside the stability/pipeline suites. Storm suite included this run (throughput, memory, coalescing all unchanged). Dogfooded against `qa_agent/` itself post-change: 11 files checked, clean, exit 0. `--help` output unchanged.

**Status: Phase C Part 1 CLOSED.**

**Next step:** Part 2 — a second real analyzer (candidate: a second Python tool such as Mypy or Pyright, both of which will exercise the same-extension-multiple-adapters path this part built for), which will also force the deferred multi-failure presentation policy (§5/§10 of docs/13) to get designed against something real.

---

## Phase C, Part 2 — First Multi-Language Analyzer: ESLint (Design) (2026-09-07)

**Goal:** Design review only, no code. Prove Part 1's architecture scales to a real second tool by designing (not yet implementing) an ESLint adapter for `.js`. The goal is explicitly not "support JavaScript" — it's proving that adding a tool requires only implementing the contract and registering it, with no engine redesign. Pyright, Mypy, ShellCheck discussed only as future scalability, not implemented.

**Constraints given:** design only, no code; ESLint only, no other future analyzers implemented; explain current architecture first, then the ESLint adapter design (interface, command construction, parsing strategy, error handling, cwd/`node_modules` assumptions, exit codes — prose, not parser code); full integration/dependency-graph/error-isolation/normalization/performance/testing/trade-offs/future-scalability writeup; performance section explicitly required to be measured, not speculated; no plugins, no engine/Finding/report/watch/config redesign, no parallel execution, no caching, no LLM, no unjustified abstraction.

**Produced:** [docs/14-first-multi-language-analyzer.md](./14-first-multi-language-analyzer.md).

**Measured before designing (not assumed):** installed ESLint 9.39.5 into the session scratchpad (Node v26.5.0, npm 11.17.0, both already present on this machine) and tested it directly rather than reasoning from documentation. Two real, load-bearing findings came out of that testing, not from inspection:

1. **ESLint has no `--exit-zero` equivalent** (confirmed against its own `--help`). Exit `0` = clean, `1` = findings present (errorCount > 0), `2` = genuine tool/config failure — verified across five real scenarios including a fatal syntax error inside a batch (folded into the same JSON as an ordinary message, not a crash). The engine's current `_invoke()` treats any nonzero exit as a tool failure, an assumption that was only ever true because ruff's adapter passes `--exit-zero` — invisible with one tool, wrong the moment a second tool uses exit codes differently.
2. **ESLint's flat config (`eslint.config.js`) is resolved by searching upward from the subprocess's working directory**, exactly like git finds `.git` — verified working from the project root and from a subdirectory (upward search succeeds), and failing outright from an unrelated directory even with a fully-qualified absolute file path. `_invoke()` never sets a subprocess `cwd` today, which has never mattered for ruff (a deliberately zero-config tool) but breaks ESLint specifically in watch mode, where the qa_agent process is not chdir'd into the watched directory.

**Design proposed:**
- Two generalizations to `_invoke()`, both adapter-blind or adapter-declared rather than ESLint-specific: an `ok_exit_codes` attribute each adapter declares (ruff: `{0}`, formalizing existing truth; eslint: `{0, 1}`), and a generic helper that anchors the subprocess `cwd` to the common ancestor of each invocation's file batch (harmless for ruff, necessary for eslint).
- `ESLintAdapter`: `.js` only (TypeScript deliberately deferred — needs its own due-diligence pass, would blur this part's actual goal); assumes `eslint` resolves via `PATH`, matching ruff's own assumption and measured 2.7x faster than routing through `npx` (0.40s vs. 1.09s, measured).
- **One decision flagged as needing explicit sign-off, not silently decided:** Part 1's `run()` still raises and discards the whole `RunResult` on the first tool failure, even though execution isolation (each adapter is genuinely attempted) already works — meaning today, a failing ESLint would still erase ruff's already-computed findings on the way out. Recommended fix: `RunResult` gains a `tool_errors` field; `run()` returns partial results instead of raising; `report.py` gains one small additive rendering block (same pattern as the existing `Skipped`/`Missing` sections). This is the one place this document touches `report.py`, called out explicitly rather than folded into the "files changed" table unremarked.
- Real-repository behavior matrix, testing plan (new unit/integration/golden suites plus a dogfooding pass against a real JS and a real mixed repo, following this project's "measured, not asserted" precedent), and trade-offs (Node/npm as a non-pinnable external prerequisite, local-vs-global-vs-npx, ESLint 8/9 config incompatibility, the CWD-anchoring philosophy gap) all written up in full.

**Status: Phase C Part 2 DESIGN PROPOSED — awaiting approval, in particular the `tool_errors`/partial-result-propagation recommendation in §7. No implementation started.**

---

## Phase C, Part 2 — First Multi-Language Analyzer: ESLint (Implementation) (2026-09-07)

**Goal:** Implement exactly the approved design (docs/14), strictly scoped to Part 2. Two amendments made at implementation start: the §7 `tool_errors`/partial-result-propagation change explicitly deferred (execution isolation from Part 1 already works; result propagation stays a tracked, tested known limitation, not silently dropped); everything else implemented as designed.

**Produced:** `ESLintAdapter` registered in `adapters.py`; `runner.py` gained the two designed generalizations (`ok_exit_codes`, cwd anchoring) plus a third, undesigned one forced by reality (`use_shell` + a `shutil.which()` pre-check); two new test suites (34 + 20 checks); a real `tests/fixtures/eslint` npm fixture; documentation updated across docs/12, docs/14, README.

**Two real discoveries made only by implementing and testing, not by the design review — both now documented in docs/14:**
1. **npm's `.cmd` shim can't be launched by `subprocess.run(shell=False)` on Windows at all**, regardless of correct PATH resolution (`FileNotFoundError [WinError 2]`, verified even against ESLint's exact, confirmed-correct path). Fixed with a third adapter-declared attribute, `use_shell` (ruff: `False`, unchanged; eslint: `True`), consumed generically.
2. **That fix silently broke "eslint not installed" detection**: with `shell=True`, a genuinely-missing tool never raises `FileNotFoundError` — `cmd.exe` reports "not recognized" as an ordinary exit `1` with empty stdout, which ESLint's own `ok_exit_codes={0,1}` would have misread as "ran cleanly, no findings" — inventing a false clean bill of health, exactly the failure mode this project has refused since Step 1. Caught by `test_missing_tool_is_caught_before_ever_invoking_a_shell`, one of my own new tests, before it ever reached a human. Fixed with an explicit `shutil.which(command[0])` check before every invocation, generic across all adapters, not eslint-specific.

**Verified byte-identical for ruff before trusting the fix, not assumed:** confirmed ruff already resolves a relative input path to an absolute `filename` internally (`D:\\Working\\Qa-Agent\\...`), and confirmed directly that ruff echoes an already-absolute, backslash-style path back completely unchanged **regardless of subprocess cwd** — proving the pre-resolve-to-absolute step required for safe cwd anchoring produces byte-identical output to today's golden files before it was ever wired in.

**Files changed:** `qa_agent/adapters.py` (RuffAdapter gains `ok_exit_codes`/`use_shell`; new `ESLintAdapter`; `ADAPTERS` gains a second entry), `qa_agent/runner.py` (`_batch_cwd()`, generalized exit-code check, `shutil.which()` pre-check, `shell=adapter.use_shell`), `.gitignore` (+`node_modules/`), `tests/harness.py` (+`env` passthrough on `run_agent`, +`ESLINT_BIN`/`eslint_available()`/`env_with_eslint()`), `tests/fixtures/eslint/package.json` (new, real npm fixture - `node_modules` gitignored, `npm install` once, mirrors how `.venv` already works for Python), `tests/regression/test_multi_analyzer.py` (new, 34 unit checks, no live eslint needed), `tests/integration/test_multi_language.py` (new, 20 checks against the real fixture install), `docs/12-architecture.md` (Modules/Dependencies/Testing tables), `docs/14-first-multi-language-analyzer.md` (status, both discoveries, both amendments), `README.md` (ESLint prerequisite paragraph, doc table, status). **Zero changes** to `report.py`, `watch.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py`, `analysis_bridge.py`, `gitdiff.py`, and — the literal proof of the stated success criterion — `__main__.py`: the watch-mode banner already reads `"eslint (.js), ruff (.py)"` from registering the adapter alone.

**Verified (2026-09-07):** `python tests/run_all.py` — **9/9 suites** (7 unchanged + 2 new), **all checks pass**, 61.1s. Every pre-existing golden file still matches byte-for-byte with both new generalizations active. New coverage: 34 unit checks (ESLint JSON parsing incl. a real captured "file ignored under node_modules" advisory that exposed and fixed a `KeyError` on a missing `line` field — found by testing against real data, not invented), 20 integration checks against a real, `npm install`ed ESLint (mixed-project runs, cwd-anchoring from an unrelated directory, a genuine missing-config `ToolError`, a fatal syntax error rendered as an honest finding, real eslint genuinely absent, watch mode's banner and a live `.js` save producing a real finding end-to-end).

**Dogfooded against real external repositories, not just scratch fixtures:**
- **`npm/cli`** (real, major, 654 `.js` files) — confirmed the documented ESLint-8-vs-9 config-format trade-off for real: it ships `.eslintrc.js` (legacy), so our ESLint-9-based adapter correctly reports a clear, honest `ToolError` rather than crashing or inventing a result.
- **`unjs/ofetch`** (real, modern, flat-config project) — every unsupported extension (`.ts`, `.md`, `.json`, `.yaml`, no-extension dotfiles) honestly skipped with a clear reason, zero crashes, zero invented findings, across a genuinely varied real repository tree.
- **Found via this dogfooding, not anticipated by the design:** ofetch's real JS-equivalent source files are `.mjs`, not `.js` — plain JavaScript, needing no extra tooling, so the design's own stated reason for excluding TypeScript doesn't actually apply to `.mjs`/`.cjs`. **Deliberately left as `.js`-only** rather than silently widened (a scope change to an already-approved, already-implemented design deserves an explicit decision, not a quiet addition) — flagged as a small, low-risk, concretely-specified follow-up.

**Deliberately deferred, not solved (per instruction, not silently dropped):**
- The §7 `tool_errors`/partial-result-propagation decision — execution isolation works; a failing tool still discards an already-succeeded one's findings on the way out of `run()`. Tracked by a dedicated, named test (`test_deferred_known_limitation_failure_hides_other_tools_findings`) specifically so it can't rot into "forgotten."
- Widening `extensions` to include `.mjs`/`.cjs` — real gap found via dogfooding, small and low-risk, deliberately left for an explicit decision rather than folded in unasked.
- Everything already out of scope per the design: Pyright, Mypy, ShellCheck, TypeScript/JSX, local-`node_modules`/npx auto-discovery, per-adapter timeout override, parallel execution, config-enable/disable toggles.

**Status: Phase C Part 2 CLOSED.**

---

## Phase C, Part 3 — Unified Multi-Analyzer Reporting (2026-09-07)

**Goal:** Make findings from every registered analyzer read as one coherent result while preserving each finding's own attribution: normalize (already true via `Finding`), merge (already true), deterministic ordering independent of adapter execution order, conservative deduplication of exact cross-tool duplicates, and — the item that mattered most — proper multi-tool error reporting: a failing analyzer must never hide a succeeding one's findings. This is exactly what Part 2's docs/14 §7 deferred pending sign-off; that sign-off is this phase.

**Constraints given:** implement directly (spec given in full, not a separate pre-approval design pass this time); items 1-7 in scope, explicitly: normalize, merge, deterministic order, conservative dedup ("do not merge findings that are merely similar"), multi-tool error reporting with successful findings preserved, unified report rendering, attribution preserved; tests for mixed-language repos, multiple analyzers, duplicates, analyzer failures, stable ordering, and ruff-only regression; out of scope: config system, enable/disable, severity policies, include/exclude paths, caching, parallel execution, performance work, additional analyzers, LLM, watch-mode redesign; preserve existing ruff-only behavior; minimize architectural changes; reuse Part 1's infrastructure; avoid speculative abstraction; dogfood against a mixed-language repository.

**Produced:** [docs/15-unified-reporting.md](./15-unified-reporting.md).

**Design, in brief (full detail in docs/15):**
- `RunResult` gains `tool_errors` (a list of `(tool_name, ToolError)` pairs). `run()`'s per-adapter loop now appends failures there instead of collecting-then-raising - it **never raises `ToolError` for an analyzer failure again**, always returning a `RunResult` with whatever findings succeeded.
- One final `sort()` on the merged findings, key `(file, line, severity, message, tool)` - verified against the existing golden fixtures' own natural order (ascending line within a file) before trusting it, not assumed.
- A conservative `_dedupe()` immediately after sorting: exact `(file, line, severity, message)` match only, `tool` is the sole field allowed to differ, applied to an already-sorted list so which copy of a genuine duplicate survives is deterministic (alphabetically-first tool), not incidental.
- `report.py` gains one new "Analyzer errors" section in the one shared `_findings_lines()` function (plus its Markdown twin) - the CLI, `--output`, and the watch-mode live stream all render it identically for free, with zero changes to `live_report.py`.
- `__main__.py`'s exit-code logic gains a leading `tool_errors` check (exit 2, preserving the exact pre-Part-3 precedent that any tool failure was always exit 2) **before** the findings check, so findings are now printed in full even when another tool failed.
- `analysis_bridge.py`'s `except ToolError` branch removed - provably unreachable now that `run()` never raises it - along with its now-unused import.

**Since ruff and eslint never share an extension today, "duplicate findings" and "two adapters on one file" can't happen through them yet** (Part 1's dispatch already supports it, per docs/13 §9 - just nothing exercises it). Tested directly against the merge/sort/dedup mechanism using two fake adapters sharing a fake extension, exactly mirroring how Part 1 tested its own dispatch generality before a second real adapter existed - proving the mechanism, not something that happens to work only for ruff+eslint's disjoint scopes.

**Existing tests updated, not silently broken:** two `test_multi_language.py` assertions moved from checking `proc.stderr` to `proc.stdout` (a tool error is now part of the one unified report, not a separate stderr-only crash message - working as designed); one, `test_timeout_recovery.py`'s "reported as a tool error" check, updated to look for the new "Analyzer errors" section instead of the old `render_tool_error()` text (that text is now reserved for a failure before `run()` even starts, e.g. `--git-diff` outside a repo); one, `test_reporting.py`'s "missing tool" bridge test, updated to check `result.tool_errors` instead of `outcome.error`/`expected`, since a missing tool is no longer a bridge-level failure at all. **Most notably:** `test_deferred_known_limitation_failure_hides_other_tools_findings`, written in Part 2 specifically anticipating this exact fix, was flipped to `test_failing_analyzer_never_hides_another_tools_findings` and now asserts the opposite of what it asserted before - exactly per its own docstring's instruction.

**Files changed:** `qa_agent/runner.py` (`RunResult.tool_errors`, `_finding_sort_key()`, `_dedupe()`, `run()`'s loop and final sort+dedupe), `qa_agent/report.py` (`_findings_lines()` + `render_markdown()` gain the Analyzer errors section), `qa_agent/__main__.py` (exit-code precedence, docstring), `qa_agent/analysis_bridge.py` (dead branch and import removed), `tests/regression/test_multi_analyzer.py` (+4 tests, +12 checks: ordering, dedup, dedup-doesn't-over-collapse, result isolation - all via fake adapters), `tests/integration/test_multi_language.py` (3 tests updated in place), `tests/integration/test_reporting.py` (1 test updated), `tests/integration/test_timeout_recovery.py` (1 assertion updated), docs/12, docs/15, README. **Untouched:** `qa_agent/adapters.py`, `watch.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py`, `gitdiff.py`.

**Verified (2026-09-07):** `python tests/run_all.py` - **9/9 suites, 205 checks total, 61.2s**, every golden-file CLI comparison still byte-for-byte identical (ruff-only behavior unchanged, proven not assumed). `test_multi_analyzer.py` alone: 46/46.

**Dogfooded against a real mixed Python+JS project, genuine ruff and eslint installs (not mocks), not just unit tests:**
- Clean run: both tools' real findings (3 ruff, 3 eslint) in one report, deterministically ordered by file then line (`api.py` before `frontend.js` before clean `helpers.py`), `Tools:   eslint, ruff`.
- Broke eslint's config for real (deleted `eslint.config.js`): ruff's 3 real findings still printed in full, plus eslint's genuine, verbatim, multi-line failure message in its own "Analyzer errors" section, exit 2 - **reproduced identically through a live watch-mode `.js` save**, confirming the zero-`live_report.py`-changes design actually holds under a real running process, not just in theory.

**Deliberately deferred, not solved:** everything explicitly out of scope per the instruction (config system, enable/disable, severity policies, include/exclude paths, caching, parallel execution, performance work, additional analyzers, LLM, watch-mode redesign). One pre-existing, unrelated wording nuance noticed but not fixed: `_findings_lines()` says "no issues found" (not "not actually checked") when a tool fails on the only file in its batch, because `checked.extend()` happens before the `try` in `run()` - a Part 1 ordering choice, not a Part 3 regression, and redefining what "checked" means is a bigger, separate decision than this phase's scope.

**Status: Phase C Part 3 CLOSED.**

---

## Phase C, Part 4 — Configuration System (Design) (2026-09-07)

**Goal:** Design review only, no code. A per-project configuration system that naturally integrates into the existing architecture without redesigning the engine — following the same standard, depth, and reasoning as docs/13 and docs/14.

**Constraints given:** architecture-first, implementation-second; cover ~30 named topics (current architecture, why now, goals, proposed architecture, lifecycle, loading, discovery, precedence, validation, failure behavior, component responsibilities, dependency graph, format, no-config default, enable/disable analyzers, include/exclude paths, severity filtering "only if justified," per-adapter config, integration with Runner/Adapters/Unified Reporting, interaction with watch mode/`--git-diff`/`--output`/multi-analyzer execution, error isolation, performance, testing, dogfooding, trade-offs, future scalability, explicit deferrals); reason from the existing implementation, inspect current code first, justify every decision, avoid speculative abstraction, prefer extending existing mechanisms; explicitly exclude plugins, config inheritance, remote/user/global config, caching, concurrency, scheduling, watch-mode redesign, LLM, dashboards, policy engines, additional analyzers — anything excluded gets the literal phrase "Deferred because solving it now would expand Phase C Part 4."

**Produced:** [docs/16-configuration-system.md](./16-configuration-system.md).

**Reread the actual current code before designing** (adapters.py, runner.py, report.py, `__main__.py`, gitdiff.py, analysis_bridge.py — post Part 3, not recalled from memory) and identified exactly three things genuinely hardcoded today with no way to vary per-project: which adapters run (`ADAPTERS`, a literal tuple), which paths are ignored (`IGNORED_DIRS`, a literal frozenset), and severity reclassification (`SEVERITY_POLICY`, empty since Step 3, never exercised by any dogfooding run). All three are justified against *already-documented* real pain, not invented: `npm/cli`'s legacy-ESLint-config failure (docs/14) and the Part 3 mixed-project dogfood's permanent Analyzer-errors/exit-2 state are both currently unsolvable without editing this project's own source.

**Key design decisions:**
- **Scope narrowed on severity, explicitly.** "Severity filtering" (threshold suppression of real findings) has zero dogfooding evidence and would be this project's first-ever case of hiding a real finding — deferred with the required phrase. Moving the *existing* `SEVERITY_POLICY` into config was also deferred — it would touch every adapter's `parse()` signature, a bigger change than anything else in the design, for a setting that's been empty and unused for four parts running.
- **`adapters.py` stays completely untouched.** Config only ever selects a *subset* of the already-registered `ADAPTERS` tuple; it never edits how a known tool is invoked. This kept the "per-adapter configuration" item scoped to enable/disable only, with everything else (extra flags, custom config paths) explicitly deferred.
- **Two failure classes, kept genuinely separate.** A `ConfigError` (bad JSON, unrecognized key, a typo'd analyzer name) is a whole-run precondition failure — caught at the composition root exactly where `gitdiff.py`'s own `ToolError` already is, never folded into `RunResult.tool_errors` alongside genuinely isolated per-adapter failures it structurally isn't one of.
- **Discovery reuses `_batch_cwd()`'s already-tested common-ancestor logic** (Part 2) as its starting point, and ESLint's own already-cited upward-search precedent (docs/14) for the walk itself — one algorithm for every entry point (one-shot, `--git-diff`, watch), deliberately not special-cased per mode.
- **JSON over TOML**, argued explicitly on the project's own documented "Python 3.8+" floor (`tomllib` needs 3.11) rather than asserted by convention.
- **One piece of CLI surface added beyond the brief's named list, flagged rather than assumed:** an optional `--config <path>` override, justified by direct precedent (`--output`'s existing shape) and by the test suite's own need for it — explicitly called out as the one place easiest to trim if a smaller Part 4 is preferred.

**Status: Phase C Part 4 DESIGN PROPOSED — awaiting approval. No implementation started.**

---

## Phase C, Part 4 — Configuration System (Implementation) (2026-09-07)

**Goal:** Implement the approved design directly, no further design docs. One scope change from docs/16 at commissioning time: "basic severity filtering" (docs/16 section 17, argued as not-yet-justified and deferred) was explicitly requested and built - kept minimal: a `min_severity` floor over the two severities this project's adapters have ever produced ("warning"/"error"), applied once after merge/dedupe, its effect always visible in the report, never silent.

**Produced:** `qa_agent/config.py` (new); `runner.py`, `report.py`, `__main__.py`, `analysis_bridge.py` extended additively; `.qa-agent.json` as the discovered filename.

**Design, exactly as docs/16 specified, unchanged at implementation:** two-tier precedence (project file or hardcoded defaults, no inheritance); JSON; upward discovery, nearest wins, starting from cwd (one-shot/`--git-diff`) or `project_path` (watch); strict validation (unrecognized key, wrong type, or an analyzer name that doesn't match a real registered adapter all raise `ConfigError` immediately - a typo must never silently mean zero analysis); `ConfigError` caught at the composition root exactly where `gitdiff.py`'s own `ToolError` already is, never folded into `RunResult.tool_errors`; `adapters.py` untouched; `--config <path>` as the discovery override. `SEVERITY_LEVELS = {"warning": 1, "error": 2}` deliberately duplicated (not imported) between `config.py` and `runner.py` - the same small, explicitly-flagged duplication already accepted for `fsmonitor.py`'s ignore list vs. `runner.py`'s `IGNORED_DIRS` back in Phase B Part 2.

**A disabled adapter's files reuse the existing `Skipped` mechanism** with a distinct reason ("disabled by config" vs. "no tool configured for X") - no new report section needed for that case at all, just a second, already-computed extension index (built from the *unfiltered* `ADAPTERS`) to tell the two cases apart. A severity filter's effect gets one new, honest line ("N finding(s) hidden by the config's severity filter... not lost") in the same shared `_findings_lines()` both the CLI and watch mode already render through - zero changes needed to `live_report.py` itself, the same zero-touch result Part 2's adapter registration already proved out for that module.

**One real bug found by testing, not by inspection:** `harness.py`'s `run_agent()` set the subprocess's `cwd` to whatever a test asked for, but never ensured `qa_agent` stayed importable from there - `python -m qa_agent` resolves the package relative to cwd, so pointing cwd at a `TempProject` (needed for config discovery, which itself starts from cwd) broke with "No module named qa_agent." No existing test had ever exercised both "a non-default cwd" and "module resolution" at once before this phase. Fixed generically in the harness (`PYTHONPATH` always includes `REPO_ROOT` now, regardless of `env`/`cwd`), not worked around per-test.

**Files changed:** `qa_agent/config.py` (new - `Config`, `ConfigError`, `discover()`, `load()`, `resolve()`); `qa_agent/runner.py` (`collect_paths()`/`_walk()` gain an optional `ignored_dirs`; `_extension_index()` takes an explicit adapter set; `RunResult.filtered`; `run()` gains `config=None`, consumes it into an effective adapter set/ignore set/severity floor, computes the disabled-vs-unsupported skip reason, filters findings by severity last); `qa_agent/report.py` (`render()`/`render_markdown()` gain `config_path=None`; a filtered-count line; new `render_config_error()`); `qa_agent/__main__.py` (`--config` on both subcommands; resolves config once per invocation; catches `ConfigError`; threads config through `run()`/`render()`/`render_markdown()`/`AnalysisBridge`; watch banner and watched-extension set both derive from the config-filtered adapter set); `qa_agent/analysis_bridge.py` (`AnalysisBridge.__init__(config=None)`, held for the session, threaded into every `run()` call - `analyze_paths(paths)`'s own signature unchanged); `tests/harness.py` (the `run_agent()` PYTHONPATH fix above). **Untouched:** `qa_agent/adapters.py`, `watch.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py`, `gitdiff.py` - exactly as docs/16 predicted for every one of them.

**Tests added:** `tests/regression/test_config.py` (new, 28 checks - discovery, loading, every validation rule, `resolve()`'s precedence, no live tool needed); `tests/regression/test_multi_analyzer.py` (+5 tests/+14 checks - config consumption inside `run()` via fake adapters: `config=None` byte-identical to no config, adapter disabling with the honest skip reason, extra-ignore/include set arithmetic, severity filtering keeping unrecognized severities, no-filter-configured leaves everything untouched); `tests/integration/test_config_cli.py` (new, 26 checks - real CLI, real ruff, real fixture-installed eslint where needed: no-config regression, disable/ignore/include/malformed-JSON/typo'd-analyzer-name/`--config`-override, and two real-eslint scenarios proving a genuine "warning"-level finding gets hidden by `min_severity` while the real "error"-level one survives).

**Verified (2026-09-07):** `python tests/run_all.py` - **11/11 suites** (9 unchanged + 2 new), every check passes, 63.8s. Every pre-existing golden CLI fixture still byte-for-byte identical with no config file present - the hard invariant (docs/16 section 14) actually proven, not just asserted.

**Dogfooded with one real, hand-written `.qa-agent.json`** against a real mixed Python+JS project (genuine ruff and fixture-installed eslint, not mocks) — `{"analyzers": ["ruff", "eslint"], "ignore": ["vendor"], "min_severity": "error"}`:
- Clean run showed 5 real findings (down from 7 unfiltered), the `Config:` path line, a vendored third-party file's real bug genuinely invisible, and one real eqeqeq warning honestly reported as hidden rather than silently dropped.
- Re-run with `{"analyzers": ["ruff"], "ignore": ["vendor"]}` — the live watch-mode banner correctly read `Analyzers: ruff (.py)` instead of listing eslint, confirmed with zero `watch.py` changes.
- **One real mistake caught by the dogfooding itself, not by a test:** the first dogfood attempt was run from the qa_agent repo's own directory rather than the analyzed project's directory - discovery correctly found nothing there (matching cwd-based discovery exactly as designed) and fell back to defaults, which looked like a bug in the report until re-run from the right directory made every effect appear correctly. Left in the record here rather than quietly redone, since it's a real, honest illustration of the design's own "invoke from your project root" assumption (docs/16 section 7) actually mattering in practice.

**Deliberately deferred (per docs/16, unchanged at implementation):** per-rule severity reclassification via config (`SEVERITY_POLICY` stays hardcoded); per-adapter settings beyond enable/disable; glob/gitignore-style ignore patterns; fine-grained path-based include; config hot-reload in watch mode; inheritance, remote, and user/global configuration; caching, concurrency, scheduling, plugins, dashboards, policy engines, LLM features, additional analyzers.

**Status: Phase C Part 4 CLOSED.**

---

## Phase C, Part 5 — Additional Analyzer Integrations (2026-09-07)

**Goal:** Prove Parts 1-4's architecture scales beyond ruff+eslint by integrating two more real analyzers - implementation directly, no separate design doc. Recommended: Pyright, ShellCheck.

**Constraints given:** at least two real analyzers, preferring tools that exercise different kinds of tooling; reuse the existing adapter contract exactly, no engine special-casing; unified reporting/config/tool-isolation all preserved automatically; modify only files genuinely required; no plugins, dynamic loading, LLM, cloud, caching, parallel execution, scheduler/watch-mode redesign, GUI, dashboard, performance framework; tests for parsing, config enable/disable, unified reporting, mixed-language repos, tool isolation, and ruff/eslint regression; dogfood against a real repository using every analyzer; document performance and limitations found.

**Integrated:** **Pyright** (`.py`, pip-pinned in `requirements.txt` like ruff) and **ShellCheck** (`.sh`, no pip/npm distribution - fetched from its GitHub release into `tests/fixtures/shellcheck/fetch.ps1`, exactly like ESLint's own external-tool precedent). Both measured directly before writing any adapter code (real JSON shapes, real exit codes, real batching) - not assumed from documentation, matching every prior part's discipline.

**Pyright is the first real (not fake-adapter-only) exercise of two adapters sharing an extension** - registering it alongside ruff on `.py` is exactly the scenario Part 1's `_extension_index()` and Part 3's merge/sort/dedup were built to support without any further engine change, and this part is the proof.

**Real discoveries made only by testing, not by inspection - all fixed inside the two new adapters, zero engine changes:**
1. **Pyright reports its own "file" paths with a lowercase drive letter** (`c:\Users\...`), a Language Server Protocol/VSCode URI convention leaking into its plain-path JSON - confirmed via its own `file://` URI error message on a missing file. Left alone, this silently broke Part 3's "sorted by file" guarantee: pyright's findings always sorted *after* every other tool's, on every file, because `"c" > "C"` in a plain string comparison, regardless of the actual filename. Fixed with a small, adapter-owned normalizer (`_normalize_drive_letter()`) inside `PyrightAdapter.parse()` only.
2. **Windows' default `Path.write_text()` translates `\n` -> `\r\n`**, which ShellCheck correctly (if surprisingly) flags as `SC1017`. Broke a test fixture meant to be clean, not a real bug - fixed generically in `tests/harness.py`'s `TempProject.write()` (`newline=""`), the same "found a real harness gap through testing" pattern as Part 4's `PYTHONPATH` fix.
3. **A stray machine-wide `ruff` install had been silently backing every test in this project's history** (found while making pyright, which has no such accidental fallback, resolvable in tests) - `harness.run_agent()` now always prepends this interpreter's own Scripts/bin directory to PATH, so tests genuinely exercise the project's own pinned `.venv` tools, not whatever happens to be globally installed. Confirmed harmless (same ruff version either way) before relying on it.
4. **Registering a second `.py` adapter changes every existing golden file's `Tools:`/`Checked:` line** - not a regression (ruff's own findings are byte-for-byte identical; pyright genuinely finds nothing new on the Phase A fixtures, since they're lint issues, not type errors), but an expected, now-recorded consequence of the phase's own stated goal. Goldens re-recorded after manually verifying the new output was correct, not by blind regeneration.

**Files changed:** `qa_agent/adapters.py` (+`PyrightAdapter`, +`ShellCheckAdapter`, +`_normalize_drive_letter()`, `ADAPTERS` gains two entries), `qa_agent/runner.py`+`qa_agent/config.py` (`SEVERITY_LEVELS` extended for shellcheck's four-level scale and pyright's "information" spelling - additive, `warning`/`error`'s relative order unchanged, so existing configs are unaffected), `requirements.txt` (+`pyright==1.1.411`), `.gitignore` (+shellcheck binary), `tests/harness.py` (PATH fix, `TempProject.write()` newline fix, +shellcheck helpers), 4 golden files (re-recorded, verified), several existing tests updated where a 3rd/4th tool genuinely changed the correct expected output (adapter-count assertions, exact `Tools:`/`Analyzers:` strings, a PATH-cleared test now covering 2 failing tools instead of 1), `tests/regression/test_multi_analyzer.py` (+9 tests: pyright/shellcheck parsing incl. the drive-letter fix, adapter-contract update), `tests/regression/test_config.py` (1 assertion's "still-invalid" severity value updated, since "info" became valid), `tests/integration/test_additional_analyzers.py` (new, 8 tests), docs/12, README. **Untouched:** `report.py`, `__main__.py`'s config-consumption logic, `watch.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py`, `gitdiff.py`, `analysis_bridge.py` - config/reporting/watch-mode already generalized enough in Parts 1-4 to need nothing here.

**Verified (2026-09-07):** `python tests/run_all.py` - **12/12 suites**, every check passes, 76.7s (up from ~63s - see performance below).

**Dogfooded against a real, substantial external repository (`pypa/pip`, 661 `.py` files, 2 `.sh`, 1 vendored `.js`), using every registered analyzer at once, not a constructed scenario:**
- **ruff:** 3,599 findings (mostly in vendored third-party code, expected).
- **pyright:** 886 real type-checking findings on a not-strictly-typed real codebase.
- **shellcheck:** 56 findings, including a real, unplanned confirmation of a genuine Windows friction point: git's `core.autocrlf` line-ending conversion had checked out both `.sh` files with CRLF, which ShellCheck correctly flagged (`SC1017`) - not a false positive, a real property of analyzing a Windows git checkout.
- **eslint:** correctly reported `'eslint' is not installed or not on PATH` (genuinely absent from this run's PATH) for the one real vendored `.js` file this repo happens to contain - ruff, pyright, and shellcheck's real findings all still completed and were reported in full alongside it, an organic, unplanned, and for that reason more convincing demonstration of tool isolation than a constructed one.

**Performance observations (measured, not estimated):** ruff and shellcheck are both near-instant per invocation (0.06s, 0.08s on `pip`'s `src/`). **Pyright is the dominant cost, ~15s per invocation, driven by its own startup/type-checking setup rather than file count.** The full run (37.7s) took notably longer than pyright alone on one subset (15.0s), consistent with `MAX_FILES_PER_CALL=200` (chosen in Step 3 for Windows' command-line length limit, fine for every other tool's near-zero per-invocation cost) causing pyright to be invoked multiple times on a large `.py`-heavy project, each time re-paying its large fixed startup cost.

**Deliberately deferred:** a per-adapter batch size (or a single unchunked pyright call) to avoid paying its startup cost multiple times - **deferred to Phase C Part 6 because solving it now would expand this step** (an optimization, not a correctness issue, and explicitly out of scope: "performance framework"). Everything else out of scope per the brief - plugin marketplace, dynamic loading, LLM, cloud analyzers, caching, parallel execution, scheduler/watch-mode redesign, GUI, dashboard.

**Status: Phase C Part 5 CLOSED.**

## Phase C, Part 6 — Multi-Analyzer Hardening & Production Validation (2026-09-07)

**Goal:** Prove the engine built across Parts 1-5 is production-ready - validate, harden, simplify, document. No new features. Implementation directly, no design doc.

**Constraints given:** validate across real ecosystems (Python, JavaScript, mixed, multi-analyzer, one-analyzer-unavailable); verify analyzer isolation; exercise every config option; stress-test small/medium/large and measure, not guess; remove only justified technical debt; a documentation pass focused on current capabilities, not re-explaining the architecture; preserve backwards compatibility; modify only files genuinely required; add tests only for issues actually found. Explicitly out of scope: new analyzers, plugins, dynamic loading, cloud/LLM, dashboard/UI, parallel execution, caching, scheduler or watch-mode redesign.

**Dogfooded against six real, external repositories on disk** (not constructed fixtures) covering every scenario the brief asked for:
- **Facial-Emotions-Recognition** (small, pure Python, 2 files) and **flask** (medium, pure Python + one real `.sh` script, 169 checked files) - single- and multi-ecosystem Python.
- **Testing-Purpose/qaai** (real JavaScript monorepo, 166 checked `.js` files, no `eslint.config.js` anywhere) - the "one analyzer intentionally unavailable" case, and the run that found the real bug below.
- **Travel Website 3D** (real mixed Python + JavaScript + a much larger unsupported TypeScript periphery) and **bug-agent** (small JS/TS) - genuine mixed-ecosystem projects.
- **qa_agent's own source** - all-clean self-check, which is how the fsmonitor.py finding below was found.

**Real bug found and fixed - a silent one, at the exact "large real repository" stress boundary the brief asked to test:** `qaai`'s 166 files, at real filesystem depth, built a ~14,300-character `eslint` command. ESLint needs `use_shell=True` on Windows (its npm `.cmd` shim), which routes the launch through `cmd.exe` - and `cmd.exe`'s own line buffer is only ~8191 characters, a completely different (and much smaller) limit than the ~32k `CreateProcess` limit `MAX_FILES_PER_CALL=200` (Step 3) was actually calibrated for. Over that limit, `cmd.exe` rejected the command ("The command line is too long") with an *ordinary* exit code (1) and empty stdout - indistinguishable, to the old count-only chunker, from ESLint's own normal "ran cleanly, found real errors" exit 1. The result: real findings were silently lost and reported as "no issues found," with no error, no attribution, nothing - a direct violation of Part 3's "a failing tool never hides behind a clean result" contract, just never triggered before because no prior dogfooding target had both `use_shell=True` and a deep-enough, large-enough file set.

**Fix (`qa_agent/runner.py`):** `_chunks()` now bounds each batch by *estimated resolved command length* as well as by file count, with two budgets - `MAX_COMMAND_LENGTH_SHELL=6000` (a safe margin under cmd.exe's real ~8191) for `use_shell=True` adapters, `MAX_COMMAND_LENGTH_DIRECT=30000` for everything launched directly. A single file whose own resolved path already exceeds the budget still gets its own one-file batch (the length check only ever flushes a *non-empty* batch), so an oversized path is sent alone, never dropped. Re-running `qaai` after the fix now correctly reports `Analyzer errors (1): eslint: ... exited with code 2 ... couldn't find an eslint.config.js` - the true, actionable state - with exit code 2, matching the documented contract. No adapter changed; the fix is entirely inside the generic, adapter-blind chunker, consistent with every prior part's rule that engine changes must never be tool-specific.

**A second, minor real finding from dogfooding against qa_agent's own source:** pyright flagged `fsmonitor.py`'s `FileEvent.old_path: Path = None` - a real type/default mismatch (the field is documented and used as "renames only," i.e. genuinely optional, but was never typed that way). Fixed as `Path | None = None` - a one-line, zero-behavior-change type-correctness fix; dataclass runtime behavior is unaffected, only pyright's static check.

**Configuration validated end-to-end against real tool output**, not just unit tests: `analyzers` enable/disable (only-ruff, only-pyright, on the same real file), `ignore` (a whole subtree invisible to the walk), `include` (overriding the default `node_modules` ignore), `min_severity` (against flask's genuine mixed pyright `warning`/`error` output - 5 findings to 3, with the "2 hidden" note), `--config` with an explicit path, and three failure cases (`command`/`timeout` - not implemented keys - `min_severity: critical`, and an unknown analyzer name) - all rejected with `ConfigError`, exit 2, nothing analyzed, exactly as designed. No config-side bugs found; `command`/`timeout` overrides remain correctly not-implemented (goal 3 explicitly said "if implemented").

**Stress-tested and measured, not estimated:**
| Tier | Corpus | Result |
|---|---|---|
| Small | Facial-Emotions-Recognition, 2 files | ~1.0s |
| Small | bug-agent, 4 files (eslint only) | ~0.5s |
| Small-medium | Travel Website 3D, 8 checked of 96 real files | ~2.5s |
| Medium | flask, 169 checked of ~700 real files, 3 tools | ~4.8s |
| Large | 2,000 synthetic `.py` files, ruff+pyright (4,000 analyses) | ~12.2s |
| Startup cost, isolated | 1 trivial file per tool | ruff+pyright ~0.95s, eslint ~0.48s, shellcheck ~0.32s (native binary, fastest); all four together ~1.23s |

Confirms Part 5's own measurement: pyright dominates multi-tool wall time, tools run sequentially per adapter (not in parallel - explicitly out of scope for this part), so total cost is the *sum* of each tool's own startup, not the max. No measurement here justified an optimization beyond the correctness fix above, so none was made ("do not optimize unless measurements show a genuine problem").

**Technical debt removed (`.gitignore` only - the only genuine debt found):** a dead `.docs/` entry (leading-dot typo; the real directory is `docs/`, already tracked and meant to stay that way - docs/01-12 are committed deliverables) that matched nothing and did nothing, and a redundant `*.pyc` line fully subsumed by the existing `*.py[cod]` pattern two lines below. Both harmless no-ops, removed for clarity, zero behavior change (verified: `git check-ignore` before and after is identical for every real path). `ruff check qa_agent/ --select F401,F811,F841` confirms no unused imports or dead code anywhere in the package - nothing else qualified as "duplicate code, dead code, obsolete compatibility paths."

**Files changed:** `qa_agent/runner.py` (length-aware `_chunks()`, +`MAX_COMMAND_LENGTH_SHELL`/`MAX_COMMAND_LENGTH_DIRECT`, the real bug fix), `qa_agent/fsmonitor.py` (`FileEvent.old_path` type-correctness fix), `.gitignore` (2 dead/redundant lines removed), `tests/regression/test_multi_analyzer.py` (+5 tests: small-batch-unchanged, file-count-cap-unchanged, length-triggered-split-for-shell-only, oversized-single-file-never-dropped, findings-merge-correctly-across-split-batches), `docs/12-architecture.md` (one stale limitation corrected, one new limitation added - see below), `docs/17-hardening-and-validation.md` (new - the Part 6 documentation pass), README.md, docs/step-log.md. **Untouched:** `adapters.py`, `config.py`, `report.py`, `__main__.py`, `analysis_bridge.py`, `watch.py`, `debouncer.py`, `live_report.py`, `gitdiff.py` - the bug and its fix were entirely inside the generic chunker; nothing else needed to change.

**Verified (2026-09-07):** `python tests/run_all.py` - **12/12 suites**, every check passes (including the 5 new ones), 78-79s, all four real tool fixtures exercised (ruff, pyright, eslint, shellcheck).

**Documentation corrected:** `docs/12-architecture.md`'s "Severity is whatever ruff reports (currently always `error`)" bullet was accurate for ruff alone but stale as a general statement - pyright (`error`/`warning`/`information`) and shellcheck (`error`/`warning`/`info`/`style`) have reported genuinely varied severities since Part 5, and `min_severity` filtering across all four was validated end-to-end this part. Corrected in place. **New limitation documented, found by dogfooding a genuinely external, unrelated Python project (flask) for the first time in this project's history:** pyright runs from qa_agent's own `.venv`, which has no visibility into a target project's actual dependencies - on flask, this produced 234 "missing import" findings, real but almost entirely unactionable noise (docs/12), versus ruff's 1 genuine style finding on the same repository. Not a bug - pyright has no way to know which environment to check against unless told - but a real, now-documented trade-off of the current design.

**Deliberately deferred, still: a per-adapter batch size (or a single unchunked pyright call) to avoid re-paying pyright's startup cost across chunks** (named for this part by Part 5's own step-log entry). Measurements this part (medium: 4.8s/3 tools, large: 12.2s/4,000 analyses) do not show a genuine problem beyond what Part 5 already quantified and accepted, and the fix would mean adapter-specific special-casing inside an otherwise adapter-blind engine - contrary to "do not optimize unless measurements show a genuine problem" and "do not add speculative abstractions." **Deferred beyond Phase C because it expands the scope beyond production hardening.** Everything else out of scope per the brief - new analyzers, plugins, dynamic loading, cloud, LLM, dashboard, parallel execution, caching, scheduler/watch-mode redesign - was not touched.

**Final review of Parts 1-6:** **Strengths** - adapter-blind engine proven across 4 real tools and 2 shared/1 novel extension; execution and result isolation both proven with a real, organically-discovered failure (this part's eslint bug, and Part 5's organically-missing eslint on `pip`); deterministic merge/sort/dedup never contradicted by any real tool output; config system validated against real, not just synthetic, severity/analyzer/path data; zero crashes across all dogfooding, ever. **Remaining limitations** - pyright's cross-venv import-resolution noise on unrelated projects; sequential (not parallel) per-tool execution; pyright's per-chunk startup cost on very large `.py`-heavy projects; the Phase B watch-mode limitations (no feedback during sustained writes, no self-healing on a dead observer) are unchanged and still accurate. **Intentionally deferred to a future phase** - everything named out-of-scope above, plus the per-adapter batch size question just discussed.

**Status: Phase C Part 6 CLOSED. Phase C (Parts 1-6) complete.**

## Phase C, Part 7 — Second Additional Language Integration: Mypy (2026-09-07)

**Goal:** Add exactly one more production-quality analyzer, proving the architecture still needs nothing beyond a new adapter. Preferred order given: ShellCheck (already done, Part 5) -> Mypy -> Pyright improvements. Mypy chosen. Implementation directly, no design doc.

**Constraints given:** one new adapter only; reuse config, unified reporting, tool-error isolation, batching/chunking, severity filtering, include/exclude, watch mode, git-diff, output modes untouched; no plugins, caching, scheduling, parallel execution, dashboards, LLM, remote/cloud; focused tests; dogfood on a real repository; document only what changed.

**Measured mypy's real behavior directly before writing any adapter code** (this project's standing discipline, Parts 2/5/6): `-O json` prints one JSON object **per line** (JSON Lines), not a single array like every other adapter here. Exit 0 clean, 1 real findings - both handled exactly like every other adapter. Exit 2 is genuinely ambiguous, verified with real inputs: a missing file (empty stdout, a real crash) and a syntax error present anywhere in the batch (real JSON still on stdout, but mypy stops checking every *other* file and reports only the syntax error, confirmed order-independent) both produce exit 2. Resolved by simply never including 2 in `ok_exit_codes` - exactly like any other adapter's undeclared exit code, conservatively treated as a tool failure rather than guessed at, with zero change to the adapter contract (`parse()` still only ever sees stdout, never the exit code).

**A real bug found and fixed before it ever reached a test or a user - by dogfooding a genuine multi-file batch, not by inspection:** without an extra flag, mypy's own `"file"` field is **inconsistent within one invocation** - given several already-absolute input files, some come back relative to mypy's own cwd (whichever it judges reachable that way) and others stay absolute (whichever it does not), confirmed with a synthetic two-file batch and again for real on `flask`'s own `examples/` tree. Left alone, this would have silently broken Part 3's "sorted by file" guarantee exactly the way Part 5's pyright lowercase-drive-letter bug did: the same file's mypy finding and pyright finding would sort into different positions in the merged report. Fixed with one flag, `--show-absolute-path`, added to `MypyAdapter.build_command()` - no `parse()`-side normalizer needed at all, unlike pyright's `_normalize_drive_letter()`. Verified fixed end-to-end with a real two-file batch: both files' findings now sort correctly interleaved, with mypy's own path string matching pyright's and ruff's byte-for-byte.

**A third real mypy quirk found by dogfooding a real external repository (`flask`), not constructed:** two files legitimately named `conftest.py` in different, package-less subdirectories (`examples/tutorial/tests/` and `examples/javascript/tests/`) make mypy exit 2 with "Duplicate module named \"conftest\"" - its own module-resolution requires unique module names across one invocation's files, which two untracked `conftest.py`s violate. Not a bug in this project; a real property of how mypy maps files to modules. Handled correctly, automatically, by the same exit-2-is-conservatively-a-tool-error design used for the syntax-error case - no special-casing needed, and ruff's and pyright's real findings on the rest of the repository (243 of them) were unaffected, still reported in full.

**Severity extended additively:** `note` (e.g. `reveal_type()` output, or a hint mypy attaches as its own diagnostic line) added to `SEVERITY_LEVELS` in both `runner.py` and `config.py`, ranked alongside `style` - the same additive, order-preserving extension pattern Part 5 used for shellcheck's and pyright's own words.

**Files changed:** `qa_agent/adapters.py` (+`MypyAdapter`, `ADAPTERS` gains a fifth entry, module docstring updated), `qa_agent/runner.py`+`qa_agent/config.py` (`SEVERITY_LEVELS` +`"note"`), `requirements.txt` (+`mypy==2.3.1`; also corrected a now-stale comment on the `pyright` pin left over from before Part 6's cross-venv-noise discovery, noticed while editing this same file for a genuinely required reason), 4 golden files (re-recorded, verified mypy is silent on all of them - matching pyright/ruff's own established silence on lint-only fixtures), several existing tests updated where a fifth tool genuinely changed the correct expected output (exact `Tools:`/banner strings in `test_multi_analyzer.py`, `test_additional_analyzers.py`, `test_multi_language.py`; a PATH-cleared test in `test_reporting.py` now covering 3 failing tools, not 2; a single-file latency threshold in `test_storm.py` raised from 1.0s to 3.0s - three sequential cold tool starts on one file, not two, measured ~1.2-1.3s steady-state), `tests/regression/test_multi_analyzer.py` (+8 tests: JSON-Lines parsing, hint appending, note severity, malformed-line handling, the `--show-absolute-path` command check, adapter-contract update), `tests/integration/test_additional_analyzers.py` (+5 tests: real type error, three-.py-tools-on-one-file, config enable, the absolute-path end-to-end proof, the syntax-error-becomes-a-tool-error proof), docs/12, docs/step-log.md, README.md. **Untouched:** `report.py`, `__main__.py`, `analysis_bridge.py`, `watch.py`, `debouncer.py`, `live_report.py`, `gitdiff.py` - exactly the same "nothing outside the adapter" list Part 5 already proved, now proven a second time.

**Verified (2026-09-07):** `python tests/run_all.py` - **12/12 suites**, every check passes, 113.1s (up from ~104s - a fifth per-.py tool's own sequential cost, consistent with Part 6's documented "sequential, not parallel" characteristic).

**Dogfooded against two real external repositories:**
- **flask** (medium, 84 real `.py` files): all three `.py` tools ran; mypy hit the real duplicate-module case above (exit 2, correctly isolated - ruff's 1 and pyright's 234 findings still reported in full, exactly as Part 3 promises); ~3.9s total, comparable to or faster than Part 6's 4 tools despite one more now being registered, since mypy's own failure short-circuits before any real type-checking work.
- **Facial-Emotions-Recognition** (small, 2 real `.py` files): mypy independently confirmed the same cross-venv "missing import" limitation Part 6 already documented for pyright (`import-not-found` vs. pyright's `reportMissingImports` - two independently-worded, both-real findings for the same root cause, correctly not deduplicated since their message text genuinely differs) - not a new limitation, a second tool exhibiting the same already-known, already-documented one.
- Config validated with the new tool specifically: `{"analyzers": ["mypy"]}` alone correctly runs only mypy; severity filtering and `--config` both worked with no change, exactly as promised.

**Bugs found during implementation:** the path-inconsistency bug above (fixed before any test was written against the broken behavior, so no regression test exists *for the bug* - only for the fix, which is the correct thing to test). No other bugs; the engine needed zero changes.

**Deferred:** nothing new. Same out-of-scope list as Part 6 - plugins, caching, scheduling, parallel execution, dashboards, LLM, remote/cloud execution, an analyzer marketplace - none of it was touched.

**Status: Phase C Part 7 CLOSED.**
