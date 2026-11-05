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
        ["report", "--junit", str(junit_xml), "--output", str(out), "--no-export", "--no-platform", "--quiet"], env=env
    )
    assert rc == 0
    sel = json.loads(out.read_text())["optimizations"]["selection"]
    assert sel["escapes"] == 1 and sel["escape_ids"] == ["tests/test_b.py::test_g"]
    assert sel["skipped"] == 1 and "skipped_files" not in sel  # the path list is not exported


def test_report_counts_no_escape_when_the_failure_was_selected(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "selection.json").write_text(
        json.dumps({"mode": "full", "policy": "advisory", "decision": "selected", "skipped_files": ["tests/test_b.py"]})
    )
    junit_xml = tmp_path / "junit.xml"
    junit_xml.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_a" name="test_f" time="0.1"><failure message="x"/></testcase>'
        "</testsuite></testsuites>"
    )
    out = tmp_path / "ci-health.json"
    command.main(
        ["report", "--junit", str(junit_xml), "--output", str(out), "--no-export", "--no-platform", "--quiet"],
        env={"MIRA_VITALS_CI_DIR": str(state)},
    )
    assert json.loads(out.read_text())["optimizations"]["selection"]["escapes"] == 0
