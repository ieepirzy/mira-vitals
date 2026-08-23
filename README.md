# mira-vitals

Code health, maintainability and **provenance** telemetry for CI.

Runs the analyzers you already trust, normalizes their output into a versioned
canonical snapshot, and exports a deliberately bounded projection of it over
OTLP. The snapshot — not an OTEL metric — is the record.

```
analyzers ─► normalized model ─┬─► code-health.json   (canonical, complete)
                               └─► OTLP exporter ─► Collector ─┬─► metrics backend
                                                               ├─► logs/events backend
                                                               ├─► trace backend
                                                               └─► durable analytical storage
```

## What makes it different from wiring a linter to a dashboard

**Missing is never zero.** Every analyzer-backed section carries a status
(`ok` / `error` / `unavailable` / `skipped`) and unmeasured values are `null`.
An analyzer that broke and an analyzer that found nothing are different facts,
and a dataset that conflates them will mislead you years later, quietly.

**Metric definitions are pinned and travel with the data.** Percentile method,
thresholds, denominators and the exact function set are written into every
snapshot, so an old artifact stays interpretable without this repository. A
change to any of them marks deltas `incomparable` rather than presenting a
discontinuity as a movement.

**Cardinality is enforced, not documented.** Metric attributes come from a
closed set, checked at export time by a guard that raises. Resource attributes
are held to the same rule, because most backends project them onto every metric
they produce — so the metrics pipeline gets a sparse resource and logs and
traces get full run identity. Commit SHAs, branch names, file paths and symbol
names never reach the metric stream.

**Provenance is explicit or absent.** Authoring mode is never inferred from
commit messages, co-author trailers or author names. Unknown stays `null`,
because backfilling unknowns as `human` would bias every human-vs-agent
comparison in exactly the direction such a study is asking about.

**It measures itself.** A metric definition nobody applies to their own code is
one nobody has checked.

## Install

```bash
pip install "mira-vitals[analyzers,otel]"
```

`analyzers` pulls pinned radon, ruff and pyright. `otel` pulls the OTLP
exporters. The core itself has **no dependencies** — a CI helper whose job is to
report that something is broken should not itself be breakable by dependency
resolution.

## Use

```bash
mira-vitals \
  --output code-health.json \
  --junit reports/junit.xml \
  --coverage reports/coverage.xml \
  --baseline baseline/code-health.json \
  --gate --emit-otlp
```

With no endpoint configured and no baseline available it still writes the
artifact and prints the summary. That is the intended degraded mode.

| Exit | Meaning |
|---|---|
| 0 | Analysis completed, no gate violations |
| 1 | A blocking gate failed |
| 2 | A required analyzer failed or is missing |
| 3 | Telemetry emission failed **and** `--blocking-telemetry` was passed |

Telemetry failure is not a build failure by default. Analyzer failure is. A CI
lane that fails because a metrics endpoint is down teaches people to ignore it.

## Configure

`mira-vitals.toml`, or `[tool.mira_vitals]` in `pyproject.toml`:

```toml
paths          = ["src/myapp"]              # complexity / LOC / maintainability
lint_paths     = ["src/myapp", "tests"]     # measured for lint (usually wider)
lint_gate_paths= ["src/myapp"]              # the subset whose findings BLOCK
lint_select    = ["E4", "E7", "E9", "F", "W"]
lint_blocking  = true
type_checker   = "pyright"                  # or "mypy"
typecheck_blocking = false                  # measure first, gate once clean
```

Measuring is deliberately wider than gating. Adopting this on an existing
codebase should not force an unrelated cleanup: measure everything from day one,
gate once a baseline reaches zero.

### Provenance is configuration, not code

This package knows nothing about your orchestrator or your label scheme, and
hard-coding one vendor's environment variables into a general tool would be
exactly the coupling it exists to avoid. The split:

- **Schema** — which variables carry agent metadata, which labels mean what —
  lives in your config file. It is a property of your conventions: versioned,
  reviewable in a pull request, identical across runs.
- **Values** — the actual run id, model, agent name — arrive as environment
  variables at run time, because only whatever launched the run knows them.
- **Secrets** (OTLP headers, endpoint tokens) go in your CI's secret store;
  non-secret per-repo values (an endpoint URL) in its variables.

```toml
[provenance.labels]
"authoring:human"            = "human"
"authoring:agent-supervised" = "agent_supervised"
"authoring:agent-autonomous" = "agent_autonomous"

[[provenance.agent_sources]]
name = "my-orchestrator"
env  = { run_id = "ORCH_RUN_ID", name = "ORCH_AGENT", model = "ORCH_MODEL",
         authoring_mode = "ORCH_MODE", environment_id = "ORCH_ENV_ID" }
```

A source contributes nothing unless its `run_id` variable is set — a stray
variable on a shared runner is not evidence that an orchestrator launched this
run. Fields the package doesn't recognise are carried through under `extra`, so
your own identifiers survive without it needing to know what they mean.

Its own `MIRA_VITALS_*` variables (`AUTHORING_MODE`, `PR_LABELS`,
`HUMAN_AUTHOR_IDS`, `REVIEW_APPROVALS`, `AGENT_NAME`, …) need no configuration.

## Reusable workflow

```yaml
jobs:
  code-health:
    uses: ieepirzy/mira-vitals/.github/workflows/measure.yml@main
    with:
      paths-artifact: test-reports-py312   # optional: JUnit + coverage
      gate: true
    secrets: inherit
```

## Documentation

- [`docs/metrics.md`](docs/metrics.md) — every metric definition, the function
  set, the percentile method, and the cardinality rules
- [`docs/provenance.md`](docs/provenance.md) — the authoring model and what it
  refuses to guess
- [`docs/endpoint.md`](docs/endpoint.md) — the wire contract for the optional
  document sink

## Licence

MIT. The value in operating this well is in taking care of it for someone, not
in the code being hard to obtain.
