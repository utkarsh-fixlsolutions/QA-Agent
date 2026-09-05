# Step 4 — Git-diff / Changed-files Input

**Status:** Confirmed
**Date:** 2026-09-05

An optional input mode: let git decide which files to check, instead of listing paths by hand. Only file *selection* is new — dispatch, ruff invocation, and reporting are the unchanged Step 3 pipeline.

## The two modes
```
python -m qa_agent <path> [<path> ...]     path mode   (Step 3, unchanged)
python -m qa_agent --git-diff              git mode    (uncommitted work)
python -m qa_agent --git-diff <REF>        git mode    (working tree vs a ref)
```
The modes are mutually exclusive: `--git-diff` picks the files itself, so passing paths alongside it is rejected. Passing neither is also rejected. Exit codes are unchanged from Step 3: `0` clean · `1` findings · `2` tool/input failure.

## What "changed" means (the git commands used)
Run from the repository root, resolved via `git rev-parse --show-toplevel`, so the mode works from any subdirectory.

**Default (`--git-diff`, no ref)** — everything not yet committed:
```
git diff --name-only HEAD -z                    staged + unstaged changes
git ls-files --others --exclude-standard -z     untracked files
```
- **`HEAD` covers staged *and* unstaged in one command** — simpler and less surprising than picking only one of them.
- **Untracked files are included.** A `.py` file you just wrote is not in `git diff` at all, yet it is exactly the file most worth checking. Files git ignores are excluded (`--exclude-standard`), so build output and virtualenvs stay out.
- In a repo with no commits yet there is no `HEAD`, so it falls back to `git diff --name-only --cached` plus untracked.

**With a ref (`--git-diff main`)** — `git diff --name-only <ref>`: what the working tree changed relative to that ref.

`-z` is used throughout so paths containing spaces or non-ASCII characters arrive intact rather than git-quoted.

## Deleted files
`git diff` lists deleted files, and handing a deleted path to ruff would fail the whole run with a tool error. They are filtered out before dispatch and reported under Skipped as `deleted in working tree` — visible, not silently dropped.

## Failure cases
| Situation | Behaviour |
|---|---|
| Not inside a git repository | `not inside a git repository - run --git-diff from within a repo, or pass explicit paths instead`, exit 2, zero findings |
| `git` not installed / not on PATH | `'git' is not installed or not on PATH`, exit 2 |
| Invalid ref (or a path passed where a ref goes) | `'some/path' is not a valid git ref in this repository`, exit 2 — the ref is verified up front so raw git usage text never leaks into the report |
| Nothing changed | `Checked: 0 file(s)` / `Findings: none - no files were checked.`, exit 0 |

No failure path ever produces a finding.

## Code changes
`qa_agent/gitdiff.py` is new and is the only module that knows about git. The rest was reused, not rewritten:
- `runner.run()` gained one optional argument, `extra_skipped`, so callers can report files they already know are uncheckable (deleted ones).
- `report.render()` takes a source label instead of assuming a list of paths, and its Skipped header is now reason-agnostic (`Skipped (N) - not checked:`) since reasons now vary.
- `__main__.py` gained the `--git-diff` flag and mode validation.
- `adapters.py` is untouched.

## What success looks like — verified 2026-09-05
Against a scratch git repo (not this one — this project is not git-initialised):

| Check | Result |
|---|---|
| Path mode regression | unchanged: 3 findings on the known-issue file, clean file clean, `.txt` skipped |
| Modified + untracked + staged `.py` | all 4 checked, 3 findings, exit 1 |
| Changed `.txt` | under Skipped, `no tool configured for '.txt'` |
| **Committed file with a real bug, left untouched** | **not reported** — confirmed ruff *would* flag it (`F401`) if asked, proving scope really is limited to changed files |
| Deleted tracked file | under Skipped, `deleted in working tree`, no tool error |
| Nothing changed | `no files were checked`, exit 0 |
| Outside a git repo | clear error, exit 2, no findings |
| `--git-diff HEAD~1` | 5 files checked against the ref, exit 1 |
| Invalid ref | clear error, exit 2 |

Still fully offline: the only subprocesses are local `git` and local `ruff`.
