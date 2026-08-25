"""Code health, maintainability and provenance telemetry.

The canonical output of a run is a versioned normalized snapshot
(``code-health.json``).  OpenTelemetry is a *transport*, not the data model:
everything exported over OTLP is derived from the snapshot, never the other
way round.  See ``docs/code-health.md`` for metric definitions and the
cardinality rules.
"""

__version__ = "0.1.0"


def collector_identity() -> dict[str, str | None]:
    """Name, version and -- when installed from VCS -- the exact revision.

    ``__version__`` alone does not identify the collector that produced a
    snapshot.  It is a constant in the source tree, so every commit between two
    releases reports the same string: two runs can execute materially different
    normalization or export logic and both claim ``0.1.0``.  That makes a
    measurement unattributable, which is the failure this package exists to
    prevent, so it is worth reading the truth rather than asserting it.

    ``direct_url.json`` (PEP 610) is written by pip for any install from a URL
    and carries ``vcs_info.commit_id`` -- the *resolved* commit, even when the
    requirement named a moving ref.  Recorded as ``revision``; ``None`` for a
    release install from an index, where the version is already exact.
    """
    import json
    from importlib.metadata import PackageNotFoundError, distribution

    version = __version__
    revision: str | None = None
    try:
        dist = distribution("mira-vitals")
        version = dist.version or version
        raw = dist.read_text("direct_url.json")
    except (PackageNotFoundError, OSError):
        # Running from a checkout rather than an install. The source constant
        # is all there is, and saying so honestly beats inventing a revision.
        return {"name": "mira-vitals", "version": version, "revision": None}

    if raw:
        try:
            revision = (json.loads(raw).get("vcs_info") or {}).get("commit_id")
        except (json.JSONDecodeError, AttributeError):
            revision = None
    return {"name": "mira-vitals", "version": version, "revision": revision}
