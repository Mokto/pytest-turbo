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
pytest-turbo -- -k slow --tb=long  # anything after -- goes to pytest
```

It runs pytest with the Python of `$VIRTUAL_ENV`, else the nearest `.venv`, else the environment it is installed in. `--python` picks another.

## Why it's faster than xdist

- **Each worker imports only the files it runs.** xdist collects the whole suite in every worker, so every worker pays for importing every test module.
- **Slowest first.** Durations from earlier runs are kept in `.pytest-turbo/`, and the slowest files go out first, so no worker is left running a long file at the end.
- **Long files are split.** A file that would take longer than a worker's fair share runs on several workers at once. Each worker collects the whole file and runs its share of it, so a file that changed since the last run is still covered in full. `--no-split` turns this off, for suites with expensive class setup.
- **Discovery doesn't import anything.** It walks the tree using your `python_files`, `testpaths` and `norecursedirs` settings, in milliseconds.

On two real suites, compared with `pytest -n auto --dist loadfile`:

| Suite | xdist | pytest-turbo |
|---|---|---|
| 1,272 unit tests, 18 workers | 15–21 s | 7.5–12 s |
| 1,603 tests against Postgres and ScyllaDB, 8 workers | 23–26 s | 17–21 s |

## Output

- Failures appear as they happen. At the end, each failure is printed in full with its captured stdout, stderr and log output.
- Tests that pass only after a retry from `pytest-rerunfailures` are reported as `FLAKY`.
- The summary lists the slowest files.
- When a worker crashes, a new one starts and the file it was running is retried once, so tests after the crash still run.

## How it works

A Rust binary finds the test files, plans the order and runs one long-lived pytest process per worker. Each worker runs a small plugin (`src/worker.py`) that replaces pytest's collection and run loop. The plugin asks the binary for the next file, collects and runs it, and reports each result as a line of JSON.

## Limits

- Package-scoped fixtures are set up again for each file a worker runs.
- No JUnit XML output yet.
- Not tested on Windows.

## Development

```sh
cargo test
uv run --with pytest --with pytest-rerunfailures python selfcheck.py  # runs pytest-turbo and plain pytest on a sample project and compares the results
```
