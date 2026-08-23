"""Bounded-cardinality metric registry.

This module is the single place that decides *what may become an OTEL metric*
and *what may become a metric dimension*.  Everything else derives from it, and
the test suite asserts against it, so widening cardinality requires editing
this file deliberately rather than adding an attribute somewhere downstream.

The rule
--------
A metric backend stores one time series per distinct combination of metric name
and attribute values, and keeps it for the retention period whether or not it
is ever written to again.  So the cost of an attribute is not the cost of one
label -- it is a multiplier on every series it touches, paid forever.  A commit
SHA as a dimension does not add a label; it adds an unbounded, monotonically
growing family of series that no backend reclaims.

Hence: **metric attributes must come from a closed set of values known in
advance.**  Repository identity qualifies (we have thirteen).  Branch name does
not (every PR invents one).  Commit SHA, run ID, PR number, file path, function
name and model version emphatically do not.

The resource trap
-----------------
This is the part that is easy to get wrong after doing the attribute part
right.  OTEL *resource* attributes are not free of this cost: most backends
project them onto every metric the resource produces (Prometheus turns them
into target labels; several OTLP-native stores treat them as ordinary
dimensions).  Putting ``vcs.ref.head.revision`` on the resource because "it's
not a metric attribute" reintroduces exactly the explosion the attribute rule
prevents.

So this module exposes *two* resource shapes:
:func:`metric_resource_attributes` -- bounded, for the metrics pipeline -- and
:func:`context_resource_attributes` -- rich, for logs/events and traces, where
per-run identity is the entire point and the storage model is designed for it.

Correlation without cardinality
-------------------------------
Per-commit, per-run and per-symbol detail is not lost; it is routed to the
signals built for it.  The canonical JSON artifact holds everything; the
structured event holds the run-level record with full identity; traces hold
timing with full identity.  The metric stream holds only what belongs in a time
series, and joins to the rest through the bounded dimensions plus timestamp.
"""

from __future__ import annotations

from typing import Any

#: Metric attribute keys permitted on *any* code-health metric.  Every one is
#: bounded by construction; the justification is recorded beside it because
#: "why is this bounded?" is the question a reviewer needs answered.
ALLOWED_METRIC_ATTRIBUTES: dict[str, str] = {
    # Standard OTEL VCS convention.  Bounded by the number of repositories.
    "vcs.repository.url.full": "one value per repository",
    # Custom, because no standard convention expresses "is this the canonical
    # branch?".  Exactly two values, ever.  The branch *name* is deliberately
    # not used: on pull-request runs it is attacker-controlled and unbounded.
    "code.health.ref_class": "exactly two values: default_branch | other",
    # Bounded by the number of languages analyzed.
    "code.health.language": "one value per analyzed language",
    # Which analyzer produced a tool-scoped metric.  Bounded by the registry
    # below.
    "code.health.tool": "one value per configured analyzer",
}

#: Resource keys permitted on the *metrics* pipeline.  Same reasoning as above:
#: a resource attribute is a dimension in most backends.
ALLOWED_METRIC_RESOURCE_ATTRIBUTES: dict[str, str] = {
    "service.name": "repository name; bounded by repository count",
    "service.namespace": "fixed constant for this system",
    "telemetry.sdk.name": "set by the SDK; bounded",
    "telemetry.sdk.language": "set by the SDK; bounded",
    "telemetry.sdk.version": "set by the SDK; bounded by SDK releases",
}

#: Keys that must never appear as a metric attribute or metric resource
#: attribute.  Enumerated rather than merely implied, so the guard can name the
#: offender and the reason in the failure.
FORBIDDEN_METRIC_ATTRIBUTES: dict[str, str] = {
    "vcs.ref.head.revision": "commit SHA: unbounded and monotonically growing",
    "vcs.ref.head.name": "branch name: unbounded across pull requests",
    "vcs.change.id": "pull-request id: unbounded",
    "cicd.pipeline.run.id": "CI run id: unbounded",
    "code.health.file": "file path: unbounded",
    "code.health.symbol": "function name: unbounded",
    "code.health.agent.run_id": "agent run id: unbounded",
    "code.health.model": "model identifier: unbounded over time",
    "enduser.id": "identity: unbounded, and personal data",
}


class CardinalityError(ValueError):
    """An attribute set violated the cardinality rules."""


#: The exported metric instruments.  ``kind`` drives instrument selection:
#: ``gauge`` for a level measured once per run (an observation of the codebase
#: as it stands), never a counter -- these are not monotonic and summing them
#: across runs is meaningless.
#:
#: ``path`` is the dotted location of the value inside the canonical snapshot,
#: which keeps the metric stream provably derived from the artifact rather than
#: separately computed.
METRICS: tuple[dict[str, Any], ...] = (
    {"name": "code.health.loc", "path": "summary.loc", "unit": "{line}", "kind": "gauge",
     "description": "Physical lines of code, including comments and blanks."},
    {"name": "code.health.source_loc", "path": "summary.source_loc", "unit": "{line}", "kind": "gauge",
     "description": "Source lines of code, excluding comments and blank lines."},
    {"name": "code.health.files", "path": "summary.files", "unit": "{file}", "kind": "gauge",
     "description": "Number of analyzed source files."},
    {"name": "code.health.functions", "path": "summary.functions", "unit": "{function}", "kind": "gauge",
     "description": "Number of functions, methods and closures analyzed."},

    {"name": "code.health.complexity.total", "path": "complexity.aggregate", "unit": "{branch}", "kind": "gauge",
     "description": "Sum of cyclomatic complexity over all analyzed functions."},
    {"name": "code.health.complexity.mean", "path": "complexity.mean", "unit": "{branch}", "kind": "gauge",
     "description": "Arithmetic mean cyclomatic complexity per function."},
    {"name": "code.health.complexity.p50", "path": "complexity.p50", "unit": "{branch}", "kind": "gauge",
     "description": "50th percentile cyclomatic complexity (nearest-rank)."},
    {"name": "code.health.complexity.p90", "path": "complexity.p90", "unit": "{branch}", "kind": "gauge",
     "description": "90th percentile cyclomatic complexity (nearest-rank)."},
    {"name": "code.health.complexity.p95", "path": "complexity.p95", "unit": "{branch}", "kind": "gauge",
     "description": "95th percentile cyclomatic complexity (nearest-rank)."},
    {"name": "code.health.complexity.max", "path": "complexity.max", "unit": "{branch}", "kind": "gauge",
     "description": "Highest cyclomatic complexity of any analyzed function."},
    {"name": "code.health.complexity.density", "path": "complexity.density_per_kloc", "unit": "{branch}/kLOC",
     "kind": "gauge",
     "description": "Aggregate cyclomatic complexity per 1000 source lines."},

    {"name": "code.health.functions.high_complexity", "path": "complexity.functions_gt_10", "unit": "{function}",
     "kind": "gauge", "description": "Functions with cyclomatic complexity above 10."},

    {"name": "code.health.maintainability.index", "path": "maintainability.mean_index", "unit": "1", "kind": "gauge",
     "description": "Unweighted mean of radon's per-file maintainability index."},

    {"name": "code.health.lint.issues", "path": "lint.total", "unit": "{issue}", "kind": "gauge",
     "description": "Lint findings across the analyzed paths."},
    {"name": "code.health.type.errors", "path": "typing.errors", "unit": "{issue}", "kind": "gauge",
     "description": "Type-checker errors."},
    {"name": "code.health.type.warnings", "path": "typing.warnings", "unit": "{issue}", "kind": "gauge",
     "description": "Type-checker warnings."},

    {"name": "code.health.test.passed", "path": "tests.passed", "unit": "{test}", "kind": "gauge",
     "description": "Passing tests in the run that produced the consumed report."},
    {"name": "code.health.test.failed", "path": "tests.failed", "unit": "{test}", "kind": "gauge",
     "description": "Failing tests."},
    {"name": "code.health.test.skipped", "path": "tests.skipped", "unit": "{test}", "kind": "gauge",
     "description": "Skipped tests."},
    {"name": "code.health.coverage", "path": "tests.coverage_percent", "unit": "%", "kind": "gauge",
     "description": "Line coverage percentage."},
)

METRIC_NAMES: frozenset[str] = frozenset(m["name"] for m in METRICS)


def assert_bounded_attributes(attributes: dict[str, Any], *, where: str = "metric") -> None:
    """Raise :class:`CardinalityError` unless every key is a permitted one.

    This is a real guard, not documentation: it runs on the actual attribute
    dict handed to the exporter, so a future edit that adds ``commit_sha`` to a
    metric fails loudly at the point of export instead of quietly degrading a
    backend over the following months.
    """
    allowed = ALLOWED_METRIC_ATTRIBUTES if where == "metric" else ALLOWED_METRIC_RESOURCE_ATTRIBUTES
    for key in attributes:
        if key in FORBIDDEN_METRIC_ATTRIBUTES:
            raise CardinalityError(
                f"{where} attribute {key!r} is forbidden: {FORBIDDEN_METRIC_ATTRIBUTES[key]}"
            )
        if key not in allowed:
            raise CardinalityError(
                f"{where} attribute {key!r} is not in the allowed set "
                f"({sorted(allowed)}); if it is genuinely bounded, add it to "
                f"metrics.py with a justification"
            )


def metric_attributes(snapshot: dict[str, Any]) -> dict[str, str]:
    """The bounded dimensions carried by every code-health metric."""
    run = snapshot["run"]
    attributes = {
        "code.health.ref_class": run["ref_class"],
        "code.health.language": snapshot["target"]["language"],
    }
    repository_url = run.get("repository_url")
    if repository_url:
        attributes["vcs.repository.url.full"] = repository_url
    assert_bounded_attributes(attributes, where="metric")
    return attributes


def metric_resource_attributes(snapshot: dict[str, Any]) -> dict[str, str]:
    """Resource for the metrics pipeline: bounded, deliberately sparse."""
    attributes = {
        "service.name": snapshot["run"]["repository"],
        "service.namespace": "code-health",
    }
    assert_bounded_attributes(attributes, where="resource")
    return attributes


def context_resource_attributes(snapshot: dict[str, Any]) -> dict[str, str]:
    """Resource for logs/events and traces: full run identity.

    Unbounded values are correct here.  A log or span store is built to hold one
    record per run with its own identity; that is what makes it the right home
    for the correlation keys the metric stream must not carry.  Keys follow the
    OpenTelemetry VCS and CICD semantic conventions, which are still marked
    experimental upstream -- noted so a future rename is recognised as an
    upstream change rather than a redefinition of our data.
    """
    run = snapshot["run"]
    ci = snapshot["ci"]
    attributes: dict[str, str] = {
        "service.name": run["repository"],
        "service.namespace": "code-health",
    }
    optional = {
        "vcs.repository.url.full": run.get("repository_url"),
        "vcs.ref.head.name": run.get("branch"),
        "vcs.ref.head.revision": run.get("commit_sha"),
        "vcs.ref.head.type": "branch" if run.get("branch") else None,
        "vcs.ref.base.name": run.get("base_ref"),
        "vcs.change.id": run.get("change_id"),
        "cicd.pipeline.name": ci.get("workflow"),
        "cicd.pipeline.run.id": ci.get("run_id"),
        "cicd.pipeline.task.name": ci.get("job"),
    }
    attributes.update({k: str(v) for k, v in optional.items() if v})
    return attributes
