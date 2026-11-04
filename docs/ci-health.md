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

### How well the optimizations work

CI is what lets us ship fast and stay safe, so the record measures each
optimization, and its safety, and not only the timings:

| Measurement | Source | Notes |
|---|---|---|
| Test selection: mode (`full` / `selected`), the full-run triggers that fired (`conftest`, `migrations`, `deps`, `ci`, `non-py`, `deleted-py`, `conftest-closure`), selected / total tests, estimated time saved, selector name and version, graph source with ruff and Lomitus versions, Merkle closure cache hits | `selection.json` | Written by the coming `mira-vitals ci select`. Selector version is the before/after boundary. |
| Escapes: a test that failed on the default branch in a full run but did not run in the merged PR's selected run | derived (`ci.escapes`) | Joined on PR number, merge sha and test id. The escape rate is the gate for trusting selection. |
| Parallelism: xdist worker count, per-worker wall time, imbalance (max/mean), per-worker DB isolation | `workers.json` or `--workers N` | |
| Caches: uv/npm, docker layers, test-result cache | `--cache name=hit` | Any name; one map. |
| Flakiness: failed and passed on the same commit, first and last seen, quarantine status | derived (`ci.flaky_tests`); quarantine from `quarantine.json` | |
| PR linkage: the PR a run belongs to, its base sha, and on the default branch the PR a commit merged | event payload; GitHub adapter (`commits/{sha}/pulls`) | The join keys for escapes and per-PR cost. |
| Outcome safety: red main after a green PR, reverts, rollbacks after deploy | `ci.outcomes` (reserved), `ci.red_main_after_green_pr` (derived now) | Producers for reverts and rollbacks come later. |

File contracts (all optional, in the job's state directory or passed with
`--selection` / `--workers-file` / `--quarantine`; a bad file is a warning):

```json
// selection.json
{"mode": "selected", "triggers": [], "selected": 812, "total": 2629,
 "estimated_saved_seconds": 940.0,
 "selector": {"name": "mira-vitals-select", "version": "0.2.0"},
 "graph": {"source": "ruff", "ruff_version": "0.16.4", "lomitus_version": "1.3.0"},
 "closure_cache_hits": 1500}

// workers.json
{"workers": [{"id": "gw0", "wall_seconds": 301.2, "tests": 655, "db_isolated": true}]}

// quarantine.json
{"test_ids": ["tests/test_x.py::test_flaky"]}
```

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
    event       LowCardinality(String),
    pr_number   Nullable(UInt32),   -- the PR this run belongs to (PR runs)
    merged_pr   Nullable(UInt32),   -- the PR this commit merged (default-branch runs)
    selection_mode LowCardinality(Nullable(String)),  -- 'full' | 'selected' | NULL (no selector)
    test_id     String,
    suite       String,
    outcome     LowCardinality(String),
    duration_s  Float64,
    quarantined UInt8,
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
    -- Every cache by name: uv, npm, docker (layers), test-results, ...
    caches            Map(LowCardinality(String), UInt8),
    tests_count       Nullable(UInt32),
    tests_total_s     Nullable(Float64),
    tests_for_80pct   Nullable(UInt32),

    -- PR linkage: the join keys for escapes and per-PR cost.
    event             LowCardinality(String),
    ref               String,
    pr_number         Nullable(UInt32),
    base_sha          Nullable(FixedString(40)),
    merged_pr         Nullable(UInt32),

    -- Test selection (mira-vitals ci select; NULL where no selector ran).
    selection_mode         LowCardinality(Nullable(String)),
    selection_triggers     Array(LowCardinality(String)),   -- why the run was full
    tests_selected         Nullable(UInt32),
    tests_available        Nullable(UInt32),
    selection_saved_est_s  Nullable(Float64),
    selector_name          LowCardinality(Nullable(String)),
    selector_version       LowCardinality(Nullable(String)),
    graph_source           LowCardinality(Nullable(String)),
    ruff_version           LowCardinality(Nullable(String)),
    lomitus_version        LowCardinality(Nullable(String)),
    closure_cache_hits     Nullable(UInt32),   -- tests skipped as unchanged (Merkle closure)

    -- Parallelism (pytest-xdist).
    xdist_workers        Nullable(UInt16),
    xdist_wall_max_s     Nullable(Float64),
    xdist_imbalance      Nullable(Float64),   -- max / mean worker wall time
    xdist_db_isolated    LowCardinality(Nullable(String)),   -- 'true' | 'false' | 'mixed'

    ts                DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ts)
PARTITION BY toYYYYMM(ts)
ORDER BY (repository, run_id, run_attempt, job_name);

-- One row per xdist worker per job.
CREATE TABLE ci.workers
(
    repository  LowCardinality(String),
    run_id      UInt64,
    run_attempt UInt16,
    job_name    LowCardinality(String),
    worker      LowCardinality(String),
    wall_s      Nullable(Float64),
    tests       Nullable(UInt32),
    db_isolated Nullable(UInt8),
    ts          DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ts)
PARTITION BY toYYYYMM(ts)
ORDER BY (repository, run_id, run_attempt, job_name, worker);

-- Outcome safety. Reserved now, filled by later producers (a deploy
-- controller for rollbacks, a commit scanner for reverts); the
-- red-main-after-green-PR kind can also be derived (view below).
CREATE TABLE ci.outcomes
(
    repository  LowCardinality(String),
    kind        LowCardinality(String),   -- 'red_main_after_green_pr' | 'revert' | 'rollback'
    sha         FixedString(40),          -- the commit that went bad / was reverted / rolled back
    pr_number   Nullable(UInt32),
    related_sha Nullable(FixedString(40)),   -- the revert commit, or the deploy it rolled back to
    environment LowCardinality(Nullable(String)),   -- rollbacks: which deployment
    source      LowCardinality(String),   -- who reported it
    detected_at DateTime64(3, 'UTC'),
    details     String
)
ENGINE = ReplacingMergeTree(detected_at)
PARTITION BY toYYYYMM(detected_at)
ORDER BY (repository, kind, sha);

-- Flakiness: a test that failed and passed on the same commit. One row per
-- (repository, test, commit) with outcome counts, kept by a view on ci.tests.
CREATE TABLE ci.test_commit_outcomes
(
    repository LowCardinality(String),
    test_id    String,
    sha        FixedString(40),
    fails      SimpleAggregateFunction(sum, UInt64),
    passes     SimpleAggregateFunction(sum, UInt64),
    first_ts   SimpleAggregateFunction(min, DateTime64(3, 'UTC')),
    last_ts    SimpleAggregateFunction(max, DateTime64(3, 'UTC'))
)
ENGINE = AggregatingMergeTree
ORDER BY (repository, test_id, sha);

CREATE MATERIALIZED VIEW ci.test_commit_outcomes_mv TO ci.test_commit_outcomes AS
SELECT repository, test_id, sha,
       toUInt64(outcome IN ('failed', 'error')) AS fails,
       toUInt64(outcome = 'passed')             AS passes,
       ts AS first_ts, ts AS last_ts
FROM ci.tests;

-- Flaky tests with first and last seen, and whether they are quarantined now.
CREATE VIEW ci.flaky_tests AS
SELECT o.repository AS repository, o.test_id AS test_id,
       count()             AS flaky_commits,
       min(o.commit_first) AS first_seen,
       max(o.commit_last)  AS last_seen,
       any(q.quarantined)  AS quarantined_now
FROM
(
    SELECT repository, test_id, sha, sum(fails) AS n_fail, sum(passes) AS n_pass,
           min(first_ts) AS commit_first, max(last_ts) AS commit_last
    FROM ci.test_commit_outcomes
    GROUP BY repository, test_id, sha
) AS o
LEFT JOIN
(
    SELECT repository, test_id, argMax(quarantined, ts) AS quarantined
    FROM ci.tests
    GROUP BY repository, test_id
) AS q ON q.repository = o.repository AND q.test_id = o.test_id
WHERE o.n_fail > 0 AND o.n_pass > 0
GROUP BY o.repository, o.test_id;

-- Escapes, the safety metric for test selection: a test that failed on the
-- default branch in a FULL run, for the commit that merged PR N, but did not
-- run at all in PR N's last run because that run was a SELECTION.
CREATE VIEW ci.escapes AS
WITH
    main_failures AS
    (
        SELECT repository, merged_pr AS pr, sha AS merge_sha, test_id, min(ts) AS failed_at
        FROM ci.tests FINAL
        WHERE merged_pr IS NOT NULL
          AND outcome IN ('failed', 'error')
          AND quarantined = 0
          AND (selection_mode = 'full' OR selection_mode IS NULL)
        GROUP BY repository, pr, merge_sha, test_id
    ),
    pr_last_run AS
    (
        SELECT repository, pr_number AS pr,
               argMax(run_id, ts) AS last_run_id, argMax(selection_mode, ts) AS mode,
               argMax(selector_version, ts) AS selector_version
        FROM ci.reports FINAL
        WHERE pr_number IS NOT NULL AND tests_count IS NOT NULL
        GROUP BY repository, pr
    ),
    pr_ran AS
    (
        SELECT DISTINCT t.repository AS repository, t.pr_number AS pr, t.test_id AS test_id
        FROM ci.tests AS t FINAL
        INNER JOIN pr_last_run AS l
            ON l.repository = t.repository AND l.pr = t.pr_number AND l.last_run_id = t.run_id
    )
SELECT f.repository AS repository, f.pr AS pr, f.merge_sha AS merge_sha, f.test_id AS test_id,
       f.failed_at AS failed_at, l.selector_version AS selector_version
FROM main_failures AS f
INNER JOIN pr_last_run AS l ON l.repository = f.repository AND l.pr = f.pr
LEFT ANTI JOIN pr_ran AS r ON r.repository = f.repository AND r.pr = f.pr AND r.test_id = f.test_id
WHERE l.mode = 'selected';

-- Red main after a green PR, derived until a producer writes ci.outcomes:
-- the merge commit's default-branch run failed, the PR's last run passed.
CREATE VIEW ci.red_main_after_green_pr AS
SELECT m.repository AS repository, m.merged_pr AS pr, m.sha AS merge_sha, m.last_ts AS failed_at
FROM
(
    SELECT repository, merged_pr, sha, max(ts) AS last_ts, argMax(outcome, ts) AS last_outcome
    FROM ci.reports FINAL
    WHERE merged_pr IS NOT NULL
    GROUP BY repository, merged_pr, sha
) AS m
INNER JOIN
(
    SELECT repository, pr_number, argMax(outcome, ts) AS last_outcome
    FROM ci.reports FINAL
    WHERE pr_number IS NOT NULL
    GROUP BY repository, pr_number
) AS p ON p.repository = m.repository AND p.pr_number = m.merged_pr
WHERE m.last_outcome = 'failure' AND p.last_outcome = 'success';

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

The views for the job's own record, its tests and its workers:

```sql
CREATE MATERIALIZED VIEW ci.reports_mv TO ci.reports AS
WITH LogAttributes AS a,
     a['ci.selection']   AS sel,
     a['ci.parallelism'] AS par,
     a['ci.runner']      AS rn,
     a['ci.tests.summary'] AS ts_
SELECT
    ResourceAttributes['vcs.repository.name']                       AS repository,
    a['cicd.pipeline.name']                                         AS workflow,
    a['ci.reporter.job']                                            AS job_name,
    toUInt64OrZero(a['cicd.pipeline.run.id'])                       AS run_id,
    toUInt16OrZero(a['cicd.pipeline.run.attempt'])                  AS run_attempt,
    toFixedString(a['vcs.ref.head.revision'], 40)                   AS sha,
    nullIf(a['cicd.pipeline.result'], '')                           AS outcome,
    toFloat64OrNull(a['ci.job.duration'])                           AS wall_s,
    toFloat64OrNull(a['ci.job.queue.wait'])                         AS queue_wait_s,
    JSONExtract(rn, 'cpu_usage_usec', 'Nullable(Float64)') / 1e6    AS cpu_usage_s,
    JSONExtract(rn, 'cpu_throttled_usec', 'Nullable(Float64)') / 1e6 AS cpu_throttled_s,
    JSONExtract(rn, 'cpu_limit_cores', 'Nullable(Float64)')         AS cpu_limit_cores,
    JSONExtract(rn, 'memory_peak_bytes', 'Nullable(UInt64)')        AS memory_peak_bytes,
    CAST(JSONExtract(ifNull(nullIf(a['ci.caches'], ''), '{}'), 'Map(String, Bool)'), 'Map(LowCardinality(String), UInt8)') AS caches,
    JSONExtract(ts_, 'count', 'Nullable(UInt32)')                   AS tests_count,
    JSONExtract(ts_, 'total_seconds', 'Nullable(Float64)')          AS tests_total_s,
    JSONExtract(ts_, 'tests_for_80pct_time', 'Nullable(UInt32)')    AS tests_for_80pct,
    a['cicd.pipeline.trigger']                                      AS event,
    a['vcs.ref.head.name']                                          AS ref,
    toUInt32OrNull(a['vcs.change.id'])                              AS pr_number,
    if(a['vcs.ref.base.revision'] = '', NULL, toFixedString(a['vcs.ref.base.revision'], 40)) AS base_sha,
    toUInt32OrNull(a['ci.merged_change.id'])                        AS merged_pr,
    JSONExtract(sel, 'mode', 'Nullable(String)')                    AS selection_mode,
    JSONExtract(sel, 'triggers', 'Array(String)')                   AS selection_triggers,
    JSONExtract(sel, 'selected', 'Nullable(UInt32)')                AS tests_selected,
    JSONExtract(sel, 'total', 'Nullable(UInt32)')                   AS tests_available,
    JSONExtract(sel, 'estimated_saved_seconds', 'Nullable(Float64)') AS selection_saved_est_s,
    JSONExtract(sel, 'selector_name', 'Nullable(String)')           AS selector_name,
    JSONExtract(sel, 'selector_version', 'Nullable(String)')        AS selector_version,
    JSONExtract(sel, 'graph_source', 'Nullable(String)')            AS graph_source,
    JSONExtract(sel, 'ruff_version', 'Nullable(String)')            AS ruff_version,
    JSONExtract(sel, 'lomitus_version', 'Nullable(String)')         AS lomitus_version,
    JSONExtract(sel, 'closure_cache_hits', 'Nullable(UInt32)')      AS closure_cache_hits,
    JSONExtract(par, 'count', 'Nullable(UInt16)')                   AS xdist_workers,
    JSONExtract(par, 'wall_max_seconds', 'Nullable(Float64)')       AS xdist_wall_max_s,
    JSONExtract(par, 'imbalance', 'Nullable(Float64)')              AS xdist_imbalance,
    nullIf(JSONExtractRaw(par, 'db_isolated'), '')                  AS xdist_db_isolated,
    Timestamp                                                       AS ts
FROM otel.otel_logs
WHERE LogAttributes['event.name'] = 'ci.report';

CREATE MATERIALIZED VIEW ci.workers_mv TO ci.workers AS
SELECT
    ResourceAttributes['vcs.repository.name']                      AS repository,
    toUInt64OrZero(LogAttributes['cicd.pipeline.run.id'])          AS run_id,
    toUInt16OrZero(LogAttributes['cicd.pipeline.run.attempt'])     AS run_attempt,
    LogAttributes['ci.reporter.job']                               AS job_name,
    JSONExtractString(w, 'id')                                     AS worker,
    JSONExtract(w, 'wall_seconds', 'Nullable(Float64)')            AS wall_s,
    JSONExtract(w, 'tests', 'Nullable(UInt32)')                    AS tests,
    JSONExtract(w, 'db_isolated', 'Nullable(UInt8)')               AS db_isolated,
    Timestamp                                                      AS ts
FROM otel.otel_logs
ARRAY JOIN JSONExtractArrayRaw(LogAttributes['ci.parallelism'], 'workers') AS w
WHERE LogAttributes['event.name'] = 'ci.report';

CREATE MATERIALIZED VIEW ci.tests_mv TO ci.tests AS
SELECT
    ResourceAttributes['vcs.repository.name']                      AS repository,
    LogAttributes['cicd.pipeline.name']                            AS workflow,
    LogAttributes['cicd.pipeline.task.name']                       AS job_name,
    toUInt64OrZero(LogAttributes['cicd.pipeline.run.id'])          AS run_id,
    toUInt16OrZero(LogAttributes['cicd.pipeline.run.attempt'])     AS run_attempt,
    toFixedString(LogAttributes['vcs.ref.head.revision'], 40)      AS sha,
    LogAttributes['vcs.ref.head.name']                             AS ref,
    LogAttributes['cicd.pipeline.trigger']                         AS event,
    toUInt32OrNull(LogAttributes['vcs.change.id'])                 AS pr_number,
    toUInt32OrNull(LogAttributes['ci.merged_change.id'])           AS merged_pr,
    nullIf(LogAttributes['ci.selection.mode'], '')                 AS selection_mode,
    LogAttributes['test.case.id']                                  AS test_id,
    LogAttributes['test.suite.name']                               AS suite,
    LogAttributes['test.case.result.status']                       AS outcome,
    toFloat64OrZero(LogAttributes['test.case.duration'])           AS duration_s,
    toUInt8(LogAttributes['test.case.quarantined'] = 'true')       AS quarantined,
    Timestamp                                                      AS ts
FROM otel.otel_logs
WHERE LogAttributes['event.name'] = 'ci.test';
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

## Optimization efficacy

The question the CI exists to answer: did an optimization make merging
faster *without* making it less safe? Each query compares like with like:
before/after a selector version or a worker count, per merged PR, with the
safety metrics (escapes, flakes, red main) beside the speed ones.

**Before/after a selector version** (speed and safety side by side)

```sql
SELECT r.repository AS repository, r.selector AS selector, r.runs, r.wall_p50, r.wall_p90,
       r.selected_share, r.est_hours_saved, r.full_runs,
       ifNull(e.escapes, 0) AS escapes
FROM
(
    SELECT repository, ifNull(selector_version, 'none') AS selector,
           count()                                          AS runs,
           quantile(0.5)(wall_s)                            AS wall_p50,
           quantile(0.9)(wall_s)                            AS wall_p90,
           avg(tests_selected / nullIf(tests_available, 0)) AS selected_share,
           sum(selection_saved_est_s) / 3600                AS est_hours_saved,
           countIf(selection_mode = 'full')                 AS full_runs
    FROM ci.reports FINAL
    WHERE ts >= now() - INTERVAL 90 DAY AND pr_number IS NOT NULL AND tests_count IS NOT NULL
    GROUP BY repository, selector
) AS r
LEFT JOIN
(
    SELECT repository, ifNull(selector_version, 'none') AS selector, count() AS escapes
    FROM ci.escapes
    GROUP BY repository, selector
) AS e ON e.repository = r.repository AND e.selector = r.selector
ORDER BY repository, selector;
```

**Which full-run triggers fire, and what they cost**

```sql
SELECT repository, arrayJoin(selection_triggers) AS trigger,
       count() AS runs, quantile(0.5)(wall_s) AS wall_p50
FROM ci.reports FINAL
WHERE ts >= now() - INTERVAL 30 DAY AND selection_mode = 'full'
GROUP BY repository, trigger
ORDER BY runs DESC;
```

**Before/after an xdist worker count**

```sql
SELECT repository, job_name, ifNull(xdist_workers, 1) AS workers,
       count()                               AS runs,
       quantile(0.5)(wall_s)                 AS wall_p50,
       quantile(0.9)(wall_s)                 AS wall_p90,
       quantile(0.5)(cpu_usage_s) / 60       AS cpu_minutes_p50,
       quantile(0.9)(xdist_imbalance)        AS imbalance_p90,
       quantile(0.9)(cpu_throttled_s / nullIf(wall_s, 0)) AS throttled_share_p90,
       groupUniqArray(xdist_db_isolated)     AS db_isolation
FROM ci.reports FINAL
WHERE ts >= now() - INTERVAL 90 DAY AND tests_count IS NOT NULL
GROUP BY repository, job_name, workers
ORDER BY repository, job_name, workers;
```

**Wall time and CPU-minutes per merged PR** (every run of the PR plus its
merge run; the unit the business cares about)

```sql
WITH per_pr AS
(
    SELECT repository, coalesce(pr_number, merged_pr) AS pr,
           max(ts)                                    AS last_ts,
           sum(wall_s) / 60                           AS job_minutes,
           sum(cpu_usage_s) / 60                      AS cpu_minutes,
           uniqExact(run_id, run_attempt)             AS runs
    FROM ci.reports FINAL
    WHERE coalesce(pr_number, merged_pr) IS NOT NULL
    GROUP BY repository, pr
    HAVING countIf(merged_pr IS NOT NULL) > 0   -- merged PRs only
)
SELECT repository, toStartOfWeek(last_ts) AS week, count() AS merged_prs,
       quantiles(0.5, 0.9)(job_minutes) AS job_minutes_p50_p90,
       quantiles(0.5, 0.9)(cpu_minutes) AS cpu_minutes_p50_p90,
       avg(runs)                        AS runs_per_pr
FROM per_pr
GROUP BY repository, week
ORDER BY repository, week;
```

**Escape rate over time** (the gate for trusting selection)

```sql
SELECT r.repository AS repository, toStartOfWeek(r.ts) AS week,
       uniqExact(r.merged_pr)                                         AS merged_prs,
       uniqExactIf(r.merged_pr, e.pr IS NOT NULL)                     AS prs_with_escape,
       prs_with_escape / nullIf(merged_prs, 0)                        AS escape_rate
FROM ci.reports AS r FINAL
LEFT JOIN (SELECT DISTINCT repository, pr FROM ci.escapes) AS e
    ON e.repository = r.repository AND e.pr = r.merged_pr
WHERE r.merged_pr IS NOT NULL AND r.ts >= now() - INTERVAL 180 DAY
GROUP BY repository, week
ORDER BY repository, week;
```

**Flake rate** (commits on which a test both failed and passed, per test
run, quarantined tests excluded)

```sql
SELECT o.repository AS repository, toStartOfWeek(o.commit_first) AS week,
       countIf(o.n_fail > 0 AND o.n_pass > 0) AS flaky_test_commits,
       count()                                AS test_commits,
       flaky_test_commits / test_commits      AS flake_rate
FROM
(
    SELECT repository, test_id, sha, sum(fails) AS n_fail, sum(passes) AS n_pass, min(first_ts) AS commit_first
    FROM ci.test_commit_outcomes
    GROUP BY repository, test_id, sha
) AS o
LEFT JOIN (SELECT DISTINCT repository, test_id FROM ci.tests WHERE quarantined = 1) AS q
    ON q.repository = o.repository AND q.test_id = o.test_id
WHERE q.test_id = ''   -- not quarantined (LEFT JOIN default)
GROUP BY repository, week
ORDER BY repository, week;
```

**Outcome safety per week** (red main after a green PR, reverts,
rollbacks)

```sql
SELECT repository, toStartOfWeek(detected_at) AS week, kind, count() AS n
FROM
(
    SELECT repository, kind, detected_at FROM ci.outcomes FINAL
    UNION ALL
    SELECT repository, 'red_main_after_green_pr' AS kind, failed_at AS detected_at
    FROM ci.red_main_after_green_pr
)
GROUP BY repository, week, kind
ORDER BY repository, week, kind;
```

**Cache efficacy** (hit rate per cache, and what a miss costs)

```sql
SELECT repository, job_name, cache,
       avg(hit)                         AS hit_rate,
       quantileIf(0.5)(wall_s, hit = 1) AS wall_p50_hit,
       quantileIf(0.5)(wall_s, hit = 0) AS wall_p50_miss
FROM ci.reports FINAL
ARRAY JOIN mapKeys(caches) AS cache, mapValues(caches) AS hit
WHERE ts >= now() - INTERVAL 30 DAY
GROUP BY repository, job_name, cache
ORDER BY repository, job_name, cache;
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
