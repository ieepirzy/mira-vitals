"""Comparison against a baseline snapshot.

Deltas are computed only between snapshots that are actually comparable.  Two
runs that measured different paths, or were produced by different schema
versions, or used different type checkers, do not yield a meaningful
difference in ``typing.errors`` -- and a delta presented without that caveat is
worse than no delta, because it looks like a regression.

An absent baseline yields ``None`` for every delta, never ``0``.  Zero means
"measured, and unchanged".
"""

from __future__ import annotations

from typing import Any

from .schema import DEFINITIONS


def _get(document: dict[str, Any], path: str) -> Any:
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _difference(current: dict[str, Any], baseline: dict[str, Any], path: str) -> float | None:
    new = _get(current, path)
    old = _get(baseline, path)
    if isinstance(new, bool) or isinstance(old, bool):
        return None
    if not isinstance(new, (int, float)) or not isinstance(old, (int, float)):
        # One side was never measured.  The difference is unknown, and saying
        # so is the point.
        return None
    return new - old


#: Delta fields, each named for the snapshot path it differences.
DELTA_PATHS: dict[str, str] = {
    "loc": "summary.loc",
    "source_loc": "summary.source_loc",
    "files": "summary.files",
    "functions": "summary.functions",
    "aggregate_complexity": "complexity.aggregate",
    "mean_complexity": "complexity.mean",
    "p50_complexity": "complexity.p50",
    "p90_complexity": "complexity.p90",
    "p95_complexity": "complexity.p95",
    "max_complexity": "complexity.max",
    "functions_gt_10": "complexity.functions_gt_10",
    "functions_gt_15": "complexity.functions_gt_15",
    "functions_gt_20": "complexity.functions_gt_20",
    "high_complexity_fraction": "complexity.high_complexity_fraction",
    "complexity_density": "complexity.density_per_kloc",
    "maintainability_index": "maintainability.mean_index",
    "lint_total": "lint.total",
    "type_errors": "typing.errors",
    "type_errors_excluding_imports": "typing.errors_excluding_imports",
    "coverage_percent": "tests.coverage_percent",
}


def comparability(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Why these two snapshots may or may not be differenced."""
    reasons: list[str] = []
    if current["schema_version"] != baseline.get("schema_version"):
        reasons.append(
            f"schema_version {baseline.get('schema_version')} -> {current['schema_version']}"
        )
    if sorted(current["target"].get("paths", [])) != sorted(baseline.get("target", {}).get("paths", [])):
        reasons.append("analyzed paths differ")
    if current["definitions"] != baseline.get("definitions"):
        reasons.append("metric definitions differ")
    current_checker = _get(current, "typing.tool")
    baseline_checker = _get(baseline, "typing.tool")
    if current_checker != baseline_checker:
        reasons.append(f"type checker {baseline_checker} -> {current_checker}")
    # A checker's error count depends on the interpreter it resolved, so a
    # Python upgrade can move `typing.errors` with no code change at all.
    # `lint.total` means "findings under this rule selection". A different
    # selection is a different metric, not a movement in the same one.
    if _get(current, "lint.select") != _get(baseline, "lint.select"):
        reasons.append(
            f"ruff selection {_get(baseline, 'lint.select')} -> {_get(current, 'lint.select')}"
        )
    # Coverage differs between interpreters wherever a branch is
    # version-gated, so a coverage delta across a change of test interpreter is
    # not a change in coverage.
    current_tests_python = _get(current, "tests.python_version")
    baseline_tests_python = _get(baseline, "tests.python_version")
    if (
        current_tests_python
        and baseline_tests_python
        and current_tests_python != baseline_tests_python
    ):
        reasons.append(f"test interpreter {baseline_tests_python} -> {current_tests_python}")
    current_python = _get(current, "typing.environment.python_version")
    baseline_python = _get(baseline, "typing.environment.python_version")
    if current_python and baseline_python and current_python != baseline_python:
        reasons.append(f"python {baseline_python} -> {current_python}")
    return {"comparable": not reasons, "reasons": reasons}


def compute(current: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    """Build the ``deltas`` block."""
    if baseline is None:
        return {
            "status": "unavailable",
            "reason": "no baseline snapshot was provided",
            "baseline": None,
            "values": {name: None for name in DELTA_PATHS},
            "complexity_growth_per_loc": None,
        }

    compare = comparability(current, baseline)
    baseline_run = baseline.get("run", {})
    values = {name: _difference(current, baseline, path) for name, path in DELTA_PATHS.items()}

    # complexity_growth_per_loc: how much complexity arrived per line added.
    # Meaningless when the line count barely moved -- and actively misleading
    # when it moved the other way, so the guard is on the magnitude.
    delta_cc = values["aggregate_complexity"]
    delta_sloc = values["source_loc"]
    growth: float | None = None
    if (
        delta_cc is not None
        and delta_sloc is not None
        and abs(delta_sloc) >= DEFINITIONS["growth_min_abs_delta_loc"]
    ):
        growth = delta_cc / delta_sloc

    return {
        # A delta computed across a definition change is reported, but flagged:
        # suppressing it would hide a real discontinuity in the series.
        "status": "ok" if compare["comparable"] else "incomparable",
        "reason": None if compare["comparable"] else "; ".join(compare["reasons"]),
        "baseline": {
            "commit_sha": baseline_run.get("commit_sha"),
            "branch": baseline_run.get("branch"),
            "timestamp": baseline_run.get("timestamp"),
            "observation_id": baseline_run.get("observation_id"),
            "schema_version": baseline.get("schema_version"),
        },
        "values": values,
        "complexity_growth_per_loc": growth,
    }


def symbol_changes(
    current: dict[str, Any],
    baseline: dict[str, Any] | None,
    *,
    min_absolute: int = 3,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Per-symbol complexity movements worth a human's attention.

    Keyed on ``(path, symbol)`` rather than line number, so that a function
    moving down a file is not reported as a deletion plus an addition.  Renames
    still read as one of each; that is honest -- the tool cannot tell a rename
    from a replacement, and guessing would fabricate history.
    """
    if baseline is None:
        return []

    def index(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {(s["path"], s["symbol"]): s for s in document.get("symbols", [])}

    now, before = index(current), index(baseline)
    changes: list[dict[str, Any]] = []
    for key in set(now) | set(before):
        new_symbol, old_symbol = now.get(key), before.get(key)
        new_cc = (new_symbol or {}).get("cyclomatic_complexity")
        old_cc = (old_symbol or {}).get("cyclomatic_complexity")
        if new_cc is None and old_cc is None:
            continue
        if new_cc is not None and old_cc is not None:
            change = new_cc - old_cc
            kind = "changed"
        elif new_cc is not None:
            change, kind = new_cc, "added"
        else:
            change, kind = -old_cc, "removed"
        if abs(change) < min_absolute:
            continue
        changes.append(
            {
                "path": key[0],
                "symbol": key[1],
                "kind": kind,
                "before": old_cc,
                "after": new_cc,
                "delta": change,
            }
        )
    changes.sort(key=lambda c: (-abs(c["delta"]), c["path"], c["symbol"]))
    return changes[:limit]
