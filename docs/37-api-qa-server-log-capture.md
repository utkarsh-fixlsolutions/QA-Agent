# Step 37 - API QA server log capture ("why did it fail")

## The gap this closes

`http_client.py` already reports everything a real HTTP response can carry:
status code, timing, a body sample. For a connection failure, that's the
whole story. But for an application-level error - an unhandled exception
in a route handler - the *why* often never makes it into the HTTP response
at all (a dev server can return a bare 500 with an empty body, e.g. Next.js
dev cutting the connection on an uncaught exception) while the real
explanation - the actual stack trace - is sitting in the dev server's own
console output the whole time, unused.

Confirmed against a real project (`D:\Major projects\lms-ai`,
`/api/sentry-example-api`, a route that deliberately
`throw new SentryExampleAPIError(...)`): the HTTP response was a bare
`500` with no usable body, while the dev server's own stdout printed the
real stack trace at the moment of the call.

## What changed

`server.py`:
- `ServerHandle` now keeps a reference to the same live `queue.Queue`
  `_reader_thread` appends real stdout/stderr lines to for as long as the
  process stays alive (previously discarded once `start_and_wait_ready`
  returned - the reader thread kept running, but nothing read from its
  queue afterward).
- `drain_log_tail(handle, max_chars=MAX_LOG_TAIL_CHARS)`: drains whatever
  is currently queued (non-blocking - never waits for more output),
  bounded to the last `MAX_LOG_TAIL_CHARS` (4000) characters. `""` when the
  server was never started or printed nothing new.

`runner.py`: after `resolve_and_execute` finishes making every real call
(and before `handle.stop()` in `run_api_qa`'s own `finally`), drains
whatever the server printed during that whole window into the new
`ApiTestResult.server_log_tail` field.

`render.py`: the terminal report shows a "Server log (during this run)"
section (last 40 lines) whenever `server_log_tail` is non-empty *and* at
least one call did not pass - never shown for an all-passing run, where it
would just be noise. `to_dict`/`to_json` expose the full (still bounded)
tail unconditionally. `to_html` (docs/36) renders it as a `<pre>` block
when present.

## Scope and honesty boundary

- This is real, already-happening dev-server console output, captured
  exactly as printed - never parsed, summarized, or matched to a specific
  call. For a single failing call (the common case) it's unambiguous; for
  several concurrent-ish failures in one run, the tail is the *whole
  window's* output, not per-call-attributed - correlating individual lines
  to individual calls would require timestamp-matching against inherently
  unstructured, framework-specific log text, which this step deliberately
  does not attempt (a real scope boundary, not a silent gap).
- `CALL_PASS`/`CALL_FAIL` semantics (`http_client.py`) are completely
  unchanged - this only adds context to an already-decided outcome, never
  influences the pass/fail decision itself.
- CSV export (docs/36) does not include the log tail - it isn't a
  per-row/per-call fact, and stuffing a multi-line stack trace into one CSV
  cell would break the "one clean row per call" shape a spreadsheet expects.
  The HTML report and terminal output are where it belongs.
