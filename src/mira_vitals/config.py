"""Per-repository configuration.

A repository declares its own analysis target in a file it owns, rather than in
workflow YAML.  That is what lets the same collector run unchanged across
repositories: CI orchestrates, the repository configures, and neither contains
the data model.

Two sources, in precedence order:

``mira-vitals.toml``
    A standalone file with the settings at the top level.  For repositories
    with no ``pyproject.toml`` -- an application built around
    ``requirements.txt`` rather than a package, where adding a
    ``pyproject.toml`` purely to hold a few settings changes how build and
    packaging tools see the repository, which is a bigger change than it looks.
``pyproject.toml``
    ``[tool.mira_vitals]``, for repositories that already have one.

``code-health.toml`` and ``[tool.code_health]`` are also read, after those:
the first repositories to adopt this collector did so before it was extracted
into a package, and breaking them on rename would be a poor advertisement for a
tool whose entire pitch is not silently redefining things.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 and older
    tomllib = None  # type: ignore[assignment]


@dataclass
class Config:
    #: Paths measured for complexity, LOC and maintainability -- normally the
    #: shipped package, not the test suite: test complexity is a different
    #: quantity and mixing them makes both series uninterpretable.
    paths: list[str] = field(default_factory=lambda: ["."])
    #: Paths linted.  Usually wider than `paths`.
    lint_paths: list[str] = field(default_factory=list)
    #: The subset of lint findings that block the build.  Starts narrow.
    lint_gate_paths: list[str] = field(default_factory=list)
    #: The ruff rule selection, passed as --select.  Explicit on purpose: see
    #: `analyzers/ruff_lint.py`.  Changing it redefines `lint.total`, so it is
    #: recorded in every snapshot and makes deltas across the change
    #: incomparable.
    lint_select: list[str] = field(default_factory=lambda: ["E4", "E7", "E9", "F", "W"])
    lint_ignore: list[str] = field(default_factory=list)
    #: Whether lint blocks at all.  A repository adopting the lane with
    #: pre-existing findings sets this false: the findings are still measured
    #: and trended from day one, and the gate is switched on once the baseline
    #: reaches zero.  Expressing that as an empty `lint_gate_paths` would be
    #: ambiguous with "not configured", which means "gate everything".
    lint_blocking: bool = True
    #: Paths type-checked.
    typecheck_paths: list[str] = field(default_factory=list)
    type_checker: str = "pyright"
    #: Whether the type checker blocks.  Off until a repository's baseline is
    #: at zero; measured from day one either way.
    typecheck_blocking: bool = False
    language: str = "python"
    hotspot_limit: int = 20
    max_detail_events: int = 50
    #: Which file supplied the settings, recorded in the snapshot's target
    #: block so a reader knows what governed the run.
    source: str | None = None

    #: Pull request labels that declare an authoring mode, as
    #: ``{label: mode}``.  Empty by default: applying a label requires write
    #: access, which is what makes it trustworthy, so the vocabulary has to be
    #: the repository's rather than this package's.
    provenance_labels: dict[str, str] = field(default_factory=dict)

    #: Orchestrators whose environment variables carry agent execution
    #: metadata.  Each entry is ``{"name": ..., "env": {field: ENV_VAR}}``.
    #: Empty by default: this package knows nothing about any particular
    #: orchestrator, and hard-coding one vendor's variables into a general tool
    #: would be exactly the coupling it exists to avoid.
    provenance_agent_sources: list[dict[str, Any]] = field(default_factory=list)

    @property
    def effective_lint_paths(self) -> list[str]:
        return self.lint_paths or self.paths

    @property
    def effective_typecheck_paths(self) -> list[str]:
        return self.typecheck_paths or self.paths

    @property
    def effective_lint_gate_paths(self) -> list[str]:
        """Which paths' lint findings block.

        Defaults to the measured paths rather than to *everything* linted: a
        repository that widens `lint_paths` to include its test suite should
        not thereby start gating it.
        """
        return self.lint_gate_paths or self.paths


#: Where configuration may live, most specific first.  The second element of
#: each pair is the path to the settings table inside that file.
CONFIG_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mira-vitals.toml", ()),
    ("pyproject.toml", ("tool", "mira_vitals")),
    ("code-health.toml", ()),
    ("pyproject.toml", ("tool", "code_health")),
)


def _read_table(path: str, keys: tuple[str, ...]) -> dict[str, Any] | None:
    if tomllib is None or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as handle:
            payload: Any = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        # A malformed pyproject.toml is not this tool's problem to report, and
        # falling back to defaults silently would hide a real misconfiguration
        # -- so it is surfaced by the caller failing on the resulting analysis,
        # not swallowed into a wrong config.
        raise
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
        if payload is None:
            return None
    return payload if isinstance(payload, dict) else None


def load(repo_root: str = ".") -> Config:
    """Load repository configuration, falling back to sane defaults.

    An unconfigured repository gets ``paths = ["."]``, which measures and gates
    everything.  That is the safe default: a repository that forgot to
    configure the lane is over-measured, never silently under-measured.
    """
    config = Config()
    for filename, keys in CONFIG_SOURCES:
        section = _read_table(os.path.join(repo_root, filename), keys)
        if section is None:
            continue
        for key, value in section.items():
            if hasattr(config, key):
                setattr(config, key, value)
        # `[provenance]` is nested in the file for readability; flattened here
        # so the rest of the code sees one object.
        provenance = section.get("provenance")
        if isinstance(provenance, dict):
            labels = provenance.get("labels")
            if isinstance(labels, dict):
                config.provenance_labels = {str(k): str(v) for k, v in labels.items()}
            sources = provenance.get("agent_sources")
            if isinstance(sources, list):
                config.provenance_agent_sources = [s for s in sources if isinstance(s, dict)]
        config.source = filename
        break
    return config
