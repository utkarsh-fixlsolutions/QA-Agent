"""The whole watch pipeline, driven as a real process with real file edits.

Watchdog -> FileSystemMonitor -> Debouncer -> AnalysisBridge -> Runner -> LiveReporter
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite, TempProject  # noqa: E402


def _watch(root, actions, settle=1.6):
    """Run watch mode as a real subprocess while performing real file operations."""
    log = root / "_watch.log"
    handle = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "qa_agent", "watch", str(root)],
        cwd=str(REPO_ROOT), stdout=handle, stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(2.5)  # let the observer come up
        for action in actions:
            action()
            time.sleep(settle)
        alive = proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()
    return log.read_text(encoding="utf-8"), alive


def test_full_pipeline(suite):
    with TempProject() as root:
        (root / "src").mkdir()
        # Pre-existing files with real bugs: they must never be analysed, which
        # is what proves the analysis is incremental rather than a rescan.
        (root / "untouched.py").write_text("import os\n", encoding="utf-8")

        def create_bad():
            (root / "src" / "capture.py").write_text(
                "import json\n\n\ndef f(x):\n    if x == None:\n        return 1\n    return 2\n",
                encoding="utf-8")

        def fix_it():
            (root / "src" / "capture.py").write_text("def f(x):\n    return x\n", encoding="utf-8")

        def two_files():
            (root / "src" / "a.py").write_text("import os\n", encoding="utf-8")
            (root / "src" / "b.py").write_text("y = 1\n", encoding="utf-8")

        def rename():
            os.rename(root / "src" / "a.py", root / "src" / "renamed.py")

        def delete():
            (root / "src" / "b.py").unlink()

        def unsupported():
            (root / "notes.txt").write_text("hello\n", encoding="utf-8")

        def bulk():
            for i in range(14):
                (root / "src" / "bulk{}.py".format(i)).write_text("v = 1\n", encoding="utf-8")

        out, alive = _watch(root, [create_bad, fix_it, two_files, rename, delete,
                                   unsupported, bulk])

    batches = re.findall(r"Batch #(\d+)   (\d{2}:\d{2}:\d{2})", out)
    numbers = [int(n) for n, _ in batches]
    inputs = re.findall(r"Files changed:", out)

    suite.check("the watcher survived every operation", alive)
    suite.check("each change produced a batch", len(batches) >= 6, "  [{}]".format(len(batches)))
    suite.check("batches are numbered sequentially", numbers == list(range(1, len(numbers) + 1)))
    suite.check("every batch lists its files", len(inputs) == len(batches))
    suite.check("findings are real", "F401" in out and "E711" in out)
    suite.check("a fixed file reports clean", "no issues found" in out)
    suite.check("rename is reported with its origin", "(renamed from" in out)
    suite.check("deletion is reported", "(deleted)" in out)
    suite.check("deletion is not analysed", "Nothing to analyze." in out)
    suite.check("unsupported file produces no output at all", "notes.txt" not in out)
    suite.check("large batch is capped", "...and 4 more" in out)
    suite.check("INCREMENTAL: pre-existing buggy file never analysed", "untouched" not in out)
    suite.check("no duplicated summary block", "Run at:" not in out and "Checked:" not in out)
    suite.check("output is ASCII, safe when redirected", all(ord(c) < 128 for c in out))
    suite.check("no traceback leaked into the stream", "Traceback" not in out)


if __name__ == "__main__":
    suite = Suite("Watch pipeline end to end")
    sys.exit(suite.run([test_full_pipeline]))
