# Step 15 — Unified Multi-Analyzer Reporting

**Status:** IMPLEMENTED (2026-09-07).
**Phase:** C, Part 3 of "Multi-Language Intelligence."
**Scope:** make findings from every registered analyzer read as one coherent result while keeping each finding's own attribution — merge, deterministic order, conservative deduplication, and result isolation (a failing analyzer never hides another's real findings). No new analyzer, no config system, no caching, no parallel execution.

This picks up exactly where [docs/14 §7](./14-first-multi-language-analyzer.md) left off: Part 2 built execution isolation (every adapter genuinely attempted independently) but deliberately deferred result isolation (a failing adapter still discarded an already-succeeded adapter's findings on the way out of `run()`), pending explicit sign-off. That sign-off is this phase.

---

## 1. What was already true, and what wasn't

**Already true, from Part 1/2, unchanged here:**
- `Finding` (`file, line, severity, message, tool`) is already the one shared representation every adapter parses into — this *is* item 1's "normalized internal representation." Nothing about its shape needed to change.
- `run()` already merges every adapter's findings into one `RunResult.findings` list (item 2) — it's been one list since Step 3.
- Execution isolation (Part 1) already guarantees every adapter is genuinely attempted, regardless of an earlier one's failure.

**Not true until now:**
1. **Order was incidental, not designed.** Findings landed in whichever order `sorted(batches.items())` (alphabetical by tool name) and each adapter's own chunk/subprocess output happened to produce them — stable in practice today only because there's never been a real reason for it to vary, not because anything enforced it.
2. **No deduplication existed at all.** Irrelevant while ruff and eslint claim disjoint extensions (`.py`/`.js` never overlap), but Part 1's own dispatch (`_extension_index()`) already supports two adapters claiming the same extension — the moment that happens (a second `.py` tool, per docs/13 §9), any issue both agree on would appear twice with no mechanism to collapse it.
3. **A failing adapter still discarded a succeeding one's results.** `run()` attempted every adapter (execution isolation), but then raised the first `ToolError` it collected, and that `raise` unwound past `return result` — the *caller* never saw the `RunResult` at all, succeeded findings included. Verified directly in Part 2's dogfooding and captured in a test (`test_deferred_known_limitation_failure_hides_other_tools_findings`) specifically designed to flip when this got fixed.

---

## 2. Design

### Deterministic ordering (item 3)

One `sort()` at the end of `run()`, key `(file, line, severity, message, tool)`. `tool` only ever breaks a tie — the same file+line+severity+message from two different tools — everything else already reads top-to-bottom by file exactly as a single-tool report always has.

**Verified compatible with existing behavior, not assumed:** the current golden fixtures (`cli_findings.txt`, `cli_directory.txt`) already show ruff's own findings in ascending line order within one file — exactly what this key produces. All golden comparisons still pass byte-for-byte.

### Deduplication (item 4)

A finding is a duplicate of another only if `(file, line, severity, message)` match **exactly** — `tool` is the only field allowed to differ. Applied *after* sorting, so which copy of a genuine duplicate survives is a deterministic function of the sort key (alphabetically-first tool wins), never incidental to adapter registration or dict iteration order.

Deliberately conservative, per the explicit instruction: two findings that merely share a file, or a line, or a message prefix, are two different findings and both survive. No fuzzy matching, no normalization beyond what `Finding` already carries.

**Untestable through ruff+eslint today** (they never share an extension), so tested directly against the merge/sort/dedup mechanism using two fake adapters that both claim a fake extension — proving the mechanism itself, not something that happens to work only for ruff and eslint's disjoint scopes. This mirrors exactly how Part 1 tested its own dispatch generality before a second real adapter existed.

### Result isolation / multi-tool error reporting (item 5)

`RunResult` gains one field: `tool_errors` — a list of `(tool_name, ToolError)` pairs. `run()`'s per-adapter `try/except` now appends to `result.tool_errors` instead of collecting into a local list and raising afterward. **`run()` no longer raises `ToolError` for an analyzer failure, ever** — it always returns a `RunResult`, with real findings from whichever adapters succeeded and named failures from whichever didn't.

This is a real, deliberate contract change to `run()`, not an additive one — every caller was audited:
- `__main__.py`'s one-shot CLI: the `try/except ToolError` wrapping `run()` is now only ever reachable via `changed_files()` (a `--git-diff` selection failure — "not a git repo," "bad ref" — a different class of error entirely, unrelated to analyzer failures, still raised exactly as before). Exit-code logic gains one new leading check: `tool_errors` present → exit 2, **before** the findings check — preserving the exact pre-Part-3 precedent that a tool failure was always exit 2, while now also printing whatever findings did succeed instead of hiding them.
- `analysis_bridge.py`: `analyze_paths()`'s `except ToolError` branch is now provably unreachable (confirmed: nothing in `run()`'s call graph raises `ToolError` any more) and was removed, along with its now-unused import. The remaining `except Exception` "last line of defence" is unchanged.
- `live_report.py`: **zero changes.** `outcome.ok` is now always `True` for any run that completes, so `_body()`'s existing `render_findings(outcome.result)` branch is the only one ever reached for an analyzer failure — and that already calls into the same `_findings_lines()` report.py updates for item 6. Watch mode gets the new "Analyzer errors" section for free, verified by a live, real `.js` save with a genuinely broken eslint config.

### Report rendering (item 6)

One new section in `_findings_lines()` (shared by `render()`, `render_findings()`, and — through `render_markdown()`'s own copy — the Markdown output), placed right after Findings and before Skipped/Missing:

```
Analyzer errors (1) - findings from these tools may be incomplete:
  eslint: 'eslint' exited with code 2: ...
```

Because this lives in the one shared function Phase B Part 5 built specifically for this kind of reuse, the one-shot CLI, `--output` Markdown, and the live watch-mode stream all render it identically with no per-caller special-casing.

### Attribution (item 7)

Untouched by construction — `Finding.tool` was always there, dedup only ever removes an entry whose `tool` differs from a *kept* entry's, and the kept entry still names its own producing tool.

---

## 3. Files changed

| File | Change | Why |
|---|---|---|
| `qa_agent/runner.py` | `RunResult.tool_errors` field; `_finding_sort_key()`, `_dedupe()`; `run()`'s loop appends to `result.tool_errors` instead of raising; a final sort+dedupe before returning. | Items 1–5. |
| `qa_agent/report.py` | `_findings_lines()` and `render_markdown()` each gain an "Analyzer errors" section. | Item 6. |
| `qa_agent/__main__.py` | Exit-code logic gains a leading `tool_errors` check; module docstring's exit-code list gets one clarifying line. | Item 6 (user-facing exit-code contract), item 5. |
| `qa_agent/analysis_bridge.py` | Removed the now-unreachable `except ToolError` branch and its now-unused import; `AnalysisOutcome`'s docstring updated to say why. | Direct consequence of `run()`'s contract change — dead code left in place would misrepresent a case that can no longer occur. |

**Untouched:** `qa_agent/adapters.py` (the normalized shape, `Finding`, was already right), `qa_agent/watch.py`, `qa_agent/fsmonitor.py`, `qa_agent/debouncer.py`, `qa_agent/live_report.py`, `qa_agent/gitdiff.py`.

---

## 4. Verified

- `python tests/run_all.py` — **9/9 suites**, every check, including all four golden-file CLI comparisons byte-for-byte identical — ruff-only behavior is unchanged.
- 4 new regression tests (46 total in that suite now) prove the merge/sort/dedup/result-isolation mechanisms directly, using fake adapters, independent of ruff/eslint's current disjoint scopes.
- 3 existing tests were updated, not broken by surprise: two because a tool error now renders on stdout as part of the one unified report rather than a bare stderr crash message (item 6, working as designed), and one — `test_deferred_known_limitation_failure_hides_other_tools_findings` — flipped from documenting the limitation to proving it fixed, exactly as its own docstring said to do when this landed.
- Dogfooded against a real mixed Python+JS project with genuine ruff and eslint installs (not mocks): a clean run shows both tools' real findings in one deterministically-ordered report; breaking eslint's config for real shows ruff's 3 real findings still printed in full, plus eslint's genuine, verbatim failure message in its own section, exit 2 — reproduced identically through the live watch-mode stream with a real file save.

## 5. What is intentionally NOT built here

Everything explicitly out of scope for this part: a config system, per-analyzer enable/disable, severity policies beyond what already exists (`SEVERITY_POLICY`), include/exclude paths, caching, parallel execution, performance work, additional analyzers, LLM features, or any watch-mode redesign beyond the zero-code-change reuse described above.

One pre-existing, unrelated wording nuance was noticed but not changed: when a tool fails on the *only* file in a batch, `_findings_lines()` still says "Findings: no issues found." (because `result.checked` was populated before the failure, per Part 1's existing `checked.extend()`-before-`try` ordering) even though that tool never actually produced a result. Not a Part 3 regression — this exact phrasing already existed for a ruff timeout in Part 2 — and fixing what "checked" means is a bigger, separate decision than this phase's scope.
