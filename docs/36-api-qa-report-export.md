# Step 36 - API QA report export (CSV / HTML)

## What this adds

Two new pure-presentation functions in `qa_agent/api_qa/render.py`:

- `to_csv(result)` - one row per call, plain CSV (`method, path, status,
  status_code, response_time_ms, reason, error, response_sample,
  resolved_path, resolution_evidence, source_file`).
- `to_html(result)` - a single, self-contained HTML file (inline CSS only,
  no external assets or network fetches) with one color-coded row per
  endpoint: method, path, a colored result badge, the real HTTP status
  code, timing, and the real failure detail (reason/error, plus a short
  response-body sample when one exists).

Both are pure functions over an already-finished `ApiTestResult` - neither
makes a network call or re-runs anything, the same "only ever formats a
finished result" role `render()`/`to_dict()`/`to_json()` already have.

## Why a report file at all

`render()`'s terminal output already shows every call's real outcome, but
it's ephemeral - scrolled past, not something you can hand to someone else
or import into a spreadsheet/ticket. `to_csv`/`to_html` reuse the exact same
already-captured evidence (`ApiCallResult`'s own fields) and reshape it into
something durable.

## Color convention (HTML)

A row's color bucket is decided by the most specific real fact available -
never guessed, never computed from anything but a real, already-observed
field:

| Bucket        | When                                          | Color  |
|---------------|------------------------------------------------|--------|
| `2xx`         | a real 2xx status code was received             | green  |
| `3xx`         | a real 3xx status code was received             | blue   |
| `4xx`         | a real 4xx status code was received             | amber  |
| `5xx`         | a real 5xx status code was received             | red    |
| `NO RESPONSE` | no HTTP response was ever received at all (connection refused, timeout) | dark red |
| `SKIPPED`     | the call was never attempted (dynamic route with no resolvable evidence, server never started, etc.) | gray |

`SKIPPED` always wins over any stale `status_code` - a skipped call's
`status_code` is always `None` by construction (see `models.py`), so this
never actually collides, but the check is explicit rather than assumed.

`NO RESPONSE` is deliberately distinct from `5xx`: they mean different
things operationally (the target process is down/unreachable vs. the
target process is up and actively returning an error), and collapsing them
into one color would hide that distinction.

## CLI

```
qa_agent discover <path> --api-report <PATH>
```

Format is chosen by `<PATH>`'s extension: `.csv` for the plain-text export,
anything else (documented as `.html`) for the color-coded HTML report.
`--api-report` implies `--api-test` (consistent with `--api-diagnose`/
`--api-repair`'s own existing "implies" chain) - you never need both flags
together. Terminal output (`render()`'s own plain-text report) is
unchanged; the file is written in addition, the same "also write to PATH,
terminal output unchanged" contract the top-level `-o`/`--output` flag
already established for the static-check report (`docs/05-report-file.md`).

A write failure (bad path, no permission) is reported to stderr via the
existing `render_write_error` helper and never crashes the run - the same
handling `-o` already has.

## Scope boundary

This step only adds presentation. It does not change what gets discovered,
called, or how pass/fail is decided - `discovery.py`/`resolution.py`/
`http_client.py`/`runner.py` are untouched. See `docs/37-api-qa-server-log
-capture.md` for the companion step that makes *why* a failure happened
more informative by capturing real dev-server log output during the run.
