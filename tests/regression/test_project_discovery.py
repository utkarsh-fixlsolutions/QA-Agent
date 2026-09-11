"""Phase F Part 1: the Project Discovery Engine (docs/19-project-discovery
-engine.md) - `qa_agent.project.discover_project()`, its detectors, and the
`discover` CLI command.

Every fixture project here is a real, throwaway directory on disk
(`TempProject`), not a mock or a hand-built in-memory object - the same
discipline every other adapter/engine suite in this project already
follows: detection is proven against real files, not against what the
implementation merely expects to see.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.project import (  # noqa: E402
    STATUS_DISCOVERY_FAILED,
    STATUS_INVALID_ROOT,
    STATUS_PARTIAL_SUCCESS,
    STATUS_PERMISSION_DENIED,
    STATUS_SUCCESS,
    DetectedItem,
    ProjectKnowledge,
    discover_project,
)


def _names(detected_items):
    return {item.name for item in detected_items}


class _Writer:
    """Adapts `TempProject.write()` onto a directory that already exists -
    used together with `with TempProject() as root:` so a fixture needs
    exactly one real temp directory, not two (a second bare `TempProject()`
    call creates its own `tempfile.mkdtemp()` in `__init__` that would
    otherwise never get cleaned up if only its `.write()` method is used).
    """

    def __init__(self, path):
        self.path = path

    write = TempProject.write


# --- invalid / edge-case roots -------------------------------------------

def test_invalid_root_nonexistent_path(suite):
    result = discover_project("Z:/this/path/almost-certainly-does-not-exist-qa-agent")
    suite.check("nonexistent root -> INVALID_ROOT", result.status == STATUS_INVALID_ROOT)
    suite.check("nonexistent root -> no project", result.project is None)
    suite.check("nonexistent root -> at least one error named", len(result.errors) >= 1)


def test_invalid_root_path_is_a_file(suite):
    with TempProject() as root:
        target = root / "just_a_file.txt"
        target.write_text("hello", encoding="utf-8")
        result = discover_project(target)
    suite.check("a file, not a directory -> INVALID_ROOT", result.status == STATUS_INVALID_ROOT)
    suite.check("file-as-root -> no project", result.project is None)


def test_invalid_root_wrong_type_never_raises(suite):
    # An int is not a valid path at all - Path(12345) actually succeeds in
    # CPython (it becomes the string "12345"), so this exercises the "does
    # not exist" branch rather than the TypeError branch; both must still
    # come back as a structured INVALID_ROOT, never an exception.
    try:
        result = discover_project(12345)
        raised = False
    except Exception:
        raised = True
    suite.check("a non-path-like root never raises", not raised)
    if not raised:
        suite.check("a non-path-like root -> INVALID_ROOT", result.status == STATUS_INVALID_ROOT)


def test_empty_repository(suite):
    with TempProject() as root:
        result = discover_project(root)
    suite.check("empty repo -> SUCCESS", result.status == STATUS_SUCCESS)
    suite.check("empty repo -> a project object still exists", result.project is not None)
    suite.check("empty repo -> no languages", result.project.languages == ())
    suite.check("empty repo -> repository_type unknown", result.project.repository_type == "unknown")
    suite.check("empty repo -> application_type unknown", result.project.application_type == "unknown")
    suite.check("empty repo -> zero scanned files", result.scanned_files == 0)


# --- language detection ---------------------------------------------------

def test_python_only_project(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("main.py", "print('hi')\n")
        proj.write("requirements.txt", "requests==2.0\n")
        result = discover_project(root)
    suite.check("python project -> SUCCESS", result.status == STATUS_SUCCESS)
    suite.check("python-only -> Python detected", "Python" in _names(result.project.languages))
    suite.check("python-only -> no JavaScript", "JavaScript" not in _names(result.project.languages))
    suite.check("python-only -> pip detected", "pip" in _names(result.project.package_managers))


def test_mixed_language_repository(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("backend/main.py", "print('hi')\n")
        proj.write("frontend/index.ts", "export {}\n")
        proj.write("frontend/app.jsx", "export default function App() {}\n")
        result = discover_project(root)
    langs = _names(result.project.languages)
    suite.check("mixed repo -> Python detected", "Python" in langs)
    suite.check("mixed repo -> TypeScript detected", "TypeScript" in langs)
    suite.check("mixed repo -> JavaScript detected", "JavaScript" in langs)


# --- framework detection ---------------------------------------------------

def test_nextjs_project_reports_both_nextjs_and_react(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("package.json", '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}')
        proj.write("next.config.ts", "export default {}\n")
        proj.write("app/page.tsx", "export default function Page() { return null }\n")
        result = discover_project(root)
    names = _names(result.project.frameworks)
    suite.check("next.js project -> Next.js reported", "Next.js" in names)
    suite.check("next.js project -> React also reported (not collapsed)", "React" in names)
    next_item = next(i for i in result.project.frameworks if i.name == "Next.js")
    suite.check("Next.js DetectedItem carries real evidence", len(next_item.evidence) >= 1)


def test_react_only_project(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("package.json", '{"dependencies": {"react": "18.0.0", "react-dom": "18.0.0"}}')
        result = discover_project(root)
    names = _names(result.project.frameworks)
    suite.check("react-only -> React reported", "React" in names)
    suite.check("react-only -> Next.js not reported", "Next.js" not in names)


def test_express_project(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("package.json", '{"dependencies": {"express": "4.18.0"}}')
        proj.write("server.js", "require('express')\n")
        result = discover_project(root)
    suite.check("express -> Express detected", "Express" in _names(result.project.frameworks))


def test_fastapi_project(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("requirements.txt", "fastapi==0.100.0\nuvicorn\n")
        proj.write("main.py", "from fastapi import FastAPI\n")
        result = discover_project(root)
    suite.check("fastapi -> FastAPI detected", "FastAPI" in _names(result.project.frameworks))


def test_flask_project(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("requirements.txt", "flask==3.0.0\n")
        result = discover_project(root)
    suite.check("flask -> Flask detected", "Flask" in _names(result.project.frameworks))


def test_django_project_via_manage_py(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("manage.py", "#!/usr/bin/env python\n")
        result = discover_project(root)
    suite.check("django via manage.py -> Django detected", "Django" in _names(result.project.frameworks))
    suite.check(
        "manage.py categorized as a runtime file", "manage.py" in result.project.runtime_files
    )


def test_nestjs_project(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("package.json", '{"dependencies": {"@nestjs/core": "10.0.0"}}')
        proj.write("nest-cli.json", "{}")
        result = discover_project(root)
    suite.check("nestjs -> NestJS detected", "NestJS" in _names(result.project.frameworks))


def test_vue_and_angular_projects(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("package.json", '{"dependencies": {"vue": "3.0.0"}}')
        vue_result = discover_project(root)
    with TempProject() as root2:
        proj2 = TempProject()
        proj2.path = root2
        proj2.write("angular.json", "{}")
        angular_result = discover_project(root2)
    suite.check("vue project -> Vue detected", "Vue" in _names(vue_result.project.frameworks))
    suite.check(
        "angular.json alone -> Angular detected", "Angular" in _names(angular_result.project.frameworks)
    )


def test_spring_boot_via_pom_xml(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write(
            "pom.xml",
            "<project><dependencies><dependency>"
            "<artifactId>spring-boot-starter-web</artifactId>"
            "</dependency></dependencies></project>",
        )
        result = discover_project(root)
    suite.check("pom.xml with spring-boot -> Spring Boot detected", "Spring Boot" in _names(result.project.frameworks))
    suite.check("pom.xml -> maven package manager", "maven" in _names(result.project.package_managers))


def test_laravel_via_composer_json(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("composer.json", '{"require": {"laravel/framework": "^10.0"}}')
        result = discover_project(root)
    suite.check("composer.json with laravel -> Laravel detected", "Laravel" in _names(result.project.frameworks))
    suite.check("composer.json -> composer package manager", "composer" in _names(result.project.package_managers))


def test_aspnet_via_csproj(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write(
            "App.csproj",
            '<Project Sdk="Microsoft.NET.Sdk.Web">'
            '<ItemGroup><PackageReference Include="Microsoft.AspNetCore.App" /></ItemGroup>'
            "</Project>",
        )
        result = discover_project(root)
    suite.check("csproj mentioning AspNetCore -> ASP.NET detected", "ASP.NET" in _names(result.project.frameworks))


def test_unknown_framework_reports_nothing_invented(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("package.json", '{"dependencies": {"some-totally-unknown-lib": "1.0.0"}}')
        result = discover_project(root)
    suite.check("an unrecognized dependency -> no framework invented", result.project.frameworks == ())


# --- package manager detection ---------------------------------------------

def _pm_project(files):
    with TempProject() as root:
        proj = _Writer(root)
        for rel, text in files.items():
            proj.write(rel, text)
        return discover_project(root)


def test_package_manager_npm(suite):
    result = _pm_project({"package-lock.json": "{}", "package.json": "{}"})
    suite.check("package-lock.json -> npm", "npm" in _names(result.project.package_managers))


def test_package_manager_pnpm(suite):
    result = _pm_project({"pnpm-lock.yaml": ""})
    suite.check("pnpm-lock.yaml -> pnpm", "pnpm" in _names(result.project.package_managers))


def test_package_manager_yarn(suite):
    result = _pm_project({"yarn.lock": ""})
    suite.check("yarn.lock -> yarn", "yarn" in _names(result.project.package_managers))


def test_package_manager_bun(suite):
    result = _pm_project({"bun.lockb": ""})
    suite.check("bun.lockb -> bun", "bun" in _names(result.project.package_managers))


def test_package_manager_poetry_via_pyproject_marker(suite):
    with_poetry = _pm_project({"pyproject.toml": "[tool.poetry]\nname = \"x\"\n"})
    without_poetry = _pm_project({"pyproject.toml": "[project]\nname = \"x\"\n"})
    suite.check(
        "[tool.poetry] section -> poetry detected", "poetry" in _names(with_poetry.project.package_managers)
    )
    suite.check(
        "bare PEP 621 pyproject.toml -> poetry NOT guessed",
        "poetry" not in _names(without_poetry.project.package_managers),
    )


def test_package_manager_cargo_and_go_modules(suite):
    cargo = _pm_project({"Cargo.toml": "[package]\nname = \"x\"\n"})
    go = _pm_project({"go.mod": "module example.com/x\n"})
    suite.check("Cargo.toml -> cargo", "cargo" in _names(cargo.project.package_managers))
    suite.check("go.mod -> go modules", "go modules" in _names(go.project.package_managers))


def test_package_manager_maven_and_gradle(suite):
    maven = _pm_project({"pom.xml": "<project></project>"})
    gradle = _pm_project({"build.gradle": ""})
    suite.check("pom.xml -> maven", "maven" in _names(maven.project.package_managers))
    suite.check("build.gradle -> gradle", "gradle" in _names(gradle.project.package_managers))


# --- build system detection -------------------------------------------------

def test_build_system_vite_and_webpack(suite):
    vite = _pm_project({"vite.config.ts": "export default {}\n"})
    webpack = _pm_project({"webpack.config.js": "module.exports = {}\n"})
    suite.check("vite.config.ts -> Vite", "Vite" in _names(vite.project.build_systems))
    suite.check("webpack.config.js -> Webpack", "Webpack" in _names(webpack.project.build_systems))


def test_build_system_make_and_cmake(suite):
    make = _pm_project({"Makefile": "all:\n\techo hi\n"})
    cmake = _pm_project({"CMakeLists.txt": "cmake_minimum_required(VERSION 3.10)\n"})
    suite.check("Makefile -> Make", "Make" in _names(make.project.build_systems))
    suite.check("CMakeLists.txt -> CMake", "CMake" in _names(cmake.project.build_systems))


# --- file categorization ----------------------------------------------------

def test_categorization_docker(suite):
    result = _pm_project({"Dockerfile": "FROM python:3.12\n", "docker-compose.yml": "services: {}\n"})
    suite.check("Dockerfile -> docker category", "Dockerfile" in result.project.docker_files)
    suite.check("docker-compose.yml -> docker category", "docker-compose.yml" in result.project.docker_files)


def test_categorization_ci(suite):
    result = _pm_project({".github/workflows/ci.yml": "on: push\n"})
    suite.check(
        "a workflow file under .github/workflows -> ci category",
        ".github/workflows/ci.yml" in result.project.ci_files,
    )


def test_categorization_dependency(suite):
    result = _pm_project({"package-lock.json": "{}", "poetry.lock": ""})
    suite.check("package-lock.json -> dependency category", "package-lock.json" in result.project.dependency_files)
    suite.check("poetry.lock -> dependency category", "poetry.lock" in result.project.dependency_files)


def test_categorization_environment_never_reads_contents(suite):
    secret_value = "SUPER_SECRET_API_KEY=do-not-leak-this-12345"
    result = _pm_project({".env": secret_value, ".env.example": "SUPER_SECRET_API_KEY=\n"})
    suite.check(".env -> environment category (presence)", ".env" in result.project.environment_files)
    suite.check(".env.example -> environment category", ".env.example" in result.project.environment_files)
    rendered = repr(result.project) + repr(result.project.evidence)
    suite.check(
        "the real .env's secret value never appears anywhere in the model",
        secret_value not in rendered,
    )


def test_categorization_specification(suite):
    result = _pm_project({"openapi.yaml": "openapi: 3.0.0\n", "swagger.json": "{}"})
    suite.check("openapi.yaml -> specification category", "openapi.yaml" in result.project.specification_files)
    suite.check("swagger.json -> specification category", "swagger.json" in result.project.specification_files)


def test_categorization_documentation(suite):
    result = _pm_project({"README.md": "# Hi\n", "CHANGELOG.md": "# Changes\n", "LICENSE": "MIT\n"})
    suite.check("README.md -> documentation category", "README.md" in result.project.documentation_files)
    suite.check("CHANGELOG.md -> documentation category", "CHANGELOG.md" in result.project.documentation_files)
    suite.check("LICENSE -> documentation category", "LICENSE" in result.project.documentation_files)


def test_categorization_configuration(suite):
    result = _pm_project({"tsconfig.json": "{}", "package.json": "{}"})
    suite.check("tsconfig.json -> configuration category", "tsconfig.json" in result.project.configuration_files)
    suite.check("package.json -> configuration category", "package.json" in result.project.configuration_files)


def test_categorization_runtime(suite):
    result = _pm_project({"main.py": "print(1)\n", "server.js": "console.log(1)\n"})
    suite.check("main.py -> runtime category", "main.py" in result.project.runtime_files)
    suite.check("server.js -> runtime category", "server.js" in result.project.runtime_files)


def test_categorization_every_file_at_most_one_category(suite):
    result = _pm_project({"package.json": "{}"})
    categories = [
        result.project.configuration_files, result.project.runtime_files,
        result.project.documentation_files, result.project.specification_files,
        result.project.dependency_files, result.project.environment_files,
        result.project.docker_files, result.project.ci_files,
    ]
    counts = sum(1 for bucket in categories if "package.json" in bucket)
    suite.check("package.json lands in exactly one category", counts == 1)


def test_uncategorized_files_do_not_appear_as_important(suite):
    # helpers.py is deliberately not one of the recognized runtime entrypoint
    # names (main.py/app.py/server.js/...) - an ordinary module, not special.
    result = _pm_project({"src/helpers.py": "def f(): pass\n", "src/notes.txt": "just notes\n"})
    suite.check(
        "an ordinary source file is not swept into important_files",
        "src/helpers.py" not in result.project.important_files,
    )
    suite.check("a plain .txt file is not swept in either", "src/notes.txt" not in result.project.important_files)


# --- repository type ---------------------------------------------------

def test_repository_type_single_package(suite):
    result = _pm_project({"package.json": "{}"})
    suite.check("one manifest at root -> single-package", result.project.repository_type == "single-package")


def test_repository_type_workspace_multiple_manifests(suite):
    result = _pm_project({"apps/web/package.json": "{}", "apps/api/package.json": "{}"})
    suite.check(
        "multiple independent manifests, no monorepo marker -> workspace",
        result.project.repository_type == "workspace",
    )


def test_repository_type_monorepo_via_marker_file(suite):
    result = _pm_project({"pnpm-workspace.yaml": "packages:\n  - apps/*\n", "apps/web/package.json": "{}"})
    suite.check("pnpm-workspace.yaml present -> monorepo", result.project.repository_type == "monorepo")


def test_repository_type_monorepo_via_package_json_workspaces_key(suite):
    result = _pm_project({"package.json": '{"workspaces": ["apps/*"]}', "apps/web/package.json": "{}"})
    suite.check(
        'root package.json "workspaces" key -> monorepo', result.project.repository_type == "monorepo"
    )


def test_repository_type_unknown_when_no_manifest(suite):
    result = _pm_project({"README.md": "# hi\n"})
    suite.check("no manifest anywhere -> unknown repository_type", result.project.repository_type == "unknown")


def test_workspace_structure_and_monorepo_packages(suite):
    result = _pm_project({
        "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
        "apps/web/package.json": "{}",
        "apps/api/package.json": "{}",
    })
    suite.check("workspace_structure names 'apps'", "apps" in result.project.workspace_structure)
    suite.check(
        "monorepo_packages lists both sub-package directories",
        {"apps/api", "apps/web"} <= set(result.project.monorepo_packages),
    )


# --- application type ---------------------------------------------------

def test_application_type_frontend_only(suite):
    result = _pm_project({"package.json": '{"dependencies": {"react": "18.0.0"}}'})
    suite.check("react only, no backend -> frontend", result.project.application_type == "frontend")


def test_application_type_backend_only(suite):
    result = _pm_project({"requirements.txt": "flask==3.0\n"})
    suite.check("flask only, no frontend -> backend", result.project.application_type == "backend")


def test_application_type_full_stack_two_frameworks(suite):
    result = _pm_project({
        "package.json": '{"dependencies": {"react": "18.0.0"}}',
        "requirements.txt": "flask==3.0\n",
    })
    suite.check(
        "react + flask in one repo -> full-stack", result.project.application_type == "full-stack"
    )


def test_application_type_full_stack_via_nextjs_api_directory(suite):
    result = _pm_project({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "app/api/hello/route.ts": "export function GET() {}\n",
    })
    suite.check(
        "Next.js with a real app/api route -> full-stack, not just frontend",
        result.project.application_type == "full-stack",
    )


def test_application_type_library_no_entrypoint(suite):
    result = _pm_project({"pyproject.toml": "[project]\nname = \"mylib\"\n"})
    suite.check(
        "a manifest with no frontend/backend/cli signal -> library",
        result.project.application_type == "library",
    )


def test_application_type_cli_via_package_json_bin(suite):
    result = _pm_project({"package.json": '{"bin": {"mytool": "./cli.js"}}'})
    suite.check('package.json "bin" field -> cli', result.project.application_type == "cli")


def test_application_type_cli_via_python_console_scripts(suite):
    result = _pm_project({"pyproject.toml": "[project.scripts]\nmytool = \"pkg:main\"\n"})
    suite.check("[project.scripts] -> cli", result.project.application_type == "cli")


def test_application_type_unknown_when_nothing_found(suite):
    result = _pm_project({"README.md": "# empty\n"})
    suite.check("nothing recognizable -> unknown application_type", result.project.application_type == "unknown")


def test_application_type_desktop_via_electron(suite):
    result = _pm_project({"package.json": '{"dependencies": {"electron": "27.0.0"}}'})
    suite.check("electron dependency -> desktop", result.project.application_type == "desktop")


def test_application_type_mobile_via_react_native(suite):
    result = _pm_project({"package.json": '{"dependencies": {"react-native": "0.72.0"}}'})
    suite.check("react-native dependency -> mobile", result.project.application_type == "mobile")


# --- ignore policy -----------------------------------------------------

def test_ignore_policy_excludes_node_modules(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("node_modules/somelib/index.js", "module.exports = {}\n")
        proj.write("app.js", "console.log(1)\n")
        result = discover_project(root)
    files_seen = result.scanned_files
    suite.check(
        "a file under node_modules is never counted as scanned",
        files_seen == 1,
        " (scanned={})".format(files_seen),
    )


def test_ignore_policy_excludes_next_build_output(suite):
    """The exact real bug this project hit dogfooding against a real
    Next.js project (docs/18/step-log) - .next must never be walked.
    """
    with TempProject() as root:
        proj = _Writer(root)
        proj.write(".next/build/chunks/huge.js", "/* compiled bundle */\n")
        proj.write("app/page.tsx", "export default function Page() { return null }\n")
        result = discover_project(root)
    suite.check("a file under .next is never scanned", result.scanned_files == 1)


def test_ignore_policy_excludes_git_dist_build_coverage_venv(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write(".git/HEAD", "ref: refs/heads/main\n")
        proj.write("dist/bundle.js", "x\n")
        proj.write("build/out.js", "x\n")
        proj.write("coverage/index.html", "<html></html>\n")
        proj.write(".venv/pyvenv.cfg", "home = /usr\n")
        proj.write("real_source.py", "print(1)\n")
        result = discover_project(root)
    suite.check(
        "every ignored directory is pruned, only the real source file is scanned",
        result.scanned_files == 1,
        " (scanned={})".format(result.scanned_files),
    )


def test_ci_files_still_found_despite_dot_github_not_being_hard_ignored(suite):
    """Documented deviation from a literal reading of docs/19's ignore list:
    .github holds real CI files this engine must detect (docs/19's own File
    Categorization section requires .github/workflows), so it is walked -
    only .git itself is pruned.
    """
    result = _pm_project({".github/workflows/ci.yml": "on: push\n"})
    suite.check(
        "a .github/workflows file is found, not silently pruned away",
        ".github/workflows/ci.yml" in result.project.ci_files,
    )


# --- directories ---------------------------------------------------------

def test_important_directories_detected(suite):
    result = _pm_project({
        "src/index.py": "x = 1\n",
        "components/Button.tsx": "export {}\n",
        "docs/readme.md": "hi\n",
    })
    dirs = result.project.important_directories
    suite.check("src/ detected", "src" in dirs)
    suite.check("components/ detected", "components" in dirs)
    suite.check("docs/ detected", "docs" in dirs)


def test_test_directories_detected(suite):
    result = _pm_project({
        "tests/test_x.py": "def test_x(): pass\n",
        "components/__tests__/x.test.tsx": "test('x', () => {})\n",
    })
    suite.check("tests/ detected as a test directory", "tests" in result.project.test_directories)
    suite.check(
        "__tests__/ detected as a test directory",
        "components/__tests__" in result.project.test_directories,
    )


# --- symlinks & permission errors (platform-guarded, best effort) --------

def test_broken_symlink_is_ignored_not_crashed_on(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("real.py", "print(1)\n")
        link = root / "broken_link.py"
        try:
            os.symlink(root / "does_not_exist.py", link)
        except (OSError, NotImplementedError):
            return suite.check("broken symlink test skipped (symlinks unsupported here)", True)
        result = discover_project(root)
    suite.check("discovery completes despite a broken symlink", result.status == STATUS_SUCCESS)
    suite.check(
        "the broken symlink itself is not counted as a scanned file",
        result.scanned_files == 1,
        " (scanned={})".format(result.scanned_files),
    )


def test_circular_symlink_does_not_hang(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("real.py", "print(1)\n")
        (root / "sub").mkdir()
        loop_link = root / "sub" / "back_to_root"
        try:
            os.symlink(root, loop_link, target_is_directory=True)
        except (OSError, NotImplementedError):
            return suite.check("circular symlink test skipped (symlinks unsupported here)", True)
        started = time.perf_counter()
        result = discover_project(root)
        elapsed = time.perf_counter() - started
    suite.check("a circular symlink does not cause a hang", elapsed < 10.0, " (took {:.2f}s)".format(elapsed))
    suite.check("discovery still completes successfully", result.status == STATUS_SUCCESS)


def test_permission_denied_subtree_is_partial_success(suite):
    if os.name == "nt":
        return suite.check(
            "permission-denied-subtree test skipped on Windows (chmod semantics differ)", True
        )
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("readable.py", "print(1)\n")
        locked = root / "locked"
        locked.mkdir()
        (locked / "secret.py").write_text("print('secret')\n", encoding="utf-8")
        os.chmod(locked, 0o000)
        try:
            result = discover_project(root)
        finally:
            os.chmod(locked, 0o755)
    suite.check(
        "an unreadable subtree still yields a usable result",
        result.status in (STATUS_PARTIAL_SUCCESS, STATUS_SUCCESS),
    )
    suite.check("the readable file outside it is still found", result.project is not None and result.scanned_files >= 1)


def test_permission_denied_root_itself(suite):
    if os.name == "nt":
        return suite.check("permission-denied-root test skipped on Windows", True)
    with TempProject() as root:
        os.chmod(root, 0o000)
        try:
            result = discover_project(root)
        finally:
            os.chmod(root, 0o755)
    suite.check("a completely unreadable root -> PERMISSION_DENIED", result.status == STATUS_PERMISSION_DENIED)
    suite.check("permission-denied root -> no project", result.project is None)


# --- determinism & performance -------------------------------------------

def test_determinism_repeated_calls_produce_identical_result(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("package.json", '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}')
        proj.write("app/api/x/route.ts", "export function GET() {}\n")
        proj.write("requirements.txt", "flask\n")
        first = discover_project(root)
        second = discover_project(root)
    suite.check("repository_type is identical across two runs", first.project.repository_type == second.project.repository_type)
    suite.check("application_type is identical across two runs", first.project.application_type == second.project.application_type)
    suite.check(
        "languages tuple is byte-identical across two runs", first.project.languages == second.project.languages
    )
    suite.check(
        "frameworks tuple is byte-identical across two runs", first.project.frameworks == second.project.frameworks
    )
    suite.check("important_files is byte-identical across two runs", first.project.important_files == second.project.important_files)


def test_large_ignored_tree_stays_fast(suite):
    """Not hundreds of thousands of files (too slow for a regression suite
    that runs on every change) - a moderate stress count, entirely inside
    an ignored directory, proving pruning happens before descent rather
    than after (an O(ignored-tree-size) implementation would be visibly
    slow here; an O(pruned-at-the-door) one finishes in a fraction of a
    second regardless of how large node_modules is).
    """
    with TempProject() as root:
        proj = _Writer(root)
        for i in range(2000):
            proj.write("node_modules/pkg{}/index.js".format(i), "module.exports = {{}};\n")
        proj.write("app.py", "print(1)\n")
        started = time.perf_counter()
        result = discover_project(root)
        elapsed = time.perf_counter() - started
    suite.check("2000 ignored files are pruned, not walked", result.scanned_files == 1)
    suite.check("pruning keeps discovery fast", elapsed < 5.0, " (took {:.2f}s)".format(elapsed))


def test_nested_workspace_deep_structure(suite):
    result = _pm_project({
        "turbo.json": "{}",
        "apps/web/package.json": '{"dependencies": {"react": "1.0.0"}}',
        "apps/web/src/index.tsx": "export {}\n",
        "packages/ui/package.json": "{}",
        "packages/ui/src/Button.tsx": "export {}\n",
    })
    suite.check("nested workspace -> monorepo", result.project.repository_type == "monorepo")
    suite.check(
        "nested workspace finds both sub-packages",
        {"apps/web", "packages/ui"} <= set(result.project.monorepo_packages),
    )


# --- immutability & the "no evidence, no fact" contract --------------------

def test_detected_item_refuses_empty_evidence(suite):
    try:
        DetectedItem(name="Nothing", evidence=())
        raised = False
    except ValueError:
        raised = True
    suite.check("DetectedItem with no evidence raises ValueError", raised)


def test_project_knowledge_is_frozen(suite):
    result = discover_project(".")
    try:
        result.project.root_path = "/tampered"
        raised = False
    except Exception:
        raised = True
    suite.check("ProjectKnowledge cannot be mutated after construction", raised)


def test_project_discovery_result_is_frozen(suite):
    result = discover_project(".")
    try:
        result.status = "tampered"
        raised = False
    except Exception:
        raised = True
    suite.check("ProjectDiscoveryResult cannot be mutated after construction", raised)


def test_evidence_mapping_is_read_only(suite):
    result = discover_project(".")
    try:
        result.project.evidence["injected"] = ("nope",)
        raised = False
    except TypeError:
        raised = True
    suite.check("ProjectKnowledge.evidence cannot be written to from outside", raised)


# --- no AI, no subprocess, no network imports ------------------------------

def test_project_package_never_imports_qa_agent_ai(suite):
    """Source-grep isolation check, the same pattern every AI-era module in
    this project already uses to keep the one-directional import rule
    honest (qa_agent/ai/__init__.py's own docstring) - matched against
    actual import statements only (anchored at line-start, ignoring
    indentation), not bare substring occurrences, which would otherwise
    trip on this module's own docstrings explaining the isolation rule in
    prose (the same false-positive class documented repeatedly in
    docs/step-log.md for Phase E's isolation tests).
    """
    import re

    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "project"
    pattern = re.compile(r"^\s*(import qa_agent\.ai|from \.\.ai\b|from \.ai\b|from qa_agent\.ai\b)", re.MULTILINE)
    offending = []
    for path in package_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offending.append(path.name)
    suite.check("no file in qa_agent/project/ imports qa_agent.ai", offending == [], " ({})".format(offending))


def test_project_package_never_imports_subprocess_or_requests(suite):
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "project"
    offending = []
    for path in package_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import subprocess" in text or "import requests" in text or "urllib.request" in text:
            offending.append(path.name)
    suite.check(
        "no file in qa_agent/project/ imports subprocess/requests/urllib.request",
        offending == [],
        " ({})".format(offending),
    )


# --- CLI integration -------------------------------------------------------

def test_cli_discover_reports_a_real_project(suite):
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("package.json", '{"dependencies": {"react": "18.0.0"}}')
        proc = run_agent(["discover", str(root)])
    suite.check("discover CLI exits 0 on a normal project", proc.returncode == 0)
    suite.check("discover CLI output names React", "React" in proc.stdout)
    suite.check("discover CLI output names the status", "Status:  success" in proc.stdout)


def test_cli_discover_invalid_root_exits_nonzero(suite):
    proc = run_agent(["discover", "Z:/definitely/not/a/real/path/qa-agent"])
    suite.check("discover CLI exits 2 on an invalid root", proc.returncode == 2)
    suite.check("discover CLI still prints a structured status, not a traceback", "Traceback" not in proc.stdout)


def test_cli_discover_never_invokes_ai_or_analyzers(suite):
    """The CLI command itself performs no analysis - a project with a real,
    known ruff finding must show zero findings-related output from the
    `discover` command, since it never calls `runner.run()` at all.
    """
    with TempProject() as root:
        proj = _Writer(root)
        proj.write("bad.py", "import os\n")  # a real, unused-import finding for ruff
        proc = run_agent(["discover", str(root)])
    suite.check("discover CLI output never mentions ruff", "ruff" not in proc.stdout.lower())
    suite.check("discover CLI output never mentions findings", "finding" not in proc.stdout.lower())


if __name__ == "__main__":
    suite = Suite("Phase F Part 1: Project Discovery Engine")
    sys.exit(suite.run([
        test_invalid_root_nonexistent_path,
        test_invalid_root_path_is_a_file,
        test_invalid_root_wrong_type_never_raises,
        test_empty_repository,
        test_python_only_project,
        test_mixed_language_repository,
        test_nextjs_project_reports_both_nextjs_and_react,
        test_react_only_project,
        test_express_project,
        test_fastapi_project,
        test_flask_project,
        test_django_project_via_manage_py,
        test_nestjs_project,
        test_vue_and_angular_projects,
        test_spring_boot_via_pom_xml,
        test_laravel_via_composer_json,
        test_aspnet_via_csproj,
        test_unknown_framework_reports_nothing_invented,
        test_package_manager_npm,
        test_package_manager_pnpm,
        test_package_manager_yarn,
        test_package_manager_bun,
        test_package_manager_poetry_via_pyproject_marker,
        test_package_manager_cargo_and_go_modules,
        test_package_manager_maven_and_gradle,
        test_build_system_vite_and_webpack,
        test_build_system_make_and_cmake,
        test_categorization_docker,
        test_categorization_ci,
        test_categorization_dependency,
        test_categorization_environment_never_reads_contents,
        test_categorization_specification,
        test_categorization_documentation,
        test_categorization_configuration,
        test_categorization_runtime,
        test_categorization_every_file_at_most_one_category,
        test_uncategorized_files_do_not_appear_as_important,
        test_repository_type_single_package,
        test_repository_type_workspace_multiple_manifests,
        test_repository_type_monorepo_via_marker_file,
        test_repository_type_monorepo_via_package_json_workspaces_key,
        test_repository_type_unknown_when_no_manifest,
        test_workspace_structure_and_monorepo_packages,
        test_application_type_frontend_only,
        test_application_type_backend_only,
        test_application_type_full_stack_two_frameworks,
        test_application_type_full_stack_via_nextjs_api_directory,
        test_application_type_library_no_entrypoint,
        test_application_type_cli_via_package_json_bin,
        test_application_type_cli_via_python_console_scripts,
        test_application_type_unknown_when_nothing_found,
        test_application_type_desktop_via_electron,
        test_application_type_mobile_via_react_native,
        test_ignore_policy_excludes_node_modules,
        test_ignore_policy_excludes_next_build_output,
        test_ignore_policy_excludes_git_dist_build_coverage_venv,
        test_ci_files_still_found_despite_dot_github_not_being_hard_ignored,
        test_important_directories_detected,
        test_test_directories_detected,
        test_broken_symlink_is_ignored_not_crashed_on,
        test_circular_symlink_does_not_hang,
        test_permission_denied_subtree_is_partial_success,
        test_permission_denied_root_itself,
        test_determinism_repeated_calls_produce_identical_result,
        test_large_ignored_tree_stays_fast,
        test_nested_workspace_deep_structure,
        test_detected_item_refuses_empty_evidence,
        test_project_knowledge_is_frozen,
        test_project_discovery_result_is_frozen,
        test_evidence_mapping_is_read_only,
        test_project_package_never_imports_qa_agent_ai,
        test_project_package_never_imports_subprocess_or_requests,
        test_cli_discover_reports_a_real_project,
        test_cli_discover_invalid_root_exits_nonzero,
        test_cli_discover_never_invokes_ai_or_analyzers,
    ]))
