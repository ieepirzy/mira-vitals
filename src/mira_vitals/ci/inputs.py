"""``mira-vitals ci inputs``: record which data files each test file reads.

The selector's ``non-py`` trigger exists because tests read data, fixtures and
templates without an import edge any graph could follow. In a repository that
is mostly data (Flux manifests, alert rules), that trigger fires on nearly
every change and selection never happens. A *data map* replaces the trigger
with a measurement: for each test file, the input globs it was seen to read.

**Recording.** Each test file runs in its own pytest process (so no cache or
session fixture hides a read behind another file's run) with
``inputs_plugin`` loaded. Per file, the recorded paths become globs:

* a file it opened -> every file in that directory (``dir/*``): a sibling the
  test probes for without opening is still covered;
* a directory it listed -> everything below it (``dir/**``): a new file or
  sub-directory a walk or glob would find is covered;
* it started a process, a test was skipped, failed or errored, the session
  was interrupted, or it listed the repository root -> ``**`` (everything).
  The record cannot be trusted to be complete, so the file runs on any data
  change. Python source a test reads as data (hashing it, say) is recorded
  like any file; reads made by the import system are not, the graph owns them.

**Staleness.** Each entry pins the test file's *code digest*: the contents of
the test file and its first-party forward import closure (ruff's graph). When
the selector sees a different digest, that test's code changed since the
recording, so its entry is ignored and the file runs on any data change, the
same as a test file the map does not list. Re-record to win the savings back.

The map is written as JSON (``mira-vitals-inputs.json`` by default) and named
in the repository's configuration as ``select.data_map``.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

MAP_VERSION = 1
UNIVERSAL = "**"
PLUGIN_MODULE = "mira_vitals_inputs_plugin"


def code_digest(repo: str, graph: dict[str, list[str]], test_file: str) -> str:
    """sha256 over the test file and its first-party forward import closure."""
    from .select import forward_closure

    files = sorted(forward_closure(graph, [test_file]) | {test_file})
    h = hashlib.sha256()
    for rel in files:
        try:
            with open(os.path.join(repo, rel), "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            digest = "missing"
        h.update(f"{rel}\0{digest}\n".encode())
    return h.hexdigest()


def to_globs(record: dict[str, Any]) -> tuple[list[str], str | None]:
    """Input globs for one test file's record, and why it is universal (if)."""
    if record.get("spawned"):
        return [UNIVERSAL], "spawned a process: " + str(record["spawned"][0])[:120]
    if record.get("incomplete"):
        return [UNIVERSAL], "incomplete recording: " + str(record["incomplete"][0])[
            :120
        ]
    globs: set[str] = set()
    for rel in record.get("lists") or []:
        if rel in (".", ""):
            return [UNIVERSAL], "listed the repository root"
        globs.add(f"{rel}/**")
    for rel in record.get("reads") or []:
        if rel.endswith(".pyc"):
            continue
        parent = os.path.dirname(rel)
        globs.add(f"{parent}/*" if parent else "*")
    # Drop what an ancestor's subtree glob already covers.
    trees = {g[:-3] for g in globs if g.endswith("/**")}

    def covered(glob: str) -> bool:
        base, _, tail = glob.rpartition("/")
        under = any(
            base.startswith(t + "/") for t in trees
        )  # strictly below another subtree
        return under or (tail == "*" and base in trees)  # dir/* beside dir/**

    return sorted(g for g in globs if not covered(g)), None


def _record_one(
    repo: str,
    python: str,
    plugin_dir: str,
    test_file: str,
    extra: list[str],
    timeout: float,
) -> dict[str, Any]:
    fd, out = tempfile.mkstemp(prefix="mv-inputs-", suffix=".json")
    os.close(fd)
    os.unlink(out)
    env = dict(os.environ)
    env["MIRA_VITALS_INPUTS_ROOT"] = os.path.realpath(repo)
    env["MIRA_VITALS_INPUTS_OUT"] = out
    env["PYTHONPATH"] = plugin_dir + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [
        python,
        "-m",
        "pytest",
        "-p",
        PLUGIN_MODULE,
        "-p",
        "no:cacheprovider",
        "-q",
        "-x",
        *extra,
        test_file,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        return {"incomplete": [f"timeout after {timeout:.0f}s"]}
    try:
        with open(out, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return {"incomplete": [f"no record (pytest exit {rc})"]}
    finally:
        if os.path.exists(out):
            os.unlink(out)
    return record


def record(
    repo: str,
    test_files: list[str],
    graph: dict[str, list[str]],
    *,
    python: str | None = None,
    extra_args: list[str] | None = None,
    jobs: int = 2,
    timeout: float = 900.0,
    commit: str | None = None,
) -> dict[str, Any]:
    """Run every test file under the recorder and build the data map."""
    python = python or sys.executable
    plugin_dir = tempfile.mkdtemp(prefix="mv-inputs-plugin-")
    try:
        shutil.copyfile(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "inputs_plugin.py"
            ),
            os.path.join(plugin_dir, PLUGIN_MODULE + ".py"),
        )
        with cf.ThreadPoolExecutor(max(1, jobs)) as pool:
            records = dict(
                zip(
                    test_files,
                    pool.map(
                        lambda f: _record_one(
                            repo, python, plugin_dir, f, list(extra_args or []), timeout
                        ),
                        test_files,
                    ),
                )
            )
    finally:
        shutil.rmtree(plugin_dir, ignore_errors=True)
    tests: dict[str, Any] = {}
    for rel in sorted(records):
        globs, why = to_globs(records[rel])
        entry: dict[str, Any] = {
            "inputs": globs,
            "code_digest": code_digest(repo, graph, rel),
        }
        if why:
            entry["universal_because"] = why
        tests[rel] = entry
    return {
        "version": MAP_VERSION,
        "tool": "mira-vitals ci inputs",
        "commit": commit,
        "tests": tests,
    }


def load_map(path: str) -> dict[str, dict[str, Any]]:
    """The map's ``tests`` table, validated. Raises on anything unexpected (the
    selector turns that into a full run)."""
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    if not isinstance(doc, dict) or doc.get("version") != MAP_VERSION:
        raise ValueError(f"data map {path}: version must be {MAP_VERSION}")
    tests = doc.get("tests")
    if not isinstance(tests, dict):
        raise ValueError(f"data map {path}: 'tests' must be a table")  # noqa: TRY004 - config errors are ValueError
    for rel, entry in tests.items():
        inputs = entry.get("inputs") if isinstance(entry, dict) else None
        # Empty is valid: the test was seen to read no data at all.
        if not isinstance(inputs, list) or not all(
            isinstance(g, str) and g for g in inputs
        ):
            raise ValueError(f"data map {path}: {rel} needs a list of input globs")
        if not isinstance(entry.get("code_digest"), str):
            raise ValueError(f"data map {path}: {rel} has no code_digest")  # noqa: TRY004 - config errors are ValueError
    return tests


def current_entries(
    repo: str, tests: dict[str, dict[str, Any]], graph: dict[str, list[str]] | None
) -> tuple[dict[str, list[str]], list[str]]:
    """Entries whose code digest still matches (test -> globs), and the stale
    ones. Without a graph nothing can be checked, so nothing is current."""
    if graph is None:
        return {}, sorted(tests)
    current, stale = {}, []
    for rel, entry in tests.items():
        if code_digest(repo, graph, rel) == entry["code_digest"]:
            current[rel] = list(entry["inputs"])
        else:
            stale.append(rel)
    return current, sorted(stale)


def write_map(doc: dict[str, Any], output: str) -> None:
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, sort_keys=True)
        handle.write("\n")
