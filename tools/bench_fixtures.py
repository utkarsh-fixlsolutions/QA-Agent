"""Realistic fixture builders for the model benchmark (tools/model_bench.py).

Deliberately standalone - this is a tool, not part of `qa_agent`, and does
not import `tests/harness.py` either (tools/ and tests/ stay independent of
each other; both depend on `qa_agent`, neither depends on the other).
Fixture *style* mirrors the existing test suites' own conventions
(`test_runtime_diagnosis.py`'s hand-built `RuntimeCheckResult`s,
`test_ai_behavior_contract.py`'s `_project`/`_thin_context` helpers) closely
enough that anyone familiar with those tests will recognize this shape
immediately - not literally imported, since a tool has no business
depending on test-only infrastructure.

Every object built here is a real instance of a real production dataclass
(`RuntimeCheckResult`, `RuntimeCheck`, `RepositoryContext`, `Finding`,
a small `RunResult`-shaped stand-in) - never a mock of the data layer.
The one exception, `_RunResultLike`, exists because `RunResult` itself is
`runner.py`'s own internal dataclass built up field-by-field over a real
tool invocation; `build_summary_prompt` only ever reads `.checked`/
`.tools_used`/`.findings` off whatever it is given (duck-typed, matching
`prompts.py`'s own documented convention), so a plain stand-in is exactly
what every other test of that prompt already uses.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from qa_agent.adapters import Finding
from qa_agent.project.context import RepositoryContext
from qa_agent.project.models import DetectedItem, ProjectKnowledge
from qa_agent.runtime.execution_models import STATUS_ERROR, STATUS_FAIL, RuntimeCheckResult
from qa_agent.runtime.models import PRIORITY_HIGH, RuntimeCheck


class FixtureTree:
    """A real, throwaway temporary directory holding the small real source
    files a repair/explain task needs `extract_context()` to actually read.
    Cleaned up once, explicitly, at the end of a benchmark run - never left
    behind on disk.
    """

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="qa-agent-bench-"))

    def write(self, relative: str, text: str) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
        return target

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def project(**overrides) -> ProjectKnowledge:
    kwargs = dict(root_path="C:/bench", repository_type="single-package", application_type="backend")
    kwargs.update(overrides)
    return ProjectKnowledge(**kwargs)


def repository_context(**overrides) -> RepositoryContext:
    proj = overrides.pop("project", None) or project(
        languages=(DetectedItem(name="JavaScript", evidence=("build.js",)),),
        frameworks=(),
        package_managers=(DetectedItem(name="npm", evidence=("package-lock.json",)),),
    )
    return RepositoryContext(project=proj, **overrides)


def check_result(check_id="build-verification", name="Build Verification", status=STATUS_FAIL,
                  reason="build exited 1", logs=(), exception=None) -> RuntimeCheckResult:
    return RuntimeCheckResult(
        id=check_id, name=name, status=status, start_time="", end_time="",
        duration=1.0, reason=reason, logs=logs, exception=exception,
    )


def runtime_check(check_id="build-verification", name="Build Verification",
                   required_evidence=("package.json",), reason="Build tooling detected") -> RuntimeCheck:
    return RuntimeCheck(
        id=check_id, name=name, category="Build & CI", priority=PRIORITY_HIGH,
        reason=reason, required_evidence=required_evidence,
    )


@dataclass(frozen=True)
class _RunResultLike:
    """Duck-typed stand-in for `runner.RunResult` - see module docstring."""

    checked: Tuple[str, ...]
    tools_used: Tuple[str, ...]
    findings: Tuple[Finding, ...]


def run_result(checked, tools_used, findings) -> _RunResultLike:
    return _RunResultLike(checked=tuple(checked), tools_used=tuple(tools_used), findings=tuple(findings))


# --- the small set of real source files every repair/explain task reads ----

BROKEN_SINGLE_LINE_JS = "console.error('Error: intentional failure');\nprocess.exit(1);\n"
FIXED_SINGLE_LINE_JS = "console.log('build ok');\nprocess.exit(0);"

BROKEN_MULTILINE_JS = (
    "function calculateDiscount(price, rate) {\n"
    "  const discount = price * rate\n"
    "  if (discount > price) {\n"
    "    return undefinedVariable;\n"
    "  }\n"
    "  return price - discount;\n"
    "}\n"
    "console.log(calculateDiscount(100, 2));\n"
)

THIN_EVIDENCE_JS = (
    "const config = require('./config.json');\n"
    "function start() {\n"
    "  console.log('starting with', config);\n"
    "}\n"
    "start();\n"
)

PAYMENT_PY = (
    "def charge(order):\n"
    "    amount = order['amount']\n"
    "    return amount * 1.0\n"
)

UNUSED_IMPORT_PY = (
    "import os\n"
    "import sys\n"
    "\n"
    "def main():\n"
    "    print(sys.argv)\n"
)


def build_fixture_tree() -> FixtureTree:
    tree = FixtureTree()
    tree.write("build_simple.js", BROKEN_SINGLE_LINE_JS)
    tree.write("build_multiline.js", BROKEN_MULTILINE_JS)
    tree.write("app_thin_evidence.js", THIN_EVIDENCE_JS)
    tree.write("payment.py", PAYMENT_PY)
    tree.write("finding_example.py", UNUSED_IMPORT_PY)
    return tree


__all__ = [
    "FixtureTree",
    "STATUS_ERROR",
    "STATUS_FAIL",
    "BROKEN_SINGLE_LINE_JS",
    "BROKEN_MULTILINE_JS",
    "THIN_EVIDENCE_JS",
    "PAYMENT_PY",
    "UNUSED_IMPORT_PY",
    "build_fixture_tree",
    "check_result",
    "project",
    "repository_context",
    "run_result",
    "runtime_check",
]
