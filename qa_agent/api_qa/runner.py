"""API QA v1's one public entry point (docs/30-api-qa-v1.md;
docs/33-api-qa-deterministic-verification.md): `run_api_qa(context, root,
config=None, on_progress=None)`.

Composition, nothing more: `discovery.py` finds real endpoints, `server.py`
starts (and always stops) a real dev server, `resolution.py` decides which
endpoints are safely testable (resolving a dynamic path parameter or
request body from real prior evidence where it genuinely can) and executes
them via `http_client.call_endpoint`. This module owns none of that logic
itself - only the order to run it in and how to turn what happened into
one honest `ApiTestResult`, the same "one public entry point orchestrates
already-independent pieces" shape `run_runtime_plan` and `discover_project`
already established for their own layers.

Never raises. A missing precondition (no endpoints discovered, no start
command found, the server crashing on startup) is always a normal, honest
result - never an exception, and never silently treated as success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import server as _server
from .discovery import discover_api_endpoints
from .http_client import DEFAULT_TIMEOUT_SECONDS as _DEFAULT_REQUEST_TIMEOUT
from .planning import build_test_plan, execute_test_plan
from .resolution import (
    find_static_openapi_schema,
    generate_and_execute_negative_cases,
    resolve_and_execute,
    validate_response_schemas,
)
from .models import (
    CALL_SKIPPED,
    SERVER_CRASHED,
    SERVER_SKIPPED,
    SERVER_STARTED,
    SERVER_START_FAILED,
    SERVER_UNREACHABLE,
    ApiCallResult,
    ApiTestResult,
)

# The single most common local dev-server port across every framework this
# package discovers a start command for (Next.js's own real default; also
# Express/FastAPI's own frequent choice) - used only when nothing more
# specific was ever observed (`server._observed_base_url` already tries a
# real URL, then a real "port NNNN" log line, before this is ever reached)
# - a documented, honest last-resort guess (see `_resolve_base_url`'s own
# docstring), never presented as anything other than an assumption when it
# is one.
_DEFAULT_FALLBACK_URL = "http://localhost:3000"


@dataclass(frozen=True)
class ApiQaConfig:
    server_startup_timeout: float = 20.0
    request_timeout: float = _DEFAULT_REQUEST_TIMEOUT
    env: Optional[dict] = None
    # A real, live TCP-connect check run after `_resolve_base_url` and
    # before any real endpoint call (docs/39-connect-probe.md) - a matched
    # "ready" log line alone is not trustworthy enough to start making real
    # calls against; see `server.wait_until_connectable`'s own docstring
    # for the real race this closes.
    connect_probe_timeout: float = _server.DEFAULT_CONNECT_PROBE_TIMEOUT
    # A second, still-real readiness check run after the TCP probe succeeds
    # (Phase 1: environment readiness gate) - a real HTTP request, lenient
    # about status code (any real response, even a 404/500, proves the
    # server is genuinely answering); only a true connection-level failure
    # on every candidate path blocks the run, exactly like the TCP gate.
    http_readiness_timeout: float = _server.DEFAULT_HTTP_READINESS_TIMEOUT
    # docs/45-synthetic-mutation-testing.md: whether a mutating call or a
    # dynamic path parameter may fall back to a clearly-labeled synthetic
    # value (see resolution.py's own `DEFAULT_ALLOW_SYNTHETIC_MUTATIONS`)
    # when no real evidence/schema default exists, rather than honestly
    # skipping. `True` by default so POST/PUT/PATCH/DELETE testing actually
    # exercises the target out of the box; set `False` to reproduce the
    # original strict evidence-only behavior exactly.
    allow_synthetic_mutations: bool = True


DEFAULT_CONFIG = ApiQaConfig()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _skip_all(endpoints, reason):
    return tuple(
        ApiCallResult(endpoint=e, status=CALL_SKIPPED, reason=reason) for e in endpoints
    )


def _combine_progress(on_progress):
    """Four phase-scoped callbacks (`resolve_and_execute`'s primary calls,
    negative cases, schema validation, then Phase 3's own functional test
    plan) combined into one overall `(done, total, label)` stream for a
    single caller-facing `on_progress` (docs/44-live-progress.md). Each
    phase's own `total` only becomes known to the caller once that phase
    actually starts reporting - so the overall `total` only ever grows, in
    step with real, newly-known work, never shrinks and never estimates
    ahead of what is actually known. `(None, None, None, None)` when
    `on_progress` itself is `None` - every phase then gets a real `None`
    too, its own already-documented no-op default.
    """
    if on_progress is None:
        return None, None, None, None

    state = {"primary_total": 0, "primary_done": 0,
              "negative_total": 0, "negative_done": 0,
              "schema_total": 0, "schema_done": 0,
              "plan_total": 0, "plan_done": 0}

    def _emit(label):
        done = state["primary_done"] + state["negative_done"] + state["schema_done"] + state["plan_done"]
        total = state["primary_total"] + state["negative_total"] + state["schema_total"] + state["plan_total"]
        on_progress(done, total, label)

    def primary_cb(done, total, label):
        state["primary_total"], state["primary_done"] = total, done
        _emit(label)

    def negative_cb(done, total, label):
        state["negative_total"], state["negative_done"] = total, done
        _emit(label)

    def schema_cb(done, total, label):
        state["schema_total"], state["schema_done"] = total, done
        _emit(label)

    def plan_cb(done, total, label):
        state["plan_total"], state["plan_done"] = total, done
        _emit(label)

    return primary_cb, negative_cb, schema_cb, plan_cb


def _resolve_base_url(handle, warnings):
    """Prefer a real URL, or a real "port NNNN" log line, actually observed
    in the server's own startup output (`server._observed_base_url`); only
    fall back to the single most common default port when neither was ever
    observed, and say so explicitly in `warnings` - never silently assume.

    Returns `(base_url, was_guessed)` - `was_guessed` (docs/46) lets a
    later, real connection failure against this URL say plainly whether it
    was really observed evidence or an unconfirmed guess, rather than
    leaving that distinction buried in an earlier, separate warning.
    """
    if handle.base_url:
        return handle.base_url, False
    warnings.append(
        "server startup logs did not include a recognizable URL or 'port <number>' line; "
        "assuming the common default {}".format(_DEFAULT_FALLBACK_URL)
    )
    return _DEFAULT_FALLBACK_URL, True


def run_api_qa(context, root, config=None, on_progress=None):
    config = config or DEFAULT_CONFIG
    root = Path(root)
    started_at = _now()
    started_perf = time.perf_counter()

    endpoints, warnings = discover_api_endpoints(context, root)
    warnings = list(warnings)

    def _finish(**fields):
        return ApiTestResult(
            root_path=str(root), warnings=tuple(warnings),
            started_at=started_at, finished_at=_now(),
            total_duration=time.perf_counter() - started_perf,
            **fields,
        )

    if not endpoints:
        return _finish(
            endpoints=(), calls=(), server_status=SERVER_SKIPPED,
            server_detail="no Next.js App Router API route handler was discovered under this project",
        )

    command, evidence, server_cwd = _server.discover_server_start_command(root, context.project)
    if command is None:
        return _finish(
            endpoints=endpoints,
            calls=_skip_all(endpoints, "server was not started: {}".format(evidence)),
            server_status=SERVER_SKIPPED, server_detail=evidence,
        )

    handle = _server.start_and_wait_ready(
        command, server_cwd, config.server_startup_timeout, env=config.env,
    )
    try:
        if handle.status == _server.STATUS_NOT_FOUND:
            return _finish(
                endpoints=endpoints,
                calls=_skip_all(endpoints, "server was not started: {}".format(handle.reason)),
                server_status=SERVER_START_FAILED, server_detail=handle.reason,
            )
        if handle.status == _server.STATUS_CRASHED:
            # The real reason a process "exited with code N" almost always
            # lives in what it printed before dying (a missing module, a
            # syntax error, an unhandled startup exception) - already
            # captured in `handle.logs` the whole time, just never
            # surfaced until now (the same "why did it fail" gap docs/37
            # already closed for a failing call against a server that DID
            # start; this closes the equivalent gap for one that never did).
            return _finish(
                endpoints=endpoints,
                calls=_skip_all(endpoints, "server crashed on startup: {}".format(handle.reason)),
                server_status=SERVER_CRASHED, server_detail=handle.reason,
                server_log_tail="".join(handle.logs)[-_server.MAX_LOG_TAIL_CHARS:],
            )

        base_url, base_url_was_guessed = _resolve_base_url(handle, warnings)
        if not _server.wait_until_connectable(base_url, timeout=config.connect_probe_timeout):
            # docs/48-fail-fast-and-classification.md: stop the whole run
            # here, honestly, rather than proceeding to call every endpoint
            # against a server that was never confirmed reachable (the
            # earlier docs/39/docs/46 behavior - warn, then proceed anyway -
            # is deliberately replaced: it produced a wall of real but
            # uninformative "connection refused" failures that read exactly
            # like a broken API, when the real problem was upstream, in the
            # environment itself). State plainly, from the real facts
            # already known, both *what* the server actually did (matched a
            # real ready-shaped log line vs. merely stayed alive with none
            # recognized) and *whether the URL being probed was real
            # evidence or a guess* - never a fixed sentence regardless of
            # either fact.
            signal_desc = (
                "matched a ready signal" if handle.status == _server.STATUS_READY
                else "stayed running without a recognized ready signal"
            )
            url_desc = (
                "an unconfirmed guess - no real port was ever observed in its startup output"
                if base_url_was_guessed else "a real port actually observed in its startup output"
            )
            reason = (
                "Server is not reachable. Stopping the run. The process {} but never accepted "
                "a real TCP connection on {} ({}) within {:.0f}s. Command: {} (cwd: {}, "
                "startup timeout: {:.0f}s).".format(
                    signal_desc, base_url, url_desc, config.connect_probe_timeout,
                    " ".join(command), server_cwd, config.server_startup_timeout)
            )
            return _finish(
                endpoints=endpoints,
                calls=_skip_all(endpoints, reason),
                server_status=SERVER_UNREACHABLE, server_detail=reason, base_url=base_url,
                server_log_tail=_server.drain_log_tail(handle),
            )

        readiness = _server.check_http_readiness(
            base_url, endpoints, timeout=config.http_readiness_timeout,
        )
        if not readiness.reached:
            # TCP itself connected (the gate above already passed) but
            # nothing ever answered as HTTP - a real, distinct failure from
            # both "unreachable at the TCP level" and "an endpoint returned
            # an error", never conflated with either (Phase 1, section 6).
            reason = (
                "Server is not reachable. Stopping the run. {} (base URL: {}). "
                "Command: {} (cwd: {}).".format(
                    readiness.detail, base_url, " ".join(command), server_cwd)
            )
            return _finish(
                endpoints=endpoints,
                calls=_skip_all(endpoints, reason),
                server_status=SERVER_UNREACHABLE, server_detail=reason, base_url=base_url,
                server_log_tail=_server.drain_log_tail(handle),
                http_readiness_detail=readiness.detail,
            )

        # Phase 2 (API contract understanding, docs/53): a real, already-
        # checked-out OpenAPI/Swagger file, when the project ships one -
        # found once here (root is only available at this layer), passed
        # down as a fallback only ever used once the live `/openapi.json`
        # fetch itself comes back empty (see resolution.py's own `_schema()`
        # docstring for the exact precedence).
        static_schema_doc = find_static_openapi_schema(root)

        primary_cb, negative_cb, schema_cb, plan_cb = _combine_progress(on_progress)
        calls = resolve_and_execute(
            endpoints, base_url, config.request_timeout, on_progress=primary_cb,
            allow_synthetic_mutations=config.allow_synthetic_mutations,
            static_schema_doc=static_schema_doc,
        )
        negative_calls = generate_and_execute_negative_cases(
            endpoints, calls, base_url, config.request_timeout, on_progress=negative_cb,
            allow_synthetic_mutations=config.allow_synthetic_mutations,
            static_schema_doc=static_schema_doc,
        )
        schema_validations = validate_response_schemas(
            endpoints, calls, base_url, config.request_timeout, on_progress=schema_cb,
            static_schema_doc=static_schema_doc,
        )
        # Phase 3 (functional API test planning, docs/54): a second,
        # additive, dependency-aware pass - reuses the same real base_url/
        # OpenAPI evidence the verification pass above already established,
        # never a parallel HTTP mechanism. Deliberately run after (not
        # instead of) `resolve_and_execute` above: that pass remains the
        # existing, unchanged, per-endpoint verification contract every
        # prior phase's report/render/regression tests already rely on;
        # this one is a distinct, workflow-shaped view of the same real
        # server, allowed to call an endpoint more than once when a real
        # workflow (create -> ... -> verify deletion) genuinely requires it.
        test_plan = build_test_plan(endpoints)
        functional_results = execute_test_plan(
            test_plan, base_url, config.request_timeout,
            static_schema_doc=static_schema_doc, on_progress=plan_cb,
            allow_synthetic_mutations=config.allow_synthetic_mutations,
        )
        return _finish(
            endpoints=endpoints, calls=calls, server_status=SERVER_STARTED,
            server_detail=handle.reason, base_url=base_url,
            server_log_tail=_server.drain_log_tail(handle),
            negative_calls=negative_calls, schema_validations=schema_validations,
            http_readiness_detail=readiness.detail,
            test_plan=test_plan, functional_results=functional_results,
        )
    finally:
        handle.stop()
