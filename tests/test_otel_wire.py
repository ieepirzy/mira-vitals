"""End-to-end cardinality check against the real OTEL SDK.

The unit tests assert that the *builders* produce bounded attributes.  This one
asserts the property that actually matters -- that nothing unbounded reaches
the metrics pipeline once the SDK has assembled resource, scope and data point
together.  It is the resource trap that makes the distinction worth paying for:
a resource attribute is invisible in the attribute dict and still becomes a
dimension in most backends.

Skipped when the SDK is absent, because the collector must work without it.
"""

import pytest

from mira_vitals import metrics, otel

pytest.importorskip("opentelemetry.sdk.metrics")

COMMIT = "43af83ee43b47825521d3e3a01b20ec640c2d929"
BRANCH = "claude/code-health-telemetry-ci-16h0xa"


@pytest.fixture
def identified(snapshot_factory):
    document = snapshot_factory()
    document["run"]["commit_sha"] = COMMIT
    document["run"]["branch"] = BRANCH
    document["ci"]["run_id"] = "18446744073709551615"
    document["hotspots"] = [
        {"path": "src/app/handlers.py", "symbol": "authorize", "line": 683, "cyclomatic_complexity": 29}
    ]
    return document


def _collect_metrics(document):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.resources import Resource

    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create(metrics.metric_resource_attributes(document)),
        metric_readers=[reader],
    )
    meter = provider.get_meter("code-health", "0.1.0")
    for point in otel.build_metric_points(document):
        meter.create_gauge(point["name"], unit=point["unit"], description=point["description"]).set(
            point["value"], point["attributes"]
        )
    data = reader.get_metrics_data()
    provider.shutdown()
    return data


def test_no_unbounded_identifier_reaches_the_metrics_pipeline(identified):
    """Resource attributes included -- they become dimensions downstream."""
    data = _collect_metrics(identified)
    forbidden = {COMMIT, BRANCH, "18446744073709551615", "src/mira_vitals/cli.py", "collect_snapshot"}

    seen_values = set()
    seen_keys = set()
    for resource_metric in data.resource_metrics:
        seen_values.update(str(v) for v in resource_metric.resource.attributes.values())
        seen_keys.update(resource_metric.resource.attributes)
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                for point in metric.data.data_points:
                    seen_values.update(str(v) for v in point.attributes.values())
                    seen_keys.update(point.attributes)

    leaked = forbidden & seen_values
    assert not leaked, f"unbounded value(s) reached the metrics pipeline: {leaked}"
    for key in seen_keys:
        assert key not in metrics.FORBIDDEN_METRIC_ATTRIBUTES, f"forbidden key {key} present"


def test_the_metrics_that_should_be_there_are(identified):
    data = _collect_metrics(identified)
    names = {
        metric.name
        for resource_metric in data.resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
    }
    assert "code.health.complexity.total" in names
    assert "code.health.coverage" in names
    assert names <= metrics.METRIC_NAMES


def test_bounded_dimensions_are_present_so_series_are_still_separable(identified):
    data = _collect_metrics(identified)
    attributes = [
        dict(point.attributes)
        for resource_metric in data.resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
        for point in metric.data.data_points
    ]
    assert attributes, "no data points were produced"
    for attribute_set in attributes:
        assert attribute_set["code.health.ref_class"] in {"default_branch", "other"}
        assert attribute_set["vcs.repository.url.full"] == "https://github.com/ieepirzy/mira-vitals"
