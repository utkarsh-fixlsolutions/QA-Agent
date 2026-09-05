# Step 1 — QA Agent v1 Definition

**Status:** Confirmed
**Date:** 2026-09-05

## 1. Mission
A local, on-demand assistant that inspects real code changes in a repository and reports concrete, tool-verified issues — so problems are caught before they're committed or reviewed, without any cloud dependency.

## 2. What it DOES (v1)
- Accepts a **repo path** and/or a **set of changed files** (e.g. from `git diff`) as input.
- Runs **local, free, deterministic tools** appropriate to the files given (linters, type checkers, formatters-in-check-mode, simple static rules).
- Collects each tool's raw findings and normalizes them into one structured report.
- Outputs a single, readable report: **file, line/location, message, source tool** — nothing else.
- Runs when invoked — manually, via a git hook, or on a schedule (e.g. cron/Task Scheduler). It does not run continuously in the background.

## 3. What it does NOT do (v1)
- No full-repo continuous/background scanning — only runs when triggered.
- No LLM calls of any kind yet (local or cloud) — pure tool-output aggregation.
- No auto-fixing, no auto-committing, no PR comments, no CI integration.
- No invented findings — if no tool flags something, it is not reported. Silence is a valid, expected outcome.
- No UI/dashboard — output is a file/terminal report.
- No multi-repo, multi-agent, or plugin architecture — single repo, single run, single report.

## 4. How it works on the codebase (paths / diffs / scope)
- **Input modes** (either supported in v1):
  1. **Targeted mode** — a specific list of changed files (e.g. from `git diff --name-only`). This is the default/expected everyday use, matching the "react to changes" principle.
  2. **Path mode** — a specific file or directory path given explicitly by the caller, for on-demand checks outside a diff.
- The agent never decides on its own to scan an entire large repo — scope is always given to it by the caller (a diff, a file list, or an explicit path).
- **Decided (2026-09-05):** v1 is first validated against a **Python** target (see rationale in the step log). The design itself isn't language-locked — this only fixes which tool adapter gets built first.

## 5. Primary method: local free tools first
- For each file, the agent picks tools based on file type/extension — e.g. a linter and/or type checker appropriate to that language.
- Each tool must already be a standard, free, offline, well-known tool (no paid tiers, no telemetry-required cloud calls).
- The agent's own code does no "understanding" of correctness — it only shells out to tools, parses their output, and formats it. All judgment about what's a bug comes from the tool, not from invented reasoning.
- Local LLM (e.g. Ollama) is explicitly out of scope for v1 and may be added later purely as an optional, additional opinion layer — never a replacement for tool evidence.

## 6. Output format (high-level)
A single structured report per run, containing:
- **Run summary**: what was scanned (files/paths), which tools ran, timestamp.
- **Findings list**, each with:
  - File path
  - Line (and column, if the tool provides it)
  - Severity (as reported by the source tool, e.g. error/warning)
  - Message (verbatim or lightly normalized from the tool)
  - Source tool name
- **Clean run** case: explicitly states "no issues found" rather than an empty/ambiguous report.
- Exact serialization (plain text vs Markdown vs JSON) is an implementation detail for a later step — not decided here.

## 7. Success criteria for v1
- Given a real repo and a set of changed files, running the agent once produces a report that:
  1. Lists only findings that came directly from a real local tool's output (traceable, reproducible).
  2. Correctly identifies file + location for each finding.
  3. Runs to completion without needing any network/cloud call.
  4. Completes in a reasonable time for a typical small diff (seconds, not minutes).
  5. Produces "no issues found" cleanly when the tools report nothing.
- v1 is considered done when this works reliably on at least one real project you actually use — not a toy example.

## Resolved questions
1. **Language target:** Python (rationale: single free tool — ruff — gives fast, config-free, JSON-native output, fitting the "as minimal as possible" constraint better than a JS/TS multi-tool setup). Not a permanent lock-in; other languages can be added later as additional tool adapters.
2. **Target repo:** No existing local repo yet. Real GitHub repos will be pointed at this agent once it's built. For now, a small scaffolded test repo (with a few deliberate, known issues) will be used to verify the agent during Step 2/3 build-out.
