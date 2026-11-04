"""mira-vitals ci: the never-fail contract and every measurement it makes."""

from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mira_vitals.ci import cgroup, command, junit, marks, platforms, record
from mira_vitals.ci.http import Deadline

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
  <testcase classname="tests.test_a" name="test_fast" file="tests/test_a.py" time="0.010"/>
  <testcase classname="tests.test_a" name="test_slow" file="tests/test_a.py" time="8.000"/>
  <testcase classname="tests.test_b" name="test_broken" file="tests/test_b.py" time="1.500"><failure message="x"/></testcase>
  <testcase classname="tests.test_b" name="test_skip" time="0"><skipped/></testcase>
  <testcase classname="tests.test_c" name="test_mid" time="0.490"/>
</testsuite></testsuites>
"""


@pytest.fixture
def env(tmp_path):
    return {"MIRA_VITALS_CI_DIR": str(tmp_path / "state"), "MIRA_VITALS_CGROUP_ROOT": str(tmp_path / "nocgroup")}


# --- marks -----------------------------------------------------------------


def test_marks_become_named_phases(env):
    marks.record("start", env=env, now=100.0)
    marks.record("install", env=env, now=112.5)
    marks.record("tests", env=env, now=160.0)
    got = marks.phases(marks.load(env))
    assert [(p["name"], p["duration_seconds"]) for p in got] == [("install", 12.5), ("tests", 47.5)]


@pytest.mark.parametrize("bad", ["Install", "a b", "x" * 49, "", "../etc", "phase;rm"])
def test_mark_names_are_bounded_identifiers(env, bad):
    with pytest.raises(ValueError):
        marks.record(bad, env=env)


def test_corrupt_mark_lines_are_skipped(env):
    marks.record("start", env=env, now=1.0)
    with open(marks.marks_path(env), "a", encoding="utf-8") as handle:
        handle.write("not json\n{\"name\": 3}\n")
    marks.record("done", env=env, now=2.0)
    assert [m["name"] for m in marks.load(env)] == ["start", "done"]


# --- JUnit -----------------------------------------------------------------


def test_junit_durations_outcomes_and_concentration(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT)
    cases, files, problems = junit.collect([str(path)])
    assert files == [str(path)] and problems == []
    summary = junit.summarize(cases)
    assert summary["count"] == 5
    assert summary["by_outcome"] == {"passed": 3, "failed": 1, "skipped": 1}
    assert summary["total_seconds"] == 10.0
    assert summary["max_seconds"] == 8.0
    # One test (the 8 s one) is 80% of the 10 s total.
    assert summary["tests_for_80pct_time"] == 1
    assert summary["slowest"][0]["name"] == "test_slow"
    assert junit.test_id(summary["slowest"][0]) == "tests/test_a.py::test_slow"


def test_junit_problems_are_reported_not_raised(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuite><testcase")
    cases, files, problems = junit.collect([str(bad), str(tmp_path / "missing-*.xml")])
    assert cases == [] and files == []
    assert any("could not read" in p for p in problems)


def test_junit_glob_expands(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.xml").write_text(JUNIT)
    (tmp_path / "a" / "two.xml").write_text(JUNIT)
    cases, files, _ = junit.collect([str(tmp_path / "a" / "*.xml")])
    assert len(files) == 2 and len(cases) == 10


# --- cgroup ----------------------------------------------------------------


def _cgroup(root, usage, throttled, peak):
    root.mkdir(parents=True, exist_ok=True)
    (root / "cpu.stat").write_text(
        f"usage_usec {usage}\nuser_usec 1\nsystem_usec 1\nnr_periods 100\nnr_throttled 25\nthrottled_usec {throttled}\n"
    )
    (root / "cpu.max").write_text("200000 100000\n")
    (root / "memory.peak").write_text(f"{peak}\n")
    (root / "memory.max").write_text("max\n")


def test_cgroup_reads_and_subtracts_a_baseline(tmp_path):
    root = tmp_path / "cg"
    _cgroup(root, 1_000_000, 50_000, 2**30)
    start = cgroup.read(str(root))
    _cgroup(root, 9_000_000, 450_000, 2**31)
    got = cgroup.delta(cgroup.read(str(root)), start)
    assert got["cpu_usage_usec"] == 8_000_000
    assert got["cpu_throttled_usec"] == 400_000
    assert got["cpu_limit_cores"] == 2.0
    assert got["memory_peak_bytes"] == 2**31
    assert got["memory_limit_bytes"] is None  # "max" is no limit, not zero
    assert got["baseline"] is True


def test_cgroup_counter_going_backwards_is_unknown_not_negative(tmp_path):
    root = tmp_path / "cg"
    _cgroup(root, 5_000_000, 0, 1)
    start = cgroup.read(str(root))
    _cgroup(root, 1_000_000, 0, 1)
    assert cgroup.delta(cgroup.read(str(root)), start)["cpu_usage_usec"] is None


def test_no_cgroup_means_none_everywhere(tmp_path):
    sample = cgroup.read(str(tmp_path / "absent"))
    assert all(v is None for v in sample.values())


# --- platform detection ----------------------------------------------------


@pytest.mark.parametrize(
    "variables,want",
    [
        ({"GITHUB_ACTIONS": "true", "GITHUB_API_URL": "https://api.github.com"}, "github"),
        ({"GITHUB_ACTIONS": "true", "GITHUB_API_URL": "https://code.example/api/v1"}, "forgejo"),
        ({"GITHUB_ACTIONS": "true", "FORGEJO_ACTIONS": "true"}, "forgejo"),
        ({"GITHUB_ACTIONS": "true", "MIRA_VITALS_CI_PLATFORM": "none"}, None),
        ({}, None),
    ],
)
def test_platform_detection(variables, want):
    assert platforms.detect(variables) == want


# --- a fake platform API ---------------------------------------------------


class _Api:
    def __init__(self, routes):
        self.routes = routes
        self.seen = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                api.seen.append((self.command, self.path, dict(self.headers), raw))
                for prefix, (status, body) in api.routes.items():
                    if self.path.startswith(prefix):
                        payload = json.dumps(body).encode()
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                self.send_response(404)
                self.end_headers()

            do_GET = do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


GH_RUN = {
    "id": 7,
    "run_attempt": 1,
    "run_number": 3,
    "name": "CI",
    "event": "push",
    "head_sha": "c" * 40,
    "head_branch": "main",
    "created_at": "2026-11-03T14:18:51Z",
    "run_started_at": "2026-11-03T14:18:51Z",
    "html_url": "https://github.com/o/r/actions/runs/7",
}
GH_JOBS = {
    "jobs": [
        {
            "id": 11,
            "name": "test",
            "status": "in_progress",
            "conclusion": None,
            "created_at": "2026-11-03T14:19:32Z",
            "started_at": "2026-11-03T14:34:04Z",
            "completed_at": None,
            "runner_name": "runner-a",
            "labels": ["mira-ci"],
            "steps": [
                {"number": 1, "name": "Set up job", "status": "completed", "conclusion": "success",
                 "started_at": "2026-11-03T14:34:04Z", "completed_at": "2026-11-03T14:34:10Z"},
                {"number": 7, "name": "Test", "status": "completed", "conclusion": "success",
                 "started_at": "2026-11-03T14:34:30Z", "completed_at": "2026-11-03T14:55:22Z"},
                {"number": 8, "name": "Report", "status": "in_progress", "conclusion": None,
                 "started_at": "2026-11-03T14:55:22Z", "completed_at": None},
            ],
        },
        {
            "id": 12,
            "name": "compose",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-11-03T14:19:32Z",
            "started_at": "2026-11-03T14:22:03Z",
            "completed_at": "2026-11-03T14:22:15Z",
            "runner_name": "runner-b",
            "labels": ["mira-ci"],
            "steps": [],
        },
    ]
}


def _gh_env(env, api, **extra):
    return {
        **env,
        "GITHUB_ACTIONS": "true",
        "GITHUB_API_URL": api.url,
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "7",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_NUMBER": "3",
        "GITHUB_WORKFLOW": "CI",
        "GITHUB_JOB": "test",
        "GITHUB_SHA": "c" * 40,
        "GITHUB_REF_NAME": "main",
        "GITHUB_EVENT_NAME": "push",
        "RUNNER_NAME": "runner-a",
        "GITHUB_TOKEN": "tok-secret-123",
        **extra,
    }


def test_github_adapter_queue_wait_steps_and_self(env):
    api = _Api({"/repos/o/r/actions/runs/7/attempts/1/jobs": (200, GH_JOBS), "/repos/o/r/actions/runs/7": (200, GH_RUN)})
    try:
        got, note = platforms.fetch(_gh_env(env, api), Deadline(10))
    finally:
        api.close()
    assert got is not None, note
    test = next(j for j in got["jobs"] if j["name"] == "test")
    assert test["self"] is True
    assert test["queue_wait_seconds"] == 872.0  # 14:19:32 -> 14:34:04
    assert test["queue_basis"] == "job_created"
    assert [s["duration_seconds"] for s in test["steps"]] == [6.0, 1252.0, None]
    compose = next(j for j in got["jobs"] if j["name"] == "compose")
    assert compose["self"] is False and compose["duration_seconds"] == 12.0
    # The token went as a bearer to the API, and only there.
    assert all(h.get("Authorization") == "Bearer tok-secret-123" for _, _, h, _ in api.seen)


def test_github_adapter_refusal_is_a_note_not_an_error(env):
    api = _Api({"/repos/o/r/actions/runs/7": (403, {"message": "Resource not accessible by integration"})})
    try:
        got, note = platforms.fetch(_gh_env(env, api), Deadline(10))
    finally:
        api.close()
    assert got is None and "actions: read" in note


def test_no_token_skips_the_adapter(env):
    got, note = platforms.fetch({**env, "GITHUB_ACTIONS": "true", "GITHUB_API_URL": "https://api.github.com"}, Deadline(5))
    assert got is None and "no token" in note


def test_credentials_never_go_over_plain_http_to_a_remote_host(env):
    got, note = platforms.fetch(
        {**env, "GITHUB_ACTIONS": "true", "GITHUB_API_URL": "http://example.invalid", "GITHUB_REPOSITORY": "o/r",
         "GITHUB_RUN_ID": "1", "GITHUB_TOKEN": "t"},
        Deadline(5),
    )
    assert got is None and "plain http" in note


def test_forgejo_adapter_reports_tasks_with_run_created_basis(env):
    runs = {"workflow_runs": [{"index_in_repo": 9, "created": "2026-11-03T10:00:00Z"}]}
    tasks = {
        "workflow_runs": [
            {"id": 1, "name": "build", "run_number": 9, "status": "success",
             "created_at": "2026-11-03T10:03:00Z", "run_started_at": "2026-11-03T10:03:00Z",
             "updated_at": "2026-11-03T10:08:00Z", "head_sha": "d" * 40},
            {"id": 2, "name": "old", "run_number": 8, "status": "success",
             "created_at": "2026-11-03T09:00:00Z", "run_started_at": "2026-11-03T09:00:00Z",
             "updated_at": "2026-11-03T09:01:00Z"},
        ]
    }
    api = _Api({"/api/v1/repos/o/r/actions/runs": (200, runs), "/api/v1/repos/o/r/actions/tasks": (200, tasks)})
    try:
        got, note = platforms.fetch(
            {**env, "GITHUB_ACTIONS": "true", "GITHUB_API_URL": api.url + "/api/v1", "GITHUB_REPOSITORY": "o/r",
             "GITHUB_RUN_NUMBER": "9", "GITHUB_TOKEN": "fj"},
            Deadline(10),
        )
    finally:
        api.close()
    assert got is not None, note
    assert [j["name"] for j in got["jobs"]] == ["build"]
    build = got["jobs"][0]
    assert build["queue_wait_seconds"] == 180.0 and build["queue_basis"] == "run_created"
    assert build["duration_seconds"] == 300.0 and build["conclusion"] == "success"


# --- the command -----------------------------------------------------------


def test_report_end_to_end_writes_document_and_exports(env, tmp_path):
    junit_path = tmp_path / "junit.xml"
    junit_path.write_text(JUNIT)
    out = tmp_path / "ci-health.json"
    summary = tmp_path / "summary.md"
    api = _Api({
        "/repos/o/r/actions/runs/7/attempts/1/jobs": (200, GH_JOBS),
        "/repos/o/r/actions/runs/7": (200, GH_RUN),
        "/v1/metrics": (200, {}),
        "/v1/logs": (200, {}),
    })
    try:
        e = _gh_env(
            env, api,
            OTEL_EXPORTER_OTLP_ENDPOINT=api.url,
            OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20ingest-tok",
            GITHUB_STEP_SUMMARY=str(summary),
            MIRA_VITALS_CACHE_DOCKER="false",
        )
        assert command.main(["start"], env=e) == 0
        assert command.main(["mark", "install"], env=e) == 0
        rc = command.main(
            ["report", "--junit", str(junit_path), "--cache", "uv=true", "--cache", "npm=",
             "--outcome", "success", "--output", str(out), "--quiet"],
            env=e,
        )
    finally:
        api.close()
    assert rc == 0
    doc = json.loads(out.read_text())
    assert doc["schema_version"] == "ci-health/1"
    assert doc["job"]["queue_wait_seconds"] == 872.0
    assert doc["job"]["wall_basis"] == "platform"
    assert doc["caches"] == {"uv": True, "docker": False}  # empty npm= ignored
    assert [p["name"] for p in doc["phases"]] == ["install"]
    assert doc["tests"]["summary"]["count"] == 5
    assert doc["export"]["metrics"] == "ok" and doc["export"]["logs"] == "ok"
    assert "Queue wait: **14m 32.0s**" in summary.read_text()

    posts = {path: (headers, body) for method, path, headers, body in api.seen if method == "POST"}
    metrics = json.loads(posts["/v1/metrics"][1])
    names = {m["name"] for m in metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
    assert {"ci.job.queue.wait", "ci.job.duration", "ci.step.duration", "ci.tests.count", "ci.cache.hit"} <= names
    assert posts["/v1/metrics"][0]["Authorization"] == "Bearer ingest-tok"
    logs = json.loads(posts["/v1/logs"][1])
    records = logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    events = [r["eventName"] for r in records]
    assert events[0] == "ci.report"
    assert events.count("ci.job") == 2 and events.count("ci.test") == 5
    # Completed steps only: the in-progress "Report" step is not a record.
    assert events.count("ci.step") == 2
    # The failing test is first among the tests: a cap never drops a failure.
    first_test = next(r for r in records if r["eventName"] == "ci.test")
    attrs = {a["key"]: a["value"] for a in first_test["attributes"]}
    assert attrs["test.case.result.status"] == {"stringValue": "failed"}


def _metric_attr_keys(body):
    keys = set()
    for metric in body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
        for point in metric["gauge"]["dataPoints"]:
            keys |= {a["key"] for a in point["attributes"]}
    return keys


def test_metric_attributes_are_bounded():
    """No id, sha, url or runner name ever becomes a metric dimension."""
    doc = record.build(
        identity={"repository": "o/r", "workflow": "CI", "job": "test", "run_id": 7, "sha": "c" * 40,
                  "runner_name": "runner-a", "url": "https://x"},
        platform=None, platform_note="", marks=[], phases=[{"name": "install", "started_at": 0, "ended_at": 1,
                                                            "duration_seconds": 1.0}],
        tests=[], test_files=[], caches={"uv": True}, runner={"cpu_usage_usec": 5}, outcome="success",
        warnings=[], generated_by={"version": "0.1.0"}, now=10.0,
        optimizations={
            "selection": {"mode": "full", "triggers": ["conftest", "deps"], "selected": 10, "total": 10,
                          "estimated_saved_seconds": 0.0, "closure_cache_hits": 0},
            "parallelism": {"count": 4, "wall_max_seconds": 30.0, "imbalance": 1.2},
        },
    )
    keys = _metric_attr_keys(record.otlp_metrics_body(doc, 1))
    assert keys <= {
        "cicd.pipeline.name", "cicd.pipeline.task.name", "cicd.worker.labels", "cicd.pipeline.result",
        "ci.phase.name", "cicd.pipeline.task.step.name", "test.outcome", "ci.cache.name",
        "ci.selection.mode", "ci.selection.trigger",
    }


def test_test_cap_keeps_failures(env):
    cases = [{"classname": "c", "name": f"t{i}", "file": "", "duration_seconds": float(i), "outcome": "passed"}
             for i in range(10)]
    cases.append({"classname": "c", "name": "boom", "file": "", "duration_seconds": 0.0, "outcome": "failed"})
    doc = record.build(identity={"job": "test"}, platform=None, platform_note="", marks=[], phases=[],
                       tests=cases, test_files=["x"], caches={}, runner={}, outcome=None, warnings=[],
                       generated_by={}, now=1.0)
    tests = [r for r in record.log_records(doc, max_tests=3) if r["event"] == "ci.test"]
    assert [r["attributes"]["test.case.name"] for r in tests] == ["boom", "t9", "t8"]


@pytest.mark.parametrize(
    "argv",
    [
        ["mark", "Not A Name"],
        ["report", "--junit", "/nonexistent/*.xml", "--no-export"],
        ["bogus-subcommand"],
        ["report", "--budget", "not-a-number"],
        [],
    ],
)
def test_never_fails_the_build(env, tmp_path, argv, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert command.main(argv, env=env) == 0


def test_an_unreachable_endpoint_is_bounded_and_non_fatal(env, tmp_path):
    out = tmp_path / "doc.json"
    # A port nothing listens on: refused at once. The budget bounds the rest.
    e = {**env, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"}
    assert command.main(["report", "--output", str(out), "--budget", "5", "--quiet"], env=e) == 0
    doc = json.loads(out.read_text())
    assert doc["export"]["metrics"] == "error"


def test_an_unexpected_crash_is_a_warning(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(command, "_report", boom)
    assert command.main(["report"], env=env) == 0
    assert "::warning" in capsys.readouterr().out


def test_action_metadata_carries_no_expression_outside_defaults_and_steps():
    """GitHub evaluates `${{ }}` in an action's metadata when it loads it, with
    only a few contexts available. One in an input description made the
    action fail to load, which failed the calling job: the one thing this
    command must never do. Text-based, so no YAML dependency."""
    import os
    import re

    path = os.path.join(os.path.dirname(__file__), "..", ".github", "actions", "ci-health", "action.yml")
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    in_runs = False
    seen = 0
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if line.startswith("runs:"):
            in_runs = True
        for expr in re.findall(r"\$\{\{(.*?)\}\}", line):
            seen += 1
            root = expr.strip().split(".")[0]
            if in_runs:
                assert root in ("steps", "inputs", "github"), (number, line)
            else:
                # Before `runs:` only an input default may hold one, and only
                # from the github context.
                assert stripped.startswith("default:") and root == "github", (number, line)
    assert seen > 0


# --- optimizations: selection, parallelism, quarantine, PR linkage ----------


def test_selection_record_is_validated(tmp_path):
    from mira_vitals.ci import optimizations

    path = tmp_path / "selection.json"
    path.write_text(json.dumps({
        "mode": "selected", "triggers": ["conftest", "Not Allowed!"], "selected": 812, "total": 2629,
        "estimated_saved_seconds": 940, "selector": {"name": "mira-vitals-select", "version": "0.2.0"},
        "graph": {"source": "ruff", "ruff_version": "0.16.4", "lomitus_version": "1.3.0"},
        "closure_cache_hits": 1500,
    }))
    warnings = []
    got = optimizations.selection(str(path), warnings)
    assert got["mode"] == "selected" and got["selected"] == 812 and got["total"] == 2629
    assert got["triggers"] == ["conftest"] and any("Not Allowed" in w for w in warnings)
    assert (got["selector_version"], got["ruff_version"], got["lomitus_version"]) == ("0.2.0", "0.16.4", "1.3.0")
    path.write_text(json.dumps({"mode": "selected", "selected": 5, "total": 2}))
    assert optimizations.selection(str(path), [])["selected"] is None  # impossible counts dropped
    path.write_text(json.dumps({"mode": "partial"}))
    assert optimizations.selection(str(path), []) is None
    path.write_text("{not json")
    warnings = []
    assert optimizations.selection(str(path), warnings) is None and warnings


def test_worker_imbalance_and_isolation(tmp_path):
    from mira_vitals.ci import optimizations

    path = tmp_path / "workers.json"
    path.write_text(json.dumps({"workers": [
        {"id": "gw0", "wall_seconds": 100, "tests": 600, "db_isolated": True},
        {"id": "gw1", "wall_seconds": 300, "tests": 700, "db_isolated": True},
    ]}))
    got = optimizations.workers(str(path), None, [])
    assert got["count"] == 2 and got["wall_max_seconds"] == 300 and got["imbalance"] == 1.5
    assert got["db_isolated"] == "true"
    assert optimizations.workers(None, 8, [])["count"] == 8
    assert optimizations.workers(None, None, []) is None


def test_pr_linkage_from_ref_and_event(tmp_path):
    from mira_vitals.ci import optimizations

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 148, "base": {"sha": "b" * 40}, "head": {"sha": "h" * 40}}}))
    got = optimizations.pr_linkage({"GITHUB_REF": "refs/pull/148/merge", "GITHUB_EVENT_PATH": str(event)})
    assert got == {"pr_number": 148, "base_sha": "b" * 40, "head_sha": "h" * 40, "trigger_at": None}
    assert optimizations.pr_linkage({"GITHUB_REF": "refs/heads/main"})["pr_number"] is None


def test_report_carries_selection_workers_quarantine_and_merged_pr(env, tmp_path):
    from mira_vitals.ci import optimizations

    junit_path = tmp_path / "junit.xml"
    junit_path.write_text(JUNIT)
    state = env["MIRA_VITALS_CI_DIR"]
    import os

    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "selection.json"), "w") as handle:
        json.dump({"mode": "selected", "triggers": [], "selected": 5, "total": 20, "estimated_saved_seconds": 30}, handle)
    with open(os.path.join(state, "quarantine.json"), "w") as handle:
        json.dump({"test_ids": ["tests/test_b.py::test_broken"]}, handle)
    run = dict(GH_RUN, event="push")
    api = _Api({
        "/repos/o/r/actions/runs/7/attempts/1/jobs": (200, GH_JOBS),
        "/repos/o/r/actions/runs/7": (200, run),
        "/repos/o/r/commits/": (200, [{"number": 41, "merged_at": "2026-11-03T14:18:00Z"}]),
    })
    out = tmp_path / "doc.json"
    try:
        assert command.main(
            ["report", "--junit", str(junit_path), "--workers", "4", "--no-export", "--output", str(out), "--quiet"],
            env=_gh_env(env, api),
        ) == 0
    finally:
        api.close()
    doc = json.loads(out.read_text())
    assert doc["optimizations"]["selection"]["selected"] == 5
    assert doc["optimizations"]["parallelism"]["count"] == 4
    assert doc["identity"]["merged_pr_number"] == 41
    broken = next(c for c in doc["tests"]["cases"] if c["name"] == "test_broken")
    assert broken["quarantined"] is True
    records = record.log_records(doc, max_tests=100)
    assert records[0]["attributes"]["ci.merged_change.id"] == 41
    assert records[0]["attributes"]["ci.selection"]["mode"] == "selected"
    # The escape join reads the selection mode and PR link on every test row.
    test_row = next(r for r in records if r["event"] == "ci.test")
    assert test_row["attributes"]["ci.selection.mode"] == "selected"
    assert test_row["attributes"]["ci.merged_change.id"] == 41
    assert optimizations  # imported for the module's side-effect-free API


def test_runs_from_a_bare_checkout_with_pythonpath_only(tmp_path):
    """What the action does: no install, `PYTHONPATH=<checkout>/src python -m
    mira_vitals ci ...`. The pinned ref, not any installed distribution, is
    recorded as the revision."""
    import os
    import subprocess
    import sys

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": src,
        "MIRA_VITALS_CI_DIR": str(tmp_path / "state"),
        "MIRA_VITALS_CI_REVISION": "deadbeef",
    }
    out = tmp_path / "doc.json"
    for argv in (["ci", "start"], ["ci", "report", "--no-export", "--quiet", "--output", str(out)]):
        result = subprocess.run(
            [sys.executable, "-m", "mira_vitals", *argv], env=env, cwd=tmp_path,
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc["generated_by"]["revision"] == "deadbeef"
    assert doc["generated_by"]["source"] == "action-checkout"
    assert doc["job"]["wall_basis"] == "marks"


# --- review round: hardening ------------------------------------------------


def _action_wrapper(tmp_path, python_path):
    """Run the action's own install step (from action.yml) to build the
    wrapper, exactly as the runner would."""
    import os
    import re
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".github", "actions", "ci-health", "action.yml"), encoding="utf-8") as handle:
        manifest = handle.read()
    blocks = re.findall(r"      run: \|\n((?:        .*\n|\n)+)", manifest)
    install = next(b for b in blocks if "printf" in b)
    script = tmp_path / "install.sh"
    script.write_text("\n".join(line[8:] for line in install.splitlines()) + "\n")
    state = tmp_path / "state"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "MV_DIR": str(state),
        "MV_PY": python_path,
        "MV_READY": "",
        "MV_SRC": os.path.join(root, "src"),
        "GITHUB_PATH": str(tmp_path / "github_path"),
    }
    subprocess.run(["bash", str(script)], env=env, check=True, timeout=60, capture_output=True)
    return state / "bin" / "mira-vitals"


def test_action_wrapper_ignores_a_shadowing_module_in_the_workspace(tmp_path):
    """M1: the report runs in the checked-out repository with tokens in its
    environment. A repo-root json.py must not be imported instead of the
    standard library's."""
    import os
    import subprocess
    import sys

    wrapper = _action_wrapper(tmp_path, sys.executable)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "pwned"
    (workspace / "json.py").write_text(f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(99)\n")
    env = {"PATH": os.environ.get("PATH", ""), "MIRA_VITALS_CI_DIR": str(tmp_path / "s"), "GITHUB_TOKEN": "t"}
    result = subprocess.run(
        [str(wrapper), "ci", "report", "--no-export", "--quiet", "--output", "doc.json"],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert not marker.exists(), "the workspace json.py was imported"
    assert result.returncode == 0, result.stderr
    assert json.loads((workspace / "doc.json").read_text())["schema_version"] == "ci-health/1"


def test_action_runs_nothing_unbounded():
    """M3: no step downloads anything, and the wrapper runs under timeout."""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".github", "actions", "ci-health", "action.yml"), encoding="utf-8") as handle:
        manifest = handle.read()
    code = "\n".join(line for line in manifest.splitlines() if not line.strip().startswith("#"))
    assert "uses:" not in code  # no setup-uv or any other download step
    assert "uv python install" not in code
    assert "exec timeout 120 %q -P -m mira_vitals" in code
    assert "timeout 20 uv python find" in code


def test_a_redirect_is_refused_and_the_token_never_follows_it():
    """M2: urllib's default handler follows a 302 and copies Authorization
    to the new host; here a redirect is an answer, not followed."""
    from mira_vitals.ci.http import request

    other = _Api({"/": (200, {"stolen": True})})
    first = _Api({})
    first.routes = {}
    # A route that redirects to the other server.

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            first.seen.append(("GET", self.path, dict(self.headers), b""))
            self.send_response(302)
            self.send_header("Location", other.url + "/collect")
            self.end_headers()

        do_POST = do_GET

    first.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/start"
    try:
        status, body, error = request("GET", url, headers={"Authorization": "Bearer secret-token"}, deadline=Deadline(10))
        status_post, _, _ = request("POST", url, headers={"Authorization": "Bearer secret-token"}, body={"a": 1},
                                    deadline=Deadline(10))
    finally:
        server.shutdown()
        other.close()
    assert status == 302 and status_post == 302
    assert other.seen == [], "the redirect was followed"


def test_untrusted_names_cannot_inject_workflow_commands(env, tmp_path, capsys):
    """M7: a test or step name with a newline must not start a stdout line
    (the runner reads `::command::` there) or break the summary table."""
    junit_path = tmp_path / "junit.xml"
    junit_path.write_text(
        '<testsuite><testcase classname="tests.test_a" name="evil&#10;::error::pwned|`x`" time="9"/>'
        '<testcase classname="tests.test_a" name="ok&#13;&#10;::add-mask::x" time="1"/></testsuite>'
    )
    jobs = json.loads(json.dumps(GH_JOBS))
    jobs["jobs"][0]["steps"][1]["name"] = "Test\n::error::from-step"
    api = _Api({"/repos/o/r/actions/runs/7/attempts/1/jobs": (200, jobs), "/repos/o/r/actions/runs/7": (200, GH_RUN)})
    summary = tmp_path / "summary.md"
    try:
        command.main(
            ["report", "--junit", str(junit_path), "--no-export", "--output", str(tmp_path / "d.json")],
            env=_gh_env(env, api, GITHUB_STEP_SUMMARY=str(summary)),
        )
    finally:
        api.close()
    out = capsys.readouterr().out
    for text in (out, summary.read_text()):
        for line in text.splitlines():
            assert not line.lstrip().startswith("::error") and not line.lstrip().startswith("::add-mask"), line
    # The pipe is escaped for the table, the backticks neutralised.
    assert "evil ::error::pwned\\|'x'" in summary.read_text()


def test_the_whole_command_has_a_wall_clock_deadline(env, monkeypatch, capsys):
    """M3: not only per-request timeouts -- a stuck report is stopped."""
    import time as time_module

    monkeypatch.setattr(command, "_deadline_seconds", lambda budget: 0.3)

    def slow(*a, **k):
        time_module.sleep(3)

    monkeypatch.setattr(command, "_report", slow)
    started = time_module.monotonic()
    assert command.main(["report"], env=env) == 0
    assert time_module.monotonic() - started < 2
    assert "budget" in capsys.readouterr().out


@pytest.mark.parametrize(
    "case,want",
    [
        ({"classname": "tests.test_a", "name": "test_x"}, "tests/test_a.py::test_x"),
        ({"classname": "tests.unit.test_a.TestThing", "name": "test_x[1-2]"}, "tests/unit/test_a.py::TestThing::test_x[1-2]"),
        ({"classname": "tests.test_a", "name": "test_x", "file": "tests/test_a.py"}, "tests/test_a.py::test_x"),
        ({"classname": "tests.test_a.TestT", "name": "test_x", "file": "tests/test_a.py"}, "tests/test_a.py::TestT::test_x"),
    ],
)
def test_test_ids_are_pytest_node_ids(case, want):
    """M9: xunit2 (pytest's default) has no file attribute; ids still match
    the node ids a quarantine list uses."""
    assert junit.test_id(case) == want


def test_quarantine_ids_normalize_to_node_ids(tmp_path):
    from mira_vitals.ci import optimizations

    path = tmp_path / "q.json"
    path.write_text(json.dumps({"test_ids": ["tests/test_a.py::test_x", "tests.test_b::test_y", "  "]}))
    assert optimizations.quarantine(str(path), []) == {"tests/test_a.py::test_x", "tests/test_b.py::test_y"}


def test_jobs_carried_over_from_an_earlier_attempt_are_flagged(env):
    """M4: a re-run's attempt lists jobs it did not re-run; their created_at
    is after they started, and they must not count twice."""
    jobs = json.loads(json.dumps(GH_JOBS))
    jobs["jobs"][1]["created_at"] = "2026-11-03T15:30:00Z"  # attempt 2 created after compose ran
    api = _Api({"/repos/o/r/actions/runs/7/attempts/1/jobs": (200, jobs), "/repos/o/r/actions/runs/7": (200, GH_RUN)})
    try:
        got, _ = platforms.fetch(_gh_env(env, api), Deadline(10))
    finally:
        api.close()
    compose = next(j for j in got["jobs"] if j["name"] == "compose")
    assert compose["carried_over"] is True and compose["queue_wait_seconds"] is None and compose["steps"] == []
    test = next(j for j in got["jobs"] if j["name"] == "test")
    assert test["carried_over"] is False


def test_the_reporting_job_closes_its_own_row(env, tmp_path):
    """M5: the last job of a run reports while in progress; its ci.job row
    still gets a completion (the report time) and a duration."""
    api = _Api({"/repos/o/r/actions/runs/7/attempts/1/jobs": (200, GH_JOBS), "/repos/o/r/actions/runs/7": (200, GH_RUN)})
    out = tmp_path / "d.json"
    try:
        command.main(["report", "--no-export", "--quiet", "--output", str(out)], env=_gh_env(env, api))
    finally:
        api.close()
    doc = json.loads(out.read_text())
    me = next(j for j in doc["platform"]["jobs"] if j["self"])
    assert me["completed_at"] is not None and me["completed_basis"] == "report"
    assert me["duration_seconds"] == pytest.approx(doc["job"]["wall_seconds"], abs=1)
    row = next(r for r in record.log_records(doc, max_tests=0) if r["event"] == "ci.job" and r["attributes"]["ci.job.self"])
    assert row["attributes"]["ci.job.completed.basis"] == "report"
    assert row["attributes"]["ci.reporter.job.id"] == 11


def test_a_refused_merged_pr_lookup_is_a_warning(env, tmp_path, capsys):
    """M8: commits/{sha}/pulls needs pull-requests: read; a 403 says so."""
    run = dict(GH_RUN, event="push")
    api = _Api({
        "/repos/o/r/actions/runs/7/attempts/1/jobs": (200, GH_JOBS),
        "/repos/o/r/actions/runs/7": (200, run),
        "/repos/o/r/commits/": (403, {"message": "Resource not accessible by integration"}),
    })
    try:
        command.main(["report", "--no-export", "--output", str(tmp_path / "d.json")], env=_gh_env(env, api))
    finally:
        api.close()
    assert "pull-requests: read" in capsys.readouterr().out


def test_schema_views_use_no_throwing_conversions():
    """H1: a materialized view that throws fails the INSERT into the shared
    otel_logs table and drops other services' logs. The documented views are
    validated on ClickHouse against a hostile batch (64-char SHAs, garbage
    numbers and JSON); this guards the known throwing forms statically."""
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "docs", "ci-health.md"), encoding="utf-8") as handle:
        sql = "\n".join(re.findall(r"```sql\n(.*?)```", handle.read(), re.S))
    assert "FixedString" not in sql
    views = [b for b in re.split(r";\s*\n", sql) if "MATERIALIZED VIEW" in b]
    assert len(views) >= 5
    for view in views:
        # Plain conversions throw on bad input; only the total forms are
        # allowed -- except an integer cast of a boolean, which cannot.
        for match in re.finditer(r"\bto(?:U?Int\d+|Float\d+|Date(?:Time)?(?:64)?|Decimal\d*)\(([^()]*)\)", view):
            argument = match.group(1)
            if match.group(0).startswith("toUInt") and re.search(r"(=|\bIN\b)", argument):
                continue
            raise AssertionError(f"throwing conversion {match.group(0)} in a materialized view")
        assert "JSONExtractRaw(" not in view


def test_push_trigger_time_from_the_event(tmp_path):
    """Feedback latency starts at the push: repository.pushed_at."""
    from mira_vitals.ci import optimizations

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pusher": {"name": "x"}, "repository": {"pushed_at": 1762200000}}))
    assert optimizations.pr_linkage({"GITHUB_EVENT_PATH": str(event)})["trigger_at"] == 1762200000.0
    event.write_text(json.dumps({"pull_request": {"number": 1}, "repository": {"pushed_at": "2026-01-01T00:00:00Z"}}))
    assert optimizations.pr_linkage({"GITHUB_EVENT_PATH": str(event)})["trigger_at"] is None
