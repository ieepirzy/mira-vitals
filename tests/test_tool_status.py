"""Analyzer failure must never look like a clean result."""

from mira_vitals.analyzers import base, ruff_lint, tests_cov, typecheck


def test_version_parsing_skips_an_update_warning(monkeypatch):
    """pyright prints an upgrade notice *before* its version.

    Taking the first line records "WARNING: there is a new pyright version
    available..." as the tool version -- corrupting the one field the dataset
    relies on to keep measurements comparable across time.
    """
    monkeypatch.setattr(
        base,
        "run",
        lambda command, timeout=60: base.Completed(
            returncode=0,
            stdout="WARNING: there is a new pyright version available (v1.1.408 -> v1.1.411).\npyright 1.1.408\n",
            stderr="",
            duration=0.0,
        ),
    )
    assert base.tool_version(["pyright", "--version"]) == "pyright 1.1.408"


def test_version_parsing_returns_none_rather_than_a_guess(monkeypatch):
    """An unknown version is a fact; a wrong one is corruption."""
    monkeypatch.setattr(
        base,
        "run",
        lambda command, timeout=60: base.Completed(returncode=0, stdout="no idea\n", stderr="", duration=0.0),
    )
    assert base.tool_version(["thing", "--version"]) is None


def test_a_missing_tool_yields_null_counts_not_zero(monkeypatch):
    monkeypatch.setattr(ruff_lint, "which", lambda name: None)
    monkeypatch.setattr(ruff_lint, "tool_version", lambda command: None)
    data, tool = ruff_lint.collect(["src"])
    assert tool.status == "unavailable"
    assert data["errors"] is None and data["total"] is None


def test_a_crashing_tool_yields_null_counts_not_zero(monkeypatch):
    monkeypatch.setattr(ruff_lint, "tool_version", lambda command: "ruff 0.15.8")
    monkeypatch.setattr(
        ruff_lint,
        "run",
        lambda command, cwd=None: base.Completed(returncode=2, stdout="", stderr="bad config", duration=0.1),
    )
    data, tool = ruff_lint.collect(["src"])
    assert tool.status == "error"
    assert "bad config" in tool.error
    assert data["errors"] is None


def test_ruff_findings_are_not_a_tool_failure(monkeypatch):
    """Exit code carries findings for many tools; --exit-zero separates them."""
    monkeypatch.setattr(ruff_lint, "tool_version", lambda command: "ruff 0.15.8")
    monkeypatch.setattr(
        ruff_lint,
        "run",
        lambda command, cwd=None: base.Completed(
            returncode=0,
            stdout='[{"code":"F401","fix":{"applicability":"safe"}},{"code":"D100"}]',
            stderr="",
            duration=0.1,
        ),
    )
    data, tool = ruff_lint.collect(["src"])
    assert tool.status == "ok"
    assert data["errors"] == 1  # F401 is in the blocking selection
    assert data["warnings"] == 1  # D100 is measured, not gated
    assert data["fixable"] == 1


def test_a_ruff_syntax_error_is_blocking_even_without_a_code(monkeypatch):
    """ruff reports syntax errors with code=None; a prefix test would drop them."""
    monkeypatch.setattr(ruff_lint, "tool_version", lambda command: "ruff 0.15.8")
    monkeypatch.setattr(
        ruff_lint,
        "run",
        lambda command, cwd=None: base.Completed(
            returncode=0, stdout='[{"code":null}]', stderr="", duration=0.1
        ),
    )
    data, _ = ruff_lint.collect(["src"])
    assert data["errors"] == 1
    assert data["by_rule"] == {"syntax-error": 1}


def test_missing_reports_are_never_zero_tests(tmp_path):
    """The property this test has always been about: absence is not a pass of zero.

    It previously also asserted `status == "error"`, which encoded a belief
    rather than a requirement -- and that belief was what made a cancelled test
    leg fail the whole lane (see
    `test_a_report_that_was_never_produced_is_skipped_not_broken`). The
    classification changed deliberately; the not-zero guarantee did not.
    """
    data, tool = tests_cov.collect(junit_path=str(tmp_path / "nope.xml"))
    assert tool.status == "skipped"
    assert data["passed"] is None
    assert data["total"] is None
    assert data["coverage_percent"] is None


def test_no_reports_configured_is_skipped_not_failed():
    data, tool = tests_cov.collect()
    assert tool.status == "skipped"
    assert data["status"] == "skipped"
    assert data["passed"] is None


def test_junit_counts_are_summed_across_suites(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite tests="10" failures="1" errors="1" skipped="2" time="3.5"/>'
        '<testsuite tests="5" failures="0" errors="0" skipped="0" time="1.5"/></testsuites>'
    )
    data, tool = tests_cov.collect(junit_path=str(report))
    assert tool.status == "ok"
    assert data["total"] == 15
    assert data["passed"] == 11  # 15 - 1 failure - 1 error - 2 skipped
    assert data["duration_seconds"] == 5.0


def test_coverage_xml_is_rescaled_to_a_percentage(tmp_path):
    report = tmp_path / "coverage.xml"
    report.write_text('<coverage line-rate="0.8918"></coverage>')
    data, _ = tests_cov.collect(coverage_path=str(report))
    assert data["coverage_percent"] == 89.18


def test_coverage_json_is_read_as_a_percentage(tmp_path):
    report = tmp_path / "coverage.json"
    report.write_text('{"totals": {"percent_covered": 89.18}}')
    data, _ = tests_cov.collect(coverage_path=str(report))
    assert data["coverage_percent"] == 89.18


def test_an_unknown_type_checker_is_unavailable_not_silently_skipped():
    data, tool = typecheck.collect(["src"], tool_name="pytype")
    assert tool.status == "unavailable"
    assert data["errors"] is None


def _snapshot_with_tools(factory, tools):
    document = factory()
    document["tools"] = tools
    return document


def test_a_missing_required_analyzer_fails_the_run(snapshot_factory):
    """Without this, a runner that lost radon emits an all-null snapshot,
    exports no complexity metrics, and exits 0 -- the series just stops."""
    from mira_vitals.cli import _analyzer_failures

    document = _snapshot_with_tools(
        snapshot_factory, {"radon": {"status": "unavailable", "error": "radon is not installed"}}
    )
    assert _analyzer_failures(document, type_checker="pyright")


def test_a_missing_configured_type_checker_fails_the_run(snapshot_factory):
    from mira_vitals.cli import _analyzer_failures

    document = _snapshot_with_tools(
        snapshot_factory, {"pyright": {"status": "unavailable", "error": "pyright is not installed"}}
    )
    assert _analyzer_failures(document, type_checker="pyright")


def test_an_unused_type_checker_being_absent_is_fine(snapshot_factory):
    """A repository on mypy must not fail because pyright is not installed."""
    from mira_vitals.cli import _analyzer_failures

    document = _snapshot_with_tools(
        snapshot_factory, {"pyright": {"status": "unavailable", "error": "not installed"}}
    )
    assert not _analyzer_failures(document, type_checker="mypy")


def test_an_unconfigured_optional_analyzer_does_not_fail_the_run(snapshot_factory):
    from mira_vitals.cli import _analyzer_failures

    document = _snapshot_with_tools(
        snapshot_factory,
        {"tests": {"status": "skipped", "error": None}, "security": {"status": "skipped", "error": None}},
    )
    assert not _analyzer_failures(document, type_checker="pyright")


def test_any_analyzer_error_fails_the_run(snapshot_factory):
    from mira_vitals.cli import _analyzer_failures

    document = _snapshot_with_tools(snapshot_factory, {"tests": {"status": "error", "error": "bad xml"}})
    assert _analyzer_failures(document, type_checker="pyright")


def test_the_test_interpreter_is_recorded(tmp_path):
    """Coverage differs between interpreters wherever a branch is
    version-gated, so the number has to name the interpreter that produced it."""
    report = tmp_path / "junit.xml"
    report.write_text('<testsuite tests="3" failures="0" errors="0" skipped="0" time="1"/>')
    data, _ = tests_cov.collect(junit_path=str(report), python_version="3.12")
    assert data["python_version"] == "3.12"


def test_an_unrecorded_test_interpreter_is_null_not_guessed(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text('<testsuite tests="3" failures="0" errors="0" skipped="0" time="1"/>')
    data, _ = tests_cov.collect(junit_path=str(report))
    assert data["python_version"] is None


def test_a_report_that_was_never_produced_is_skipped_not_broken(tmp_path):
    """A cancelled upstream test leg must not fail the whole lane.

    Found in review: with the reports absent the collector returned
    `error`, `_analyzer_failures` exited 2, and the documented degraded mode
    ("tests: skipped, structural measurement still lands") never happened.
    """
    data, tool = tests_cov.collect(
        junit_path=str(tmp_path / "junit.xml"), coverage_path=str(tmp_path / "coverage.xml")
    )
    assert tool.status == "skipped"
    assert data["status"] == "skipped"
    assert data["passed"] is None
    # The reason is kept, so "not produced" stays distinguishable from
    # "never configured" when reading the artifact months later.
    assert "not produced" in data["error"]


def test_a_report_that_exists_but_is_broken_is_still_an_error(tmp_path):
    """The other half of the distinction: a measurement we thought we had."""
    report = tmp_path / "junit.xml"
    report.write_text("this is not xml at all <<<")
    data, tool = tests_cov.collect(junit_path=str(report))
    assert tool.status == "error"
    assert data["status"] == "error"


def test_one_report_missing_and_one_present_keeps_what_was_read(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text('<testsuite tests="5" failures="0" errors="0" skipped="0" time="1"/>')
    data, tool = tests_cov.collect(
        junit_path=str(report), coverage_path=str(tmp_path / "nope.xml")
    )
    assert tool.status == "ok"
    assert data["passed"] == 5
    assert data["coverage_percent"] is None
    assert "not produced" in data["error"]
