"""Type-checker adapter: pyright (default) or mypy.

Which checker runs is configuration, not inference, and the choice is recorded
in the snapshot -- because pyright and mypy do not measure the same thing.  As invoked
here, on this repository, pyright reports 49 errors over 7 files and mypy
reports 36 over 6.  Neither number is wrong; they are different instruments,
and the gap is not a constant offset -- their rule taxonomies barely overlap
(pyright: 25 reportArgumentType, 10 reportAttributeAccessIssue; mypy: 18
arg-type, 11 import-not-found).  Silently switching checker would therefore be a *redefinition* of
``typing.errors``, which is exactly the kind of quiet break this dataset is
supposed to be immune to.  Hence ``typing.tool`` and ``typing.tool_version``
travel with every observation.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

from .base import ToolRun, run, tool_version, which

_MYPY_LINE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+):(?:\d+:)?\s*(?P<sev>error|warning|note):\s*(?P<msg>.*?)(?:\s+\[(?P<rule>[\w-]+)\])?$")


def collect(
    paths: list[str],
    *,
    tool_name: str = "pyright",
    cwd: str | None = None,
) -> tuple[dict[str, Any], ToolRun]:
    if tool_name == "mypy":
        return _collect_mypy(paths, cwd=cwd)
    if tool_name == "pyright":
        return _collect_pyright(paths, cwd=cwd)
    return _empty(tool_name, "unavailable", f"unknown type checker {tool_name!r}"), ToolRun(
        name=tool_name, status="unavailable", error=f"unknown type checker {tool_name!r}"
    )


def _collect_pyright(paths: list[str], *, cwd: str | None) -> tuple[dict[str, Any], ToolRun]:
    executable = which("pyright") or "pyright"
    argv = [executable, "--outputjson", *paths]
    tool = ToolRun(name="pyright", command=argv)
    tool.version = tool_version([executable, "--version"])
    if tool.version is None:
        tool.status = "unavailable"
        tool.error = "pyright is not installed"
        return _empty("pyright", "unavailable", tool.error), tool

    result = run(argv, cwd=cwd)
    tool.exit_code = result.returncode
    tool.duration_seconds = result.duration
    # pyright exits 1 when it reports diagnostics: a successful run with
    # findings, not a failure.  Only an unparseable payload is a real error.
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        tool.status = "error"
        tool.error = f"could not parse pyright JSON: {exc}; stderr={result.stderr.strip()[:500]}"
        return _empty("pyright", "error", tool.error), tool

    summary = payload.get("summary", {})
    by_rule: Counter[str] = Counter()
    for diagnostic in payload.get("generalDiagnostics", []):
        by_rule[diagnostic.get("rule") or diagnostic.get("severity", "unknown")] += 1

    import_errors = by_rule.get("reportMissingImports", 0) + by_rule.get("reportMissingModuleSource", 0)
    errors = summary.get("errorCount")
    return (
        {
            "status": "ok",
            "tool": "pyright",
            "tool_version": tool.version,
            "errors": errors,
            "warnings": summary.get("warningCount"),
            "information": summary.get("informationCount"),
            "files_analyzed": summary.get("filesAnalyzed"),
            # Environment-driven rather than code-driven; see module docstring.
            "import_errors": import_errors,
            "errors_excluding_imports": None if errors is None else errors - import_errors,
            "environment": _environment(),
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: (-kv[1], kv[0]))),
        },
        tool,
    )


def _collect_mypy(paths: list[str], *, cwd: str | None) -> tuple[dict[str, Any], ToolRun]:
    executable = which("mypy") or "mypy"
    argv = [executable, "--no-color-output", "--no-error-summary", "--show-error-codes", *paths]
    tool = ToolRun(name="mypy", command=argv)
    tool.version = tool_version([executable, "--version"])
    if tool.version is None:
        tool.status = "unavailable"
        tool.error = "mypy is not installed"
        return _empty("mypy", "unavailable", tool.error), tool

    result = run(argv, cwd=cwd)
    tool.exit_code = result.returncode
    tool.duration_seconds = result.duration
    # mypy: 0 = clean, 1 = findings, 2 = a real failure (bad flags, crash).
    if result.returncode not in (0, 1):
        tool.status = "error"
        tool.error = (result.stderr or result.stdout).strip()[:2000]
        return _empty("mypy", "error", tool.error), tool

    errors = warnings = 0
    by_rule: Counter[str] = Counter()
    files: set[str] = set()
    for line in result.stdout.splitlines():
        match = _MYPY_LINE.match(line.strip())
        if not match:
            continue
        severity = match.group("sev")
        if severity == "note":
            continue
        files.add(match.group("path"))
        by_rule[match.group("rule") or severity] += 1
        if severity == "error":
            errors += 1
        else:
            warnings += 1

    import_errors = by_rule.get("import-not-found", 0) + by_rule.get("import-untyped", 0)
    return (
        {
            "status": "ok",
            "tool": "mypy",
            "tool_version": tool.version,
            "errors": errors,
            "warnings": warnings,
            "information": None,
            "files_analyzed": len(files) or None,
            "import_errors": import_errors,
            "errors_excluding_imports": errors - import_errors,
            "environment": _environment(),
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: (-kv[1], kv[0]))),
        },
        tool,
    )


def _empty(tool_name: str, status: str, error: str | None = None) -> dict[str, Any]:
    """Type errors are unknown, not zero."""
    return {
        "status": status,
        "tool": tool_name,
        "tool_version": None,
        "errors": None,
        "warnings": None,
        "information": None,
        "files_analyzed": None,
        "import_errors": None,
        "errors_excluding_imports": None,
        "environment": _environment(),
        "by_rule": {},
        "error": error,
    }


def _environment() -> dict[str, Any]:
    """The Python environment the *collector* is running under.

    Recorded so that a change in ``typing.errors`` caused by a dependency being
    absent is distinguishable from one caused by the code.

    This is the collector's own interpreter, which is the one pyright and mypy
    resolve when invoked from the same PATH -- the normal CI arrangement, and
    the one the workflow in this repository uses.  It is not a reading of the
    interpreter the checker actually chose: both tools can be pointed elsewhere
    by their own configuration (``pyright --pythonpath``, ``mypy
    --python-executable``, a ``venvPath`` in pyrightconfig.json).  Where that is
    done, this field describes the collector rather than the check, and the
    honest reading of it is "the environment the analysis ran in".
    """
    import sys

    return {
        "python_version": sys.version.split()[0],
        "executable": sys.executable,
        "virtual_env": os.environ.get("VIRTUAL_ENV"),
    }
