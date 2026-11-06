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
import time
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
        return s


@dataclass
class Change:
    status: str  # added | modified | deleted | renamed | other
    path: str
    old_path: str | None = None


# A small, stable vocabulary: categories are intentionally allowed to overlap.
# These are observations about paths, not dependency or safety conclusions.
# Never export file names in this summary. Changes outside these patterns are
# classified as ``other`` rather than silently disappearing.
_CATEGORY_GLOBS: dict[str, tuple[str, ...]] = {
    "tests": ("tests/**", "test/**", "**/tests/**", "**/test/**", "**/test_*.py", "**/*_test.py",
              "**/*.test.*", "**/*.spec.*", "**/conftest.py"),
    "migrations": ("**/migrations/**", "**/alembic/**", "**/alembic.ini"),
    "dependencies": ("**/pyproject.toml", "**/uv.lock", "**/requirements*.txt", "**/requirements*.in",
                     "**/setup.py", "**/setup.cfg", "**/poetry.lock", "**/Pipfile*", "**/package.json",
                     "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock", "**/Cargo.toml", "**/Cargo.lock",
                     "**/go.mod", "**/go.sum"),
    "ci": (".github/**", "ci/**", "**/.gitlab-ci.yml", "**/Jenkinsfile", "mira-vitals.toml"),
    "frontend": ("**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs", "**/*.ts", "**/*.tsx", "**/*.vue",
                 "**/*.html", "**/*.css", "**/*.scss", "**/*.sass"),
    "documentation": ("**/*.md", "**/*.rst", "**/*.mdx", "**/LICENSE*"),
}


def classify_changes(changes: list[Change]) -> dict[str, int]:
    """Count changed files by overlapping, bounded categories.

    Both sides of a rename contribute their categories, but a renamed file is
    counted at most once per category. An empty diff yields an empty mapping;
    a failed diff is represented by ``None`` by the caller.
    """
    counts: dict[str, int] = {}
    for change in changes:
        paths = [change.path] + ([change.old_path] if change.old_path else [])
        categories = {
            name for name, patterns in _CATEGORY_GLOBS.items()
            if any(matches(path, patterns) for path in paths)
        }
        if any(path.endswith(".py") and not matches(path, _CATEGORY_GLOBS["tests"])
               and not matches(path, _CATEGORY_GLOBS["migrations"])
               and not matches(path, _CATEGORY_GLOBS["ci"]) for path in paths):
            categories.add("application")
        if not categories:
            categories.add("other")
        for name in categories:
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def selection_config_hash(settings: Settings, policy: str) -> str:
    """Stable fingerprint of the effective non-secret selection settings.

    Cache environment values, service URLs and closure-hash inputs do not
    control which tests are selected and must never enter this fingerprint.
    Prefix/version the contract so future changes to the input set are not
    mixed with this one. The raw configuration is not exported.
    """
    config = {
        "version": 1,
        "policy": policy,
        "trigger_globs": settings.trigger_globs,
        "ignore_globs": settings.ignore_globs,
        "test_globs": settings.test_globs,
        "always_run": settings.always_run,
        "graph_sources": settings.graph_sources,
        "ruff_paths": settings.ruff_paths,
    }
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
    return {"triggers": sorted(triggers), "selected": sorted(selected), "tests": sorted(tests)}


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
    started = time.monotonic()
    env = dict(os.environ) if env is None else env
    policy = policy or settings.policy
    doc: dict[str, Any] = {
        "policy": policy,
        "event": event,
        "base": base,
        "head": head,
        "count_unit": "files",
        "selector": {"name": SELECTOR_NAME, "version": _selector_version(env)},
        "selection_config_hash": selection_config_hash(settings, policy),
        "graph": {"source": "+".join(settings.graph_sources)},
        "errors": [],
        "changed_files": None,
        "change_categories": None,
        "change_category_counts": None,
    }
    changes: list[Change] | None = None
    try:
        changes = changed(repo, base, head)
        counts = classify_changes(changes)
        doc["changed_files"] = len(changes)
        doc["change_categories"] = sorted(counts)
        doc["change_category_counts"] = counts
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
    except Exception as exc:  # noqa: BLE001 - fail open: the run is full
        doc["errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
        # Preserve known path-based reasons even when a graph tool fails.
        # Graph-dependent reasons are unknown, not asserted absent.
        known_triggers = decide(settings, changes, [], None, None)["triggers"] if changes is not None else []
        result = {"triggers": sorted({*known_triggers, "selector_error"}), "selected": [], "tests": []}
        test_files = []

    triggers = list(result["triggers"])
    # The argument-file contract is one path per line. Never turn an
    # incomplete list into a smaller test run when the list exceeds the
    # telemetry bound: a full suite is the only safe fallback.
    if (not triggers and policy == "enforce" and event == "pull_request"
            and len(result["selected"]) > MAX_LISTED_FILES):
        triggers.append("unsafe-output-size")
    decision = "full" if triggers else "selected"
    tests = result["tests"]
    selected = result["selected"] if decision == "selected" else tests
    skipped = sorted(set(tests) - set(selected)) if decision == "selected" else []
    # An empty args file means the caller runs the full suite, not zero tests.
    ran_selection = bool(selected) and decision == "selected" and policy == "enforce" and event == "pull_request"
    doc.update(
        {
            # `mode` is what ran (the CI-health contract); `decision` is what
            # the selector would have run. In advisory mode they differ.
            "mode": "selected" if ran_selection else "full",
            "decision": decision,
            "triggers": triggers,
            # A graph failure can leave some triggers unknown, even though
            # known path triggers are retained above.
            "triggers_complete": not doc["errors"],
            "selected": len(selected) if tests else None,
            "total": len(tests) if tests else None,
            "skipped_files": skipped[:MAX_LISTED_FILES],
            "run_files": selected[:MAX_LISTED_FILES] if ran_selection else [],
            "skipped_files_complete": not doc["errors"] and len(skipped) <= MAX_LISTED_FILES,
            "run_files_complete": not doc["errors"],
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
    doc["selector_duration_seconds"] = max(0.0, time.monotonic() - started)
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
        # One test file per line, or empty for "run the whole suite". Read it
        # with `mapfile -t`, never by interpolating it into a command line.
        with open(args_out, "w", encoding="utf-8") as handle:
            handle.writelines(path + "\n" for path in doc.get("run_files") or [])


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
            "triggers_complete": False,
            "selected": None,
            "total": None,
            "count_unit": "files",
            "skipped_files": [],
            "run_files": [],
            "skipped_files_complete": False,
            "run_files_complete": False,
            "selector": {"name": SELECTOR_NAME, "version": _selector_version(env)},
            "selection_config_hash": None,
            "graph": {"source": "none"},
            "errors": [f"config: {exc}"],
            "changed_files": None,
            "change_categories": None,
            "change_category_counts": None,
            "selector_duration_seconds": None,
        }
        return doc
    return run(argv_ns.repo, argv_ns.base, argv_ns.head, settings, policy=argv_ns.mode, event=argv_ns.event, env=env)


if __name__ == "__main__":  # pragma: no cover
    sys.exit("use: python -m mira_vitals ci select")
