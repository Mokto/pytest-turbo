"""End-to-end check: pytest-turbo and plain pytest must report the same results on a throwaway project.

Run from a Python env that has pytest: `uv run python selfcheck.py` (builds pytest-turbo first).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
FILES = {
    "conftest.py": """
import os

import pytest

os.environ.setdefault("SELFCHECK_KEY", "from-conftest")

SETUPS = []

@pytest.fixture(scope="session")
def shared():
    SETUPS.append(1)
    assert len(SETUPS) == 1, "session fixture set up twice in one worker"
    return "shared"
""",
    "test_funcs.py": """
import pytest

@pytest.mark.parametrize("n", [1, 2, 3])
def test_param(n, shared):
    assert shared == "shared"

def test_fails():
    print("some captured output")
    assert 1 == 2

@pytest.mark.skip
def test_skipped():
    pass

@pytest.mark.xfail
def test_xfail():
    assert False
""",
    "pkg/case_test.py": """
import unittest

class WeirdlyNamed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready = True

    def test_one(self):
        self.assertTrue(self.ready)

    def test_subtests(self):
        for i in range(2):
            with self.subTest(i=i):
                self.assertGreaterEqual(i, 0)
""",
    "test_big.py": "import os\nimport pytest\n\n"
    + "".join(
        f'@pytest.mark.xdist_group("g")\ndef test_{i}():\n    open("groups.txt", "a").write(os.environ.get("PYTEST_XDIST_WORKER", "main") + "\\n")\n\n'
        if i in (0, 39)
        else f"def test_{i}():\n    pass\n\n"
        for i in range(40)
    ),
    "helper.py": "import os\n\nPID = os.getpid()\nKEY = os.environ.get('SELFCHECK_KEY')\n",
    "tests/__init__.py": "",
    "a/tests/__init__.py": "",
    "a/tests/test_a.py": "def test_a():\n    pass\n",
    "test_preload.py": """
import os

import helper

def test_sees_what_conftest_set_on_import():
    assert helper.KEY == "from-conftest"

def test_imported_before_fork():
    open("preloaded.txt", "a").write(f"{helper.PID != os.getpid()}\\n")

def test_asserts_still_rewritten():
    y = 3
    assert y == 4
""",
    "test_crash.py": """
import os

def test_before():
    pass

def test_kills_worker():
    if not os.path.exists("crashed_once"):
        open("crashed_once", "w").close()
        os._exit(1)

def test_after():
    pass
""",
}


def counts(line):
    return {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?)\b", line)}


def last_line(text):
    return [l for l in text.splitlines() if l.strip()][-1]


subprocess.run(["cargo", "build", "--release", "-q"], cwd=HERE, check=True)
TURBO = HERE / "target/release/pytest-turbo"

with tempfile.TemporaryDirectory() as tmp:
    for name, body in FILES.items():
        Path(tmp, name).parent.mkdir(parents=True, exist_ok=True)
        Path(tmp, name).write_text(body)
    Path(tmp, "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    env = {**os.environ, "NO_COLOR": "1"}

    Path(tmp, "crashed_once").touch()  # plain pytest can't survive the crash: it runs test_kills_worker as a pass
    plain = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider"], cwd=tmp, capture_output=True, text=True, env=env)
    want = counts(last_line(plain.stdout))
    want["passed"] -= 1  # under pytest-turbo it kills its worker and is reported as an error
    want["error"] = 1
    os.remove(Path(tmp, "crashed_once"))
    os.remove(Path(tmp, "groups.txt"))
    os.remove(Path(tmp, "preloaded.txt"))

    # Make test_big.py look slow so the scheduler splits it across workers.
    big = str(Path(tmp, "test_big.py").resolve())
    Path(tmp, ".pytest-turbo").mkdir()
    Path(tmp, ".pytest-turbo/durations.json").write_text(json.dumps({big: [1.0] * 40}))

    # The first preloading run has nothing to preload yet and records what the workers imported; the second forks from it.
    for workers, preload in (("1", ["--no-preload"]), ("3", ["--no-preload"]), ("3", []), ("3", [])):
        Path(tmp, "preloaded.txt").unlink(missing_ok=True)
        turbo = subprocess.run([TURBO, "-n", workers, *preload, "--python", sys.executable], cwd=tmp, capture_output=True, text=True, env=env)
        os.remove(Path(tmp, "crashed_once"))
        got = counts(last_line(turbo.stdout))
        assert got == want, f"-n {workers}: turbo {got} != pytest {want}\n{turbo.stdout}"
        assert turbo.returncode == 1, turbo.returncode
        assert "assert 1 == 2" in turbo.stdout and "some captured output" in turbo.stdout, turbo.stdout
        assert "CRASH" in turbo.stdout and "test_crash.py::test_kills_worker" in turbo.stdout, turbo.stdout
        assert "2 subtests passed" in last_line(turbo.stdout), turbo.stdout
        group_workers = Path(tmp, "groups.txt").read_text().split()
        assert len(group_workers) == 2 and len(set(group_workers)) == 1, f"xdist_group split across workers: {group_workers}"
        os.remove(Path(tmp, "groups.txt"))
        assert "assert 3 == 4" in turbo.stdout, turbo.stdout
        print(f"-n {workers} {' '.join(preload)}: ok {got}")
    if sys.platform == "darwin":  # pytest-turbo does not preload on macOS
        print("preload: skipped on macOS")
    else:
        # On their own: a worker that test_crash.py kills never reports what it imported. The workers import
        # a/tests as "tests" from the directory pytest adds to sys.path; without it, "tests" is another package.
        modules = Path(tmp, ".pytest-turbo/durations.modules.json")
        for _ in range(2):
            Path(tmp, "preloaded.txt").unlink(missing_ok=True)
            turbo = subprocess.run(
                [TURBO, "-n", "1", "--python", sys.executable, "test_preload.py", "a/tests/test_a.py"], cwd=tmp, capture_output=True, text=True, env=env
            )
            assert counts(last_line(turbo.stdout)) == {"passed": 3, "failed": 1}, turbo.stdout
        assert Path(tmp, "preloaded.txt").read_text() == "True\n", "helper.py was not imported before the worker forked"
        saved = json.loads(Path(tmp, ".pytest-turbo/durations.modules.json").read_text())[""]
        names = {name for name, _ in saved["modules"]}
        assert "helper" in names and not {"conftest", "test_preload", "pkg.case_test"} & names, names
        print("preload: ok")
        Path(tmp, "exits.py").write_text("import os\n\nos._exit(3)\n")
        saved = json.loads(modules.read_text())
        saved[""]["modules"].append(["exits", str(Path(tmp, "exits.py").resolve())])
        modules.write_text(json.dumps(saved))
        turbo = subprocess.run([TURBO, "-n", "2", "--python", sys.executable, "test_preload.py"], cwd=tmp, capture_output=True, text=True, env=env, timeout=60)
        assert "the process the workers are forked from died" in turbo.stdout + turbo.stderr and counts(last_line(turbo.stdout)) == {"passed": 2, "failed": 1}, turbo.stdout + turbo.stderr
        Path(tmp, "exits.py").unlink()
        print("preload, its process dies: ok")

    cache = json.loads(Path(tmp, ".pytest-turbo/durations.json").read_text())
    assert len(cache["test_big.py"]) == 40, "split chunks did not report every test of the split file"
    assert Path(tmp, ".pytest-turbo/.gitignore").read_text() == "*\n"

    elsewhere = Path(tmp, "elsewhere/durations.json")
    subprocess.run([TURBO, "--python", sys.executable, "--durations-file", str(elsewhere), "pkg"], cwd=tmp, capture_output=True, env=env)
    assert set(json.loads(elsewhere.read_text())) == {"pkg/case_test.py"}, elsewhere.read_text()
    subprocess.run([TURBO, "--python", sys.executable, "--durations-file", str(elsewhere), "test_big.py"], cwd=tmp, capture_output=True, env=env)
    assert set(json.loads(elsewhere.read_text())) == {"pkg/case_test.py", "test_big.py"}, "a run dropped another suite's durations"
    print("durations file: ok")

    ignored = Path(tmp, "ignored/durations.json")
    subprocess.run([TURBO, "--python", sys.executable, "--durations-file", str(ignored), "--", "--ignore-glob=*big*", "--ignore", "pkg"], cwd=tmp, capture_output=True, env=env)
    kept = set(json.loads(ignored.read_text()))
    assert "test_big.py" not in kept and not any(k.startswith("pkg/") for k in kept) and "test_crash.py" in kept, kept
    os.remove(Path(tmp, "crashed_once"))
    print("--ignore: ok")

    turbo = subprocess.run([TURBO, "-x", "-n", "2", "--python", sys.executable], cwd=tmp, capture_output=True, text=True, env=env)
    got = counts(last_line(turbo.stdout))
    stops = got.get("failed", 0) + got.get("error", 0)  # the failing test or the crash, whichever comes first
    assert turbo.returncode == 1 and stops == 1 and got.get("passed", 0) < want["passed"], turbo.stdout
    print(f"-x: ok {got}")
    turbo = subprocess.run([TURBO, "-x", "-n", "2", "--no-preload", "--python", sys.executable], cwd=tmp, capture_output=True, text=True, env=env, timeout=60)
    got = counts(last_line(turbo.stdout))
    assert turbo.returncode == 1 and got.get("failed", 0) + got.get("error", 0) == 1, turbo.stdout
    print(f"-x --no-preload: ok {got}")

    flaky = Path(tmp, "test_flaky.py")
    flaky.write_text("import os\n\ndef test_flaky():\n    if not os.path.exists('flaked'):\n        open('flaked', 'w').close()\n        assert False\n")
    turbo = subprocess.run([TURBO, "--python", sys.executable, str(flaky), "--", "--reruns", "1"], cwd=tmp, capture_output=True, text=True, env=env)
    assert turbo.returncode == 0 and "1 passed, 1 flaky" in turbo.stdout, turbo.stdout
    print("flaky: ok")

    env_test = Path(tmp, "test_env.py")
    env_test.write_text(
        "import os, sys\n\n"
        "def test_looks_like_xdist():\n"
        "    assert os.environ['PYTEST_XDIST_WORKER'].startswith('gw')\n"
        "    assert os.environ['PYTEST_XDIST_WORKER_COUNT'] == '2'\n"
        "    assert sys.argv[0].endswith('pytest')\n"
    )
    turbo = subprocess.run([TURBO, "-n", "2", "--python", sys.executable, str(env_test), str(flaky)], cwd=tmp, capture_output=True, text=True, env=env)
    assert turbo.returncode == 0, turbo.stdout
    print("xdist env: ok")

# Reruns and timeouts are pytest-turbo's own, configured the way pytest-rerunfailures and pytest-timeout are.
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--reruns 2 --only-rerun \'ConnectionError: flaky\'"\ntimeout = 1\n'
    )
    Path(tmp, "test_x.py").write_text(
        "import os, time, pytest\n\n"
        "def test_flaky_twice():\n"
        "    n = os.path.getsize('tries') if os.path.exists('tries') else 0\n"
        "    open('tries', 'a').write('x')\n"
        "    if n < 2:\n        raise ConnectionError('flaky')\n\n"
        "def test_other_error_is_not_rerun():\n"
        "    open('other', 'a').write('x')\n"
        "    raise ValueError('flaky')\n\n"
        "def test_slow():\n    time.sleep(5)\n\n"
        "def test_crashes_once():\n"
        "    if not os.path.exists('crashed'):\n        open('crashed', 'w').close()\n        os._exit(1)\n\n"
        "@pytest.mark.timeout(3)\ndef test_marker_allows_more():\n    time.sleep(1.5)\n\n"
        "def test_prints():\n    print('visible with -s')\n"
    )
    env = {**os.environ, "NO_COLOR": "1"}
    Path(tmp, "test_hang.py").write_text(
        "import signal, time\n\ndef test_hangs_in_c():\n    signal.signal(signal.SIGALRM, signal.SIG_IGN)\n    time.sleep(60)\n\n"
        "def test_after():\n    pass\n"
    )
    turbo = subprocess.run([TURBO, "-n", "2", "--python", sys.executable, "test_x.py"], cwd=tmp, capture_output=True, text=True, env=env)
    out = turbo.stdout
    assert counts(last_line(out)) == {"passed": 4, "failed": 2}, out
    assert "2 flaky" in last_line(out), out
    assert "test_crashes_once worker" in out and "test_crashes_once passed after 1 rerun(s)" in out, out
    assert "FLAKY" in out and "test_flaky_twice passed after 2 rerun(s)" in out, out
    assert Path(tmp, "tries").read_text() == "xxx" and Path(tmp, "other").read_text() == "x", "rerun counts"
    assert "Timeout (>1.0s) from pytest-turbo" in out, out
    print("reruns and timeout from addopts and ini: ok")

    hang = subprocess.run([TURBO, "--python", sys.executable, "test_hang.py", "--", "--reruns", "0"], cwd=tmp, capture_output=True, text=True, env=env)
    assert counts(last_line(hang.stdout)) == {"passed": 1, "error": 1}, hang.stdout
    assert "test_hang.py::test_hangs_in_c (crash" in hang.stdout and 'test_hang.py", line 5 in test_hangs_in_c' in hang.stdout, hang.stdout
    print("uninterruptible hang: ok")

    node = subprocess.run(
        [TURBO, "--python", sys.executable, "test_x.py::test_prints", "--", "-s"], cwd=tmp, capture_output=True, text=True, env=env
    )
    assert node.returncode == 0 and counts(last_line(node.stdout)) == {"passed": 1}, node.stdout
    assert "visible with -s" in node.stdout + node.stderr, node.stdout + node.stderr
    print("node id and -s: ok")

# Snapshots: snapshottest's keys and files, written once by the controller even when the file is split.
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, "test_snap.py").write_text(
        "import os, pytest\n\n"
        "class TestA:\n"
        "    @pytest.fixture(scope='class', autouse=True)\n"
        "    def snap(self, request):\n"
        "        request.cls.snap = request.config.pluginmanager.get_plugin('pytest_turbo').snapshot(request)\n\n"
        "    def test_one(self):\n        self.snap.assert_match('one\\nline \"quoted\"', 'first')\n\n"
        "    def test_two(self):\n        self.snap.assert_match(b'bytes', 'second')\n\n"
        "def test_f1(snapshot):\n    snapshot.assert_match({'a': 1})\n\n"
        "def test_f2(snapshot):\n    snapshot.assert_match('x')\n    snapshot.assert_match('y')\n\n"
        "def test_f3(snapshot):\n    snapshot.assert_match(os.environ.get('SNAPVAL', 'v1'), 'env')\n\n"
        "def test_f4():\n    pass\n"
    )
    durations = Path(tmp, "durations.json")
    snap = Path(tmp, "snapshots", "snap_test_snap.py")

    def run(*pytest_args, val="v1", paths=()):
        durations.write_text(json.dumps({"test_snap.py": [1.0] * 6}))  # slow enough to split in 3
        cmd = [TURBO, "-n", "3", "--python", sys.executable, "--durations-file", str(durations), *paths, "--", *pytest_args]
        return subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1", "SNAPVAL": val})

    def result_line(out):
        return [l for l in out.splitlines() if "workers, discovery" in l][-1]

    def keys():
        return sorted(re.findall(r'^snapshots\[\s*"([^"]+)"', snap.read_text(), re.M))

    missing = run()
    assert counts(result_line(missing.stdout)) == {"failed": 5, "passed": 1}, missing.stdout
    assert "no snapshot 'test_f1 1'" in missing.stdout and not snap.exists(), missing.stdout
    written = run("--snapshot-update")
    assert written.returncode == 0 and "3 workers" in result_line(written.stdout), written.stdout
    assert "snapshots: 6 written, 0 removed, 1 files changed" in written.stdout, written.stdout
    want = ["TestA.TestA first", "TestA.TestA second", "test_f1 1", "test_f2 1", "test_f2 2", "test_f3 env"]
    assert keys() == want and snap.read_text().startswith("# Written by pytest-turbo"), snap.read_text()
    again = run("--snapshot-update")
    assert "0 written, 0 removed, 0 files changed" in again.stdout, again.stdout
    assert run().returncode == 0
    print("snapshots written across split workers: ok")

    snap.write_text(snap.read_text() + '\nsnapshots["gone 1"] = "stale"\n')
    partial = run("--snapshot-update", "-k", "not f1", val="v2")
    assert keys() == sorted(want + ["gone 1"]) and "'v2'" not in snap.read_text() and '"v2"' in snap.read_text(), snap.read_text()
    node = run("--snapshot-update", paths=["test_snap.py::test_f2"])
    assert node.returncode == 0 and "gone 1" in keys(), node.stdout
    full = run("--snapshot-update", val="v2")
    assert keys() == want and "1 removed" in full.stdout, full.stdout
    print("unvisited snapshots kept on partial runs, removed on full ones: ok")

    differ = run(val="v3")
    assert counts(result_line(differ.stdout)) == {"failed": 1, "passed": 5}, differ.stdout
    assert "snapshot 'test_f3 env' does not match" in differ.stdout and "-v2" in differ.stdout and "+v3" in differ.stdout, differ.stdout
    old = snap.read_text().replace(
        "# Written by pytest-turbo --snapshot-update.\nsnapshots = {}\n",
        "# -*- coding: utf-8 -*-\n# snapshottest: v1 - https://goo.gl/zC4yUc\nfrom __future__ import unicode_literals\n\n"
        "from snapshottest import Snapshot\n\nsnapshots = Snapshot()\n",
    )
    snap.write_text(old)
    assert run(val="v2").returncode == 0, "reads snapshottest's own files"
    print("snapshot diff and snapshottest files: ok")
