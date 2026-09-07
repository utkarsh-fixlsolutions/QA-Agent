"""CLI entrypoint.

  python -m qa_agent <path> [<path> ...]   check the given files/directories
  python -m qa_agent --git-diff [REF]      check only what git reports as changed
  ... --output report.md                   also write the report to a file
  python -m qa_agent watch <dir>           run as a long-running process

A directory literally named "watch" must be given as an explicit path (for
example "./watch" or an absolute path), since a bare "watch" selects watch mode.

Exit codes:
  0  ran cleanly, no findings
  1  ran cleanly, findings reported
  2  a tool failed to run, the config file itself was invalid, or no usable
     input was given - even if another tool succeeded and reported real
     findings; check the report for which
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapters import ADAPTERS, ToolError
from .gitdiff import changed_files, describe
from .analysis_bridge import AnalysisBridge
from .config import ConfigError, resolve as resolve_config
from .debouncer import Debouncer
from .fsmonitor import FileSystemMonitor
from .live_report import LiveReporter
from .report import (
    render,
    render_config_error,
    render_markdown,
    render_tool_error,
    render_unexpected_error,
    render_write_error,
)
from .runner import run
from .watch import WatchSession

_ADAPTER_NAMES = [adapter.name for adapter in ADAPTERS]


def _effective_adapters(config):
    """ADAPTERS filtered by config.analyzers, or ADAPTERS unchanged when
    config is the default (no file, or no `analyzers` key) - the same
    filtering run() itself does, needed here too only so the watch-mode
    banner and file-watch extension set reflect it (docs/16 section 22).
    """
    if config.analyzers is None:
        return ADAPTERS
    return tuple(adapter for adapter in ADAPTERS if adapter.name in config.analyzers)


def _watch_main(argv):
    parser = argparse.ArgumentParser(
        prog="qa_agent watch",
        description=(
            "Watch a project and analyze source files as they change. "
            "Runs until Ctrl+C."
        ),
    )
    parser.add_argument("project_path", help="directory to watch")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="use this config file instead of discovering .qa-agent.json",
    )
    args = parser.parse_args(argv)

    root = Path(args.project_path)
    try:
        config = resolve_config(root, args.config, known_analyzers=_ADAPTER_NAMES)
    except ConfigError as exc:
        print(render_config_error(exc), file=sys.stderr)
        return 2

    # The registry (filtered by config) stays the single source of truth for
    # what is enabled, so the banner cannot drift from reality - whether that
    # reality is "another adapter registered" (Part 2) or "config disabled
    # one" (Part 4).
    active = _effective_adapters(config)
    by_tool = {}
    for adapter in active:
        by_tool.setdefault(adapter.name, set()).update(adapter.extensions)
    analyzers = [
        "{} ({})".format(name, ", ".join(sorted(exts)))
        for name, exts in sorted(by_tool.items())
    ]

    # Composition root: the registry decides which extensions matter, and the
    # monitor is handed them. Neither module imports the other.
    bridge = AnalysisBridge(config=config)
    reporter = LiveReporter(root=root)

    def analyze_batch(batch):
        """One quiet period: analyze what changed, then report it as one batch."""
        outcome = bridge.analyze_paths(batch.paths) if batch.paths else None
        reporter.report(batch, outcome)

    def report_failure(error):
        """Anything unforeseen in the event path: shown in full, never swallowed."""
        print(render_unexpected_error("a filesystem event", error), file=sys.stderr, flush=True)

    # monitor -> debouncer -> bridge -> existing pipeline. Each link knows only
    # the next one's interface: the monitor has never heard of debouncing, and
    # the debouncer has never heard of ruff.
    debouncer = Debouncer(on_flush=analyze_batch, on_error=report_failure)
    watched_extensions = {ext for adapter in active for ext in adapter.extensions}
    monitor = FileSystemMonitor(
        root=root,
        extensions=watched_extensions,
        on_event=debouncer.submit,
        on_error=report_failure,
    )
    # Listed source-first and stopped in that order: the monitor is silenced
    # before the debouncer, so the debouncer's shutdown races nothing.
    return WatchSession(args.project_path, analyzers, components=[monitor, debouncer]).run()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "watch":
        return _watch_main(argv[1:])

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
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="use this config file instead of discovering .qa-agent.json",
    )
    args = parser.parse_args(argv)

    # argparse's error() exits 2, matching this tool's input-failure code.
    if args.git_diff is None and not args.paths:
        parser.error("give one or more paths, or --git-diff")
    if args.git_diff is not None and args.paths:
        parser.error("--git-diff selects the files itself; do not also pass paths")

    try:
        # Discovery starts from cwd - the same directory a user's shell is
        # already in when they type the command, and the same starting point
        # --git-diff already assumes when it says it "works from any
        # subdirectory" (docs/04): both walk upward until they find what
        # they're looking for.
        config = resolve_config(Path.cwd(), args.config, known_analyzers=_ADAPTER_NAMES)
    except ConfigError as exc:
        print(render_config_error(exc), file=sys.stderr)
        return 2

    try:
        if args.git_diff is not None:
            ref = None if args.git_diff is True else args.git_diff
            files, deleted = changed_files(ref)
            skipped = [(path, "deleted in working tree") for path in deleted]
            result = run(files, extra_skipped=skipped, config=config)
            source = describe(ref)
        else:
            result = run(args.paths, config=config)
            source = ", ".join(args.paths)
    except ToolError as exc:
        print(render_tool_error(exc), file=sys.stderr)
        return 2

    print(render(result, source, config_path=config.path))

    if args.output:
        try:
            Path(args.output).write_text(
                render_markdown(result, source, config_path=config.path), encoding="utf-8"
            )
        except OSError as exc:
            # The run itself succeeded; only the file write failed. Report that
            # plainly rather than discarding real results or inventing any.
            print(render_write_error(args.output, exc), file=sys.stderr)
            return 2

    # A failing analyzer never hides another analyzer's real findings - both
    # are already printed above (report.py) - but it still wins the exit
    # code, matching this project's exit-code contract since Step 3: a tool
    # failure is always exit 2, exactly as it was before a second tool
    # existed to fail independently (docs/15-unified-reporting.md).
    if result.tool_errors:
        return 2
    if result.findings:
        return 1
    if result.missing and not result.checked and not result.skipped:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
