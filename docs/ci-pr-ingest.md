# Importing PR CI-health reports

PR jobs can execute contributor-controlled code before their report step.
Do not give them a collector token. The opt-in artifact path below lets a
separate, trusted `workflow_run` import bounded report observations after a
PR run completes, including failed and cancelled runs. This is GitHub.com
only; other GitHub installations and Forgejo need their own trusted adapter.

In the PR job, request the artifact on the reporting action, without an OTLP
credential or endpoint in that job:

```yaml
- uses: ieepirzy/mira-vitals/.github/actions/ci-health@<reviewed-40-hex-sha>
  if: always()
  with:
    junit: reports/junit.xml
    outcome: ${{ job.status }}
    # Only when every intended lane actually finished and its reports were collected.
    tests-complete: ${{ steps.tests.outcome == 'success' }}
- uses: ieepirzy/mira-vitals/.github/actions/ci-health-artifact@<reviewed-40-hex-sha>
  if: always()
```

The report action writes a report as before; the separate opt-in action uploads `ci-health.json` under a
unique name using its numeric GitHub job ID. If there is no report or the
platform adapter could not identify the job, no artifact is uploaded. Grant
`actions: read` so the adapter can identify the job. This does not change the
tests or their results. The artifact is visible to people who can download
repository workflow artifacts; it can contain test names and local paths.
Protect any sensitive test data accordingly.

In a **separate default-branch workflow**, replace both SHA placeholders with
the same reviewed immutable collector commit. Set the endpoint and credential
as trusted repository configuration; do not take either from a PR artifact.

```yaml
name: Import CI health PR reports
on:
  workflow_run:
    workflows: [CI Pipeline]  # exact upstream display name
    types: [completed]
permissions:
  contents: read
  actions: read
jobs:
  import:
    uses: ieepirzy/mira-vitals/.github/workflows/ci-pr-ingest.yml@<reviewed-40-hex-sha>
    with:
      collector-sha: <reviewed-40-hex-sha>
      upstream-workflow-path: .github/workflows/ci.yml
      runner: mira-ci
      otlp-endpoint: https://collector.example
    secrets:
      OTEL_EXPORTER_OTLP_HEADERS: ${{ secrets.OTEL_EXPORTER_OTLP_HEADERS }}
```

The importer checks that the event is a completed PR `workflow_run`; retrieves
the run, attempt, job list and artifact list from the GitHub API; requires the
upstream workflow path to equal the configured allowlist; and checks the
report's run, repository and job against those GitHub records. It only checks
out the separately pinned public collector, never PR code. It downloads each
archive through GitHub, with an HTTPS storage-host allowlist and **without**
forwarding the GitHub token to the redirected host. Archives are never
extracted to disk: each must contain exactly one bounded `ci-health.json`.

All imported values are still **untrusted PR observations**. Code in the PR
can change or forge its own report, including the test-completion attestation.
The importer strips unknown fields, validates bounds, uses GitHub rather than
the artifact for run/job identity and job conclusion, and labels every row
`ci.observation.trust=untrusted_pr_artifact`. Do not use these rows as an
authoritative approval or automatic enforcement gate. No per-test records are
imported; the importer marks test inventory incomplete, so existing escape
joins cannot treat absent rows as skipped tests.

At most 50 matching artifacts are processed; artifacts and uncompressed JSON
have explicit limits, API lists must be complete within 100 entries, and the
whole import has a deadline. Invalid input rejects the batch with a warning;
missing, expired or absent artifacts produce no observations, not zeroes.
Failures of the separate importer do not alter the result of the upstream PR
run. A receiver must retain the trust attribute before comparing these reports
with trusted push data. Use run/attempt/job IDs to deduplicate retries.
