"""Code health, maintainability and provenance telemetry.

The canonical output of a run is a versioned normalized snapshot
(``code-health.json``).  OpenTelemetry is a *transport*, not the data model:
everything exported over OTLP is derived from the snapshot, never the other
way round.  See ``docs/code-health.md`` for metric definitions and the
cardinality rules.
"""

__version__ = "0.1.0"
