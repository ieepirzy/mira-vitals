"""CI health: how long a CI job waited, ran, and spent where.

The code-health collector measures the code; this package measures the
pipeline that checks it. One command, run as the last step of a job with
``if: always()``::

    mira-vitals ci start                 # first step (optional): the clock
    mira-vitals ci mark install          # between steps (optional): phases
    mira-vitals ci report --junit reports/junit.xml

It is platform-neutral at its core -- wall time, phases, per-test durations
from JUnit XML, cache outcomes, the runner's own cgroup -- and learns more
from an optional platform adapter (GitHub or Forgejo Actions) using the
job's own token: when the job was queued, when a runner took it, every
step's timing, and the other jobs of the same run.

The one rule above all others: **it never fails the build**. Every error
becomes a warning and the process exits 0. A broken telemetry step must not
turn a green pipeline red, and a slow API must not hold one up: every
network call is bounded, and so is the whole report.
"""
