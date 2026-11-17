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
import pytest

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

    # Make test_big.py look slow so the scheduler splits it across workers.
    big = str(Path(tmp, "test_big.py").resolve())
    Path(tmp, ".pytest-turbo").mkdir()
    Path(tmp, ".pytest-turbo/durations.json").write_text(json.dumps({big: [1.0] * 40}))

    for workers in ("1", "3"):
        turbo = subprocess.run([TURBO, "-n", workers, "--python", sys.executable], cwd=tmp, capture_output=True, text=True, env=env)
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
        print(f"-n {workers}: ok {got}")

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
    assert turbo.returncode == 1 and got["failed"] == 1 and got.get("passed", 0) < want["passed"], turbo.stdout
    print(f"-x: ok {got}")

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
        "def test_hangs_in_c():\n    import signal\n    signal.signal(signal.SIGALRM, signal.SIG_IGN)\n    time.sleep(60)\n\n"
        "@pytest.mark.timeout(3)\ndef test_marker_allows_more():\n    time.sleep(1.5)\n\n"
        "def test_prints():\n    print('visible with -s')\n"
    )
    env = {**os.environ, "NO_COLOR": "1"}
    turbo = subprocess.run([TURBO, "-n", "2", "--python", sys.executable], cwd=tmp, capture_output=True, text=True, env=env)
    out = turbo.stdout
    assert counts(last_line(out)) == {"passed": 3, "failed": 2, "error": 1}, out
    assert "test_x.py::test_hangs_in_c (crash" in out and 'test_x.py", line 19 in test_hangs_in_c' in out, out
    assert "FLAKY" in out and "test_flaky_twice passed after 2 rerun(s)" in out, out
    assert Path(tmp, "tries").read_text() == "xxx" and Path(tmp, "other").read_text() == "x", "rerun counts"
    assert "Timeout (>1.0s) from pytest-turbo" in out, out
    print("reruns and timeout from addopts and ini: ok")

    node = subprocess.run(
        [TURBO, "--python", sys.executable, "test_x.py::test_prints", "--", "-s"], cwd=tmp, capture_output=True, text=True, env=env
    )
    assert node.returncode == 0 and counts(last_line(node.stdout)) == {"passed": 1}, node.stdout
    assert "visible with -s" in node.stdout + node.stderr, node.stdout + node.stderr
    print("node id and -s: ok")
