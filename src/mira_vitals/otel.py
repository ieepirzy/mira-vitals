"""OTLP export: bounded metrics, structured events, and a run trace.

OpenTelemetry is transport here, not the data model.  Everything exported is
read out of the canonical snapshot by dotted path (see
:data:`mira_vitals.metrics.METRICS`), so the metric stream cannot drift
away from the artifact -- there is no second computation to disagree.

Three signals, three jobs:

metrics
    Bounded aggregate levels only.  One gauge per :data:`METRICS` entry, with
    the bounded attribute set and the *sparse* metric resource.  Exported once
    per run through a manual reader and a single force-flush, so a run costs
    one OTLP request rather than one per instrument.
logs / events
    One ``code.health.analysis`` event carrying the run-level record with full
    identity, plus optional per-hotspot events.  Batched, capped, and
    deliberately not "one event per symbol" -- the complete per-symbol record
    lives in the artifact, which is not rate-limited by anyone's ingest quota.
traces
    One span per analyzer under a run root, so analyzer duration and failure
    correlate with the results in the same backend.  Adopts ``TRACEPARENT``
    when the CI job provides one, so these spans nest under the pipeline's own
    trace instead of forming an orphan.

The whole module degrades to a no-op with a recorded status when the OTEL SDK
is absent or the endpoint is unset.  Telemetry export never fails a build; a
broken analyzer does.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from . import metrics as metric_registry
from .metrics import METRICS, CardinalityError

#: Cap on per-hotspot event records.  The artifact keeps every symbol; this
#: bounds what crosses the wire in the log signal.
DEFAULT_MAX_DETAIL_EVENTS = 50


class OTelUnavailable(RuntimeError):
    """The OTEL SDK or an endpoint is not available."""


def _dotted(document: dict[str, Any], path: str) -> Any:
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def endpoint_configured(env: dict[str, str] | None = None) -> bool:
    """Standard OTEL environment variables only -- no proprietary config."""
    env = os.environ if env is None else env
    return bool(
        env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or env.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
        or env.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
        or env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )


def build_metric_points(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve every registered metric against the snapshot.

    A metric whose value is ``None`` is *not recorded at all* rather than
    recorded as zero.  A gap in a time series says "not measured"; a zero says
    "measured, and it was zero", and conflating them would make every analyzer
    outage look like a sudden improvement.
    """
    attributes = metric_registry.metric_attributes(snapshot)
    points = []
    for spec in METRICS:
        value = _dotted(snapshot, spec["path"])
        if value is None or isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            continue
        points.append(
            {
                "name": spec["name"],
                "value": value,
                "unit": spec["unit"],
                "description": spec["description"],
                "attributes": dict(attributes),
            }
        )
    return points


def build_analysis_event(snapshot: dict[str, Any], *, max_hotspots: int = 20) -> dict[str, Any]:
    """The one run-level structured event.

    Carries the summary sections and a bounded hotspot list -- not the full
    symbol table.  A repository with 5,000 functions would otherwise produce a
    multi-megabyte log record on every push, which is a self-inflicted load
    test rather than telemetry.
    """
    run = snapshot["run"]
    return {
        "event.name": "code.health.analysis",
        "schema_version": snapshot["schema_version"],
        "observation_id": run["observation_id"],
        "repository": run["repository"],
        "commit_sha": run["commit_sha"],
        "branch": run["branch"],
        "ref_class": run["ref_class"],
        "canonical": run["canonical"],
        "timestamp": run["timestamp"],
        "ci": snapshot["ci"],
        "definitions": snapshot["definitions"],
        "summary": snapshot["summary"],
        "complexity": snapshot["complexity"],
        "maintainability": snapshot["maintainability"],
        "halstead": snapshot["halstead"],
        "lint": snapshot["lint"],
        "typing": snapshot["typing"],
        "tests": snapshot["tests"],
        "security": snapshot["security"],
        "deltas": snapshot["deltas"],
        "provenance": snapshot["provenance"],
        "tools": snapshot["tools"],
        "hotspots": snapshot["hotspots"][:max_hotspots],
        "counts": {
            "symbols_total": len(snapshot["symbols"]),
            "files_total": len(snapshot["files"]),
            "hotspots_total": len(snapshot["hotspots"]),
        },
    }


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    """Flatten nested JSON into OTLP-compatible scalar attributes.

    OTLP log attributes are scalars or homogeneous arrays; nested maps are not
    portable across collectors and backends.  Lists of objects are serialized
    to JSON strings rather than dropped, so nothing silently disappears.
    """
    import json

    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else key, item, out)
    elif isinstance(value, list):
        if value and all(isinstance(v, (str, int, float, bool)) for v in value):
            out[prefix] = value
        elif value:
            out[prefix] = json.dumps(value, ensure_ascii=False)
    elif value is not None:
        out[prefix] = value


def _emit_event(logger: Any, severity: Any, *, name: str, attributes: dict[str, Any]) -> None:
    """Emit one structured event, across two generations of the OTEL logs API.

    ``Logger.emit`` gained keyword arguments -- including a first-class
    ``event_name`` -- and ``LogRecord`` moved out of ``opentelemetry.sdk._logs``
    into ``opentelemetry._logs`` (absent from ``sdk._logs`` as of SDK 1.44).
    The keyword form is the current sanctioned API and is used first; the
    record form is the fallback for older SDKs.  ``event.name`` is also set as
    an attribute in both paths, because collectors and backends that predate
    the Events API only look there.

    The logs signal is still marked experimental upstream, which is precisely
    why this is pinned to the library's own API rather than to a private
    constructor -- and why the canonical artifact, not the event, is the record
    of truth.
    """
    payload = {"event.name": name, **attributes}
    try:
        logger.emit(
            body=name,
            severity_number=severity,
            attributes=payload,
            event_name=name,
        )
        return
    except TypeError:
        pass
    try:
        from opentelemetry.sdk._logs import LogRecord  # type: ignore[attr-defined]
    except ImportError:
        from opentelemetry._logs import LogRecord  # type: ignore[no-redef]
    logger.emit(LogRecord(body=name, severity_number=severity, attributes=payload))


class Exporter:
    """Owns the OTEL providers for one analysis run."""

    def __init__(self, snapshot_stub: dict[str, Any] | None = None) -> None:
        self.status: dict[str, Any] = {
            "status": "skipped",
            "endpoint_configured": endpoint_configured(),
            "metrics_exported": 0,
            "events_exported": 0,
            "spans_exported": 0,
            "error": None,
        }
        self._tracer = None
        self._tracer_provider = None
        self._snapshot_stub = snapshot_stub or {}

    # -- tracing ---------------------------------------------------------

    def start_tracing(self, resource_attributes: dict[str, str]) -> None:
        """Begin a trace for this analysis run, if the SDK is present."""
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            return
        if not endpoint_configured():
            return
        provider = TracerProvider(resource=Resource.create(dict(resource_attributes)))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        self._tracer_provider = provider
        # Not set as the global provider: this process is a short-lived CI
        # step, and a global would leak into anything else importing OTEL here.
        self._tracer = provider.get_tracer("code-health", "0.1.0")
        _ = trace  # imported for its side-effect-free availability check

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        """A child span, or a no-op when tracing is unavailable."""
        if self._tracer is None:
            yield None
            return
        with self._tracer.start_as_current_span(name) as active:
            for key, value in (attributes or {}).items():
                if value is not None:
                    active.set_attribute(key, value)
            yield active

    def shutdown_tracing(self) -> None:
        if self._tracer_provider is not None:
            self._tracer_provider.shutdown()

    # -- metrics and events ---------------------------------------------

    def export(self, snapshot: dict[str, Any], *, max_detail_events: int = DEFAULT_MAX_DETAIL_EVENTS) -> dict[str, Any]:
        """Export metrics and events for a completed snapshot."""
        if not endpoint_configured():
            self.status.update(status="skipped", error="no OTLP endpoint configured")
            return self.status
        try:
            self._export_metrics(snapshot)
            self._export_events(snapshot, max_detail_events=max_detail_events)
        except CardinalityError:
            # A cardinality violation is a bug in this repository, not a
            # transport failure, and must not be swallowed as one.
            raise
        except Exception as exc:  # noqa: BLE001 - transport failures are non-blocking by policy
            self.status.update(status="error", error=f"{type(exc).__name__}: {exc}")
            return self.status
        self.status["status"] = "ok"
        return self.status

    def _export_metrics(self, snapshot: dict[str, Any]) -> None:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
        except ImportError as exc:
            raise OTelUnavailable(str(exc)) from exc

        points = build_metric_points(snapshot)
        # The sparse resource.  See metrics.py: resource attributes become
        # dimensions in most backends, so run identity must not go here.
        resource = Resource.create(metric_registry.metric_resource_attributes(snapshot))
        # A long interval plus one explicit force_flush: this process exports
        # exactly once and exits, so periodic collection would only add
        # duplicate points for the same observation.
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(), export_interval_millis=60 * 60 * 1000
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = provider.get_meter("code-health", "0.1.0")

        for point in points:
            gauge = meter.create_gauge(point["name"], unit=point["unit"], description=point["description"])
            gauge.set(point["value"], point["attributes"])

        provider.force_flush()
        provider.shutdown()
        self.status["metrics_exported"] = len(points)

    def _export_events(self, snapshot: dict[str, Any], *, max_detail_events: int) -> None:
        try:
            from opentelemetry._logs import SeverityNumber
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.resources import Resource
        except ImportError as exc:
            raise OTelUnavailable(str(exc)) from exc

        # The rich resource: run identity belongs on the log signal.
        resource = Resource.create(metric_registry.context_resource_attributes(snapshot))
        provider = LoggerProvider(resource=resource)
        # Batched, so one run costs one request rather than one per record.
        provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        logger = provider.get_logger("code-health", "0.1.0")

        attributes: dict[str, Any] = {}
        _flatten("", build_analysis_event(snapshot), attributes)
        _emit_event(
            logger,
            SeverityNumber.INFO,
            name="code.health.analysis",
            attributes=attributes,
        )
        exported = 1

        # Per-hotspot detail records, capped.  Symbol-level detail is valuable
        # but is not worth an unbounded number of log records per run; the
        # artifact remains the complete source.
        for hotspot in snapshot["hotspots"][:max_detail_events]:
            detail: dict[str, Any] = {}
            _flatten("", hotspot, detail)
            _emit_event(
                logger,
                SeverityNumber.INFO,
                name="code.health.symbol",
                attributes={"observation_id": snapshot["run"]["observation_id"], **detail},
            )
            exported += 1

        provider.force_flush()
        provider.shutdown()
        self.status["events_exported"] = exported
