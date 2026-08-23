"""Radon adapters: cyclomatic complexity, raw LOC, maintainability, Halstead.

The complexity walk here carries the one non-obvious correctness rule in the
whole collector, so it is stated once, precisely.

``radon cc -j`` returns, per file, a *flat* list of blocks containing class,
function and method blocks.  Each method appears twice -- once at top level and
once nested under its class's ``methods`` key -- and each class block carries a
complexity *derived from* its methods rather than measured independently.
Summing the flat list therefore counts every method roughly twice.  Closures
are the mirror-image trap: by default they appear only nested under
``closures``, so a top-level-only walk silently drops them.

``--show-closures`` is radon's own answer to the second half: it promotes each
closure to top level under a qualified name (``https_open.build_conn``) while
*also* leaving it nested.  So with the flag on, recursing into ``closures``
would reintroduce a double count from the other direction.  The flag is used
and the recursion is not, on the same principle that governs the rest of this
codebase: prefer the library's own API over hand-rolling equivalent behaviour.

Verified directly against a real package (24 files, radon 6.0.1):

* without the flag the flat list holds 100 blocks = 10 classes + 30 functions
  + 60 methods, and radon's own ``--total-average`` footer agrees ("100 blocks
  (classes, functions, methods)");
* the 60 top-level method entries are identical to the 60 nested under classes;
* the flag adds exactly the 2 closures that existed, as top-level qualified
  functions, and leaves them nested as well;
* naive sum over the flat list = 484 over 102 blocks; the correct
  function-level aggregate = 440 over 92 symbols.

So: take top-level ``function`` and ``method`` blocks with ``--show-closures``
on, and never count ``class``.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from .base import Completed, ToolRun, run, tool_version, which

#: Block types that count as "a function" for the aggregate and for the
#: denominator of every per-function ratio.
FUNCTION_BLOCK_TYPES = frozenset({"function", "method"})


def _radon_argv(executable: str | None) -> list[str]:
    """Prefer an explicit radon on PATH, else the module (works in a venv)."""
    if executable:
        return [executable]
    return ["python3", "-m", "radon"]


def _walk_functions(blocks: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield exactly the function-like blocks, once each.

    Top level only.  See the module docstring for why classes are skipped and
    why the nested ``closures`` lists are deliberately *not* recursed into.
    """
    for block in blocks:
        if block.get("type") not in FUNCTION_BLOCK_TYPES:
            # `class`: its complexity is derived from methods that are already
            # present at top level, so counting it double-counts them.
            continue
        classname = block.get("classname")
        name = block.get("name")
        # radon already qualifies promoted closures ("https_open.build_conn");
        # methods carry their class separately.
        qualified = ".".join(p for p in (classname, name) if p)
        yield {**block, "qualified_name": qualified}


def collect_complexity(paths: list[str], *, cwd: str | None = None) -> tuple[list[dict[str, Any]], ToolRun]:
    """Per-symbol cyclomatic complexity.

    Returns normalized symbol records and the :class:`ToolRun`.  On failure the
    record list is empty *and* the status is ``error``/``unavailable`` -- the
    caller must not read the empty list as "no complexity".
    """
    executable = which("radon")
    argv = _radon_argv(executable) + ["cc", "-j", "--show-closures", *paths]
    tool = ToolRun(name="radon", command=argv)
    tool.version = tool_version(_radon_argv(executable) + ["--version"])
    if tool.version is None:
        tool.status = "unavailable"
        tool.error = "radon is not installed or did not report a version"
        return [], tool

    result: Completed = run(argv, cwd=cwd)
    tool.exit_code = result.returncode
    tool.duration_seconds = result.duration
    if result.returncode != 0:
        tool.status = "error"
        tool.error = (result.stderr or result.stdout).strip()[:2000]
        return [], tool

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        tool.status = "error"
        tool.error = f"could not parse radon cc JSON: {exc}"
        return [], tool

    symbols: list[dict[str, Any]] = []
    failures: list[str] = []
    for path, blocks in payload.items():
        # radon reports a per-file error as an object rather than a list.
        if isinstance(blocks, dict) and "error" in blocks:
            failures.append(f"{path}: {blocks['error']}")
            continue
        if not isinstance(blocks, list):
            continue
        for block in _walk_functions(blocks):
            symbols.append(
                {
                    "path": path,
                    "symbol": block.get("qualified_name") or block.get("name"),
                    "symbol_type": block.get("type"),
                    "line": block.get("lineno"),
                    "endline": block.get("endline"),
                    # radon does not report a block's line count directly;
                    # endline-lineno+1 is the span it measured.  It is a span,
                    # not radon's `sloc`, and is labelled accordingly.
                    "loc": (
                        block["endline"] - block["lineno"] + 1
                        if isinstance(block.get("endline"), int) and isinstance(block.get("lineno"), int)
                        else None
                    ),
                    "cyclomatic_complexity": block.get("complexity"),
                    # radon computes MI per file, never per symbol, and offers
                    # no nesting-depth metric at all.  Null rather than a
                    # fabricated number.
                    "maintainability_index": None,
                    "nesting_depth": None,
                }
            )

    if failures:
        # Some files parsed, some did not.  That is a partial measurement and
        # must not masquerade as a clean one.
        tool.status = "error"
        tool.error = "; ".join(failures)[:2000]
    return symbols, tool


def _partition(payload: dict[str, Any], tool: ToolRun) -> tuple[dict[str, dict[str, Any]], ToolRun]:
    """Keep the files radon measured; report the ones it could not.

    radon returns a successful JSON response that carries a per-file
    ``{"error": ...}`` entry for any file it failed to parse. Silently dropping
    those leaves the tool in ``ok`` state while LOC, file count and density are
    computed over a subset -- partial numbers presented as complete, which is
    the one thing this collector is built not to do. ``collect_complexity``
    already reported them; these three now do the same.
    """
    kept: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for path, value in payload.items():
        if isinstance(value, dict) and "error" in value:
            failures.append(f"{path}: {value['error']}")
        elif isinstance(value, dict):
            kept[path] = value
    if failures:
        tool.status = "error"
        tool.error = "; ".join(failures)[:2000]
    return kept, tool


def collect_raw(paths: list[str], *, cwd: str | None = None) -> tuple[dict[str, dict[str, Any]], ToolRun]:
    """Per-file raw line metrics (loc / sloc / comments / blank)."""
    executable = which("radon")
    argv = _radon_argv(executable) + ["raw", "-j", *paths]
    tool = ToolRun(name="radon-raw", command=argv)
    tool.version = tool_version(_radon_argv(executable) + ["--version"])
    if tool.version is None:
        tool.status = "unavailable"
        tool.error = "radon is not installed"
        return {}, tool

    result = run(argv, cwd=cwd)
    tool.exit_code = result.returncode
    tool.duration_seconds = result.duration
    if result.returncode != 0:
        tool.status = "error"
        tool.error = (result.stderr or result.stdout).strip()[:2000]
        return {}, tool
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        tool.status = "error"
        tool.error = f"could not parse radon raw JSON: {exc}"
        return {}, tool
    return _partition(payload, tool)


def collect_maintainability(paths: list[str], *, cwd: str | None = None) -> tuple[dict[str, dict[str, Any]], ToolRun]:
    """Per-file maintainability index.

    ``radon mi`` reports MI on radon's 0-100 rescaled variant, not the raw
    Coleman-Oman formula; the two differ and are not interchangeable across
    tools.  Recorded as-is with the tool version alongside.
    """
    executable = which("radon")
    argv = _radon_argv(executable) + ["mi", "-j", "--show", *paths]
    tool = ToolRun(name="radon-mi", command=argv)
    tool.version = tool_version(_radon_argv(executable) + ["--version"])
    if tool.version is None:
        tool.status = "unavailable"
        tool.error = "radon is not installed"
        return {}, tool

    result = run(argv, cwd=cwd)
    tool.exit_code = result.returncode
    tool.duration_seconds = result.duration
    if result.returncode != 0:
        tool.status = "error"
        tool.error = (result.stderr or result.stdout).strip()[:2000]
        return {}, tool
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        tool.status = "error"
        tool.error = f"could not parse radon mi JSON: {exc}"
        return {}, tool
    return _partition(payload, tool)


def collect_halstead(paths: list[str], *, cwd: str | None = None) -> tuple[dict[str, dict[str, Any]], ToolRun]:
    """Per-file Halstead metrics.

    Cheap to collect (one more radon pass) and kept because it is one of the
    few volume-style measures independent of cyclomatic complexity.  Radon
    reports these per file under a ``total`` key plus per-function entries;
    only the file totals are normalized here.
    """
    executable = which("radon")
    argv = _radon_argv(executable) + ["hal", "-j", *paths]
    tool = ToolRun(name="radon-hal", command=argv)
    tool.version = tool_version(_radon_argv(executable) + ["--version"])
    if tool.version is None:
        tool.status = "unavailable"
        tool.error = "radon is not installed"
        return {}, tool

    result = run(argv, cwd=cwd)
    tool.exit_code = result.returncode
    tool.duration_seconds = result.duration
    if result.returncode != 0:
        tool.status = "error"
        tool.error = (result.stderr or result.stdout).strip()[:2000]
        return {}, tool
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        tool.status = "error"
        tool.error = f"could not parse radon hal JSON: {exc}"
        return {}, tool

    keys = ("h1", "h2", "N1", "N2", "vocabulary", "length", "calculated_length",
            "volume", "difficulty", "effort", "time", "bugs")
    out: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for path, value in payload.items():
        if isinstance(value, dict) and "error" in value:
            failures.append(f"{path}: {value['error']}")
            continue
        if not isinstance(value, dict) or "total" not in value:
            continue
        total = value["total"]
        if isinstance(total, list) and len(total) == len(keys):
            out[path] = dict(zip(keys, total))
    if failures:
        tool.status = "error"
        tool.error = "; ".join(failures)[:2000]
    return out, tool
