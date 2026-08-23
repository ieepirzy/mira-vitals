"""Lint gating is narrower than lint measurement, and exactly so."""

from mira_vitals.analyzers import base, ruff_lint


def _fake_findings(monkeypatch, findings_json):
    monkeypatch.setattr(ruff_lint, "tool_version", lambda command: "ruff 0.15.8")
    monkeypatch.setattr(
        ruff_lint,
        "run",
        lambda command, cwd=None: base.Completed(returncode=0, stdout=findings_json, stderr="", duration=0.1),
    )


def test_a_finding_inside_the_gated_path_blocks(monkeypatch, tmp_path):
    _fake_findings(monkeypatch, f'[{{"code":"F401","filename":"{tmp_path}/src/app/handlers.py"}}]')
    data, _ = ruff_lint.collect(["src"], cwd=str(tmp_path), gate_paths=["src"])
    assert data["errors"] == 1
    assert data["gate_errors"] == 1


def test_a_finding_outside_the_gated_path_is_measured_but_does_not_block(monkeypatch, tmp_path):
    """The common adoption case: pre-existing findings in tests/ that must not gate."""
    _fake_findings(monkeypatch, f'[{{"code":"F401","filename":"{tmp_path}/tests/test_endpoints.py"}}]')
    data, _ = ruff_lint.collect(["src", "tests"], cwd=str(tmp_path), gate_paths=["src"])
    assert data["errors"] == 1, "still measured"
    assert data["gate_errors"] == 0, "but not gated"


def test_gating_is_not_a_string_prefix_match(monkeypatch, tmp_path):
    """`src_extra/` must not be gated by a gate on `src`."""
    _fake_findings(monkeypatch, f'[{{"code":"F401","filename":"{tmp_path}/src_extra/thing.py"}}]')
    data, _ = ruff_lint.collect(["."], cwd=str(tmp_path), gate_paths=["src"])
    assert data["gate_errors"] == 0


def test_nested_gate_paths_are_matched(monkeypatch, tmp_path):
    _fake_findings(monkeypatch, f'[{{"code":"F401","filename":"{tmp_path}/src/app/main.py"}}]')
    data, _ = ruff_lint.collect(["src"], cwd=str(tmp_path), gate_paths=["src/app"])
    assert data["gate_errors"] == 1


def test_without_gate_paths_everything_blocking_gates(monkeypatch, tmp_path):
    """Default is the safe one: no configured narrowing means no narrowing."""
    _fake_findings(monkeypatch, f'[{{"code":"F401","filename":"{tmp_path}/anywhere.py"}}]')
    data, _ = ruff_lint.collect(["."], cwd=str(tmp_path), gate_paths=None)
    assert data["gate_errors"] == 1


def test_non_blocking_rules_never_gate(monkeypatch, tmp_path):
    _fake_findings(monkeypatch, f'[{{"code":"D100","filename":"{tmp_path}/src/a.py"}}]')
    data, _ = ruff_lint.collect(["src"], cwd=str(tmp_path), gate_paths=["src"])
    assert data["warnings"] == 1
    assert data["gate_errors"] == 0


def test_lint_blocking_false_measures_without_failing(snapshot_factory):
    from mira_vitals.cli import _gate
    from mira_vitals.config import Config

    document = snapshot_factory()
    document["lint"] = {"status": "ok", "gate_errors": 19, "gate_paths": ["app"],
                        "blocking_rule_prefixes": ["F"], "errors": 19}
    document["tests"] = {"status": "ok", "failed": 0, "errors": 0}
    violations = _gate(document, Config(lint_blocking=False), lambda *a: None)
    assert violations == []


def test_lint_blocking_true_does_fail(snapshot_factory):
    from mira_vitals.cli import _gate
    from mira_vitals.config import Config

    document = snapshot_factory()
    document["lint"] = {"status": "ok", "gate_errors": 19, "gate_paths": ["app"],
                        "blocking_rule_prefixes": ["F"], "errors": 19}
    document["tests"] = {"status": "ok", "failed": 0, "errors": 0}
    violations = _gate(document, Config(lint_blocking=True), lambda *a: None)
    assert len(violations) == 1


def test_the_rule_selection_is_passed_explicitly(monkeypatch, tmp_path):
    """Never left to ruff's defaults.

    The same commit measured 12 findings under ruff 0.15.8 and 153 under
    0.16.4, because 0.16 widened its default selection -- an unpinned float
    silently redefining the metric by 12x.
    """
    captured = {}

    monkeypatch.setattr(ruff_lint, "tool_version", lambda command: "ruff 0.16.4")

    def fake_run(command, cwd=None):
        captured["argv"] = command
        return base.Completed(returncode=0, stdout="[]", stderr="", duration=0.1)

    monkeypatch.setattr(ruff_lint, "run", fake_run)
    ruff_lint.collect(["app"], cwd=str(tmp_path), select=["E4", "F"])
    assert "--select" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--select") + 1] == "E4,F"


def test_the_default_selection_is_used_when_unconfigured(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(ruff_lint, "tool_version", lambda command: "ruff 0.16.4")
    monkeypatch.setattr(
        ruff_lint, "run",
        lambda command, cwd=None: (
            captured.__setitem__("argv", command),
            base.Completed(returncode=0, stdout="[]", stderr="", duration=0.1),
        )[1],
    )
    data, _ = ruff_lint.collect(["app"], cwd=str(tmp_path))
    assert captured["argv"][captured["argv"].index("--select") + 1] == "E4,E7,E9,F,W"
    assert data["select"] == list(ruff_lint.DEFAULT_SELECT)


def test_the_selection_travels_with_the_measurement(monkeypatch, tmp_path):
    """Without it, `lint.total` is uninterpretable across time."""
    _fake_findings(monkeypatch, "[]")
    data, _ = ruff_lint.collect(["app"], cwd=str(tmp_path), select=["F"], ignore=["F401"])
    assert data["select"] == ["F"]
    assert data["ignore"] == ["F401"]
