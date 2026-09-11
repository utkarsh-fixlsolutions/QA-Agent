"""Assembles a `ProjectKnowledge` model from already-collected filesystem
facts, and renders a `ProjectDiscoveryResult` for the `discover` CLI
command (Phase F Part 1, docs/19-project-discovery-engine.md).

This module never touches the filesystem itself - `build_knowledge()` only
combines what `discovery.py`'s single walk already gathered and what
`detectors.py`'s pure functions already computed from it. Keeping assembly
separate from both the walk and the individual detection rules mirrors
`report.py`'s own role for the deterministic analyzer pipeline: `runner.py`
walks and invokes, `adapters.py` parses each tool's own output, `report.py`
only ever formats an already-finished `RunResult` - the same three-way split
applied here to project discovery instead of static analysis.
"""

from __future__ import annotations

from . import detectors
from .detectors import (
    CATEGORY_CI,
    CATEGORY_CONFIGURATION,
    CATEGORY_DEPENDENCY,
    CATEGORY_DOCKER,
    CATEGORY_DOCUMENTATION,
    CATEGORY_ENVIRONMENT,
    CATEGORY_RUNTIME,
    CATEGORY_SPECIFICATION,
)
from .models import ProjectKnowledge, _frozen_evidence_map


def build_knowledge(root, files, dirs):
    """`files`/`dirs`: sorted tuples of POSIX-style paths relative to
    `root`, already filtered of every ignored directory - exactly what
    `discovery.py`'s single walk produces. Always returns a fully-formed
    `ProjectKnowledge`, even for an empty repository (every field is then
    simply empty, never `None` - a `ProjectKnowledge` object always exists
    whenever this function is called at all; discovery.py itself is the
    layer that decides whether to call it, based on the walk having
    succeeded).
    """
    languages = detectors.detect_languages(files)
    package_managers = detectors.detect_package_managers(root, files)
    build_systems = detectors.detect_build_systems(files)
    frameworks = detectors.detect_frameworks(root, files)
    categories = detectors.categorize_files(files)
    important_directories = detectors.detect_directories(dirs)
    test_directories = detectors.detect_test_directories(dirs)
    repository_type, repo_type_evidence = detectors.detect_repository_type(root, files)
    workspace_structure, monorepo_packages = detectors.detect_workspace_structure(
        root, files, repository_type
    )
    application_type, app_type_evidence = detectors.detect_application_type(root, files, frameworks)

    important_files = tuple(sorted({rel for paths in categories.values() for rel in paths}))

    evidence = _frozen_evidence_map({
        "repository_type": repo_type_evidence,
        "application_type": app_type_evidence,
    })

    return ProjectKnowledge(
        root_path=str(root),
        repository_type=repository_type,
        application_type=application_type,
        languages=languages,
        frameworks=frameworks,
        package_managers=package_managers,
        build_systems=build_systems,
        workspace_structure=workspace_structure,
        monorepo_packages=monorepo_packages,
        important_directories=important_directories,
        important_files=important_files,
        configuration_files=categories[CATEGORY_CONFIGURATION],
        runtime_files=categories[CATEGORY_RUNTIME],
        documentation_files=categories[CATEGORY_DOCUMENTATION],
        specification_files=categories[CATEGORY_SPECIFICATION],
        dependency_files=categories[CATEGORY_DEPENDENCY],
        environment_files=categories[CATEGORY_ENVIRONMENT],
        docker_files=categories[CATEGORY_DOCKER],
        ci_files=categories[CATEGORY_CI],
        test_directories=test_directories,
        entry_points=categories[CATEGORY_RUNTIME],
        ignored_paths=(),
        evidence=evidence,
    )


def _section(title, items):
    if not items:
        return ""
    lines = ["  {}:".format(title)]
    lines.extend("    - {}".format(item) for item in items)
    return "\n".join(lines) + "\n"


def _detected_section(title, detected_items):
    if not detected_items:
        return ""
    lines = ["  {}:".format(title)]
    for item in detected_items:
        lines.append("    - {}  (evidence: {})".format(item.name, ", ".join(item.evidence)))
    return "\n".join(lines) + "\n"


def render(result):
    """Human-readable rendering of one `ProjectDiscoveryResult` - the
    entire output of `python -m qa_agent discover <path>`. Deliberately
    plain text, not Markdown/JSON (report.py's two formats are for the
    analyzer pipeline; this command exists for dogfooding and debugging
    per docs/19, not for piping into another tool yet).
    """
    lines = ["Project Discovery Report", ""]
    lines.append("  Status:  {}".format(result.status))
    lines.append("  Elapsed: {:.3f}s".format(result.elapsed_time))
    lines.append("  Scanned: {} file(s), {} ignored".format(result.scanned_files, result.ignored_files))
    lines.append("")

    if result.errors:
        lines.append("  Errors:")
        lines.extend("    - {}".format(e) for e in result.errors)
        lines.append("")
    if result.warnings:
        lines.append("  Warnings:")
        lines.extend("    - {}".format(w) for w in result.warnings)
        lines.append("")

    project = result.project
    if project is None:
        return "\n".join(lines).rstrip() + "\n"

    lines.append("  Root:             {}".format(project.root_path))
    lines.append("  Repository type:  {}".format(project.repository_type))
    lines.append("  Application type: {}".format(project.application_type))
    lines.append("")

    body = "".join([
        _detected_section("Languages", project.languages),
        _detected_section("Frameworks", project.frameworks),
        _detected_section("Package managers", project.package_managers),
        _detected_section("Build systems", project.build_systems),
        _section("Workspace structure", project.workspace_structure),
        _section("Monorepo packages", project.monorepo_packages),
        _section("Important directories", project.important_directories),
        _section("Test directories", project.test_directories),
        _section("Configuration files", project.configuration_files),
        _section("Runtime files / entry points", project.runtime_files),
        _section("Documentation files", project.documentation_files),
        _section("Specification files", project.specification_files),
        _section("Dependency files", project.dependency_files),
        _section("Environment files (presence only - contents never read)", project.environment_files),
        _section("Docker files", project.docker_files),
        _section("CI files", project.ci_files),
    ])
    lines.append(body.rstrip())
    return "\n".join(lines).rstrip() + "\n"
