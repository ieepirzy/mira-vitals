"""The CI-health document, and its OTLP projection.

One document per report (``ci-health.json``), written whether or not
anything is exported, and two OTLP signals derived from it -- the same
split the code-health collector and the fleet build reporter use:

metrics
    Bounded-cardinality gauges about *this* job: its wall time and queue
    wait, its phases and steps, its test totals, its runner's CPU and
    memory, its cache outcomes. Attributes are names from a bounded set
    (workflow, job, step, phase, runner labels, outcome), never ids.
logs
    The high-cardinality record: one ``ci.job`` per job of the run the
    platform reported (so queue waits and gaps between jobs are queryable
    even for jobs that do not run this command), one ``ci.step`` per
    completed step, one ``ci.phase`` per mark interval, one ``ci.test`` per
    test case (capped), and one ``ci.report`` with the summary. Every
    record carries the run identity (repository, run id, attempt, sha) so
    the store can deduplicate repeated reports of the same job.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import junit as junit_module

SCHEMA_VERSION = "ci-health/1"


def build(
    *,
    identity: dict[str, Any],
    platform: dict[str, Any] | None,
    platform_note: str,
    marks: list[dict[str, Any]],
    phases: list[dict[str, Any]],
    tests: list[dict[str, Any]],
    test_files: list[str],
    caches: dict[str, bool],
    runner: dict[str, Any],
    outcome: str | None,
    warnings: list[str],
    generated_by: dict[str, Any],
    optimizations: dict[str, Any] | None = None,
    now: float | None = None,
    max_cases: int | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    self_job = None
    if platform:
        selves = [j for j in platform["jobs"] if j.get("self")]
        if not selves:
            # A matrix leg or a renamed job: fall back to the in-progress job
            # with this job's id-name, if exactly one matches.
            named = [
                j
                for j in platform["jobs"]
                if j.get("status") == "in_progress" and j.get("name") == identity.get("job")
            ]
            selves = named if len(named) == 1 else []
        if selves:
            self_job = selves[0]
            self_job["self"] = True
            # The report is the job's last step, so its own row would stay
            # "in progress" for ever: this is its final snapshot. Completion
            # is the report time, flagged by its basis.
            if self_job.get("completed_at") is None and self_job.get("started_at"):
                self_job["completed_at"] = now
                self_job["duration_seconds"] = round(max(0.0, now - self_job["started_at"]), 3)
                self_job["completed_basis"] = "report"

    started = (self_job or {}).get("started_at")
    if started:
        wall, wall_basis = round(max(0.0, now - started), 3), "platform"
    elif marks:
        wall, wall_basis = round(max(0.0, now - marks[0]["ts"]), 3), "marks"
    else:
        wall, wall_basis = None, None

    job = {
        "name": (self_job or {}).get("name") or identity.get("job"),
        "id": (self_job or {}).get("id"),
        "wall_seconds": wall,
        "wall_basis": wall_basis,
        "queue_wait_seconds": (self_job or {}).get("queue_wait_seconds"),
        "queue_basis": (self_job or {}).get("queue_basis"),
        "labels": (self_job or {}).get("labels") or [],
        "runner_name": (self_job or {}).get("runner_name") or identity.get("runner_name"),
        "outcome": outcome,
        "steps": (self_job or {}).get("steps") or [],
    }

    tests_summary = junit_module.summarize(tests) if test_files else None
    # The document keeps every failure and then the slowest tests, up to the
    # cap; the summary above is over all of them. `dropped` says how many
    # passing tests were left out, so a consumer never mistakes the cap for
    # "did not run" (the escape view depends on that).
    ordered = _cap_order(tests)
    kept = ordered if max_cases is None else ordered[: max(0, max_cases)]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": generated_by,
        "reported_at": round(now, 3),
        "identity": identity,
        "job": job,
        "phases": phases,
        "caches": caches,
        "runner": runner,
        "tests": {"files": test_files, "summary": tests_summary, "cases": kept, "dropped": len(ordered) - len(kept)},
        "optimizations": optimizations or {"selection": None, "parallelism": None},
        "platform": platform,
        "platform_note": platform_note,
        "warnings": warnings,
    }


# --- OTLP projection --------------------------------------------------------


def _attr(key: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, (list, dict)):
        return {"key": key, "value": {"stringValue": json.dumps(value, separators=(",", ":"), sort_keys=True)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _attrs(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in (_attr(k, v) for k, v in mapping.items()) if a is not None]


def resource(doc: dict[str, Any]) -> dict[str, Any]:
    ident = doc["identity"]
    return {
        "attributes": _attrs(
            {
                "service.name": "mira-vitals-ci",
                "vcs.repository.name": ident.get("repository"),
                "cicd.system": ident.get("platform"),
                "mira_vitals.schema": doc["schema_version"],
                "mira_vitals.version": doc["generated_by"].get("version"),
            }
        )
    }


def _base_attrs(doc: dict[str, Any]) -> dict[str, Any]:
    ident, job = doc["identity"], doc["job"]
    return {
        "cicd.pipeline.name": ident.get("workflow"),
        "cicd.pipeline.task.name": job.get("name"),
        "cicd.worker.labels": ",".join(job.get("labels") or []) or None,
        "cicd.pipeline.result": job.get("outcome"),
    }


def metric_points(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{name, unit, value, attributes}]`` -- only measured values."""
    base = _base_attrs(doc)
    job, runner = doc["job"], doc["runner"] or {}
    points: list[dict[str, Any]] = []

    def add(name: str, unit: str, value: Any, **extra: Any) -> None:
        if value is None or isinstance(value, bool):
            return
        if not isinstance(value, (int, float)):
            return
        points.append({"name": name, "unit": unit, "value": float(value), "attributes": {**base, **extra}})

    add("ci.job.duration", "s", job.get("wall_seconds"))
    add("ci.job.queue.wait", "s", job.get("queue_wait_seconds"))
    for phase in doc["phases"]:
        add("ci.phase.duration", "s", phase["duration_seconds"], **{"ci.phase.name": phase["name"]})
    for step in job.get("steps") or []:
        add("ci.step.duration", "s", step.get("duration_seconds"), **{"cicd.pipeline.task.step.name": step.get("name")})
    summary = (doc["tests"] or {}).get("summary")
    if summary:
        add("ci.tests.duration", "s", summary.get("total_seconds"))
        add("ci.tests.p90", "s", summary.get("p90_seconds"))
        add("ci.tests.slowest_10pct_share", "1", summary.get("slowest_10pct_share"))
        for outcome, count in sorted((summary.get("by_outcome") or {}).items()):
            add("ci.tests.count", "{test}", count, **{"test.outcome": outcome})
    usage = runner.get("cpu_usage_usec")
    add("ci.runner.cpu.usage", "s", usage / 1e6 if isinstance(usage, int) else None)
    throttled = runner.get("cpu_throttled_usec")
    add("ci.runner.cpu.throttled", "s", throttled / 1e6 if isinstance(throttled, int) else None)
    periods, nthrottled = runner.get("cpu_nr_periods"), runner.get("cpu_nr_throttled")
    if isinstance(periods, int) and periods > 0 and isinstance(nthrottled, int):
        add("ci.runner.cpu.throttled_ratio", "1", round(nthrottled / periods, 4))
    add("ci.runner.cpu.limit", "{cpu}", runner.get("cpu_limit_cores"))
    add("ci.runner.memory.peak", "By", runner.get("memory_peak_bytes"))
    add("ci.runner.memory.limit", "By", runner.get("memory_limit_bytes"))
    for name, hit in sorted(doc["caches"].items()):
        add("ci.cache.hit", "1", 1 if hit else 0, **{"ci.cache.name": name})
    opts = doc.get("optimizations") or {}
    sel = opts.get("selection")
    if sel:
        mode = {"ci.selection.mode": sel["mode"]}
        add("ci.selection.tests.selected", "{test}", sel.get("selected"), **mode)
        add("ci.selection.tests.total", "{test}", sel.get("total"), **mode)
        add("ci.selection.saved.estimate", "s", sel.get("estimated_saved_seconds"), **mode)
        add("ci.selection.closure_cache.hits", "{test}", sel.get("closure_cache_hits"), **mode)
        for trigger in sel.get("triggers") or []:
            add("ci.selection.full_trigger", "1", 1, **{"ci.selection.trigger": trigger})
    par = opts.get("parallelism")
    if par:
        add("ci.xdist.workers", "{worker}", par.get("count"))
        add("ci.xdist.worker.wall.max", "s", par.get("wall_max_seconds"))
        add("ci.xdist.imbalance", "1", par.get("imbalance"))
    return points


def _identity_attrs(doc: dict[str, Any]) -> dict[str, Any]:
    ident = doc["identity"]
    return {
        "cicd.pipeline.name": ident.get("workflow"),
        "cicd.pipeline.run.id": ident.get("run_id"),
        "cicd.pipeline.run.attempt": ident.get("run_attempt"),
        "cicd.pipeline.run.number": ident.get("run_number"),
        "vcs.ref.head.revision": ident.get("sha"),
        "vcs.ref.head.name": ident.get("ref"),
        "cicd.pipeline.trigger": ident.get("event"),
        "ci.reporter.job": doc["job"].get("name"),
        "ci.reporter.job.id": doc["job"].get("id"),
        "ci.selection.mode": ((doc.get("optimizations") or {}).get("selection") or {}).get("mode"),
        "vcs.change.id": ident.get("pr_number"),
        "vcs.ref.base.revision": ident.get("base_sha"),
        "ci.merged_change.id": ident.get("merged_pr_number"),
        "ci.trigger.at": ident.get("trigger_at"),
    }


def log_records(doc: dict[str, Any], *, max_tests: int) -> list[dict[str, Any]]:
    """``[{event, body, attributes}]`` in send order."""
    ident = _identity_attrs(doc)
    records: list[dict[str, Any]] = []

    def add(event: str, body: str, **attributes: Any) -> None:
        records.append({"event": event, "body": body, "attributes": {"event.name": event, **ident, **attributes}})

    job = doc["job"]
    summary = (doc["tests"] or {}).get("summary") or {}
    add(
        "ci.report",
        f"{ident['ci.reporter.job']}: wall {job.get('wall_seconds')}s, queue {job.get('queue_wait_seconds')}s",
        **{
            "ci.job.duration": job.get("wall_seconds"),
            "ci.job.duration.basis": job.get("wall_basis"),
            "ci.job.queue.wait": job.get("queue_wait_seconds"),
            "ci.job.queue.basis": job.get("queue_basis"),
            "cicd.pipeline.result": job.get("outcome"),
            "cicd.worker.name": job.get("runner_name"),
            "cicd.worker.labels": job.get("labels") or None,
            "ci.tests.summary": {k: v for k, v in summary.items() if k != "slowest"} or None,
            "ci.runner": doc["runner"] or None,
            "ci.caches": doc["caches"] or None,
            "ci.selection": (doc.get("optimizations") or {}).get("selection"),
            "ci.parallelism": (doc.get("optimizations") or {}).get("parallelism"),
            "ci.platform.note": doc["platform_note"],
            "ci.warnings": doc["warnings"] or None,
            "url.full": doc["identity"].get("url"),
        },
    )

    platform = doc.get("platform") or {}
    run = platform.get("run") or {}
    for pjob in platform.get("jobs") or []:
        add(
            "ci.job",
            f"{pjob.get('name')}: {pjob.get('status')}/{pjob.get('conclusion')}",
            **{
                "cicd.pipeline.task.run.id": pjob.get("id"),
                "cicd.pipeline.task.name": pjob.get("name"),
                "ci.job.status": pjob.get("status"),
                "cicd.pipeline.task.run.result": pjob.get("conclusion"),
                "ci.job.created_at": pjob.get("created_at"),
                "ci.job.started_at": pjob.get("started_at"),
                "ci.job.completed_at": pjob.get("completed_at"),
                "ci.job.queue.wait": pjob.get("queue_wait_seconds"),
                "ci.job.queue.basis": pjob.get("queue_basis"),
                "ci.job.duration": pjob.get("duration_seconds"),
                "cicd.worker.name": pjob.get("runner_name"),
                "cicd.worker.labels": pjob.get("labels") or None,
                "ci.job.self": bool(pjob.get("self")),
                "ci.job.carried_over": bool(pjob.get("carried_over")),
                "ci.job.completed.basis": pjob.get("completed_basis") or ("platform" if pjob.get("completed_at") else None),
                "ci.run.created_at": run.get("created_at"),
                "ci.run.started_at": run.get("started_at"),
                "url.full": pjob.get("url"),
            },
        )
        for step in pjob.get("steps") or []:
            if step.get("completed_at") is None:
                continue
            add(
                "ci.step",
                f"{pjob.get('name')} / {step.get('name')}",
                **{
                    "cicd.pipeline.task.run.id": pjob.get("id"),
                    "cicd.pipeline.task.name": pjob.get("name"),
                    "ci.step.number": step.get("number"),
                    "cicd.pipeline.task.step.name": step.get("name"),
                    "ci.step.result": step.get("conclusion"),
                    "ci.step.started_at": step.get("started_at"),
                    "ci.step.completed_at": step.get("completed_at"),
                    "ci.step.duration": step.get("duration_seconds"),
                },
            )

    for phase in doc["phases"]:
        add(
            "ci.phase",
            f"{phase['name']}: {phase['duration_seconds']}s",
            **{
                "cicd.pipeline.task.name": job.get("name"),
                "ci.phase.name": phase["name"],
                "ci.phase.started_at": phase["started_at"],
                "ci.phase.ended_at": phase["ended_at"],
                "ci.phase.duration": phase["duration_seconds"],
            },
        )

    cases = (doc["tests"] or {}).get("cases") or []
    ordered = _cap_order(cases)
    for case in ordered[:max_tests]:
        add(
            "ci.test",
            junit_module.test_id(case),
            **{
                "cicd.pipeline.task.name": job.get("name"),
                "test.case.id": junit_module.test_id(case),
                "test.suite.name": case.get("classname") or None,
                "test.case.name": case.get("name"),
                "code.filepath": case.get("file") or None,
                "test.case.result.status": case["outcome"],
                "test.case.duration": case["duration_seconds"],
                "test.case.quarantined": case.get("quarantined"),
            },
        )
    dropped = max(0, len(ordered) - max_tests) + int((doc["tests"] or {}).get("dropped") or 0)
    if dropped:
        records[0]["attributes"]["ci.tests.dropped"] = dropped
    return records


def _cap_order(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failures first (flake and escape detection need every one), then the
    slowest, so any cap drops the fastest passing tests, never a failure."""
    return sorted(cases, key=lambda c: (c["outcome"] not in ("failed", "error"), -c["duration_seconds"]))


def otlp_metrics_body(doc: dict[str, Any], now_ns: int) -> dict[str, Any]:
    metrics = []
    for point in metric_points(doc):
        metrics.append(
            {
                "name": point["name"],
                "unit": point["unit"],
                "gauge": {
                    "dataPoints": [
                        {
                            "timeUnixNano": str(now_ns),
                            "asDouble": point["value"],
                            "attributes": _attrs(point["attributes"]),
                        }
                    ]
                },
            }
        )
    return {
        "resourceMetrics": [
            {
                "resource": resource(doc),
                "scopeMetrics": [{"scope": {"name": "mira_vitals.ci"}, "metrics": metrics}],
            }
        ]
    }


def otlp_logs_bodies(doc: dict[str, Any], now_ns: int, *, max_tests: int, chunk: int = 1000) -> list[dict[str, Any]]:
    records = log_records(doc, max_tests=max_tests)
    bodies = []
    for start in range(0, len(records), chunk):
        bodies.append(
            {
                "resourceLogs": [
                    {
                        "resource": resource(doc),
                        "scopeLogs": [
                            {
                                "scope": {"name": "mira_vitals.ci"},
                                "logRecords": [
                                    {
                                        "timeUnixNano": str(now_ns),
                                        "observedTimeUnixNano": str(now_ns),
                                        "severityNumber": 9,
                                        "severityText": "INFO",
                                        "eventName": r["event"],
                                        "body": {"stringValue": r["body"]},
                                        "attributes": _attrs(r["attributes"]),
                                    }
                                    for r in records[start : start + chunk]
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    return bodies
