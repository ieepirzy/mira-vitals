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


# -- gates: test-level skips by keyword when their inputs are untouched -----------

GATE = {"gates": {"rules": {"keyword": "pinned_rule_tool", "inputs": ["rules/**", "tests/units/**"]}}}


def _gates(changes, triggers=(), reached=(), files=("tests/test_rules.py",), table=GATE):
    s = Settings.from_table(table)
    return select.decide_gates(s, changes, list(triggers), set(reached), {"rules": set(files)})


def test_a_gate_stays_closed_for_a_data_change_outside_its_inputs():
    [g] = _gates([Change("modified", "clusters/app.yaml")], triggers=["non-py"])
    assert g["open"] is False and g["reasons"] == [] and g["keyword"] == "pinned_rule_tool"


def test_a_gate_opens_on_its_inputs_including_a_renamed_from_path():
    assert _gates([Change("modified", "rules/alerts.yaml")], triggers=["non-py"])[0]["reasons"] == [
        "input:rules/alerts.yaml"
    ]
    [g] = _gates([Change("renamed", "elsewhere/a.yaml", "tests/units/a_test.yaml")], triggers=["non-py"])
    assert g["open"] and "input:tests/units/a_test.yaml" in g["reasons"]


@pytest.mark.parametrize("trigger", ["conftest", "conftest-closure", "deps", "ci", "deleted-py", "unresolved",
                                     "unknown-change", "unsafe-path", "selector_error"])
def test_every_trigger_but_non_py_opens_a_gate(trigger):
    [g] = _gates([Change("modified", "x.txt")], triggers=["non-py", trigger])
    assert g["open"] and g["reasons"] == [f"trigger:{trigger}"]


def test_a_gate_opens_when_the_change_reaches_a_file_holding_its_tests():
    assert _gates([Change("modified", "src/pkg/a.py")], reached={"tests/test_rules.py"})[0]["open"]
    # the test file itself changed
    assert _gates([Change("modified", "tests/test_rules.py")])[0]["open"]
    # reached, but not a gated file: closed
    assert not _gates([Change("modified", "src/pkg/a.py")], reached={"tests/test_a.py"})[0]["open"]


def test_a_gate_matching_no_test_stays_open():
    assert _gates([Change("modified", "x.txt")], triggers=["non-py"], files=())[0]["reasons"] == ["no-gated-tests"]


@pytest.mark.parametrize(
    "gate",
    [
        {"keyword": "not", "inputs": ["a/**"]},
        {"keyword": "a b", "inputs": ["a/**"]},
        {"keyword": "x or y", "inputs": ["a/**"]},
        {"keyword": "ab", "inputs": ["a/**"]},
        {"keyword": "pinned_rule_tool", "inputs": []},
        {"keyword": "pinned_rule_tool"},
        {"keyword": "pinned_rule_tool", "inputs": ["a/**"], "policy": "yolo"},
        {"keyword": "pinned_rule_tool", "inputs": ["a/**"], "inputz": ["b"]},
        "pinned_rule_tool",
    ],
    ids=str,
)
def test_bad_gate_config_is_rejected(gate):
    with pytest.raises(ValueError):
        Settings.from_table({"gates": {"rules": gate}})
    with pytest.raises(ValueError):
        Settings.from_table({"gates": {"Bad Name": {"keyword": "pinned_rule_tool", "inputs": ["a/**"]}}})


def test_gated_test_files_cover_everything_pytest_k_can_match(tmp_path):
    """-k matches names, parametrize ids, markers and extra keywords: any
    mention counts (over-counting only opens the gate more often)."""
    (tmp_path / "tests").mkdir()
    files = {
        "tests/test_fn.py": "def test_x_with_the_PINNED_RULE_TOOL():\n    pass\n",  # case-insensitive
        "tests/test_cls.py": "class TestPinned_rule_toolThings:\n    def test_a(self):\n        pass\n",
        "tests/test_pinned_rule_tool_module.py": "def test_a():\n    pass\n",  # the module name matches
        "tests/test_marker.py": "@pytest.mark.pinned_rule_tool\ndef test_a():\n    pass\n",
        "tests/test_param.py": "@pytest.mark.parametrize('x', [1], ids=['pinned_rule_tool_case'])\ndef test_a(x): pass\n",
        "tests/test_other.py": "def test_a():\n    pass\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).write_text(text)
    got = select.gated_test_files(str(tmp_path), [*files, "tests/test_missing.py"], "pinned_rule_tool")
    assert got == set(files) - {"tests/test_other.py"} | {"tests/test_missing.py"}  # unreadable counts


def test_a_conftest_naming_the_keyword_gates_every_test_file(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    for i in items: i.add_marker('pinned_rule_tool')\n"
    )
    (tmp_path / "tests/test_other.py").write_text("def test_a():\n    pass\n")
    got = select.gated_test_files(str(tmp_path), ["tests/test_other.py"], "pinned_rule_tool", ["tests/conftest.py"])
    assert got == {"tests/test_other.py"}


def test_a_gate_never_skips_the_always_run_list():
    table = {**GATE, "always_run": ["tests/test_rules.py"]}
    [g] = _gates([Change("modified", "x.txt")], triggers=["non-py"], table=table)
    assert g["open"] and g["reasons"] == ["always-run:tests/test_rules.py"]


def test_a_gated_file_outside_the_expected_files_keeps_the_gate_open():
    table = {"gates": {"rules": {**GATE["gates"]["rules"], "files": ["tests/test_rules.py"]}}}
    assert not _gates([Change("modified", "x.txt")], triggers=["non-py"], table=table)[0]["open"]
    [g] = _gates([Change("modified", "x.txt")], triggers=["non-py"], table=table,
                 files=("tests/test_rules.py", "tests/test_firewall_rules.py"))
    assert g["open"] and g["reasons"] == ["unexpected-gated-file:tests/test_firewall_rules.py"]


def test_a_submodule_bump_inside_the_inputs_opens_the_gate():
    table = {"gates": {"rules": {"keyword": "pinned_rule_tool", "inputs": ["vendor/rules/**"]}}}
    assert _gates([Change("modified", "vendor/rules")], triggers=["non-py"], table=table)[0]["open"]


@pytest.fixture
def gate_repo(tmp_path):
    r = tmp_path / "repo"
    for d in ("src/pkg", "tests", "rules", "clusters"):
        (r / d).mkdir(parents=True)
    (r / "src/pkg/__init__.py").write_text("")
    (r / "src/pkg/a.py").write_text("def f():\n    return 1\n")
    (r / "rules/alerts.yaml").write_text("rule: 1\n")
    (r / "clusters/app.yaml").write_text("replicas: 1\n")
    (r / "tests/test_a.py").write_text("from pkg.a import f\n\ndef test_f():\n    assert f() == 1\n")
    (r / "tests/test_rules.py").write_text(
        "def test_rule_shape():\n    pass\n\n"
        "def test_rules_with_the_pinned_rule_tool():\n    raise AssertionError('gated test ran')\n"
    )
    (r / "pyproject.toml").write_text('[tool.ruff]\nsrc = ["src"]\n')
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return r, _rev(r)


def _commit(r, rel, text):
    (r / rel).write_text(text)
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", f"change {rel}")
    return _rev(r)


def _gate_table(policy=None):
    gate = {"keyword": "pinned_rule_tool", "inputs": ["rules/**"]}
    if policy:
        gate["policy"] = policy
    return {"graph_sources": ["ruff"], "gates": {"rules": gate}}


@needs_tools
def test_an_enforcing_gate_deselects_its_tests_on_a_pull_request_in_a_full_run(gate_repo, tmp_path):
    r, base = gate_repo
    head = _commit(r, "clusters/app.yaml", "replicas: 2\n")
    doc = select.main_select(_ns(r, base, head), {}, _gate_table("enforce"))
    # the file selection (advisory) says full because of the YAML; the gate
    # still skips its tests
    assert doc["policy"] == "advisory" and doc["decision"] == "full" and doc["triggers"] == ["non-py"]
    assert doc["deselected_keywords"] == ["pinned_rule_tool"] and doc["mode"] == "selected"
    [g] = doc["gates"]
    assert g["open"] is False and g["policy"] == "enforce" and g["files"] == ["tests/test_rules.py"]
    args_out = tmp_path / "args.txt"
    select.write_outputs(doc, str(tmp_path / "selection.json"), str(args_out))
    args = args_out.read_text().splitlines()
    assert args == ["-k", "not pinned_rule_tool"]
    # pytest really skips the gated test (it would fail) and runs the rest
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=r,
                         capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(r / "src")},
                         timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "2 passed, 1 deselected" in run.stdout


@needs_tools
def test_a_gate_input_change_runs_the_gated_tests(gate_repo):
    r, base = gate_repo
    head = _commit(r, "rules/alerts.yaml", "rule: 2\n")
    doc = select.main_select(_ns(r, base, head), {}, _gate_table("enforce"))
    assert doc["gates"][0]["open"] and doc["deselected_keywords"] == [] and doc["mode"] == "full"


@needs_tools
def test_a_closed_gate_never_skips_on_a_push_and_advisory_only_records(gate_repo):
    r, base = gate_repo
    head = _commit(r, "clusters/app.yaml", "replicas: 2\n")
    for event in ("push", "schedule", None):
        doc = select.main_select(_ns(r, base, head, event=event), {}, _gate_table("enforce"))
        assert doc["deselected_keywords"] == [] and doc["mode"] == "full"
        assert doc["skipped_keywords"] == ["pinned_rule_tool"]
    doc = select.main_select(_ns(r, base, head), {}, _gate_table())  # inherits advisory
    assert doc["gates"][0]["policy"] == "advisory" and doc["deselected_keywords"] == []
    assert doc["skipped_keywords"] == ["pinned_rule_tool"] and doc["mode"] == "full"


@needs_tools
def test_a_marker_only_gated_test_reached_by_a_code_change_runs(gate_repo, tmp_path):
    """Review finding: a test -k matches by marker, importing the changed
    module, must open the gate (it used to be missed and deselected)."""
    r, _ = gate_repo
    (r / "src/pkg/render.py").write_text("def render():\n    return 1\n")
    (r / "tests/test_marker.py").write_text(
        "import pytest\nfrom pkg.render import render\n\n"
        "@pytest.mark.pinned_rule_tool\ndef test_render():\n    assert render() == 1\n"
    )
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "marker test")
    base = _rev(r)
    head = _commit(r, "src/pkg/render.py", "def render():\n    return 2\n")
    doc = select.main_select(_ns(r, base, head), {}, _gate_table("enforce"))
    [g] = doc["gates"]
    assert g["open"] and "reaches:tests/test_marker.py" in g["reasons"]
    assert doc["deselected_keywords"] == []


@needs_tools
def test_mode_advisory_on_the_command_line_turns_every_gate_off(gate_repo):
    r, base = gate_repo
    head = _commit(r, "clusters/app.yaml", "replicas: 2\n")
    doc = select.main_select(_ns(r, base, head, mode="advisory"), {}, _gate_table("enforce"))
    assert doc["gates"][0]["open"] is False and doc["gates"][0]["policy"] == "advisory"
    assert doc["deselected_keywords"] == [] and doc["mode"] == "full"
    # ...and --mode enforce never raises a gate soaking in advisory
    doc = select.main_select(_ns(r, base, head, mode="enforce"), {}, _gate_table("advisory"))
    assert doc["gates"][0]["policy"] == "advisory" and doc["deselected_keywords"] == []


@needs_tools
def test_a_selector_error_opens_every_gate(gate_repo):
    r, _base = gate_repo
    doc = select.main_select(_ns(r, "0" * 40, _rev(r)), {}, _gate_table("enforce"))
    assert doc["triggers"] == ["selector_error"] and doc["gates"][0]["open"]
    assert doc["deselected_keywords"] == [] and doc["mode"] == "full"


def test_report_counts_a_gate_escape_and_records_the_closed_gate(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "selection.json").write_text(
        json.dumps(
            {
                "mode": "full", "policy": "advisory", "decision": "full", "count_unit": "files",
                "triggers": ["non-py"], "skipped_files": [], "skipped_keywords": ["pinned_rule_tool"],
                "deselected_keywords": [],
                "gates": [{"name": "rules", "keyword": "pinned_rule_tool", "open": False}],
            }
        )
    )
    junit_xml = tmp_path / "junit.xml"
    junit_xml.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_rules" name="test_rule_shape" time="0.1"><failure message="x"/></testcase>'
        '<testcase classname="tests.test_rules" name="test_rules_with_the_Pinned_Rule_Tool" time="0.1">'
        '<failure message="x"/></testcase>'
        "</testsuite></testsuites>"
    )
    out = tmp_path / "ci-health.json"
    rc = command.main(
        ["report", "--junit", str(junit_xml), "--output", str(out), "--no-export", "--no-platform", "--quiet"],
        env={"MIRA_VITALS_CI_DIR": str(state), "PATH": os.environ["PATH"]},
    )
    assert rc == 0
    doc = json.loads(out.read_text())
    sel = doc["optimizations"]["selection"]
    assert sel["escapes"] == 1 and sel["escape_ids"] == ["tests/test_rules.py::test_rules_with_the_Pinned_Rule_Tool"]
    assert "skipped_keywords" not in sel and sel["gates_closed"] == ["rules"]
