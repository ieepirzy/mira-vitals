"""Snapshot -> OTEL mapping."""

from mira_vitals import otel


def test_analysis_event_carries_run_identity(snapshot_factory):
    event = otel.build_analysis_event(snapshot_factory())
    assert event["event.name"] == "code.health.analysis"
    assert event["commit_sha"] == "a" * 40
    assert event["observation_id"].startswith("sha256:")
    assert event["schema_version"] == 1


def test_analysis_event_does_not_carry_the_full_symbol_table(snapshot_factory):
    """One event per run, not one megabyte per run."""
    document = snapshot_factory()
    document["symbols"] = [{"path": f"m{i}.py", "symbol": f"f{i}"} for i in range(5000)]
    document["hotspots"] = [{"path": "a.py", "symbol": f"h{i}", "cyclomatic_complexity": 30} for i in range(100)]
    event = otel.build_analysis_event(document, max_hotspots=20)
    assert "symbols" not in event
    assert len(event["hotspots"]) == 20
    # The counts are still reported, so a reader knows what was elided.
    assert event["counts"]["symbols_total"] == 5000
    assert event["counts"]["hotspots_total"] == 100


def test_analysis_event_records_definitions(snapshot_factory):
    """An event must be interpretable without this repository."""
    event = otel.build_analysis_event(snapshot_factory())
    assert event["definitions"]["percentile_method"] == "nearest_rank"


def test_flatten_produces_only_scalars_and_scalar_arrays():
    out = {}
    otel._flatten("", {"a": {"b": 1}, "c": [1, 2], "d": [{"x": 1}], "e": None, "f": "s"}, out)
    assert out["a.b"] == 1
    assert out["c"] == [1, 2]
    assert isinstance(out["d"], str), "list of objects must serialize, not vanish"
    assert "e" not in out, "None must not become a string 'None'"
    assert out["f"] == "s"


def test_export_is_a_noop_without_an_endpoint(snapshot_factory):
    """Telemetry must never be a build dependency."""
    exporter = otel.Exporter()
    status = exporter.export(snapshot_factory())
    assert status["status"] == "skipped"
    assert status["error"] == "no OTLP endpoint configured"


def test_span_is_a_noop_without_a_tracer():
    exporter = otel.Exporter()
    with exporter.span("anything") as span:
        assert span is None
