"""Build the canonical snapshot from analyzer output.

Every derived number in here is computed from raw counts that are also stored,
so a future reader who disagrees with a definition can recompute from the
artifact rather than being stuck with our choice.  That is the whole reason the
per-symbol records are preserved.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from . import __version__, schema
from .schema import DEFINITIONS


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile on an ascending sample.

    ``index = ceil(q/100 * n)``, 1-based, clamped to ``[1, n]``.  Returns an
    element that is actually present in the sample.

    Chosen over linear interpolation because cyclomatic complexity is a
    discrete count: interpolating between a function of complexity 9 and one of
    complexity 12 yields 10.8, a value no function has, which then moves
    whenever ``n`` changes even if no function changed.  Nearest-rank keeps a
    percentile comparable across runs of different size, which is what a
    longitudinal series needs.
    """
    if not values:
        return None
    ordered = sorted(values)
    if q <= 0:
        return ordered[0]
    rank = math.ceil(q / 100.0 * len(ordered))
    return ordered[min(max(rank, 1), len(ordered)) - 1]


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _safe_ratio(numerator: float | None, denominator: float | None, *, min_denominator: float) -> float | None:
    """A ratio, or ``None`` when the denominator makes it meaningless.

    Guards the case the brief calls out: a repository of forty source lines
    produces a complexity density that swings by hundreds between commits and
    predicts nothing.  Emitting ``None`` says "not meaningful here"; emitting a
    number would put noise into a series that later gets regressed against.
    """
    if numerator is None or denominator is None:
        return None
    if abs(denominator) < min_denominator:
        return None
    return numerator / denominator


def summarize_complexity(symbols: Iterable[dict[str, Any]], *, source_loc: int | None) -> dict[str, Any]:
    """Repository-level complexity aggregates.

    The denominator for every per-function figure is the function set defined
    in :mod:`mira_vitals.analyzers.radon_cc`: top-level function and
    method blocks plus promoted closures, never class blocks.
    """
    values = [
        s["cyclomatic_complexity"]
        for s in symbols
        if isinstance(s.get("cyclomatic_complexity"), (int, float))
    ]
    if not values:
        return {
            "aggregate": None, "mean": None, "p50": None, "p90": None, "p95": None,
            "max": None, "min": None, "functions_gt_10": None, "functions_gt_15": None,
            "functions_gt_20": None, "high_complexity_fraction": None, "density_per_kloc": None,
        }

    total = sum(values)
    buckets = {t: sum(1 for v in values if v > t) for t in DEFINITIONS["complexity_buckets"]}
    high = buckets[DEFINITIONS["high_complexity_threshold"]]

    return {
        "aggregate": total,
        "mean": total / len(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "max": max(values),
        "min": min(values),
        "functions_gt_10": buckets[10],
        "functions_gt_15": buckets[15],
        "functions_gt_20": buckets[20],
        # Denominator is the analyzed function count, stated in summary.functions.
        "high_complexity_fraction": high / len(values),
        "density_per_kloc": _safe_ratio(
            total,
            None if source_loc is None else source_loc / 1000.0,
            # min_denominator is expressed in kLOC to match the ratio's units.
            min_denominator=DEFINITIONS["density_min_source_loc"] / 1000.0,
        ),
    }


def build_files(
    raw: dict[str, dict[str, Any]],
    maintainability: dict[str, dict[str, Any]],
    halstead: dict[str, dict[str, Any]],
    symbols: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Per-file records, joined across radon's four passes."""
    per_file_cc: dict[str, list[float]] = {}
    for symbol in symbols:
        value = symbol.get("cyclomatic_complexity")
        if isinstance(value, (int, float)):
            per_file_cc.setdefault(symbol["path"], []).append(value)

    paths = sorted(set(raw) | set(maintainability) | set(halstead) | set(per_file_cc))
    files = []
    for path in paths:
        raw_metrics = raw.get(path, {})
        complexities = per_file_cc.get(path, [])
        files.append(
            {
                "path": path,
                "loc": raw_metrics.get("loc"),
                "source_loc": raw_metrics.get("sloc"),
                "comment_lines": raw_metrics.get("comments"),
                "blank_lines": raw_metrics.get("blank"),
                "logical_loc": raw_metrics.get("lloc"),
                "functions": len(complexities) or None,
                "complexity_total": sum(complexities) if complexities else None,
                "complexity_max": max(complexities) if complexities else None,
                "maintainability_index": maintainability.get(path, {}).get("mi"),
                "maintainability_rank": maintainability.get(path, {}).get("rank"),
                "halstead": halstead.get(path) or None,
            }
        )
    return files


def build_maintainability(maintainability: dict[str, dict[str, Any]], status: str) -> dict[str, Any]:
    values = [v["mi"] for v in maintainability.values() if isinstance(v.get("mi"), (int, float))]
    return {
        "status": status,
        "tool": "radon",
        # Unweighted mean over files, per DEFINITIONS.  A LOC-weighted variant
        # would be a different metric under a different key, never a silent
        # change to this one.
        "mean_index": _mean(values),
        "min_index": min(values) if values else None,
        "p10_index": percentile(values, 10) if values else None,
        "files_measured": len(values) or None,
    }


def build_halstead(halstead: dict[str, dict[str, Any]], status: str) -> dict[str, Any]:
    def total(key: str) -> float | None:
        values = [v[key] for v in halstead.values() if isinstance(v.get(key), (int, float))]
        return sum(values) if values else None

    return {
        "status": status,
        "tool": "radon",
        "volume_total": total("volume"),
        "difficulty_mean": _mean(
            [v["difficulty"] for v in halstead.values() if isinstance(v.get("difficulty"), (int, float))]
        ),
        "effort_total": total("effort"),
        "bugs_total": total("bugs"),
        "files_measured": len(halstead) or None,
    }


def hotspots(symbols: Sequence[dict[str, Any]], *, limit: int = 20, threshold: int | None = None) -> list[dict[str, Any]]:
    """The worst functions, for the human summary and the structured event.

    A bounded slice of ``symbols`` -- which is stored in full regardless -- so
    that the console output and the event payload stay small without the
    detailed record being sampled away.
    """
    threshold = DEFINITIONS["high_complexity_threshold"] if threshold is None else threshold
    ranked = [
        s for s in symbols
        if isinstance(s.get("cyclomatic_complexity"), (int, float))
        and s["cyclomatic_complexity"] > threshold
    ]
    ranked.sort(key=lambda s: (-s["cyclomatic_complexity"], s["path"], s["symbol"]))
    return ranked[:limit]


def build(
    *,
    run: dict[str, Any],
    ci: dict[str, Any],
    target: dict[str, Any],
    provenance: dict[str, Any],
    symbols: list[dict[str, Any]],
    raw: dict[str, dict[str, Any]],
    maintainability: dict[str, dict[str, Any]],
    halstead: dict[str, dict[str, Any]],
    lint: dict[str, Any],
    typing: dict[str, Any],
    tests: dict[str, Any],
    security: dict[str, Any],
    tools: dict[str, Any],
    complexity_status: str,
    summary_status: str = "ok",
    maintainability_status: str,
    halstead_status: str,
    deltas: dict[str, Any] | None = None,
    hotspot_limit: int = 20,
) -> dict[str, Any]:
    """Assemble and validate the canonical snapshot."""
    loc = sum(v["loc"] for v in raw.values() if isinstance(v.get("loc"), int)) if raw else None
    source_loc = sum(v["sloc"] for v in raw.values() if isinstance(v.get("sloc"), int)) if raw else None

    complexity = summarize_complexity(symbols, source_loc=source_loc)
    complexity["status"] = complexity_status

    document = {
        "schema_version": schema.SCHEMA_VERSION,
        # Names the package that actually produced the snapshot. The telemetry
        # namespace stays `code.health.*` -- that is a data contract describing
        # the measurement, not the producer, and the two are deliberately
        # allowed to differ.
        "generated_by": {"name": "mira-vitals", "version": __version__},
        "definitions": dict(DEFINITIONS),
        "run": run,
        "ci": ci,
        "target": target,
        "provenance": provenance,
        "summary": {
            # Governs the LOC and file counts: they come from radon's raw pass,
            # which can parse some files and fail on others. Without this the
            # metric exporter has no way to tell a partial total from a whole
            # one, and a metric point carries no status downstream.
            "status": summary_status,
            "loc": loc,
            "source_loc": source_loc,
            "files": len(raw) or None,
            # The function set, spelled out again where the number lives, so a
            # reader of the artifact alone knows what the denominator is.
            "functions": len(symbols) or None,
            "function_set": DEFINITIONS["function_set"],
        },
        "complexity": complexity,
        "maintainability": build_maintainability(maintainability, maintainability_status),
        "halstead": build_halstead(halstead, halstead_status),
        "lint": lint,
        "typing": typing,
        "tests": tests,
        "security": security,
        "deltas": deltas if deltas is not None else {},
        "hotspots": hotspots(symbols, limit=hotspot_limit),
        "files": build_files(raw, maintainability, halstead, symbols),
        "symbols": sorted(symbols, key=lambda s: (s["path"], s.get("line") or 0)),
        "tools": tools,
    }
    schema.validate(document)
    return document
