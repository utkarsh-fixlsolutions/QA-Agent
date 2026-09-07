"""Run every test suite. One command to verify the project, months from now.

    python tests/run_all.py            everything (about a minute)
    python tests/run_all.py --quick    skip the stress suites
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
GROUPS = ("regression", "integration", "stress")


def suites(skip_stress):
    for group in GROUPS:
        if skip_stress and group == "stress":
            continue
        for path in sorted((TESTS / group).glob("test_*.py")):
            yield group, path


def main(argv):
    quick = "--quick" in argv
    results = []
    started = time.perf_counter()

    for group, path in suites(quick):
        print("=" * 66)
        print("{}/{}".format(group, path.name))
        print("=" * 66)
        proc = subprocess.run([sys.executable, str(path)], cwd=str(TESTS.parent))
        results.append((group, path.name, proc.returncode))

    elapsed = time.perf_counter() - started
    print("=" * 66)
    failed = [r for r in results if r[2] != 0]
    for group, name, code in results:
        print("  {}  {}/{}".format("PASS" if code == 0 else "FAIL", group, name))
    print("-" * 66)
    print("{}/{} suites passed in {:.1f}s{}".format(
        len(results) - len(failed), len(results), elapsed,
        "  (stress skipped)" if quick else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
