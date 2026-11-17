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

## Output

- Failures appear as they happen. At the end, each failure is printed in full with its captured stdout, stderr and log output.
- Tests that pass only after a rerun are reported as `FLAKY`.
- The summary lists the slowest files.
- When a worker crashes in a test, that test is reported as an error with the worker's output since it started, and a new worker runs the rest of the file. A crash between tests retries the file once.
- With `-s`, the workers' output goes to the terminal.

## How it works

A Rust binary finds the test files, plans the order and runs one long-lived pytest process per worker. Each worker runs a small plugin (`src/worker.py`) that replaces pytest's collection and run loop. The plugin asks the binary for the next file, collects and runs it, and reports each result as a line of JSON.

## Limits

- Package-scoped fixtures are set up again for each file a worker runs.
- `collect_ignore` and `collect_ignore_glob` in a `conftest.py` are not applied; `--ignore` and `--ignore-glob` after `--` are.
- No JUnit XML output yet.
- Coverage (pytest-cov) is not combined across workers.
- Not supported from pytest-rerunfailures and pytest-timeout: `--reruns-delay`, `--rerun-except`, `@pytest.mark.flaky`, `timeout_method` and `timeout_func_only`.
- Not tested on Windows.

## Development

```sh
cargo test
uv run --with pytest python selfcheck.py  # runs pytest-turbo and plain pytest on a sample project and compares the results
```
