# Endpoint contract


For whoever implements the receiving side. The producer is deliberately
independent of it.

```
POST $CODE_HEALTH_ENDPOINT
Content-Type: application/json
Content-Encoding: gzip
Authorization: Bearer $CODE_HEALTH_TOKEN     (omitted when unset)
Idempotency-Key: <run.observation_id>
User-Agent: code-health/0.1.0

<gzipped canonical snapshot>
```

Expected behaviour:

* **Any 2xx** is success. `202` is appropriate for async ingestion.
* **Deduplicate on `Idempotency-Key`**, which equals `run.observation_id` in the
  body. Repeated delivery of the same analysis is expected — reruns, replayed
  webhooks, a retried workflow — and must not create duplicate rows.
* **4xx other than 429 is not retried** — a malformed payload will be equally
  malformed on the third attempt. Return a body explaining the rejection; it is
  logged (truncated to 500 chars).
* **429 and 5xx are retried**, 3 attempts, backoff 1s → 2s, 15s timeout.
* The client never logs the token, and reads only the response body on error —
  never echoing request headers.
* Store `schema_version` and refuse to silently coerce across versions.

The `files` and `symbols` arrays are unbounded in principle (one entry per file
and per function). For this repository the gzipped payload is a few tens of KB;
a large monorepo will be larger, so size limits should be explicit rather than
discovered.

## Why a document sink as well as OTLP

Raised in review on a typical package#97: the telemetry architecture is standardising on
OTLP plus a Collector, so does the direct `CODE_HEALTH_ENDPOINT` path still
earn its place? The answer this repository is going with, stated so it can be
overruled on the evidence rather than on habit:

**The canonical snapshot is a document, not a telemetry signal.** The two need
different things from their storage:

* **Structure.** OTLP log attributes are scalars or homogeneous arrays. Nested
  structure has to be flattened (`complexity.p95`) or JSON-stringified — which
  is what `otel._flatten` does to `symbols`-shaped data. That round trip is not
  byte-exact, so the log record is a *view* of the snapshot, not the snapshot.
* **Size.** A complete artifact runs tens to low hundreds of KB (single-digit
  KB gzipped) for a package of a few hundred functions, carrying a per-symbol
  record for each. The event path deliberately sends only the run record plus a capped
  hotspot list, precisely so it does not become a per-push megabyte. Pushing
  the whole document through the log signal would undo that.
* **Retention.** Log backends are usually provisioned in weeks. This dataset is
  meant to answer questions years out, and "preserve the complete canonical
  artifact regardless" is the one rule the whole design is built around.

There is also a concrete near-term requirement: the receiving endpoints are
being built now, and the wire contract they implement is specified above under
**Endpoint contract**.

**What would change the answer.** If the Collector routes to durable
object/analytical storage that accepts the document intact — which is squarely
within what a Collector can do — then this path is redundant and should go. It
is deliberately cheap to remove: one module (`emit.py`), one flag
(`--emit-http`), two environment variables, and no schema change. Nothing else
depends on it, and the artifact is written to disk either way.

**It is off unless configured.** With `CODE_HEALTH_ENDPOINT` unset, emission is
skipped and recorded as such. Failure is non-blocking by default.

