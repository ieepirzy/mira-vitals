"""Cardinality rules for the metrics pipeline.

The failure this guards against is silent and slow: an attribute added today
degrades a metrics backend over months, and by the time anyone notices, the
series is unusable and the cost is already paid.  So the rules are asserted
mechanically rather than left to review.
"""

import pytest

from mira_vitals import metrics, otel
from mira_vitals.metrics import CardinalityError


def test_forbidden_keys_are_rejected_as_metric_attributes():
    for key in metrics.FORBIDDEN_METRIC_ATTRIBUTES:
        with pytest.raises(CardinalityError, match="forbidden"):
            metrics.assert_bounded_attributes({key: "value"})


def test_commit_sha_is_specifically_forbidden():
    """The brief's named example. Named here too, so it cannot regress quietly."""
    with pytest.raises(CardinalityError, match="commit SHA"):
        metrics.assert_bounded_attributes({"vcs.ref.head.revision": "a" * 40})


def test_unknown_attributes_are_rejected_even_if_harmless_looking():
    with pytest.raises(CardinalityError, match="not in the allowed set"):
        metrics.assert_bounded_attributes({"code.health.author": "someone"})


def test_metric_attributes_are_bounded(snapshot_factory):
    attributes = metrics.metric_attributes(snapshot_factory())
    metrics.assert_bounded_attributes(attributes)
    assert set(attributes) <= set(metrics.ALLOWED_METRIC_ATTRIBUTES)


def test_ref_class_has_exactly_two_possible_values(snapshot_factory):
    """The bounded stand-in for the unbounded branch name."""
    default = snapshot_factory()
    other = snapshot_factory()
    other["run"]["ref_class"] = "other"
    values = {
        metrics.metric_attributes(default)["code.health.ref_class"],
        metrics.metric_attributes(other)["code.health.ref_class"],
    }
    assert values == {"default_branch", "other"}


def test_branch_name_never_reaches_metric_attributes(snapshot_factory):
    """A PR branch name is attacker-controlled and unbounded."""
    document = snapshot_factory()
    document["run"]["branch"] = "feature/attacker-chosen-" + "x" * 100
    assert document["run"]["branch"] not in metrics.metric_attributes(document).values()


def test_metric_resource_is_bounded_too(snapshot_factory):
    """The resource trap: resource attributes become dimensions in most backends."""
    resource = metrics.metric_resource_attributes(snapshot_factory())
    metrics.assert_bounded_attributes(resource, where="resource")
    for forbidden in ("vcs.ref.head.revision", "cicd.pipeline.run.id", "vcs.ref.head.name"):
        assert forbidden not in resource


def test_context_resource_does_carry_run_identity(snapshot_factory):
    """Logs and traces are where per-run identity belongs."""
    resource = metrics.context_resource_attributes(snapshot_factory())
    assert resource["vcs.ref.head.revision"] == "a" * 40
    assert resource["vcs.ref.head.name"] == "main"
    assert resource["cicd.pipeline.run.id"] == "1"


def test_every_registered_metric_resolves_from_the_snapshot(snapshot_factory):
    """The metric stream must be derived from the artifact, not recomputed."""
    document = snapshot_factory()
    document["summary"]["source_loc"] = 1651
    points = otel.build_metric_points(document)
    names = {point["name"] for point in points}
    # Each registered metric whose snapshot path holds a number must appear.
    for spec in metrics.METRICS:
        value = otel._dotted(document, spec["path"])
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            assert spec["name"] in names, f"{spec['name']} did not resolve from {spec['path']}"


def test_missing_values_are_omitted_not_zeroed(snapshot_factory):
    """A gap says 'not measured'; a zero says 'measured, and it was zero'."""
    document = snapshot_factory()
    document["tests"]["coverage_percent"] = None
    document["typing"]["errors"] = None
    points = {point["name"]: point["value"] for point in otel.build_metric_points(document)}
    assert "code.health.coverage" not in points
    assert "code.health.type.errors" not in points
    assert points["code.health.loc"] == 2377


def test_metric_names_match_the_registry(snapshot_factory):
    points = otel.build_metric_points(snapshot_factory())
    assert {point["name"] for point in points} <= metrics.METRIC_NAMES


def test_every_metric_point_carries_the_bounded_attribute_set(snapshot_factory):
    for point in otel.build_metric_points(snapshot_factory()):
        metrics.assert_bounded_attributes(point["attributes"])
