"""Optional platform adapters: what only the CI platform knows.

The core measures from inside the job. Some of the most useful numbers
are invisible from there: how long the job sat queued before a runner took
it, every step's timing (including steps that ran before any mark), and the
other jobs of the same run. An adapter asks the platform's API for them
with the job's own short-lived token (``GITHUB_TOKEN``; Forgejo sets the
same variable). No webhook, no org-level app, nothing outside the job.

Every adapter is optional. No token, an unknown platform, a refused or
slow API: the adapter returns ``None`` and a reason, and the report goes
on with what the job measured itself.

The normalized shape (one per run)::

    {"platform": "github" | "forgejo",
     "run": {"id", "attempt", "number", "workflow", "event", "head_sha",
             "head_branch", "created_at", "started_at", "url"},
     "jobs": [{"id", "name", "status", "conclusion", "created_at",
               "started_at", "completed_at", "runner_name", "labels",
               "queue_wait_seconds", "queue_basis", "duration_seconds",
               "steps": [{"number", "name", "status", "conclusion",
                          "started_at", "completed_at",
                          "duration_seconds"}], "self": bool}]}

Times are epoch seconds (floats) in UTC.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime
from typing import Any

from .http import Deadline, request

MAX_PAGES = 5


def _ts(value: Any) -> float | None:
    """ISO-8601 to epoch seconds; None for absent or zero-value times."""
    if not isinstance(value, str) or not value or value.startswith("0001-01-01"):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _span(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return round(end - start, 3)


def detect(env: dict[str, str]) -> str | None:
    """``github``, ``forgejo`` or None. ``MIRA_VITALS_CI_PLATFORM`` wins.

    Forgejo Actions sets the GITHUB_* variables for compatibility, so they
    alone do not identify GitHub: Forgejo's API lives under ``/api/v1``,
    GitHub's at ``api.github.com`` or a GHES ``/api/v3``.
    """
    explicit = (env.get("MIRA_VITALS_CI_PLATFORM") or "").strip().lower()
    if explicit in ("github", "forgejo", "none"):
        return None if explicit == "none" else explicit
    if env.get("FORGEJO_ACTIONS") or env.get("GITEA_ACTIONS"):
        return "forgejo"
    api = env.get("GITHUB_API_URL") or ""
    if api.rstrip("/").endswith("/api/v1"):
        return "forgejo"
    if env.get("GITHUB_ACTIONS") == "true":
        return "github"
    return None


def _token(env: dict[str, str]) -> str | None:
    for key in ("MIRA_VITALS_CI_TOKEN", "GITHUB_TOKEN", "FORGEJO_TOKEN", "GITEA_TOKEN"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    return None


def fetch(env: dict[str, str], deadline: Deadline) -> tuple[dict[str, Any] | None, str]:
    """``(normalized_run_or_None, note)``. Never raises."""
    platform = detect(env)
    if platform is None:
        return None, "no CI platform adapter for this environment"
    token = _token(env)
    if not token:
        return None, f"{platform}: no token in the job (pass GITHUB_TOKEN to the step)"
    try:
        if platform == "github":
            return _github(env, token, deadline)
        return _forgejo(env, token, deadline)
    except Exception as exc:  # noqa: BLE001 - an adapter never fails the report
        return None, f"{platform}: adapter error {type(exc).__name__}: {str(exc)[:200]}"


# --- GitHub Actions -------------------------------------------------------


def _github(env: dict[str, str], token: str, deadline: Deadline) -> tuple[dict[str, Any] | None, str]:
    api = (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    repo = env.get("GITHUB_REPOSITORY") or ""
    run_id = env.get("GITHUB_RUN_ID") or ""
    attempt = env.get("GITHUB_RUN_ATTEMPT") or "1"
    if not (repo and run_id.isdigit() and attempt.isdigit()):
        return None, "github: GITHUB_REPOSITORY/GITHUB_RUN_ID missing"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = f"{api}/repos/{urllib.parse.quote(repo, safe='/')}/actions/runs/{run_id}"
    status, run, error = request("GET", base, headers=headers, deadline=deadline)
    if status != 200 or not isinstance(run, dict):
        return None, f"github: run lookup failed ({error or status}); the job needs `actions: read`"
    jobs_raw: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        status, body, error = request(
            "GET", f"{base}/attempts/{attempt}/jobs?per_page=100&page={page}", headers=headers, deadline=deadline
        )
        if status != 200 or not isinstance(body, dict):
            return None, f"github: jobs lookup failed ({error or status})"
        batch = [j for j in body.get("jobs") or [] if isinstance(j, dict)]
        jobs_raw.extend(batch)
        if len(batch) < 100:
            break
    runner = env.get("RUNNER_NAME") or ""
    jobs = []
    for job in jobs_raw:
        created, started, completed = _ts(job.get("created_at")), _ts(job.get("started_at")), _ts(job.get("completed_at"))
        # A re-run's attempt lists the jobs it did not re-run, carried over
        # from the earlier attempt: their created_at is the new attempt's,
        # after they started. They are flagged and their queue wait and steps
        # dropped, so they are not counted twice.
        carried = created is not None and started is not None and started + 1 < created
        steps = []
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            s_start, s_end = _ts(step.get("started_at")), _ts(step.get("completed_at"))
            steps.append(
                {
                    "number": step.get("number"),
                    "name": str(step.get("name") or ""),
                    "status": step.get("status"),
                    "conclusion": step.get("conclusion"),
                    "started_at": s_start,
                    "completed_at": s_end,
                    "duration_seconds": _span(s_start, s_end),
                }
            )
        jobs.append(
            {
                "id": job.get("id"),
                "name": str(job.get("name") or ""),
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "created_at": created,
                "started_at": started,
                "completed_at": completed,
                "runner_name": job.get("runner_name") or None,
                "runner_group": job.get("runner_group_name") or None,
                "labels": sorted(str(x) for x in job.get("labels") or []),
                # GitHub's job created_at is when the job became queued
                # (its `needs` satisfied), so this is pure runner wait.
                "queue_wait_seconds": None if carried else _span(created, started),
                "queue_basis": "job_created",
                "duration_seconds": _span(started, completed),
                "steps": [] if carried else steps,
                "carried_over": carried,
                "self": bool(runner) and job.get("runner_name") == runner and job.get("status") == "in_progress",
                "url": job.get("html_url"),
            }
        )
    # On a push (a merge to the default branch), the PR that commit merged:
    # the join key for escapes (a main failure the PR run did not catch).
    merged_pr = None
    warnings: list[str] = []
    if run.get("event") == "push" and run.get("head_sha"):
        sha = urllib.parse.quote(str(run["head_sha"]), safe="")
        api_repo = urllib.parse.quote(repo, safe="/")
        status, pulls, error = request("GET", f"{api}/repos/{api_repo}/commits/{sha}/pulls", headers=headers, deadline=deadline)
        if status == 200 and isinstance(pulls, list):
            merged = [p for p in pulls if isinstance(p, dict) and p.get("merged_at") and isinstance(p.get("number"), int)]
            if merged:
                merged_pr = merged[0]["number"]
        else:
            # Without it a merge run cannot be joined to its PR (escapes).
            warnings.append(
                f"github: merged-PR lookup failed ({error or status}); the job needs `pull-requests: read`"
            )
    return (
        {
            "platform": "github",
            "run": {
                "merged_pr_number": merged_pr,
                "id": run.get("id"),
                "attempt": run.get("run_attempt"),
                "number": run.get("run_number"),
                "workflow": run.get("name"),
                "event": run.get("event"),
                "head_sha": run.get("head_sha"),
                "head_branch": run.get("head_branch"),
                "created_at": _ts(run.get("created_at")),
                "started_at": _ts(run.get("run_started_at")),
                "url": run.get("html_url"),
            },
            "jobs": jobs,
            "warnings": warnings,
        },
        f"github: {len(jobs)} job(s)",
    )


# --- Forgejo Actions ------------------------------------------------------


def _forgejo(env: dict[str, str], token: str, deadline: Deadline) -> tuple[dict[str, Any] | None, str]:
    """Forgejo's Actions API exposes *tasks* (a job a runner took) and runs.

    It has no per-job queued time and no per-step timings, so this adapter
    reports less than GitHub's, and says so: a task's queue wait is measured
    from its *run's* creation (``queue_basis: run_created``), an upper bound
    that also contains time spent waiting on ``needs``.
    """
    server = (env.get("GITHUB_SERVER_URL") or "").rstrip("/")
    api = (env.get("GITHUB_API_URL") or (server + "/api/v1" if server else "")).rstrip("/")
    repo = env.get("GITHUB_REPOSITORY") or ""
    number = env.get("GITHUB_RUN_NUMBER") or ""
    if not (api and repo and number.isdigit()):
        return None, "forgejo: GITHUB_API_URL/GITHUB_REPOSITORY/GITHUB_RUN_NUMBER missing"
    headers = {"Authorization": f"token {token}"}
    base = f"{api}/repos/{urllib.parse.quote(repo, safe='/')}/actions"

    run_created = None
    for page in range(1, MAX_PAGES + 1):
        status, body, _ = request("GET", f"{base}/runs?limit=50&page={page}", headers=headers, deadline=deadline)
        if status != 200 or not isinstance(body, dict):
            break
        runs = [r for r in body.get("workflow_runs") or [] if isinstance(r, dict)]
        hit = next((r for r in runs if str(r.get("index_in_repo")) == number), None)
        if hit is not None:
            run_created = _ts(hit.get("created") or hit.get("created_at"))
            break
        if len(runs) < 50:
            break

    tasks: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        status, body, error = request(
            "GET", f"{base}/tasks?limit=50&page={page}", headers=headers, deadline=deadline
        )
        if status != 200 or not isinstance(body, dict):
            return None, f"forgejo: tasks lookup failed ({error or status})"
        batch = [t for t in body.get("workflow_runs") or [] if isinstance(t, dict)]
        mine = [t for t in batch if str(t.get("run_number")) == number]
        tasks.extend(mine)
        # Tasks come newest first; once a page holds only older runs, stop.
        if len(batch) < 50 or (batch and all(_older(t, number) for t in batch)):
            break
    jobs = []
    for task in tasks:
        started = _ts(task.get("run_started_at"))
        done = str(task.get("status") or "") in ("success", "failure", "cancelled", "skipped")
        completed = _ts(task.get("updated_at")) if done else None
        jobs.append(
            {
                "id": task.get("id"),
                "name": str(task.get("name") or ""),
                "status": "completed" if done else task.get("status"),
                "conclusion": task.get("status") if done else None,
                "created_at": _ts(task.get("created_at")),
                "started_at": started,
                "completed_at": completed,
                "runner_name": None,
                "runner_group": None,
                "labels": [],
                "queue_wait_seconds": _span(run_created, started),
                "queue_basis": "run_created",
                "duration_seconds": _span(started, completed),
                "steps": [],
                "carried_over": False,
                "self": False,
                "url": task.get("url"),
            }
        )
    first = tasks[0] if tasks else {}
    return (
        {
            "platform": "forgejo",
            "run": {
                "id": env.get("GITHUB_RUN_ID"),
                "attempt": env.get("GITHUB_RUN_ATTEMPT"),
                "number": int(number),
                "workflow": env.get("GITHUB_WORKFLOW"),
                "event": first.get("event") or env.get("GITHUB_EVENT_NAME"),
                "head_sha": first.get("head_sha") or env.get("GITHUB_SHA"),
                "head_branch": first.get("head_branch") or env.get("GITHUB_REF_NAME"),
                "created_at": run_created,
                "started_at": min((j["started_at"] for j in jobs if j["started_at"]), default=None),
                "url": None,
            },
            "jobs": jobs,
            "warnings": [],
        },
        f"forgejo: {len(jobs)} task(s)",
    )


def _older(task: dict[str, Any], number: str) -> bool:
    try:
        return int(str(task.get("run_number"))) < int(number)
    except (TypeError, ValueError):
        return False
