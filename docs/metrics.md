# Metric definitions and cardinality rules

Every definition here is also written into each snapshot under `definitions`,
so an old artifact stays interpretable without this repository.

## Metric definitions

Definitions are also written into every artifact under `definitions`, so an old
snapshot stays interpretable without this file.

### The function set — the denominator behind every per-function number

`radon cc -j` returns a flat list per file containing **class, function and
method** blocks. Two traps:

1. Each method appears **twice** — at top level and nested under its class's
   `methods` — and each class block carries a complexity *derived from* its
   methods. Summing the flat list double-counts every method.
2. Closures appear **only** nested by default. `--show-closures` promotes them
   to top level under a qualified name (`https_open.build_conn`) *while also
   leaving them nested*, so recursing with the flag on double-counts closures.

Measured on a real package (24 files, radon 6.0.1), the gap is not marginal:

| Walk | Blocks | Aggregate CC |
|---|---|---|
| Naive sum over the flat list | 102 | 484 |
| **Function set used here** | **92** | **440** |

The function set is: **top-level `function` and `method` blocks, with
`--show-closures` on, excluding `class` blocks, without recursing into
`closures`.** `summary.functions` is its cardinality, and it is the
denominator of `mean`, `high_complexity_fraction` and every bucket count.

### Complexity

| Field | Definition |
|---|---|
| `aggregate` | Sum of cyclomatic complexity over the function set. |
| `mean` | `aggregate / functions`. |
| `p50` / `p90` / `p95` | **Nearest-rank**: `index = ceil(q/100 × n)`, 1-based, clamped to `[1, n]`. |
| `max`, `min` | Extremes over the function set. |
| `functions_gt_10/15/20` | Strictly greater than the threshold. |
| `high_complexity_fraction` | `functions_gt_10 / functions`. |
| `density_per_kloc` | `aggregate / (source_loc / 1000)`. `null` below 200 source lines. |

**Why nearest-rank and not interpolation.** Cyclomatic complexity is a discrete
count. Interpolating between a function of 9 and one of 12 yields 10.8 — a
value no function has, which then moves whenever `n` changes even if no
function changed. Nearest-rank always returns an observed value, keeping a
percentile comparable across runs of different size.

### Lint

`lint.total` counts findings **under an explicitly declared rule selection**,
passed to ruff as `--select` and recorded in `lint.select`. It is never left to
ruff's defaults.

That is not a style preference — it is what makes the number a metric. Caught
on this lane's first CI run: the same commit measured **12 findings under ruff
0.15.8 and 153 under ruff 0.16.4**, because 0.16 widened its default selection
to include UP, I, RUF, BLE, SIM and TRY — and simultaneously dropped E402 from
the defaults, so the *blocking* count moved 12 → 10 as well. An unpinned
dependency float had silently redefined the metric by a factor of twelve and
changed which findings gate the build. With `--select` passed explicitly the
two versions agree exactly (13 findings, 12 blocking).

The default selection is `E4, E7, E9, F, W`: pyflakes, the syntax/runtime-error
subset of pycodestyle, and its warnings. It deliberately excludes E1/E2/E3/E5 —
whitespace and line length — which can produce hundreds of E501 findings and would
swamp the number with a signal about line width rather than code health.

Analyzer versions are **pinned exactly** in the `code-health` extra. `--select`
fixes the rule set; pinning closes the remaining drift, since a rule's
implementation can change between releases. Upgrading is a deliberate act and
appears in the series as a step change with the new version recorded beside it.

Changing `lint_select` makes deltas across the change `incomparable`, for the
same reason a type-checker swap does.

### LOC

From `radon raw`: `loc` is physical lines; `source_loc` is `sloc` — comments
and blank lines excluded. Both are stored; ratios use `source_loc`.

### Maintainability

`maintainability.mean_index` is the **unweighted mean** of radon's per-file MI.
Radon's MI is its own 0–100 rescaling, not the raw Coleman-Oman formula; the
two are not interchangeable across tools. A LOC-weighted variant would be a new
field, never a silent change to this one.

### Deltas

Computed against the previous successful default-branch snapshot.

* An absent baseline yields `null` for every delta — never `0`. Zero means
  "measured, and unchanged".
* `complexity_growth_per_loc = Δaggregate_cc / Δsource_loc`, suppressed when
  `|Δsource_loc| < 25`, where the ratio is dominated by its denominator.
* Deltas across a schema change, a definition change, a type-checker swap or a
  Python-version change are still reported but marked `incomparable` with a
  reason. Suppressing them would hide a real discontinuity in the series.

## Cardinality rules

A metric backend stores one series per distinct combination of name and
attribute values, and keeps it for the retention period whether or not it is
ever written again. The cost of an attribute is not one label — it is a
multiplier on every series it touches, paid forever.

**Permitted metric attributes** (`metrics.ALLOWED_METRIC_ATTRIBUTES`):

| Attribute | Bound |
|---|---|
| `vcs.repository.url.full` | One value per repository. |
| `code.health.ref_class` | Exactly two: `default_branch`, `other`. |
| `code.health.language` | One per analyzed language. |
| `code.health.tool` | One per configured analyzer. |

**Forbidden, and enforced at export time** by `assert_bounded_attributes`,
which raises rather than degrading quietly: commit SHA, branch name, PR id, CI
run id, file path, symbol name, agent run id, model identifier, end-user id.

This is forbidden:

```
code.health.function.complexity{commit_sha="…", file="…", function="…"}
```

### The resource trap

Resource attributes are **not** exempt. Most backends project them onto every
metric the resource produces — Prometheus turns them into target labels.
Putting `vcs.ref.head.revision` on the resource "because it isn't a metric
attribute" reintroduces exactly the explosion the attribute rule prevents.

So there are two resource shapes: `metric_resource_attributes()` (sparse:
`service.name`, `service.namespace`) for metrics, and
`context_resource_attributes()` (full run identity) for logs and traces.

Verified against the real SDK, on the wire (see `tests/code_health/test_otel_wire.py`
and the measurement below): the `/v1/metrics` payload contains **zero**
occurrences of the commit SHA, the branch name, or any file path.

### Where correlation lives instead

Per-commit and per-symbol detail is not lost, only routed. The artifact holds
everything; the `code.health.analysis` event holds the run-level record with
full identity; traces hold timing with full identity. Metrics join to those
through the bounded dimensions plus timestamp.

## Signals

**Metrics** — 20 gauges, listed in `metrics.METRICS`. Gauges rather than
counters: these are levels measured once per run, not monotonic totals, and
summing them across runs is meaningless. A metric whose value is `null` is
**not recorded at all** — a gap says "not measured", a zero says "measured, and
it was zero".

**Events** — one `code.health.analysis` log record per run carrying summary,
complexity, provenance, deltas, tool versions and a bounded hotspot list, plus
one `code.health.symbol` record per hotspot (capped, default 50). The full
symbol table stays in the artifact; a repository with 5,000 functions would
otherwise emit a multi-megabyte record on every push.

**Traces** — a `code_health.analysis` root span with one child per analyzer, so
analyzer duration and failure correlate with results. Adopts `TRACEPARENT` when
CI provides one, nesting under the pipeline's own trace.

## Statistical integrity

* Raw counts are preserved alongside every derived metric, so a future reader
  who disagrees with a definition can recompute from the artifact.
* Tool versions are recorded per analyzer. The version parser skips pyright's
  "a new version is available" notice, which would otherwise be stored *as* the
  version.
* `typing.errors` depends on the resolved Python environment, not only the code:
  the same pyright over the same code reports 49 errors with dependencies
  installed and 53 without, the difference being `reportMissingImports`. So
  `typing.import_errors` and `typing.errors_excluding_imports` are stored
  separately and `typing.environment` records the interpreter.
* Timestamps are UTC, second resolution, `Z`-suffixed.
* No rounding before storage; rounding happens only in `report.py`.
* Missing ≠ zero, and analyzer failure ≠ no findings, everywhere. Each
  analyzer-backed section carries `status ∈ {ok, error, unavailable, skipped}`.
* Schema changes that alter meaning require a `SCHEMA_VERSION` bump; the test
  suite pins the version, the top-level key tuple, and every definition
  parameter, so a silent redefinition fails CI.
