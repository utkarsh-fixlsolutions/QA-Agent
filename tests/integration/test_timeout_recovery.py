"""A hung analyzer must time out, be reported, and leave the watcher working.

Recovery matters more than the timeout itself: a watcher that survives a hang but
never analyses anything again has not really recovered.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent import runner  # noqa: E402
from qa_agent.analysis_bridge import AnalysisBridge  # noqa: E402
from qa_agent.debouncer import Debouncer  # noqa: E402
from qa_agent.fsmonitor import MODIFIED, FileEvent  # noqa: E402
from qa_agent.live_report import LiveReporter  # noqa: E402


class _Hung:
    """Stands in for a ruff process that never returns."""

    returncode = 0
    stdout = "[]"
    stderr = ""


def _hanging_run(command, **kwargs):
    """Behaves like subprocess.run with a timeout that always expires."""
    timeout = kwargs.get("timeout")
    if timeout is None:
        time.sleep(30)  # no timeout passed: this is the bug we are guarding against
        return _Hung()
    time.sleep(min(timeout, 1.0))
    raise runner.subprocess.TimeoutExpired(cmd=command, timeout=timeout)


def test_timeout_is_passed_at_all(suite):
    """The guard only works if a timeout actually reaches subprocess.run."""
    captured = {}
    original = runner.subprocess.run

    def spy(command, **kwargs):
        captured.update(kwargs)
        return original(command, **kwargs)

    runner.subprocess.run = spy
    try:
        with TempProject() as root:
            (root / "x.py").write_text("v = 1\n", encoding="utf-8")
            runner.run([root / "x.py"])
    finally:
        runner.subprocess.run = original

    suite.check("a timeout is passed to the analyzer process",
                captured.get("timeout") is not None,
                "  [{}s]".format(captured.get("timeout")))
    suite.check("the timeout is the documented 60s",
                captured.get("timeout") == runner.ANALYSIS_TIMEOUT_SECONDS)


def test_hang_times_out_and_recovers(suite):
    outputs = []
    bridge = AnalysisBridge()
    reporter = LiveReporter(root=Path("."), out=outputs.append)

    def flush(batch):
        reporter.report(batch, bridge.analyze_paths(batch.paths) if batch.paths else None)

    debouncer = Debouncer(on_flush=flush, interval=0.1)

    with TempProject() as root:
        target = root / "hangs.py"
        target.write_text("import os\n", encoding="utf-8")

        # 1. the analyzer hangs
        original = runner.subprocess.run
        runner.subprocess.run = _hanging_run
        try:
            debouncer.submit(FileEvent(kind=MODIFIED, path=target))
            deadline = time.perf_counter() + 15
            while not outputs and time.perf_counter() < deadline:
                time.sleep(0.1)
        finally:
            runner.subprocess.run = original

        suite.check("the hang produced a report rather than hanging forever", bool(outputs))
        first = outputs[0] if outputs else ""
        suite.check("timeout reported clearly", "did not finish within" in first,
                    "" if "did not finish within" in first else "  [{}]".format(first[:90]))
        suite.check("reported as an analyzer error, not as code findings",
                    "Analyzer errors" in first)
        suite.check("no finding was invented from the failure", "Findings (" not in first)

        # 2. the watcher is still alive and the NEXT analysis works normally
        outputs.clear()
        debouncer.submit(FileEvent(kind=MODIFIED, path=target))
        deadline = time.perf_counter() + 15
        while not outputs and time.perf_counter() < deadline:
            time.sleep(0.1)

        recovered = outputs[0] if outputs else ""
        suite.check("the very next analysis ran", bool(outputs))
        suite.check("...and produced real findings", "F401" in recovered,
                    "" if "F401" in recovered else "  [{}]".format(recovered[:90]))
        suite.check("...with no trace of the earlier timeout",
                    "did not finish within" not in recovered)

    suite.check("debouncer still stops cleanly afterwards", debouncer.stop() is True)


def test_hang_does_not_block_forever_at_shutdown(suite):
    """Even mid-hang, stop() must return within its bound rather than wedging."""
    debouncer = Debouncer(on_flush=lambda batch: time.sleep(5.0), interval=0.05)
    debouncer.submit(FileEvent(kind=MODIFIED, path=Path("a.py")))
    time.sleep(0.4)
    started = time.perf_counter()
    finished = debouncer.stop(timeout=1.0)
    waited = time.perf_counter() - started
    suite.check("stop() returns within its bound", waited < 2.5, "  [{:.1f}s]".format(waited))
    suite.check("...and reports that work was still running", finished is False)
    debouncer.stop(timeout=10)


if __name__ == "__main__":
    suite = Suite("Analyzer timeout and recovery")
    sys.exit(suite.run([
        test_timeout_is_passed_at_all,
        test_hang_times_out_and_recovers,
        test_hang_does_not_block_forever_at_shutdown,
    ]))
