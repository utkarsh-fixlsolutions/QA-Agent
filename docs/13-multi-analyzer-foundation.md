# Step 13 — Multi-Analyzer Foundation (Design)

**Status:** APPROVED with two amendments (2026-09-07). Implementation follows this document.
**Phase:** C, Part 1 of "Multi-Language Intelligence."
**Scope of Part 1:** restructure the runner so it coordinates *any number* of analysis tools; register exactly one (ruff, unchanged). No new analyzers, no CLI/watch/report changes.

This document is the design review done before implementation, in the same form used for every step in Phases A and B: architecture, responsibilities, dependency graph, what changes, what doesn't, why it scales, trade-offs, and what is deliberately deferred.

**Amendments made on review, applied throughout this document:**

1. **No file renames.** `adapters.py` and `runner.py` keep their names — the rename would have widened the diff without changing behavior, and naming can be revisited in a dedicated cleanup phase later. `RuffAdapter` and the `ADAPTERS` registry keep their names too, for the same reason; `ADAPTERS` changes from a dict to a tuple, but the identifier stays.
2. **Multi-failure presentation policy deferred, scope narrowed accordingly.** Part 1 makes the invocation loop capable of running multiple registered adapters *independently* — one adapter's failure isolated from another's, both always attempted — while keeping output byte-identical with ruff alone. Deciding how to *show* two simultaneous tool failures at once is explicitly left for Part 2, when a real second adapter exists to design that against.

---

## 1. Where today's architecture already is, and isn't, ready for this

Before proposing changes it's worth being precise about what already exists, because the target picture in the brief —

```
Project -> Engine -> Multiple Analysis Tools -> Unified Findings
```

— is *closer to the current code* than the old diagram (`Runner -> Ruff`) suggests. [runner.py](../qa_agent/runner.py) already:

- groups files by which tool handles them (`batches.setdefault(adapter.name, ...)`),
- iterates over every distinct tool found, sorted by name,
- invokes each one generically (build a command, run it, parse its stdout) — nothing in `_invoke` or `run()` mentions ruff by name,
- merges every tool's findings into one `RunResult`.

So the *invocation and aggregation* mechanics are already tool-agnostic. What is **not** yet ready:

1. **The registry is `extension -> one tool`.** [adapters.py](../qa_agent/adapters.py) defines `ADAPTERS = {".py": RuffAdapter()}` — a dict keyed by extension, one tool per key. Part 2 will need Pyright *and* Mypy *and* ruff to all claim `.py`. A dict keyed by extension cannot hold two tools under one key without changing its value type — and that change is exactly the kind of runner-internal rework requirement 3 says adding a tool must never require.
2. **One tool's failure can erase another tool's results.** In `run()`, `_invoke` raising `ToolError` propagates straight out of the loop, past `return result` — including findings already collected from tools processed earlier in the same call. With one tool this is unobservable (there's nothing to lose). With two, a failing ESLint would silently discard a successful ruff run.

Everything else — `report.py` rendering generic `Finding` objects, `watch.py`'s lifecycle, `fsmonitor.py`/`debouncer.py`'s event pipeline — is already correctly tool-agnostic and needs nothing. Naming (`adapters.py`/`ADAPTERS`, "adapter" rather than "analysis tool") stays as-is per amendment 1 — a real gap, but a naming one, deliberately left for a later cleanup phase rather than bundled into a structural change.

---

## 2. Overall architecture

**Before:**

```
Project
  |
  v
runner.run()  ---dict lookup--->  ADAPTERS[".py"]  --->  RuffAdapter
  |                                                            |
  +---------------------- one tool per extension ----<---------+
  |
  v
RunResult (findings)
```

**After:**

```
Project
  |
  v
runner.run()  ---"which adapters claim this file?"--->  ADAPTERS (tuple)
  |                                                            |
  |                                    each: RuffAdapter, (future) EslintAdapter, ...
  |                                                            |
  +--------- every matching adapter invoked, failures isolated per adapter ---<---+
  |
  v
RunResult (unified findings, same shape as today)
```

The visible shape of the pipeline in [docs/12-architecture.md](./12-architecture.md) does not change — `run()` does exactly the job it does today, called the same way by `analysis_bridge.py` (watch mode) and `__main__.py` (one-shot). Only what's inside that function changes: dispatch by extension-ownership-per-adapter instead of a dict lookup, and independent per-adapter invocation instead of one shared try that aborts everything after it.

---

## 3. Component responsibilities

| Component | Responsibility | Change from today |
|---|---|---|
| **Runner** (`runner.py`) | Collect paths, decide which registered adapter(s) apply to each file, invoke each adapter as a subprocess (build command, run with timeout, handle exit code), parse its output, aggregate into one `RunResult`. Never mentions a specific tool by name. | Dispatch changes from "one dict lookup" to "which adapters claim this extension" (naturally more than one, when more than one is registered). Per-adapter error isolation added (§5). |
| **Adapter** (`adapters.py`) | Declares the extensions it claims, builds the exact CLI command for a batch of files, parses that tool's raw output into `Finding` objects. Knows nothing about the runner, watch mode, or reporting. | `RuffAdapter` gains an explicit `extensions` attribute (today implied by the external dict). `ADAPTERS` changes from a dict to a tuple of adapter instances. `Finding`, `ToolError`, `SEVERITY_POLICY` unchanged. |
| **Reporting** (`report.py`) | Render `RunResult` — findings, skipped, missing, errors — as terminal text or Markdown. | None. Already renders `finding.tool` generically; never assumed ruff. |
| **Watch mode** (`watch.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py`, `analysis_bridge.py`) | Lifecycle, filesystem events, debouncing, live output, bridging events to the runner. | None in behavior, no changes at all — neither file imports anything whose name or shape is changing. |
| **Configuration** (registration tuple in `adapters.py`) | Which adapters exist and which extensions each owns. | Was implicit (the `ADAPTERS` dict *was* the configuration); now explicit and owned by each adapter instance rather than assembled externally. |

This keeps the boundary from requirement 5 intact: the runner coordinates, the adapter analyzes, reporting reports, watch mode watches. The one thing that moves is *where the extension-ownership fact lives* — from a dict assembled outside every adapter, to an attribute each adapter declares about itself. That's a reduction in indirection, not an addition.

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

Unchanged — same nodes, same edges, before and after. Because nothing renames, `analysis_bridge.py` and `gitdiff.py` need no changes at all: both only ever import `ToolError` (and `run`), and neither name's module or shape moves. Adding a second adapter later adds a class *inside* `adapters.py` and a line in its registration tuple — it adds no new edge to this graph. That's the concrete sense in which requirement 9 ("the dependency graph should become simpler, not more complicated") is satisfied: it doesn't shrink, because it was already minimal, but it does not grow with tool count either.

---

## 5. Error isolation (requirement 7, precisely)

Today: `run()` iterates registered tools sorted by name; the first one to raise `ToolError` aborts the whole call, discarding any findings already gathered from tools processed earlier in that same call. Unobservable today (one tool), a real bug the moment a second tool exists.

Part 1 scope, precisely (per amendment 2): the invocation loop attempts **every** adapter that has matching files, regardless of whether an earlier one failed. Each adapter's `ToolError` is caught at the point of invocation, not left to propagate through the loop, so one adapter's failure can never stop another's files from being analyzed. After every adapter has been attempted, if any failed, `run()` raises — preserving the current external contract exactly (callers still catch a single `ToolError`, same as today).

With one adapter registered, this is **byte-identical** to current behavior: one adapter, attempted once, and if it fails, raised immediately after — indistinguishable from today's flow. The moment a second adapter exists, both are always attempted independently, and neither can starve the other.

What Part 1 deliberately does **not** decide: what happens when two adapters fail in the *same* run and both errors need to be shown at once, or whether the caller should see partial findings alongside a partial failure. That's a presentation policy question, not an execution-isolation question, and it can't honestly be resolved without a real second tool to design it against — left for Part 2.

---

## 6. Files that change

All changes below are additive (a new attribute, a registry that's a tuple instead of a dict, a loop that isolates errors per adapter) — no file is renamed, and no import path changes anywhere in the package. No change alters what a user sees for the current ruff-only case.

| File | Change |
|---|---|
| `qa_agent/adapters.py` | `RuffAdapter` gains `extensions = frozenset({".py"})`. `ADAPTERS = {".py": RuffAdapter()}` → `ADAPTERS = (RuffAdapter(),)`. `Finding`, `ToolError`, `SEVERITY_POLICY` untouched. |
| `qa_agent/runner.py` | Dispatch changes from `ADAPTERS.get(path.suffix)` to matching `path.suffix` against each registered adapter's `.extensions`. Per-adapter error isolation (§5) added to the invocation loop. `collect_paths`, `_walk`, `_chunks`, `_invoke`'s subprocess/timeout handling, `RunResult`, `IGNORED_DIRS`, `MAX_FILES_PER_CALL`, `ANALYSIS_TIMEOUT_SECONDS` — all unchanged. |
| `qa_agent/__main__.py` | The only call site touched outside the two files above. The watch-mode banner builder (`_watch_main`, currently `for extension, adapter in sorted(ADAPTERS.items())`) changes shape to iterate the `ADAPTERS` tuple and each adapter's `.extensions` — same computed banner text for the current single-adapter case. The `FileSystemMonitor(extensions=set(ADAPTERS), ...)` call site computes its extension set as the union of every adapter's `.extensions` instead of treating `ADAPTERS` as a dict of extensions. |
| `tests/**` | No changes expected. No test imports `ADAPTERS` or asserts on its dict shape (confirmed by search before writing this plan) — the one test that imports the `runner` module (`test_timeout_recovery.py`) only patches `runner.subprocess.run` and reads `runner.ANALYSIS_TIMEOUT_SECONDS`, neither of which moves. |

## 7. Files that remain untouched

`report.py`, `watch.py`, `fsmonitor.py`, `debouncer.py`, `live_report.py`, `analysis_bridge.py`, `gitdiff.py`, `__init__.py`, `requirements.txt`, and the entire `tests/` tree. None of these know a tool's name today, and none need to — and per amendment 1, no filename changes ripple into them either.

---

## 8. Why this scales naturally

Adding adapter N+1 (say, ESLint in a later part) becomes: write a class in `adapters.py` with `name`, `extensions`, `build_command`, `parse`; append one instance to `ADAPTERS`. That's it —

- The runner's dispatch loop already asks "which registered adapters claim this file's extension," so a second `.py` adapter (Mypy, Pyright) or a first `.js` adapter both fall out of the same loop with no branching added.
- Error isolation is already per-adapter, so a broken new tool can't take down a working one.
- Reporting, watch mode, and the CLI already only ever see `Finding` and `RunResult` — neither type gained a field, so nothing downstream needs to change when a tool is added.
- The dependency graph (§4) gains no new edges when a tool is added — only new content inside the one file already responsible for tool identity.

This is the concrete meaning of requirement 3: implementing the contract and registering the instance is the whole job.

---

## 9. Trade-offs

- **Registry becomes a tuple of self-describing adapters instead of a dict assembled from outside.** Slightly less obvious at a glance which extension maps to which tool (no single dict to eyeball), but it's the only structure under which two adapters can claim the same extension without changing the runner — which Part 2 will need immediately (Mypy/Pyright alongside ruff, both on `.py`). Judged worth it now rather than reworked twice.
- **Naming stays inconsistent with the brief's own vocabulary** ("adapter"/"runner" vs. "analysis tool"/"engine") — a real, acknowledged gap (amendment 1), deliberately left open rather than bundled into a structural-only change. Worth a dedicated small pass later so it doesn't quietly compound as more tools and docs accumulate.
- **No formal contract type (`Protocol`/ABC) for "adapter."** Documented in the module docstring instead, same as `adapters.py` documents "the contract every adapter satisfies" today. Matches existing house style — no other module in the codebase uses ABCs or `Protocol` — and enforcing the contract mechanically buys nothing until a second, differently-shaped adapter actually exists to get it wrong.
- **Sequential adapter invocation stays sequential.** Two adapters on the same large batch of files run one after another, not concurrently. Simpler, matches today's behavior exactly, and correct until adapter count or per-run latency actually makes it worth revisiting (requirement 8).

---

## 10. What is intentionally NOT being built in Part 1

- No new adapters (ESLint, Pyright, Mypy, ShellCheck) — registration tuple still has one entry.
- No file/module renames — deferred to a later cleanup phase (amendment 1).
- No config file or CLI flag to enable/disable specific adapters.
- No per-adapter timeout override — `ANALYSIS_TIMEOUT_SECONDS` stays global.
- No concurrent/parallel adapter execution.
- No policy for presenting **multiple simultaneous** adapter failures in one run (§5) — deferred until a second real adapter exists to design against (amendment 2).
- No formal `Protocol`/ABC contract type — the contract stays documented, not enforced.
- No change to `Finding`'s shape (requirement 6 — it already carries `tool`, already language-agnostic, nothing about it assumes ruff or Python).
- No CLI, watch-mode, or report output changes of any kind.
