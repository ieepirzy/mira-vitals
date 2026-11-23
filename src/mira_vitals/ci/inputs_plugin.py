"""pytest plugin: record which repository files one pytest process reads.

Loaded by ``mira-vitals ci inputs`` into the *target* project's pytest, one
process per test file, as ``-p mira_vitals_inputs_plugin`` from a copy of this
file. It must stay standard-library only: it runs in someone else's
environment, where mira_vitals itself may not be installed.

An audit hook (PEP 578) sees every ``open`` (read access only), directory
listing (``os.listdir``, ``os.scandir``, hence ``os.walk`` and
``pathlib.Path.glob``) and ``glob.glob`` pattern under
``MIRA_VITALS_INPUTS_ROOT``. It also notes anything that starts another
process: a child's reads are invisible here, so the recorder treats such a
test file as reading everything. A skipped, failed or errored test, or a
session that collected nothing, marks the record ``incomplete`` for the same
reason (the test may not have reached its reads). The record is written to
``MIRA_VITALS_INPUTS_OUT`` as JSON when the session ends.
"""

from __future__ import annotations

import json
import os
import sys
import sysconfig

_ROOT = os.environ.get("MIRA_VITALS_INPUTS_ROOT")
_OUT = os.environ.get("MIRA_VITALS_INPUTS_OUT")
_READS: set[str] = set()
_LISTS: set[str] = set()
_SPAWNED: list[str] = []
_INCOMPLETE: list[str] = []
_BUSY = False
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
_PATHS = sysconfig.get_paths()
_STDLIB = tuple(
    {os.path.realpath(_PATHS[k]) + os.sep for k in ("stdlib", "platstdlib")}
)
#: Installed packages. In a virtual environment platstdlib is the venv's
#: lib/pythonX.Y, which holds site-packages: those are never the stdlib.
_SITE = tuple(
    {os.path.realpath(_PATHS[k]) + os.sep for k in ("purelib", "platlib")}
    | {os.sep + "site-packages" + os.sep}
)
#: pytest's own collection lists the root and test directories and reads its
#: configuration; those reads are the runner's, not the test's.
_RUNNER_PARTS = (
    os.sep + "_pytest" + os.sep,
    os.sep + "pluggy" + os.sep,
    os.sep + "pytest" + os.sep,
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _rel(path: object) -> str | None:
    """Repository-relative POSIX path, or None outside the repository."""
    if path is None or isinstance(path, int):
        return None
    try:
        text = os.fsdecode(path)  # str, bytes or PathLike
    except TypeError:
        return None
    root = os.path.realpath(_ROOT or ".")
    full = os.path.realpath(os.path.join(os.getcwd(), text))
    if full == root:
        return "."
    if not full.startswith(root + os.sep):
        return None
    rel = os.path.relpath(full, root).replace(os.sep, "/")
    parts = rel.split("/")
    if (
        parts[0]
        in (".git", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache")
        or "__pycache__" in parts
    ):
        return None
    return rel


def _from_the_runner() -> bool:
    """Whether the access comes from pytest itself: the nearest caller frame
    outside the standard library belongs to pytest or pluggy. Test code, the
    project's helpers and third-party libraries a test calls all count."""
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if name == __file__:
            frame = frame.f_back
            continue
        real = os.path.realpath(name) if not name.startswith("<") else name
        stdlib = real.startswith(_STDLIB) and not (
            real.startswith(_SITE) or os.sep + "site-packages" + os.sep in real
        )
        if name.startswith("<frozen") or stdlib:
            frame = frame.f_back
            continue
        return any(part in real for part in _RUNNER_PARTS)
    return False


def _from_the_import_system() -> bool:
    """A listing or read made by the import system itself (FileFinder caching
    a sys.path entry, a loader reading a module's source, zipimport): that is
    importing, which the import graph owns, not a data read. A test that
    reads a .py file as data (hashing sources, say) is still recorded."""
    caller = sys._getframe(
        2
    )  # 0 = this function, 1 = _hook, 2 = the code that called os.listdir
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
    global _BUSY
    if _BUSY:
        return
    _BUSY = True
    try:
        if event in _SPAWN_EVENTS:
            _SPAWNED.append(f"{event}: {str(args)[:200]}")
            return
        if (
            event
            not in ("open", "os.listdir", "os.scandir", "glob.glob", "glob.glob/2")
            or _from_the_runner()
        ):
            return
        if event in ("os.listdir", "os.scandir", "open") and _from_the_import_system():
            return
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if isinstance(mode, str):
                if any(ch in mode for ch in "wax"):
                    return
            elif isinstance(flags, int) and flags & _WRITE_FLAGS:
                return
            rel = _rel(path)
            if rel is not None:
                _READS.add(rel)
        elif event in ("os.listdir", "os.scandir"):
            path = args[0] if args else "."
            rel = _rel("." if path is None else path)
            if rel is not None:
                _LISTS.add(rel)
        elif event in ("glob.glob", "glob.glob/2"):
            pattern = os.fsdecode(args[0]) if args else ""
            root_dir = args[2] if len(args) > 2 and args[2] is not None else ""
            rel = _rel(os.path.join(os.fsdecode(root_dir), _glob_prefix(pattern)))
            if rel is not None:
                _LISTS.add(rel)
    except Exception:  # noqa: BLE001 - a hook must never break the test it watches
        _INCOMPLETE.append(f"hook error on {event}")
    finally:
        _BUSY = False


if _ROOT and _OUT:
    sys.addaudithook(_hook)


def pytest_collectreport(report):
    if report.failed:
        _INCOMPLETE.append(f"collect:{report.nodeid}")


def pytest_runtest_logreport(report):
    if report.failed or report.skipped:
        _INCOMPLETE.append(f"{report.when}:{report.nodeid}")


def pytest_sessionfinish(session, exitstatus):
    if not (_ROOT and _OUT):
        return
    status = int(exitstatus)
    incomplete = list(_INCOMPLETE)
    if status not in (
        0,
        1,
    ):  # 1 = tests failed (already listed); 2+ = interrupted, usage, nothing collected
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
