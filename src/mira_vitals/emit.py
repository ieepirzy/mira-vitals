"""Direct HTTP emission of the canonical snapshot.

Kept alongside the OTLP path rather than replaced by it.  The Collector is the
preferred route for observability signals, but the snapshot itself is a
document, and posting the document to a durable store is a different job from
exporting derived metrics -- one is the record, the other is a view of it.  A
repository can use either, both, or neither; with neither, the artifact is
still written to disk.

Standard library only: ``urllib``.  A CI helper that needs ``requests``
installed to report health is one more thing that can be missing at the moment
you need the data.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_ATTEMPTS = 3


def _redact(value: str | None) -> str:
    """Never print a token, not even partially in a way that helps an attacker."""
    return "<redacted>" if value else "<unset>"


def emit(
    snapshot: dict[str, Any],
    *,
    endpoint: str | None = None,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    blocking: bool = False,
    log: Any = print,
) -> dict[str, Any]:
    """POST the snapshot as gzipped JSON.

    Returns a status record.  Failure is non-blocking unless ``blocking`` is
    set: an unreachable telemetry endpoint must not fail a build whose code is
    fine.  Analyzer failure is a different matter and is handled by the caller.
    """
    endpoint = endpoint or os.environ.get("CODE_HEALTH_ENDPOINT")
    token = token or os.environ.get("CODE_HEALTH_TOKEN")

    status: dict[str, Any] = {
        "status": "skipped",
        "endpoint_configured": bool(endpoint),
        "attempts": 0,
        "http_status": None,
        "error": None,
    }
    if not endpoint:
        return status

    payload = gzip.compress(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
    headers = {
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "User-Agent": "code-health/0.1.0",
        # Lets the receiver reject a duplicate ingestion of the same analysis
        # without parsing the body.
        "Idempotency-Key": snapshot["run"]["observation_id"],
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        status["attempts"] = attempt
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied endpoint
                status["http_status"] = response.status
                if 200 <= response.status < 300:
                    status["status"] = "ok"
                    return status
                last_error = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            status["http_status"] = exc.code
            # The body often explains the rejection; the headers may echo the
            # Authorization value back, so only the body is read, truncated.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except OSError:
                pass
            last_error = f"HTTP {exc.code}: {detail}"
            # 4xx other than 429 will not succeed on retry.
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < max_attempts:
            backoff = 2 ** (attempt - 1)
            log(f"code-health: telemetry POST failed ({last_error}); retrying in {backoff}s")
            time.sleep(backoff)

    status["status"] = "error"
    status["error"] = last_error
    log(
        f"code-health: telemetry emission failed after {status['attempts']} attempt(s): "
        f"{last_error} (endpoint={endpoint}, token={_redact(token)})"
    )
    if blocking:
        raise RuntimeError(f"code-health telemetry emission failed: {last_error}")
    return status
