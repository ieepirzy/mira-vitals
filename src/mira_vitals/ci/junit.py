"""Per-test durations from JUnit XML (pytest ``--junitxml``, vitest's junit
reporter, and anything else that writes the de-facto schema).

The code-health ``tests`` section already counts outcomes; this module keeps
every test case's own duration, because the question here is different:
not "did the suite pass" but "where does its time go". A suite whose 50
slowest tests take 80% of its runtime has a different fix from one where the
time is spread evenly, and only the per-test distribution tells them apart.
"""

from __future__ import annotations

import glob
import math
import os
import xml.etree.ElementTree as ET  # noqa: S405 - our own CI's report, size-capped below
from typing import Any

#: A JUnit file larger than this is not parsed. Real suites of tens of
#: thousands of tests stay well under it; anything bigger is a mistake or an
#: attack on the runner's memory, and either way not worth an OOM-killed
#: telemetry step.
MAX_FILE_BYTES = 64 * 1024 * 1024


def _outcome(case: ET.Element) -> str:
    for child in case:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "failure":
            return "failed"
        if tag == "error":
            return "error"
        if tag == "skipped":
            return "skipped"
    return "passed"


def _seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return 0.0
    return number if number >= 0 and math.isfinite(number) else 0.0


def _time_measured(value: str | None) -> bool:
    if not value:
        return False
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return False
    return math.isfinite(number) and number >= 0


def parse_file(path: str) -> list[dict[str, Any]]:
    """Every ``<testcase>`` in one file. Raises ValueError/ET.ParseError."""
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise ValueError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    tree = ET.parse(path)  # noqa: S314 - see the import note
    cases = []
    for case in tree.iter():
        if case.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        cases.append(
            {
                "classname": case.get("classname") or "",
                "name": case.get("name") or "",
                "file": case.get("file") or "",
                "duration_seconds": round(_seconds(case.get("time")), 6),
                "duration_measured": _time_measured(case.get("time")),
                "outcome": _outcome(case),
            }
        )
    return cases


def expand(patterns: list[str]) -> list[str]:
    """Glob patterns to existing files, de-duplicated, in order."""
    seen: list[str] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=True)) or ([pattern] if os.path.isfile(pattern) else [])
        for match in matches:
            if match not in seen:
                seen.append(match)
    return seen


def collect(patterns: list[str]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """``(cases, files_read, problems)``. Never raises."""
    cases: list[dict[str, Any]] = []
    problems: list[str] = []
    files = expand(patterns)
    if patterns and not files:
        problems.append(f"no JUnit report found for {', '.join(patterns)}")
    read: list[str] = []
    for path in files:
        try:
            cases.extend(parse_file(path))
            read.append(path)
        except (OSError, ValueError, ET.ParseError) as exc:
            problems.append(f"could not read JUnit report {path}: {exc}")
    return cases, read, problems


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    # Nearest-rank: a value that actually occurred, never an interpolation.
    rank = max(1, min(len(sorted_values), int(-(-q * len(sorted_values) // 1))))
    return sorted_values[rank - 1]


def summarize(cases: list[dict[str, Any]], *, top: int = 20) -> dict[str, Any]:
    """Counts, total, percentiles and how concentrated the time is."""
    durations = sorted(c["duration_seconds"] for c in cases)
    total = sum(durations)
    by_outcome: dict[str, int] = {}
    for case in cases:
        by_outcome[case["outcome"]] = by_outcome.get(case["outcome"], 0) + 1

    # How many of the slowest tests account for 80% of the suite's time, and
    # what share the slowest 1% and 10% take. Ratios, so they compare across
    # repositories of different sizes.
    descending = durations[::-1]
    to_80 = None
    if total > 0:
        running = 0.0
        for index, value in enumerate(descending, start=1):
            running += value
            if running >= 0.8 * total:
                to_80 = index
                break

    def share(fraction: float) -> float | None:
        if total <= 0 or not descending:
            return None
        count = max(1, int(len(descending) * fraction))
        return round(sum(descending[:count]) / total, 4)

    slowest = sorted(cases, key=lambda c: c["duration_seconds"], reverse=True)[:top]
    return {
        "count": len(cases),
        "by_outcome": by_outcome,
        "total_seconds": round(total, 3),
        "p50_seconds": _percentile(durations, 0.50),
        "p90_seconds": _percentile(durations, 0.90),
        "p99_seconds": _percentile(durations, 0.99),
        "max_seconds": durations[-1] if durations else None,
        "tests_for_80pct_time": to_80,
        "slowest_1pct_share": share(0.01),
        "slowest_10pct_share": share(0.10),
        "slowest": slowest,
    }


def test_id(case: dict[str, Any]) -> str:
    """The test's pytest node id, ``path/to/test_x.py::Class::test_name``.

    A stable identity for trend, flake and escape queries, and the form a
    quarantine list names tests in. pytest's default JUnit family (xunit2)
    writes no ``file`` attribute, only a dotted ``classname`` such as
    ``tests.test_x.TestThing``; the module path is the dotted prefix before
    the first segment that names a class (``Test...``), the rest is the class
    chain. With ``file`` present (xunit1) it is used as is.
    """
    name = case.get("name") or ""
    classname = case.get("classname") or ""
    path = (case.get("file") or "").replace("\\", "/")
    segments = [s for s in classname.split(".") if s]
    classes: list[str] = []
    if path:
        module_segments = path[:-3].replace("/", ".").split(".") if path.endswith(".py") else []
        if module_segments and segments[: len(module_segments)] == module_segments:
            classes = segments[len(module_segments):]
    else:
        split = next((i for i, s in enumerate(segments) if s[:1].isupper()), len(segments))
        module, classes = segments[:split], segments[split:]
        path = "/".join(module) + ".py" if module else ""
    return "::".join(p for p in (path, *classes, name) if p)


def normalize_id(value: str) -> str:
    """A quarantine-list id in the same form as :func:`test_id`."""
    value = value.strip().replace("\\", "/")
    head, sep, tail = value.partition("::")
    if not sep or "/" in head or head.endswith(".py"):
        return value  # already a node id (or not a test id at all)
    # A dotted classname form ("tests.test_x.TestT::test_y") is converted.
    return test_id({"classname": head, "name": tail})
