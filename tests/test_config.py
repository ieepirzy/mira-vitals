"""Repository configuration."""

from mira_vitals.config import Config, load


def test_defaults_do_not_narrow_the_gate():
    """An unconfigured repository gates everything it measures."""
    config = Config(paths=["pkg"])
    assert config.effective_lint_paths == ["pkg"]
    assert config.effective_lint_gate_paths == ["pkg"]
    assert config.effective_typecheck_paths == ["pkg"]


def test_widening_lint_paths_does_not_widen_the_gate():
    config = Config(paths=["pkg"], lint_paths=["pkg", "tests"])
    assert config.effective_lint_paths == ["pkg", "tests"]
    assert config.effective_lint_gate_paths == ["pkg"], "measuring tests must not start gating them"


def test_this_package_measures_itself():
    """Dogfooding, asserted so a silent edit is caught.

    A metric definition nobody applies to themselves is one nobody has checked.
    """
    config = load(".")
    assert config.source == "pyproject.toml"
    assert config.paths == ["src/mira_vitals"]
    assert config.lint_blocking is True, "this package has no excuse for a dirty baseline"
    assert config.type_checker == "pyright"


def test_the_provenance_schema_is_configuration_not_code():
    """The labels live in the config file, not in provenance.py."""
    config = load(".")
    assert config.provenance_labels["authoring:agent-supervised"] == "agent_supervised"


def test_an_unconfigured_repository_assumes_no_provenance_sources(tmp_path):
    config = load(str(tmp_path))
    assert config.provenance_labels == {}
    assert config.provenance_agent_sources == []


def test_a_standalone_config_can_declare_agent_sources(tmp_path):
    (tmp_path / "mira-vitals.toml").write_text(
        'paths = ["app"]\n'
        "[provenance.labels]\n"
        '"authoring:human" = "human"\n'
        "[[provenance.agent_sources]]\n"
        'name = "mirarun"\n'
        "env = { run_id = \"MIRARUN_RUN_ID\", model = \"MIRARUN_MODEL\" }\n"
    )
    config = load(str(tmp_path))
    assert config.provenance_labels == {"authoring:human": "human"}
    assert config.provenance_agent_sources[0]["name"] == "mirarun"
    assert config.provenance_agent_sources[0]["env"]["run_id"] == "MIRARUN_RUN_ID"


def test_the_pre_extraction_config_filenames_still_work(tmp_path):
    (tmp_path / "code-health.toml").write_text('paths = ["legacy"]\n')
    assert load(str(tmp_path)).paths == ["legacy"]


def test_a_repository_without_configuration_still_works(tmp_path):
    config = load(str(tmp_path))
    assert config.paths == ["."]
    assert config.language == "python"


def test_a_standalone_code_health_toml_is_read(tmp_path):
    """For repositories with no pyproject.toml."""
    (tmp_path / "code-health.toml").write_text(
        'paths = ["app"]\nlint_paths = ["app", "tests"]\ntype_checker = "mypy"\n'
    )
    config = load(str(tmp_path))
    assert config.paths == ["app"]
    assert config.type_checker == "mypy"
    assert config.source == "code-health.toml"


def test_a_standalone_file_wins_over_pyproject(tmp_path):
    (tmp_path / "code-health.toml").write_text('paths = ["chosen"]\n')
    (tmp_path / "pyproject.toml").write_text('[tool.code_health]\npaths = ["ignored"]\n')
    assert load(str(tmp_path)).paths == ["chosen"]


def test_pyproject_is_used_when_there_is_no_standalone_file(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.code_health]\npaths = ["pkg"]\n')
    config = load(str(tmp_path))
    assert config.paths == ["pkg"]
    assert config.source == "pyproject.toml"


def test_a_pyproject_without_our_table_falls_through_to_defaults(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    config = load(str(tmp_path))
    assert config.paths == ["."]
    assert config.source is None


def test_unknown_settings_are_ignored_not_fatal(tmp_path):
    (tmp_path / "code-health.toml").write_text('paths = ["a"]\nfuture_setting = 3\n')
    assert load(str(tmp_path)).paths == ["a"]


def test_lint_blocking_defaults_on():
    assert Config().lint_blocking is True


def test_lint_can_be_measured_without_blocking(tmp_path):
    """How a repository with pre-existing findings adopts the lane."""
    (tmp_path / "code-health.toml").write_text('paths = ["app"]\nlint_blocking = false\n')
    config = load(str(tmp_path))
    assert config.lint_blocking is False
    assert config.effective_lint_gate_paths == ["app"], "still measured against the gate paths"
