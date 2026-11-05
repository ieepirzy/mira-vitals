# CI gate (`.github/workflows/ci-gate.yml`)

A reusable workflow that gives a run with path-filtered jobs one honest
verdict.

## Why

- GitHub reports a job skipped by `if:` as a passing check, whether the skip
  was deliberate or happened because something upstream broke.
- A workflow skipped by trigger-level `paths:` reports nothing, so a merge
  that waits on it waits forever.

The gate decides from the caller's own declaration: which jobs were needed
for this change, and what each one's result was.

## Verdict (fail closed)

| Job | Result | Gate |
|---|---|---|
| required | success | ok |
| required | failure, cancelled, skipped | red |
| not needed (`false`) | success, skipped | ok |
| not needed (`false`) | failure, cancelled | red |

- A job missing from `required`, or with any value other than `false`, is
  required.
- Malformed input, an empty `needs`, or a `required` key the gate doesn't wait
  on is red.

## Using it

```yaml
jobs:
  changes:        # decides what this change needs; always runs
    ...
    outputs:
      tofu: ${{ steps.f.outputs.tofu }}   # "true" / "false"
  tofu:
    needs: changes
    if: needs.changes.outputs.tofu == 'true'
    ...
  ci-gate:
    needs: [changes, tofu, contracts]    # every job
    if: always()
    uses: ieepirzy/mira-vitals/.github/workflows/ci-gate.yml@<full sha>
    with:
      needs: ${{ toJSON(needs) }}
      required: '{"tofu": ${{ needs.changes.outputs.tofu }}}'
      runs-on: '"mira-ci"'   # required
```

- **Push to main:** make `changes` output `true` for everything, so main always
  runs everything.
- **On any doubt, run it:** if `changes` can't work out the diff, it must say
  `true`.
- **What consumers check:** merge scripts and loops require `CI gate / verdict`
  to be `success`, with every check on the head completed. They don't read
  individual job results.

## `runs-on`

Required, with no default. Our repositories run on self-hosted runners only: GitHub's hosted minutes can't sustain them and cost more than our own runners (Ilari, 2026-11-05). A gate on hosted minutes could exhaust the quota and stall every merge, so a caller that omits `runs-on` fails loudly instead.
