"""Deltas and comparability."""

from mira_vitals import deltas


def test_no_baseline_yields_none_not_zero(snapshot_factory):
    """Zero means 'measured and unchanged'; this is neither."""
    result = deltas.compute(snapshot_factory(), None)
    assert result["status"] == "unavailable"
    assert all(value is None for value in result["values"].values())
    assert result["complexity_growth_per_loc"] is None


def test_unchanged_metrics_yield_zero_not_none(snapshot_factory):
    current, baseline = snapshot_factory(), snapshot_factory()
    result = deltas.compute(current, baseline)
    assert result["status"] == "ok"
    assert result["values"]["aggregate_complexity"] == 0


def test_a_metric_missing_on_one_side_yields_none(snapshot_factory):
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["tests"]["coverage_percent"] = None
    assert deltas.compute(current, baseline)["values"]["coverage_percent"] is None


def test_differences_are_computed(snapshot_factory):
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["complexity"]["aggregate"] = 400
    baseline["summary"]["loc"] = 2000
    result = deltas.compute(current, baseline)
    assert result["values"]["aggregate_complexity"] == 40
    assert result["values"]["loc"] == 377


def test_growth_per_loc_is_suppressed_for_a_tiny_line_change(snapshot_factory):
    """The ratio is dominated by its denominator here."""
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["summary"]["source_loc"] = current["summary"]["source_loc"] - 3
    baseline["complexity"]["aggregate"] = 400
    assert deltas.compute(current, baseline)["complexity_growth_per_loc"] is None


def test_growth_per_loc_is_computed_for_a_real_change(snapshot_factory):
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["summary"]["source_loc"] = 1551  # +100 lines
    baseline["complexity"]["aggregate"] = 420  # +20 complexity
    assert deltas.compute(current, baseline)["complexity_growth_per_loc"] == 0.2


def test_growth_per_loc_handles_a_deletion(snapshot_factory):
    """Removing 100 lines and 20 complexity is still 0.2 per line."""
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["summary"]["source_loc"] = 1751
    baseline["complexity"]["aggregate"] = 460
    assert deltas.compute(current, baseline)["complexity_growth_per_loc"] == 0.2


def test_a_schema_change_makes_snapshots_incomparable(snapshot_factory):
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["schema_version"] = 0
    result = deltas.compute(current, baseline)
    assert result["status"] == "incomparable"
    assert "schema_version" in result["reason"]


def test_a_definition_change_makes_snapshots_incomparable(snapshot_factory):
    """The exact silent-redefinition failure this dataset must resist."""
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["definitions"] = {**baseline["definitions"], "percentile_method": "linear"}
    result = deltas.compute(current, baseline)
    assert result["status"] == "incomparable"
    assert "definitions" in result["reason"]


def test_a_type_checker_swap_makes_typing_deltas_incomparable(snapshot_factory):
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["typing"]["tool"] = "mypy"
    result = deltas.compute(current, baseline)
    assert result["status"] == "incomparable"
    assert "type checker" in result["reason"]


def test_a_python_upgrade_is_flagged(snapshot_factory):
    """A checker's error count moves with the interpreter it resolved."""
    current, baseline = snapshot_factory(), snapshot_factory()
    current["typing"]["environment"] = {"python_version": "3.13.0"}
    baseline["typing"]["environment"] = {"python_version": "3.12.0"}
    result = deltas.compute(current, baseline)
    assert result["status"] == "incomparable"
    assert "python" in result["reason"]


def test_incomparable_deltas_are_still_reported(snapshot_factory):
    """Suppressing them would hide a real discontinuity in the series."""
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["schema_version"] = 0
    baseline["complexity"]["aggregate"] = 400
    assert deltas.compute(current, baseline)["values"]["aggregate_complexity"] == 40


def test_a_path_change_makes_snapshots_incomparable(snapshot_factory):
    current, baseline = snapshot_factory(), snapshot_factory()
    baseline["target"]["paths"] = ["src", "tests"]
    assert deltas.compute(current, baseline)["status"] == "incomparable"


def _with_symbols(factory, entries):
    document = factory()
    document["symbols"] = [
        {"path": p, "symbol": s, "cyclomatic_complexity": c} for p, s, c in entries
    ]
    return document


def test_symbol_changes_track_a_function_getting_worse(snapshot_factory):
    current = _with_symbols(snapshot_factory, [("e.py", "token", 28)])
    baseline = _with_symbols(snapshot_factory, [("e.py", "token", 14)])
    changes = deltas.symbol_changes(current, baseline)
    assert changes == [
        {"path": "e.py", "symbol": "token", "kind": "changed", "before": 14, "after": 28, "delta": 14}
    ]


def test_symbol_changes_ignore_a_function_merely_moving_down_the_file(snapshot_factory):
    """Keyed on (path, symbol), not line number."""
    current = _with_symbols(snapshot_factory, [("e.py", "token", 28)])
    current["symbols"][0]["line"] = 900
    baseline = _with_symbols(snapshot_factory, [("e.py", "token", 28)])
    baseline["symbols"][0]["line"] = 683
    assert deltas.symbol_changes(current, baseline) == []


def test_symbol_changes_report_additions_and_removals(snapshot_factory):
    current = _with_symbols(snapshot_factory, [("e.py", "new_one", 12)])
    baseline = _with_symbols(snapshot_factory, [("e.py", "old_one", 20)])
    kinds = {c["symbol"]: c["kind"] for c in deltas.symbol_changes(current, baseline)}
    assert kinds == {"new_one": "added", "old_one": "removed"}


def test_symbol_changes_suppress_noise(snapshot_factory):
    current = _with_symbols(snapshot_factory, [("e.py", "f", 12)])
    baseline = _with_symbols(snapshot_factory, [("e.py", "f", 11)])
    assert deltas.symbol_changes(current, baseline, min_absolute=3) == []


def test_symbol_changes_without_a_baseline_are_empty(snapshot_factory):
    assert deltas.symbol_changes(snapshot_factory(), None) == []


def test_a_ruff_selection_change_makes_lint_deltas_incomparable(snapshot_factory):
    """`lint.total` means 'findings under this selection'. A different
    selection is a different metric, not a movement in the same one."""
    current, baseline = snapshot_factory(), snapshot_factory()
    current["lint"]["select"] = ["E4", "E7", "E9", "F", "W"]
    baseline["lint"]["select"] = ["E", "F"]
    result = deltas.compute(current, baseline)
    assert result["status"] == "incomparable"
    assert "ruff selection" in result["reason"]


def test_a_test_interpreter_change_makes_coverage_deltas_incomparable(snapshot_factory):
    current, baseline = snapshot_factory(), snapshot_factory()
    current["tests"]["python_version"] = "3.13"
    baseline["tests"]["python_version"] = "3.12"
    result = deltas.compute(current, baseline)
    assert result["status"] == "incomparable"
    assert "test interpreter" in result["reason"]
