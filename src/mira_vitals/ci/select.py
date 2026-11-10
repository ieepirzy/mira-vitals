"""``mira-vitals ci select``: which tests a pull request needs to run.

Test-impact selection with the safety nets agreed for it:

* **Graph.** A test is selected when it can reach a changed module: the
  union of ruff's import graph (``ruff analyze graph``, reverse closure) and
  Lomitus's (``lomitus.impact.affected_files``, which also follows deleted
  and renamed modules' old importers), plus every changed test file and a
  configured always-run list.
* **Full-run triggers.** Changes no import graph can see force a full run:
  any ``conftest.py``, migrations, dependency and container files, CI
  configuration, any non-Python file (data, fixtures and templates feed tests
  without an import), deleted or renamed modules, anything in the import
  closure of a ``conftest.py``, and a changed module the graph doesn't know.
* **Fail open.** Any error -- a missing graph tool, a bad revision, an
  unreadable config -- is a full run, recorded as trigger ``selector_error``.
* **Advisory first.** In ``advisory`` mode (the default) the decision is
  recorded but every test runs; ``ci report`` then counts *escapes*: tests the
  selector would have skipped that failed. ``enforce`` runs only the selection,
  and only on pull requests: pushes and scheduled runs always run everything.
  Turn ``enforce`` on per repository only after several weeks of advisory runs
  with zero escapes (``docs/ci-select.md``).

The unit of selection is the test *file* (pytest's per-file collection); the
counts in ``selection.json`` say so in ``count_unit``.

**Gates** narrow below the file. A gate names a keyword in the names of some
tests, slow ones whose inputs are data no import graph sees (alert-rule unit
tests, say), and the paths those inputs live in. The gate is closed when the
change touches none of its inputs, fires no full-run trigger other than
``non-py``, and reaches no file holding its tests through the graph. A closed
gate skips its tests with ``-k "not <keyword>"`` (enforce, pull requests
only) even when the rest of the run is full. Its policy can differ from the
file selection's (``[select.gates.<name>] policy``).

Result caching is not done here. With ``record_closure_hashes`` the selector
records each test file's Lomitus ``closure_hash`` (code only) with the
environment folded into ``extra`` -- Python version, lockfile digest, named
environment variables, service image digests -- and whether the file's
closure touches the database, network or clock (never cacheable), so a cache
can be *evaluated* later. Nothing is skipped on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

SELECTOR_NAME = "mira-vitals-select"

#: Full-run triggers by path pattern (repo-relative POSIX globs: ``*`` within
#: a segment, ``**`` across segments). Configurable per repository.
DEFAULT_TRIGGER_GLOBS: dict[str, list[str]] = {
    "conftest": ["**/conftest.py"],
    "migrations": ["**/migrations/**", "**/alembic/**", "**/alembic.ini"],
    "deps": [
        "**/pyproject.toml",
        "**/uv.lock",
        "**/requirements*.txt",
        "**/requirements*.in",
        "**/setup.py",
        "**/setup.cfg",
        "**/poetry.lock",
        "**/Pipfile",
        "**/Pipfile.lock",
    ],
    "docker": ["**/Dockerfile", "**/Dockerfile.*", "**/*.Dockerfile", "**/docker-compose*.y*ml", "**/compose*.y*ml"],
    "ci": [".github/**", "ci/**", "mira-vitals.toml"],
}
#: Non-Python files that never feed a test. Everything else non-Python is a
#: full-run trigger (``non-py``): tests read data, fixtures and templates
#: without an import edge any graph could follow.
DEFAULT_IGNORE_GLOBS = ["**/*.md", "**/*.rst", "**/LICENSE*", "**/.gitignore", "**/.editorconfig"]
DEFAULT_TEST_GLOBS = ["**/test_*.py", "**/*_test.py"]
DEFAULT_LOCKFILES = ["uv.lock", "poetry.lock", "requirements.txt", "requirements-dev.txt", "Pipfile.lock"]
#: Paths written to the pytest argument file must be this boring; anything
#: else makes the run full (trigger ``unsafe-path``) rather than risk a
#: shell or argument injection in the calling workflow.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\.py$")
MAX_LISTED_FILES = 5000


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)


#: A gate keyword goes into a pytest ``-k`` expression: an identifier, and
#: not one of the expression's operators.
_KEYWORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,80}$")
_GATE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
_K_OPERATORS = {"and", "or", "not"}
#: Full-run triggers that leave a gate closed: the gate's own inputs decide
#: for data files. Every other trigger (conftest, deps, ci, a graph it cannot
#: trust, a selector error) opens every gate.
GATE_NEUTRAL_TRIGGERS = {"non-py"}


@dataclass
class Gate:
    keyword: str
    inputs: list[str]
    policy: str | None = None  # None: the selection's policy
    #: Globs the gated test files must match; a gated file outside them (the
    #: keyword caught a test it was not meant for) keeps the gate open.
    files: list[str] | None = None


@dataclass
class Settings:
    policy: str = "advisory"
    trigger_globs: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_TRIGGER_GLOBS.items()})
    ignore_globs: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_GLOBS))
    test_globs: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_GLOBS))
    always_run: list[str] = field(default_factory=list)
    graph_sources: list[str] = field(default_factory=lambda: ["ruff", "lomitus"])
    ruff_paths: list[str] = field(default_factory=lambda: ["."])
    record_closure_hashes: bool = False
    cache_env: list[str] = field(default_factory=list)
    service_images: list[str] = field(default_factory=list)
    lockfiles: list[str] = field(default_factory=lambda: list(DEFAULT_LOCKFILES))
    gates: dict[str, Gate] = field(default_factory=dict)

    @classmethod
    def from_table(cls, table: dict[str, Any] | None) -> Settings:
        s = cls()
        if not isinstance(table, dict):
            return s

        def strings(value: Any, name: str) -> list[str]:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"select.{name} must be a list of strings")
            return list(value)

        if "policy" in table:
            if table["policy"] not in ("advisory", "enforce"):
                raise ValueError("select.policy must be 'advisory' or 'enforce'")
            s.policy = table["policy"]
        triggers = table.get("triggers")
        if triggers is not None:
            if not isinstance(triggers, dict):
                raise ValueError("select.triggers must be a table of name -> globs")
            # Configured triggers extend the defaults; a default can only be
            # narrowed by naming it explicitly (an empty list turns it off).
            for name, globs in triggers.items():
                s.trigger_globs[str(name)] = strings(globs, f"triggers.{name}")
        for key in ("ignore_globs", "test_globs", "always_run", "graph_sources", "ruff_paths", "cache_env",
                    "service_images", "lockfiles"):
            if key in table:
                setattr(s, key, strings(table[key], key))
        unknown = set(s.graph_sources) - {"ruff", "lomitus"}
        if unknown or not s.graph_sources:
            raise ValueError(f"select.graph_sources must name ruff and/or lomitus, not {sorted(unknown)}")
        if "record_closure_hashes" in table:
            s.record_closure_hashes = bool(table["record_closure_hashes"])
        gates = table.get("gates")
        if gates is not None:
            if not isinstance(gates, dict):
                raise ValueError("select.gates must be a table of name -> gate")
            for name, gate in gates.items():
                if not isinstance(name, str) or not _GATE_NAME.match(name):
                    raise ValueError(f"select.gates: {name!r} is not a short lowercase name")
                if not isinstance(gate, dict):
                    raise ValueError(f"select.gates.{name} must be a table")  # noqa: TRY004 - config errors are ValueError
                unknown_keys = set(gate) - {"keyword", "inputs", "policy", "files"}
                if unknown_keys:
                    raise ValueError(f"select.gates.{name}: unknown keys {sorted(unknown_keys)}")
                keyword = gate.get("keyword")
                if not isinstance(keyword, str) or not _KEYWORD.match(keyword) or keyword.lower() in _K_OPERATORS:
                    raise ValueError(f"select.gates.{name}.keyword must be an identifier of 3 or more characters")
                inputs = strings(gate.get("inputs"), f"gates.{name}.inputs")
                if not inputs:
                    raise ValueError(f"select.gates.{name}.inputs must name at least one glob")
                policy = gate.get("policy")
                if policy is not None and policy not in ("advisory", "enforce"):
                    raise ValueError(f"select.gates.{name}.policy must be 'advisory' or 'enforce'")
                files = strings(gate["files"], f"gates.{name}.files") if "files" in gate else None
                s.gates[name] = Gate(keyword=keyword, inputs=inputs, policy=policy, files=files)
        return s


@dataclass
class Change:
    status: str  # added | modified | deleted | renamed | other
    path: str
    old_path: str | None = None


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=120
    ).stdout


def changed(repo: str, base: str, head: str) -> list[Change]:
    out = _git(["diff", "--name-status", "-M", "-z", f"{base}...{head}"], repo)
    parts = out.split("\0")
    changes, i = [], 0
    while i < len(parts) - 1:
        code = parts[i]
        if not code:
            i += 1
            continue
        if code[0] in "RC":
            changes.append(Change("renamed" if code[0] == "R" else "added", parts[i + 2], parts[i + 1]))
            i += 3
        else:
            status = {"A": "added", "M": "modified", "D": "deleted"}.get(code[0], "other")
            changes.append(Change(status, parts[i + 1]))
            i += 2
    return changes


def ruff_graph(repo: str, paths: list[str]) -> dict[str, list[str]]:
    """File -> files it imports (repo-relative), from ``ruff analyze graph``."""
    ruff = os.environ.get("MIRA_VITALS_RUFF") or "ruff"
    out = subprocess.run(
        [ruff, "analyze", "graph", "--detect-string-imports", *paths],
        cwd=repo, capture_output=True, text=True, check=True, timeout=300,
    ).stdout
    data = json.loads(out)
    if not isinstance(data, dict):
        raise TypeError("ruff analyze graph did not return a map")
    return {str(k): [str(v) for v in vs] for k, vs in data.items()}


def _closure(start: Iterable[str], edges: dict[str, set[str]]) -> set[str]:
    seen = set(start)
    todo = list(seen)
    while todo:
        for nxt in edges.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def reverse_closure(deps: dict[str, list[str]], files: Iterable[str]) -> set[str]:
    dependents: dict[str, set[str]] = {}
    for src, targets in deps.items():
        for t in targets:
            dependents.setdefault(t, set()).add(src)
    return _closure(files, dependents)


def forward_closure(deps: dict[str, list[str]], files: Iterable[str]) -> set[str]:
    return _closure(files, {k: set(v) for k, v in deps.items()})


def lomitus_affected(repo: str, base: str, head: str) -> tuple[set[str], str | None]:
    from lomitus import impact  # optional dependency: absent -> the caller fails open

    version = None
    try:
        from importlib.metadata import version as _v

        version = _v("lomitus")
    except Exception:  # noqa: BLE001 - a version is a nicety
        version = None
    return set(impact.affected_files(repo, base, head)), version


def _ruff_version() -> str | None:
    try:
        out = subprocess.run(
            [os.environ.get("MIRA_VITALS_RUFF") or "ruff", "--version"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        return out.strip().split()[-1]
    except Exception:  # noqa: BLE001
        return None


def _text(repo: str, rel: str) -> str | None:
    try:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def gated_test_files(repo: str, test_files: list[str], keyword: str, conftests: list[str] = ()) -> set[str]:
    """Test files that may hold a test pytest's ``-k <keyword>`` matches.

    ``-k`` matches case-insensitively on module, class and function names,
    parametrize ids, marker names and extra keywords, so a file counts when
    the keyword appears anywhere in its path or text, and every test file
    counts when a conftest.py mentions it (a conftest can add markers or
    keywords to any test). Over-counting only opens the gate more often; a
    file that cannot be read counts too."""
    kw = keyword.lower()
    if any(kw in (_text(repo, c) or kw).lower() for c in conftests):
        return set(test_files)
    return {rel for rel in test_files if kw in rel.lower() or kw in (_text(repo, rel) or kw).lower()}


def decide_gates(
    settings: Settings,
    changes: list[Change],
    triggers: list[str],
    reached: set[str],
    gated_files: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Per gate: open (its tests run) or closed, and why it is open."""
    out = []
    changed_paths = [p for c in changes for p in [c.path] + ([c.old_path] if c.old_path else [])]
    for name, gate in sorted(settings.gates.items()):
        # A path is also tried as a directory: a submodule bump changes only
        # its gitlink, `vendor/rules`, which `vendor/rules/**` would miss.
        reasons = [f"input:{p}" for p in changed_paths
                   if matches(p, gate.inputs) or matches(p + "/-", gate.inputs)]
        reasons += [f"trigger:{t}" for t in triggers if t not in GATE_NEUTRAL_TRIGGERS]
        files = gated_files.get(name, set())
        reasons += [f"reaches:{f}" for f in sorted(files & (reached | set(changed_paths)))]
        # The always-run list is a must-run safety net: a gate never skips it.
        reasons += [f"always-run:{f}" for f in sorted(files) if matches(f, settings.always_run)]
        if gate.files is not None:
            reasons += [f"unexpected-gated-file:{f}" for f in sorted(files) if not matches(f, gate.files)]
        if not files:
            reasons.append("no-gated-tests")
        out.append(
            {
                "name": name,
                "keyword": gate.keyword,
                "policy": gate.policy,
                "open": bool(reasons),
                "reasons": reasons[:20],
                "files": sorted(files)[:200],
            }
        )
    return out


def tracked_files(repo: str) -> list[str]:
    return [p for p in _git(["ls-files", "-z"], repo).split("\0") if p]


def decide(
    settings: Settings,
    changes: list[Change],
    test_files: list[str],
    graph: dict[str, list[str]] | None,
    lomitus_files: set[str] | None,
) -> dict[str, Any]:
    """The selection itself, pure: which full-run triggers fire and which test
    files a change reaches. ``graph`` is ruff's dependency map (or None when
    ruff isn't a configured source); ``lomitus_files`` is Lomitus's
    affected-files set (or None)."""
    triggers: set[str] = set()
    tests = set(test_files)
    present_py: list[str] = []
    for c in changes:
        paths = [c.path] + ([c.old_path] if c.old_path else [])
        for name, globs in settings.trigger_globs.items():
            if globs and any(matches(p, globs) for p in paths):
                triggers.add(name)
        if c.status == "other":
            triggers.add("unknown-change")
        is_py = c.path.endswith(".py")
        was_py = bool(c.old_path and c.old_path.endswith(".py"))
        if c.status == "deleted" and is_py:
            triggers.add("deleted-py")
        if c.status == "renamed" and (is_py or was_py):
            triggers.add("deleted-py")
        for p in paths:
            if not p.endswith(".py") and not matches(p, settings.ignore_globs):
                triggers.add("non-py")
        if is_py and c.status != "deleted":
            present_py.append(c.path)

    reached: set[str] = set()
    if graph is not None:
        known = set(graph) | {t for ts in graph.values() for t in ts}
        if any(p not in known for p in present_py):
            triggers.add("unresolved")
        conftests = [f for f in graph if f.rsplit("/", 1)[-1] == "conftest.py"]
        conftest_closure = forward_closure(graph, conftests)
        if any(p in conftest_closure for p in present_py):
            triggers.add("conftest-closure")
        reached |= reverse_closure(graph, present_py)
    if lomitus_files is not None:
        reached |= lomitus_files
    selected = {f for f in reached if f in tests}
    selected |= {p for p in present_py if p in tests}
    selected |= {f for f in tests if matches(f, settings.always_run)}
    if any(not _SAFE_PATH.match(f) for f in selected):
        triggers.add("unsafe-path")
    return {"triggers": sorted(triggers), "selected": sorted(selected), "tests": sorted(tests), "reached": reached}


def _sha256_file(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def cache_extra(settings: Settings, repo: str, env: dict[str, str]) -> list[str]:
    """What a result-cache key needs beyond the code: Python version, lockfile
    digests, the named environment variables (hashed), service image digests."""
    extra = [f"python={_platform.python_version()}"]
    for lock in settings.lockfiles:
        digest = _sha256_file(os.path.join(repo, lock))
        if digest:
            extra.append(f"lock:{lock}={digest}")
    for name in settings.cache_env:
        value = env.get(name, "")
        extra.append(f"env:{name}={hashlib.sha256(value.encode()).hexdigest()}")
    extra += [f"image={img}" for img in settings.service_images]
    return extra


def closure_hashes(repo: str, test_files: list[str], extra: list[str]) -> dict[str, dict[str, Any]]:
    """Per test file: Lomitus's code-only closure hash (with ``extra``) and
    whether its closure reaches the database, network or clock (never
    cacheable). Recorded for evaluation; nothing is skipped on it."""
    from lomitus import impact

    graph = impact.index_project(repo)
    effects = impact.node_effects(graph)
    external_files = {f for (f, _n), kind in effects.items() if kind == "external"}
    out: dict[str, dict[str, Any]] = {}
    for rel in test_files:
        absolute = os.path.join(os.path.abspath(repo), rel)
        key = impact.closure_hash(graph, absolute, repo, extra=extra)
        closure = impact.forward_closure(graph, [absolute])
        out[rel] = {"closure_hash": key, "cacheable": not (closure & external_files)}
    return out


def run(
    repo: str,
    base: str,
    head: str,
    settings: Settings,
    *,
    policy: str | None = None,
    event: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compute ``selection.json``. Never raises: any error is a full run with
    trigger ``selector_error`` (and the message in ``errors``)."""
    env = dict(os.environ) if env is None else env
    # An explicit `--mode advisory` turns all skipping off, the gates'
    # included. `--mode enforce` never raises a gate soaking in advisory.
    override = policy
    policy = policy or settings.policy
    doc: dict[str, Any] = {
        "policy": policy,
        "event": event,
        "base": base,
        "head": head,
        "count_unit": "files",
        "selector": {"name": SELECTOR_NAME, "version": _selector_version(env)},
        "graph": {"source": "+".join(settings.graph_sources)},
        "errors": [],
    }
    try:
        changes = changed(repo, base, head)
        all_files = tracked_files(repo)
        test_files = [f for f in all_files if f.endswith(".py") and matches(f, settings.test_globs)]
        graph = None
        if "ruff" in settings.graph_sources:
            graph = ruff_graph(repo, settings.ruff_paths)
            doc["graph"]["ruff_version"] = _ruff_version()
        lomitus_files = None
        if "lomitus" in settings.graph_sources:
            lomitus_files, lomitus_version = lomitus_affected(repo, base, head)
            doc["graph"]["lomitus_version"] = lomitus_version
        result = decide(settings, changes, test_files, graph, lomitus_files)
        conftests = [f for f in all_files if f.rsplit("/", 1)[-1] == "conftest.py"]
        gated = {name: gated_test_files(repo, test_files, g.keyword, conftests) for name, g in settings.gates.items()}
        gates = decide_gates(settings, changes, result["triggers"], result["reached"], gated)
        doc["changed_files"] = len(changes)
    except Exception as exc:  # noqa: BLE001 - fail open: the run is full
        doc["errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
        result = {"triggers": ["selector_error"], "selected": [], "tests": []}
        test_files = []
        gates = [
            {"name": n, "keyword": g.keyword, "policy": g.policy, "open": True, "reasons": ["selector_error"],
             "files": []}
            for n, g in sorted(settings.gates.items())
        ]

    triggers = result["triggers"]
    decision = "full" if triggers else "selected"
    tests = result["tests"]
    selected = result["selected"] if decision == "selected" else tests
    skipped = sorted(set(tests) - set(selected)) if decision == "selected" else []
    ran_selection = decision == "selected" and policy == "enforce" and event == "pull_request"
    for gate in gates:
        gate["policy"] = "advisory" if override == "advisory" else (gate["policy"] or policy)
    closed = [g for g in gates if not g["open"]]
    # A closed gate's tests are skipped only by an enforcing gate on a pull
    # request, like the file selection; the default branch runs everything.
    deselected = [g["keyword"] for g in closed if g["policy"] == "enforce" and event == "pull_request"]
    doc.update(
        {
            # `mode` is what ran (the CI-health contract); `decision` is what
            # the selector would have run. In advisory mode they differ.
            "mode": "selected" if ran_selection or deselected else "full",
            "gates": gates,
            # Keywords whose tests this change would skip (closed gates) and
            # those it did skip: `ci report` counts escapes on the former.
            "skipped_keywords": sorted(g["keyword"] for g in closed),
            "deselected_keywords": sorted(deselected),
            "decision": decision,
            "triggers": triggers,
            "selected": len(selected) if tests else None,
            "total": len(tests) if tests else None,
            "skipped_files": skipped[:MAX_LISTED_FILES],
            "run_files": selected[:MAX_LISTED_FILES] if ran_selection else [],
        }
    )
    if settings.record_closure_hashes and test_files and not doc["errors"]:
        try:
            doc["closure"] = {
                "extra": cache_extra(settings, repo, env),
                "files": closure_hashes(repo, test_files, cache_extra(settings, repo, env)),
            }
        except Exception as exc:  # noqa: BLE001 - recording only
            doc["errors"].append(f"closure hashes: {type(exc).__name__}: {str(exc)[:200]}")
    return doc


def _selector_version(env: dict[str, str]) -> str:
    from .. import __version__

    rev = (env.get("MIRA_VITALS_CI_REVISION") or "")[:12]
    return f"{__version__}+{rev}" if re.fullmatch(r"[0-9a-f]{7,12}", rev) else __version__


def write_outputs(doc: dict[str, Any], output: str, args_out: str | None) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(output)) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, sort_keys=True)
        handle.write("\n")
    if args_out:
        # pytest arguments, one per line: the test files to run (none means
        # the whole suite), then `-k` and an expression when a gate deselects
        # tests. Read it with `mapfile -t`, never by interpolating it into a
        # command line.
        lines = list(doc.get("run_files") or [])
        keywords = [k for k in doc.get("deselected_keywords") or [] if _KEYWORD.match(k)]
        if keywords:
            lines += ["-k", " and ".join(f"not {k}" for k in keywords)]
        with open(args_out, "w", encoding="utf-8") as handle:
            handle.writelines(line + "\n" for line in lines)


def main_select(argv_ns: Any, env: dict[str, str], table: dict[str, Any] | None) -> dict[str, Any]:
    try:
        settings = Settings.from_table(table)
    except Exception as exc:  # noqa: BLE001 - a bad config is a full run
        settings = Settings()
        doc = {
            "policy": argv_ns.mode or "advisory",
            "event": argv_ns.event,
            "mode": "full",
            "decision": "full",
            "triggers": ["selector_error"],
            "selected": None,
            "total": None,
            "count_unit": "files",
            "skipped_files": [],
            "run_files": [],
            "gates": [],
            "skipped_keywords": [],
            "deselected_keywords": [],
            "selector": {"name": SELECTOR_NAME, "version": _selector_version(env)},
            "graph": {"source": "none"},
            "errors": [f"config: {exc}"],
        }
        return doc
    return run(argv_ns.repo, argv_ns.base, argv_ns.head, settings, policy=argv_ns.mode, event=argv_ns.event, env=env)


if __name__ == "__main__":  # pragma: no cover
    sys.exit("use: python -m mira_vitals ci select")
