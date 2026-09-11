"""Deterministic detection rules for the Project Discovery Engine (Phase F
Part 1, docs/19-project-discovery-engine.md).

Every function here is a pure function over an already-collected file list
(and, where a fact cannot be told from a filename alone, that file's own
content) - none of them touch the filesystem beyond reading the small,
well-known manifest files this module explicitly names (package.json,
pyproject.toml, requirements.txt, composer.json, pom.xml/build.gradle*,
*.csproj). discovery.py performs the one real directory walk; everything
below only ever looks at what that walk already found.

Every detected name is backed by at least one real file - `DetectedItem`
itself refuses to be constructed otherwise (models.py). Nothing here calls
an AI provider, spawns a subprocess, or makes a network request - the same
"filesystem inspection only" boundary discovery.py's own docstring states.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import MAX_EVIDENCE_PER_ITEM, DetectedItem

# Manifest/lock files read for their own text content, never their raw
# bytes wholesale - each is inherently small (a real-world package.json or
# pyproject.toml is kilobytes, not megabytes), but a hard cap is applied
# anyway so a pathological giant file is skipped rather than fully read,
# matching the "never read large files completely when only metadata is
# required" performance rule even for the deliberate exception of manifest
# content inspection.
_MAX_MANIFEST_BYTES = 2_000_000


def _read_text_capped(path):
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_json_capped(path):
    text = _read_text_capped(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _package_json_dependency_names(data):
    """Every declared dependency name, from all four npm dependency
    sections - a framework named only in `devDependencies` (a common real
    shape, e.g. a build-time-only tool) is still real evidence of its use.
    """
    if not isinstance(data, dict):
        return frozenset()
    names = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            names.update(section.keys())
    return frozenset(names)


class _Accumulator:
    """name -> capped, ordered, deduplicated evidence paths, finalized into
    a sorted tuple of `DetectedItem` - the one place every `DetectedItem` in
    this module is actually built, so the "never empty evidence" and
    "cap displayed evidence" rules are enforced exactly once, not
    re-implemented per detector.
    """

    def __init__(self):
        self._items = {}

    def add(self, name, evidence_path):
        bucket = self._items.setdefault(name, [])
        if evidence_path not in bucket and len(bucket) < MAX_EVIDENCE_PER_ITEM:
            bucket.append(evidence_path)

    def finalize(self):
        return tuple(
            DetectedItem(name=name, evidence=tuple(paths))
            for name, paths in sorted(self._items.items())
        )


# extension (lowercase, with dot) -> language display name. A project can
# and often does contain more than one - every match is independent.
_LANGUAGE_EXTENSIONS = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".swift": "Swift",
    ".php": "PHP",
    ".rb": "Ruby",
    ".cs": "C#",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
}


def detect_languages(files):
    """One `DetectedItem` per language with at least one matching source
    file anywhere in the (already-ignore-filtered) walk. Evidence is capped
    per item (models.MAX_EVIDENCE_PER_ITEM) - presence itself is still
    checked across every file, only the displayed sample is capped.
    """
    acc = _Accumulator()
    for rel in files:
        ext = Path(rel).suffix.lower()
        language = _LANGUAGE_EXTENSIONS.get(ext)
        if language is not None:
            acc.add(language, rel)
    return acc.finalize()


# basename (exact) -> package manager name. Matched by basename regardless
# of directory depth - a lockfile inside a monorepo sub-package is exactly
# as real a piece of evidence as one at the root, so both are found by the
# same single pass rather than a root-only check.
_PACKAGE_MANAGER_BASENAMES = {
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
    "requirements.txt": "pip",
    "poetry.lock": "poetry",
    "uv.lock": "uv",
    "Cargo.lock": "cargo",
    "Cargo.toml": "cargo",
    "go.sum": "go modules",
    "go.mod": "go modules",
    "composer.lock": "composer",
    "composer.json": "composer",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "gradlew": "gradle",
}


def detect_package_managers(root, files):
    """Lockfile/manifest presence is treated as sufficient evidence on its
    own (no content read needed) for every entry except `poetry`, which
    additionally needs `pyproject.toml`'s own `[tool.poetry]` marker - a
    bare PEP 621 `pyproject.toml` with no such section is real evidence of
    *a* Python project, but not specifically of Poetry, so it is
    deliberately left undetected rather than guessed.
    """
    acc = _Accumulator()
    for rel in files:
        name = Path(rel).name
        manager = _PACKAGE_MANAGER_BASENAMES.get(name)
        if manager is not None:
            acc.add(manager, rel)
        if name == "pyproject.toml":
            text = _read_text_capped(root / rel)
            if text is not None and "[tool.poetry]" in text:
                acc.add("poetry", rel)
    return acc.finalize()


# basename (exact) -> build system name. Deliberately a starter set, not
# exhaustive (docs/19 names this an explicit, documented scope boundary) -
# extending it later needs no change outside this one table.
_BUILD_SYSTEM_BASENAMES = {
    "Makefile": "Make",
    "makefile": "Make",
    "CMakeLists.txt": "CMake",
    "pom.xml": "Maven",
    "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle",
    ".babelrc": "Babel",
}

# basename *prefix* -> build system name, for the "<tool>.config.<ext>" family.
_BUILD_SYSTEM_PREFIXES = {
    "vite.config.": "Vite",
    "webpack.config.": "Webpack",
    "rollup.config.": "Rollup",
    "babel.config.": "Babel",
}


def detect_build_systems(files):
    acc = _Accumulator()
    for rel in files:
        name = Path(rel).name
        exact = _BUILD_SYSTEM_BASENAMES.get(name)
        if exact is not None:
            acc.add(exact, rel)
            continue
        for prefix, system in _BUILD_SYSTEM_PREFIXES.items():
            if name.startswith(prefix):
                acc.add(system, rel)
                break
    return acc.finalize()


# package.json dependency name -> framework display name. Checked against
# the union of dependencies/devDependencies/peerDependencies/
# optionalDependencies (`_package_json_dependency_names`).
_JS_FRAMEWORK_DEPENDENCIES = {
    "next": "Next.js",
    "react": "React",
    "react-dom": "React",
    "vue": "Vue",
    "nuxt": "Nuxt",
    "nuxt3": "Nuxt",
    "@angular/core": "Angular",
    "svelte": "Svelte",
    "express": "Express",
    "@nestjs/core": "NestJS",
    "electron": "Electron",
    "react-native": "React Native",
}

# basename prefix/exact -> framework, for config files that are themselves
# unambiguous evidence even without reading package.json at all.
_JS_FRAMEWORK_FILE_MARKERS = {
    "next.config.": "Next.js",
    "nuxt.config.": "Nuxt",
}
_JS_FRAMEWORK_FILE_EXACT = {
    "angular.json": "Angular",
    "nest-cli.json": "NestJS",
}

# Substring (case-insensitive) found in requirements.txt or pyproject.toml
# -> Python web framework. Substring search, not a requirements-format
# parser or a TOML parser - both files are simple enough that this is
# accurate in practice, and it avoids a new dependency purely to detect a
# package name (this project's own dependency philosophy: use a library
# when the tool needs sophistication it can't reasonably reimplement, never
# by default).
_PYTHON_FRAMEWORK_MARKERS = {
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
}


def detect_frameworks(root, files):
    """Multiple frameworks may - and often should - coexist for the same
    evidence file: a Next.js `package.json` is simultaneously real evidence
    of Next.js *and* of React, and both are reported (docs/19's explicit
    "do not collapse framework hierarchies" rule) rather than picking one.
    """
    acc = _Accumulator()
    for rel in files:
        name = Path(rel).name

        if name == "package.json":
            data = _read_json_capped(root / rel)
            deps = _package_json_dependency_names(data)
            for dep_name, framework in _JS_FRAMEWORK_DEPENDENCIES.items():
                if dep_name in deps:
                    acc.add(framework, rel)

        for prefix, framework in _JS_FRAMEWORK_FILE_MARKERS.items():
            if name.startswith(prefix):
                acc.add(framework, rel)
        exact = _JS_FRAMEWORK_FILE_EXACT.get(name)
        if exact is not None:
            acc.add(exact, rel)

        if name in ("requirements.txt", "pyproject.toml"):
            text = _read_text_capped(root / rel)
            if text is not None:
                lowered = text.lower()
                for marker, framework in _PYTHON_FRAMEWORK_MARKERS.items():
                    if marker in lowered:
                        acc.add(framework, rel)
        if name == "manage.py":
            # django-admin's own generated entrypoint - unambiguous on its
            # own even without reading a requirements file at all.
            acc.add("Django", rel)

        if name in ("pom.xml", "build.gradle", "build.gradle.kts"):
            text = _read_text_capped(root / rel)
            if text is not None and "spring-boot" in text.lower():
                acc.add("Spring Boot", rel)

        if name == "composer.json":
            data = _read_json_capped(root / rel)
            require = data.get("require") if isinstance(data, dict) else None
            if isinstance(require, dict) and "laravel/framework" in require:
                acc.add("Laravel", rel)

        if name.endswith(".csproj"):
            text = _read_text_capped(root / rel)
            if text is not None and "Microsoft.AspNetCore" in text:
                acc.add("ASP.NET", rel)

    return acc.finalize()


# --- file categorization -----------------------------------------------
#
# Every discovered file belongs to at most one of these categories
# (docs/19's "exactly one" rule) - checked in this fixed order, first match
# wins, so a file that could plausibly fit two buckets (e.g. `pom.xml`,
# which is also build-system/package-manager evidence elsewhere) still
# lands in exactly one category here. A file matching none of these is not
# "important" and is simply not recorded - this module never claims to
# categorize *every* file in a repository, only the ones with recognized,
# real significance.

CATEGORY_DOCKER = "docker"
CATEGORY_CI = "ci"
CATEGORY_DEPENDENCY = "dependency"
CATEGORY_ENVIRONMENT = "environment"
CATEGORY_SPECIFICATION = "specification"
CATEGORY_DOCUMENTATION = "documentation"
CATEGORY_CONFIGURATION = "configuration"
CATEGORY_RUNTIME = "runtime"

ALL_CATEGORIES = (
    CATEGORY_DOCKER,
    CATEGORY_CI,
    CATEGORY_DEPENDENCY,
    CATEGORY_ENVIRONMENT,
    CATEGORY_SPECIFICATION,
    CATEGORY_DOCUMENTATION,
    CATEGORY_CONFIGURATION,
    CATEGORY_RUNTIME,
)

_DEPENDENCY_BASENAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "poetry.lock", "Cargo.lock", "composer.lock", "go.sum", "uv.lock",
}

_DOC_PREFIXES = ("readme", "changelog", "contributing", "license")

_CONFIGURATION_BASENAMES = {
    "package.json", "pyproject.toml", "requirements.txt", "tsconfig.json",
    "jsconfig.json", "ruff.toml", ".ruff.toml", "angular.json",
    "nest-cli.json", "tailwind.config.js", "tailwind.config.ts",
    "postcss.config.js", "jest.config.js", "jest.config.ts",
    "setup.cfg", "setup.py", "Cargo.toml", "go.mod", "composer.json",
    "pom.xml", "build.gradle", "build.gradle.kts",
}
_CONFIGURATION_PREFIXES = (
    "eslint.config.", ".eslintrc", "next.config.", "nuxt.config.",
    "vite.config.", "webpack.config.", "rollup.config.", "babel.config.",
    ".babelrc",
)

_RUNTIME_BASENAMES = {"main.py", "app.py", "server.js", "server.ts", "index.ts", "index.js", "manage.py"}


def _categorize_one(rel):
    name = Path(rel).name
    lowered = name.lower()

    if lowered == "dockerfile" or lowered.startswith("dockerfile.") or lowered.startswith("docker-compose"):
        return CATEGORY_DOCKER
    if (
        "/.github/workflows/" in "/" + rel.replace("\\", "/")
        and (lowered.endswith(".yml") or lowered.endswith(".yaml"))
    ) or lowered in (".gitlab-ci.yml", "azure-pipelines.yml") or "/.circleci/" in "/" + rel.replace("\\", "/"):
        return CATEGORY_CI
    if name in _DEPENDENCY_BASENAMES:
        return CATEGORY_DEPENDENCY
    if lowered == ".env" or lowered.startswith(".env."):
        return CATEGORY_ENVIRONMENT
    if lowered.startswith("openapi") or lowered.startswith("swagger") or lowered.endswith(".graphql"):
        return CATEGORY_SPECIFICATION
    if lowered.startswith(_DOC_PREFIXES):
        return CATEGORY_DOCUMENTATION
    if name in _CONFIGURATION_BASENAMES or lowered.startswith(_CONFIGURATION_PREFIXES):
        return CATEGORY_CONFIGURATION
    if name in _RUNTIME_BASENAMES:
        return CATEGORY_RUNTIME
    return None


def categorize_files(files):
    """Every file, sorted into at most one category. Returns a dict keyed
    by the `CATEGORY_*` constants above, each value a sorted tuple of
    relative paths - `important_files` (knowledge.py) is simply the union
    of all of them.

    Security note (docs/19's explicit rule): a raw `.env` is categorized by
    *name* only, exactly like every other file here - this function never
    opens or reads any file's content, so a real `.env`'s secrets are never
    at risk of being read, let alone surfaced, by this categorization step.
    """
    buckets = {category: [] for category in ALL_CATEGORIES}
    for rel in files:
        category = _categorize_one(rel)
        if category is not None:
            buckets[category].append(rel)
    return {category: tuple(sorted(paths)) for category, paths in buckets.items()}


# --- directories ----------------------------------------------------------

_IMPORTANT_DIR_NAMES = frozenset({
    "src", "app", "pages", "api", "middleware", "backend", "frontend",
    "components", "routes", "controllers", "models", "services",
    "database", "migrations", "public", "assets", "static", "scripts",
    "tests", "test", "e2e", "docs",
    # Added in Phase F Part 2 (docs/20-repository-context-engine.md): a
    # monorepo's shared/common code directory is exactly as real a
    # structural signal as "components" or "controllers" already were -
    # found missing when Part 2's repository_layout "Shared" zone had
    # nothing to detect against, since a bare shared/ directory (no
    # manifest inside it) was invisible to important_directories entirely.
    "shared", "packages", "libs", "common",
})

_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "e2e", "spec", "specs"})


def detect_directories(dirs):
    """Every visited (non-ignored) directory whose final path segment is a
    recognized name - matched on the segment, not the whole path, so
    `backend/src` and `frontend/src` are both found as two distinct real
    directories, never collapsed into one.
    """
    return tuple(sorted(d for d in dirs if Path(d).name in _IMPORTANT_DIR_NAMES))


def detect_test_directories(dirs):
    return tuple(sorted(d for d in dirs if Path(d).name in _TEST_DIR_NAMES))


# --- repository type --------------------------------------------------

_MONOREPO_MARKER_BASENAMES = frozenset({
    "pnpm-workspace.yaml", "turbo.json", "nx.json", "rush.json", "lerna.json",
})

_MANIFEST_BASENAMES = frozenset({
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "composer.json", "pom.xml",
})


def detect_repository_type(root, files):
    """single-package / workspace / monorepo / unknown - never inferred
    from directory names alone, only from an explicit monorepo-tool marker
    file, a `package.json` "workspaces" key, or more than one independent
    manifest file found in more than one directory (docs/19's own repository
    -type evidence list).

    Returns `(type, evidence_paths)`.
    """
    manifests = [rel for rel in files if Path(rel).name in _MANIFEST_BASENAMES]
    monorepo_markers = [rel for rel in files if Path(rel).name in _MONOREPO_MARKER_BASENAMES]

    if monorepo_markers:
        return "monorepo", tuple(sorted(monorepo_markers))

    root_package_json = next((rel for rel in files if rel == "package.json"), None)
    if root_package_json is not None:
        data = _read_json_capped(root / root_package_json)
        if isinstance(data, dict) and isinstance(data.get("workspaces"), (list, dict)):
            return "monorepo", (root_package_json,)

    manifest_dirs = {Path(rel).parent.as_posix() for rel in manifests}
    if len(manifest_dirs) > 1:
        return "workspace", tuple(sorted(manifests))
    if len(manifests) >= 1:
        return "single-package", tuple(sorted(manifests))
    return "unknown", ()


def detect_workspace_structure(root, files, repository_type):
    """For a workspace/monorepo only: which top-level directories actually
    hold more than one manifest-bearing sub-package (e.g. "apps", "packages"),
    and the specific sub-package directories themselves. Empty for
    single-package/unknown repositories - there is nothing to name.
    """
    if repository_type not in ("workspace", "monorepo"):
        return (), ()
    manifests = [rel for rel in files if Path(rel).name in _MANIFEST_BASENAMES]
    package_dirs = sorted({
        Path(rel).parent.as_posix() for rel in manifests if Path(rel).parent.as_posix() != "."
    })
    top_level = sorted({p.split("/")[0] for p in package_dirs})
    return tuple(top_level), tuple(package_dirs)


# --- application type ---------------------------------------------------

_FRONTEND_FRAMEWORKS = frozenset({"React", "Vue", "Angular", "Svelte", "Next.js", "Nuxt"})
_BACKEND_FRAMEWORKS = frozenset({
    "Express", "NestJS", "FastAPI", "Flask", "Django", "Spring Boot", "Laravel", "ASP.NET",
})


def _has_cli_entrypoint(root, files):
    for rel in files:
        name = Path(rel).name
        if name == "package.json":
            data = _read_json_capped(root / rel)
            if isinstance(data, dict) and data.get("bin"):
                return True, rel
        if name == "pyproject.toml":
            text = _read_text_capped(root / rel)
            if text is not None and ("[project.scripts]" in text or "[tool.poetry.scripts]" in text):
                return True, rel
        if name in ("setup.py", "setup.cfg"):
            text = _read_text_capped(root / rel)
            if text is not None and "console_scripts" in text:
                return True, rel
        if name == "Cargo.toml":
            text = _read_text_capped(root / rel)
            if text is not None and "[[bin]]" in text:
                return True, rel
    return False, None


def detect_application_type(root, files, frameworks):
    """frontend / backend / full-stack / cli / desktop / mobile / library /
    unknown - evaluated as a fixed priority over independent yes/no
    signals (docs/19's own rule list, in the order given there): a
    conflicting combination (e.g. both Electron and React Native evidence
    in the same repository) is `unknown` rather than an arbitrary pick.

    A Next.js `app/api/` or `pages/api/` directory is treated as backend
    evidence in its own right, alongside the explicit backend-framework
    list - a documented extension of the rule list, not a deviation from
    it: Next.js API routes are real backend code, evidenced by real files,
    exactly like an Express route file would be.
    """
    framework_names = {item.name for item in frameworks}
    has_frontend = bool(framework_names & _FRONTEND_FRAMEWORKS)
    has_backend = bool(framework_names & _BACKEND_FRAMEWORKS)
    api_route_evidence = next(
        (rel for rel in files if "/api/" in ("/" + rel.replace("\\", "/"))), None
    )
    if api_route_evidence is not None and "Next.js" in framework_names:
        has_backend = True

    has_electron = "Electron" in framework_names
    has_react_native = "React Native" in framework_names
    has_cli, cli_evidence = _has_cli_entrypoint(root, files)

    evidence = []
    for rel in files:
        name = Path(rel).name
        if name == "package.json" and (has_frontend or has_backend or has_electron or has_react_native):
            evidence.append(rel)
    if api_route_evidence is not None and has_backend and "Next.js" in framework_names:
        evidence.append(api_route_evidence)
    if has_cli and cli_evidence:
        evidence.append(cli_evidence)

    if has_electron and has_react_native:
        return "unknown", tuple(sorted(set(evidence))) or ()
    if has_electron:
        return "desktop", tuple(sorted(set(evidence)))
    if has_react_native:
        return "mobile", tuple(sorted(set(evidence)))
    if has_frontend and has_backend:
        return "full-stack", tuple(sorted(set(evidence)))
    if has_frontend:
        return "frontend", tuple(sorted(set(evidence)))
    if has_backend:
        return "backend", tuple(sorted(set(evidence)))
    if has_cli:
        return "cli", ((cli_evidence,) if cli_evidence else ())
    has_any_manifest = any(Path(rel).name in _MANIFEST_BASENAMES for rel in files)
    if has_any_manifest:
        manifest_evidence = tuple(sorted(rel for rel in files if Path(rel).name in _MANIFEST_BASENAMES))
        return "library", manifest_evidence
    return "unknown", ()
