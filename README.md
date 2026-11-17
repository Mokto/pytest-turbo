# pytest-turbo

Runs your pytest suite in parallel, about twice as fast as `pytest-xdist`, while running real pytest: your plugins, fixtures, conftest files, `unittest.TestCase` classes and test classes keep working.

> Alpha. Compare its test counts with plain pytest before relying on it.

## Install

```sh
uv add --dev pytest-turbo     # or: pip install pytest-turbo
```

## Use

```sh
pytest-turbo                       # every test, one worker per CPU
pytest-turbo -n 8 tests/api        # 8 workers, one directory
pytest-turbo -x                    # stop at the first failure
pytest-turbo tests/test_api.py::TestUsers::test_create -- -s  # one test, its output on the terminal
pytest-turbo -- -k slow --tb=long  # anything after -- goes to pytest
```

It runs pytest with the Python of `$VIRTUAL_ENV`, else the nearest `.venv`, else the environment it is installed in. `--python` picks another.

Each worker gets `PYTEST_XDIST_WORKER` (`gw0`, `gw1`, …) and `PYTEST_XDIST_WORKER_COUNT`, as under pytest-xdist, so suites that give each worker its own database keep working. Tests marked `@pytest.mark.xdist_group(name)` in one file always run on the same worker, even when the file is split.

### Reruns and timeouts

pytest-turbo replaces pytest-rerunfailures and pytest-timeout, with the same options, so they can stay in `addopts`:

```toml
[tool.pytest.ini_options]
addopts = "--reruns 2 --only-rerun ConnectionError"
timeout = 120
```

- `--reruns N` runs a failed test again, up to N times, as a task of its own, often on another worker. `--only-rerun REGEX` (repeatable) limits reruns to failures whose exception, `Type: message`, matches. A test that passes on a rerun is reported as `FLAKY`. Reports of the failed attempts have the outcome `rerun`, as with pytest-rerunfailures.
- `--timeout SECONDS`, the `timeout` ini key or `@pytest.mark.timeout(seconds)` fail a test that runs too long, with a traceback where it was stuck. If the test can't be interrupted (a C call that never returns), the worker prints every thread's stack and exits 10 seconds later; the test is reported with that output and the rest of its file goes on.
- pytest-rerunfailures and pytest-timeout are disabled in the workers if installed, and plain `pytest` rejects these options once they are uninstalled.

### Snapshots

pytest-turbo replaces snapshottest, with its keys and its files (`snapshots/snap_<module>.py` beside each test module), so existing snapshots keep working:

```python
def test_render(snapshot):
    snapshot.assert_match(render(), "page")
```

- Without `--snapshot-update`, a missing snapshot fails, and a different one fails with a unified diff.
- With `--snapshot-update`, workers send what they saw and the controller writes each file once, at the end. A file split across workers is updated in full, so updating snapshots needs no `--no-split`. Entries no test used are removed only when every test of the module ran and passed (not with `-k`, a test id, `-x` or a failure).
- Written entries are formatted as ruff formats them; unchanged entries keep their text.
- For a class-scoped fixture, `request.config.pluginmanager.get_plugin("pytest_turbo").snapshot(request)` returns the same object, keyed by the class as snapshottest keys it.
- snapshottest is disabled in the workers if installed. Its `from snapshottest import Snapshot` header is still read; files are written with `snapshots = {}` instead.

### In CI

The speedup depends on knowing test durations from earlier runs. On a fresh checkout there are none, so keep the file somewhere that survives between jobs:

```sh
pytest-turbo --durations-file /persistent/cache/my-suite.json
```

Paths in it are relative to the pytest rootdir. Each run merges its results into the file and replaces it atomically, so parallel jobs and different suites can share one file.

## Why it's faster than xdist

- **Each worker imports only the files it runs.** xdist collects the whole suite in every worker, so every worker pays for importing every test module.
- **Slowest first.** Durations from earlier runs are kept in `.pytest-turbo/` in the rootdir, and the slowest files go out first, so no worker is left running a long file at the end.
- **Long files are split.** A file that would take longer than a worker's fair share runs on several workers at once. Each worker collects the whole file and runs its share of it, so a file that changed since the last run is still covered in full. `--no-split` turns this off, for suites with expensive class setup.
- **Discovery doesn't import anything.** It walks the tree using your `python_files`, `testpaths` and `norecursedirs` settings, in milliseconds.

On two real suites, compared with `pytest -n auto --dist loadfile`:

| Suite | xdist | pytest-turbo |
|---|---|---|
| 1,272 unit tests, 18 workers | 15–21 s | 7.5–12 s |
| 1,603 tests against Postgres and ScyllaDB, 8 workers | 23–26 s | 17–21 s |

### Less memory: shared imports

Every worker imports the same packages and the code under test, often most of what it uses. So one process imports them once and the workers are forked from it, sharing that memory instead of each holding a copy. On a suite of 1,400 tests with 8 workers, peak memory went from 3.6 GiB to 1.7 GiB. `--no-preload` starts every worker on its own instead.

- Each run records what the workers imported, except test modules and `conftest.py` files, in a file next to the durations file (`durations.modules.json` by default). The next run imports those modules before forking. The first run has nothing to import yet and uses as much memory as with `--no-preload`.
- Before importing them, that process loads the `conftest.py` files a worker loads before collecting, so modules that read environment variables on import see what those conftests set. It has `PYTEST_XDIST_WORKER_COUNT` and `sys.argv[0]` set as in a worker, but no `PYTEST_XDIST_WORKER`: a module that reads its worker id on import gets it wrong.
- Modules that resolve to another file without the test directories pytest adds to `sys.path`, such as test helpers or the many packages named `tests`, are left to the workers, as are modules that fail to import.
- Threads that the imported code starts don't exist in the forked workers. If a suite behaves differently than with `--no-preload`, this is the first suspect.
- If the process the workers are forked from dies, the run goes on with workers started the usual way.
- Off on macOS: its system frameworks aren't safe to use after a fork, and forked workers hang in calls such as `urllib.request.getproxies()`, which httpx makes for every client.
- The list only grows, since the workers report what was preloaded too. Delete the file to start over.

## Output

- Failures appear as they happen. At the end, each failure is printed in full with its captured stdout, stderr and log output.
- Tests that pass only after a rerun are reported as `FLAKY`.
- The summary lists the slowest files.
- When a worker crashes in a test, a new worker runs the rest of the file. The test is rerun if `--reruns` allows (whatever `--only-rerun` says: a crash has no exception to match), otherwise it is reported as an error with the worker's output since it started. A crash between tests retries the file once.
- With `-s`, the workers' output goes to the terminal.

## How it works

A Rust binary finds the test files, plans the order and runs one long-lived pytest process per worker. Each worker runs a small plugin (`src/worker.py`) that replaces pytest's collection and run loop. The plugin asks the binary for the next file, collects and runs it, and reports each result as a line of JSON.

## Limits

- Package-scoped fixtures are set up again for each file a worker runs.
- `collect_ignore` and `collect_ignore_glob` in a `conftest.py` are not applied; `--ignore` and `--ignore-glob` after `--` are.
- No JUnit XML output yet.
- Coverage (pytest-cov) is not combined across workers.
- Snapshot values are strings, bytes and other Python literals; snapshottest's `FileSnapshot` and `GenericRepr` are not supported.
- Not supported from pytest-rerunfailures and pytest-timeout: `--reruns-delay`, `--rerun-except`, `@pytest.mark.flaky`, `timeout_method` and `timeout_func_only`.
- Not tested on Windows.

## Development

```sh
cargo test
uv run --with pytest python selfcheck.py  # runs pytest-turbo and plain pytest on a sample project and compares the results
```
