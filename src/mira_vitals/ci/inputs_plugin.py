"""pytest plugin: record which repository files one pytest process reads.

Loaded by ``mira-vitals ci inputs`` into the *target* project's pytest, one
process per test file, as ``-p mira_vitals_inputs_plugin`` from a copy of this
file. It must stay standard-library only: it runs in someone else's
environment, where mira_vitals itself may not be installed.

An audit hook (PEP 578) sees every ``open`` (read access only), directory
listing (``os.listdir``, ``os.scandir``, hence ``os.walk`` and
``pathlib.Path.glob``), ``glob.glob`` pattern and ``sqlite3.connect`` under
``MIRA_VITALS_INPUTS_ROOT``. A path reached through a symbolic link is
recorded under both its link and its target; a link out of the repository
marks the record incomplete. Anything that starts another process is noted:
a child's reads are invisible here, so the recorder treats such a test file
as reading everything. A skipped, failed, errored or deselected test, or a
session that collected nothing, marks the record ``incomplete`` for the same
reason (the test may not have reached its reads). The record is written to
``MIRA_VITALS_INPUTS_OUT`` as JSON when the session ends.

What no audit hook sees: ``os.stat`` / ``os.path.exists`` / ``os.access``
probes, and files opened from C code (extension modules). The selector's
fail-safe covers a new or never-recorded file; an existing file one test was
recorded reading, which another test reads only that way, is the residual
blind spot.
"""

from __future__ import annotations

import json
import os
import sys
import sysconfig
import threading
from importlib.machinery import all_suffixes

_ROOT = os.environ.get("MIRA_VITALS_INPUTS_ROOT")
_OUT = os.environ.get("MIRA_VITALS_INPUTS_OUT")
_READS: set[str] = set()
_LISTS: set[str] = set()
_SPAWNED: list[str] = []
_INCOMPLETE: list[str] = []
#: Re-entrancy guard, per thread: the hook's own realpath/lstat calls must not
#: recurse, but another thread's open in the meantime must still be seen.
_LOCAL = threading.local()
_SPAWN_EVENTS = {
    "subprocess.Popen",
    "os.system",
    "os.posix_spawn",
    "os.exec",
    "os.spawn",
    "os.fork",
    "os.forkpty",
    "os.startfile",
    "pty.spawn",
}
_WATCHED = {
    "open",
    "os.listdir",
    "os.scandir",
    "glob.glob",
    "glob.glob/2",
    "sqlite3.connect",
}
#: Files the import system reads as modules (the import graph owns them). Any
#: other file it reads -- pkgutil.get_data, a loader's get_data -- is data.
_MODULE_SUFFIXES = tuple(set(all_suffixes()) | {".pyc", ".pth"})
_PATHS = sysconfig.get_paths()
_STDLIB = tuple(
    {os.path.realpath(_PATHS[k]) + os.sep for k in ("stdlib", "platstdlib")}
)
#: Installed packages. In a virtual environment platstdlib is the venv's
#: lib/pythonX.Y, which holds site-packages: those are never the stdlib.
_SITE = tuple({os.path.realpath(_PATHS[k]) + os.sep for k in ("purelib", "platlib")})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_SKIP_TOP = (".git", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache")
_RUNNER: tuple[tuple[str, ...], tuple[str, ...]] | None = None


def _runner_dirs() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """pytest's and pluggy's install locations (real paths), and pytest's
    vendored ``py`` library, which is a test's tool, not the runner. Taken
    from the loaded modules, never from a path segment: a checkout may well
    live under a directory named ``pytest``."""
    global _RUNNER
    if _RUNNER is None:
        dirs: set[str] = set()
        excepted: set[str] = set()
        for name in ("_pytest", "pluggy", "pytest"):
            where = getattr(sys.modules.get(name), "__file__", None)
            if not where:
                continue
            real = os.path.realpath(where)
            if os.path.basename(real) == "__init__.py":
                base = os.path.dirname(real)
                dirs.add(base + os.sep)
                if name == "_pytest":
                    excepted.add(base + os.sep + "_py" + os.sep)
            else:  # a single-module install
                dirs.add(real)
        _RUNNER = (tuple(dirs), tuple(excepted))
    return _RUNNER


def _inside(full: str, root: str) -> str | None:
    if full == root:
        return "."
    if not full.startswith(root + os.sep):
        return None
    rel = os.path.relpath(full, root).replace(os.sep, "/")
    parts = rel.split("/")
    if parts[0] in _SKIP_TOP or "__pycache__" in parts:
        return None
    return rel


def _rels(path: object) -> list[str]:
    """Repository-relative POSIX paths for an accessed path: as named (a
    symbolic link stays the link) and resolved, each when inside the
    repository. A path named inside the repository that resolves outside it
    marks the record incomplete: what it reads is not tracked."""
    if path is None or isinstance(path, int):
        return []
    try:
        text = os.fsdecode(path)  # str, bytes or PathLike
    except TypeError:
        return []
    root = os.path.realpath(_ROOT or ".")
    named = os.path.normpath(os.path.join(os.getcwd(), text))
    real = os.path.realpath(named)
    out: list[str] = []
    for full in (named, real):
        rel = _inside(full, root)
        if rel is not None and rel not in out:
            out.append(rel)
    named_rel = _inside(named, root)
    if named_rel not in (None, ".") and _inside(real, root) is None and real != named:
        _INCOMPLETE.append(f"symlink out of the repository: {named_rel}")
    return out


def _stdlib(real: str) -> bool:
    return real.startswith(_STDLIB) and not (
        real.startswith(_SITE) or os.sep + "site-packages" + os.sep in real
    )


def _from_the_runner() -> bool:
    """Whether the access comes from pytest itself: the nearest caller frame
    outside the standard library belongs to pytest or pluggy. Test code, the
    project's helpers and third-party libraries a test calls all count."""
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if name == __file__ or name.startswith("<frozen"):
            frame = frame.f_back
            continue
        real = os.path.realpath(name) if not name.startswith("<") else name
        if _stdlib(real):
            frame = frame.f_back
            continue
        dirs, excepted = _runner_dirs()
        if real.startswith(excepted):
            return False
        return real.startswith(dirs) or real in dirs
    return False


def _from_the_import_system() -> bool:
    """Whether the direct caller is the import system (FileFinder caching a
    sys.path entry, a loader reading a module, zipimport). The hook drops
    its listings and its reads of module files only."""
    caller = sys._getframe(2)  # 0 = this function, 1 = _hook, 2 = the caller
    name = caller.f_code.co_filename if caller is not None else ""
    return name.startswith(("<frozen importlib", "<frozen zipimport")) or name.endswith(
        ("importlib/_bootstrap_external.py", "zipimport.py")
    )


def _glob_prefix(pattern: str) -> str:
    """The directory part of a glob pattern before its first wildcard."""
    head: list[str] = []
    for part in pattern.replace(os.sep, "/").split("/"):
        if any(ch in part for ch in "*?["):
            break
        head.append(part)
    else:
        head = head[:-1]  # no wildcard: the pattern names a file; its directory
    return "/".join(head) or "."


def _hook(event: str, args: tuple) -> None:
    if event not in _WATCHED and event not in _SPAWN_EVENTS:
        return
    if getattr(_LOCAL, "busy", False):
        return
    _LOCAL.busy = True
    try:
        if event in _SPAWN_EVENTS:
            _SPAWNED.append(f"{event}: {str(args)[:200]}")
            return
        if _from_the_runner():
            return
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if isinstance(mode, str):
                if any(ch in mode for ch in "wax"):
                    return
            elif isinstance(flags, int) and flags & _WRITE_FLAGS:
                return
            if (
                isinstance(path, (str, bytes, os.PathLike))
                and os.fsdecode(path).endswith(_MODULE_SUFFIXES)
                and _from_the_import_system()
            ):
                return
            _READS.update(_rels(path))
        elif event in ("os.listdir", "os.scandir"):
            if _from_the_import_system():
                return
            path = args[0] if args else "."
            _LISTS.update(_rels("." if path is None else path))
        elif event in ("glob.glob", "glob.glob/2"):
            pattern = os.fsdecode(args[0]) if args else ""
            root_dir = args[2] if len(args) > 2 and args[2] is not None else ""
            _LISTS.update(
                _rels(os.path.join(os.fsdecode(root_dir), _glob_prefix(pattern)))
            )
        elif event == "sqlite3.connect":
            database = args[0] if args else None
            if isinstance(database, (str, bytes, os.PathLike)):
                text = os.fsdecode(database)
                if text.startswith("file:"):
                    _INCOMPLETE.append(f"sqlite URI not tracked: {text[:80]}")
                elif text and text != ":memory:":
                    _READS.update(_rels(text))
    except Exception:  # noqa: BLE001 - a hook must never break the test it watches
        _INCOMPLETE.append(f"hook error on {event}")
    finally:
        _LOCAL.busy = False


if _ROOT and _OUT:
    sys.addaudithook(_hook)


def pytest_collectreport(report):
    if report.failed:
        _INCOMPLETE.append(f"collect:{report.nodeid}")


def pytest_deselected(items):
    # -k / -m / --deselect / addopts left tests unrun: their reads are unknown.
    for item in list(items)[:5]:
        _INCOMPLETE.append(f"deselected:{getattr(item, 'nodeid', item)}")


def pytest_runtest_logreport(report):
    if report.failed or report.skipped:
        _INCOMPLETE.append(f"{report.when}:{report.nodeid}")


def pytest_sessionfinish(session, exitstatus):
    if not (_ROOT and _OUT):
        return
    status = int(exitstatus)
    incomplete = list(_INCOMPLETE)
    # 1 = tests failed (already listed); 2+ = interrupted, usage, nothing collected
    if status not in (0, 1):
        incomplete.append(f"exitstatus:{status}")
    if getattr(session, "testscollected", 1) == 0:
        incomplete.append("no tests collected")
    doc = {
        "reads": sorted(_READS),
        "lists": sorted(_LISTS),
        "spawned": _SPAWNED[:50],
        "incomplete": incomplete[:50],
    }
    with open(_OUT, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
