"""``mira-vitals ci start | mark <name> | report``.

Exit status is always 0. Problems are printed as ``::warning::`` lines,
which GitHub and Forgejo both render as annotations, and recorded in the
document's ``warnings``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from typing import Any

from .. import collector_identity
from . import cgroup, export, junit, marks, optimizations, platforms, record
from .http import Deadline

DEFAULT_BUDGET_SECONDS = 60.0
DEFAULT_MAX_TESTS = 20000


_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\u0085]")


def _line(text: Any) -> str:
    """Untrusted text (test, step and job names, warnings) made safe to print.

    The runner reads `::command::` workflow commands from the start of any
    stdout line, and a test name can carry a newline (`&#10;` in JUnit XML).
    So every control character, line and paragraph separators included,
    becomes a space: nothing printed can start a new line.
    """
    return _CONTROL.sub(" ", str(text))


def _cell(text: Any, limit: int = 160) -> str:
    """:func:`_line`, then made safe inside a markdown table cell and code
    span: no pipe ends the cell, no backtick ends the span."""
    return _line(text)[:limit].replace("|", "\\|").replace("`", "'")


def _warn(message: str) -> None:
    # One line: a newline in a warning would let its text start a command.
    print(f"::warning title=mira-vitals ci::{_line(message)[:900]}", flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mira-vitals ci", description="CI-health telemetry. Never fails the build.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start", help="Open the job's clock and take a resource baseline (first step).")
    # Internal: the detached CPU sampler `start` launches.
    sub.add_parser("sample", help=argparse.SUPPRESS)
    mark = sub.add_parser("mark", help="Close a phase named after this mark.")
    mark.add_argument("name")
    report = sub.add_parser("report", help="Measure, write ci-health.json, export (last step, if: always()).")
    report.add_argument("--junit", action="append", default=[], help="JUnit XML file or glob; repeatable.")
    report.add_argument(
        "--cache",
        action="append",
        default=[],
        help="name=hit outcome of a cache restore, e.g. uv=true; repeatable. Empty values are ignored.",
    )
    report.add_argument("--outcome", help="The job's status so far (success/failure/cancelled).")
    report.add_argument("--selection", help="Test-selection record (default: selection.json in the state dir).")
    report.add_argument("--workers-file", help="Per-worker record (default: workers.json in the state dir).")
    report.add_argument("--workers", type=int, help="xdist worker count, when no workers file says it.")
    report.add_argument("--quarantine", help="Quarantined test ids (default: quarantine.json in the state dir).")
    report.add_argument("--output", default="ci-health.json")
    report.add_argument("--no-export", action="store_true", help="Write the document only.")
    report.add_argument("--no-platform", action="store_true", help="Skip the platform adapter.")
    report.add_argument("--max-tests", type=int, default=DEFAULT_MAX_TESTS, help="Cap on exported ci.test records.")
    report.add_argument("--budget", type=float, default=DEFAULT_BUDGET_SECONDS, help="Seconds for the whole report.")
    report.add_argument("--quiet", action="store_true")
    return parser


def _baseline_path(env: dict[str, str]) -> str:
    return os.path.join(marks.state_dir(env), "cgroup-start.json")


def _samples_path(env: dict[str, str]) -> str:
    return os.path.join(marks.state_dir(env), "cpu-samples.jsonl")


def _stop_path(env: dict[str, str]) -> str:
    return os.path.join(marks.state_dir(env), "cpu-samples.stop")


def _start(env: dict[str, str]) -> None:
    marks.record(marks.START, env=env)
    root = env.get("MIRA_VITALS_CGROUP_ROOT") or cgroup.ROOT
    baseline = cgroup.read(root)
    try:
        with open(_baseline_path(env), "w", encoding="utf-8") as handle:
            json.dump(baseline, handle)
    except OSError as exc:
        _warn(f"could not save the resource baseline: {exc}")
    # A detached sampler for peak CPU (the average alone hides contention).
    # Only where the cgroup is readable; it stops itself (see sample_loop).
    if baseline.get("cpu_usage_usec") is not None and env.get("MIRA_VITALS_CI_NO_SAMPLER") != "1":
        import subprocess

        try:
            subprocess.Popen(  # noqa: S603 - our own interpreter and module, fixed argv
                [sys.executable, "-P", "-m", "mira_vitals", "ci", "sample"],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            _warn(f"could not start the CPU sampler: {exc}")


def _sample(env: dict[str, str]) -> None:
    cgroup.sample_loop(env.get("MIRA_VITALS_CGROUP_ROOT") or cgroup.ROOT, _samples_path(env), _stop_path(env))


def _read_samples(env: dict[str, str]) -> list[dict[str, Any]]:
    try:
        with open(_stop_path(env), "w", encoding="utf-8"):
            pass
    except OSError:
        pass
    out: list[dict[str, Any]] = []
    try:
        with open(_samples_path(env), encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and all(isinstance(item.get(k), (int, float)) for k in ("t", "u")):
                    out.append(item)
    except OSError:
        return []
    return out[: cgroup.MAX_SAMPLES]


def _caches(pairs: list[str], env: dict[str, str], warnings: list[str]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    items = list(pairs)
    for key, value in env.items():
        if key.startswith("MIRA_VITALS_CACHE_"):
            items.append(f"{key[len('MIRA_VITALS_CACHE_'):].lower()}={value}")
    for item in items:
        for piece in item.split(","):
            if "=" not in piece:
                continue
            name, value = (s.strip() for s in piece.split("=", 1))
            if not value:
                continue  # an action that never ran leaves its output empty
            if not marks.valid_name(name.lower()):
                warnings.append(f"cache name {name!r} ignored (lowercase identifier, max 48)")
                continue
            out[name.lower()] = value.lower() in ("true", "1", "yes", "hit")
    return out


def _identity(env: dict[str, str], platform: str | None) -> dict[str, Any]:
    server = (env.get("GITHUB_SERVER_URL") or "").rstrip("/")
    repo = env.get("GITHUB_REPOSITORY")
    run_id = env.get("GITHUB_RUN_ID")
    url = f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None

    def as_int(key: str) -> int | None:
        value = env.get(key) or ""
        return int(value) if value.isdigit() else None

    return {
        "repository": repo,
        "platform": platform,
        "workflow": env.get("GITHUB_WORKFLOW"),
        "job": env.get("GITHUB_JOB"),
        "run_id": as_int("GITHUB_RUN_ID"),
        "run_attempt": as_int("GITHUB_RUN_ATTEMPT"),
        "run_number": as_int("GITHUB_RUN_NUMBER"),
        "sha": env.get("GITHUB_SHA"),
        "ref": env.get("GITHUB_REF_NAME"),
        "event": env.get("GITHUB_EVENT_NAME"),
        "runner_name": env.get("RUNNER_NAME"),
        # The node the runner pod ran on, for co-tenancy. ARC does not set it
        # by default: the runner template exposes it through the downward API
        # (env NODE_NAME from spec.nodeName). Without it the store maps the
        # pod (runner_name) to its node from cluster metrics.
        "node_name": next(
            (env[k] for k in ("MIRA_VITALS_NODE_NAME", "NODE_NAME", "K8S_NODE_NAME", "KUBERNETES_NODE_NAME") if env.get(k)),
            None,
        ),
        "runner_os": env.get("RUNNER_OS"),
        "runner_arch": env.get("RUNNER_ARCH"),
        "url": url,
        **optimizations.pr_linkage(env),
        "merged_pr_number": None,
    }


def _summary_markdown(doc: dict[str, Any], export_status: dict[str, Any]) -> str:
    job = doc["job"]

    def s(value: Any) -> str:
        if value is None:
            return "n/a"
        minutes, seconds = divmod(float(value), 60)
        return f"{int(minutes)}m {seconds:04.1f}s" if minutes else f"{seconds:.1f}s"

    lines = [f"### CI health: {_cell(job.get('name'))}", ""]
    lines.append(f"- Queue wait: **{s(job.get('queue_wait_seconds'))}** ({job.get('queue_basis') or 'no platform data'})")
    lines.append(f"- Job so far: **{s(job.get('wall_seconds'))}** ({job.get('wall_basis') or 'no clock'})")
    runner = doc["runner"] or {}
    if runner.get("cpu_usage_usec") is not None:
        throttled = runner.get("cpu_throttled_usec")
        peak = runner.get("memory_peak_bytes")
        lines.append(
            f"- Runner CPU: {s(runner['cpu_usage_usec'] / 1e6)} used, "
            f"{s(throttled / 1e6) if throttled is not None else 'n/a'} throttled, "
            f"limit {runner.get('cpu_limit_cores') or 'none'} cores; "
            f"memory peak {f'{round(peak / 2**20)} MiB' if peak is not None else 'n/a'}"
            + ("" if runner.get("baseline") else " (no start baseline: counters may span earlier work)")
        )
    if doc["caches"]:
        lines.append("- Caches: " + ", ".join(f"{k} {'hit' if v else 'miss'}" for k, v in sorted(doc["caches"].items())))
    steps = sorted(
        (st for st in job.get("steps") or [] if st.get("duration_seconds") is not None),
        key=lambda st: st["duration_seconds"],
        reverse=True,
    )[:8]
    if steps:
        lines += ["", "| Slowest steps | Time |", "|---|---|"]
        lines += [f"| {_cell(st['name'])} | {s(st['duration_seconds'])} |" for st in steps]
    if doc["phases"]:
        lines += ["", "| Phase | Time |", "|---|---|"]
        lines += [f"| {_cell(p['name'])} | {s(p['duration_seconds'])} |" for p in doc["phases"]]
    summary = (doc["tests"] or {}).get("summary")
    if summary:
        lines += [
            "",
            f"**Tests:** {summary['count']} in {s(summary['total_seconds'])} (sum of test times); "
            f"p50 {s(summary['p50_seconds'])}, p90 {s(summary['p90_seconds'])}, p99 {s(summary['p99_seconds'])}; "
            f"{summary['tests_for_80pct_time']} tests take 80% of the time; "
            f"slowest 10% take {round((summary['slowest_10pct_share'] or 0) * 100)}%.",
            "",
            "| Slowest tests | Time |",
            "|---|---|",
        ]
        lines += [f"| `{_cell(junit.test_id(c), 120)}` | {s(c['duration_seconds'])} |" for c in summary["slowest"][:10]]
    other = [
        j
        for j in (doc.get("platform") or {}).get("jobs") or []
        if not j.get("self") and j.get("queue_wait_seconds") is not None
    ]
    if other:
        lines += ["", "| Other jobs in this run | Queue wait | Duration |", "|---|---|---|"]
        lines += [f"| {_cell(j['name'])} | {s(j['queue_wait_seconds'])} | {s(j['duration_seconds'])} |" for j in other]
    lines += ["", f"_Export: metrics {export_status.get('metrics')}, logs {export_status.get('logs')}._"]
    return "\n".join(lines) + "\n"


def _report(args: argparse.Namespace, env: dict[str, str]) -> None:
    deadline = Deadline(max(5.0, args.budget))
    warnings: list[str] = []
    platform_name = None if args.no_platform else platforms.detect(env)

    platform, note = (None, "platform adapter disabled")
    if not args.no_platform:
        platform, note = platforms.fetch(env, deadline)
        if platform is None and platform_name is not None:
            warnings.append(note)
        if platform is not None:
            warnings.extend(platform.pop("warnings", None) or [])

    all_marks = marks.load(env)
    cases, files, problems = junit.collect(args.junit) if args.junit else ([], [], [])
    warnings.extend(problems)

    baseline = None
    try:
        with open(_baseline_path(env), encoding="utf-8") as handle:
            baseline = json.load(handle)
    except (OSError, json.JSONDecodeError):
        baseline = None
    runner = cgroup.delta(cgroup.read(env.get("MIRA_VITALS_CGROUP_ROOT") or cgroup.ROOT), baseline)
    runner.update(cgroup.summarize_samples(_read_samples(env)))

    identity = collector_identity()
    # The action runs the code from its own checkout, so the ref it was
    # pinned to is the truth -- not whatever mira-vitals distribution happens
    # to be installed on the runner, whose metadata collector_identity reads.
    if env.get("MIRA_VITALS_CI_REVISION"):
        identity["revision"] = env["MIRA_VITALS_CI_REVISION"]
        identity["source"] = "action-checkout"

    ident = _identity(env, (platform or {}).get("platform") or platform_name)
    ident["merged_pr_number"] = ((platform or {}).get("run") or {}).get("merged_pr_number")
    opts = {
        "selection": optimizations.selection(
            args.selection or optimizations.default_path(env, "selection.json"), warnings
        ),
        "parallelism": optimizations.workers(
            args.workers_file or optimizations.default_path(env, "workers.json"), args.workers, warnings
        ),
    }
    quarantined = optimizations.quarantine(
        args.quarantine or optimizations.default_path(env, "quarantine.json"), warnings
    )
    for case in cases:
        case["quarantined"] = junit.test_id(case) in quarantined

    doc = record.build(
        identity=ident,
        platform=platform,
        platform_note=note,
        marks=all_marks,
        phases=marks.phases(all_marks),
        tests=cases,
        test_files=files,
        caches=_caches(args.cache, env, warnings),
        runner=runner,
        outcome=args.outcome,
        warnings=warnings,
        generated_by=identity,
        optimizations=opts,
        max_cases=max(0, args.max_tests),
    )

    export_status: dict[str, Any] = {"metrics": "skipped", "logs": "skipped", "notes": ["--no-export"]}
    if not args.no_export:
        now_ns = int(time.time() * 1e9)
        export_status = export.send(
            env,
            metrics_body=record.otlp_metrics_body(doc, now_ns),
            logs_bodies=record.otlp_logs_bodies(doc, now_ns, max_tests=max(0, args.max_tests)),
            deadline=deadline,
        )
    doc["export"] = export_status
    for item in export_status.get("notes") or []:
        if item not in ("--no-export",) and "no OTLP endpoint" not in item:
            warnings.append(item)

    try:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        warnings.append(f"could not write {args.output}: {exc}")

    markdown = _summary_markdown(doc, export_status)
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(markdown)
        except OSError:
            pass
    if not args.quiet:
        print(markdown)
    for message in warnings:
        _warn(message)


def _deadline_seconds(budget: float) -> float:
    """The whole command's wall-clock limit: the request budget plus room
    for parsing and writing."""
    return max(5.0, budget) + 15.0


class _OutOfTime(Exception):
    pass


def _on_alarm(signum: int, frame: Any) -> None:
    raise _OutOfTime()


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    """Always returns 0. See the module docstring."""
    env = dict(os.environ if env is None else env)
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        _warn("bad arguments; nothing measured")
        return 0
    # A wall-clock deadline for the whole command, on top of the per-request
    # timeouts: parsing a huge report or a slow disk must not hold a job up
    # either. POSIX main thread only; elsewhere the request budget still holds.
    budget = getattr(args, "budget", DEFAULT_BUDGET_SECONDS)
    deadline_set = False
    previous: Any = None
    if hasattr(signal, "SIGALRM"):
        try:
            previous = signal.signal(signal.SIGALRM, _on_alarm)
            signal.setitimer(signal.ITIMER_REAL, _deadline_seconds(budget))
            deadline_set = True
        except ValueError:  # not the main thread
            pass
    try:
        if args.command == "start":
            _start(env)
        elif args.command == "sample":
            _sample(env)
        elif args.command == "mark":
            marks.record(args.name, env=env)
        elif args.command == "report":
            _report(args, env)
        else:
            _warn("usage: mira-vitals ci start | mark <name> | report")
    except _OutOfTime:
        _warn(f"{args.command} ran out of its {budget:.0f} s budget; stopped, nothing more measured")
    except Exception as exc:  # noqa: BLE001 - the contract: never fail the build
        _warn(f"{args.command or 'ci'} failed: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        if deadline_set:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
