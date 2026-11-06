"""PR reports are untrusted data, never executable code or authority."""
from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from pathlib import Path

import pytest

from mira_vitals.ci import pr_ingest
from mira_vitals.ci.http import Deadline


def archive(document, name="ci-health.json"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, json.dumps(document))
    return buf.getvalue()


def document():
    return {"schema_version": "ci-health/1", "identity": {"repository": "o/r", "run_id": 42, "run_attempt": 1,
                                                             "sha": "untrusted merge SHA"},
            "job": {"id": 100, "name": "Tests", "outcome": "forged"},
            "optimizations": {"selection": {"mode": "full", "decision": "selected", "policy": "advisory",
                                               "selected": 1, "total": 2, "change_categories": ["tests"],
                                               "escapes": None, "escapes_observed": 1}},
            "work": {"unit-test": "success"}, "tests": {"dropped": 0}}


@pytest.fixture
def setup_import(tmp_path, monkeypatch):
    event = {"action": "completed", "repository": {"full_name": "o/r"},
             "workflow_run": {"id": 42, "run_attempt": 1, "event": "pull_request", "status": "completed",
                              "conclusion": "failure"}}
    p = tmp_path / "event.json"
    p.write_text(json.dumps(event))
    env = {"GITHUB_EVENT_NAME": "workflow_run", "GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_PATH": str(p)}
    run = {"id": 42, "run_attempt": 1, "event": "pull_request", "status": "completed", "conclusion": "failure",
           "path": ".github/workflows/ci.yml", "head_sha": "verified-head", "name": "CI", "run_number": 4,
           "pull_requests": [{"number": 9, "base": {"sha": "a" * 40}}]}
    jobs = {"total_count": 1, "jobs": [{"id": 100, "name": "Tests", "status": "completed", "conclusion": "failure",
             "created_at": "2026-11-06T12:00:00Z", "started_at": "2026-11-06T12:02:00Z",
             "completed_at": "2026-11-06T12:05:00Z"}]}
    artifacts = {"total_count": 1, "artifacts": [{"id": 77, "name": "mira-vitals-ci-100", "size_in_bytes": 500}]}
    monkeypatch.setattr(pr_ingest, "_api", lambda path, token, deadline: jobs if "/jobs?" in path else (
        artifacts if "/artifacts?" in path else run))
    docs = [document()]
    monkeypatch.setattr(pr_ingest, "_download", lambda repo, aid, token, deadline: archive(docs[0]))
    return env, run, jobs, artifacts, docs


def attrs(record):
    return {a["key"]: next(iter(a["value"].values())) for a in record["attributes"]}


def test_import_binds_to_github_and_labels_the_untrusted_report(setup_import):
    env, _run, _jobs, _artifacts, _docs = setup_import
    batches = pr_ingest.collect(env, ".github/workflows/ci.yml", "secret", Deadline(10))
    logs = batches[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert len(logs) == 1
    out = attrs(logs[0])
    assert out["vcs.ref.head.revision"] == "verified-head"  # not artifact's claimed SHA
    assert out["cicd.pipeline.result"] == "failure"       # not artifact's forged outcome
    assert out["ci.observation.trust"] == "untrusted_pr_artifact"
    assert out["ci.tests.dropped"] == "1"                # escape inventory cannot look complete
    assert out["ci.job.queue.wait"] == 120.0
    assert out["ci.job.duration"] == 180.0
    assert out["vcs.change.id"] == "9"
    assert out["vcs.ref.base.revision"] == "a" * 40
    assert json.loads(out["ci.selection"])["escapes_observed"] == 1
    assert json.loads(out["ci.work"]) == {"unit-test": "success"}


@pytest.mark.parametrize("field,value", [("run_id", 999), ("run_attempt", 2), ("repository", "other/repo")])
def test_forged_artifact_identity_is_rejected(setup_import, field, value):
    env, _run, _jobs, _artifacts, docs = setup_import
    docs[0]["identity"][field] = value
    with pytest.raises(ValueError):
        pr_ingest.collect(env, ".github/workflows/ci.yml", "secret", Deadline(10))


def test_unapproved_workflow_and_unknown_job_are_rejected(setup_import):
    env, run, _jobs, _artifacts, docs = setup_import
    run["path"] = ".github/workflows/attacker.yml"
    with pytest.raises(ValueError):
        pr_ingest.collect(env, ".github/workflows/ci.yml", "secret", Deadline(10))
    run["path"] = ".github/workflows/ci.yml"
    docs[0]["job"]["id"] = 101
    with pytest.raises(ValueError):
        pr_ingest.collect(env, ".github/workflows/ci.yml", "secret", Deadline(10))


def test_old_attempt_artifacts_are_not_reassigned_to_the_new_attempt(setup_import):
    env, _run, _jobs, artifacts, _docs = setup_import
    artifacts["artifacts"].append({"id": 88, "name": "mira-vitals-ci-99", "size_in_bytes": 500})
    artifacts["total_count"] = 2
    batches = pr_ingest.collect(env, ".github/workflows/ci.yml", "secret", Deadline(10))
    assert len(batches[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]) == 1


def test_untrusted_artifact_is_never_extracted_and_must_be_bounded():
    with pytest.raises(ValueError):
        pr_ingest._unpack(archive(document(), "../ci-health.json"))
    with pytest.raises(ValueError):
        pr_ingest._unpack(b"x" * (pr_ingest.MAX_ARCHIVE + 1))
    with pytest.raises(ValueError):
        pr_ingest._work({"arbitrary-unbounded-label": "success"})
    with pytest.raises(ValueError):
        pr_ingest._selection({"change_categories": ["arbitrary-unbounded-label"]})


def test_archive_redirect_never_receives_github_authorization(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return b"zip"

    class Opener:
        def open(self, req, timeout):
            requests.append(req)
            if len(requests) == 1:
                raise urllib.error.HTTPError(req.full_url, 302, "redirect", {"Location": "https://x.blob.core.windows.net/zip"}, None)
            return Response()

    monkeypatch.setattr(pr_ingest, "_OPENER", Opener())
    assert pr_ingest._download("o/r", 77, "supersecret", Deadline(10)) == b"zip"
    assert requests[0].get_header("Authorization") == "Bearer supersecret"
    assert requests[1].get_header("Authorization") is None


def test_archive_redirect_to_arbitrary_host_is_rejected(monkeypatch):
    class Opener:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 302, "redirect", {"Location": "https://attacker.example/zip"}, None)
    monkeypatch.setattr(pr_ingest, "_OPENER", Opener())
    with pytest.raises(ValueError):
        pr_ingest._download("o/r", 77, "secret", Deadline(10))


def test_pr_upload_and_trusted_import_are_separate():
    root = Path(__file__).resolve().parent.parent
    upload = (root / ".github/actions/ci-health-artifact/action.yml").read_text()
    trusted = (root / ".github/workflows/ci-pr-ingest.yml").read_text()
    assert "github.event_name == 'pull_request'" in upload
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in upload
    assert "github.event_name == 'workflow_run'" in trusted
    assert "repository: ieepirzy/mira-vitals" in trusted
    assert "persist-credentials: false" in trusted
    assert "github.event.workflow_run.head" not in trusted


def test_self_measure_does_not_forward_ingest_credentials_to_pr_code():
    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github/workflows/ci.yml").read_text()
    for secret in ("OTEL_EXPORTER_OTLP_HEADERS", "CODE_HEALTH_TOKEN"):
        assert f"{secret}: ${{{{ github.event_name == 'push' && secrets.{secret} || '' }}}}" in workflow
