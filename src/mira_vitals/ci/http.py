"""The one HTTP helper the CI command uses: bounded, quiet, never raising.

Every call has its own timeout *and* respects a whole-report deadline, so
the slowest possible report is bounded no matter how many requests it
makes. A credential is only ever sent over https (or to a loopback host in
tests), and an error message never contains request headers.
"""

from __future__ import annotations

import gzip
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "mira-vitals-ci"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. urllib's default handler follows them and
    copies the Authorization header to the new location -- any host, even
    plain http. Nothing this command calls needs a redirect, so a 3xx is
    returned as the answer instead of being followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


#: The one opener every request goes through (OTLP export and adapters),
#: unless the caller pins a CA (``cafile``): then an opener whose TLS trusts
#: that CA alone, still refusing redirects.
_OPENER = urllib.request.build_opener(_NoRedirect)


def _opener(cafile: str | None) -> urllib.request.OpenerDirector:
    if cafile is None:
        return _OPENER
    # cafile given: ONLY that CA (create_default_context loads no system
    # roots when a cafile is passed), hostname checked, TLS 1.2+.
    context = ssl.create_default_context(cafile=cafile)
    return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))


class Deadline:
    """A wall-clock budget shared by every request in one report."""

    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.end - time.monotonic()

    def timeout(self, cap: float) -> float | None:
        left = self.remaining()
        if left <= 0.5:
            return None
        return min(cap, left)


def _secure(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    deadline: Deadline,
    cap: float = 10.0,
    gzip_body: bool = False,
    cafile: str | None = None,
) -> tuple[int | None, Any, str | None]:
    """``(status, parsed_json_or_None, error)``. Never raises.

    ``status`` is None when no response arrived (timeout, refused, budget
    spent); ``error`` then says why, without echoing headers. ``cafile``
    (https only) verifies the server against that CA alone; an unreadable
    file is an error, never a fallback to the system roots.
    """
    headers = dict(headers or {})
    if any(k.lower() == "authorization" for k in headers) and not _secure(url):
        return None, None, "refused to send a credential over plain http"
    if cafile is not None and urllib.parse.urlparse(url).scheme != "https":
        return None, None, "a CA certificate was given for a non-https endpoint"
    try:
        opener = _opener(cafile)
    except (OSError, ssl.SSLError, ValueError) as exc:
        return None, None, f"CA certificate unusable: {type(exc).__name__}"
    timeout = deadline.timeout(cap)
    if timeout is None:
        return None, None, "time budget spent"
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
        if gzip_body:
            data = gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 - https or loopback only (above)
    try:
        with opener.open(req, timeout=timeout) as response:  # noqa: S310 - https or loopback only
            raw = response.read(32 * 1024 * 1024)
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(500).decode("utf-8", "replace")
        except OSError:
            pass
        return exc.code, None, f"HTTP {exc.code}: {detail[:300]}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, None, f"{type(exc).__name__}: {str(exc)[:200]}"
    if not raw:
        return status, None, None
    try:
        return status, json.loads(raw), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, None, None
