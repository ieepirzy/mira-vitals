"""OTLP/HTTP+JSON export, stdlib only.

Configured by the standard OpenTelemetry variables and nothing else, so the
same job can point at the code-health collector today and at the OTel
gateway in front of the central ClickHouse later without a producer change:

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` -- base URL; ``/v1/metrics`` and
  ``/v1/logs`` are appended, as the OTel SDKs do;
* ``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`` / ``..._LOGS_ENDPOINT`` -- full
  per-signal URLs, used as-is when set;
* ``OTEL_EXPORTER_OTLP_HEADERS`` -- ``key=value`` pairs, comma-separated,
  values percent-encoded (e.g. ``Authorization=Bearer%20<token>``);
* ``OTEL_EXPORTER_OTLP_CERTIFICATE`` / ``..._METRICS_CERTIFICATE`` /
  ``..._LOGS_CERTIFICATE`` -- a PEM file of the CA to verify the endpoint
  against, alone (a private receiver's own CA). Unreadable: that signal is
  not sent; never a fallback to the system roots.

Unset endpoint: nothing is sent and the report says "export skipped". Not
an error -- the document is on disk either way.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from .http import Deadline, request


def headers_from_env(env: dict[str, str]) -> dict[str, str]:
    raw = env.get("OTEL_EXPORTER_OTLP_HEADERS") or ""
    out: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            out[key] = urllib.parse.unquote(value.strip())
    return out


def endpoint(env: dict[str, str], signal: str) -> str | None:
    specific = env.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    if specific:
        return specific
    base = env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not base:
        return None
    return base.rstrip("/") + f"/v1/{signal}"


def certificate(env: dict[str, str], signal: str) -> str | None:
    return env.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_CERTIFICATE") or env.get("OTEL_EXPORTER_OTLP_CERTIFICATE") or None


def _partial(body: Any, field: str) -> int:
    if not isinstance(body, dict):
        return 0
    partial = body.get("partialSuccess")
    if not isinstance(partial, dict):
        return 0
    try:
        return int(partial.get(field) or 0)
    except (TypeError, ValueError):
        return 0


def send(
    env: dict[str, str],
    *,
    metrics_body: dict[str, Any],
    logs_bodies: list[dict[str, Any]],
    deadline: Deadline,
) -> dict[str, Any]:
    """Post both signals. Returns a status record; never raises."""
    status: dict[str, Any] = {"metrics": "skipped", "logs": "skipped", "notes": []}
    headers = headers_from_env(env)
    metrics_url, logs_url = endpoint(env, "metrics"), endpoint(env, "logs")
    if not metrics_url and not logs_url:
        status["notes"].append("no OTLP endpoint configured; export skipped")
        return status
    if metrics_url:
        code, body, error = request(
            "POST", metrics_url, headers=headers, body=metrics_body, deadline=deadline, gzip_body=True,
            cafile=certificate(env, "metrics"),
        )
        if code is not None and 200 <= code < 300:
            rejected = _partial(body, "rejectedDataPoints")
            status["metrics"] = "ok" if not rejected else f"partial ({rejected} rejected)"
        else:
            status["metrics"] = "error"
            status["notes"].append(f"metrics: {error or code}")
    if logs_url:
        sent, rejected, failures = 0, 0, 0
        for body_out in logs_bodies:
            code, body, error = request(
                "POST", logs_url, headers=headers, body=body_out, deadline=deadline, gzip_body=True,
                cafile=certificate(env, "logs"),
            )
            if code is not None and 200 <= code < 300:
                sent += 1
                rejected += _partial(body, "rejectedLogRecords")
            else:
                failures += 1
                status["notes"].append(f"logs: {error or code}")
                break  # one failure is the answer; the budget is shared
        if failures:
            status["logs"] = "error"
        elif rejected:
            status["logs"] = f"partial ({rejected} rejected; the receiver may not store ci.* records yet)"
        else:
            status["logs"] = "ok"
        status["log_requests"] = sent
    return status
