"""Run and CI identity, gathered from the environment.

CI-provider facts only: who ran this, on what commit, on which branch.  Nothing
here classifies authorship -- that is :mod:`mira_vitals.provenance`, and
keeping the two apart is what stops "GitHub says the actor is X" from quietly
becoming an authorship claim.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import Any

from .schema import observation_id


def utc_now_iso() -> str:
    """UTC, second resolution, always ``Z``-suffixed.

    Machine-consistent by construction: no local timezone ever enters the
    record, so snapshots from a self-hosted runner in Helsinki and a hosted
    runner in us-east sort correctly against each other.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _git(args: list[str], *, cwd: str | None = None) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def collect(
    env: dict[str, str] | None = None,
    *,
    repo_root: str | None = None,
    repository_override: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(run, ci)`` blocks."""
    env = dict(os.environ if env is None else env)

    github_repository = env.get("GITHUB_REPOSITORY")  # "owner/name"
    repository = (
        repository_override
        or env.get("CODE_HEALTH_REPOSITORY")
        or (github_repository.split("/")[-1] if github_repository else None)
        or os.path.basename(os.path.abspath(repo_root or "."))
    )
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repository_url = (
        env.get("CODE_HEALTH_REPOSITORY_URL")
        or (f"{server}/{github_repository}" if github_repository else None)
        or _git(["config", "--get", "remote.origin.url"], cwd=repo_root)
    )

    event = env.get("GITHUB_EVENT_NAME")
    is_pull_request = event == "pull_request"

    if is_pull_request:
        # GITHUB_SHA on a pull_request event points at the ephemeral merge
        # commit GitHub creates, which exists nowhere in the repository's
        # history.  Recording it would make every PR observation unjoinable to
        # any other record of the change, so the head SHA is used instead.
        commit_sha = env.get("GITHUB_EVENT_PULL_REQUEST_HEAD_SHA") or _git(["rev-parse", "HEAD"], cwd=repo_root)
        branch = env.get("GITHUB_HEAD_REF")
    else:
        commit_sha = env.get("GITHUB_SHA") or _git(["rev-parse", "HEAD"], cwd=repo_root)
        branch = env.get("GITHUB_REF_NAME") or _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)

    default_branch = env.get("CODE_HEALTH_DEFAULT_BRANCH") or env.get("GITHUB_DEFAULT_BRANCH") or "main"
    is_default_branch = bool(branch) and branch == default_branch and not is_pull_request

    base_ref = env.get("GITHUB_BASE_REF") or (None if is_default_branch else default_branch)
    merge_base = None
    if base_ref:
        merge_base = _git(["merge-base", f"origin/{base_ref}", "HEAD"], cwd=repo_root) or _git(
            ["merge-base", base_ref, "HEAD"], cwd=repo_root
        )

    run = {
        "repository": repository,
        "repository_url": repository_url,
        "commit_sha": commit_sha,
        # The tree that was actually measured. On a pull request this is
        # GitHub's synthetic merge commit, which differs from `commit_sha`
        # above and exists nowhere in the repository's history -- so it
        # identifies the measurement without being joinable, which is exactly
        # the opposite of `commit_sha` and why both are kept.
        "analyzed_tree_sha": _git(["rev-parse", "HEAD"], cwd=repo_root),
        "branch": branch,
        "default_branch": default_branch,
        "is_default_branch": is_default_branch,
        # The bounded metric dimension.  Exactly two values, ever.
        "ref_class": "default_branch" if is_default_branch else "other",
        "change_id": env.get("GITHUB_PR_NUMBER") or env.get("CODE_HEALTH_CHANGE_ID"),
        "base_ref": base_ref,
        "merge_base_sha": merge_base,
        "timestamp": utc_now_iso(),
        # Default-branch runs are the canonical historical series; PR runs are
        # observations of a proposal that may never land.  Marked so analysis
        # can filter without re-deriving the rule.
        "canonical": is_default_branch,
        "observation_id": "",  # filled below, once target paths are known
    }

    ci = {
        "provider": "github-actions" if env.get("GITHUB_ACTIONS") == "true" else env.get("CODE_HEALTH_CI_PROVIDER"),
        "workflow": env.get("GITHUB_WORKFLOW"),
        "job": env.get("GITHUB_JOB"),
        "run_id": env.get("GITHUB_RUN_ID"),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "run_number": env.get("GITHUB_RUN_NUMBER"),
        "event_name": event,
        "runner_os": env.get("RUNNER_OS"),
        "runner_environment": env.get("RUNNER_ENVIRONMENT"),
        "actor": env.get("GITHUB_ACTOR"),
    }
    return run, ci


def finalize_observation_id(run: dict[str, Any], target_paths: list[str]) -> None:
    """Stamp the dedup key, once the analyzed paths are known."""
    run["observation_id"] = observation_id(
        repository=run["repository"],
        commit_sha=run["commit_sha"],
        target_paths=target_paths,
        analyzed_tree_sha=run.get("analyzed_tree_sha"),
    )
