"""Phase F Part 2: the Repository Context Engine (docs/20-repository
-context-engine.md) - `qa_agent.project.build_repository_context()`, its
serialization, and the `discover --context` CLI flag.

Every fixture project is a real, throwaway directory on disk, discovered
for real through `discover_project()` first - `RepositoryContext` is never
built from a hand-constructed `ProjectKnowledge`, so these tests exercise
the real Part 1 -> Part 2 pipeline end to end, not Part 2 in isolation
against invented input.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.project import (  # noqa: E402
    RepositoryContext,
    build_repository_context,
    context_to_dict,
    context_to_json,
    discover_project,
    render_context,
)
from qa_agent.project.models import ProjectKnowledge  # noqa: E402

_PROJECT_KNOWLEDGE_FIELDS = frozenset(f.name for f in ProjectKnowledge.__dataclass_fields__.values())
_REPOSITORY_CONTEXT_FIELDS = frozenset(f.name for f in RepositoryContext.__dataclass_fields__.values())


class _Writer:
    def __init__(self, path):
        self.path = path

    write = TempProject.write


def _context_for(files):
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        for rel, text in files.items():
            writer.write(rel, text)
        result = discover_project(proj.path)
        return build_repository_context(result.project)
    finally:
        proj.__exit__(None, None, None)


# --- no field duplication --------------------------------------------------

def test_repository_context_does_not_duplicate_project_knowledge_fields(suite):
    """docs/20's explicit rule: RepositoryContext references ProjectKnowledge
    rather than re-declaring its fields.
    """
    overlap = _REPOSITORY_CONTEXT_FIELDS & _PROJECT_KNOWLEDGE_FIELDS
    suite.check(
        "no RepositoryContext field name duplicates a ProjectKnowledge field",
        overlap == set(),
        " (overlap: {})".format(overlap),
    )


def test_repository_context_holds_a_real_project_knowledge_reference(suite):
    context = _context_for({"package.json": '{"dependencies": {"react": "18.0.0"}}'})
    suite.check("context.project is a real ProjectKnowledge", isinstance(context.project, ProjectKnowledge))
    suite.check("context.project reflects real detection", "React" in {i.name for i in context.project.frameworks})


# --- architecture summary ---------------------------------------------------

def test_architecture_summary_next_js_project(suite):
    context = _context_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "app/api/x/route.ts": "export function GET() {}\n",
    })
    joined = " | ".join(context.architecture_summary)
    suite.check("architecture summary names Application Type", "Application Type: full-stack" in joined)
    suite.check("architecture summary names Frameworks", "Next.js" in joined and "React" in joined)
    suite.check("architecture summary names Frontend Technologies", "Frontend Technologies:" in joined)


def test_architecture_summary_fixed_order(suite):
    """docs/20's own algorithm: Application Type, Languages, Frameworks,
    Repository Structure, Package Managers, Backend, Frontend,
    Infrastructure - in that order, whichever subset actually applies.
    """
    context = _context_for({
        "requirements.txt": "flask==3.0\n",
        "app.py": "from flask import Flask\n",
        "Dockerfile": "FROM python:3.12\n",
    })
    order = [line.split(":")[0] for line in context.architecture_summary]
    expected_order = [
        "Application Type", "Languages", "Frameworks", "Package Managers",
        "Backend Technologies", "Infrastructure",
    ]
    suite.check(
        "architecture summary lines appear in the fixed relative order",
        order == expected_order,
        " (got {})".format(order),
    )


def test_architecture_summary_omits_sections_with_no_evidence(suite):
    context = _context_for({"README.md": "# empty\n"})
    suite.check(
        "an empty-ish repo's architecture summary has no Frameworks line",
        not any(line.startswith("Frameworks:") for line in context.architecture_summary),
    )
    suite.check(
        "an empty-ish repo's architecture summary has no Infrastructure line",
        not any(line.startswith("Infrastructure:") for line in context.architecture_summary),
    )


def test_architecture_summary_backend_technologies_only_lists_real_backend_frameworks(suite):
    context = _context_for({"requirements.txt": "django==5.0\n", "manage.py": "#!/usr/bin/env python\n"})
    joined = " | ".join(context.architecture_summary)
    suite.check("Django appears under Backend Technologies", "Backend Technologies: Django" in joined)
    suite.check("no Frontend Technologies line for a backend-only repo", "Frontend Technologies:" not in joined)


# --- repository layout -----------------------------------------------------

def test_repository_layout_single_package(suite):
    context = _context_for({"package.json": '{"dependencies": {"react": "18.0.0"}}'})
    suite.check("single-package repo -> 'Single Package' in layout", "Single Package" in context.repository_layout)
    suite.check("frontend evidence -> 'Frontend' in layout", "Frontend" in context.repository_layout)
    suite.check("no backend evidence -> 'Backend' absent", "Backend" not in context.repository_layout)


def test_repository_layout_monorepo(suite):
    context = _context_for({
        "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
        "apps/web/package.json": '{"dependencies": {"react": "18.0.0"}}',
    })
    suite.check("monorepo repo -> 'Monorepo' in layout, not 'Single Package'", "Monorepo" in context.repository_layout)
    suite.check("'Single Package' absent for a real monorepo", "Single Package" not in context.repository_layout)


def test_repository_layout_shared_directory(suite):
    context = _context_for({
        "pnpm-workspace.yaml": "packages:\n  - packages/*\n",
        "packages/ui/package.json": "{}",
        "shared/utils.ts": "export {}\n",
    })
    suite.check("a shared/ directory -> 'Shared' in layout", "Shared" in context.repository_layout)


def test_repository_layout_only_includes_sections_that_exist(suite):
    context = _context_for({"main.py": "print(1)\n"})
    suite.check(
        "a bare single-file repo has no Documentation section",
        "Documentation" not in context.repository_layout,
    )
    suite.check(
        "a bare single-file repo has no Infrastructure section",
        "Infrastructure" not in context.repository_layout,
    )


# --- constraints ----------------------------------------------------------

def test_constraints_python_project(suite):
    context = _context_for({"requirements.txt": "flask\n", "app.py": "print(1)\n"})
    suite.check("Python project -> 'Python project' constraint", "Python project" in context.constraints)
    suite.check("Python project -> 'Requires Python' constraint", "Requires Python" in context.constraints)
    suite.check("no Node dependency -> 'Requires Node' absent", "Requires Node" not in context.constraints)


def test_constraints_typescript_and_node(suite):
    context = _context_for({"package.json": "{}", "index.ts": "export {}\n"})
    suite.check("TypeScript project -> 'TypeScript project' constraint", "TypeScript project" in context.constraints)
    suite.check("TS/JS project -> 'Requires Node' constraint", "Requires Node" in context.constraints)


def test_constraints_dockerized(suite):
    context = _context_for({"Dockerfile": "FROM python:3.12\n", "main.py": "print(1)\n"})
    suite.check("a Dockerfile -> 'Dockerized' constraint", "Dockerized" in context.constraints)


def test_constraints_monorepo(suite):
    context = _context_for({"turbo.json": "{}", "apps/web/package.json": "{}"})
    suite.check("a monorepo -> 'Monorepo' constraint", "Monorepo" in context.constraints)


def test_constraints_github_actions(suite):
    context = _context_for({".github/workflows/ci.yml": "on: push\n"})
    suite.check(
        "a real .github/workflows file -> 'Uses GitHub Actions' constraint",
        "Uses GitHub Actions" in context.constraints,
    )


def test_constraints_are_facts_not_recommendations(suite):
    context = _context_for({"main.py": "print(1)\n"})
    for item in context.constraints:
        suite.check(
            "constraint '{}' contains no advice-shaped wording".format(item),
            not any(word in item.lower() for word in ("should", "consider", "recommend", "please")),
        )


# --- known limitations -----------------------------------------------------

_ALLOWED_LIMITATIONS = frozenset({
    "No backend detected", "No frontend detected", "No package manager detected",
    "No CI configuration detected", "No container configuration detected",
    "No documentation detected",
})


def test_known_limitations_is_always_a_subset_of_the_curated_list(suite):
    for files in (
        {"main.py": "print(1)\n"},
        {"package.json": '{"dependencies": {"react": "18.0.0"}}'},
        {},
    ):
        context = _context_for(files)
        extra = set(context.known_limitations) - _ALLOWED_LIMITATIONS
        suite.check(
            "known_limitations never invents an entry outside the curated list",
            extra == set(),
            " (extra: {})".format(extra),
        )


def test_known_limitations_empty_repo_lists_everything(suite):
    context = _context_for({})
    suite.check(
        "a completely empty repo has every curated limitation listed",
        set(context.known_limitations) == _ALLOWED_LIMITATIONS,
    )


def test_known_limitations_full_stack_project_has_no_backend_or_frontend_limitation(suite):
    context = _context_for({
        "package.json": '{"dependencies": {"react": "18.0.0"}}',
        "requirements.txt": "flask\n",
    })
    suite.check("a full-stack repo has no 'No backend detected'", "No backend detected" not in context.known_limitations)
    suite.check("a full-stack repo has no 'No frontend detected'", "No frontend detected" not in context.known_limitations)


def test_known_limitations_documented_repo_has_no_documentation_limitation(suite):
    context = _context_for({"README.md": "# hi\n", "main.py": "print(1)\n"})
    suite.check(
        "a repo with a real README has no 'No documentation detected'",
        "No documentation detected" not in context.known_limitations,
    )


# --- generated_at -----------------------------------------------------

def test_generated_at_is_a_real_iso_timestamp(suite):
    context = _context_for({"main.py": "print(1)\n"})
    suite.check(
        "generated_at looks like a real ISO-8601 timestamp",
        bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", context.generated_at)),
        " (got {!r})".format(context.generated_at),
    )


# --- serialization -----------------------------------------------------

def test_to_dict_contains_every_field(suite):
    context = _context_for({"package.json": '{"dependencies": {"next": "14.0.0"}}'})
    data = context_to_dict(context)
    suite.check("to_dict has a 'project' key", "project" in data)
    suite.check("to_dict has 'architecture_summary'", "architecture_summary" in data)
    suite.check("to_dict has 'repository_layout'", "repository_layout" in data)
    suite.check("to_dict has 'constraints'", "constraints" in data)
    suite.check("to_dict has 'known_limitations'", "known_limitations" in data)
    suite.check("to_dict has 'generated_at'", "generated_at" in data)
    suite.check("nested project dict has 'frameworks'", "frameworks" in data["project"])
    suite.check(
        "nested frameworks are plain {name, evidence} dicts",
        all("name" in f and "evidence" in f for f in data["project"]["frameworks"]),
    )


def test_to_json_round_trips_without_losing_information(suite):
    context = _context_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "app/api/x/route.ts": "export function GET() {}\n",
        "Dockerfile": "FROM node:20\n",
    })
    raw = context_to_json(context)
    try:
        parsed = json.loads(raw)
        valid_json = True
    except json.JSONDecodeError:
        parsed, valid_json = None, False
    suite.check("to_json produces valid, parseable JSON", valid_json)
    if valid_json:
        suite.check(
            "round-tripped JSON matches to_dict exactly",
            parsed == context_to_dict(context),
        )
        suite.check(
            "round-tripped JSON still names Next.js",
            "Next.js" in [f["name"] for f in parsed["project"]["frameworks"]],
        )


def test_to_dict_is_a_plain_json_safe_structure(suite):
    context = _context_for({"main.py": "print(1)\n"})
    data = context_to_dict(context)

    def _all_json_safe(value):
        if isinstance(value, dict):
            return all(isinstance(k, str) and _all_json_safe(v) for k, v in value.items())
        if isinstance(value, list):
            return all(_all_json_safe(v) for v in value)
        return isinstance(value, (str, int, float, bool)) or value is None

    suite.check("to_dict's structure is entirely plain dict/list/str/etc, no tuples or MappingProxyType", _all_json_safe(data))


# --- CLI (discover --context) -----------------------------------------

def test_cli_context_flag_prints_both_sections(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package.json", '{"dependencies": {"react": "18.0.0"}}')
        proc = run_agent(["discover", str(root), "--context"])
    suite.check("discover --context exits 0", proc.returncode == 0)
    suite.check("output includes the ProjectKnowledge report first", "Project Discovery Report" in proc.stdout)
    suite.check("output includes the Repository Context section", "Repository Context" in proc.stdout)
    suite.check(
        "ProjectKnowledge report appears before RepositoryContext",
        proc.stdout.index("Project Discovery Report") < proc.stdout.index("Repository Context"),
    )
    suite.check("context output names React", "React" in proc.stdout)


def test_cli_without_context_flag_omits_repository_context(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package.json", "{}")
        proc = run_agent(["discover", str(root)])
    suite.check(
        "discover without --context never prints Repository Context",
        "Repository Context" not in proc.stdout,
    )


def test_cli_context_flag_on_an_invalid_root_still_exits_cleanly(suite):
    proc = run_agent(["discover", "Z:/not/a/real/path/qa-agent", "--context"])
    suite.check("an invalid root with --context still exits 2, not a crash", proc.returncode == 2)
    suite.check("no traceback leaks to the user", "Traceback" not in proc.stdout)


# --- language/framework/monorepo/library coverage --------------------

def test_python_only_repository(suite):
    context = _context_for({"requirements.txt": "flask\n", "app.py": "from flask import Flask\n"})
    suite.check("python-only repo -> backend layout", "Backend" in context.repository_layout)
    suite.check("python-only repo -> no frontend layout", "Frontend" not in context.repository_layout)


def test_node_only_repository(suite):
    context = _context_for({"package.json": '{"dependencies": {"express": "4.18.0"}}', "server.js": "x\n"})
    suite.check("express-only repo -> backend layout", "Backend" in context.repository_layout)
    joined = " | ".join(context.architecture_summary)
    suite.check("express-only repo names Express in Backend Technologies", "Backend Technologies: Express" in joined)


def test_library_repository_has_no_frontend_or_backend_layout(suite):
    context = _context_for({"pyproject.toml": "[project]\nname = \"mylib\"\n"})
    suite.check("a bare library has no Backend layout section", "Backend" not in context.repository_layout)
    suite.check("a bare library has no Frontend layout section", "Frontend" not in context.repository_layout)
    suite.check("a bare library -> 'No backend detected' limitation", "No backend detected" in context.known_limitations)
    suite.check("a bare library -> 'No frontend detected' limitation", "No frontend detected" in context.known_limitations)


def test_mixed_repository_full_stack(suite):
    context = _context_for({
        "package.json": '{"dependencies": {"react": "18.0.0"}}',
        "requirements.txt": "flask\n",
    })
    suite.check("mixed repo -> Backend and Frontend both in layout", {"Backend", "Frontend"} <= set(context.repository_layout))
    joined = " | ".join(context.architecture_summary)
    suite.check("mixed repo architecture summary shows full-stack", "Application Type: full-stack" in joined)


def test_empty_repository_context(suite):
    context = _context_for({})
    suite.check("empty repo -> empty architecture summary", context.architecture_summary == ())
    suite.check("empty repo -> empty repository layout", context.repository_layout == ())
    suite.check("empty repo -> empty constraints", context.constraints == ())
    suite.check("empty repo -> every curated limitation present", set(context.known_limitations) == _ALLOWED_LIMITATIONS)


# --- determinism / repeatability ----------------------------------------

def test_repeatability_every_field_but_generated_at(suite):
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        writer.write("package.json", '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}')
        writer.write("app/api/x/route.ts", "export function GET() {}\n")
        result = discover_project(proj.path)
        first = build_repository_context(result.project)
        second = build_repository_context(result.project)
    finally:
        proj.__exit__(None, None, None)
    suite.check("architecture_summary is identical across two builds", first.architecture_summary == second.architecture_summary)
    suite.check("repository_layout is identical across two builds", first.repository_layout == second.repository_layout)
    suite.check("constraints is identical across two builds", first.constraints == second.constraints)
    suite.check("known_limitations is identical across two builds", first.known_limitations == second.known_limitations)


# --- error contract: never raise --------------------------------------

def test_build_repository_context_never_raises_on_a_broken_project_object(suite):
    class _Broken:
        """A ProjectKnowledge-shaped object whose every attribute access
        raises - proves build_repository_context() catches a genuinely
        unexpected failure rather than crashing the caller.
        """

        def __getattr__(self, name):
            raise RuntimeError("simulated failure reading {}".format(name))

    try:
        context = build_repository_context(_Broken())
        raised = False
    except Exception:
        raised = True
    suite.check("a broken project object never raises out of build_repository_context", not raised)
    if not raised:
        suite.check(
            "the failure is named in known_limitations instead",
            any("failed" in item.lower() for item in context.known_limitations),
        )


# --- isolation: no AI, no subprocess, no network ------------------------

def test_context_modules_never_import_qa_agent_ai(suite):
    pattern = re.compile(r"^\s*(import qa_agent\.ai|from \.\.ai\b|from \.ai\b|from qa_agent\.ai\b)", re.MULTILINE)
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "project"
    offending = []
    for name in ("context.py", "context_builder.py"):
        text = (package_dir / name).read_text(encoding="utf-8")
        if pattern.search(text):
            offending.append(name)
    suite.check("neither context.py nor context_builder.py imports qa_agent.ai", offending == [], " ({})".format(offending))


def test_context_modules_never_import_subprocess_network_or_ai_provider(suite):
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "project"
    offending = []
    for name in ("context.py", "context_builder.py"):
        text = (package_dir / name).read_text(encoding="utf-8")
        if "import subprocess" in text or "import requests" in text or "urllib.request" in text or "OllamaProvider" in text:
            offending.append(name)
    suite.check(
        "neither file imports subprocess/requests/urllib.request/an AI provider",
        offending == [],
        " ({})".format(offending),
    )


def test_qa_agent_ai_package_is_completely_untouched_by_this_phase(suite):
    """docs/20's own hard rule, scoped to *this phase's own files*
    (`context.py`/`context_builder.py`, already checked above): Phase F
    Part 2 itself never reaches into the AI layer.

    This does not - and, per docs/20's own text, was never meant to -
    forbid a *later* phase from reading `RepositoryContext` as input, the
    same one-directional exception `validator.py` (Phase E Part 3) already
    established for `runner.run`'s output. Phase G Part 3 (docs/23) is
    exactly that later phase: `diagnosis_parser.py`/`diagnosis_prompts.py`
    deliberately read `RepositoryContext` as real evidence for diagnosing a
    runtime failure - a documented, explicit decision, not a regression.
    Checking the whole `qa_agent/ai/` directory here (as an earlier version
    of this test did) stopped being precise the moment that legitimate,
    later integration existed - the same category of over-broad-glob
    staleness already fixed once before in `test_runtime_planner.py`
    (Phase G Part 2).
    """
    ai_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "ai"
    offending = []
    for name in ("explainer.py", "fixer.py", "summarizer.py", "repair.py", "repair_loop.py", "decision.py", "apply.py", "workspace.py", "validator.py"):
        text = (ai_dir / name).read_text(encoding="utf-8")
        if "RepositoryContext" in text or "build_repository_context" in text or "context_builder" in text:
            offending.append(name)
    suite.check(
        "no pre-existing (Phase D/E) AI module references RepositoryContext/build_repository_context/context_builder",
        offending == [],
        " ({})".format(offending),
    )


# --- backwards compatibility with existing AI/report output --------------

def test_render_context_never_appears_in_plain_render_discovery_output(suite):
    """Golden-style guard: the pre-existing `discover` command (no
    --context) must render byte-for-byte the same shape it did in Part 1 -
    RepositoryContext-only text must never leak into it.
    """
    from qa_agent.project import render as render_discovery
    result = discover_project(".")
    output = render_discovery(result)
    suite.check("plain render_discovery output never mentions 'Repository Context'", "Repository Context" not in output)
    suite.check("plain render_discovery output never mentions 'Known Limitations'", "Known Limitations" not in output)


if __name__ == "__main__":
    suite = Suite("Phase F Part 2: Repository Context Engine")
    sys.exit(suite.run([
        test_repository_context_does_not_duplicate_project_knowledge_fields,
        test_repository_context_holds_a_real_project_knowledge_reference,
        test_architecture_summary_next_js_project,
        test_architecture_summary_fixed_order,
        test_architecture_summary_omits_sections_with_no_evidence,
        test_architecture_summary_backend_technologies_only_lists_real_backend_frameworks,
        test_repository_layout_single_package,
        test_repository_layout_monorepo,
        test_repository_layout_shared_directory,
        test_repository_layout_only_includes_sections_that_exist,
        test_constraints_python_project,
        test_constraints_typescript_and_node,
        test_constraints_dockerized,
        test_constraints_monorepo,
        test_constraints_github_actions,
        test_constraints_are_facts_not_recommendations,
        test_known_limitations_is_always_a_subset_of_the_curated_list,
        test_known_limitations_empty_repo_lists_everything,
        test_known_limitations_full_stack_project_has_no_backend_or_frontend_limitation,
        test_known_limitations_documented_repo_has_no_documentation_limitation,
        test_generated_at_is_a_real_iso_timestamp,
        test_to_dict_contains_every_field,
        test_to_json_round_trips_without_losing_information,
        test_to_dict_is_a_plain_json_safe_structure,
        test_cli_context_flag_prints_both_sections,
        test_cli_without_context_flag_omits_repository_context,
        test_cli_context_flag_on_an_invalid_root_still_exits_cleanly,
        test_python_only_repository,
        test_node_only_repository,
        test_library_repository_has_no_frontend_or_backend_layout,
        test_mixed_repository_full_stack,
        test_empty_repository_context,
        test_repeatability_every_field_but_generated_at,
        test_build_repository_context_never_raises_on_a_broken_project_object,
        test_context_modules_never_import_qa_agent_ai,
        test_context_modules_never_import_subprocess_network_or_ai_provider,
        test_qa_agent_ai_package_is_completely_untouched_by_this_phase,
        test_render_context_never_appears_in_plain_render_discovery_output,
    ]))
