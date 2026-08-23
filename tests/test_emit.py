"""HTTP emitter: retries, redaction, and non-blocking failure."""

import gzip
import json

import pytest

from mira_vitals import emit


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_no_endpoint_is_a_skip_not_an_error(snapshot_factory):
    """The artifact is still produced; emission is optional."""
    status = emit.emit(snapshot_factory(), endpoint=None, log=lambda *a: None)
    assert status["status"] == "skipped"
    assert status["endpoint_configured"] is False


def test_payload_is_gzipped_json_with_an_idempotency_key(snapshot_factory, monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.headers)
        captured["body"] = request.data
        captured["url"] = request.full_url
        return _Response(202)

    monkeypatch.setattr(emit.urllib.request, "urlopen", fake_urlopen)
    document = snapshot_factory()
    status = emit.emit(document, endpoint="https://telemetry.example/ingest", token="secret", log=lambda *a: None)

    assert status["status"] == "ok"
    assert json.loads(gzip.decompress(captured["body"]))["schema_version"] == 1
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["content-encoding"] == "gzip"
    # Lets the receiver reject a duplicate without parsing the body.
    assert headers["idempotency-key"] == document["run"]["observation_id"]
    assert headers["authorization"] == "Bearer secret"


def test_transient_failure_is_retried_with_backoff(snapshot_factory, monkeypatch):
    attempts = {"n": 0}

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise emit.urllib.error.URLError("connection refused")
        return _Response(200)

    monkeypatch.setattr(emit.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(emit.time, "sleep", lambda seconds: None)
    status = emit.emit(snapshot_factory(), endpoint="https://x/i", log=lambda *a: None)
    assert status["status"] == "ok"
    assert attempts["n"] == 3


def test_a_4xx_is_not_retried(snapshot_factory, monkeypatch):
    """A malformed payload will be just as malformed on the third attempt."""
    attempts = {"n": 0}

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        raise emit.urllib.error.HTTPError("https://x/i", 400, "Bad Request", {}, None)

    monkeypatch.setattr(emit.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(emit.time, "sleep", lambda seconds: None)
    status = emit.emit(snapshot_factory(), endpoint="https://x/i", log=lambda *a: None)
    assert status["status"] == "error"
    assert attempts["n"] == 1


def test_a_429_is_retried(snapshot_factory, monkeypatch):
    attempts = {"n": 0}

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        raise emit.urllib.error.HTTPError("https://x/i", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(emit.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(emit.time, "sleep", lambda seconds: None)
    emit.emit(snapshot_factory(), endpoint="https://x/i", max_attempts=3, log=lambda *a: None)
    assert attempts["n"] == 3


def test_failure_is_non_blocking_by_default(snapshot_factory, monkeypatch):
    """An unreachable telemetry endpoint must not fail a build whose code is fine."""
    monkeypatch.setattr(
        emit.urllib.request, "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(emit.urllib.error.URLError("down")),
    )
    monkeypatch.setattr(emit.time, "sleep", lambda seconds: None)
    status = emit.emit(snapshot_factory(), endpoint="https://x/i", log=lambda *a: None)
    assert status["status"] == "error"


def test_failure_can_be_made_blocking(snapshot_factory, monkeypatch):
    monkeypatch.setattr(
        emit.urllib.request, "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(emit.urllib.error.URLError("down")),
    )
    monkeypatch.setattr(emit.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError):
        emit.emit(snapshot_factory(), endpoint="https://x/i", blocking=True, log=lambda *a: None)


def test_the_token_is_never_logged(snapshot_factory, monkeypatch):
    lines = []
    monkeypatch.setattr(
        emit.urllib.request, "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(emit.urllib.error.URLError("down")),
    )
    monkeypatch.setattr(emit.time, "sleep", lambda seconds: None)
    emit.emit(
        snapshot_factory(),
        endpoint="https://x/i",
        token="super-secret-token",
        log=lines.append,
    )
    joined = "\n".join(lines)
    assert "super-secret-token" not in joined
    assert "<redacted>" in joined
