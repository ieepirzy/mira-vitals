"""Canonical normalized code-health snapshot: version, shape, validation.

The snapshot -- not an OTEL metric, not a row in someone's TSDB -- is the
source representation.  Everything else is derived from it.  That makes the
rules here load-bearing for a dataset meant to stay interpretable for years:

* ``SCHEMA_VERSION`` bumps whenever the *meaning* of an existing field
  changes.  Adding a new optional field does not require a bump; redefining
  ``complexity.aggregate`` does.
* Missing is not zero.  A metric that was not measured, or whose analyzer
  failed, is ``None`` -- never ``0``.  Every analyzer-backed section carries
  its own :data:`STATUSES` value so a reader can tell the two apart without
  guessing.
* Denominators and thresholds that a derived metric depends on are written
  into the artifact (``definitions``) rather than living only in prose, so a
  future analyst can interpret an old snapshot without this repository.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Bumped only when the meaning of an existing field changes.  History:
#   1 -- initial version.
SCHEMA_VERSION = 1

# Parameters that derived metrics depend on.  Recorded in every snapshot.
# Changing any of these changes what the numbers mean, so they are versioned
# alongside the schema and asserted by tests.
DEFINITIONS: dict[str, Any] = {
    # Which radon blocks count as a "function" for the aggregate and for the
    # denominator of every per-function ratio.  radon's JSON lists a class
    # *and*, separately at top level, each of its methods; the class entry
    # carries a derived aggregate.  Counting both double-counts every method,
    # so classes are excluded and their methods counted once.  Closures are
    # nested only (never at top level) and would otherwise be dropped, so they
    # are walked recursively.
    "function_set": "radon:function|method|closure (class blocks excluded)",
    # Nearest-rank on the sorted ascending sample: index = ceil(q/100 * n),
    # 1-based, clamped to [1, n].  Chosen over linear interpolation because
    # cyclomatic complexity is a discrete count -- nearest-rank always returns
    # a value some function actually has, which keeps percentiles comparable
    # across runs where n changes.
    "percentile_method": "nearest_rank",
    "high_complexity_threshold": 10,
    "complexity_buckets": [10, 15, 20],
    # density = aggregate_cc / (source_loc / 1000).  Suppressed below this
    # many source lines: a 40-line repository yields a density that swings by
    # hundreds between commits and means nothing.
    "density_min_source_loc": 200,
    # complexity_growth_per_loc = delta_aggregate_cc / delta_source_loc.
    # Suppressed when |delta_source_loc| is below this, where the ratio is
    # dominated by its denominator.
    "growth_min_abs_delta_loc": 25,
    # radon's maintainability index is per-file.  The repository figure is the
    # unweighted mean over analyzed files, which is what "mean_index" means.
    # It is deliberately NOT LOC-weighted; a weighted variant would be a new
    # field, not a redefinition of this one.
    "maintainability_aggregation": "unweighted_mean_over_files",
    "loc_definition": "radon.raw: loc=physical lines, sloc=source lines "
    "(comments and blank lines excluded)",
}

#: Every analyzer-backed section carries one of these.  ``ok`` with zero
#: findings and ``error`` are different facts and must never collapse.
STATUSES = frozenset({"ok", "error", "unavailable", "skipped"})

#: Explicit, trusted authoring modes.  Never inferred from commit messages,
#: author names, or other heuristics.
AUTHORING_MODES = frozenset(
    {"human", "human_assisted", "agent_supervised", "agent_autonomous", "mixed"}
)

#: Top-level sections, in the order they are serialized.  The test suite
#: asserts this exact tuple, so adding or removing a section is a deliberate,
#: reviewed act rather than a drive-by.
TOP_LEVEL_KEYS: tuple[str, ...] = (
    "schema_version",
    "generated_by",
    "definitions",
    "run",
    "ci",
    "target",
    "provenance",
    "summary",
    "complexity",
    "maintainability",
    "halstead",
    "lint",
    "typing",
    "tests",
    "security",
    "deltas",
    "hotspots",
    "files",
    "symbols",
    "tools",
)


class SchemaError(ValueError):
    """A snapshot did not match the declared schema."""


def observation_id(
    *,
    repository: str,
    commit_sha: str | None,
    target_paths: list[str],
    analyzed_tree_sha: str | None = None,
    schema_version: int = SCHEMA_VERSION,
) -> str:
    """A stable identity for "this analysis of this code".

    Re-analyzing the same code with the same target produces the same value,
    which is what lets a backend deduplicate repeated ingestion (a rerun, a
    replayed webhook, a workflow retried after a flake).  ``run_id`` and
    ``run_attempt`` are recorded separately and deliberately excluded here, so
    that reruns remain *distinguishable* while still being *dedupable*.

    ``analyzed_tree_sha`` is what makes that "this code" rather than "this
    commit", and it is not redundant with ``commit_sha``.  On a pull request
    CI checks out GitHub's synthetic merge commit, so the tree actually
    measured is head-merged-into-base while ``commit_sha`` records the head --
    the identifier the observation can be joined on, since the merge commit
    exists nowhere in the repository's history.  Keying only on the head means
    that when the base branch advances under an unchanged head, a rerun
    measures genuinely different code and produces the *same* id, and a
    receiver deduplicating on it discards the newer measurement as though it
    were a replay.  Including the analyzed tree fixes that in the right
    direction: identical code still dedupes, different code no longer collides.
    """
    material = json.dumps(
        {
            "schema_version": schema_version,
            "repository": repository,
            "commit_sha": commit_sha,
            "analyzed_tree_sha": analyzed_tree_sha,
            "target_paths": sorted(target_paths),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def _check_number_or_none(section: dict[str, Any], key: str, where: str) -> None:
    value = section.get(key, "__missing__")
    _require(value != "__missing__", f"{where}: missing key {key!r}")
    _require(
        value is None or isinstance(value, (int, float)) and not isinstance(value, bool),
        f"{where}.{key}: expected number or null, got {type(value).__name__}",
    )


def validate(doc: dict[str, Any]) -> None:
    """Structurally validate a snapshot, raising :class:`SchemaError`.

    This is a real check of the properties the dataset depends on -- the key
    set, the status vocabulary, numbers-or-null in the numeric sections -- not
    a restatement of the builder.  It runs on every generated artifact so a
    malformed snapshot fails the run that produced it rather than being
    discovered years later during analysis.
    """
    _require(isinstance(doc, dict), "snapshot must be an object")
    _require(
        tuple(doc.keys()) == TOP_LEVEL_KEYS,
        f"top-level keys/order mismatch: {tuple(doc.keys())!r} != {TOP_LEVEL_KEYS!r}",
    )
    _require(doc["schema_version"] == SCHEMA_VERSION, "schema_version mismatch")

    run = doc["run"]
    for key in ("repository", "timestamp", "observation_id"):
        _require(isinstance(run.get(key), str) and run[key], f"run.{key} must be a non-empty string")
    _require(run["timestamp"].endswith("Z"), "run.timestamp must be UTC and end with 'Z'")
    _require(isinstance(run.get("canonical"), bool), "run.canonical must be a bool")

    mode = doc["provenance"].get("authoring_mode")
    _require(
        mode is None or mode in AUTHORING_MODES,
        f"provenance.authoring_mode: {mode!r} not in {sorted(AUTHORING_MODES)}",
    )

    for name in ("lint", "typing", "tests", "security", "maintainability", "halstead"):
        status = doc[name].get("status")
        _require(status in STATUSES, f"{name}.status: {status!r} not in {sorted(STATUSES)}")

    for key in ("loc", "source_loc", "files", "functions"):
        _check_number_or_none(doc["summary"], key, "summary")
    for key in (
        "aggregate",
        "mean",
        "p50",
        "p90",
        "p95",
        "max",
        "functions_gt_10",
        "functions_gt_15",
        "functions_gt_20",
        "high_complexity_fraction",
        "density_per_kloc",
    ):
        _check_number_or_none(doc["complexity"], key, "complexity")

    for name in ("hotspots", "files", "symbols"):
        _require(isinstance(doc[name], list), f"{name} must be a list")

    for symbol in doc["symbols"]:
        for key in ("path", "symbol", "symbol_type"):
            _require(isinstance(symbol.get(key), str), f"symbol.{key} must be a string")
        for key in ("line", "loc", "cyclomatic_complexity", "maintainability_index", "nesting_depth"):
            _check_number_or_none(symbol, key, f"symbol {symbol.get('symbol')!r}")


def dumps(doc: dict[str, Any]) -> str:
    """Serialize a snapshot.

    Keys are emitted in insertion order rather than sorted, so the artifact
    reads in the order :data:`TOP_LEVEL_KEYS` declares.  Floats are not
    rounded here -- rounding is a presentation concern and belongs in
    ``report.py``, never in the stored record.
    """
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
