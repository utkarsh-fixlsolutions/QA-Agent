# Step 39 - real connect probe before the first API QA call

## The bug, found dogfooding through the new web frontend

A real Express project, tested through `web/`'s new folder-upload flow:

```
Server: started (http://localhost:3000) - 2 call(s)
POST /create-post   SKIPPED   no OpenAPI schema is available to construct a request body
GET  /posts         FAIL      could not reach 'http://localhost:3000/posts':
                               [WinError 10061] connection refused
```

The `SKIPPED` result is correct, working-as-designed behavior (docs/33 -
Express has no OpenAPI schema, so no body can be safely constructed). The
`FAIL` was not: `server_status` said `started`, a real base URL was
observed, and the very first real call still got an OS-level connection
refusal - not a 404, not a 500, nothing was listening at all.

## Root cause

`start_and_wait_ready` (`server.py`) decides "the server is ready" purely
by matching text in the process's own stdout against `DEFAULT_READY_
PATTERNS` (`"ready"`, `"listening"`, `"watching for file changes"`, ...).
That only proves the process printed something ready-shaped - never that a
listener socket is actually bound and accepting connections yet. This is
the same class of race already documented in docs/step-log.md's earlier
sessions (a cold-compiling Next.js dev server), now reproduced with a
second, different framework: a nodemon-wrapped Express server, where the
wrapper process prints its own status text before the real child process
it spawns has finished binding the port.

## The fix

`server.py`: `wait_until_connectable(base_url, timeout, poll_interval)` - a
real, live TCP-connect poll (`socket.create_connection`), never inferred
from log text. Returns `True` the moment a real connect succeeds, `False`
once `timeout` elapses without one; never blocks past `timeout`;
deliberately only a TCP connect, not a real HTTP request, so the probe
itself is never counted as one of the real calls `resolve_and_execute`
goes on to report.

`runner.py`: `run_api_qa` calls it once, right after `_resolve_base_url`
and before any real endpoint call, via a new `ApiQaConfig.connect_probe_
timeout` field (default `10.0s`, `server.DEFAULT_CONNECT_PROBE_TIMEOUT`).
When the probe never succeeds in time, a real, honest warning is added
(`"server matched a ready signal but never accepted a real TCP connection
on <url> within <N>s - the calls below may still fail for that reason"`) -
the run is never blocked or failed outright by this alone; the real,
authoritative pass/fail decision still belongs entirely to each actual
call in `http_client.py`, unchanged.

## Verified

Reproduced the exact race directly (a server that prints its ready line
*immediately* but only actually binds the port ~1.2s later): before this
fix, the real call got a connection-refused; after it, `wait_until_
connectable` notices the real bind within its own poll loop and the same
call now genuinely passes. Also verified the honest-warning path (a server
that prints ready but never binds anything at all) - bounded wait, no
hang, no fabricated pass, a clear warning naming the real gap.

`test_api_qa.py`: 4 new tests (`wait_until_connectable` unit tests against
a real socket bound after a real delay / never bound at all; two full
`run_api_qa` end-to-end reproductions of the exact bug and its fix) -
143/143 total, all pre-existing checks unchanged.

## Scope

This only adds a bounded wait + an honest warning - it does not change
`CALL_PASS`/`CALL_FAIL` semantics, discovery, or resolution at all. A
project whose dev server takes longer than both `server_startup_timeout`
*and* `connect_probe_timeout` to actually bind its port will still see
real, honest connection failures - now at least explained by a warning
naming the real reason, rather than silently attributed to nothing.
