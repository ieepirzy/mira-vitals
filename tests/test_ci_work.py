import json

from mira_vitals.ci import command, record


def test_explicit_work_outcomes_are_bounded_and_conflicts_unknown():
    warnings = []
    work = command._work(["unit-test=success", "security-scan=failure", "unit-test=skipped",
                          "unit-test=success", "private-name=success", "lint=bogus"], warnings)
    assert work == {"unit-test": "unknown", "security-scan": "failure"}
    assert warnings
    assert command._work([], []) == {}


def test_work_is_emitted_in_report_log():
    doc = record.build(identity={"job": "test"}, platform=None, platform_note="", marks=[], phases=[],
                       tests=[], test_files=[], caches={}, runner={}, outcome="success", warnings=[],
                       generated_by={}, now=1)
    doc["work"] = {"unit-test": "success", "security-scan": "skipped"}
    report = record.log_records(doc, max_tests=0)[0]
    assert report["attributes"]["ci.work"] == doc["work"]


def test_report_records_explicit_work_and_missing_is_unknown(tmp_path):
    output = tmp_path / "health.json"
    args = ["report", "--no-platform", "--no-export", "--output", str(output), "--quiet"]
    assert command.main(args + ["--work", "unit-test=success"], env={}) == 0
    assert json.loads(output.read_text())["work"] == {"unit-test": "success"}
    assert command.main(args, env={}) == 0
    assert json.loads(output.read_text())["work"] is None


def test_opportunity_cli_counts_unreadable_and_unmatched(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"identity": {"repository": "o/r", "run_id": "1", "run_attempt": 1},
                                "job": {"id": 2, "name": "test"}, "work": {"unit-test": "success"}}))
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert command.main(["opportunity", str(good), str(bad), str(tmp_path / "missing*.json")], env={}) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["input_files"], result["unreadable_files"], result["unmatched_patterns"]) == (2, 1, 1)
    assert result["groups"][0]["work_outcomes"] == [{"category": "unit-test", "outcome": "success", "jobs": 1}]
