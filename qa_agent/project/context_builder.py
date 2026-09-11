"""Pure functions that build a `RepositoryContext` from an already-built
`ProjectKnowledge`, and that serialize/render one (Phase F Part 2,
docs/20-repository-context-engine.md).

Mirrors `knowledge.py`'s own role in Part 1 one layer up: `context.py`
holds the data shape, this module holds the assembly and presentation
logic. Nothing here touches the filesystem, calls a subprocess, an AI
provider, or a network - it only ever reads plain attributes off the
`ProjectKnowledge` it is given. `build_repository_context()` never raises -
an unexpected internal error still returns a `RepositoryContext`, with the
failure named in `known_limitations` rather than propagated, matching this
package's Part 1 "never raise to a caller" discipline.

This module deliberately does not modify, import from, or get imported by
anything in `qa_agent/ai/` (docs/20's own explicit architecture rule for
this phase): `RepositoryContext` is produced and made available - printable,
serializable, independently testable - but nothing in this phase wires it
into any AI prompt builder. Doing so would change what an LLM is actually
asked, and therefore its output - a real, deliberate integration decision
for a later phase to make explicitly, not something to smuggle into a part
whose own success criteria require zero AI-layer behavior change.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .context import RepositoryContext
from .detectors import _BACKEND_FRAMEWORKS as _BACKEND_FRAMEWORK_NAMES
from .detectors import _FRONTEND_FRAMEWORKS as _FRONTEND_FRAMEWORK_NAMES

# --- architecture summary -------------------------------------------------
#
# Fixed order (docs/20's own algorithm), each line included only when real
# evidence backs it - never invented, never reordered.


def _architecture_summary(project):
    lines = []
    if project.application_type != "unknown":
        lines.append("Application Type: {}".format(project.application_type))
    if project.languages:
        lines.append("Languages: {}".format(", ".join(i.name for i in project.languages)))
    if project.frameworks:
        lines.append("Frameworks: {}".format(", ".join(i.name for i in project.frameworks)))
    if project.repository_type != "unknown":
        lines.append("Repository Structure: {}".format(project.repository_type))
    if project.package_managers:
        lines.append(
            "Package Managers: {}".format(", ".join(i.name for i in project.package_managers))
        )
    backend = sorted(i.name for i in project.frameworks if i.name in _BACKEND_FRAMEWORK_NAMES)
    if backend:
        lines.append("Backend Technologies: {}".format(", ".join(backend)))
    frontend = sorted(i.name for i in project.frameworks if i.name in _FRONTEND_FRAMEWORK_NAMES)
    if frontend:
        lines.append("Frontend Technologies: {}".format(", ".join(frontend)))
    infrastructure = []
    if project.docker_files:
        infrastructure.append("Docker")
    if project.ci_files:
        infrastructure.append("CI")
    if infrastructure:
        lines.append("Infrastructure: {}".format(", ".join(infrastructure)))
    return tuple(lines)


# --- repository layout ------------------------------------------------
#
# Which structural "zones" a repository has, evidence-based. Not spelled
# out to this level of precision in docs/20 itself - a documented
# implementation decision, not an assumption: a directory counts toward a
# zone by its own recognized name (Part 1's `important_directories`), and
# "Backend"/"Frontend" are additionally backed by `application_type` alone,
# since a backend-only repository with no directory literally named
# "backend" (e.g. a bare Flask `app.py` at the root) is still, honestly, a
# backend.

_SHARED_DIR_NAMES = frozenset({"shared", "packages", "libs", "common"})
_BACKEND_DIR_NAMES = frozenset({"backend", "api", "server", "controllers", "routes", "services"})
_FRONTEND_DIR_NAMES = frozenset({"frontend", "components", "pages", "public"})


def _repository_layout(project):
    sections = []
    if project.repository_type == "monorepo":
        sections.append("Monorepo")
    elif project.repository_type == "workspace":
        sections.append("Workspace")
    elif project.repository_type == "single-package":
        sections.append("Single Package")

    dir_names = {d.rsplit("/", 1)[-1] for d in project.important_directories}
    has_backend = project.application_type in ("backend", "full-stack") or bool(dir_names & _BACKEND_DIR_NAMES)
    has_frontend = project.application_type in ("frontend", "full-stack") or bool(dir_names & _FRONTEND_DIR_NAMES)
    if has_backend:
        sections.append("Backend")
    if has_frontend:
        sections.append("Frontend")
    if dir_names & _SHARED_DIR_NAMES:
        sections.append("Shared")
    if project.documentation_files or "docs" in dir_names:
        sections.append("Documentation")
    if project.configuration_files:
        sections.append("Configuration")
    if project.docker_files or project.ci_files:
        sections.append("Infrastructure")
    return tuple(sections)


# --- constraints ------------------------------------------------------
#
# Facts, never recommendations (docs/20's own rule) - every entry here is
# something that is verifiably true of the repository, not advice about it.

_NODE_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_PYTHON_PACKAGE_MANAGERS = frozenset({"pip", "poetry", "uv"})


def _constraints(project):
    items = []
    for language in sorted(i.name for i in project.languages):
        items.append("{} project".format(language))
    if project.repository_type == "monorepo":
        items.append("Monorepo")
    if project.docker_files:
        items.append("Dockerized")
    language_names = {i.name for i in project.languages}
    if language_names & {"JavaScript", "TypeScript"}:
        items.append("Requires Node")
    if "Python" in language_names:
        items.append("Requires Python")
    if any(f.startswith(".github/workflows/") for f in project.ci_files):
        items.append("Uses GitHub Actions")
    return tuple(items)


# --- known limitations -------------------------------------------------
#
# A curated, finite list only (docs/20's own explicit rule) - never an
# open-ended enumeration of every possible absence, which would turn into
# noise rather than signal. Each is a verifiable negative result of a real
# check already performed by ProjectKnowledge, not a guess.


def _known_limitations(project):
    limitations = []
    if project.application_type not in ("backend", "full-stack"):
        limitations.append("No backend detected")
    if project.application_type not in ("frontend", "full-stack"):
        limitations.append("No frontend detected")
    if not project.package_managers:
        limitations.append("No package manager detected")
    if not project.ci_files:
        limitations.append("No CI configuration detected")
    if not project.docker_files:
        limitations.append("No container configuration detected")
    if not project.documentation_files:
        limitations.append("No documentation detected")
    return tuple(limitations)


def build_repository_context(project):
    """The one public entry point. Never raises: an unexpected internal
    error still returns a `RepositoryContext` referencing the given
    `project`, with the failure named in `known_limitations` rather than
    propagated - the same "never invent success, never crash a caller,
    always say honestly what went wrong" discipline `discover_project()`
    itself already follows in Part 1.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        return RepositoryContext(
            project=project,
            architecture_summary=_architecture_summary(project),
            repository_layout=_repository_layout(project),
            constraints=_constraints(project),
            known_limitations=_known_limitations(project),
            generated_at=generated_at,
        )
    except Exception as exc:  # noqa: BLE001 - must never crash a caller
        return RepositoryContext(
            project=project,
            known_limitations=("repository context generation failed: {!r}".format(exc),),
            generated_at=generated_at,
        )


# --- serialization -------------------------------------------------------
#
# dict/JSON without losing information (docs/20's own requirement) - every
# tuple becomes a list, every DetectedItem becomes a small {name, evidence}
# object, and ProjectKnowledge's MappingProxyType evidence map becomes a
# plain dict. Round-trips through json.dumps/json.loads losslessly for
# every field this phase defines (tuples becoming lists on the way back is
# the one, expected, universal JSON round-trip property - not a defect).


def _detected_items_to_list(items):
    return [{"name": item.name, "evidence": list(item.evidence)} for item in items]


def _project_knowledge_to_dict(project):
    return {
        "root_path": project.root_path,
        "repository_type": project.repository_type,
        "application_type": project.application_type,
        "languages": _detected_items_to_list(project.languages),
        "frameworks": _detected_items_to_list(project.frameworks),
        "package_managers": _detected_items_to_list(project.package_managers),
        "build_systems": _detected_items_to_list(project.build_systems),
        "workspace_structure": list(project.workspace_structure),
        "monorepo_packages": list(project.monorepo_packages),
        "important_directories": list(project.important_directories),
        "important_files": list(project.important_files),
        "configuration_files": list(project.configuration_files),
        "runtime_files": list(project.runtime_files),
        "documentation_files": list(project.documentation_files),
        "specification_files": list(project.specification_files),
        "dependency_files": list(project.dependency_files),
        "environment_files": list(project.environment_files),
        "docker_files": list(project.docker_files),
        "ci_files": list(project.ci_files),
        "test_directories": list(project.test_directories),
        "entry_points": list(project.entry_points),
        "ignored_paths": list(project.ignored_paths),
        "evidence": {key: list(value) for key, value in project.evidence.items()},
    }


def to_dict(context):
    """A plain, JSON-safe `dict` - every tuple becomes a list, every
    `DetectedItem` becomes `{"name": ..., "evidence": [...]}`.
    """
    return {
        "project": _project_knowledge_to_dict(context.project),
        "architecture_summary": list(context.architecture_summary),
        "repository_layout": list(context.repository_layout),
        "constraints": list(context.constraints),
        "known_limitations": list(context.known_limitations),
        "generated_at": context.generated_at,
    }


def to_json(context, indent=2):
    return json.dumps(to_dict(context), indent=indent, sort_keys=False)


def render(context):
    """Human-readable rendering for the `discover --context` CLI flag -
    plain text, matching `knowledge.render()`'s own style in Part 1.
    """
    lines = ["Repository Context", ""]

    def _section(title, items):
        if not items:
            return
        lines.append("  {}:".format(title))
        lines.extend("    - {}".format(item) for item in items)
        lines.append("")

    _section("Architecture Summary", context.architecture_summary)
    _section("Repository Layout", context.repository_layout)
    _section("Constraints", context.constraints)
    _section("Known Limitations", context.known_limitations)
    lines.append("  Generated at: {}".format(context.generated_at))
    return "\n".join(lines).rstrip() + "\n"
