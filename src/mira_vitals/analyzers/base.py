"""Shared plumbing for analyzer adapters."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

#: Nothing here should ever hang a CI job.  Analyzers get a generous but
#: finite budget; exceeding it is recorded as an error, not a zero.
DEFAULT_TIMEOUT_SECONDS = 600


@dataclass
class ToolRun:
    """One analyzer invocation, successful or not.

    ``status`` distinguishes the three failure shapes that a naive integration
    collapses into "0 findings": the tool is not installed (``unavailable``),
    the tool ran and failed (``error``), and the tool was deliberately not run
    (``skipped``).
    """

    name: str
    status: str = "ok"
    version: str | None = None
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    duration_seconds: float | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "version": self.version,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
        }


@dataclass
class Completed:
    """Result of running a subprocess."""

    returncode: int
    stdout: str
    stderr: str
    duration: float


def which(executable: str) -> str | None:
    return shutil.which(executable)


def run(
    command: list[str],
    *,
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Completed:
    """Run a command, capturing both streams.

    stderr is captured and kept, never discarded: a tool that writes a parse
    error to stderr while still exiting 0 is exactly the case a
    ``>/dev/null 2>&1`` integration hides until CI fails.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return Completed(
            returncode=-1,
            stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
            stderr=f"timed out after {timeout}s",
            duration=time.monotonic() - started,
        )
    except OSError as exc:
        return Completed(returncode=-1, stdout="", stderr=str(exc), duration=time.monotonic() - started)
    return Completed(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration=time.monotonic() - started,
    )


#: Lines that are not the version, however first they appear.  pyright prints
#: "WARNING: there is a new pyright version available (v1.1.408 -> v1.1.411)."
#: *before* its version, so naively taking the first line records the upgrade
#: notice as the tool version -- in the one field the whole dataset relies on
#: to keep measurements comparable across time.
_VERSION_NOISE_PREFIXES = ("warning", "note", "notice", "deprecat", "[")

#: A line that plausibly carries a version number.
_VERSION_PATTERN = re.compile(r"\d+\.\d+")


def tool_version(command: list[str]) -> str | None:
    """Best-effort version string for an analyzer.

    Recorded in every snapshot: a metric is only comparable across time if you
    know which build of which tool produced it.  Returns ``None`` rather than a
    guess when no line looks like a version -- an unknown version is a fact,
    a wrong one is corruption.
    """
    result = run(command, timeout=60)
    if result.returncode != 0:
        return None
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    for line in (raw.strip() for raw in text.splitlines()):
        if not line or line.lower().startswith(_VERSION_NOISE_PREFIXES):
            continue
        if _VERSION_PATTERN.search(line):
            return line
    return None
