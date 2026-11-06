"""`mira-vitals ci select`: triggers, graph selection, fail-open, modes, escapes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mira_vitals.ci import command, select
from mira_vitals.ci.select import Change, Settings, decide

TESTS = ["tests/test_a.py", "tests/test_b.py", "tests/test_pinned.py"]
# a.py <- test_a ; b.py <- test_b ; conftest imports fixtures.py
GRAPH = {
    "src/pkg/a.py": [],
    "src/pkg/b.py": [],
    "src/pkg/fixtures.py": [],
    "tests/conftest.py": ["src/pkg/fixtures.py"],
    "tests/test_a.py": ["src/pkg/a.py"],
    "tests/test_b.py": ["src/pkg/b.py"],
    "tests/test_pinned.py": [],
}


def _decide(changes, settings=None, graph=GRAPH, lomitus=None):
    return decide(settings or Settings(), changes, TESTS, graph, lomitus)


# -- selection ------------------------------------------------------------------


def test_a_module_change_selects_only_its_importers():
    r = _decide([Change("modified", "src/pkg/a.py")])
    assert r["triggers"] == []
    assert r["selected"] == ["tests/test_a.py"]


def test_a_changed_test_file_is_selected_itself():
    r = _decide([Change("modified", "tests/test_b.py")])
    assert r["triggers"] == [] and r["selected"] == ["tests/test_b.py"]


def test_lomitus_files_are_unioned_with_ruff():
    # Lomitus sees an edge ruff doesn't (e.g. an ancestor __init__): union.
    r = _decide([Change("modified", "src/pkg/a.py")], lomitus={"src/pkg/a.py", "tests/test_b.py"})
    assert r["selected"] == ["tests/test_a.py", "tests/test_b.py"]


def test_the_pinned_always_run_list_is_always_selected():
    s = Settings(always_run=["tests/test_pinned.py"])
    r = _decide([Change("modified", "src/pkg/a.py")], settings=s)
    assert "tests/test_pinned.py" in r["selected"]


# -- full-run triggers ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "trigger"),
    [
        ("tests/conftest.py", "conftest"),
        ("conftest.py", "conftest"),
        ("migrations/versions/0001_x.py", "migrations"),
        ("src/pkg/alembic/env.py", "migrations"),
        ("alembic.ini", "migrations"),
        ("pyproject.toml", "deps"),
        ("uv.lock", "deps"),
        ("requirements-dev.txt", "deps"),
        ("Dockerfile", "docker"),
        ("docker-compose.yml", "docker"),
        (".github/workflows/ci.yml", "ci"),
        ("ci/make_worker_dbs.py", "ci"),
    ],
    ids=lambda v: v.replace("/", "_").replace(".", "_") if isinstance(v, str) else str(v),
)
def test_path_triggers(path, trigger):
    graph = {**GRAPH, path: []} if path.endswith(".py") else GRAPH
    assert trigger in _decide([Change("modified", path)], graph=graph)["triggers"]


def test_any_non_python_file_is_a_trigger_except_ignored_docs():
    assert "non-py" in _decide([Change("modified", "docs/audit/capture/page1.json")])["triggers"]
    assert "non-py" in _decide([Change("modified", "src/pkg/template.html")])["triggers"]
    assert _decide([Change("modified", "README.md")])["triggers"] == []


def test_deleted_and_renamed_modules_are_triggers():
    assert "deleted-py" in _decide([Change("deleted", "src/pkg/gone.py")])["triggers"]
    renamed = _decide([Change("renamed", "src/pkg/new.py", "src/pkg/a.py")], graph={**GRAPH, "src/pkg/new.py": []})
    assert "deleted-py" in renamed["triggers"]


def test_a_change_in_the_conftest_import_closure_is_a_trigger():
    assert "conftest-closure" in _decide([Change("modified", "src/pkg/fixtures.py")])["triggers"]


def test_a_module_the_graph_does_not_know_is_a_trigger():
    assert "unresolved" in _decide([Change("modified", "scripts/oddball.py")])["triggers"]


def test_an_unusual_status_is_a_trigger():
    assert "unknown-change" in _decide([Change("other", "src/pkg/a.py")])["triggers"]


def test_a_test_path_that_is_not_shell_safe_forces_a_full_run():
    graph = {**GRAPH, "tests/test_x;rm.py": ["src/pkg/a.py"]}
    r = decide(Settings(), [Change("modified", "src/pkg/a.py")], [*TESTS, "tests/test_x;rm.py"], graph, None)
    assert "unsafe-path" in r["triggers"]


def test_a_default_trigger_can_be_narrowed_by_config():
    s = Settings.from_table({"triggers": {"docker": []}})
    assert "docker" not in _decide([Change("modified", "Dockerfile")], settings=s)["triggers"]
    assert "non-py" in _decide([Change("modified", "Dockerfile")], settings=s)["triggers"]


def test_bad_config_is_rejected():
    for bad in ({"policy": "yolo"}, {"always_run": "tests/x.py"}, {"graph_sources": ["jedi"]}, {"graph_sources": []}):
        with pytest.raises(ValueError):
            Settings.from_table(bad)


def test_change_categories_overlap_and_renames_count_once_per_category():
    counts = select.classify_changes([
        Change("modified", ".github/workflows/ci.yml"),
        Change("modified", "migrations/001.py"),
        Change("modified", "tests/test_api.py"),
        Change("renamed", "web/main.ts", "web/old.ts"),
        Change("modified", "package.json"),
        Change("modified", "src/api.py"),
        Change("modified", "README.md"),
        Change("modified", "assets/logo.svg"),
    ])
    assert counts == {"application": 1, "ci": 1, "dependencies": 1, "documentation": 1,
                      "frontend": 1, "migrations": 1, "other": 1, "tests": 1}
    assert select.classify_changes([]) == {}
    assert select.classify_changes([Change("renamed", "tests/test_a.py", "src/a.py")]) == {
        "application": 1, "tests": 1,
    }


def test_selection_config_hash_tracks_effective_policy_and_settings_without_cache_secrets():
    original = Settings()
    same = Settings.from_table({})
    baseline = select.selection_config_hash(original, "advisory")
    assert baseline.startswith("sha256:") and len(baseline) == 71
    assert baseline == select.selection_config_hash(same, "advisory")
    assert baseline != select.selection_config_hash(same, "enforce")
    same.always_run.append("tests/test_critical.py")
    assert baseline != select.selection_config_hash(same, "advisory")
    same = Settings()
    same.cache_env = ["SECRET_TOKEN"]
    same.service_images = ["https://user:password@example.invalid/image"]
    assert baseline == select.selection_config_hash(same, "advisory")


def test_selector_keeps_known_change_categories_if_graph_fails(repo, monkeypatch):
    r, base, head = repo
    monkeypatch.setattr(select, "ruff_graph", lambda *_: (_ for _ in ()).throw(RuntimeError("failed graph")))
    doc = select.run(str(r), base, head, Settings(graph_sources=["ruff"]))
    assert doc["triggers"] == ["selector_error"]
    assert doc["changed_files"] == 1
    assert doc["change_categories"] == ["application"]
    assert doc["change_category_counts"] == {"application": 1}
    assert doc["selector_duration_seconds"] >= 0
    assert doc["skipped_files_complete"] is False


def test_selector_unknown_change_categories_if_diff_fails(repo):
    r, _base, head = repo
    doc = select.run(str(r), "invalid-revision", head, Settings(graph_sources=["ruff"]))
    assert doc["changed_files"] is None
    assert doc["change_categories"] is None
    assert doc["change_category_counts"] is None
    assert doc["selector_duration_seconds"] >= 0


def test_path_triggers_remain_visible_when_graph_fails(monkeypatch):
    monkeypatch.setattr(select, "changed", lambda *_: [Change("modified", "migrations/065.sql")])
    monkeypatch.setattr(select, "tracked_files", lambda *_: ["tests/test_a.py"])
    monkeypatch.setattr(select, "ruff_graph", lambda *_: (_ for _ in ()).throw(RuntimeError("failed graph")))
    doc = select.run(".", "base", "head", Settings(graph_sources=["ruff"]))
    assert doc["triggers"] == ["migrations", "non-py", "selector_error"]
    assert doc["triggers_complete"] is False
    assert doc["change_category_counts"] == {"migrations": 1}


def test_enforce_falls_back_to_full_when_selection_list_exceeds_bound(monkeypatch):
    files = [f"tests/test_{i}.py" for i in range(5001)]
    monkeypatch.setattr(select, "changed", lambda *_: [Change("modified", "src/a.py")])
    monkeypatch.setattr(select, "tracked_files", lambda *_: files)
    monkeypatch.setattr(select, "ruff_graph", lambda *_: {"src/a.py": [], **{f: ["src/a.py"] for f in files}})
    doc = select.run(".", "base", "head", Settings(graph_sources=["ruff"]), policy="enforce", event="pull_request")
    assert doc["triggers"] == ["unsafe-output-size"]
    assert doc["decision"] == "full" and doc["mode"] == "full"
    assert doc["run_files"] == []
    assert doc["run_files_complete"] is True


def test_advisory_marks_truncated_skip_list_as_incomplete(monkeypatch):
    files = [f"tests/test_{i}.py" for i in range(5001)]
    monkeypatch.setattr(select, "changed", lambda *_: [Change("modified", "src/a.py")])
    monkeypatch.setattr(select, "tracked_files", lambda *_: files)
    monkeypatch.setattr(select, "ruff_graph", lambda *_: {"src/a.py": [], **{f: [] for f in files}})
    doc = select.run(".", "base", "head", Settings(graph_sources=["ruff"]), policy="advisory", event="pull_request")
    assert doc["decision"] == "selected" and doc["mode"] == "full"
    assert len(doc["skipped_files"]) == 5000
    assert doc["skipped_files_complete"] is False


def test_empty_selected_argument_file_is_reported_as_full(monkeypatch):
    monkeypatch.setattr(select, "changed", lambda *_: [Change("modified", "src/a.py")])
    monkeypatch.setattr(select, "tracked_files", lambda *_: ["tests/test_b.py"])
    monkeypatch.setattr(select, "ruff_graph", lambda *_: {"src/a.py": [], "tests/test_b.py": []})
    doc = select.run(".", "base", "head", Settings(graph_sources=["ruff"]), policy="enforce", event="pull_request")
    assert doc["decision"] == "selected" and doc["mode"] == "full" and doc["run_files"] == []


# -- a real repository: modes and fail-open -------------------------------------

needs_tools = pytest.mark.skipif(shutil.which("git") is None or shutil.which("ruff") is None, reason="git and ruff")


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _rev(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src/pkg").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src/pkg/__init__.py").write_text("")
    (r / "src/pkg/a.py").write_text("def f():\n    return 1\n")
    (r / "src/pkg/b.py").write_text("def g():\n    return 2\n")
    (r / "tests/test_a.py").write_text("from pkg.a import f\n\ndef test_f():\n    assert f() == 1\n")
    (r / "tests/test_b.py").write_text("from pkg.b import g\n\ndef test_g():\n    assert g() == 2\n")
    (r / "pyproject.toml").write_text('[tool.ruff]\nsrc = ["src"]\n')
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = _rev(r)
    (r / "src/pkg/a.py").write_text("def f():\n    return 1  # changed\n\n\nX = 3\n")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "change a")
    return r, base, _rev(r)


def _ns(repo, base, head, mode=None, event="pull_request"):
    return SimpleNamespace(repo=str(repo), base=base, head=head, mode=mode, event=event)


@needs_tools
def test_advisory_records_the_selection_but_runs_everything(repo):
    r, base, head = repo
    doc = select.main_select(_ns(r, base, head), {}, {"graph_sources": ["ruff"]})
    assert doc["policy"] == "advisory" and doc["decision"] == "selected"
    assert doc["mode"] == "full" and doc["run_files"] == []
    assert doc["selected"] == 1 and doc["total"] == 2
    assert doc["skipped_files"] == ["tests/test_b.py"]


@needs_tools
def test_enforce_runs_only_the_selection_on_pull_requests(repo):
    r, base, head = repo
    doc = select.main_select(_ns(r, base, head, mode="enforce"), {}, {"graph_sources": ["ruff"]})
    assert doc["mode"] == "selected" and doc["run_files"] == ["tests/test_a.py"]


@needs_tools
def test_enforce_never_narrows_a_push_or_a_schedule(repo):
    r, base, head = repo
    for event in ("push", "schedule", None):
        doc = select.main_select(_ns(r, base, head, mode="enforce", event=event), {}, {"graph_sources": ["ruff"]})
        assert doc["decision"] == "selected" and doc["mode"] == "full" and doc["run_files"] == []


@needs_tools
def test_a_bad_revision_fails_open(repo):
    r, _base, head = repo
    doc = select.main_select(_ns(r, "0" * 40, head, mode="enforce"), {}, {"graph_sources": ["ruff"]})
    assert doc["triggers"] == ["selector_error"] and doc["mode"] == "full" and doc["errors"]


def test_a_missing_graph_tool_fails_open(repo, monkeypatch):
    r, base, head = repo
    monkeypatch.setenv("MIRA_VITALS_RUFF", "/nonexistent/ruff")
    doc = select.main_select(_ns(r, base, head, mode="enforce"), dict(os.environ), {"graph_sources": ["ruff"]})
    assert doc["triggers"] == ["selector_error"] and doc["mode"] == "full"


def test_lomitus_unavailable_fails_open(repo, monkeypatch):
    r, base, head = repo
    monkeypatch.setitem(sys.modules, "lomitus", None)  # import fails
    doc = select.main_select(_ns(r, base, head, mode="enforce"), {}, {"graph_sources": ["lomitus"]})
    assert doc["triggers"] == ["selector_error"] and doc["mode"] == "full"


def test_an_invalid_config_fails_open(repo):
    r, base, head = repo
    doc = select.main_select(_ns(r, base, head, mode="enforce"), {}, {"policy": "nonsense"})
    assert doc["triggers"] == ["selector_error"] and doc["mode"] == "full"


@needs_tools
def test_the_cli_writes_selection_and_an_args_file_and_never_fails(repo, tmp_path):
    r, base, head = repo
    (r / "mira-vitals.toml").write_text('[select]\npolicy = "enforce"\ngraph_sources = ["ruff"]\n')
    out, args_out = tmp_path / "selection.json", tmp_path / "args.txt"
    rc = command.main(
        ["select", "--repo", str(r), "--base", base, "--head", head, "--output", str(out), "--args-out", str(args_out)],
        env={"GITHUB_EVENT_NAME": "pull_request", "PATH": os.environ["PATH"]},
    )
    assert rc == 0
    doc = json.loads(out.read_text())
    # mira-vitals.toml is uncommitted here, so the diff is just a.py.
    assert doc["mode"] == "selected"
    assert args_out.read_text().splitlines() == ["tests/test_a.py"]
    # garbage in: still 0, and a full run
    rc = command.main(["select", "--repo", str(r), "--base", "nope", "--head", head, "--output", str(out)], env={})
    assert rc == 0 and json.loads(out.read_text())["mode"] == "full"


# -- report: escapes ------------------------------------------------------------


def test_report_counts_an_advisory_escape(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "selection.json").write_text(
        json.dumps(
            {
                "mode": "full", "policy": "advisory", "decision": "selected", "count_unit": "files",
                "triggers": [], "selected": 1, "total": 2, "skipped_files": ["tests/test_b.py"],
            }
        )
    )
    junit_xml = tmp_path / "junit.xml"
    junit_xml.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_a" name="test_f" time="0.1"/>'
        '<testcase classname="tests.test_b" name="test_g" time="0.1"><failure message="x"/></testcase>'
        "</testsuite></testsuites>"
    )
    out = tmp_path / "ci-health.json"
    env = {"MIRA_VITALS_CI_DIR": str(state), "PATH": os.environ["PATH"]}
    rc = command.main(
        ["report", "--junit", str(junit_xml), "--tests-complete", "--output", str(out), "--no-export", "--no-platform", "--quiet"], env=env
    )
    assert rc == 0
    sel = json.loads(out.read_text())["optimizations"]["selection"]
    assert sel["escapes"] == 1 and sel["escape_ids"] == ["tests/test_b.py::test_g"]
    assert sel["skipped"] == 1 and "skipped_files" not in sel  # the path list is not exported


def test_report_counts_no_escape_when_the_failure_was_selected(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "selection.json").write_text(
        json.dumps({"mode": "full", "policy": "advisory", "decision": "selected", "selected": 1, "total": 2,
                    "skipped_files": ["tests/test_b.py"]})
    )
    junit_xml = tmp_path / "junit.xml"
    junit_xml.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_a" name="test_f" time="0.1"><failure message="x"/></testcase>'
        '<testcase classname="tests.test_b" name="test_g" time="0.1"/>'
        "</testsuite></testsuites>"
    )
    out = tmp_path / "ci-health.json"
    command.main(
        ["report", "--junit", str(junit_xml), "--tests-complete", "--output", str(out), "--no-export", "--no-platform", "--quiet"],
        env={"MIRA_VITALS_CI_DIR": str(state)},
    )
    assert json.loads(out.read_text())["optimizations"]["selection"]["escapes"] == 0


@pytest.mark.parametrize("condition", ["no_junit", "invalid_junit", "missing_file", "truncated", "cancelled"])
def test_report_does_not_claim_zero_escapes_without_complete_evidence(tmp_path, condition):
    state = tmp_path / "state"
    state.mkdir()
    selection = {"mode": "full", "policy": "advisory", "decision": "selected", "selected": 1, "total": 2,
                 "skipped_files": ["tests/test_b.py"], "skipped_files_complete": condition != "truncated"}
    (state / "selection.json").write_text(json.dumps(selection))
    junit_xml = tmp_path / "junit.xml"
    if condition == "invalid_junit":
        junit_xml.write_text("not XML")
    else:
        classname = "tests.test_a" if condition == "missing_file" else "tests.test_b"
        junit_xml.write_text(f'<testsuite><testcase classname="{classname}" name="test_g"/></testsuite>')
    out = tmp_path / "ci-health.json"
    args = ["report", "--tests-complete", "--output", str(out), "--no-export", "--no-platform", "--quiet"]
    if condition != "no_junit":
        args += ["--junit", str(junit_xml)]
    if condition == "cancelled":
        args += ["--outcome", "cancelled"]
    assert command.main(args, env={"MIRA_VITALS_CI_DIR": str(state)}) == 0
    sel = json.loads(out.read_text())["optimizations"]["selection"]
    assert sel["escapes"] is None
    assert sel["escape_observation"] == "incomplete"


def test_full_selection_is_not_a_zero_escape_trial(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "selection.json").write_text(json.dumps({
        "mode": "full", "decision": "full", "selected": 2, "total": 2, "skipped_files": [],
    }))
    out = tmp_path / "ci-health.json"
    command.main(["report", "--output", str(out), "--no-export", "--no-platform", "--quiet"],
                 env={"MIRA_VITALS_CI_DIR": str(state)})
    sel = json.loads(out.read_text())["optimizations"]["selection"]
    assert sel["escapes"] is None and sel["escape_observation"] == "not_applicable"


@pytest.mark.parametrize("time_attr, outcome, expected", [
    ('time="2.5"', "success", 2.5), ("", "success", None),
    ('time="NaN"', "success", None), ('time="2.5"', None, None),
])
def test_skipped_test_seconds_are_observed_not_assumed(tmp_path, time_attr, outcome, expected):
    state = tmp_path / "state"
    state.mkdir()
    (state / "selection.json").write_text(json.dumps({
        "mode": "full", "policy": "advisory", "decision": "selected", "selected": 1, "total": 2,
        "skipped_files": ["tests/test_b.py"], "skipped_files_complete": True,
    }))
    xml = tmp_path / "junit.xml"
    xml.write_text(f'<testsuite><testcase classname="tests.test_b" name="test_g" {time_attr}/></testsuite>')
    out = tmp_path / "ci-health.json"
    args = ["report", "--tests-complete", "--output", str(out), "--junit", str(xml), "--no-export", "--no-platform", "--quiet"]
    if outcome:
        args += ["--outcome", outcome]
    command.main(args, env={"MIRA_VITALS_CI_DIR": str(state)})
    sel = json.loads(out.read_text())["optimizations"]["selection"]
    assert sel["escape_observation"] == "complete"
    assert sel["skipped_test_seconds_observed"] == expected


def test_missing_one_of_multiple_junit_reports_is_incomplete(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "selection.json").write_text(json.dumps({
        "mode": "full", "decision": "selected", "selected": 1, "total": 2,
        "skipped_files": ["tests/test_b.py"],
    }))
    xml = tmp_path / "junit.xml"
    xml.write_text('<testsuite><testcase classname="tests.test_b" name="test_g" time="1"/></testsuite>')
    out = tmp_path / "ci-health.json"
    command.main(["report", "--tests-complete", "--output", str(out), "--junit", str(xml), "--junit", str(tmp_path / "missing.xml"),
                  "--no-export", "--no-platform", "--quiet"], env={"MIRA_VITALS_CI_DIR": str(state)})
    sel = json.loads(out.read_text())["optimizations"]["selection"]
    assert sel["escapes"] is None and sel["escape_observation"] == "incomplete"


def test_without_attestation_positive_failure_is_only_a_lower_bound(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "selection.json").write_text(json.dumps({
        "mode": "full", "decision": "selected", "selected": 1, "total": 2,
        "skipped_files": ["tests/test_b.py"],
    }))
    xml = tmp_path / "junit.xml"
    xml.write_text('<testsuite><testcase classname="tests.test_b" name="test_g" time="1"><failure/></testcase></testsuite>')
    out = tmp_path / "ci-health.json"
    command.main(["report", "--output", str(out), "--junit", str(xml), "--outcome", "success",
                  "--no-export", "--no-platform", "--quiet"], env={"MIRA_VITALS_CI_DIR": str(state)})
    sel = json.loads(out.read_text())["optimizations"]["selection"]
    assert sel["tests_complete_attested"] is False
    assert sel["escape_observation"] == "incomplete" and sel["escapes"] is None
    assert sel["escapes_observed"] == 1 and sel["escape_ids_observed"] == ["tests/test_b.py::test_g"]
    assert sel["skipped_test_seconds_observed"] is None
