"""Summarize CI-health reports without turning missing measurements into zero.

This module consumes the local ``ci-health.json`` documents written by
``ci report``. It does not fetch reports or assume that the input is a
complete population. Its unit is a *job attempt*, not a PR: a workflow with
several reporting jobs contributes several observations. Matrix jobs must
have distinct platform job IDs.

These are observed and estimated test-seconds, not a prediction of a faster
verdict. The latter needs the job dependency graph and scheduling model.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _identifiers(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return None
    return sorted(set(value))


def _identity(doc: dict[str, Any]) -> tuple[str, str, str, str] | None:
    identity = doc.get("identity") if isinstance(doc.get("identity"), dict) else {}
    job = doc.get("job") if isinstance(doc.get("job"), dict) else {}
    parts = (identity.get("repository"), identity.get("run_id"), identity.get("run_attempt"), job.get("id"))
    # Missing or invalid IDs cannot be safely deduplicated: no job-name
    # fallback, because matrix legs may share a name.
    if any(isinstance(p, bool) or not isinstance(p, (str, int)) or not str(p) for p in parts):
        return None
    return tuple(str(p) for p in parts)  # type: ignore[return-value]


def aggregate(reports: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate newest snapshots per repository, run, attempt and job ID.

    ``reported_at`` orders successive snapshots; conflicting snapshots with an
    equal (or absent) timestamp are excluded, because either might be stale.
    ``unidentified`` and ``ambiguous`` make this incompleteness visible.
    Version/policy/event grouping keeps incomparable selector results apart.
    A missing selector, list or escape count remains unknown, never zero.
    """
    latest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    ambiguous: set[tuple[str, str, str, str]] = set()
    unidentified = 0
    snapshots = 0
    for doc in reports:
        snapshots += 1
        if not isinstance(doc, dict) or (key := _identity(doc)) is None:
            unidentified += 1
            continue
        old = latest.get(key)
        if old is None:
            latest[key] = doc
            continue
        previous, current = _number(old.get("reported_at")), _number(doc.get("reported_at"))
        if old == doc:
            continue
        if previous is None or current is None or previous == current:
            ambiguous.add(key)
        elif current > previous:
            latest[key] = doc
            ambiguous.discard(key)

    groups: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for key, doc in latest.items():
        if key in ambiguous:
            continue
        ident, job = doc["identity"], doc["job"]
        opts = doc.get("optimizations") if isinstance(doc.get("optimizations"), dict) else {}
        sel = opts.get("selection") if isinstance(opts.get("selection"), dict) else None
        group_key = (key[0], str(job.get("name") or "unknown"), str(ident.get("event") or "unknown"),
                     str((sel or {}).get("policy") or "unknown"), str((sel or {}).get("selector_version") or "unknown"),
                     str((sel or {}).get("selection_config_hash") or "unknown"))
        g = groups.setdefault(group_key, {
            "repository": group_key[0], "job": group_key[1], "event": group_key[2],
            "policy": group_key[3], "selector_version": group_key[4], "config_version": group_key[5], "jobs": 0,
            "selection_missing": 0, "decision_unknown": 0, "full": 0, "selected": 0,
            "categories_unknown": 0, "categories": Counter(), "categories_selected": Counter(),
            "triggers_unknown": 0, "triggers": Counter(), "sole_triggers": Counter(),
            "trigger_sets": Counter(),
            "escape_observations": 0, "escape_unknown": 0, "escape_not_applicable": 0,
            "escape_incomplete": 0, "escapes": 0,
            "escape_lower_bound_observations": 0, "escapes_observed_lower_bound": 0,
            "selector_duration_observations": 0, "selector_duration_seconds": 0.0,
            "saved_test_seconds_observations": 0, "saved_test_seconds": 0.0,
            "work_unknown": 0, "work_outcomes": Counter(),
            "_runs": set(), "_category_runs": {},
        })
        g["jobs"] += 1
        work = doc.get("work")
        if not isinstance(work, dict) or not work:
            g["work_unknown"] += 1
        else:
            for category, outcome in work.items():
                if isinstance(category, str) and isinstance(outcome, str):
                    g["work_outcomes"][(category, outcome)] += 1
        run_key = key[:3]
        g["_runs"].add(run_key)
        if sel is None:
            g["selection_missing"] += 1
            g["decision_unknown"] += 1
            g["categories_unknown"] += 1
            g["triggers_unknown"] += 1
            g["escape_unknown"] += 1
            continue
        decision = sel.get("decision")
        if decision in ("full", "selected"):
            g[decision] += 1
        else:
            g["decision_unknown"] += 1
        categories = _identifiers(sel.get("change_categories"))
        if categories is None:
            g["categories_unknown"] += 1
        else:
            g["categories"].update(categories)
            for category in categories:
                g["_category_runs"].setdefault(category, set()).add(run_key)
            if decision == "selected":
                g["categories_selected"].update(categories)
        triggers = _identifiers(sel.get("triggers")) if sel.get("triggers_complete") is True else None
        if triggers is None:
            g["triggers_unknown"] += 1
        else:
            g["triggers"].update(triggers)
            if triggers:
                g["trigger_sets"][tuple(triggers)] += 1
            if len(triggers) == 1:
                g["sole_triggers"].update(triggers)
        escapes = _number(sel.get("escapes"))
        # Require explicit observation completeness, not just a numeric zero.
        observation = sel.get("escape_observation")
        if observation == "complete" and escapes is not None:
            g["escape_observations"] += 1
            g["escapes"] += escapes
        elif observation == "not_applicable":
            g["escape_not_applicable"] += 1
        elif observation == "incomplete":
            g["escape_incomplete"] += 1
            observed = _number(sel.get("escapes_observed"))
            if observed is not None and observed > 0:
                g["escape_lower_bound_observations"] += 1
                g["escapes_observed_lower_bound"] += observed
        else:
            g["escape_unknown"] += 1
        duration = _number(sel.get("selector_duration_seconds"))
        if duration is not None:
            g["selector_duration_observations"] += 1
            g["selector_duration_seconds"] += duration
        saved = _number(sel.get("skipped_test_seconds_observed"))
        # This is a sum of test durations, not an estimate of elapsed time.
        if saved is not None and observation == "complete":
            g["saved_test_seconds_observations"] += 1
            g["saved_test_seconds"] += saved
    result = []
    for key in sorted(groups):
        g = groups[key]
        g["runs"] = len(g.pop("_runs"))
        g["category_runs"] = {k: len(v) for k, v in sorted(g.pop("_category_runs").items())}
        for field in ("categories", "categories_selected", "triggers", "sole_triggers"):
            g[field] = dict(sorted(g[field].items()))
        g["trigger_sets"] = [{"triggers": list(k), "jobs": v} for k, v in sorted(g["trigger_sets"].items())]
        g["work_outcomes"] = [{"category": k[0], "outcome": k[1], "jobs": v}
                              for k, v in sorted(g["work_outcomes"].items())]
        for field, count in (("selector_duration_seconds", "selector_duration_observations"),
                             ("saved_test_seconds", "saved_test_seconds_observations"),
                             ("escapes", "escape_observations"),
                             ("escapes_observed_lower_bound", "escape_lower_bound_observations")):
            if not g[count]:
                g[field] = None
        result.append(g)
    return {"snapshots": snapshots, "unidentified": unidentified, "ambiguous": len(ambiguous),
            "jobs": sum(g["jobs"] for g in result), "groups": result}
