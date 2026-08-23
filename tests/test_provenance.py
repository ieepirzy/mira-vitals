"""Provenance: what it records, and what it refuses to guess."""

from mira_vitals import provenance


def test_nothing_declared_yields_unknown_not_human():
    """The single most important behaviour in this module.

    Defaulting an unknown to `human` would bias every future human-vs-agent
    comparison in exactly the direction being measured.
    """
    result = provenance.collect({})
    assert result["authoring_mode"] is None
    assert result["authoring_mode_source"] is None


def test_commit_message_is_never_consulted():
    """Explicit metadata only -- no heuristics, however tempting."""
    env = {
        "GIT_COMMIT_MESSAGE": "Generated with Claude Code\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        "GITHUB_ACTOR": "claude-bot",
    }
    assert provenance.collect(env)["authoring_mode"] is None


def test_the_pre_extraction_env_prefix_is_still_honoured():
    """Repositories adopted this before it was a package and set CODE_HEALTH_*.

    Breaking them on rename would be a poor advertisement for a tool whose
    whole pitch is not silently redefining things.
    """
    assert provenance.collect({"CODE_HEALTH_AUTHORING_MODE": "human"})["authoring_mode"] == "human"


def test_the_current_prefix_wins_where_both_are_set():
    result = provenance.collect(
        {"MIRA_VITALS_AUTHORING_MODE": "agent_supervised", "CODE_HEALTH_AUTHORING_MODE": "human"}
    )
    assert result["authoring_mode"] == "agent_supervised"


def test_workflow_input_is_trusted():
    result = provenance.collect({"MIRA_VITALS_AUTHORING_MODE": "agent_supervised"})
    assert result["authoring_mode"] == "agent_supervised"
    assert result["authoring_mode_source"] == "workflow_input"


def test_an_invalid_mode_is_rejected_not_coerced():
    """A typo must not silently become a data point."""
    result = provenance.collect({"MIRA_VITALS_AUTHORING_MODE": "agent-supervised"})
    assert result["authoring_mode"] is None


LABELS = {
    "authoring:human": "human",
    "authoring:agent-supervised": "agent_supervised",
    "authoring:agent-autonomous": "agent_autonomous",
}

#: A configured orchestrator. Nothing about this shape is known to the package;
#: it is entirely what a repository declares in its config file.
MIRARUN = [
    {
        "name": "mirarun",
        "env": {
            "run_id": "MIRARUN_RUN_ID",
            "name": "MIRARUN_AGENT_NAME",
            "provider": "MIRARUN_AGENT_PROVIDER",
            "model": "MIRARUN_MODEL",
            "authoring_mode": "MIRARUN_AUTHORING_MODE",
            "environment_id": "MIRARUN_ENVIRONMENT_ID",
        },
    }
]


def test_no_configuration_means_no_assumptions():
    """The package ships knowing nothing about anyone's orchestrator.

    Agent variables present in the environment are ignored unless a repository
    declared them, because a general tool guessing at one vendor's variable
    names is how a measurement quietly starts depending on which machine it ran
    on.
    """
    env = {"MIRARUN_RUN_ID": "r1", "MIRARUN_MODEL": "m"}
    result = provenance.collect(env)
    assert result["agents"] == []
    assert result["authoring_mode"] is None


def test_labels_are_only_read_when_configured():
    env = {"MIRA_VITALS_PR_LABELS": "authoring:agent-autonomous"}
    assert provenance.collect(env)["authoring_mode"] is None
    assert provenance.collect(env, labels=LABELS)["authoring_mode"] == "agent_autonomous"


def test_pr_label_populates_the_mode():
    result = provenance.collect(
        {"MIRA_VITALS_PR_LABELS": "size/M,authoring:agent-autonomous"}, labels=LABELS
    )
    assert result["authoring_mode"] == "agent_autonomous"
    assert result["authoring_mode_source"] == "pr_label"


def test_contradictory_labels_become_mixed_not_a_coin_flip():
    result = provenance.collect(
        {"MIRA_VITALS_PR_LABELS": "authoring:human,authoring:agent-autonomous"}, labels=LABELS
    )
    assert result["authoring_mode"] == "mixed"


def test_workflow_input_outranks_a_label_and_the_conflict_is_recorded():
    """Disagreement is itself data; it must not be silently dropped."""
    result = provenance.collect(
        {"MIRA_VITALS_AUTHORING_MODE": "agent_supervised", "MIRA_VITALS_PR_LABELS": "authoring:human"},
        labels=LABELS,
    )
    assert result["authoring_mode"] == "agent_supervised"
    assert result["authoring_mode_source"] == "workflow_input"
    assert result["conflict"] is True
    assert result["declared_modes"] == {"workflow_input": "agent_supervised", "pr_label": "human"}


def test_a_configured_orchestrator_populates_agents():
    result = provenance.collect(
        {
            "MIRARUN_RUN_ID": "run_123",
            "MIRARUN_AGENT_NAME": "claude-code",
            "MIRARUN_AGENT_PROVIDER": "anthropic",
            "MIRARUN_MODEL": "claude-opus-5",
            "MIRARUN_ENVIRONMENT_ID": "env_9",
        },
        agent_sources=MIRARUN,
    )
    assert result["agents"][0]["run_id"] == "run_123"
    assert result["agents"][0]["source"] == "mirarun"
    assert result["agent_run_ids"] == ["run_123"]
    assert result["models"] == ["claude-opus-5"]


def test_unknown_fields_are_carried_through_rather_than_dropped():
    """An orchestrator's own identifiers survive without this package knowing
    what they mean -- that is what keeps the join back to its records possible."""
    result = provenance.collect(
        {"MIRARUN_RUN_ID": "run_123", "MIRARUN_ENVIRONMENT_ID": "env_9"}, agent_sources=MIRARUN
    )
    assert result["agents"][0]["extra"] == {"environment_id": "env_9"}


def test_a_source_contributes_nothing_without_its_run_id():
    """Stray variables on a shared machine must not manufacture an agent.

    These runners also host production; a lingering variable is not evidence
    that an orchestrator launched this run.
    """
    result = provenance.collect({"MIRARUN_MODEL": "claude-opus-5"}, agent_sources=MIRARUN)
    assert result["agents"] == []


def test_an_agent_running_does_not_by_itself_imply_supervision_level():
    """The orchestrator knows an agent ran; not whether a human reviewed."""
    result = provenance.collect(
        {"MIRARUN_RUN_ID": "run_123", "MIRARUN_MODEL": "m"}, agent_sources=MIRARUN
    )
    assert result["agents"], "the agent must still be recorded"
    assert result["authoring_mode"] is None


def test_an_orchestrator_may_assert_the_mode_explicitly():
    result = provenance.collect(
        {"MIRARUN_RUN_ID": "run_1", "MIRARUN_AUTHORING_MODE": "agent_autonomous"},
        agent_sources=MIRARUN,
    )
    assert result["authoring_mode"] == "agent_autonomous"
    assert result["authoring_mode_source"] == "agent_source"


def test_human_authors_are_stable_ids_not_personal_data():
    result = provenance.collect({"MIRA_VITALS_HUMAN_AUTHOR_IDS": "u_17, u_22"})
    assert [a["id"] for a in result["human_authors"]] == ["u_17", "u_22"]


def test_review_state_is_recorded_when_ci_knows_it():
    assert provenance.collect({"MIRA_VITALS_REVIEW_APPROVALS": "2"})["review"] == {
        "human_reviewed": True,
        "approval_count": 2,
        "review_source": "workflow_input",
    }
    assert provenance.collect({"MIRA_VITALS_REVIEW_APPROVALS": "0"})["review"]["human_reviewed"] is False
    assert provenance.collect({})["review"]["human_reviewed"] is None


def test_granularity_is_declared_honestly():
    """v1 records change-level provenance and says so, rather than implying more."""
    assert provenance.collect({})["granularity"] == "change"
