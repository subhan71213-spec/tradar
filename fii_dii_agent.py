"""Minimal HTTP client for NSE's public JSON endpoints.

Built on the standard library (urllib) so Phase 2 adds zero third-party
dependencies, matching the Phase 1 persistence layer's approach.

NSE's website requires a browser-like User-Agent and a session cookie
obtained from the homepage before its /api/* endpoints will respond (a
bare request to the API returns 401/403). This client performs that
handshake once per instance and reuses the cookie jar for subsequent
calls.

This client raises plain OSError/TimeoutError on network failure (which
`retry_on_network_failure` catches and retries) and json.JSONDecodeError
on malformed responses (which is NOT retried -- bad data is bad data,
retrying a broken payload won't fix it).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

_BASE_URL = "https://www.nseindia.com"
_HOMEPAGE_URL = f"{_BASE_URL}/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": _BASE_URL,
}


class NseHttpClient:
    """Fetches JSON from NSE's public API, handling the session-cookie
    handshake NSE requires before it will serve /api/* requests."""

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds
        self._cookie_jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self._session_primed = False

    def _prime_session(self) -> None:
        """Hit the homepage once to obtain NSE's anti-bot session cookies."""
        request = urllib.request.Request(_HOMEPAGE_URL, headers=_HEADERS)
        with self._opener.open(request, timeout=self._timeout):
            pass
        self._session_primed = True

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET a JSON endpoint under nseindia.com and return the parsed body.

        `path` should start with '/', e.g. '/api/quote-equity'.
        Raises OSError/TimeoutError (network) or json.JSONDecodeError (bad
        payload) -- callers/adapters translate these into domain exceptions.
        """
        if not self._session_primed:
            self._prime_session()

        url = f"{_BASE_URL}{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"

        request = urllib.request.Request(url, headers=_HEADERS)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            # 401/403 usually means the session cookie expired; re-prime once.
            if exc.code in (401, 403) and self._session_primed:
                self._session_primed = False
                return self.get_json(path, params)
            raise OSError(f"NSE endpoint {path} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"NSE endpoint {path} unreachable: {exc.reason}") from exc

        return json.loads(body)
