"""Schema stability.

A longitudinal dataset survives only if its shape changes deliberately.  These
tests fail on any silent addition, removal or reordering of a top-level
section, on a schema-version bump that nobody noticed, and on a change to a
metric definition that would redefine an existing series.
"""

import pytest

from mira_vitals import schema
from mira_vitals.schema import SchemaError


def test_schema_version_is_pinned():
    """Bumping this must be a deliberate edit, reviewed with a migration note."""
    assert schema.SCHEMA_VERSION == 1


def test_top_level_keys_are_pinned():
    assert schema.TOP_LEVEL_KEYS == (
        "schema_version", "generated_by", "definitions", "run", "ci", "target",
        "provenance", "summary", "complexity", "maintainability", "halstead",
        "lint", "typing", "tests", "security", "deltas", "hotspots", "files",
        "symbols", "tools",
    )


def test_metric_definitions_are_pinned():
    """These parameters *are* the meaning of the derived metrics.

    Changing one without bumping the schema version silently redefines a
    series that may already have years of history.
    """
    assert schema.DEFINITIONS["percentile_method"] == "nearest_rank"
    assert schema.DEFINITIONS["high_complexity_threshold"] == 10
    assert schema.DEFINITIONS["complexity_buckets"] == [10, 15, 20]
    assert schema.DEFINITIONS["density_min_source_loc"] == 200
    assert schema.DEFINITIONS["growth_min_abs_delta_loc"] == 25
    assert schema.DEFINITIONS["maintainability_aggregation"] == "unweighted_mean_over_files"
    assert "class blocks excluded" in schema.DEFINITIONS["function_set"]


def test_observation_id_is_stable_across_reruns(snapshot_factory):
    """Re-analyzing the same commit must dedupe, and reruns must not diverge."""
    first = schema.observation_id(repository="example-pkg", commit_sha="abc123", target_paths=["src"])
    second = schema.observation_id(repository="example-pkg", commit_sha="abc123", target_paths=["src"])
    assert first == second


def test_observation_id_ignores_path_ordering():
    a = schema.observation_id(repository="r", commit_sha="s", target_paths=["a", "b"])
    b = schema.observation_id(repository="r", commit_sha="s", target_paths=["b", "a"])
    assert a == b


def test_observation_id_changes_with_commit_and_target():
    base = schema.observation_id(repository="r", commit_sha="s", target_paths=["a"])
    assert base != schema.observation_id(repository="r", commit_sha="t", target_paths=["a"])
    assert base != schema.observation_id(repository="r", commit_sha="s", target_paths=["b"])
    assert base != schema.observation_id(repository="q", commit_sha="s", target_paths=["a"])


def test_validate_accepts_a_well_formed_snapshot(snapshot_factory):
    schema.validate(snapshot_factory())


def test_validate_rejects_a_missing_section(snapshot_factory):
    document = snapshot_factory()
    del document["halstead"]
    with pytest.raises(SchemaError, match="top-level keys"):
        schema.validate(document)


def test_validate_rejects_reordered_sections(snapshot_factory):
    document = snapshot_factory()
    reordered = {"run": document["run"], **{k: v for k, v in document.items() if k != "run"}}
    with pytest.raises(SchemaError, match="top-level keys"):
        schema.validate(reordered)


def test_validate_rejects_a_non_utc_timestamp(snapshot_factory):
    document = snapshot_factory()
    document["run"]["timestamp"] = "2026-08-23T10:00:00+03:00"
    with pytest.raises(SchemaError, match="UTC"):
        schema.validate(document)


def test_validate_rejects_an_unknown_authoring_mode(snapshot_factory):
    document = snapshot_factory()
    document["provenance"]["authoring_mode"] = "vibes"
    with pytest.raises(SchemaError, match="authoring_mode"):
        schema.validate(document)


def test_validate_rejects_an_unknown_status(snapshot_factory):
    document = snapshot_factory()
    document["lint"]["status"] = "probably_fine"
    with pytest.raises(SchemaError, match="lint.status"):
        schema.validate(document)


def test_validate_rejects_a_string_where_a_number_belongs(snapshot_factory):
    document = snapshot_factory()
    document["complexity"]["max"] = "28"
    with pytest.raises(SchemaError, match="complexity.max"):
        schema.validate(document)


def test_null_is_allowed_everywhere_a_number_is(snapshot_factory):
    """Missing must always be representable; that is the point."""
    document = snapshot_factory()
    for key in ("loc", "source_loc", "files", "functions"):
        document["summary"][key] = None
    for key in ("aggregate", "mean", "p50", "p90", "p95", "max", "density_per_kloc"):
        document["complexity"][key] = None
    schema.validate(document)


def test_observation_id_distinguishes_different_analyzed_trees():
    """The PR merge-commit collision.

    CI analyzes GitHub's synthetic merge commit while
    the snapshot records the PR head. Keyed on the head alone, a base branch
    advancing under an unchanged head produces genuinely different code with
    the *same* observation id, and a receiver deduplicating on it discards the
    newer measurement as a replay.
    """
    same_head = dict(repository="example-pkg", commit_sha="head123", target_paths=["src"])
    first = schema.observation_id(**same_head, analyzed_tree_sha="merge_aaa")
    second = schema.observation_id(**same_head, analyzed_tree_sha="merge_bbb")
    assert first != second


def test_observation_id_still_dedupes_identical_analyses():
    """The property the key exists for, unchanged."""
    args = dict(
        repository="example-pkg",
        commit_sha="head123",
        target_paths=["src"],
        analyzed_tree_sha="merge_aaa",
    )
    assert schema.observation_id(**args) == schema.observation_id(**args)


def test_repository_identity_keeps_its_owner():
    """Two organisations can both have a repository called `backend`.

    Reduced to a basename they share a `service.name`, merging their metric
    series -- and a fork at the same commit and tree produces the same
    `observation_id`, so a receiver deduplicating on it discards one
    repository's measurement as a replay of the other's.
    """
    from mira_vitals import context

    run_a, _ = context.collect({"GITHUB_REPOSITORY": "org-a/backend"}, repo_root=".")
    run_b, _ = context.collect({"GITHUB_REPOSITORY": "org-b/backend"}, repo_root=".")
    assert run_a["repository"] == "org-a/backend"
    assert run_b["repository"] == "org-b/backend"

    same = dict(commit_sha="c" * 40, target_paths=["src"], analyzed_tree_sha="t" * 40)
    assert schema.observation_id(repository=run_a["repository"], **same) != schema.observation_id(
        repository=run_b["repository"], **same
    )
