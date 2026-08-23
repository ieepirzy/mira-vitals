"""Explicit, trusted code provenance.

The point of this module is what it refuses to do.  Authorship is never
inferred from commit-message text, co-author trailers written by anyone who can
push, author names, or "this looks like an agent wrote it".  Those are guesses,
and a dataset built to compare human and AI-assisted development cannot be
built on guesses -- a heuristic that is 90% accurate produces a 10% mislabel
rate that is *correlated with the thing being measured*, which is worse than no
data at all.

So provenance is only ever populated from sources that are explicit about
themselves.  Which sources those are is **configuration, not code**: this
package knows nothing about any particular orchestrator, CI provider or label
scheme, and hard-coding one vendor's environment variables into a general tool
would be exactly the coupling it exists to avoid.

The split that makes this work:

*schema* -- which environment variables carry agent metadata, and which pull
    request labels mean which authoring mode -- lives in the repository's own
    config file.  It is a property of that repository's conventions, so it
    belongs somewhere versioned, reviewable in a pull request, and identical
    across every run.
*values* -- the actual run id, model and agent name -- arrive as environment
    variables at run time, because only whatever launched the run knows them.

When nothing trustworthy is available the authoring mode is ``None``.  That is
a first-class, expected value -- "we do not know" -- and it must never be
silently coerced to ``human``.  Backfilling unknowns as human would
systematically bias every future comparison in exactly the direction the
research question is about.
"""

from __future__ import annotations

import os
from typing import Any

from .schema import AUTHORING_MODES

#: Environment variables this package defines itself, as opposed to the ones a
#: repository maps in its config.  ``MIRA_VITALS_*`` is the current spelling;
#: ``CODE_HEALTH_*`` is read as well, because the first repositories to adopt
#: this collector did so before it was extracted into a package and their
#: workflows already set it.  The newer spelling wins where both are present.
ENV_PREFIXES: tuple[str, ...] = ("MIRA_VITALS_", "CODE_HEALTH_")

#: Precedence, most trusted first.  An explicit workflow input beats a label
#: because it is set per-run by whoever launched the run; a label describes the
#: change and can be edited after the fact.
SOURCE_PRECEDENCE: tuple[str, ...] = ("workflow_input", "agent_source", "pr_label")


def env_value(env: dict[str, str], suffix: str) -> str | None:
    """Read ``<PREFIX><suffix>`` under each supported prefix, in order."""
    for prefix in ENV_PREFIXES:
        value = _clean(env.get(prefix + suffix))
        if value is not None:
            return value
    return None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def collect(
    env: dict[str, str] | None = None,
    *,
    agent_sources: list[dict[str, Any]] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the provenance block from explicit metadata only.

    ``agent_sources`` and ``labels`` come from the repository's configuration.
    Both default to empty: with no configuration this package assumes nothing
    about who or what produced the change, which is the correct default for a
    tool that must not guess.
    """
    env = dict(os.environ if env is None else env)
    agent_sources = agent_sources or []
    labels = labels or {}

    candidates: dict[str, str] = {}

    declared = env_value(env, "AUTHORING_MODE")
    if declared and declared in AUTHORING_MODES:
        # An unrecognised value is recorded as a rejection rather than being
        # coerced to something plausible: a typo'd mode must not silently
        # become a data point.
        candidates["workflow_input"] = declared

    agents = _collect_agents(env, agent_sources)

    # An orchestrator may assert the supervision level itself.  "An agent ran"
    # is knowable from its own metadata; whether a human supervised is not, so
    # it is only recorded when the source explicitly says so.
    for agent in agents:
        asserted = agent.pop("_asserted_mode", None)
        if asserted in AUTHORING_MODES and "agent_source" not in candidates:
            candidates["agent_source"] = asserted

    label_modes = sorted(
        {labels[label] for label in _split_list(env_value(env, "PR_LABELS")) if label in labels}
    )
    if len(label_modes) == 1:
        candidates["pr_label"] = label_modes[0]
    elif len(label_modes) > 1:
        # Two contradictory declarations.  "mixed" is a real category here, not
        # a fudge: the change genuinely carries more than one authoring claim.
        candidates["pr_label"] = "mixed"

    authoring_mode: str | None = None
    source: str | None = None
    for candidate_source in SOURCE_PRECEDENCE:
        if candidate_source in candidates:
            authoring_mode = candidates[candidate_source]
            source = candidate_source
            break

    return {
        "authoring_mode": authoring_mode,
        "authoring_mode_source": source,
        # Every declaration seen, not just the winner.  If a label and a
        # workflow input disagree, that disagreement is itself data.
        "declared_modes": candidates,
        "conflict": len(set(candidates.values())) > 1,
        "human_authors": _collect_human_authors(env),
        "agents": agents,
        # Flattened for convenience of downstream queries; derived from
        # `agents`, never populated independently.
        "agent_run_ids": [a["run_id"] for a in agents if a.get("run_id")],
        "models": sorted({a["model"] for a in agents if a.get("model")}),
        # Granularity actually achieved by this run.  The schema is designed
        # for commit/change/file/symbol provenance; v1 populates change level
        # only, and says so rather than implying more.
        "granularity": "change",
        "review": _collect_review(env),
    }


#: Fields an agent source may map, and the record key each populates.  Anything
#: else a source declares is carried through under ``extra``, so an
#: orchestrator with its own identifiers keeps them without this package
#: needing to know what they mean.
AGENT_FIELDS: tuple[str, ...] = ("run_id", "name", "provider", "model", "authoring_mode")


def _collect_agents(env: dict[str, str], agent_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agent records, from the environment variables the config names.

    A source contributes nothing unless its ``run_id`` variable is set: that is
    the signal that this orchestrator actually launched the run, rather than
    its variables happening to linger in the environment of a machine that also
    runs other things.
    """
    agents: list[dict[str, Any]] = []

    for source in agent_sources:
        mapping = source.get("env", {})
        run_id_var = mapping.get("run_id")
        if not run_id_var:
            continue
        run_id = _clean(env.get(run_id_var))
        if not run_id:
            continue
        record: dict[str, Any] = {
            "name": _clean(env.get(mapping.get("name", ""))),
            "provider": _clean(env.get(mapping.get("provider", ""))),
            "model": _clean(env.get(mapping.get("model", ""))),
            "run_id": run_id,
            "source": source.get("name") or "agent_source",
            "extra": {
                key: _clean(env.get(var))
                for key, var in mapping.items()
                if key not in AGENT_FIELDS and _clean(env.get(var))
            },
        }
        record["_asserted_mode"] = _clean(env.get(mapping.get("authoring_mode", "")))
        agents.append(record)

    # A workflow may declare an agent directly, without an orchestrator.
    declared_agent = env_value(env, "AGENT_NAME")
    if declared_agent:
        agents.append(
            {
                "name": declared_agent,
                "provider": env_value(env, "AGENT_PROVIDER"),
                "model": env_value(env, "AGENT_MODEL"),
                "run_id": env_value(env, "AGENT_RUN_ID"),
                "source": "workflow_input",
                "extra": {},
                "_asserted_mode": None,
            }
        )
    return agents


def _collect_human_authors(env: dict[str, str]) -> list[dict[str, Any]]:
    """Stable internal IDs only.

    Deliberately *not* email addresses or display names.  A longitudinal
    dataset that accumulates personal data for years is a liability, and the
    analyses this is built for -- does authoring mode correlate with
    complexity -- need a stable pseudonymous key, not an identity.
    """
    return [
        {"id": author_id, "source": "workflow_input"}
        for author_id in _split_list(env_value(env, "HUMAN_AUTHOR_IDS"))
    ]


def _collect_review(env: dict[str, str]) -> dict[str, Any]:
    """Whether the change passed human review, when CI can know it.

    Needed for "does code-review intervention improve agent-generated code
    quality?".  Populated only from explicit values; unknown stays ``None``.
    """
    approvals = env_value(env, "REVIEW_APPROVALS")
    reviewed: bool | None = None
    approval_count: int | None = None
    if approvals is not None:
        try:
            approval_count = int(approvals)
        except ValueError:
            approval_count = None
        else:
            reviewed = approval_count > 0
    return {
        "human_reviewed": reviewed,
        "approval_count": approval_count,
        "review_source": "workflow_input" if approval_count is not None else None,
    }
