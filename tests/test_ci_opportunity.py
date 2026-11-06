from copy import deepcopy

from mira_vitals.ci.opportunity import aggregate


def report(*, job_id=1, attempt=1, timestamp=1, selection=None, event="pull_request"):
    return {
        "identity": {"repository": "org/repo", "run_id": 23, "run_attempt": attempt, "event": event},
        "job": {"id": job_id, "name": "test"},
        "reported_at": timestamp,
        "optimizations": {"selection": selection},
    }


def selection(**updates):
    return {"policy": "advisory", "selector_version": "0.2", "decision": "full",
            "change_categories": ["ci", "python"], "triggers": ["ci", "non-py"],
            "triggers_complete": True, **updates}


def test_overlaps_and_sole_triggers_count_per_job_not_per_snapshot():
    first = report(selection=selection())
    second = report(job_id=2, selection=selection(triggers=["ci"], change_categories=["python"]))
    output = aggregate([first, deepcopy(first), second])
    assert output["snapshots"] == 3 and output["jobs"] == 2
    group = output["groups"][0]
    assert group["categories"] == {"ci": 1, "python": 2}
    assert group["runs"] == 1 and group["category_runs"] == {"ci": 1, "python": 1}
    assert group["triggers"] == {"ci": 2, "non-py": 1}
    assert group["sole_triggers"] == {"ci": 1}
    assert group["trigger_sets"] == [{"triggers": ["ci"], "jobs": 1},
                                     {"triggers": ["ci", "non-py"], "jobs": 1}]
    assert group["full"] == 2


def test_latest_snapshot_wins_and_distinct_attempts_and_matrix_jobs_remain():
    first = report(selection=selection())
    newer = report(timestamp=2, selection=selection(decision="selected", triggers=[]))
    result = aggregate([newer, first, report(job_id=2), report(attempt=2)])
    assert result["jobs"] == 3
    assert sum(g["selected"] for g in result["groups"]) == 1
    assert sum(g["selection_missing"] for g in result["groups"]) == 2


def test_conflicting_equal_timestamps_and_unidentified_are_not_counted():
    first = report(selection=selection())
    conflict = report(selection=selection(decision="selected"))
    missing_id = report(job_id=None)
    result = aggregate([first, conflict, missing_id])
    assert result["jobs"] == 0 and result["ambiguous"] == 1 and result["unidentified"] == 1


def test_newer_snapshot_resolves_older_tie():
    result = aggregate([report(selection=selection()), report(selection=selection(decision="selected")),
                        report(timestamp=2, selection=selection())])
    assert result["ambiguous"] == 0 and result["jobs"] == 1


def test_unknown_never_becomes_zero_observation_or_saved_time():
    docs = [report(), report(job_id=2, selection=selection(escapes=0, estimated_saved_seconds=42)),
            report(job_id=3, selection=selection(escape_observation="incomplete", escapes=2))]
    groups = aggregate(docs)["groups"]
    assert sum(g["escape_unknown"] for g in groups) == 2
    assert sum(g["escape_incomplete"] for g in groups) == 1
    assert all(g["escapes"] is None and g["saved_test_seconds"] is None for g in groups)
    assert sum(g["selection_missing"] for g in groups) == 1


def test_complete_escapes_and_labeled_work_savings_only():
    sel = selection(decision="selected", triggers=[], escape_observation="complete", escapes=0,
                    skipped_test_seconds_observed=33.5,
                    selector_duration_seconds=1.5)
    group = aggregate([report(selection=sel)])["groups"][0]
    assert group["escape_observations"] == 1 and group["escapes"] == 0
    assert group["saved_test_seconds"] == 33.5 and group["selector_duration_seconds"] == 1.5
    assert group["categories_selected"] == {"ci": 1, "python": 1}


def test_config_versions_and_events_are_not_mixed():
    docs = [report(selection=selection(selection_config_hash="a")),
            report(job_id=2, selection=selection(selection_config_hash="b")),
            report(job_id=3, event="push", selection=selection(selection_config_hash="a"))]
    assert len(aggregate(docs)["groups"]) == 3


def test_partial_positive_escapes_are_lower_bound_not_complete():
    docs = [report(selection=selection(escape_observation="incomplete", escapes=None, escapes_observed=2))]
    result = aggregate(docs)["groups"][0]
    assert result["escapes"] is None
    assert result["escapes_observed_lower_bound"] == 2
    assert result["escape_incomplete"] == 1


def test_nonfinite_values_and_invalid_lists_are_unknown():
    sel = selection(change_categories=["ci", None], triggers="ci", selector_duration_seconds=float("inf"),
                    escape_observation="complete", escapes=float("nan"))
    group = aggregate([report(selection=sel)])["groups"][0]
    assert group["categories_unknown"] == group["triggers_unknown"] == group["escape_unknown"] == 1
    assert group["selector_duration_seconds"] is None
