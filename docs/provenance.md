# Provenance


The point of this module is what it refuses to do. Authorship is **never**
inferred from commit-message text, co-author trailers, author names, or "this
looks agent-written". A heuristic that is 90% accurate produces a 10% mislabel
rate *correlated with the thing being measured*, which is worse than no data.

Populated only from sources explicit about themselves, in precedence order:

1. `workflow_input` — a `workflow_dispatch` input or explicit environment variable.
2. `agent_source` — agent execution metadata from an orchestrator your
   config declares. This package ships knowing about none of them.
3. `pr_label` — `authoring:*` labels; trusted because applying one needs write access.

Modes: `human`, `human_assisted`, `agent_supervised`, `agent_autonomous`, `mixed`.

Rules that matter:

* **Unknown stays `null`.** Backfilling unknowns as `human` would bias every
  future comparison in exactly the direction the research question is about.
* **An invalid mode is rejected, not coerced** — a typo must not become a data point.
* **Conflicts are recorded, not resolved away.** If a label and a workflow input
  disagree, `conflict: true` and both appear in `declared_modes`; the
  disagreement is itself data.
* **A mixed change is representable** rather than forced into a binary.
* **`human_authors` are stable internal IDs**, not names or emails. The analyses
  need a pseudonymous key, not an identity, and a dataset accumulating personal
  data for years is a liability.
* `granularity` is `"change"` in v1 and says so. The schema is designed for
  commit / change / file / symbol provenance; only change level is populated
  reliably today, and claiming more would be fabrication.


## Configuring the sources

See the README. In short: the *schema* (which variables, which labels) is
config-file, because it is a property of your repository's conventions; the
*values* are environment variables, because only whatever launched the run knows
them. Nothing is assumed by default.
