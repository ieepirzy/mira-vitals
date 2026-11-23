"""Data map: `mira-vitals ci inputs` recording and the selector's use of it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mira_vitals.ci import inputs, select
from mira_vitals.ci.select import Change, Settings, decide

TESTS = [
    "tests/test_rules.py",
    "tests/test_docs.py",
    "tests/test_any.py",
    "tests/test_new.py",
]
MAP = {
    "tests/test_rules.py": ["rules/alerts/*", "tests/units/**"],
    "tests/test_docs.py": ["docs/*"],
    "tests/test_any.py": ["**"],
    # tests/test_new.py: not in the map (added after the recording)
}


#: What the recording saw: files read, directories listed.
SEEN = (
    {"rules/alerts/disk.yaml", "docs/guide.txt", "src/hashed/m.py"},
    {"tests/units", "tests/units/deep", "rules/alerts"},
)


def _d(changes, data_map=MAP, settings=None, seen=SEEN):
    return decide(
        settings or Settings(),
        changes,
        TESTS,
        {t: [] for t in TESTS},
        None,
        data_map,
        seen,
    )


# -- the selector with a map ----------------------------------------------------


def test_a_mapped_data_change_selects_its_readers_not_everything():
    r = _d([Change("modified", "rules/alerts/disk.yaml")])
    assert "non-py" not in r["triggers"] and r["triggers"] == []
    # its reader, the file that reads everything, and the unmapped file
    assert r["selected"] == [
        "tests/test_any.py",
        "tests/test_new.py",
        "tests/test_rules.py",
    ]


def test_a_listed_directory_covers_new_files_below_it():
    r = _d([Change("added", "tests/units/deep/new_case.yaml")])
    assert (
        "tests/test_rules.py" in r["selected"]
        and "tests/test_docs.py" not in r["selected"]
    )


def test_a_sibling_glob_does_not_reach_a_sub_directory():
    r = _d([Change("added", "rules/alerts/sub/x.yaml")])
    assert "tests/test_rules.py" not in r["selected"]


def test_renamed_data_selects_readers_of_either_path():
    r = _d([Change("renamed", "elsewhere/disk.yaml", "rules/alerts/disk.yaml")])
    assert "tests/test_rules.py" in r["selected"]


def test_a_gitlink_path_is_tried_as_a_directory():
    r = _d([Change("modified", "tests/units")])
    assert "tests/test_rules.py" in r["selected"]


def test_without_a_map_any_data_change_is_still_a_full_run():
    r = _d([Change("modified", "rules/alerts/disk.yaml")], data_map=None)
    assert r["triggers"] == ["non-py"]


def test_other_full_run_triggers_still_fire_with_a_map():
    r = _d(
        [
            Change("modified", "rules/alerts/disk.yaml"),
            Change("modified", "pyproject.toml"),
        ]
    )
    assert "deps" in r["triggers"]


def test_python_read_as_data_is_selected_through_the_map():
    data_map = {**MAP, "tests/test_rules.py": ["src/hashed/**"]}
    graph = {"src/hashed/m.py": [], **{t: [] for t in TESTS}}
    r = decide(
        Settings(),
        [Change("modified", "src/hashed/m.py")],
        TESTS,
        graph,
        None,
        data_map,
    )
    # the test hashing the sources, the one that reads everything, and the
    # unmapped one (nothing says what it reads now)
    assert r["triggers"] == [] and r["selected"] == [
        "tests/test_any.py",
        "tests/test_new.py",
        "tests/test_rules.py",
    ]


def test_ignored_files_select_nothing():
    r = _d([Change("modified", "README.md")])
    assert r["selected"] == [] and r["triggers"] == []


# -- recording: globs from a record ---------------------------------------------


def test_reads_become_sibling_globs_and_lists_become_subtrees():
    globs, why = inputs.to_globs(
        {"reads": ["a/b/c.yaml", "top.toml", "src/m.py"], "lists": ["d/e"]}
    )
    assert why is None
    assert globs == ["*", "a/b/*", "d/e/**", "src/*"]  # .py read as data counts


def test_globs_under_a_listed_subtree_are_pruned():
    globs, _ = inputs.to_globs(
        {
            "lists": ["conf", "conf/sub"],
            "reads": ["conf/a.yaml", "conf/sub/b.yaml", "conf2/c.yaml"],
        }
    )
    assert globs == ["conf/**", "conf2/*"]


@pytest.mark.parametrize(
    "record",
    [
        {"reads": ["a/x.yaml"], "spawned": ["subprocess.Popen: helm"]},
        {"reads": ["a/x.yaml"], "incomplete": ["call:tests/test_x.py::t"]},
        {"lists": ["."]},
    ],
)
def test_untrustworthy_records_read_everything(record):
    globs, why = inputs.to_globs(record)
    assert globs == ["**"] and why


# -- the plugin in a real pytest process ----------------------------------------


@pytest.fixture
def project(tmp_path):
    r = tmp_path / "proj"
    (r / "tests").mkdir(parents=True)
    (r / "conf/sub").mkdir(parents=True)
    (r / "data").mkdir()
    (r / "data/x.yaml").write_text("a: 1\n")
    (r / "conf/sub/y.yaml").write_text("b: 2\n")
    (r / "tests/test_reads.py").write_text(
        "import pathlib\n"
        "def test_read():\n"
        "    assert open('data/x.yaml').read()\n"
        "    assert list(pathlib.Path('conf').rglob('*.yaml'))\n"
        "    open('/dev/null').close()\n"  # outside the repository: not recorded
    )
    (r / "tests/test_spawns.py").write_text(
        "import subprocess, sys\ndef test_spawn():\n    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
    )
    (r / "tests/test_skips.py").write_text(
        "import pytest\ndef test_ok():\n    open('data/x.yaml').read()\n"
        "@pytest.mark.skip(reason='tool missing')\ndef test_skipped():\n    pass\n"
    )
    (r / "src").mkdir()
    (r / "src/hashed.py").write_text("X = 1\n")
    (r / "src/imported_mod.py").write_text("Y = 2\n")
    (r / "tests/test_hashes.py").write_text(
        "import hashlib, sys\nsys.path.insert(0, 'src')\nimport imported_mod\n"
        "def test_hash():\n    assert hashlib.sha256(open('src/hashed.py', 'rb').read()).hexdigest()\n"
    )
    (r / "tests/test_writes.py").write_text(
        "def test_write(tmp_path):\n    open('data/out.txt', 'w').write('x')\n"
    )
    return r


def _graph(tests):
    return {t: [] for t in tests}


def test_record_maps_each_test_file_by_what_it_read(project):
    tests = [
        "tests/test_reads.py",
        "tests/test_spawns.py",
        "tests/test_skips.py",
        "tests/test_writes.py",
    ]
    doc = inputs.record(
        str(project), tests, _graph(tests), python=sys.executable, jobs=2, timeout=120
    )
    t = doc["tests"]
    assert t["tests/test_reads.py"]["inputs"] == ["conf/**", "data/*"]
    assert "universal_because" not in t["tests/test_reads.py"]
    assert t["tests/test_spawns.py"]["inputs"] == ["**"]
    assert t["tests/test_spawns.py"]["universal_because"].startswith(
        "spawned a process"
    )
    assert t["tests/test_skips.py"]["inputs"] == ["**"]
    assert "incomplete" in t["tests/test_skips.py"]["universal_because"]
    # a write is not a read: the test read nothing (only its own .py)
    assert t["tests/test_writes.py"]["inputs"] == []
    assert all(len(e["code_digest"]) == 64 for e in t.values())


def test_source_read_as_data_is_recorded_but_imports_are_not(project):
    doc = inputs.record(
        str(project),
        ["tests/test_hashes.py"],
        _graph(["tests/test_hashes.py"]),
        timeout=120,
    )
    # src/hashed.py was opened by the test; src/imported_mod.py only imported
    assert doc["tests"]["tests/test_hashes.py"]["inputs"] == ["src/*"]
    rec = inputs._record_one(
        str(project), sys.executable, _plugin_dir(), "tests/test_hashes.py", [], 120
    )
    assert "src/hashed.py" in rec["reads"] and "src/imported_mod.py" not in rec["reads"]


def _plugin_dir():
    import tempfile

    d = tempfile.mkdtemp()
    shutil.copyfile(
        os.path.join(os.path.dirname(inputs.__file__), "inputs_plugin.py"),
        os.path.join(d, inputs.PLUGIN_MODULE + ".py"),
    )
    return d


def test_a_test_that_cannot_run_reads_everything(project):
    (project / "tests/test_broken.py").write_text("import nonexistent_module_xyz\n")
    doc = inputs.record(
        str(project),
        ["tests/test_broken.py"],
        _graph(["tests/test_broken.py"]),
        timeout=120,
    )
    assert doc["tests"]["tests/test_broken.py"]["inputs"] == ["**"]


def test_the_plugin_is_standard_library_only():
    path = os.path.join(os.path.dirname(inputs.__file__), "inputs_plugin.py")
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                f"import ast,sys; t=ast.parse(open({path!r}).read()); "
                "mods={a.name.split('.')[0] for n in ast.walk(t) if isinstance(n, ast.Import) for a in n.names} | "
                "{n.module.split('.')[0] for n in ast.walk(t) if isinstance(n, ast.ImportFrom) and n.module}; "
                "print(sorted(m for m in mods if m not in sys.stdlib_module_names and m != '__future__'))"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "[]"


# -- the map file: loading, staleness, the selector end to end ------------------


def test_load_map_rejects_bad_maps(tmp_path):
    p = tmp_path / "m.json"
    for bad in (
        {"version": 1, "tests": {}},  # an older format: re-record
        {"version": 2},
        {
            "version": 2,
            "tests": {
                "t.py": {"inputs": "a/*", "code_digest": "x", "reads": [], "lists": []}
            },
        },
        {
            "version": 2,
            "tests": {"t.py": {"inputs": ["a/*"], "reads": [], "lists": []}},
        },
        {
            "version": 2,
            "tests": {"t.py": {"inputs": ["a/*"], "code_digest": "x"}},
        },  # no reads/lists
    ):
        p.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            inputs.load_map(str(p))


def test_data_map_setting_must_be_a_plain_json_path():
    assert (
        Settings.from_table({"data_map": "mira-vitals-inputs.json"}).data_map
        == "mira-vitals-inputs.json"
    )
    for bad in ("../x.json", "/abs.json", "x.yaml", "a/../b.json", 3):
        with pytest.raises(ValueError):
            Settings.from_table({"data_map": bad})


needs_tools = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("ruff") is None, reason="git and ruff"
)


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _rev(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def mapped_repo(tmp_path):
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "rules").mkdir()
    (r / "docs").mkdir()
    (r / "rules/a.yaml").write_text("a: 1\n")
    (r / "docs/x.txt").write_text("x\n")
    (r / "tests/helper.py").write_text("RULES = 'rules'\n")
    (r / "tests/__init__.py").write_text("")
    (r / "tests/test_rules.py").write_text(
        "from tests.helper import RULES\n\ndef test_r():\n    open(RULES + '/a.yaml').read()\n"
    )
    (r / "tests/test_docs.py").write_text(
        "def test_d():\n    open('docs/x.txt').read()\n"
    )
    (r / "pyproject.toml").write_text("[tool.ruff]\n")
    tests = ["tests/test_docs.py", "tests/test_rules.py"]
    graph = select.ruff_graph(str(r), ["."])
    doc = inputs.record(str(r), tests, graph, timeout=120)
    inputs.write_map(doc, str(r / "mira-vitals-inputs.json"))
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "base")
    return r


def _ns(repo, base, head):
    return SimpleNamespace(
        repo=str(repo), base=base, head=head, mode="enforce", event="pull_request"
    )


TABLE = {"graph_sources": ["ruff"], "data_map": "mira-vitals-inputs.json"}


@needs_tools
def test_selector_runs_only_the_readers_of_a_changed_data_file(mapped_repo):
    r = mapped_repo
    base = _rev(r)
    (r / "rules/a.yaml").write_text("a: 2\n")
    _git(r, "commit", "-qam", "rule")
    doc = select.main_select(_ns(r, base, _rev(r)), {}, TABLE)
    assert doc["decision"] == "selected" and doc["run_files"] == ["tests/test_rules.py"]
    assert doc["data_map"]["current"] == 2 and doc["data_map"]["stale"] == 0


@needs_tools
def test_a_test_whose_code_changed_since_recording_runs_on_any_data_change(mapped_repo):
    r = mapped_repo
    base = _rev(r)
    # helper.py is in test_rules.py's import closure: its digest goes stale
    (r / "tests/helper.py").write_text("RULES = 'rules'  # moved\n")
    (r / "docs/x.txt").write_text("y\n")
    _git(r, "commit", "-qam", "helper + doc")
    doc = select.main_select(_ns(r, base, _rev(r)), {}, TABLE)
    assert doc["data_map"]["stale"] == 1
    assert doc["run_files"] == ["tests/test_docs.py", "tests/test_rules.py"]


@needs_tools
def test_changing_the_map_itself_is_a_full_run(mapped_repo):
    r = mapped_repo
    base = _rev(r)
    m = json.loads((r / "mira-vitals-inputs.json").read_text())
    m["tests"]["tests/test_rules.py"]["inputs"] = ["nothing/*"]
    (r / "mira-vitals-inputs.json").write_text(json.dumps(m))
    _git(r, "commit", "-qam", "map")
    doc = select.main_select(_ns(r, base, _rev(r)), {}, TABLE)
    assert "data-map" in doc["triggers"] and doc["mode"] == "full"


@needs_tools
def test_an_unreadable_map_fails_open(mapped_repo):
    r = mapped_repo
    base = _rev(r)
    (r / "mira-vitals-inputs.json").write_text("{not json")
    (r / "docs/x.txt").write_text("z\n")
    _git(r, "commit", "-qam", "broken map")
    doc = select.main_select(_ns(r, base, _rev(r)), {}, TABLE)
    assert doc["triggers"] == ["selector_error"] and doc["mode"] == "full"


# -- review regressions ---------------------------------------------------------
# Each of these failed before the fix it names.


def _rec(project, test_file, *extra):
    return inputs._record_one(
        str(project), sys.executable, _plugin_dir(), test_file, list(extra), 120
    )


def _data_project(root, n=0):
    (root / "tests").mkdir(parents=True)
    (root / "data").mkdir()
    for k in range(n):
        (root / f"data/f{k}.txt").write_text(str(k))
    return root


def test_reads_on_other_threads_are_all_recorded(tmp_path):
    """The re-entrancy guard is per thread: a process-global flag dropped
    other threads' opens made while the hook was busy."""
    r = _data_project(tmp_path / "proj", 200)
    (r / "tests/test_threads.py").write_text(
        "from concurrent.futures import ThreadPoolExecutor\n"
        "def test_t():\n"
        "    def rd(k):\n"
        "        with open(f'data/f{k}.txt') as h:\n"
        "            return h.read()\n"
        "    with ThreadPoolExecutor(16) as ex:\n"
        "        assert len(list(ex.map(rd, range(200)))) == 200\n"
    )
    rec = _rec(r, "tests/test_threads.py")
    assert {f"data/f{k}.txt" for k in range(200)} <= set(rec["reads"])


def test_a_checkout_under_a_directory_named_pytest_still_records(tmp_path):
    """The runner is recognised by pytest's install location, not by a
    `pytest` path segment the checkout itself may contain."""
    r = _data_project(tmp_path / "pytest" / "proj", 1)
    (r / "tests/test_p.py").write_text(
        "def test_p():\n    open('data/f0.txt').read()\n"
    )
    rec = _rec(r, "tests/test_p.py")
    assert "data/f0.txt" in rec["reads"]


def test_pytests_vendored_py_library_is_a_test_read(tmp_path):
    r = _data_project(tmp_path / "proj", 1)
    (r / "tests/test_py.py").write_text(
        "from _pytest._py.path import LocalPath\ndef test_l():\n    assert LocalPath('data/f0.txt').read()\n"
    )
    rec = _rec(r, "tests/test_py.py")
    assert "data/f0.txt" in rec["reads"]


def test_loader_get_data_of_a_data_file_is_recorded(tmp_path):
    r = _data_project(tmp_path / "proj")
    (r / "pkg").mkdir()
    (r / "pkg/__init__.py").write_text("")
    (r / "pkg/res.json").write_text("{}")
    (r / "tests/test_gd.py").write_text(
        "import pkgutil, sys\nsys.path.insert(0, '.')\n"
        "def test_g():\n    assert pkgutil.get_data('pkg', 'res.json') == b'{}'\n"
    )
    rec = _rec(r, "tests/test_gd.py")
    assert "pkg/res.json" in rec["reads"]
    assert "pkg/__init__.py" not in rec["reads"]  # importing it is the graph's business


def test_sqlite_databases_opened_by_path_are_recorded(tmp_path):
    r = _data_project(tmp_path / "proj")
    import sqlite3

    sqlite3.connect(str(r / "data/db.sqlite")).close()
    (r / "tests/test_sq.py").write_text(
        "import sqlite3\ndef test_s():\n    sqlite3.connect('data/db.sqlite').close()\n"
    )
    rec = _rec(r, "tests/test_sq.py")
    assert "data/db.sqlite" in rec["reads"]


def test_deselected_tests_make_the_record_incomplete(tmp_path):
    r = _data_project(tmp_path / "proj", 1)
    (r / "tests/test_k.py").write_text(
        "def test_kept():\n    pass\ndef test_dropped():\n    open('data/f0.txt').read()\n"
    )
    rec = _rec(r, "tests/test_k.py", "-k", "kept")
    assert any(x.startswith("deselected:") for x in rec["incomplete"])
    assert inputs.to_globs(rec)[0] == ["**"]


def test_a_symlinked_read_records_the_link_and_its_target(tmp_path):
    r = _data_project(tmp_path / "proj", 1)
    (r / "links").mkdir()
    os.symlink("../data/f0.txt", r / "links/alias.txt")
    (r / "tests/test_ln.py").write_text(
        "def test_l():\n    open('links/alias.txt').read()\n"
    )
    rec = _rec(r, "tests/test_ln.py")
    assert {"links/alias.txt", "data/f0.txt"} <= set(rec["reads"])


def test_a_symlink_out_of_the_repository_reads_everything(tmp_path):
    r = _data_project(tmp_path / "proj")
    (tmp_path / "outside.txt").write_text("x")
    os.symlink(str(tmp_path / "outside.txt"), r / "data/out.txt")
    (r / "tests/test_out.py").write_text(
        "def test_o():\n    open('data/out.txt').read()\n"
    )
    rec = _rec(r, "tests/test_out.py")
    assert inputs.to_globs(rec)[0] == ["**"]


def test_a_new_data_file_is_a_full_run():
    """Nothing recorded vouches for who reads a file that did not exist."""
    r = _d([Change("added", "rules/alerts/new.yaml")])
    assert r["triggers"] == ["data-added"]
    r = _d([Change("renamed", "rules/alerts/moved.yaml", "rules/alerts/disk.yaml")])
    assert "data-added" in r["triggers"]


def test_a_data_file_no_recording_saw_is_a_full_run():
    # docs/other.txt matches test_docs' docs/* glob, but nobody read it and
    # docs/ was never listed: a stat probe or C code may read it.
    r = _d([Change("modified", "docs/other.txt")])
    assert r["triggers"] == ["data-unseen"]


def test_editing_a_recorded_read_stays_selective():
    r = _d(
        [
            Change("modified", "docs/guide.txt"),
            Change("modified", "tests/units/deep/case.yaml"),
        ]
    )
    assert r["triggers"] == []
    assert (
        "tests/test_docs.py" in r["selected"] and "tests/test_rules.py" in r["selected"]
    )


def test_never_feeds_globs_are_exempt_from_the_fail_safe():
    r = _d([Change("added", "docs/NOTES.md")])  # **/*.md is a default ignore glob
    assert r["triggers"] == []


@pytest.fixture
def subdir_repo(tmp_path):
    """A checkout whose project (and --repo) is a sub-directory."""
    top = tmp_path / "mono"
    r = top / "proj"
    (r / "tests").mkdir(parents=True)
    (r / "rules").mkdir()
    (top / "other").mkdir()
    (top / "other/x.txt").write_text("x\n")
    (r / "rules/a.yaml").write_text("a: 1\n")
    (r / "tests/__init__.py").write_text("")
    (r / "tests/test_rules.py").write_text(
        "def test_r():\n    open('rules/a.yaml').read()\n"
    )
    (r / "tests/test_none.py").write_text("def test_n():\n    pass\n")
    (r / "pyproject.toml").write_text("[tool.ruff]\n")
    tests = ["tests/test_none.py", "tests/test_rules.py"]
    doc = inputs.record(str(r), tests, select.ruff_graph(str(r), ["."]), timeout=120)
    inputs.write_map(doc, str(r / "mira-vitals-inputs.json"))
    _git(top, "init", "-q", "-b", "main")
    _git(top, "add", ".")
    _git(top, "commit", "-qm", "base")
    return top, r


@needs_tools
def test_a_sub_directory_project_selects_by_its_own_relative_paths(subdir_repo):
    top, r = subdir_repo
    base = _rev(top)
    (r / "rules/a.yaml").write_text("a: 2\n")
    _git(top, "commit", "-qam", "rule")
    doc = select.main_select(_ns(r, base, _rev(top)), {}, TABLE)
    assert doc["triggers"] == [] and doc["run_files"] == ["tests/test_rules.py"]


@needs_tools
def test_a_sub_directory_project_still_sees_its_map_change(subdir_repo):
    top, r = subdir_repo
    base = _rev(top)
    m = json.loads((r / "mira-vitals-inputs.json").read_text())
    m["tests"]["tests/test_rules.py"]["inputs"] = ["nothing/*"]
    (r / "mira-vitals-inputs.json").write_text(json.dumps(m))
    _git(top, "commit", "-qam", "map")
    doc = select.main_select(_ns(r, base, _rev(top)), {}, TABLE)
    assert "data-map" in doc["triggers"] and doc["mode"] == "full"


@needs_tools
def test_a_change_outside_the_sub_directory_is_a_full_run(subdir_repo):
    top, r = subdir_repo
    base = _rev(top)
    (top / "other/x.txt").write_text("y\n")
    _git(top, "commit", "-qam", "outside")
    doc = select.main_select(_ns(r, base, _rev(top)), {}, TABLE)
    assert doc["triggers"] == ["outside-root"] and doc["mode"] == "full"
