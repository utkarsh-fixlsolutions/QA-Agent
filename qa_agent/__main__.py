"""CLI entrypoint.

  python -m qa_agent <path> [<path> ...]   check the given files/directories
  python -m qa_agent --git-diff [REF]      check only what git reports as changed
  ... --output report.md                   also write the report to a file

Exit codes:
  0  ran cleanly, no findings
  1  ran cleanly, findings reported
  2  a tool failed to run, or no usable input was given
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapters import ToolError
from .gitdiff import changed_files, describe
from .report import render, render_markdown, render_tool_error, render_write_error
from .runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="qa_agent",
        description="Run local checks on the given files or directories and report real findings.",
    )
    parser.add_argument("paths", nargs="*", help="file(s) and/or directory(ies) to check")
    parser.add_argument(
        "--git-diff",
        nargs="?",
        const=True,
        default=None,
        metavar="REF",
        help=(
            "check only files git reports as changed: staged + unstaged vs HEAD "
            "plus untracked files, or vs REF if given. Must run inside a git repo."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="also write the report to PATH as Markdown (terminal output is unchanged)",
    )
    args = parser.parse_args(argv)

    # argparse's error() exits 2, matching this tool's input-failure code.
    if args.git_diff is None and not args.paths:
        parser.error("give one or more paths, or --git-diff")
    if args.git_diff is not None and args.paths:
        parser.error("--git-diff selects the files itself; do not also pass paths")

    try:
        if args.git_diff is not None:
            ref = None if args.git_diff is True else args.git_diff
            files, deleted = changed_files(ref)
            skipped = [(path, "deleted in working tree") for path in deleted]
            result = run(files, extra_skipped=skipped)
            source = describe(ref)
        else:
            result = run(args.paths)
            source = ", ".join(args.paths)
    except ToolError as exc:
        print(render_tool_error(exc), file=sys.stderr)
        return 2

    print(render(result, source))

    if args.output:
        try:
            Path(args.output).write_text(render_markdown(result, source), encoding="utf-8")
        except OSError as exc:
            # The run itself succeeded; only the file write failed. Report that
            # plainly rather than discarding real results or inventing any.
            print(render_write_error(args.output, exc), file=sys.stderr)
            return 2

    if result.findings:
        return 1
    if result.missing and not result.checked and not result.skipped:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
