"""Console rendering."""

from mira_vitals import report


def test_missing_values_render_as_not_available(snapshot_factory):
    document = snapshot_factory()
    document["tests"]["coverage_percent"] = None
    text = report.summary_text(document)
    assert "n/a" in text
    assert "0.0%" not in text, "an unmeasured coverage must not render as zero"


def test_absent_deltas_render_as_nothing_not_zero(snapshot_factory):
    text = report.summary_text(snapshot_factory())
    assert "(+0)" not in text


def test_a_failed_analyzer_is_called_out(snapshot_factory):
    document = snapshot_factory()
    document["lint"]["status"] = "error"
    document["lint"]["error"] = "ruff exploded"
    text = report.summary_text(document)
    assert "Incomplete measurement" in text
    assert "ruff exploded" in text


def test_an_unconfigured_section_is_not_an_alarm(snapshot_factory):
    """`skipped` is a configuration state; flagging it every run trains people
    to skim past the banner that matters."""
    document = snapshot_factory()
    document["tests"]["status"] = "skipped"
    text = report.summary_text(document)
    assert "Incomplete measurement" not in text
    assert "not configured in this run" in text


def test_small_movements_are_not_flagged(snapshot_factory):
    document = snapshot_factory()
    document["deltas"] = {"status": "ok", "values": {"aggregate_complexity": 1}}
    assert "no movement beyond the reporting thresholds" in report.changes_text(document, [])


def test_a_real_regression_is_flagged(snapshot_factory):
    document = snapshot_factory()
    document["deltas"] = {"status": "ok", "values": {"aggregate_complexity": 40}}
    assert "⚠" in report.changes_text(document, [])


def test_an_improvement_is_marked_as_such(snapshot_factory):
    document = snapshot_factory()
    document["deltas"] = {"status": "ok", "values": {"coverage_percent": 2.0}}
    assert "✓" in report.changes_text(document, [])


def test_symbol_changes_are_rendered_compactly(snapshot_factory):
    document = snapshot_factory()
    document["deltas"] = {"status": "ok", "values": {}}
    text = report.changes_text(
        document,
        [{"path": "src/app/handlers.py", "symbol": "token", "kind": "changed", "before": 14, "after": 28, "delta": 14}],
    )
    assert "token(): CC 14 → 28" in text


def test_the_console_does_not_print_every_symbol(snapshot_factory):
    document = snapshot_factory()
    document["hotspots"] = [
        {"path": "a.py", "symbol": f"f{i}", "line": i, "cyclomatic_complexity": 30} for i in range(200)
    ]
    text = report.hotspots_text(document, limit=10)
    assert text.count("CC ") <= 11
    assert "and 190 more" in text


def test_markdown_summary_records_provenance_and_schema(snapshot_factory):
    markdown = report.markdown_summary(snapshot_factory(), [])
    assert "## Code health" in markdown
    assert "schema v1" in markdown
    assert "authoring: undeclared" in markdown
