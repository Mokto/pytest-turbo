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
    "test_big.py": "".join(f"def test_{i}():\n    pass\n\n" for i in range(40)),
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

    Path(tmp, "crashed_once").touch()  # plain pytest can't survive the crash, so it gets the second attempt's behaviour
    plain = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider"], cwd=tmp, capture_output=True, text=True, env=env)
    want = counts(last_line(plain.stdout))
    os.remove(Path(tmp, "crashed_once"))

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
        assert "CRASH" in turbo.stdout and "test_crash.py" in turbo.stdout, turbo.stdout
        assert "2 subtests passed" in last_line(turbo.stdout), turbo.stdout
        print(f"-n {workers}: ok {got}")

    cache = json.loads(Path(tmp, ".pytest-turbo/durations.json").read_text())
    assert len(cache[big]) == 40, "split chunks did not report every test of the split file"

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
