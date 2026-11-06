# Summarizing CI opportunity

`mira_vitals.ci.opportunity.aggregate` summarizes an iterable of local
`ci-health.json` documents. It is also available as
`mira-vitals ci opportunity 'reports/*.json'`. It is not a data source: callers
must supply the reports available to them, including PR reports if those are
to appear in the distribution. It does not download or export artifacts.
The CLI reports unreadable files and unmatched patterns explicitly.

```python
import json
from pathlib import Path
from mira_vitals.ci.opportunity import aggregate

reports = [json.loads(path.read_text()) for path in Path("reports").glob("*.json")]
summary = aggregate(reports)
print(json.dumps(summary, indent=2))
```

Each observation is a **job attempt**, deduplicated by repository, run ID,
attempt and platform job ID. Duplicate snapshots keep the greatest
`reported_at`; indistinguishable conflicting snapshots and reports without
identity are excluded and counted. Matrix jobs must retain distinct IDs.
Groups distinguish repository, job name, event, selection policy, selector
version and configuration version (or `unknown`). Count categories, triggers
and trigger combinations as overlapping sets; sole triggers are separately
counted. `categories_selected` counts observations where a reduced selection
was proposed. `runs` and `category_runs` count distinct runs per group, so
matrix jobs do not inflate the denominator. Groups can overlap: summing across
job groups is not a distinct-run or distinct-PR distribution.

`selection_missing`, `decision_unknown`, `categories_unknown`,
`triggers_unknown`, `escape_unknown`, `escape_incomplete`,
`escape_not_applicable` and the observation counts show coverage.
Escapes count only when the producer reports `escape_observation: complete`;
missing JUnit, cancelled work, truncated test lists and other incomplete
observations must not become zero. A full-run decision is not evidence of
selection safety. A selector duration is summed only when measured.

`ci report --tests-complete` (or action input `tests-complete: 'true'`) is an
explicit caller attestation: use it only after all intended full-suite lanes
finish and all their JUnit reports are collected. Without it escape coverage
is incomplete; observed failing tests can still be reported as a lower bound.
An attestation is not an independent verification of the workflow's claims.

`saved_test_seconds` is included only when the producer reports a complete
escape observation and `skipped_test_seconds_observed`. It is the sum of
observed test durations the selector might skip, not measured runner time saved. Parallelism,
setup, contention and retries mean it is neither a wall-clock prediction nor a
critical-path estimate. Current producers may not supply that basis; in that
case the value is `null`, with zero observations. No prediction is made for
queue time or verdict latency. The input may itself be incomplete: absence of
a report is not evidence that a job or run did not exist.
