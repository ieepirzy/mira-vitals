"""Import bounded, untrusted PR CI reports in a trusted workflow_run.

The report is data, never code. Run/job identities come from GitHub, not the
artifact. Artifact observations are labelled untrusted: schema validation
cannot establish that PR-controlled processes reported truthfully.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from typing import Any

from . import export, record
from .http import Deadline, _OPENER, request

API = "https://api.github.com"
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_DOCUMENT = 16 * 1024 * 1024
MAX_ARTIFACTS = 50
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NAME = re.compile(r"^mira-vitals-ci-([1-9][0-9]{0,19})$")
_IDENT = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,63}$")
_WORK = {"unit-test", "integration-test", "e2e-test", "lint", "typecheck", "image-build", "security-scan",
         "dependency-scan", "migration", "deploy-validation", "test-selection", "other"}
_OUTCOMES = {"success", "failure", "skipped", "cancelled", "unknown"}
_CATEGORIES = {"application", "tests", "migrations", "dependencies", "ci", "frontend", "documentation", "other"}


def _seconds_between(start: Any, end: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if first.tzinfo is None or last.tzinfo is None:
            return None
        seconds = (last - first).total_seconds()
        return seconds if 0 <= seconds <= 31_536_000 else None
    except (ValueError, OverflowError):
        return None


def _bounded_int(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _bounded_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and 0 <= value <= 1e12 else None
    except OverflowError:
        return None


def _selection(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid selection")
    out: dict[str, Any] = {}
    for field, choices in {"mode": {"full", "selected"}, "decision": {"full", "selected"},
                           "policy": {"advisory", "enforce"}, "count_unit": {"files", "tests"},
                           "escape_observation": {"complete", "incomplete", "unavailable", "not_applicable"}}.items():
        v = value.get(field)
        if v is not None and v not in choices:
            raise ValueError("invalid selection enum")
        out[field] = v
    for field in ("selected", "total", "skipped", "changed_files", "escapes", "escapes_observed", "closure_cache_hits"):
        v = value.get(field)
        out[field] = _bounded_int(v)
        if v is not None and out[field] is None:
            raise ValueError("invalid selection count")
    for field in ("selector_duration_seconds", "estimated_saved_seconds", "skipped_test_seconds_observed"):
        v = value.get(field)
        out[field] = _bounded_number(v)
        if v is not None and out[field] is None:
            raise ValueError("invalid selection duration")
    for field in ("triggers_complete", "skipped_files_complete", "run_files_complete", "tests_complete_attested"):
        v = value.get(field)
        if v is not None and type(v) is not bool:
            raise ValueError("invalid completeness flag")
        out[field] = v
    for field in ("selector_name", "selector_version", "graph_source", "ruff_version", "lomitus_version"):
        v = value.get(field)
        if v is not None and (not isinstance(v, str) or not _IDENT.fullmatch(v)):
            raise ValueError("invalid selector identity")
        out[field] = v
    config_hash = value.get("selection_config_hash")
    if config_hash is not None and (not isinstance(config_hash, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", config_hash)):
        raise ValueError("invalid selector hash")
    out["selection_config_hash"] = config_hash
    for field in ("change_categories", "triggers"):
        v = value.get(field)
        if v is not None:
            if not isinstance(v, list) or len(v) > 32 or any(not isinstance(x, str) or not _IDENT.fullmatch(x) for x in v):
                raise ValueError("invalid category list")
            if field == "change_categories" and any(x not in _CATEGORIES for x in v):
                raise ValueError("unknown change category")
            v = sorted(set(v))
        out[field] = v
    counts = value.get("change_category_counts")
    if counts is not None:
        if not isinstance(counts, dict) or any(k not in _CATEGORIES or _bounded_int(v) is None for k, v in counts.items()):
            raise ValueError("invalid category counts")
    out["change_category_counts"] = counts
    return out


def _work(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > len(_WORK):
        raise ValueError("invalid work categories")
    if any(k not in _WORK or not isinstance(v, str) or v not in _OUTCOMES for k, v in value.items()):
        raise ValueError("unknown work category or outcome")
    return value


def _unpack(blob: bytes) -> dict[str, Any]:
    if len(blob) > MAX_ARCHIVE:
        raise ValueError("archive too large")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != "ci-health.json" or entries[0].is_dir():
                raise ValueError("archive must contain only ci-health.json")
            if entries[0].file_size > MAX_DOCUMENT or entries[0].flag_bits & 1:
                raise ValueError("document too large or encrypted")
            with archive.open(entries[0]) as stream:
                raw = stream.read(MAX_DOCUMENT + 1)
            if len(raw) > MAX_DOCUMENT:
                raise ValueError("document too large")
            doc = json.loads(raw)
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise ValueError("invalid report archive") from exc
    if not isinstance(doc, dict) or doc.get("schema_version") != "ci-health/1":
        raise ValueError("unknown report schema")
    return doc


def _api(path: str, token: str, deadline: Deadline) -> dict[str, Any]:
    code, body, _error = request("GET", API + path, headers={"Authorization": "Bearer " + token,
                                "Accept": "application/vnd.github+json"}, deadline=deadline)
    if code != 200 or not isinstance(body, dict):
        raise ValueError(f"GitHub API request failed ({code or 'unavailable'})")
    return body


def _download(repo: str, artifact_id: int, token: str, deadline: Deadline) -> bytes:
    """Follow only GitHub's archive redirect, never forwarding its token."""
    timeout = deadline.timeout(15)
    if timeout is None:
        raise ValueError("time budget spent")
    url = f"{API}/repos/{repo}/actions/artifacts/{artifact_id}/zip"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "User-Agent": "mira-vitals-ci"})
    try:
        _OPENER.open(req, timeout=timeout).close()
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise ValueError("archive request failed") from exc
        location = exc.headers.get("Location") or ""
    else:
        raise ValueError("archive endpoint did not redirect")
    target = urllib.parse.urlparse(location)
    host = (target.hostname or "").lower()
    if target.scheme != "https" or target.username or target.password or not (
        host.endswith(".blob.core.windows.net") or host.endswith(".githubusercontent.com")
    ):
        raise ValueError("untrusted archive destination")
    timeout = deadline.timeout(30)
    if timeout is None:
        raise ValueError("time budget spent")
    try:
        with _OPENER.open(urllib.request.Request(location, headers={"User-Agent": "mira-vitals-ci"}), timeout=timeout) as response:
            data = response.read(MAX_ARCHIVE + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise ValueError("archive download failed") from exc
    if len(data) > MAX_ARCHIVE:
        raise ValueError("archive too large")
    return data


def _event(env: dict[str, str], expected: str) -> tuple[str, int, int]:
    if env.get("GITHUB_EVENT_NAME") != "workflow_run" or not expected.startswith(".github/workflows/"):
        raise ValueError("not an allowed workflow_run")
    repo = env.get("GITHUB_REPOSITORY") or ""
    if not _REPO.fullmatch(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise ValueError("invalid repository")
    path = env.get("GITHUB_EVENT_PATH") or ""
    if os.path.getsize(path) > MAX_DOCUMENT:
        raise ValueError("event too large")
    with open(path, encoding="utf-8") as handle:
        event = json.load(handle)
    run = event.get("workflow_run") or {}
    if (not isinstance(event.get("repository"), dict) or event.get("action") != "completed" or run.get("event") != "pull_request"
            or run.get("status") != "completed" or run.get("conclusion") not in ("success", "failure", "cancelled")
            or event.get("repository", {}).get("full_name", "").lower() != repo.lower()):
        raise ValueError("event is not a completed pull-request run")
    run_id, attempt = run.get("id"), run.get("run_attempt")
    if type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0:
        raise ValueError("invalid run identity")
    return repo, run_id, attempt


def collect(env: dict[str, str], expected: str, token: str, deadline: Deadline) -> list[dict[str, Any]]:
    repo, run_id, attempt = _event(env, expected)
    run = _api(f"/repos/{repo}/actions/runs/{run_id}", token, deadline)
    if (run.get("id") != run_id or run.get("run_attempt") != attempt or run.get("event") != "pull_request"
            or run.get("status") != "completed" or run.get("path") != expected
            or run.get("conclusion") not in ("success", "failure", "cancelled")):
        raise ValueError("upstream workflow identity mismatch")
    pull_requests = run.get("pull_requests")
    # A workflow can be associated with more than one PR, or GitHub can omit
    # the linkage (notably for forks). Do not guess when it is ambiguous.
    pr = pull_requests[0] if isinstance(pull_requests, list) and len(pull_requests) == 1 else {}
    pr = pr if isinstance(pr, dict) else {}
    pr_number = _bounded_int(pr.get("number"))
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_sha = base.get("sha")
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
        base_sha = None
    jobs_response = _api(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", token, deadline)
    jobs = jobs_response.get("jobs") or []
    if not isinstance(jobs, list) or jobs_response.get("total_count", 0) > 100:
        raise ValueError("job list incomplete")
    jobs_by_id = {j.get("id"): j for j in jobs if isinstance(j, dict) and type(j.get("id")) is int}
    artifacts_response = _api(f"/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100", token, deadline)
    artifacts = artifacts_response.get("artifacts") or []
    if not isinstance(artifacts, list) or artifacts_response.get("total_count", 0) > 100:
        raise ValueError("artifact list incomplete")
    # Artifact listings cover all attempts. Ignore earlier-attempt job IDs;
    # they must not poison a valid rerun or be attributed to its jobs.
    artifacts = [a for a in artifacts if isinstance(a, dict) and isinstance(a.get("name"), str)
                 and (match := _NAME.fullmatch(a["name"])) and int(match.group(1)) in jobs_by_id
                 and not a.get("expired")]
    if len(artifacts) > MAX_ARTIFACTS:
        raise ValueError("too many reports")
    records = []
    seen: set[int] = set()
    for artifact in artifacts:
        aid = artifact.get("id")
        if type(aid) is not int or aid <= 0 or _bounded_int(artifact.get("size_in_bytes")) is None or artifact["size_in_bytes"] > MAX_ARCHIVE:
            raise ValueError("invalid artifact metadata")
        doc = _unpack(_download(repo, aid, token, deadline))
        identity, job = doc.get("identity"), doc.get("job")
        if not isinstance(identity, dict) or not isinstance(job, dict):
            raise ValueError("invalid report identity")
        job_id = job.get("id")
        if (type(job_id) is not int or job_id in seen or job_id not in jobs_by_id
                or artifact["name"] != f"mira-vitals-ci-{job_id}"):
            raise ValueError("unknown or duplicate report job")
        actual = jobs_by_id[job_id]
        if actual.get("status") != "completed":
            raise ValueError("report job is not completed")
        if (str(identity.get("run_id")) != str(run_id) or str(identity.get("run_attempt")) != str(attempt)
                or str(identity.get("repository", "")).lower() != repo.lower()
                or job.get("name") != actual.get("name")):
            raise ValueError("artifact identity does not match GitHub")
        seen.add(job_id)
        opts = doc.get("optimizations") or {}
        if not isinstance(opts, dict):
            raise ValueError("invalid optimizations")
        tests = doc.get("tests") or {}
        if not isinstance(tests, dict):
            raise ValueError("invalid test summary")
        attrs = {"event.name": "ci.report", "ci.observation.trust": "untrusted_pr_artifact",
                 "cicd.pipeline.name": run.get("name"), "cicd.pipeline.run.id": run_id,
                 "cicd.pipeline.run.attempt": attempt, "cicd.pipeline.run.number": run.get("run_number"),
                 "vcs.ref.head.revision": run.get("head_sha"), "cicd.pipeline.trigger": "pull_request",
                 "vcs.change.id": pr_number, "vcs.ref.base.revision": base_sha,
                 "ci.reporter.job": actual.get("name"), "ci.reporter.job.id": job_id,
                 "cicd.pipeline.result": actual.get("conclusion"), "ci.selection": _selection(opts.get("selection")),
                 "ci.work": _work(doc.get("work")),
                 # These timings come from GitHub, not the PR-controlled artifact.
                 "ci.job.duration": _seconds_between(actual.get("started_at"), actual.get("completed_at")),
                 "ci.job.queue.wait": _seconds_between(actual.get("created_at"), actual.get("started_at")),
                 "ci.job.duration.basis": "platform", "ci.job.queue.basis": "job_created",
                 # No testcase rows are imported; existing escape joins must
                 # never infer a complete test inventory from this record.
                 "ci.tests.dropped": max(1, _bounded_int(tests.get("dropped")) or 0)}
        records.append({"timeUnixNano": str(time.time_ns()), "severityNumber": 9, "severityText": "INFO",
                        "eventName": "ci.report", "body": {"stringValue": "Untrusted PR report"},
                        "attributes": record._attrs(attrs)})
    return [{"resourceLogs": [{"resource": {"attributes": record._attrs({"service.name": "mira-vitals-ci",
                     "vcs.repository.name": repo, "cicd.system": "github", "mira_vitals.schema": "ci-health/1"})},
                     "scopeLogs": [{"scope": {"name": "mira_vitals.ci"}, "logRecords": records}]}]}] if records else []


def main() -> int:
    env = dict(os.environ)
    token = env.get("GITHUB_TOKEN") or ""
    expected = env.get("MIRA_VITALS_ALLOWED_WORKFLOW") or ""
    if not token:
        print("::warning::PR telemetry import has no GitHub token")
        return 1
    try:
        deadline = Deadline(120)
        bodies = collect(env, expected, token, deadline)
        endpoint = export.endpoint(env, "logs")
        if not endpoint:
            raise ValueError("no OTLP logs endpoint configured")
        target = urllib.parse.urlparse(endpoint)
        if target.scheme != "https" or not target.hostname or target.username or target.password:
            raise ValueError("PR telemetry requires an HTTPS collector")
        for body in bodies:
            status, response, error = request("POST", endpoint, headers=export.headers_from_env(env), body=body,
                                             deadline=deadline, gzip_body=True, cafile=export.certificate(env, "logs"))
            if status is None or not 200 <= status < 300 or export._partial(response, "rejectedLogRecords"):
                raise ValueError("collector rejected PR reports")
        print(f"Imported {len(bodies)} PR report batches (observations are untrusted).")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Do not print response body, artifact contents, URLs, tokens or paths.
        print(f"::warning::PR telemetry import failed ({type(exc).__name__}).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
