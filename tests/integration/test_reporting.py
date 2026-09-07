"""Bridge contract and live reporting: orchestration and presentation stay apart."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite, TempProject  # noqa: E402

from qa_agent import analysis_bridge  # noqa: E402
from qa_agent.analysis_bridge import AnalysisBridge  # noqa: E402
from qa_agent.debouncer import EventBatch  # noqa: E402
from qa_agent.fsmonitor import CREATED, DELETED, FileEvent  # noqa: E402
from qa_agent.live_report import LiveReporter  # noqa: E402


def test_bridge_returns_outcomes_and_prints_nothing(suite):
    with TempProject() as root:
        target = root / "err.py"
        target.write_text("import os\n", encoding="utf-8")
        bridge = AnalysisBridge()

        outcome = bridge.analyze_paths([target])
        suite.check("success returns a result", outcome.ok and outcome.result is not None)
        suite.check("...containing the real finding",
                    any("F401" in f.message for f in outcome.result.findings))

        saved = os.environ["PATH"]
        os.environ["PATH"] = ""
        try:
            failed = bridge.analyze_paths([target])
        finally:
            os.environ["PATH"] = saved
        suite.check("a missing tool is returned, not raised", not failed.ok)
        suite.check("...classified as an expected failure", failed.expected)

        def boom(inputs, extra_skipped=None):
            raise ValueError("simulated")

        original = analysis_bridge.run
        analysis_bridge.run = boom
        try:
            unexpected = bridge.analyze_paths([target])
        finally:
            analysis_bridge.run = original
        suite.check("an unforeseen error is returned", not unexpected.ok)
        suite.check("...classified as unexpected", not unexpected.expected)


def test_bridge_holds_no_presentation(suite):
    source = (REPO_ROOT / "qa_agent" / "analysis_bridge.py").read_text(encoding="utf-8")
    code = source.split('"""')[2]  # everything after the module docstring
    suite.check("bridge imports no renderer", "from .report" not in code)
    suite.check("bridge prints nothing", "print(" not in code)


def test_reporter_renders_every_outcome(suite):
    with TempProject() as root:
        target = root / "shown.py"
        target.write_text("import os\n", encoding="utf-8")
        batch = EventBatch(events=(FileEvent(kind=CREATED, path=target),), paths=(target,))
        bridge = AnalysisBridge()

        out = []
        reporter = LiveReporter(root=root, out=out.append)
        reporter.report(batch, bridge.analyze_paths([target]))
        suite.check("findings rendered", "F401" in out[-1])
        suite.check("batch identity present", "Batch #1" in out[-1])

        reporter.report(batch, None)
        suite.check("a batch with nothing to analyse still reports",
                    "Nothing to analyze." in out[-1])
        suite.check("batch numbering continues", "Batch #2" in out[-1])

        deletion = EventBatch(events=(FileEvent(kind=DELETED, path=target),), paths=())
        reporter.report(deletion, None)
        suite.check("deletions are annotated", "(deleted)" in out[-1])


def test_reporter_caps_long_lists(suite):
    with TempProject() as root:
        events = tuple(
            FileEvent(kind=CREATED, path=root / "f{}.py".format(i)) for i in range(25)
        )
        out = []
        LiveReporter(root=root, out=out.append).report(EventBatch(events=events, paths=()), None)
        suite.check("long file lists are capped", "...and 15 more" in out[-1])
        suite.check("the cap is presentation only, not analysis",
                    out[-1].count("- f") == 10)


def test_output_is_ascii(suite):
    """Non-ASCII would crash a redirected watch session on a cp1252 console."""
    with TempProject() as root:
        events = tuple(FileEvent(kind=CREATED, path=root / "a.py") for _ in range(1))
        out = []
        LiveReporter(root=root, out=out.append).report(EventBatch(events=events, paths=()), None)
        suite.check("reporter output is pure ASCII", all(ord(c) < 128 for c in out[-1]))


if __name__ == "__main__":
    suite = Suite("Bridge contract and live reporting")
    sys.exit(suite.run([
        test_bridge_returns_outcomes_and_prints_nothing,
        test_bridge_holds_no_presentation,
        test_reporter_renders_every_outcome,
        test_reporter_caps_long_lists,
        test_output_is_ascii,
    ]))
