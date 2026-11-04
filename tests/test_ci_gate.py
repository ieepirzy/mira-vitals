"""The reusable CI gate (.github/workflows/ci-gate.yml): its verdict script is
run here exactly as the workflow embeds it."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-gate.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _script() -> str:
    # The heredoc body, dedented by the block scalar's 10-space indent.
    m = re.search(r"<<'PY'\n(.*?)\n {10}PY\n", _text(), flags=re.S)
    assert m, "the verdict script is a quoted heredoc"
    return "\n".join(line[10:] if line.startswith(" " * 10) else line.strip()
                     for line in m.group(1).split("\n"))


def _verdict(needs, required, tmp_path: Path) -> tuple[int, str, str]:
    env = {
        "GATE_NEEDS": needs if isinstance(needs, str) else json.dumps(needs),
        "GATE_REQUIRED": required if isinstance(required, str) else json.dumps(required),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "PATH": "/usr/bin:/bin",
    }
    p = subprocess.run([sys.executable, "-I", "-"], input=_script(), env=env,
                       capture_output=True, text=True, timeout=30)
    summary = (tmp_path / "summary.md").read_text() if (tmp_path / "summary.md").exists() else ""
    return p.returncode, p.stdout + p.stderr, summary


def r(result: str) -> dict:
    return {"result": result, "outputs": {}}


ALL = {"changes": r("success"), "contracts": r("success"), "tofu": r("skipped"), "multi-arch": r("success")}


def test_skipped_job_that_was_not_needed_is_green(tmp_path):
    code, out, summary = _verdict(ALL, {"tofu": "false", "multi-arch": "true"}, tmp_path)
    assert code == 0, out
    assert "| tofu | not needed | skipped | yes |" in summary


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled"])
def test_required_job_must_succeed(tmp_path, result):
    needs = {**ALL, "tofu": r(result)}
    code, out, _ = _verdict(needs, {"tofu": "true"}, tmp_path)
    assert code == 1 and "tofu" in out


def test_job_absent_from_required_is_required(tmp_path):
    # tofu skipped, but `required` doesn't mention it: fail closed.
    code, out, _ = _verdict(ALL, {"multi-arch": "true"}, tmp_path)
    assert code == 1 and "tofu" in out


@pytest.mark.parametrize("value", ["", "no", "0", None, "maybe", 0])
def test_only_an_explicit_false_makes_a_job_optional(tmp_path, value):
    code, _, _ = _verdict(ALL, {"tofu": value}, tmp_path)
    assert code == 1


@pytest.mark.parametrize("value", [False, "false", " False "])
def test_explicit_false_forms(tmp_path, value):
    code, out, _ = _verdict(ALL, {"tofu": value}, tmp_path)
    assert code == 0, out


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_a_job_not_needed_that_failed_or_was_cancelled_is_red(tmp_path, result):
    needs = {**ALL, "tofu": r(result)}
    code, _, _ = _verdict(needs, {"tofu": "false"}, tmp_path)
    assert code == 1


def test_failed_changes_job_is_red(tmp_path):
    # The filter job itself is required (it isn't in `required`); if it fails,
    # everything that depends on it is skipped, and the gate must not pass.
    needs = {"changes": r("failure"), "tofu": r("skipped")}
    code, out, _ = _verdict(needs, {"tofu": "false"}, tmp_path)
    assert code == 1 and "changes" in out


@pytest.mark.parametrize("needs,required", [
    ("not json", "{}"),
    ({}, "{}"),
    (ALL, "not json"),
    (ALL, "[]"),
])
def test_malformed_input_is_red(tmp_path, needs, required):
    code, _, _ = _verdict(needs, required, tmp_path)
    assert code == 1


def test_required_naming_an_unknown_job_is_red(tmp_path):
    # Everything succeeded; the only fault is a `required` key the gate
    # doesn't wait on (a typo would otherwise silently gate nothing).
    green = {k: r("success") for k in ALL}
    code, out, _ = _verdict(green, {"contracs": "true"}, tmp_path)
    assert code == 1 and "contracs" in out


def test_job_names_cannot_inject_workflow_commands(tmp_path):
    needs = {"a\n::error::INJECTED": r("failure")}
    code, out, _ = _verdict(needs, {}, tmp_path)
    assert code == 1
    assert not any(line.startswith("::error::INJECTED") for line in out.splitlines())


def test_workflow_shape():
    text = _text()
    body = text.split("\non:\n", 1)[1]
    assert body.startswith("  workflow_call:\n"), "callable only, never triggered by paths"
    assert "\n  push:" not in body and "\n  pull_request:" not in body and "paths:" not in body
    assert "default: '\"ubuntu-latest\"'" in text
    assert "\npermissions: {}\n" in text
    assert "runs-on: ${{ fromJSON(inputs.runs-on) }}" in text
    assert "timeout-minutes: 5" in text
    # Inputs reach the script through env only, never interpolated into it.
    assert "${{" not in _script()
    assert "GATE_NEEDS: ${{ inputs.needs }}" in text and "GATE_REQUIRED: ${{ inputs.required }}" in text
