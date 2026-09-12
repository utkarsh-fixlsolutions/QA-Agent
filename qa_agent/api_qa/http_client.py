"""The one real HTTP call this capability makes (docs/30-api-qa-v1.md):
`call_endpoint(base_url, endpoint, timeout)`.

Stdlib only (`urllib.request`) - the same call this project's own
`qa_agent/ai/ollama.py` already made for talking to a local Ollama server:
nothing here needs more than urlopen + a status code + a bounded body read,
so the smaller footprint wins over adding `requests`/`httpx` as a new
dependency (docs/step-log.md, Dependency philosophy).

Every failure mode - connection refused, DNS failure, timeout, a non-2xx
response, a malformed JSON body - is caught here and returned as a
structured `ApiCallResult`, never raised.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from .models import CALL_FAIL, CALL_PASS, ApiCallResult

DEFAULT_TIMEOUT_SECONDS = 10.0

# A real response body is only ever sampled up to this many characters -
# the same "cap the displayed sample, never the underlying check" pattern
# project/models.py's own `MAX_EVIDENCE_PER_ITEM` already established.
MAX_RESPONSE_SAMPLE_CHARS = 500

# The raw body is never read past this many bytes, regardless of what the
# server claims via Content-Length - protects this process against a
# server that (accidentally or not) streams an unbounded response.
MAX_RESPONSE_READ_BYTES = 65_536

_TIMEOUT_EXCEPTIONS = (socket.timeout, TimeoutError)


def _is_json_content_type(content_type):
    return "application/json" in content_type.lower() or "+json" in content_type.lower()


def _decode_capped(raw_bytes):
    text = raw_bytes.decode("utf-8", errors="replace")
    truncated = len(raw_bytes) >= MAX_RESPONSE_READ_BYTES
    return text, truncated


def _validate_json_if_applicable(content_type, text, truncated):
    """Returns `valid_response` (`None`/`True`/`False`). A truncated body
    is never claimed valid or invalid - there is not enough of it to
    honestly judge either way.
    """
    if not _is_json_content_type(content_type):
        return None
    if truncated:
        return None
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        return False


def _result(endpoint, status, **kwargs):
    return ApiCallResult(endpoint=endpoint, status=status, **kwargs)


def call_endpoint(base_url, endpoint, timeout=DEFAULT_TIMEOUT_SECONDS):
    """One real HTTP call. Pass/fail rule (deliberately simple and
    deterministic, per docs/30): a 2xx status code, and - only when the
    response declares a JSON content-type - a body that actually parses as
    JSON. Any other outcome (non-2xx, invalid declared-JSON body,
    connection failure, timeout) is a fail, with the real evidence
    (status code, sample, or error) always attached.
    """
    url = base_url.rstrip("/") + endpoint.path
    request = urllib.request.Request(url, method=endpoint.method)
    start = time.perf_counter()

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_READ_BYTES)
            status_code = response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        # A real, received response with a non-2xx status - not a
        # connection failure. exc itself is a valid file-like object.
        raw = exc.read(MAX_RESPONSE_READ_BYTES)
        status_code = exc.code
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        elapsed_ms = (time.perf_counter() - start) * 1000
        text, truncated = _decode_capped(raw)
        valid = _validate_json_if_applicable(content_type, text, truncated)
        return _result(
            endpoint, CALL_FAIL, status_code=status_code, response_time_ms=elapsed_ms,
            content_type=content_type, valid_response=valid,
            response_sample=text[:MAX_RESPONSE_SAMPLE_CHARS],
            reason="HTTP {} response".format(status_code),
        )
    except _TIMEOUT_EXCEPTIONS:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return _result(
            endpoint, CALL_FAIL, response_time_ms=elapsed_ms,
            error="'{}' did not respond within {:.0f}s".format(url, timeout),
        )
    except urllib.error.URLError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        reason = exc.reason
        if isinstance(reason, _TIMEOUT_EXCEPTIONS):
            error = "'{}' did not respond within {:.0f}s".format(url, timeout)
        else:
            error = "could not reach '{}': {}".format(url, reason)
        return _result(endpoint, CALL_FAIL, response_time_ms=elapsed_ms, error=error)
    except Exception as exc:  # noqa: BLE001 - a bad call must never crash the session
        elapsed_ms = (time.perf_counter() - start) * 1000
        return _result(
            endpoint, CALL_FAIL, response_time_ms=elapsed_ms,
            error="unexpected error calling '{}': {}".format(url, exc),
        )

    elapsed_ms = (time.perf_counter() - start) * 1000
    text, truncated = _decode_capped(raw)
    valid = _validate_json_if_applicable(content_type, text, truncated)
    is_2xx = 200 <= status_code < 300
    success = is_2xx and valid is not False

    if success:
        reason = "HTTP {}".format(status_code)
    elif not is_2xx:
        reason = "HTTP {} response".format(status_code)
    else:
        reason = "HTTP {} but response body is not valid JSON despite a JSON content-type".format(status_code)

    return _result(
        endpoint, CALL_PASS if success else CALL_FAIL,
        status_code=status_code, response_time_ms=elapsed_ms,
        content_type=content_type, valid_response=valid,
        response_sample=text[:MAX_RESPONSE_SAMPLE_CHARS], reason=reason,
    )
