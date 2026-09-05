"""Tool adapters: file extension -> (command shape, output parser).

docs/02-tool-selection.md defines the contract every adapter satisfies: build a
CLI command for a batch of files, and parse that tool's raw output into the
shared finding shape {file, line, severity, message, tool}. Nothing outside this
module needs to know which language or tool produced a finding.

v1 registers exactly one adapter: ".py" -> ruff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """One real issue reported by a tool. Never constructed from anything else."""

    file: str
    line: int
    severity: str
    message: str
    tool: str


class ToolError(Exception):
    """The tool itself failed to run. Reported as a tool error, never as a finding."""


# Optional severity policy, owned by us rather than by the tool.
#
# Empty by default, which means every finding keeps the severity the tool itself
# reported (docs/02-tool-selection.md section 3 - no invented severities). Add
# entries here to classify specific ruff rules yourself, e.g.:
#     SEVERITY_POLICY = {"E711": "warning", "F821": "error"}
# Keys are exact ruff rule codes; prefix matching is deliberately not attempted,
# so any reclassification stays explicit and auditable.
SEVERITY_POLICY = {}  # type: dict


class RuffAdapter:
    """Adapter for ruff (docs/02-tool-selection.md sections 2-3)."""

    name = "ruff"

    def build_command(self, files):
        # Exactly the command shape documented in Step 2.
        return ["ruff", "check", *files, "--output-format=json", "--exit-zero"]

    def parse(self, stdout):
        if not stdout.strip():
            return []
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ToolError("ruff produced output that is not valid JSON: {}".format(exc)) from exc

        findings = []
        for item in raw:
            code = item.get("code") or ""
            message = "{}: {}".format(code, item["message"]) if code else item["message"]
            findings.append(
                Finding(
                    file=item["filename"],
                    line=item["location"]["row"],
                    severity=SEVERITY_POLICY.get(code, item.get("severity", "unknown")),
                    message=message,
                    tool=self.name,
                )
            )
        return findings


ADAPTERS = {".py": RuffAdapter()}
