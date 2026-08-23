"""Normalization: percentiles, denominators, and missing-vs-zero."""

from mira_vitals import normalize
from mira_vitals.normalize import percentile, summarize_complexity


def symbols(values):
    return [
        {"path": "m.py", "symbol": f"f{i}", "cyclomatic_complexity": v, "line": i}
        for i, v in enumerate(values)
    ]


def test_percentile_is_nearest_rank_and_returns_observed_values():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 100]
    for q in (10, 50, 90, 95, 100):
        assert percentile(values, q) in values, "interpolation would invent a value"
    assert percentile(values, 50) == 5
    assert percentile(values, 90) == 9
    assert percentile(values, 100) == 100


def test_percentile_of_empty_sample_is_none_not_zero():
    assert percentile([], 95) is None


def test_percentile_of_single_value():
    assert percentile([7], 50) == 7
    assert percentile([7], 95) == 7


def test_aggregates_match_hand_computation():
    result = summarize_complexity(symbols([1, 2, 3, 12, 16, 21]), source_loc=1000)
    assert result["aggregate"] == 55
    assert result["mean"] == 55 / 6
    assert result["max"] == 21
    assert result["min"] == 1
    assert result["functions_gt_10"] == 3
    assert result["functions_gt_15"] == 2
    assert result["functions_gt_20"] == 1
    assert result["high_complexity_fraction"] == 3 / 6
    assert result["density_per_kloc"] == 55.0


def test_density_is_suppressed_for_a_tiny_codebase():
    """A 40-line repository's density swings by hundreds and predicts nothing."""
    result = summarize_complexity(symbols([1, 2]), source_loc=40)
    assert result["density_per_kloc"] is None


def test_density_is_computed_once_the_codebase_is_big_enough():
    result = summarize_complexity(symbols([1, 2]), source_loc=1000)
    assert result["density_per_kloc"] == 3.0


def test_density_is_none_when_loc_is_unknown():
    assert summarize_complexity(symbols([1, 2]), source_loc=None)["density_per_kloc"] is None


def test_no_symbols_yields_nulls_not_zeros():
    """An analyzer that produced nothing must not read as a perfect score."""
    result = summarize_complexity([], source_loc=1000)
    assert result["aggregate"] is None
    assert result["mean"] is None
    assert result["max"] is None
    assert result["functions_gt_10"] is None
    assert result["high_complexity_fraction"] is None


def test_values_are_not_rounded_before_storage():
    result = summarize_complexity(symbols([1, 1, 1]), source_loc=777)
    assert result["density_per_kloc"] == 3 / (777 / 1000)


def test_hotspots_are_ranked_and_bounded():
    ranked = normalize.hotspots(symbols([5, 30, 12, 25, 11]), limit=2)
    assert [s["cyclomatic_complexity"] for s in ranked] == [30, 25]


def test_hotspots_exclude_functions_below_threshold():
    assert normalize.hotspots(symbols([1, 2, 10])) == []


def test_maintainability_mean_is_unweighted_over_files():
    result = normalize.build_maintainability({"a.py": {"mi": 100.0}, "b.py": {"mi": 50.0}}, "ok")
    assert result["mean_index"] == 75.0
    assert result["files_measured"] == 2


def test_maintainability_of_nothing_is_none():
    result = normalize.build_maintainability({}, "error")
    assert result["mean_index"] is None
    assert result["status"] == "error"
