"""Phase marks: timestamps a job records between its steps.

``mira-vitals ci mark <name>`` appends one line to a file in the job's
temporary directory. Phase *i* is the interval that ends at mark *i* and
starts at the mark before it, named after the mark that closes it -- so a
mark placed right after an install step names the install phase::

    mira-vitals ci start           # t0
    ...install...
    mira-vitals ci mark install    # phase "install" = t0 .. now
    ...tests...
    mira-vitals ci mark tests      # phase "tests"   = previous .. now

Marks are local to one job: every job has its own temp directory.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any

#: Phase names become a bounded metric attribute, so they are restricted to a
#: short identifier. Anything else is rejected rather than truncated: a name
#: like a commit message or a file path would quietly turn one series into
#: thousands.
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")

START = "start"


def state_dir(env: dict[str, str] | None = None) -> str:
    source: Mapping[str, str] = os.environ if env is None else env
    explicit = source.get("MIRA_VITALS_CI_DIR")
    if explicit:
        return explicit
    base = source.get("RUNNER_TEMP") or source.get("TMPDIR") or "/tmp"  # noqa: S108 - per-job temp dir in CI
    return os.path.join(base, "mira-vitals-ci")


def marks_path(env: dict[str, str] | None = None) -> str:
    return os.path.join(state_dir(env), "marks.jsonl")


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name))


def record(name: str, *, env: dict[str, str] | None = None, now: float | None = None) -> str:
    """Append a mark. Returns the path written; raises ValueError on a bad name."""
    if not valid_name(name):
        raise ValueError(
            f"mark name {name!r} must be lowercase letters, digits, '_', '.' or '-' (max 48)"
        )
    path = marks_path(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamp = time.time() if now is None else now
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"name": name, "ts": stamp}) + "\n")
    return path


def load(env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every readable mark, oldest first. Unreadable lines are skipped."""
    path = marks_path(env)
    marks: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and isinstance(item.get("ts"), (int, float))
                    and not isinstance(item.get("ts"), bool)
                ):
                    marks.append({"name": item["name"], "ts": float(item["ts"])})
    except OSError:
        return []
    marks.sort(key=lambda m: m["ts"])
    return marks


def phases(marks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive marks as named intervals. The first mark only opens."""
    out = []
    for previous, current in zip(marks, marks[1:]):
        out.append(
            {
                "name": current["name"],
                "started_at": previous["ts"],
                "ended_at": current["ts"],
                "duration_seconds": round(max(0.0, current["ts"] - previous["ts"]), 3),
            }
        )
    return out
