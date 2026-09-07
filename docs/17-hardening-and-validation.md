# 17. Hardening & Production Validation (Phase C, Part 6)

Parts 1-5 built the multi-analyzer engine. This part validated it against real
repositories, every configuration option, and three sizes of workload; found
and fixed one real, silent bug; removed the only genuine technical debt that
turned up; and corrects two stale points in docs/12. It does not re-explain
the architecture - see [docs/12-architecture.md](12-architecture.md) for the
pipeline, module map, and event flow. Full narrative, measurements, and the
bug's root cause are in [docs/step-log.md](step-log.md)'s Part 6 entry.

## Current capabilities

- **One-shot** (`python -m qa_agent <paths>` / `--git-diff`) and **watch mode**
  (`python -m qa_agent watch <dir>`), sharing one engine and one report format.
- **Five real analyzers**, dispatched independently and merged into one
  deterministically-ordered, conservatively-deduplicated report:

  | Tool | Extension | Launch | Exit codes read as "parse my output" |
  |---|---|---|---|
  | ruff | `.py` | direct | `0` (`--exit-zero`) |
  | pyright | `.py` (shares with ruff, mypy) | direct | `0`, `1` |
  | mypy | `.py` (shares with ruff, pyright) | direct | `0`, `1` (2 is ambiguous - see below, always a tool error) |
  | eslint | `.js` | via `cmd.exe` (npm `.cmd` shim) | `0`, `1` |
  | shellcheck | `.sh` | direct | `0`, `1` |

- **One tool failing never hides another's findings** - proven with a real,
  organically-discovered failure this part (eslint's command-line-too-long
  bug below), not only with constructed tests.
- **Per-project `.qa-agent.json`** - enable/disable analyzers, ignore/include
  paths, severity floor. Validated this part against real tool output, not
  only fixtures - see [docs/16](16-configuration-system.md) for the format.

## Supported analyzers and their real trade-offs

- **ruff** - fast, near-instant, always reports `error` severity in this
  project's experience.
- **pyright** - the slowest tool by far (~0.8-15s startup depending on
  project size) and, critically, **runs from qa_agent's own `.venv`**. Point
  it at a project whose dependencies aren't installed there (any external
  repo, by default) and expect a wave of "missing import" findings that are
  real but not actionable - not a bug, just something to know before
  dogfooding this against someone else's codebase.
- **eslint** - requires the *target* project to have its own flat
  `eslint.config.(js|mjs|cjs)`; without one it fails clearly with a
  `ToolError` (verified this part against two real, config-less external
  repositories) rather than silently reporting nothing.
- **shellcheck** - fastest of the five (native binary), and the tool most
  likely to flag a Windows-specific issue (`SC1017`, CRLF from git's
  `core.autocrlf`) that is a real property of the checkout, not a false
  positive.
- **mypy** - a second, independent type checker alongside pyright (Phase C
  Part 7); shares pyright's cross-venv "missing import" noise on external
  projects. Its own exit code 2 is genuinely ambiguous (a real crash, or a
  batch containing a file it can't parse or a module-naming conflict) and is
  always treated as a tool error rather than guessed at - see docs/12's
  Known Limitations for the full explanation and a real example found by
  dogfooding (`flask`'s two same-named `conftest.py` files).

## Configuration - validated this part

Every `.qa-agent.json` option was exercised against real files and real tool
output (not only unit tests):

| Option | Validated as |
|---|---|
| `analyzers` | enabling only one of two tools sharing `.py` (ruff-only vs. pyright-only) produces genuinely different, correct results on the same file |
| `ignore` | an ignored subtree is invisible to the walk, not merely "skipped and reported" |
| `include` | overrides a default ignore (`node_modules`) when explicitly named |
| `min_severity` | filtered real mixed-severity pyright output (5 findings -> 3, both hidden ones genuinely lower severity) with the "N hidden" note present |
| `--config PATH` | an explicit path is honored over discovery |
| unknown key, invalid `min_severity`, unknown analyzer name | all three fail with `ConfigError`, exit 2, nothing analyzed |

`command` and `timeout` per-adapter overrides are **not implemented** -
confirmed this part that attempting them fails clearly (`ConfigError:
unrecognized setting(s)`) rather than being silently ignored, which was the
actual requirement (Part 6 goal 3 said "if implemented").

## Stress characteristics (measured)

| Tier | Corpus | Wall time |
|---|---|---|
| Small | 2-4 real files | ~0.5-1.0s |
| Medium | ~170 real files, 3 tools | ~4.8s |
| Large | 2,000 synthetic files, 2 tools (4,000 analyses) | ~12.2s |

Tools run **sequentially per adapter**, not in parallel (explicitly out of
scope for this part) - total wall time for N tools is close to the *sum* of
each tool's own startup cost, not the slowest one. Isolated startup cost:
pyright and eslint (~0.5-1s, both pay a runtime cold-start), ruff and
shellcheck (native binaries, near-instant).

## Known limitations (current, see docs/12 for the full list)

- Pyright's cross-venv import-resolution noise on unrelated projects (above).
- Pyright's per-chunk startup cost on very large `.py`-heavy projects spanning
  several batches - still deferred; see step-log.md Part 6 for why.
- Sequential, not parallel, per-tool execution.
- Watch mode: no feedback during sustained continuous writing; a dead
  observer is reported, not self-healed (unchanged since Phase B).

## Extension guidance

Adding a sixth analyzer needs exactly what Parts 2, 5, and 7 needed and
nothing more: implement the adapter contract in `adapters.py` (`name`,
`extensions`, `ok_exit_codes`, `use_shell`, `build_command`, `parse`) and add
it to `ADAPTERS`. No change to `runner.py`, `report.py`, `config.py`,
`analysis_bridge.py`, or watch mode is required - that guarantee has now held
across three real additions (Part 2's eslint, Part 5's pyright+shellcheck,
Part 7's mypy - the third tool to share `.py`, not just the second) plus
Part 6's full validation pass, and is the single most load-bearing property
of the whole design.

If a new adapter needs `use_shell=True` (an npm-style shim, an interpreted
script), the length-aware batching in `runner._chunks()` already accounts for
it via the `use_shell` flag on the adapter itself - no engine change needed
there either. If a tool's own exit codes conflate "real findings" with "a
genuine failure" in a way `ok_exit_codes` can't cleanly separate (mypy's exit
2 does exactly this), the safe default is to exclude the ambiguous code
entirely and let it fall through to the existing generic "undeclared exit
code is a tool error" handling - never guess, and never change the adapter
contract just to resolve one tool's own ambiguity.
