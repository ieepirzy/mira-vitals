# `mira-vitals ci select`: test-impact selection

`ci select` decides which test files a pull request needs to run, records the
decision in `selection.json` (the file `ci report` already reads), and writes
the list of test files to run. It never fails the build: any error is a full
run.

## How a test file is selected

A test file runs when it can reach a changed module:

- **ruff's import graph**: `ruff analyze graph --detect-string-imports`, walked
  in reverse from every changed `.py` file;
- **Lomitus's graph**: `lomitus.impact.affected_files`, which adds the
  importers of deleted and renamed modules from the base revision's graph,
  ancestor package `__init__.py` edges, and resolved qualified-call targets;
- every changed test file;
- the configured `always_run` list (schema, migration, subprocess and other
  introspection tests no import graph can see).

The two graphs are unioned. Selection is per **file**, so the counts in
`selection.json` carry `"count_unit": "files"`.

## Full-run triggers

The whole suite runs when the change touches something no import graph sees:

| Trigger | Fires on (defaults) |
|---|---|
| `conftest` | any `conftest.py` |
| `conftest-closure` | a module imported, directly or not, by a `conftest.py` |
| `migrations` | `**/migrations/**`, `**/alembic/**`, `alembic.ini` |
| `deps` | `pyproject.toml`, `uv.lock`, `requirements*.txt`/`.in`, `setup.py`/`.cfg`, `poetry.lock`, `Pipfile*` |
| `docker` | `Dockerfile*`, `docker-compose*.yml`, `compose*.yml` |
| `ci` | `.github/**`, `ci/**`, `mira-vitals.toml` |
| `non-py` | **any** non-Python file, except `ignore_globs` (default: `*.md`, `*.rst`, `LICENSE*`, `.gitignore`, `.editorconfig`). Tests read data, fixtures and templates with no import edge |
| `deleted-py` | a deleted or renamed `.py` file |
| `unresolved` | a changed `.py` file the graph doesn't know |
| `unknown-change` | a diff status other than added, modified, deleted, renamed |
| `unsafe-path` | a selected test path with characters outside `[A-Za-z0-9_./-]` |
| `selector_error` | anything went wrong: a missing graph tool, a bad revision, a bad config. **Fail open** |

## Modes

- **`advisory`** (the default): the selector decides and records, but **every
  test runs**. `ci report` then counts **escapes**: tests the selector would
  have skipped that failed. That is the direct proof that a selection would
  have been unsafe, measured on real changes with nothing at stake.
- **`enforce`**: only the selected files run, **and only on `pull_request`
  events**. Pushes, schedules and manual runs always run the whole suite, and
  so does the default branch, which is the safety net for an escape that gets
  past a PR.

**Turn `enforce` on per repository only after several weeks of `advisory`
runs with zero escapes** (`ci.selection.escapes` in CI health). An escape in
advisory mode means a trigger or the always-run list is missing something:
fix that first.

## Gates: skip slow data-driven tests their change cannot reach

Some tests are slow and depend on data no import graph sees: alert-rule unit
tests that read YAML, say. In a repository that is mostly data, `non-py`
makes nearly every run full, so file selection never saves them. A **gate**
names those tests by a keyword and their inputs by globs:

```toml
[select.gates.rules]
keyword = "pinned_rule_tool"      # in the gated tests' function names
inputs = ["alerts/**", "tests/rule_units/**"]
files = ["tests/test_*_alerts.py"]  # optional: where gated tests may live
policy = "enforce"                # optional; default: select.policy
```

List every path the gated tests read in `inputs`, including the pinned
tool's version if it lives outside CI, dependency or Docker files. Recording
the files each gated test opens (a Python audit hook in a pytest plugin) is
a reliable way to find them.

A gate is **open** (its tests run) when any of these holds:

- a changed path (or a renamed file's old path) matches `inputs`;
- any full-run trigger fired other than `non-py`: conftest, deps, CI files, a
  deleted or unresolved module, a selector error. The gate's own inputs
  decide for data files; nothing else is trusted to;
- the import graph reaches a file that may hold gated tests, or that file
  changed;
- such a file is in `always_run`, or falls outside the gate's `files` globs
  (the keyword caught a test it was not meant for);
- the keyword matches no test (`no-gated-tests`: a configuration to fix).

A file "may hold gated tests" when the keyword appears anywhere in its path
or text, since pytest's `-k` matches case-insensitively on module, class and
function names, parametrize ids, marker names and extra keywords. When any
`conftest.py` mentions the keyword, every test file counts. This over-counts
on purpose: a miss would let `-k` skip a test whose imports nobody checked.

Otherwise it is **closed**. A closed gate whose policy is `enforce` adds
`-k "not <keyword>"` to the args file on `pull_request` events, even when the
rest of the run is full. Pushes, schedules and the default branch run
everything. In `advisory` a closed gate only records.

Pick a keyword only the gated tests carry, and set `files` so a new test
that happens to match it keeps the gate open instead of being skipped on a
data change its own inputs would have caught. `selection.json` lists each gate with `open`, its
`reasons` and the files holding its tests (`files`). `skipped_keywords`
holds the closed gates' keywords, and `ci report` counts a failed test that
matches one of them as an **escape**, as it does for skipped files.
`deselected_keywords` holds the keywords this run did skip.

A gate may enforce while the file selection stays advisory. For data files
its inputs are named, not inferred; for Python changes it relies on the same
import graph as file selection, with the same blind spots (code run through
a subprocess, dynamic imports), and the default branch running the whole
suite is the safety net. Soaking a gate in `advisory` first measures that.
An explicit `--mode` overrides every gate's policy: `--mode advisory` turns
all skipping off.

The gate writes its own `-k`. pytest keeps only the last `-k` it is given,
so a workflow that passes one too loses either the gate's or its own.

## Result caching (recorded only)

With `record_closure_hashes = true`, each test file's Lomitus `closure_hash`
is recorded in `selection.json`. The hash covers code only, so the
environment goes into `extra`:

- the Python version;
- the lockfile digests;
- the named `cache_env` variables (hashed);
- `service_images` (pin them by digest).

Each file is also marked `cacheable: false` when its closure reaches the
database, network or clock (Lomitus `node_effects` = `external`). Such a
test's result depends on more than its code.

**Nothing is skipped on these hashes.** They exist so a result cache can be
evaluated against real runs first.

## Configuration

In `mira-vitals.toml` (or `[tool.mira_vitals.select]` in `pyproject.toml`):

```toml
[select]
policy = "advisory"                 # or "enforce"
graph_sources = ["ruff", "lomitus"] # both by default; a missing one is a full run
ruff_paths = ["."]
test_globs = ["**/test_*.py", "**/*_test.py"]
always_run = ["tests/test_schema.py", "tests/test_migration_*.py"]
ignore_globs = ["**/*.md"]
record_closure_hashes = false
cache_env = []
service_images = []

[select.triggers]                   # extend or narrow the defaults
fixtures = ["tests/data/**"]
docker = []                         # an empty list turns a default off
```

## In a workflow

```yaml
- name: Select tests
  run: |
    python -P -m mira_vitals ci select \
      --base "${{ github.event.pull_request.base.sha || github.event.before }}" \
      --head "${{ github.sha }}" --args-out "$RUNNER_TEMP/tests.txt"
- name: Test
  run: |
    mapfile -t SELECTED < "$RUNNER_TEMP/tests.txt"   # empty means everything;
    # may end with `-k` and an expression when a gate deselects tests
    pytest --junitxml=reports/junit.xml "${SELECTED[@]}"
```

Read the args file with `mapfile`, never by interpolating it into a command
line. `ci report` picks up `selection.json` from the state directory on its
own.

The checkout needs the base revision: use `fetch-depth: 0`, or fetch the base
SHA. Without it the diff fails and the run is full (`selector_error`).
