"""CLI entrypoint.

  python -m qa_agent <path> [<path> ...]   check the given files/directories
  python -m qa_agent --git-diff [REF]      check only what git reports as changed
  ... --output report.md                   also write the report to a file
  python -m qa_agent watch <dir>           run as a long-running process
  python -m qa_agent discover <dir>        print a deterministic project profile (Phase F Part 1)

A directory literally named "watch" or "discover" must be given as an
explicit path (for example "./watch" or an absolute path), since a bare
"watch"/"discover" selects that subcommand instead.

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
from dataclasses import replace
from pathlib import Path

from .adapters import ADAPTERS, ToolError
from .ai import MockProvider, OllamaProvider, explain_findings, summarize_run
from .ai import suggest_fixes as ai_suggest_fixes
from .gitdiff import changed_files, describe
from .analysis_bridge import AnalysisBridge
from .config import ConfigError, resolve as resolve_config
from .debouncer import Debouncer
from .fsmonitor import FileSystemMonitor
from .live_report import LiveReporter
from .project import (
    STATUS_DISCOVERY_FAILED as _DISCOVERY_STATUS_FAILED,
    STATUS_INVALID_ROOT as _DISCOVERY_STATUS_INVALID_ROOT,
    STATUS_PERMISSION_DENIED as _DISCOVERY_STATUS_PERMISSION_DENIED,
)
from .project import build_repository_context, discover_project
from .project import render as render_discovery
from .project import render_context
from .report import (
    render,
    render_config_error,
    render_markdown,
    render_tool_error,
    render_unexpected_error,
    render_write_error,
)
from .runner import RunResult, run
from .watch import WatchSession

_ADAPTER_NAMES = [adapter.name for adapter in ADAPTERS]
_AI_PROVIDER_CHOICES = ("ollama", "mock")


def _add_ai_arguments(parser):
    """The six AI flags (Phase D Part 6), shared verbatim between the
    one-shot and watch parsers so `--ai`/`--ai-explain`/etc. mean exactly
    the same thing in both modes.
    """
    parser.add_argument(
        "--ai", action="store_true",
        help="enable AI enrichment; with no other --ai-* flag, enables all of them",
    )
    parser.add_argument("--ai-explain", action="store_true", help="enable AI explanations")
    parser.add_argument("--ai-summary", action="store_true", help="enable an AI run summary")
    parser.add_argument(
        "--ai-fix", action="store_true",
        help="enable AI-suggested fixes (advisory only; never applied automatically)",
    )
    parser.add_argument("--ai-model", metavar="MODEL", help="override the configured AI model")
    parser.add_argument(
        "--ai-provider", choices=_AI_PROVIDER_CHOICES, help="override the configured AI provider",
    )


def _effective_ai_settings(args, ai_config):
    """CLI > Config file > Defaults (docs/step-log.md, Phase D Part 6).

    Every CLI flag here can only enable something, never force it off -
    the same additive-only convention this CLI's other flags (--git-diff,
    -o) already follow; there is no existing precedent for a flag that
    un-does a config setting. A bare `--ai`, with no specific `--ai-*`
    feature flag also given, enables all three features - the "just try
    it" convenience a single flag should offer; naming a specific feature
    (`--ai-explain`, alone or combined with others) is always enough on
    its own and never silently pulls in features that were not asked for.
    """
    specific = args.ai_explain or args.ai_summary or args.ai_fix
    bare_ai = args.ai and not specific
    return replace(
        ai_config,
        enabled=args.ai or specific or ai_config.enabled,
        provider=args.ai_provider or ai_config.provider,
        model=args.ai_model or ai_config.model,
        explain=args.ai_explain or bare_ai or ai_config.explain,
        summary=args.ai_summary or bare_ai or ai_config.summary,
        suggest_fixes=args.ai_fix or bare_ai or ai_config.suggest_fixes,
    )


def _build_ai_provider(ai_settings):
    """Construct the configured provider, or None when AI is disabled.

    `provider` is already restricted to a known name by config.py's own
    validation and by --ai-provider's argparse `choices` - the final
    `return None` below is an unreachable defensive fallback, not a real
    validation path, so an unsupported name is never silently swallowed
    here; it would already have been rejected earlier, loudly.
    """
    if not ai_settings.enabled:
        return None
    if ai_settings.provider == "mock":
        kwargs = {} if ai_settings.model is None else {"model": ai_settings.model}
        return MockProvider(**kwargs)
    if ai_settings.provider == "ollama":
        kwargs = {}
        if ai_settings.model is not None:
            kwargs["model"] = ai_settings.model
        if ai_settings.endpoint is not None:
            kwargs["endpoint"] = ai_settings.endpoint
        if ai_settings.timeout is not None:
            kwargs["timeout"] = ai_settings.timeout
        return OllamaProvider(**kwargs)
    return None  # pragma: no cover - unreachable, see docstring


def _run_ai_pipeline(result, ai_settings, provider):
    """Run whichever AI features are enabled, entirely best-effort.

    Every function called here already returns None or an empty mapping on
    any failure (offline, timeout, malformed response, insufficient
    context - Parts 3-5) and never raises, so nothing here needs its own
    try/except: the deterministic `result` this was given is always
    returned to the caller completely unaffected either way (docs/step-log
    .md, Phase D Part 6, graceful degradation).
    """
    summary, explanations, fixes = None, {}, {}
    if provider is not None:
        if ai_settings.summary:
            summary = summarize_run(result, provider)
        if ai_settings.explain:
            explanations = explain_findings(result.findings, provider)
        if ai_settings.suggest_fixes:
            fixes = ai_suggest_fixes(result.findings, provider)
    return summary, explanations, fixes


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
    _add_ai_arguments(parser)
    args = parser.parse_args(argv)

    root = Path(args.project_path)
    try:
        config = resolve_config(root, args.config, known_analyzers=_ADAPTER_NAMES)
    except ConfigError as exc:
        print(render_config_error(exc), file=sys.stderr)
        return 2

    # Resolved once at startup and held for the whole session, exactly like
    # AnalysisBridge's own config - a change needs a restart, not a
    # mid-session reload (docs/step-log.md, Phase D Part 6).
    ai_settings = _effective_ai_settings(args, config.ai)
    ai_provider = _build_ai_provider(ai_settings)

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
        """One quiet period: analyze what changed, then report it as one batch.

        AI runs per batch, on that batch's own findings only - the same
        best-effort, never-raises pipeline the one-shot CLI uses
        (_run_ai_pipeline). A run-level AI summary is deliberately not
        offered here: it has no natural meaning for one incremental batch
        of changed files (Part 4's own reasoning, unchanged), so only
        explanations and suggested fixes - both already per-finding - are
        wired into watch mode.
        """
        outcome = bridge.analyze_paths(batch.paths) if batch.paths else None
        explanations, fixes = {}, {}
        if outcome is not None and outcome.ok and ai_provider is not None:
            # AnalysisOutcome.result is typed as plain `object`; outcome.ok
            # (error is None) is analysis_bridge.py's own guarantee that a
            # real RunResult is there - narrowed explicitly since pyright
            # cannot infer that from the ok check alone.
            assert isinstance(outcome.result, RunResult)
            if ai_settings.explain:
                explanations = explain_findings(outcome.result.findings, ai_provider)
            if ai_settings.suggest_fixes:
                fixes = ai_suggest_fixes(outcome.result.findings, ai_provider)
        reporter.report(batch, outcome, explanations=explanations, suggested_fixes=fixes)

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


def _discover_main(argv):
    """`python -m qa_agent discover <path>` (Phase F Part 1, docs/19). A
    read-only command, deliberately kept as small as `_watch_main` is
    large: it calls `discover_project()`, prints `render()`'s output, and
    exits - no AI, no analyzers, no repair, nothing else. Exists for
    dogfooding and debugging the discovery engine directly, matching the
    CLI-visibility docs/19 itself required.
    """
    parser = argparse.ArgumentParser(
        prog="qa_agent discover",
        description=(
            "Print a deterministic, evidence-based profile of a project's "
            "structure - languages, frameworks, package managers, and "
            "important files/directories. No AI, no analyzers, no repair, "
            "no network, no subprocess."
        ),
    )
    parser.add_argument("project_path", help="directory to inspect")
    parser.add_argument(
        "--context", action="store_true",
        help="also build and print the RepositoryContext (Phase F Part 2) - still read-only, no AI",
    )
    args = parser.parse_args(argv)

    result = discover_project(args.project_path)
    print(render_discovery(result))
    if args.context and result.project is not None:
        print(render_context(build_repository_context(result.project)))
    if result.status in (
        _DISCOVERY_STATUS_INVALID_ROOT,
        _DISCOVERY_STATUS_PERMISSION_DENIED,
        _DISCOVERY_STATUS_FAILED,
    ):
        return 2
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "watch":
        return _watch_main(argv[1:])
    if argv and argv[0] == "discover":
        return _discover_main(argv[1:])

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
    _add_ai_arguments(parser)
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

    # Runs after the deterministic result exists, exactly per the required
    # flow (RunResult -> summary -> explanations -> suggested fixes ->
    # render), and never raises - see _run_ai_pipeline's own docstring.
    # AI disabled (no flag, no config "ai" section) means ai_settings.enabled
    # is False, _build_ai_provider returns None, and every value below stays
    # at its untouched default - report output is then byte-for-byte
    # identical to Phase C (docs/step-log.md, Phase D Part 6).
    ai_settings = _effective_ai_settings(args, config.ai)
    ai_provider = _build_ai_provider(ai_settings)
    summary, explanations, suggested_fixes = _run_ai_pipeline(result, ai_settings, ai_provider)

    print(render(result, source, config_path=config.path,
                 explanations=explanations, summary=summary, suggested_fixes=suggested_fixes))

    if args.output:
        try:
            Path(args.output).write_text(
                render_markdown(result, source, config_path=config.path,
                                 explanations=explanations, summary=summary,
                                 suggested_fixes=suggested_fixes),
                encoding="utf-8",
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
