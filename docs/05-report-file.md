# Step 5 — Save Report to a File

**Status:** Confirmed
**Date:** 2026-09-05

An optional `--output` flag writes the same run to a Markdown file. Terminal output is untouched.

## Usage
```
python -m qa_agent src/ --output report.md
python -m qa_agent --git-diff --output report.md
python -m qa_agent --git-diff main -o report.md
```
`-o` is a short alias. Without the flag, behaviour is byte-for-byte what Step 4 did.

## Format choice: Markdown only
Markdown is the single v1 format. **JSON was deliberately not built** — nothing consumes it yet, and adding it now would mean maintaining a second format with no reader. It stays a small, later addition if a real need appears: `render_json()` beside `render_markdown()`, over the same data.

That "same data" is the point: `render()` (terminal) and `render_markdown()` (file) are two presentations of one `RunResult`. There is no second report pipeline — finding logic, git-diff logic, and adapters are untouched by this step.

The Markdown report contains the same substance as the terminal one:
- Summary — run time, input, files checked, tools used
- Findings as a table — file, line, severity, message, tool
- Skipped files with reasons
- `No issues found.` on a clean run, `None - no files were checked.` when nothing was checked
- Not-found paths, when any

A literal `|` in a message is escaped (`\|`) so it cannot break the table.

## Example — clean run
```markdown
# QA Agent Report

- **Run at:** 2026-09-05 11:52:38
- **Input:** ...\clean.py
- **Checked:** 1 file(s)
- **Tools:** ruff

## Findings

No issues found.
```

## Example — findings
```markdown
## Findings (3)

| File | Line | Severity | Message | Tool |
| --- | ---: | --- | --- | --- |
| `...\has_issues.py` | 1 | error | F401: `os` imported but unused | ruff |
| `...\has_issues.py` | 5 | error | E711: Comparison to `None` should be `cond is None` | ruff |
| `...\has_issues.py` | 7 | error | F841: Local variable `unused` is assigned to but never used | ruff |

## Skipped (1) - not checked

| File | Reason |
| --- | --- |
| `...\notes.txt` | no tool configured for '.txt' |
```

## When the file cannot be written
The report is printed to the terminal **first**, then written. So a failed write never costs you the results:

```
QA Agent: report ran, but could not write it to 'out\nope\report.md': [Errno 2] No such file or directory: ...
```
The message goes to stderr and the exit code is **2**, which takes precedence over `1` — the requested output was not produced, so the run should not look successful to a script. Parent directories are **not** created automatically; a missing directory is reported rather than silently made.

Files are written as UTF-8.

## Code changes
- `report.py`: added `render_markdown()`, `render_write_error()`, and a `_cell()` escape helper. Existing `render()` untouched.
- `__main__.py`: added `-o/--output`, and the write step after printing.
- `runner.py`, `adapters.py`, `gitdiff.py`: **unchanged**.

## What success looks like — verified 2026-09-05
| Check | Result |
|---|---|
| No `--output` | behaviour identical to Step 4 |
| Path mode + `--output` | file written, 4 findings + 1 skipped, exit 1 |
| `--git-diff` + `--output` | file written, correct git input label, exit 1 |
| Clean run + `--output` | file says `No issues found.`, exit 0 |
| **Terminal vs file parity** | findings and skipped entries parsed from both and compared as sets — **identical** |
| Output into a missing directory | clear error, exit 2 |
| Output path is a directory | clear error, exit 2 |
| Findings + unwritable path | findings still printed to stdout, error to stderr, exit 2 |
| `\|` inside a message | escaped; table row stays a valid 5-column row |

Still fully offline, still no dependencies beyond local `ruff` (and `git` for `--git-diff`).
