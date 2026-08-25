"""The collector against this package, for real.

Guards the wiring that unit tests cannot: that the analyzers actually run,
that the artifact validates, and that the aggregates are reproducible from the
detail the artifact preserves.

Ported from origo, where these ran against the vendored copy of this code.
They are the tests that catch a collector which unit-tests cleanly and then
produces a wrong document, so they belong with the collector rather than with
any one of its consumers.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

from mira_vitals import config as mv_config
from mira_vitals import schema

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _available(executable: str) -> bool:
    if shutil.which(executable) is not None:
        return True
    return subprocess.run(
        [sys.executable, "-m", executable, "--version"], capture_output=True, check=False
    ).returncode == 0


# Every analyzer the collector treats as required, not just radon. The origo
# copy guarded on radon alone, which was wrong in a way that only shows up in
# a partial environment: with radon present and ruff or the type checker
# absent, the collector exits 2 by design ("required analyzer unavailable"),
# the shared fixture's returncode assertion fails, and a developer who simply
# has not installed the full analyzer set sees twelve failures rather than a
# skip. The type checker is read from this repository's own configuration
# rather than hardcoded, because that is what the collector will actually
# invoke.
_REQUIRED_TOOLS = ("radon", "ruff", mv_config.load(REPO_ROOT).type_checker)
_MISSING_TOOLS = [tool for tool in _REQUIRED_TOOLS if not _available(tool)]

pytestmark = pytest.mark.skipif(
    bool(_MISSING_TOOLS),
    reason=f"required analyzers not installed: {', '.join(_MISSING_TOOLS)}",
)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    output = tmp_path_factory.mktemp("mv") / "code-health.json"
    result = subprocess.run(
        [sys.executable, "-m", "mira_vitals", "--output", str(output), "--quiet"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"collector failed: {result.stderr[-2000:]}"
    with open(output, encoding="utf-8") as handle:
        return json.load(handle)


def test_the_artifact_validates(snapshot):
    schema.validate(snapshot)


def test_the_artifact_measures_this_package(snapshot):
    assert snapshot["target"]["paths"] == ["src/mira_vitals"]
    # Lower bounds rather than the exact counts the origo copy asserted: this
    # package gains modules, and a test that fails on every new file trains
    # people to update the number without reading why it moved.
    assert snapshot["summary"]["files"] >= 15
    assert snapshot["summary"]["functions"] > 80


def test_per_symbol_detail_is_preserved(snapshot):
    """Aggregates alone cannot answer the questions this dataset is for."""
    assert len(snapshot["symbols"]) == snapshot["summary"]["functions"]
    collect = [
        s for s in snapshot["symbols"]
        if s["symbol"] == "collect" and s["path"] == "src/mira_vitals/context.py"
    ]
    assert collect, "context.collect should appear in the symbol table"
    assert collect[0]["cyclomatic_complexity"] > 10


def test_unavailable_metrics_are_null_not_fabricated(snapshot):
    """radon reports MI per file and no nesting depth at all."""
    for symbol in snapshot["symbols"]:
        assert symbol["maintainability_index"] is None
        assert symbol["nesting_depth"] is None


def test_class_blocks_are_not_in_the_symbol_table(snapshot):
    """The double-count trap, asserted against real radon output."""
    assert {s["symbol_type"] for s in snapshot["symbols"]} <= {"function", "method"}


def test_the_aggregate_equals_the_sum_of_the_symbols(snapshot):
    """The headline number must be reproducible from the preserved detail."""
    total = sum(s["cyclomatic_complexity"] for s in snapshot["symbols"])
    assert snapshot["complexity"]["aggregate"] == total


def test_tool_versions_are_recorded(snapshot):
    radon = snapshot["tools"]["radon"]
    assert radon["status"] == "ok"
    assert radon["version"], "a metric without its tool version is not comparable over time"
    assert "WARNING" not in (radon["version"] or "")


def test_unconfigured_sections_are_skipped_not_zero(snapshot):
    assert snapshot["tests"]["status"] == "skipped"
    assert snapshot["tests"]["passed"] is None
    assert snapshot["tests"]["coverage_percent"] is None


def test_deltas_without_a_baseline_are_unavailable(snapshot):
    assert snapshot["deltas"]["status"] == "unavailable"
    assert all(value is None for value in snapshot["deltas"]["values"].values())


def test_provenance_is_undeclared_by_default(snapshot):
    assert snapshot["provenance"]["authoring_mode"] is None


def test_a_pr_run_is_not_marked_canonical(snapshot, tmp_path):
    """Default-branch runs are the historical series; proposals are not."""
    output = tmp_path / "pr.json"
    env = {
        **os.environ,
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_HEAD_REF": "feature/x",
        "GITHUB_BASE_REF": "main",
        "GITHUB_REPOSITORY": "ieepirzy/mira-vitals",
    }
    subprocess.run(
        [sys.executable, "-m", "mira_vitals", "--output", str(output), "--quiet"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True,
    )
    with open(output, encoding="utf-8") as handle:
        document = json.load(handle)
    assert document["run"]["canonical"] is False
    assert document["run"]["ref_class"] == "other"
    assert document["run"]["is_default_branch"] is False


def test_the_same_commit_produces_the_same_observation_id(snapshot, tmp_path):
    """Repeated analysis must be dedupable by the backend."""
    output = tmp_path / "again.json"
    subprocess.run(
        [sys.executable, "-m", "mira_vitals", "--output", str(output), "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    with open(output, encoding="utf-8") as handle:
        assert json.load(handle)["run"]["observation_id"] == snapshot["run"]["observation_id"]
