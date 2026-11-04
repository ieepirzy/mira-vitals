# CI health

Code health measures the code. CI health measures the pipeline that checks
it: how long a job waited for a runner, how long it ran, where that time
went, and whether the runner was starved while it did.

```
mira-vitals ci start                        # first step: clock + resource baseline
mira-vitals ci mark <phase>                 # optional, between steps
mira-vitals ci report --junit reports/junit.xml --cache uv=true --outcome success
```

or, in a workflow, the composite action
[`.github/actions/ci-health`](../.github/actions/ci-health/action.yml).

## Contract

* **It never fails the build.** Exit status is always 0. Problems become
  `::warning::` annotations and entries in the document's `warnings`. A
  missing Python, a broken install, a refused API or an unreachable
  endpoint costs a measurement, never a green pipeline.
* **It is bounded.** Every request has a timeout and the whole report shares
  one budget (`--budget`, 60 s by default).
* **No platform dependency.** The core needs nothing but the job itself.
  Platform adapters are optional plug-ins that use the job's own
  short-lived token; there is no webhook and no org-level app, so it works
  the same for repositories in any organisation and on any Forgejo.
* **Stdlib only.** The `ci` command imports nothing outside the standard
  library.
* **The document is the record.** `ci-health.json` is written whether or
  not anything is exported; OTLP is transport, as for code health.

## What is measured

| Measurement | Source | Notes |
|---|---|---|
| Queue wait (queued → a runner took it) | adapter | GitHub: per job, from the job's `created_at` (needs satisfied) to `started_at`. Forgejo: per task, from the *run's* creation, an upper bound (`queue_basis: run_created`). |
| Job wall time | adapter, else marks | `wall_basis` says which. |
| Step durations, every job of the run | adapter (GitHub) | Including jobs that do not run this command: the last reporting job of a run sees them all. |
| Phases | `ci mark` | Named intervals between marks, for detail inside a step or on platforms without step timings. |
| Per-test durations and outcomes | JUnit XML | pytest `--junitxml`, vitest's junit reporter, anything with `<testcase time=…>`. |
| Test-time concentration | JUnit XML | p50/p90/p99, how many tests take 80% of the time, the share of the slowest 1% and 10%. |
| Cache hit or miss | `--cache name=hit` or `MIRA_VITALS_CACHE_<NAME>` | Pass `setup-uv`'s `cache-hit`, `actions/cache`'s output, etc. |
| Runner CPU used, CPU throttled, CPU limit | cgroup v2 | Throttling is the "slow suite" that is really a CPU quota. Delta against the `start` baseline. |
| Runner memory peak and limit | cgroup v2 | `memory.peak` vs `memory.max`. |
| Outcome, attempt, run identity | environment | `run_attempt > 1` is a rerun. |

`None` everywhere means *not measured*, never zero.

## Signals

**Metrics** (gauges, one data point per report, bounded attributes only:
workflow, job, step name, phase name, runner labels, outcome, test outcome,
cache name):

`ci.job.duration`, `ci.job.queue.wait`, `ci.step.duration`,
`ci.phase.duration`, `ci.tests.duration`, `ci.tests.count`, `ci.tests.p90`,
`ci.tests.slowest_10pct_share`, `ci.runner.cpu.usage`,
`ci.runner.cpu.throttled`, `ci.runner.cpu.throttled_ratio`,
`ci.runner.cpu.limit`, `ci.runner.memory.peak`, `ci.runner.memory.limit`,
`ci.cache.hit`.

**Logs** (one record each; attribute `event.name` and OTLP `eventName`):

| Event | One per | Key attributes |
|---|---|---|
| `ci.report` | report | job wall/queue and their bases, tests summary, runner sample, caches, warnings |
| `ci.job` | job of the run (adapter) | job id and name, status, conclusion, created/started/completed, queue wait and basis, duration, runner name and labels, `ci.job.self` |
| `ci.step` | completed step (adapter) | job id, step number and name, result, started/completed, duration |
| `ci.phase` | mark interval | phase name, start/end, duration |
| `ci.test` | test case (capped by `--max-tests`, 20 000 by default) | `test.case.id` (file::name), suite, result, duration. Failures are kept first, so a cap only ever drops fast passing tests. |

Every record carries the run identity (`vcs.repository.name` on the
resource; `cicd.pipeline.run.id`, `.attempt`, `.number`,
`vcs.ref.head.revision`, `vcs.ref.head.name`). Several jobs of one run
report the same `ci.job` and `ci.step` rows; the store deduplicates on the
key (below), keeping the newest.

## Export

Standard OpenTelemetry variables only: `OTEL_EXPORTER_OTLP_ENDPOINT` (or the
per-signal `…_METRICS_ENDPOINT` / `…_LOGS_ENDPOINT`) and
`OTEL_EXPORTER_OTLP_HEADERS`. JSON over HTTP, gzipped. Unset endpoint:
export skipped, document still written. The receiver's `partialSuccess`
counts are reported, so a receiver that does not yet store `ci.*` records
says so in the job summary instead of dropping them silently.

Pointing producers at a different receiver (a collector today, an OTel
gateway in front of ClickHouse later) is a change to that one variable.

## ClickHouse schema

Typed tables, fed by materialized views from the OpenTelemetry Collector's
ClickHouse exporter tables (`otel_logs`, whose `LogAttributes` is a
`Map(LowCardinality(String), String)`). A receiver that writes these tables
directly uses the same column names.

```sql
CREATE DATABASE IF NOT EXISTS ci;

-- One row per job per run attempt; the newest report wins.
CREATE TABLE ci.jobs
(
    repository     LowCardinality(String),
    workflow       LowCardinality(String),
    run_id         UInt64,
    run_attempt    UInt16,
    run_number     UInt64,
    sha            FixedString(40),
    ref            String,
    event          LowCardinality(String),
    job_id         UInt64,
    job_name       LowCardinality(String),
    status         LowCardinality(String),
    conclusion     LowCardinality(Nullable(String)),
    run_created_at Nullable(DateTime64(3, 'UTC')),
    created_at     Nullable(DateTime64(3, 'UTC')),
    started_at     Nullable(DateTime64(3, 'UTC')),
    completed_at   Nullable(DateTime64(3, 'UTC')),
    queue_wait_s   Nullable(Float64),
    queue_basis    LowCardinality(String),
    duration_s     Nullable(Float64),
    runner_name    String,
    labels         Array(LowCardinality(String)),
    reported_at    DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(reported_at)
PARTITION BY toYYYYMM(reported_at)
ORDER BY (repository, run_id, run_attempt, job_id);

CREATE TABLE ci.steps
(
    repository   LowCardinality(String),
    workflow     LowCardinality(String),
    run_id       UInt64,
    run_attempt  UInt16,
    job_id       UInt64,
    job_name     LowCardinality(String),
    step_number  UInt16,
    step_name    LowCardinality(String),
    result       LowCardinality(Nullable(String)),
    started_at   DateTime64(3, 'UTC'),
    completed_at DateTime64(3, 'UTC'),
    duration_s   Float64,
    reported_at  DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(reported_at)
PARTITION BY toYYYYMM(started_at)
ORDER BY (repository, run_id, run_attempt, job_id, step_number);

CREATE TABLE ci.tests
(
    repository  LowCardinality(String),
    workflow    LowCardinality(String),
    job_name    LowCardinality(String),
    run_id      UInt64,
    run_attempt UInt16,
    sha         FixedString(40),
    ref         String,
    test_id     String,
    suite       String,
    outcome     LowCardinality(String),
    duration_s  Float64,
    ts          DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ts)
PARTITION BY toYYYYMM(ts)
ORDER BY (repository, test_id, run_id, run_attempt)
TTL toDateTime(ts) + INTERVAL 180 DAY;

CREATE TABLE ci.reports
(
    repository        LowCardinality(String),
    workflow          LowCardinality(String),
    job_name          LowCardinality(String),
    run_id            UInt64,
    run_attempt       UInt16,
    sha               FixedString(40),
    outcome           LowCardinality(Nullable(String)),
    wall_s            Nullable(Float64),
    queue_wait_s      Nullable(Float64),
    cpu_usage_s       Nullable(Float64),
    cpu_throttled_s   Nullable(Float64),
    cpu_limit_cores   Nullable(Float64),
    memory_peak_bytes Nullable(UInt64),
    caches            Map(LowCardinality(String), UInt8),
    tests_count       Nullable(UInt32),
    tests_total_s     Nullable(Float64),
    tests_for_80pct   Nullable(UInt32),
    ts                DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ts)
PARTITION BY toYYYYMM(ts)
ORDER BY (repository, run_id, run_attempt, job_name);

-- A small hand-kept table for cost: what a runner label is worth.
CREATE TABLE ci.runner_classes
(
    label LowCardinality(String),
    vcpu  Float64
)
ENGINE = ReplacingMergeTree
ORDER BY label;
```

The view for `ci.jobs` (the others follow the same pattern on their own
`event.name`):

```sql
CREATE MATERIALIZED VIEW ci.jobs_mv TO ci.jobs AS
SELECT
    ResourceAttributes['vcs.repository.name']                         AS repository,
    LogAttributes['cicd.pipeline.name']                               AS workflow,
    toUInt64OrZero(LogAttributes['cicd.pipeline.run.id'])             AS run_id,
    toUInt16OrZero(LogAttributes['cicd.pipeline.run.attempt'])        AS run_attempt,
    toUInt64OrZero(LogAttributes['cicd.pipeline.run.number'])         AS run_number,
    toFixedString(LogAttributes['vcs.ref.head.revision'], 40)         AS sha,
    LogAttributes['vcs.ref.head.name']                                AS ref,
    LogAttributes['cicd.pipeline.trigger']                            AS event,
    toUInt64OrZero(LogAttributes['cicd.pipeline.task.run.id'])        AS job_id,
    LogAttributes['cicd.pipeline.task.name']                          AS job_name,
    LogAttributes['ci.job.status']                                    AS status,
    nullIf(LogAttributes['cicd.pipeline.task.run.result'], '')        AS conclusion,
    toDateTime64(toFloat64OrNull(LogAttributes['ci.run.created_at']), 3, 'UTC') AS run_created_at,
    toDateTime64(toFloat64OrNull(LogAttributes['ci.job.created_at']), 3, 'UTC')   AS created_at,
    toDateTime64(toFloat64OrNull(LogAttributes['ci.job.started_at']), 3, 'UTC')   AS started_at,
    toDateTime64(toFloat64OrNull(LogAttributes['ci.job.completed_at']), 3, 'UTC') AS completed_at,
    toFloat64OrNull(LogAttributes['ci.job.queue.wait'])               AS queue_wait_s,
    LogAttributes['ci.job.queue.basis']                               AS queue_basis,
    toFloat64OrNull(LogAttributes['ci.job.duration'])                 AS duration_s,
    LogAttributes['cicd.worker.name']                                 AS runner_name,
    JSONExtract(ifNull(nullIf(LogAttributes['cicd.worker.labels'], ''), '[]'), 'Array(String)') AS labels,
    Timestamp                                                         AS reported_at
FROM otel.otel_logs
WHERE LogAttributes['event.name'] = 'ci.job';
```

## Queries

Read with `FINAL` (or `argMax` by `reported_at`), because one job is
reported by every later job of its run.

**Duration and queue-wait percentiles per job, last 30 days**

```sql
SELECT repository, workflow, job_name,
       count() AS runs,
       quantiles(0.5, 0.9, 0.99)(duration_s)   AS duration_p50_p90_p99,
       quantiles(0.5, 0.9, 0.99)(queue_wait_s) AS queue_p50_p90_p99
FROM ci.jobs FINAL
WHERE started_at >= now() - INTERVAL 30 DAY AND conclusion IS NOT NULL
GROUP BY repository, workflow, job_name
ORDER BY quantile(0.9)(duration_s) + quantile(0.9)(queue_wait_s) DESC;
```

**Queue wait by runner label and hour of day** (runner shortage)

```sql
SELECT arrayJoin(labels) AS label, toHour(created_at) AS hour_utc,
       count() AS jobs, quantile(0.5)(queue_wait_s) AS p50, quantile(0.9)(queue_wait_s) AS p90
FROM ci.jobs FINAL
WHERE created_at >= now() - INTERVAL 14 DAY AND queue_basis = 'job_created'
GROUP BY label, hour_utc
ORDER BY label, hour_utc;
```

**Slowest steps**

```sql
SELECT repository, job_name, step_name, count() AS n,
       quantile(0.5)(duration_s) AS p50, quantile(0.9)(duration_s) AS p90, sum(duration_s) / 3600 AS hours
FROM ci.steps FINAL
WHERE started_at >= now() - INTERVAL 30 DAY
GROUP BY repository, job_name, step_name
ORDER BY hours DESC
LIMIT 50;
```

**Slowest tests, and tests getting slower** (last 7 days against the 28
before)

```sql
SELECT repository, test_id,
       avgIf(duration_s, ts >= now() - INTERVAL 7 DAY)                                AS recent_s,
       avgIf(duration_s, ts <  now() - INTERVAL 7 DAY)                                AS before_s,
       recent_s / nullIf(before_s, 0)                                                 AS ratio,
       countIf(ts >= now() - INTERVAL 7 DAY)                                          AS recent_runs
FROM ci.tests FINAL
WHERE ts >= now() - INTERVAL 35 DAY AND outcome = 'passed'
GROUP BY repository, test_id
HAVING recent_runs >= 3
ORDER BY recent_s DESC
LIMIT 100;
-- ORDER BY ratio DESC (with before_s > 0.5) for the ones trending slower.
```

**Flaky tests: failed and passed on the same commit**

```sql
SELECT repository, test_id, count() AS flaky_commits
FROM
(
    SELECT repository, test_id, sha
    FROM ci.tests FINAL
    WHERE ts >= now() - INTERVAL 30 DAY
    GROUP BY repository, test_id, sha
    HAVING countIf(outcome IN ('failed', 'error')) > 0 AND countIf(outcome = 'passed') > 0
)
GROUP BY repository, test_id
ORDER BY flaky_commits DESC;
```

**Where a run's time went: the last job to finish, broken down**

The job that completes last decides a run's wall time. Its life splits into
waiting for upstream jobs (`run_created_at → created_at`), waiting for a
runner (`created_at → started_at`) and running.

```sql
SELECT repository, run_id, run_attempt,
       argMax(job_name, completed_at)                                        AS last_job,
       dateDiff('second', min(run_created_at), max(completed_at))            AS run_wall_s,
       argMax(dateDiff('second', run_created_at, created_at), completed_at)  AS upstream_s,
       argMax(queue_wait_s, completed_at)                                    AS last_job_queue_s,
       argMax(duration_s, completed_at)                                      AS last_job_run_s,
       sum(queue_wait_s)                                                     AS all_jobs_queue_s
FROM ci.jobs FINAL
WHERE started_at >= now() - INTERVAL 7 DAY AND completed_at IS NOT NULL
GROUP BY repository, run_id, run_attempt
ORDER BY run_wall_s DESC
LIMIT 50;
```

**Idle gaps between dependent jobs** (from one job's end to the next job's
start, in the same run)

```sql
SELECT repository, run_id, job_name,
       dateDiff('second',
                max(completed_at) OVER (PARTITION BY repository, run_id, run_attempt ORDER BY started_at
                                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
                started_at) AS gap_after_previous_s
FROM ci.jobs FINAL
WHERE started_at >= now() - INTERVAL 7 DAY
ORDER BY gap_after_previous_s DESC
LIMIT 50;
```

**Runner-minutes × vCPU (cost)**

```sql
SELECT j.repository, j.workflow,
       sum(j.duration_s) / 60                       AS runner_minutes,
       sum(j.duration_s / 60 * coalesce(r.vcpu, 1)) AS vcpu_minutes
FROM (SELECT * FROM ci.jobs FINAL WHERE started_at >= now() - INTERVAL 30 DAY) AS j
LEFT JOIN ci.runner_classes AS r ON r.label = j.labels[1]
GROUP BY j.repository, j.workflow
ORDER BY vcpu_minutes DESC;
```

**Regression signal: a job's p90 this week against its own baseline**

```sql
SELECT repository, workflow, job_name,
       quantileIf(0.9)(duration_s, started_at >= now() - INTERVAL 7 DAY) AS p90_recent,
       quantileIf(0.9)(duration_s, started_at <  now() - INTERVAL 7 DAY) AS p90_baseline,
       p90_recent / nullIf(p90_baseline, 0)                               AS ratio,
       countIf(started_at >= now() - INTERVAL 7 DAY)                      AS n_recent
FROM ci.jobs FINAL
WHERE started_at >= now() - INTERVAL 35 DAY AND conclusion = 'success'
GROUP BY repository, workflow, job_name
HAVING n_recent >= 5 AND ratio > 1.25
ORDER BY ratio DESC;
```

**Runner starvation: CPU throttling and memory headroom**

```sql
SELECT repository, job_name,
       quantile(0.9)(cpu_throttled_s / nullIf(wall_s, 0))           AS throttled_share_p90,
       quantile(0.5)(cpu_usage_s / nullIf(wall_s, 0))               AS cores_used_p50,
       any(cpu_limit_cores)                                         AS limit_cores,
       quantile(0.9)(memory_peak_bytes) / 1073741824                AS mem_peak_gib_p90
FROM ci.reports FINAL
WHERE ts >= now() - INTERVAL 14 DAY
GROUP BY repository, job_name
ORDER BY throttled_share_p90 DESC;
```

## Later phases (not built)

* **Runner start-up on the cluster.** For self-hosted runners on
  Kubernetes (ARC), the queue wait above includes pod scheduling, image
  pull and node autoscaling. Split it from the cluster's own metrics
  (kube-state-metrics pod timestamps, kubelet image-pull durations,
  cluster-autoscaler scale-up events), joined on the runner name the job
  records.
* **Merge → production lead time.** From the merge commit's push to the
  deployment that carried it: main CI, image publish, promotion, rollout.
  Needs the deploy side to report the commit it rolled out.
