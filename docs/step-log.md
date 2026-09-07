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
