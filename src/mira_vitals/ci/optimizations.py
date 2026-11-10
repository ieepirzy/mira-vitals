"""How well the CI's own optimizations work, recorded beside the timings.

Timings say a job got faster. They do not say *why*, or whether it got
faster by testing less than it should. These records make each optimization
measurable on its own and make its safety measurable at all:

test selection
    Was the run full or a selection, which full-run triggers fired, how many
    tests ran out of how many, the estimated time saved, which selector and
    graph produced it (so a selector upgrade is a before/after boundary), and
    how many tests a content-addressed (Merkle) closure cache let skip.
parallelism
    pytest-xdist worker count, each worker's wall time, the imbalance
    between them, and whether each worker had its own database.
quarantine
    Which test ids are quarantined, so a quarantined failure is not read as
    a regression and the flake rate can exclude them.
PR linkage
    The pull request a run belongs to (PR runs) or merged (default-branch
    runs), so a main failure can be joined back to the PR run that should
    have caught it: an *escape*, the safety metric that decides whether
    selection can be trusted.

Producers write small JSON files into the job's state directory (or pass
paths with ``--selection`` / ``--workers`` / ``--quarantine``); ``ci report``
reads them. Each file is optional, read defensively, and a bad file is a
warning, never a failure. The file contracts are below and in
``docs/ci-health.md``; ``mira-vitals ci select`` (``docs/ci-select.md``)
writes ``selection.json`` itself, adding ``policy``, ``decision``,
``count_unit`` and ``skipped_files``, from which ``ci report`` counts escapes.

``selection.json``::

    {"mode": "full" | "selected",
     "triggers": ["conftest", "migrations", "deps", "ci", "non-py",
                  "deleted-py", "conftest-closure", ...],   # why full, if full
     "selected": 812, "total": 2629,
     "estimated_saved_seconds": 940.0,
     "selector": {"name": "mira-vitals-select", "version": "0.2.0"},
     "graph": {"source": "ruff", "ruff_version": "0.16.4",
               "lomitus_version": "1.3.0"},
     "closure_cache_hits": 1500}

``workers.json`` (one entry per xdist worker)::

    {"workers": [{"id": "gw0", "wall_seconds": 301.2, "tests": 655,
                  "db_isolated": true}, ...]}

``quarantine.json``::

    {"test_ids": ["tests/test_x.py::test_flaky", ...]}
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from . import marks

#: Known full-run trigger names. Others are kept (a selector may add one)
#: but must be short identifiers, since they become a bounded dimension.
KNOWN_TRIGGERS = (
    "conftest",
    "migrations",
    "deps",
    "ci",
    "non-py",
    "deleted-py",
    "conftest-closure",
    "docker",
    "unresolved",
    "unknown-change",
    "unsafe-path",
    "selector_error",
)
_IDENT = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
MAX_FILE_BYTES = 8 * 1024 * 1024


def _load(path: str | None, warnings: list[str], what: str) -> Any:
    if not path or not os.path.exists(path):
        return None
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            warnings.append(f"{what} file {path} is larger than {MAX_FILE_BYTES} bytes; ignored")
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        warnings.append(f"could not read {what} file {path}: {exc}")
        return None


def _num(value: Any, *, integer: bool = False) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:  # NaN or negative
        return None
    return int(value) if integer else float(value)


def _ident(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENT.match(value) else None


def _version(value: Any) -> str | None:
    return value if isinstance(value, str) and _VERSION.match(value) else None


def selection(path: str | None, warnings: list[str]) -> dict[str, Any] | None:
    raw = _load(path, warnings, "selection")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        warnings.append("selection file is not a JSON object; ignored")
        return None
    mode = raw.get("mode")
    if mode not in ("full", "selected"):
        warnings.append(f"selection mode {mode!r} is not 'full' or 'selected'; ignored")
        return None
    triggers = []
    for trigger in raw.get("triggers") or []:
        if isinstance(trigger, str) and _IDENT.match(trigger):
            triggers.append(trigger)
        else:
            warnings.append(f"selection trigger {trigger!r} ignored (short lowercase identifier)")
    selector: dict[str, Any] = raw["selector"] if isinstance(raw.get("selector"), dict) else {}
    graph: dict[str, Any] = raw["graph"] if isinstance(raw.get("graph"), dict) else {}
    selected, total = _num(raw.get("selected"), integer=True), _num(raw.get("total"), integer=True)
    if selected is not None and total is not None and selected > total:
        warnings.append("selection says more tests selected than exist; counts ignored")
        selected = total = None
    return {
        "mode": mode,
        "triggers": sorted(set(triggers)),
        "selected": selected,
        "total": total,
        "estimated_saved_seconds": _num(raw.get("estimated_saved_seconds")),
        "selector_name": _ident(selector.get("name")),
        "selector_version": _version(selector.get("version")),
        "graph_source": _ident(graph.get("source")),
        "ruff_version": _version(graph.get("ruff_version")),
        "lomitus_version": _version(graph.get("lomitus_version")),
        "closure_cache_hits": _num(raw.get("closure_cache_hits"), integer=True),
        # From `mira-vitals ci select`: the policy (advisory runs everything
        # and only records), the selector's own decision, the unit of the
        # counts, and the files it would skip -- read by `ci report` to count
        # escapes, then dropped (see command._report).
        "policy": raw.get("policy") if raw.get("policy") in ("advisory", "enforce") else None,
        "decision": raw.get("decision") if raw.get("decision") in ("full", "selected") else None,
        "count_unit": raw.get("count_unit") if raw.get("count_unit") in ("tests", "files") else None,
        "skipped_files": _paths(raw.get("skipped_files")),
        # Gates (test-level selection by keyword): the keywords whose tests a
        # closed gate would skip, read by `ci report` to count escapes like
        # skipped_files, then dropped; which gates were closed; and which
        # keywords this run did deselect.
        "skipped_keywords": _keywords(raw.get("skipped_keywords")),
        "deselected_keywords": _keywords(raw.get("deselected_keywords")) or [],
        "gates_closed": sorted(
            {
                g["name"]
                for g in raw.get("gates") or []
                if isinstance(g, dict) and g.get("open") is False and _ident(g.get("name"))
            }
        ),
    }


_KEYWORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,80}$")


def _keywords(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return sorted({v for v in value[:50] if isinstance(v, str) and _KEYWORD.match(v)})


_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,300}$")


def _paths(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [v for v in value[:5000] if isinstance(v, str) and _PATH.match(v)]


def workers(path: str | None, declared: int | None, warnings: list[str]) -> dict[str, Any] | None:
    raw = _load(path, warnings, "workers")
    entries = []
    if isinstance(raw, dict):
        for item in raw.get("workers") or []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            entries.append(
                {
                    "id": item["id"][:32],
                    "wall_seconds": _num(item.get("wall_seconds")),
                    "tests": _num(item.get("tests"), integer=True),
                    "db_isolated": item.get("db_isolated") if isinstance(item.get("db_isolated"), bool) else None,
                }
            )
    elif raw is not None:
        warnings.append("workers file is not a JSON object; ignored")
    if not entries and declared is None:
        return None
    walls = [w["wall_seconds"] for w in entries if w["wall_seconds"] is not None]
    mean = sum(walls) / len(walls) if walls else None
    isolation = {w["db_isolated"] for w in entries if w["db_isolated"] is not None}
    return {
        "count": declared if declared is not None else len(entries),
        "workers": entries,
        "wall_max_seconds": max(walls) if walls else None,
        "wall_mean_seconds": round(mean, 3) if mean is not None else None,
        # max/mean: 1.0 is perfectly balanced; 2.0 means one worker took
        # twice the average and the rest waited for it.
        "imbalance": round(max(walls) / mean, 4) if walls and mean else None,
        # A string, so the column has one type: "true" | "false" | "mixed".
        "db_isolated": (
            ("true" if isolation.pop() else "false") if len(isolation) == 1 else ("mixed" if isolation else None)
        ),
    }


def quarantine(path: str | None, warnings: list[str]) -> set[str]:
    raw = _load(path, warnings, "quarantine")
    if raw is None:
        return set()
    ids = raw.get("test_ids") if isinstance(raw, dict) else None
    if not isinstance(ids, list):
        warnings.append("quarantine file has no test_ids list; ignored")
        return set()
    from .junit import normalize_id

    return {normalize_id(i) for i in ids if isinstance(i, str) and i.strip()}


def default_path(env: dict[str, str], name: str) -> str:
    return os.path.join(marks.state_dir(env), name)


def pr_linkage(env: dict[str, str]) -> dict[str, Any]:
    """The PR a run belongs to, from the ref or the event payload."""
    out: dict[str, Any] = {"pr_number": None, "base_sha": None, "head_sha": None, "trigger_at": None}
    ref = env.get("GITHUB_REF") or ""
    match = re.match(r"^refs/pull/(\d+)/", ref)
    if match:
        out["pr_number"] = int(match.group(1))
    event_path = env.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            if os.path.getsize(event_path) <= MAX_FILE_BYTES:
                with open(event_path, encoding="utf-8") as handle:
                    event = json.load(handle)
                # The trigger time for feedback latency: a push's own time
                # (repository.pushed_at, epoch seconds). Other events fall
                # back to the run's creation in the store.
                repo_info = event.get("repository") if isinstance(event, dict) else None
                pushed = repo_info.get("pushed_at") if isinstance(repo_info, dict) else None
                if isinstance(pushed, (int, float)) and not isinstance(pushed, bool) and "pusher" in event:
                    out["trigger_at"] = float(pushed)
                pr = event.get("pull_request") if isinstance(event, dict) else None
                if isinstance(pr, dict):
                    if isinstance(pr.get("number"), int):
                        out["pr_number"] = pr["number"]
                    base, head = pr.get("base") or {}, pr.get("head") or {}
                    out["base_sha"] = base.get("sha") if isinstance(base, dict) else None
                    out["head_sha"] = head.get("sha") if isinstance(head, dict) else None
        except (OSError, ValueError):
            pass
    return out
