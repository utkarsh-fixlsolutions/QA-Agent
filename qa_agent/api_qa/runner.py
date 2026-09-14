"""API QA v1's one public entry point (docs/30-api-qa-v1.md;
docs/33-api-qa-deterministic-verification.md): `run_api_qa(context, root,
config=None)`.

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
from .resolution import resolve_and_execute
from .models import (
    CALL_SKIPPED,
    SERVER_CRASHED,
    SERVER_SKIPPED,
    SERVER_STARTED,
    SERVER_START_FAILED,
    ApiCallResult,
    ApiTestResult,
)

# Next.js's own real default when no port is otherwise observed in the
# server's startup logs - a documented, honest fallback (see
# `_resolve_base_url`'s own docstring), never presented as anything other
# than an assumption when it is one.
_DEFAULT_NEXTJS_URL = "http://localhost:3000"


@dataclass(frozen=True)
class ApiQaConfig:
    server_startup_timeout: float = 20.0
    request_timeout: float = _DEFAULT_REQUEST_TIMEOUT
    env: Optional[dict] = None


DEFAULT_CONFIG = ApiQaConfig()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _skip_all(endpoints, reason):
    return tuple(
        ApiCallResult(endpoint=e, status=CALL_SKIPPED, reason=reason) for e in endpoints
    )


def _resolve_base_url(handle, warnings):
    """Prefer a real URL observed in the server's own startup output; only
    fall back to Next.js's documented default port when nothing was
    observed, and say so explicitly in `warnings` - never silently assume.
    """
    if handle.base_url:
        return handle.base_url
    warnings.append(
        "server startup logs did not include a recognizable 'http://localhost:<port>' line; "
        "assuming Next.js's default {}".format(_DEFAULT_NEXTJS_URL)
    )
    return _DEFAULT_NEXTJS_URL


def run_api_qa(context, root, config=None):
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

    command, evidence = _server.discover_server_start_command(root, context.project)
    if command is None:
        return _finish(
            endpoints=endpoints,
            calls=_skip_all(endpoints, "server was not started: {}".format(evidence)),
            server_status=SERVER_SKIPPED, server_detail=evidence,
        )

    handle = _server.start_and_wait_ready(
        command, root, config.server_startup_timeout, env=config.env,
    )
    try:
        if handle.status == _server.STATUS_NOT_FOUND:
            return _finish(
                endpoints=endpoints,
                calls=_skip_all(endpoints, "server was not started: {}".format(handle.reason)),
                server_status=SERVER_START_FAILED, server_detail=handle.reason,
            )
        if handle.status == _server.STATUS_CRASHED:
            return _finish(
                endpoints=endpoints,
                calls=_skip_all(endpoints, "server crashed on startup: {}".format(handle.reason)),
                server_status=SERVER_CRASHED, server_detail=handle.reason,
            )

        base_url = _resolve_base_url(handle, warnings)
        calls = resolve_and_execute(endpoints, base_url, config.request_timeout)
        return _finish(
            endpoints=endpoints, calls=calls, server_status=SERVER_STARTED,
            server_detail=handle.reason, base_url=base_url,
        )
    finally:
        handle.stop()
