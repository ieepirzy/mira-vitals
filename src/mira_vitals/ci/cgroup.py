"""The runner's own resource use, from cgroup v2 when it is readable.

A container runner (ARC, a Kubernetes pod, a Docker-based self-hosted
runner) sees its own cgroup at ``/sys/fs/cgroup``. Three questions it
answers that no CI API does:

* was the job CPU-throttled (``cpu.stat`` nr_throttled / throttled_usec)?
  A suite that is slow because its pod has a 2-CPU quota looks exactly like
  a slow suite until this is measured;
* how much CPU did it actually use (``usage_usec``), against its limit
  (``cpu.max``)? Together with wall time that is the job's parallelism;
* how close did it come to its memory limit (``memory.peak`` vs
  ``memory.max``)?

The counters are cumulative for the cgroup's life, which for an ephemeral
runner pod is the job. On a long-lived runner they span jobs; ``start``
records a baseline so the report can subtract it.

On a VM runner with no readable cgroup v2 (GitHub-hosted, macOS) every
value is ``None`` -- "not measured", never zero.
"""

from __future__ import annotations

import os
from typing import Any

ROOT = "/sys/fs/cgroup"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read(4096).strip()
    except OSError:
        return None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def read(root: str = ROOT) -> dict[str, Any]:
    """A raw sample. Every field is ``None`` when unreadable."""
    sample: dict[str, Any] = {
        "cpu_usage_usec": None,
        "cpu_nr_periods": None,
        "cpu_nr_throttled": None,
        "cpu_throttled_usec": None,
        "cpu_limit_cores": None,
        "cpu_quota_usec": None,
        "cpu_period_usec": None,
        "memory_peak_bytes": None,
        "memory_limit_bytes": None,
    }
    stat = _read(os.path.join(root, "cpu.stat"))
    if stat:
        fields = dict(line.split(None, 1) for line in stat.splitlines() if " " in line)
        sample["cpu_usage_usec"] = _int(fields.get("usage_usec"))
        sample["cpu_nr_periods"] = _int(fields.get("nr_periods"))
        sample["cpu_nr_throttled"] = _int(fields.get("nr_throttled"))
        sample["cpu_throttled_usec"] = _int(fields.get("throttled_usec"))
    cpu_max = _read(os.path.join(root, "cpu.max"))
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) == 2:
            sample["cpu_period_usec"] = _int(parts[1])
            if parts[0] != "max":
                quota, period = _int(parts[0]), _int(parts[1])
                sample["cpu_quota_usec"] = quota
                if quota and period:
                    sample["cpu_limit_cores"] = round(quota / period, 3)
    sample["memory_peak_bytes"] = _int(_read(os.path.join(root, "memory.peak")))
    memory_max = _read(os.path.join(root, "memory.max"))
    if memory_max and memory_max != "max":
        sample["memory_limit_bytes"] = _int(memory_max)
    return sample


_COUNTERS = ("cpu_usage_usec", "cpu_nr_periods", "cpu_nr_throttled", "cpu_throttled_usec")


# --- sampling: peak and p95 CPU, not only the average ------------------------

#: Seconds between samples, and the most samples one job keeps (6 h at 5 s).
SAMPLE_INTERVAL = 5.0
MAX_SAMPLES = 4320


def sample_loop(root: str, path: str, stop_path: str, *, interval: float = SAMPLE_INTERVAL,
                max_samples: int = MAX_SAMPLES, sleep=None, clock=None) -> int:
    """Append (time, usage, throttled) samples until told to stop.

    Run detached by ``ci start``. It ends on its own when the stop file
    appears (``ci report`` writes it), when the state directory disappears,
    or after ``max_samples`` -- never later -- and it only ever reads two
    small files. Returns the number of samples written.
    """
    import json as _json
    import os as _os
    import time as _time

    sleep = sleep or _time.sleep
    clock = clock or _time.time
    written = 0
    while written < max_samples:
        if _os.path.exists(stop_path) or not _os.path.isdir(_os.path.dirname(path)):
            break
        stat = read(root)
        if stat["cpu_usage_usec"] is None:
            break  # nothing to sample on this runner
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(_json.dumps({"t": clock(), "u": stat["cpu_usage_usec"],
                                          "th": stat["cpu_throttled_usec"]}) + "\n")
        except OSError:
            break
        written += 1
        sleep(interval)
    return written


def summarize_samples(samples: list[dict]) -> dict:
    """Peak and p95 cores used over the sampling intervals."""
    rates = []
    for a, b in zip(samples, samples[1:]):
        dt = b["t"] - a["t"]
        du = b["u"] - a["u"]
        if dt > 0 and du >= 0:
            rates.append(du / 1e6 / dt)
    if not rates:
        return {"cpu_peak_cores": None, "cpu_p95_cores": None, "cpu_samples": len(samples)}
    ordered = sorted(rates)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return {"cpu_peak_cores": round(ordered[-1], 3), "cpu_p95_cores": round(p95, 3), "cpu_samples": len(samples)}


def delta(end: dict[str, Any], start: dict[str, Any] | None) -> dict[str, Any]:
    """``end`` minus a ``start`` baseline for the cumulative counters.

    A counter that went backwards (a different cgroup, a restarted runner)
    makes the baseline meaningless, so the delta is ``None`` for it rather
    than a negative number.
    """
    out = dict(end)
    out["baseline"] = start is not None
    if not start:
        return out
    for key in _COUNTERS:
        a, b = end.get(key), start.get(key)
        if a is None or b is None:
            out[key] = None if b is not None else a
        else:
            out[key] = a - b if a >= b else None
    return out
