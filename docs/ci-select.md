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
An explicit `--mode advisory` turns all skipping off, the gates' included;
`--mode enforce` leaves a gate configured `advisory` advisory.

Discovery reads the repository's Python text, so two matches stay invisible
to it: a parametrize id computed from data (a file named after the keyword)
and a marker added by a plugin module rather than a conftest. Avoid both for
a gated keyword; `files` limits the damage.

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

## Data map: select by the data a test was seen to read

Without a map, any changed file that is not Python (and not in
`ignore_globs`) is a full run: tests read YAML, fixtures and templates with no
import edge to follow. In a repository that is mostly data, that is nearly
every change. A data map replaces the `non-py` trigger with a measurement.

Record it with the project's own Python:

```sh
mira-vitals ci inputs --python .venv/bin/python --jobs 2   # writes select.data_map
```

Each test file runs in its own pytest process with a recording plugin (an
audit hook; standard library only). Per file:

| What it did | Input glob |
|---|---|
| opened `a/b/c.yaml` (also `sqlite3.connect`, `pkgutil.get_data`) | `a/b/*` (siblings too) |
| listed `a/b` (`os.walk`, `Path.glob`, `glob.glob`, `scandir`) | `a/b/**` |
| started any process; had a test skip, fail, error or get deselected (`-k`, `-m`, `--deselect`, `addopts`); listed the repository root; read through a symbolic link that leaves the repository | `**` (runs on every data change) |

A read through a symbolic link inside the repository is recorded under both
the link and its target. Reads made by pytest itself (collection,
configuration) and module loading by the import system are not the test's and
are not recorded. A test that reads Python source as data (hashing it, say)
is recorded, and a change to that source selects it even when no import edge
leads there. The recorder's guard against recursion is per thread, so reads
from worker threads are seen.

With `select.data_map` set, a changed data file selects:
- the test files whose globs match it;
- every test file the map does not list;
- every test file whose *code digest* (the test file and its first-party
  import closure, from ruff's graph) differs from the recording, because
  changed code may read different files.

A changed Python file consults the map too. Besides the import graph's
selection, it selects the tests recorded reading it, the universal ones, and
the unlisted or stale ones.

**Fail-safe.** The map also keeps every path the recording saw read and
every directory it saw listed. A changed data file is a full run when:
- it is new: added, or the new side of a rename (trigger `data-added`); or
- no recording saw it read, nor saw it in a listed directory
  (`data-unseen`).

Files matching `ignore_globs` (the files that never feed a test) are exempt.
Edits to files a test was recorded reading stay selective. Other full runs:
- a change to the map itself (`data-map`);
- an unreadable map (`selector_error`);
- when `--repo` is a sub-directory of the checkout, any change outside it
  (`outside-root`).

All other full-run triggers are unchanged. `selection.json` reports the map
under `data_map` (entries, current, stale, universal, selected_by_data).

**What the map cannot see.** Some reads raise no audit event:
- `os.stat`, `os.path.exists`, `os.access`;
- files opened from C code (extension modules).

A new or never-read file is covered by the fail-safe. The blind spot that
remains: an existing file that some test was recorded reading, which another
test reads only through a stat probe or C code. A change to it selects only
the recorded reader. Advisory mode and `ci report`'s escape count are the net
for that. Turn on `enforce` only after weeks of zero escapes.

Commit the map and re-record when tests change how they find their data. A
stale entry runs on every data change until then, which costs selection. It
does not restore reads the recording never saw.

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
data_map = "mira-vitals-inputs.json" # optional: `mira-vitals ci inputs` writes it

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
