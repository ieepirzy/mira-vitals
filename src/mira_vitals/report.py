"""Human-readable CI output.

Concise by construction.  Per-symbol detail belongs in the artifact; a console
report that prints hundreds of functions is one nobody reads, and a PR comment
that does it is worse.  Rounding happens here and only here -- the stored
record keeps full precision.
"""

from __future__ import annotations

from typing import Any

#: Movements smaller than these are printed without an alarm marker.  Not every
#: numerical wobble is a regression, and marking them all trains people to
#: ignore the marker.
NOTABLE = {
    "aggregate_complexity": 10,
    "complexity_density": 2.0,
    "maintainability_index": 1.0,
    "coverage_percent": 0.5,
    "p95_complexity": 1,
    "max_complexity": 1,
    "functions_gt_10": 1,
    "type_errors": 1,
    "lint_total": 1,
}


def _number(value: Any, *, digits: int = 0, percent: bool = False) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    text = f"{value:,.{digits}f}"
    return f"{text}%" if percent else text


def _delta(value: Any, *, digits: int = 0, suffix: str = "") -> str:
    """A signed change, or nothing at all when there is no baseline.

    ``None`` renders as empty rather than ``(+0)``: printing a zero for an
    unknown is the console equivalent of storing zero for missing.
    """
    if value is None:
        return ""
    if isinstance(value, float) and abs(value) < 10 ** (-digits) / 2:
        return "  (=)"
    return f"  ({value:+,.{digits}f}{suffix})"


def summary_text(snapshot: dict[str, Any]) -> str:
    """The block printed in every CI run."""
    summary = snapshot["summary"]
    complexity = snapshot["complexity"]
    maintainability = snapshot["maintainability"]
    tests = snapshot["tests"]
    values = snapshot["deltas"].get("values", {}) if isinstance(snapshot.get("deltas"), dict) else {}
    provenance = snapshot["provenance"]

    def row(label: str, value: str, delta: str = "") -> str:
        return f"{label:<24}{value:>10}{delta}"

    lines = ["Code health", "-----------"]
    lines.append(row("LOC", _number(summary["loc"]), _delta(values.get("loc"))))
    lines.append(row("Source LOC", _number(summary["source_loc"]), _delta(values.get("source_loc"))))
    lines.append(row("Files", _number(summary["files"]), _delta(values.get("files"))))
    lines.append(row("Functions", _number(summary["functions"]), _delta(values.get("functions"))))
    lines.append(
        row("Cyclomatic complexity", _number(complexity["aggregate"]), _delta(values.get("aggregate_complexity")))
    )
    lines.append(
        row("CC density /kLOC", _number(complexity["density_per_kloc"], digits=1),
            _delta(values.get("complexity_density"), digits=1))
    )
    lines.append(row("Mean CC", _number(complexity["mean"], digits=2), _delta(values.get("mean_complexity"), digits=2)))
    lines.append(row("Median CC", _number(complexity["p50"]), _delta(values.get("p50_complexity"))))
    lines.append(row("P90 CC", _number(complexity["p90"]), _delta(values.get("p90_complexity"))))
    lines.append(row("P95 CC", _number(complexity["p95"]), _delta(values.get("p95_complexity"))))
    lines.append(row("Max CC", _number(complexity["max"]), _delta(values.get("max_complexity"))))
    lines.append(row("Functions > 10", _number(complexity["functions_gt_10"]), _delta(values.get("functions_gt_10"))))
    lines.append(row("Functions > 15", _number(complexity["functions_gt_15"]), _delta(values.get("functions_gt_15"))))
    lines.append(row("Functions > 20", _number(complexity["functions_gt_20"]), _delta(values.get("functions_gt_20"))))
    lines.append(
        row("Maintainability Index", _number(maintainability["mean_index"], digits=1),
            _delta(values.get("maintainability_index"), digits=1))
    )
    lint = snapshot["lint"]
    lines.append(
        row(f"Lint issues ({lint.get('tool')})", _number(lint.get("total")), _delta(values.get("lint_total")))
    )
    typing = snapshot["typing"]
    lines.append(
        row(f"Type errors ({typing.get('tool')})", _number(typing.get("errors")), _delta(values.get("type_errors")))
    )
    lines.append(
        row("Coverage", _number(tests.get("coverage_percent"), digits=1, percent=True),
            _delta(values.get("coverage_percent"), digits=1, suffix="pp"))
    )
    mode = provenance.get("authoring_mode") or "undeclared"
    lines.append(row("Authoring mode", mode))

    degraded = _degraded_sections(snapshot)
    if degraded:
        # Loudly, and above the numbers' credibility line: a reader who does
        # not know an analyzer failed will read its absence as a clean result.
        lines.append("")
        lines.append("! Incomplete measurement -- these sections did not run cleanly:")
        for name, status, error in degraded:
            lines.append(f"    {name}: {status}" + (f" ({error[:100]})" if error else ""))

    skipped = _skipped_sections(snapshot)
    if skipped:
        lines.append("")
        lines.append(f"  not configured in this run: {', '.join(skipped)}")

    return "\n".join(lines)


#: Statuses that mean "we thought we had this measurement and we do not".
#: `skipped` is deliberately absent: a repository that configures no coverage
#: report is not broken, and flagging it every run would train readers to skim
#: past the banner that matters.
DEGRADED_STATUSES = frozenset({"error", "unavailable"})


def _degraded_sections(snapshot: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    degraded = []
    for name in ("complexity", "maintainability", "halstead", "lint", "typing", "tests", "security"):
        section = snapshot.get(name, {})
        status = section.get("status")
        if status in DEGRADED_STATUSES:
            degraded.append((name, status, section.get("error")))
    return degraded


def _skipped_sections(snapshot: dict[str, Any]) -> list[str]:
    return [
        name
        for name in ("tests", "security")
        if snapshot.get(name, {}).get("status") == "skipped"
    ]


def changes_text(snapshot: dict[str, Any], symbol_changes: list[dict[str, Any]]) -> str:
    """Notable movements, for pull-request runs."""
    deltas = snapshot.get("deltas", {})
    if deltas.get("status") == "unavailable":
        return "Notable changes\n---------------\n(no baseline snapshot available for comparison)"

    lines = ["Notable changes", "---------------"]
    if deltas.get("status") == "incomparable":
        lines.append(f"! baseline is not directly comparable: {deltas.get('reason')}")

    values = deltas.get("values", {})
    flagged = False
    for key, threshold in NOTABLE.items():
        value = values.get(key)
        if value is None or abs(value) < threshold:
            continue
        flagged = True
        # Higher is better for these two; everything else is better when lower.
        better_when_higher = key in {"maintainability_index", "coverage_percent"}
        improved = value > 0 if better_when_higher else value < 0
        lines.append(f"{'✓' if improved else '⚠'} {key}: {value:+,.2f}".rstrip("0").rstrip("."))

    for change in symbol_changes[:10]:
        flagged = True
        if change["kind"] == "added":
            lines.append(f"⚠ {change['symbol']}(): new, CC {change['after']}")
        elif change["kind"] == "removed":
            lines.append(f"✓ {change['symbol']}(): removed (was CC {change['before']})")
        else:
            marker = "⚠" if change["delta"] > 0 else "✓"
            lines.append(f"{marker} {change['symbol']}(): CC {change['before']} → {change['after']}")

    growth = deltas.get("complexity_growth_per_loc")
    if growth is not None:
        flagged = True
        lines.append(f"  complexity added per source line: {growth:+.3f}")

    if not flagged:
        lines.append("no movement beyond the reporting thresholds")
    return "\n".join(lines)


def hotspots_text(snapshot: dict[str, Any], *, limit: int = 10) -> str:
    """The current worst functions -- the standing list, not a diff."""
    rows = snapshot["hotspots"][:limit]
    if not rows:
        return "Complexity hotspots\n-------------------\n(none above threshold)"
    lines = ["Complexity hotspots", "-------------------"]
    for row in rows:
        location = f"{row['path']}:{row['line']}"
        lines.append(f"  CC {row['cyclomatic_complexity']:>3}  {location:<44} {row['symbol']}")
    total = len(snapshot["hotspots"])
    if total > limit:
        lines.append(f"  ... and {total - limit} more (complete list in code-health.json)")
    return "\n".join(lines)


def markdown_summary(snapshot: dict[str, Any], symbol_changes: list[dict[str, Any]]) -> str:
    """GitHub step-summary rendering of the same content."""
    blocks = [
        "## Code health",
        "",
        "```",
        summary_text(snapshot),
        "```",
        "",
        "```",
        hotspots_text(snapshot),
        "```",
    ]
    if snapshot["deltas"].get("status") != "unavailable" or symbol_changes:
        blocks += ["", "```", changes_text(snapshot, symbol_changes), "```"]
    run = snapshot["run"]
    blocks += [
        "",
        f"<sub>schema v{snapshot['schema_version']} · observation "
        f"`{run['observation_id'][:19]}…` · {run['ref_class']} · "
        f"authoring: {snapshot['provenance'].get('authoring_mode') or 'undeclared'}</sub>",
    ]
    return "\n".join(blocks)
