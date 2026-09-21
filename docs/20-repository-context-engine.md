# Step 20 — Phase F Part 2: Repository Context Engine

**Status:** IMPLEMENTED (2026-09-10).
**Phase:** F, Part 2 of "Project Intelligence" — the bridge between Part 1's deterministic `ProjectKnowledge` and any future AI orchestration layer (not built here — this part produces the bridge, it does not cross it).
**Scope:** one new public entry point, `build_repository_context(project)`, converting an already-discovered `ProjectKnowledge` into a richer, still entirely deterministic `RepositoryContext` — an ordered architecture summary, a repository-layout description, a list of factual constraints, and a curated set of known limitations. No AI, no subprocess, no network, no filesystem walk of its own — a pure transformation over facts Part 1 already verified.

## Why this exists, and what it deliberately does not do

Part 1 produces `ProjectKnowledge` but nothing consumes it. Before any future phase can hand repository understanding to an AI provider, that understanding needs to be assembled into something more directly useful than a flat set of tuples — an ordered summary, a description of the repository's shape, explicit facts, explicit gaps. This part builds exactly that, and stops there.

**The one architecture rule this whole part is built around, stated in the specifying prompt itself: nothing in `qa_agent/ai/` changes.** `RepositoryContext` is produced, printable, serializable, and independently testable — but no existing AI prompt builder is modified to consume it. Wiring it into real prompt assembly would change what an LLM is actually asked, and therefore its output — a genuine, deliberate integration decision for a later phase to make explicitly, with its own before/after comparison, not something to fold into a part whose own success criteria require zero AI-layer behavior change. `test_qa_agent_ai_package_is_completely_untouched_by_this_phase` exists specifically so this boundary can't silently erode.

## Architecture

```
qa_agent/project/
  context.py          the RepositoryContext dataclass (references ProjectKnowledge, never duplicates it)
  context_builder.py  build_repository_context() + serialization (to_dict/to_json) + render()
```

Mirrors Part 1's own `models.py`/`knowledge.py` split one layer up: the data shape lives separately from the assembly and presentation logic.

**`RepositoryContext` references `ProjectKnowledge` rather than duplicating any of its fields** — verified directly (`test_repository_context_does_not_duplicate_project_knowledge_fields`, computing the actual field-name overlap and asserting it's empty). Every raw fact (languages, frameworks, package managers, evidence, every file/directory list) is reached via `.project`; only genuinely new, derived information lives on `RepositoryContext` itself: `architecture_summary`, `repository_layout`, `constraints`, `known_limitations`, and `generated_at`.

**Not to be confused with `qa_agent/ai/context.py`'s `CodeContext`** (Phase D Part 2) — that is the surrounding source lines around *one finding*, used to build a per-finding repair/explanation prompt. `RepositoryContext` here describes the *whole repository* and has nothing to do with any one finding. Two similarly-named "context" concepts in two different packages; both modules' own docstrings state the distinction explicitly so it's never assumed away by a future reader (or a future AI session).

## Architecture summary — a fixed algorithm, not free text

Eight possible lines, in this fixed order, each included only when real evidence backs it: Application Type, Languages, Frameworks, Repository Structure, Package Managers, Backend Technologies, Frontend Technologies, Infrastructure. "Backend/Frontend Technologies" list only the actual framework names matching the same `_BACKEND_FRAMEWORKS`/`_FRONTEND_FRAMEWORKS` sets Part 1's own `application_type` classifier already uses (imported, not duplicated) — deliberately narrower than "Application Type," which can say `full-stack` purely from a Next.js `app/api/` directory with no named backend *framework* at all. Verified directly against a real project below: a Next.js/React repo with a real `app/api` route shows `Application Type: full-stack` but correctly omits a `Backend Technologies` line, since no backend framework was actually detected — a deliberate, documented distinction (`test_architecture_summary_backend_technologies_only_lists_real_backend_frameworks`), not an inconsistency.

## Repository layout — one documented implementation decision

The specifying prompt names the possible sections (Single Package/Monorepo, Backend, Frontend, Shared, Documentation, Configuration, Infrastructure) without fully specifying their evidence mapping. Implemented as: the repository-type label (Monorepo/Workspace/Single Package) from `ProjectKnowledge.repository_type`; Backend/Frontend from `application_type` *or* a real directory literally named `backend`/`api`/`server`/... (or `frontend`/`components`/`pages`/`public`) — a backend-only repository with no directory literally named "backend" (a bare Flask `app.py` at the root, say) is still honestly a backend; Shared from a real `shared`/`packages`/`libs`/`common` directory; Documentation/Configuration/Infrastructure from the corresponding non-empty file categories Part 1 already produces.

**A real gap found while building this, not by inspection:** Part 1's own `important_directories` never recognized `shared`/`packages`/`libs`/`common` as meaningful directory names at all — they weren't in Part 1's original scope. A test fixture with a real `shared/` directory containing real code produced no "Shared" layout entry, because the directory was invisible to `ProjectKnowledge` in the first place, not because the layout logic was wrong. Fixed by extending Part 1's `_IMPORTANT_DIR_NAMES` set in `detectors.py` with those four names — a small, well-justified, documented cross-part consistency fix (the same category of directory-name recognition Part 1 already does for `components`/`controllers`/etc.), not a redesign. Part 1's own regression suite was rerun afterward and still passes 127/127 unchanged.

## Constraints and known limitations — facts and absences, not advice

`constraints` states verifiable truths only ("Python project", "Requires Node", "Dockerized", "Monorepo", "Uses GitHub Actions") — checked directly against advice-shaped wording (`test_constraints_are_facts_not_recommendations`) to keep this honest over time, not just at review time.

`known_limitations` is a **curated, finite list of exactly six possible entries** — "No backend detected", "No frontend detected", "No package manager detected", "No CI configuration detected", "No container configuration detected", "No documentation detected" — each the verifiable negative result of a real check `ProjectKnowledge` already performed, never an open-ended enumeration of every possible absence (which would turn into noise). Verified directly that no other entry can ever appear (`test_known_limitations_is_always_a_subset_of_the_curated_list`), and that a completely empty repository produces exactly all six (`test_known_limitations_empty_repo_lists_everything`) while a real, filled-out project produces none for the facts it actually has.

## Error contract

`build_repository_context()` never raises — an unexpected internal error (proven with a deliberately broken stand-in object whose every attribute access raises) still returns a `RepositoryContext` referencing whatever was given, with the failure named in `known_limitations` rather than propagated. The same discipline `discover_project()` itself already follows in Part 1.

## Serialization

`context_to_dict()`/`context_to_json()` produce a plain, JSON-safe structure — every tuple becomes a list, every `DetectedItem` becomes `{"name": ..., "evidence": [...]}`, `ProjectKnowledge`'s `MappingProxyType` evidence map becomes a plain dict. Verified to round-trip losslessly through `json.dumps`/`json.loads` (`test_to_json_round_trips_without_losing_information`) and to contain nothing but plain `dict`/`list`/`str`/`int`/`float`/`bool`/`None` at every level (`test_to_dict_is_a_plain_json_safe_structure`).

## CLI

`python -m qa_agent discover <path> --context` — prints the existing Part 1 `ProjectKnowledge` report first, then the new `RepositoryContext` section, in that order. Without `--context`, output is unchanged from Part 1 (verified directly, `test_render_context_never_appears_in_plain_render_discovery_output`). Still read-only: no AI, no analyzers, no repair.

## Files changed

**New:** `qa_agent/project/context.py`, `context_builder.py`; `tests/regression/test_repository_context.py` (37 test functions, 82 checks); this document.
**Modified:** `qa_agent/project/__init__.py` (+exports); `qa_agent/project/detectors.py` (+4 directory names, see "Repository layout" above); `qa_agent/__main__.py` (+`--context` flag on the `discover` subcommand only); `README.md`, `docs/step-log.md`, `docs/12-architecture.md`.
**Untouched, byte-for-byte:** every file in `qa_agent/ai/`, `runner.py`, `adapters.py`, `report.py`, `watch.py`, `config.py`, and every module Part 1 already left alone — confirmed both by a direct source-grep test (`test_qa_agent_ai_package_is_completely_untouched_by_this_phase`) and by rerunning the full suite before and after this part.

## Test results

`python tests/run_all.py` — **25/26 suites** (the one failure is `integration/test_watch_pipeline.py`'s pre-existing, unrelated timing flake). `test_repository_context.py` alone: 82/82 checks. `test_project_discovery.py` (Part 1's own suite): still 127/127, unchanged in outcome, after the one small detector-set extension above.

## Dogfooding, two real projects, real output

**This project's own repository:**

```
Repository Context
  Architecture Summary:
    - Application Type: library
    - Languages: Python
    - Repository Structure: single-package
    - Package Managers: npm, pip
  Repository Layout: Single Package, Documentation, Configuration
  Constraints: Python project, Requires Python
  Known Limitations: No backend detected, No frontend detected,
                      No CI configuration detected, No container configuration detected
```

Honest and accurate — this project genuinely has no CI workflow and no Dockerfile.

**A real external Next.js project** (`D:\Major projects\lms-ai`):

```
Repository Context
  Architecture Summary:
    - Application Type: full-stack
    - Languages: JavaScript, TypeScript
    - Frameworks: Next.js, React
    - Repository Structure: single-package
    - Package Managers: npm
    - Frontend Technologies: Next.js, React
    - Infrastructure: CI
  Repository Layout: Single Package, Backend, Frontend, Documentation, Configuration, Infrastructure
  Constraints: JavaScript project, TypeScript project, Requires Node, Uses GitHub Actions
  Known Limitations: No container configuration detected
```

Confirms, live: Next.js and React both reported without collapsing; `full-stack` correctly reflected in the layout's `Backend` section despite no explicit backend framework (the `app/api` rule from Part 1, carried through correctly); the one real, accurate limitation (`lms-ai` genuinely has no Dockerfile).

## Bugs discovered

One, described above under "Repository layout" — the missing `shared`/`packages`/`libs`/`common` directory names in Part 1's `_IMPORTANT_DIR_NAMES`, found by a real test fixture, not by inspection.

## Deferred, per this phase's own explicit non-goals

Wiring `RepositoryContext` into any AI prompt builder (a deliberate, separate, later decision — this part's entire point is that it does *not* happen here); API testing, middleware validation, repair improvements, browser automation, runtime/Docker/test execution, autonomous workflows. All belong to Phase F Part 3 or later.

**Status: Phase F Part 2 CLOSED.**
