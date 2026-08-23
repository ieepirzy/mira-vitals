"""Test-result and coverage adapter.

Reads machine-readable reports rather than scraping console output: a JUnit XML
file for test outcomes and a ``coverage.py`` XML or JSON report for coverage.
Both are optional and independently missing-able -- a repository with tests but
no coverage instrumentation yields real test counts and a ``None`` coverage,
which is the honest representation and not ``0.0``.

This adapter deliberately does not *run* the test suite.  CI already runs it,
usually inside a container or a matrix; re-running it here would double the
cost and could disagree with the run that actually gates the build.  The
collector consumes the artifacts that run produced.
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from typing import Any

from .base import ToolRun


def collect(
    *,
    junit_path: str | None = None,
    coverage_path: str | None = None,
    python_version: str | None = None,
) -> tuple[dict[str, Any], ToolRun]:
    tool = ToolRun(name="tests")
    data: dict[str, Any] = {
        "status": "skipped",
        "passed": None,
        "failed": None,
        "errors": None,
        "skipped": None,
        "total": None,
        "duration_seconds": None,
        "coverage_percent": None,
        "coverage_source": None,
        "junit_source": None,
        # Which interpreter ran the suite these reports came from.  Coverage
        # differs between versions wherever a branch is version-gated, so a
        # coverage series that silently changes interpreter is not one series.
        "python_version": python_version,
    }

    # Two different facts, and collapsing them is what made a cancelled test
    # leg fail the whole lane. A report that was *never produced* (the upstream
    # job was cancelled, or did not run) is a measurement we do not have --
    # `skipped`. A report that exists and cannot be parsed is a measurement we
    # thought we had and do not -- `error`, which fails the run.
    missing: list[str] = []
    problems: list[str] = []

    if junit_path:
        if not os.path.exists(junit_path):
            missing.append(f"junit report not produced: {junit_path}")
        else:
            try:
                data.update(_parse_junit(junit_path))
                data["junit_source"] = junit_path
                data["status"] = "ok"
            except (ET.ParseError, ValueError) as exc:
                problems.append(f"could not parse junit xml {junit_path}: {exc}")

    if coverage_path:
        if not os.path.exists(coverage_path):
            missing.append(f"coverage report not produced: {coverage_path}")
        else:
            try:
                percent = _parse_coverage(coverage_path)
            except (ET.ParseError, ValueError, json.JSONDecodeError, KeyError) as exc:
                problems.append(f"could not parse coverage report {coverage_path}: {exc}")
            else:
                data["coverage_percent"] = percent
                data["coverage_source"] = coverage_path
                if data["status"] == "skipped":
                    data["status"] = "ok"

    if problems:
        # A partially-read report is a partial measurement.  Whatever was read
        # is kept, but the status says the reading was not clean.
        data["status"] = "error"
        data["error"] = "; ".join(problems + missing)
        tool.status = "error"
        tool.error = data["error"]
    elif missing and data["status"] == "skipped":
        # Nothing was produced at all. The reason is recorded rather than
        # discarded, so "not produced" stays distinguishable from "never
        # configured" when reading the artifact later.
        tool.status = "skipped"
        tool.error = "; ".join(missing)
        data["error"] = tool.error
    elif missing:
        # One report arrived and the other did not. What was read is kept and
        # the gap is recorded; it is not a failure.
        data["error"] = "; ".join(missing)
    elif data["status"] == "skipped":
        tool.status = "skipped"
        tool.error = "no junit or coverage report configured"

    return data, tool


def _parse_junit(path: str) -> dict[str, Any]:
    """Sum JUnit counters across suites.

    pytest emits a single ``<testsuite>`` wrapped in ``<testsuites>``; other
    runners emit several, and some nest them.  Two ways to double-count here:

    * summing the ``<testsuites>`` root's own attributes as well as its
      children, when the wrapper repeats their totals; and
    * summing a parent ``<testsuite>`` together with the child suites nested
      inside it, whose tests the parent's counters already include.

    Only *leaf* suites -- those containing no nested ``<testsuite>`` -- are
    summed, which is correct for both flat and hierarchical reports.
    """
    root = ET.parse(path).getroot()
    candidates = list(root.iter("testsuite")) if root.tag == "testsuites" else [root]
    # A suite that contains another suite is an aggregate of it.
    suites = [s for s in candidates if s.find("testsuite") is None]

    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    duration = 0.0
    seen = False
    for suite in suites:
        seen = True
        for key in totals:
            totals[key] += int(suite.get(key, 0) or 0)
        duration += float(suite.get("time", 0) or 0)
    if not seen:
        raise ValueError("no <testsuite> element found")

    failed = totals["failures"]
    errored = totals["errors"]
    skipped = totals["skipped"]
    passed = totals["tests"] - failed - errored - skipped
    return {
        "passed": passed,
        "failed": failed,
        "errors": errored,
        "skipped": skipped,
        "total": totals["tests"],
        "duration_seconds": duration,
    }


def _parse_coverage(path: str) -> float:
    """Line coverage as a percentage in [0, 100].

    coverage.py's XML puts a 0-1 fraction in ``line-rate``; its JSON report
    puts a 0-100 percentage in ``totals.percent_covered``.  They are rescaled
    to the same unit here, and the raw denominator stays available in the
    source report -- no rounding is applied.
    """
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return float(payload["totals"]["percent_covered"])

    root = ET.parse(path).getroot()
    line_rate = root.get("line-rate")
    if line_rate is None:
        raise ValueError("coverage xml has no line-rate attribute")
    return float(line_rate) * 100.0
