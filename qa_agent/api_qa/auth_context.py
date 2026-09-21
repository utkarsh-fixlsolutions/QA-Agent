"""General, framework-neutral auth/session propagation: capturing a real
credential (a bearer token found in a real, successful response, or a real
`Set-Cookie` header) from any call made during a run, and attaching it to
every subsequent real call - the same "evidence discovered by one call
flows into a later one" pattern already established for path-parameter id
resolution (`resolution.py`'s own `_Evidence`) and Phase 3's create-then-
use-the-real-id chaining (`planning.py`), applied here to authentication.

Never targets a specific endpoint name or path ("/login", "/auth/...") -
any endpoint's real, successful response can carry a real credential, and
every later real call benefits from it once found, regardless of which
endpoint produced it. Never invents a credential - only ever a value
actually observed in a real response.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

from .models import CALL_PASS

# A bounded, named vocabulary of real, common token field names - the same
# "narrow, recognizable convention" discipline every other evidence-
# extraction function in this package already follows, never a guess at an
# arbitrary field name.
_TOKEN_FIELD_NAMES = (
    "token", "accessToken", "access_token", "authToken", "auth_token",
    "jwt", "idToken", "id_token", "sessionToken", "session_token",
    "apiKey", "api_key",
)

_MIN_TOKEN_LENGTH = 8


def _plausible_token(value) -> bool:
    return isinstance(value, str) and len(value) >= _MIN_TOKEN_LENGTH


def find_auth_token(response_json) -> Optional[str]:
    """The first real, plausible token-shaped value in `response_json` -
    checked at the top level first (the common flat shape), then one level
    of nesting into every dict-valued field (a `result`/`data`/`user`
    wrapper - idurar-erp-crm's own real login response nests its token at
    `result.token`, a common enough shape to check generally, not as a
    special case for that one project). `None` when nothing plausible is
    found - never a guess at an unrelated string field.
    """
    if not isinstance(response_json, dict):
        return None
    for name in _TOKEN_FIELD_NAMES:
        if _plausible_token(response_json.get(name)):
            return response_json[name]
    for value in response_json.values():
        if isinstance(value, dict):
            for name in _TOKEN_FIELD_NAMES:
                if _plausible_token(value.get(name)):
                    return value[name]
    return None


_COOKIE_NAME_VALUE_RE = re.compile(r"^\s*([^=;\s]+)\s*=\s*([^;]*)")


class AuthContext:
    """One real, shared credential state for one run - built once by
    `runner.py` and passed to every real-call-making function
    (`resolve_and_execute`, `generate_and_execute_negative_cases`,
    `planning.execute_test_plan`) so a credential captured by one is
    available to the others, in the same real order this run's calls
    already happen in.
    """

    def __init__(self):
        self.token: Optional[str] = None
        self.token_source: str = ""
        self.cookies: Dict[str, str] = {}

    def headers(self) -> Dict[str, str]:
        """The real headers to attach to the *next* call - `{}` until a
        real credential has actually been captured (never a placeholder
        Authorization header sent speculatively).
        """
        headers: Dict[str, str] = {}
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        if self.cookies:
            headers["Cookie"] = "; ".join(
                "{}={}".format(name, value) for name, value in self.cookies.items()
            )
        return headers

    def evidence_for_attached_call(self) -> str:
        """A short, human-readable trace of *why* a call carried these
        headers - `""` when nothing was attached, matching the rest of
        this package's own "evidence text is empty when there is none"
        convention (`resolution_evidence`).
        """
        parts = []
        if self.token:
            parts.append("Authorization header from a token captured from {}'s response".format(
                self.token_source or "an earlier call"))
        if self.cookies:
            parts.append("session cookie(s) captured from an earlier response")
        return "; ".join(parts)

    def observe(self, endpoint, result) -> None:
        """Updates this context from one real call's own real result -
        never from a skipped/failed call (a rejection proves nothing about
        what a real credential looks like). The first real token found in
        this whole run is kept for the rest of it - never replaced by a
        later one, so a run stays internally consistent about which
        identity it is acting as.
        """
        if result.status != CALL_PASS:
            return
        if self.token is None:
            token = find_auth_token(result.response_json)
            if token is not None:
                self.token = token
                self.token_source = "{} {}".format(endpoint.method, endpoint.path)
        for raw_cookie in getattr(result, "response_cookies", ()):
            match = _COOKIE_NAME_VALUE_RE.match(raw_cookie)
            if match:
                self.cookies[match.group(1)] = match.group(2)
