"""pytest-turbo worker: one pytest session, fed tasks over a pipe.

A task line is "path<TAB>cuts<TAB>chunk<TAB>nodeid<TAB>attempt<TAB>from": collect the whole file,
run items[cuts[chunk-1]:cuts[chunk]] from index `from` on, or only `nodeid` when it is set (a rerun).
An empty line means no more work. Events go out as JSON lines.

Reruns and timeouts replace pytest-rerunfailures and pytest-timeout, with the same options:
`--reruns`, `--only-rerun`, `--timeout`, the `timeout` ini key and `@pytest.mark.timeout`.
A failed attempt is reported with outcome "rerun", as pytest-rerunfailures does, and the
controller runs it again as its own task.

Snapshots replace snapshottest, with its keys and its snapshots/snap_<module>.py files. Workers
compare; with --snapshot-update they send entries to the controller, which writes each file once.

With --preload, one process runs serve_forks: it imports what the workers of the last run
imported, then forks a worker whenever the binary asks, and the workers share those pages.
"""

import ast
import bisect
import difflib
import faulthandler
import fnmatch
import gc
import importlib
import importlib.util
import json
import os
import pprint
import re
import signal
import socket
import sys
from collections import Counter
from pathlib import Path

import pytest
from _pytest.runner import collect_one_node

HARD_TIMEOUT_GRACE = 10
SNAPSHOT_LINE = 88  # ruff's default line length: written entries come out already formatted


class Snapshot:
    """`snapshot.assert_match(value, name="")`, keyed as snapshottest keys it."""

    def __init__(self, plugin, request):
        node = request.node
        self.plugin = plugin
        self.file = str(node.path.parent / "snapshots" / f"snap_{node.path.stem}.py")
        cls = getattr(node.cls, "__name__", "")
        self.prefix = (f"{cls}." if cls else "") + re.sub(r"\s+", " ", node.name.replace(r"\n", " "))
        self.counter = 1

    def assert_match(self, value, name=""):
        __tracebackhide__ = True
        key = f"{self.prefix} {name or self.counter}"
        if not name:
            self.counter += 1
        self.plugin.match_snapshot(self.file, key, value)


def read_snapshots(file):
    """{key: (value, source of its statement)} from a snapshot file, without importing it."""
    try:
        with open(file, encoding="utf-8") as f:
            src = f.read()
    except FileNotFoundError:
        return {}
    entries = {}
    for node in ast.parse(src).body:
        target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == "snapshots":
            entries[ast.literal_eval(target.slice)] = (ast.literal_eval(node.value), ast.get_source_segment(src, node))
    return entries


def snapshot_literal(value):
    if isinstance(value, str) and "\n" in value:
        s = value.replace("\\", "\\\\").replace("\r", "\\r").replace('"""', '\\"\\"\\"')
        return '"""' + (s[:-1] + '\\"' if s.endswith('"') else s) + '"""'
    if isinstance(value, (str, bytes)):
        b = "b" if isinstance(value, bytes) else ""
        r = repr(value)[len(b):]
        if r[0] == "'" and '"' not in r:
            r = '"' + r[1:-1].replace("\\'", "'") + '"'
        return b + r
    return repr(value)


def snapshot_statement(key, value):
    """The entry as ruff formats it, so a written file passes `ruff format --check`."""
    k, v = snapshot_literal(key), snapshot_literal(value)
    flat = f"snapshots[{k}] = {v}"
    if len(flat.split("\n")[0]) <= SNAPSHOT_LINE:
        return flat
    if "\n" not in v and len(f"snapshots[{k}] = (") <= SNAPSHOT_LINE:
        return f"snapshots[{k}] = (\n    {v}\n)"
    return f"snapshots[\n    {k}\n] = {v}"


def snapshot_diff(key, expected, actual):
    if not (isinstance(expected, str) and isinstance(actual, str)):
        expected, actual = pprint.pformat(expected), pprint.pformat(actual)
    diff = difflib.unified_diff(expected.splitlines(), actual.splitlines(), "snapshot", "actual", lineterm="")
    return f"snapshot {key!r} does not match (--snapshot-update accepts the new value):\n" + "\n".join(diff)


class TurboWorker:
    def __init__(self, events, inbox):
        self.events = events
        self.inbox = inbox
        self.where = {}
        self.results = {}
        self.stderr = os.fdopen(os.dup(2), "w")  # pytest's fd capture swaps fd 2 while a test runs
        self.__name__ = "pytest_turbo"  # the name pluggy registers it under: get_plugin("pytest_turbo")
        self.snapshot_files = {}
        self.path_cache = {}  # the session's first level, as Session.collect keeps it within one collection
        self.dir_reports = {}  # directory node -> its collect report, kept for the whole run
        self.task_path = None
        self.collecting = None

    def collect_file(self, session, path):
        """session.perform_collect([path]), keeping directory nodes between tasks.

        perform_collect starts from scratch on each call: every directory from the rootdir down is
        listed again and its nodes rebuilt, which costs more than many test files take to run.
        """
        arg = Path(os.path.abspath(session.config.invocation_params.dir / path))
        module = "::" not in path and arg.is_file() and self.find_module(session, arg)
        if not module:
            return list(session.perform_collect([path]))
        session._initialpaths = frozenset([arg])
        session._initialpaths_with_parents = frozenset([arg, *arg.parents])
        session._collection_cache = {}  # so genitems collects the module again, with fresh items
        session.items = items = list(session.genitems(module))
        try:
            session.config.hook.pytest_collection_modifyitems(session=session, config=session.config, items=items)
        finally:
            session.config.hook.pytest_collection_finish(session=session)
        session.testscollected = len(items)
        return items

    def find_module(self, session, arg):
        """The collector for a test file, through cached directory nodes; None when it isn't one of them."""
        parts = [arg]
        for parent in arg.parents:
            if not session.config.pluginmanager._is_in_confcutdir(parent):
                break
            parts.insert(0, parent)
        nodes = session._collect_path(parts[0], self.path_cache)
        for i, part in enumerate(parts):
            node = next((n for n in nodes if n.path == part), None)
            if node is None or i == len(parts) - 1:
                return node
            rep = self.dir_reports.get(node)
            if rep is None:
                rep = self.dir_reports[node] = collect_one_node(node)
                if not rep.passed:
                    node.ihook.pytest_collectreport(report=rep)
            if not rep.passed:
                return None
            nodes = rep.result
        return None

    def send(self, **msg):
        self.events.write(json.dumps(msg) + "\n")
        self.events.flush()

    def next_task(self, session):
        while True:
            self.send(type="next")
            line = self.inbox.readline().rstrip("\n")
            if not line:
                return None
            path, cuts, chunk, only, attempt, start = line.split("\t")
            self.collecting = path
            try:
                items = self.collect_file(session, path)
            except Exception as exc:  # usage errors and the like, outside pytest's own reporting
                self.send(type="collect_error", path=path, error=repr(exc))
                items = []
            self.collecting = None
            bounds = [0, *(min(int(c), len(items)) for c in cuts.split(",") if c), len(items)]
            mine, owner = [], {}
            for i, item in enumerate(items):
                item.turbo_attempt = int(attempt)
                if only:
                    if item.nodeid == only:
                        self.where[item.nodeid] = (path, i)
                        mine.append(item)
                    continue
                if i < int(start):
                    continue
                at = bisect.bisect_right(bounds, i) - 1
                group = item.get_closest_marker("xdist_group")
                if group:  # as with --dist loadgroup, a group runs on one worker: the chunk of its first test
                    at = owner.setdefault(group.args[0] if group.args else group.kwargs.get("name"), at)
                if at == int(chunk):
                    self.where[item.nodeid] = (path, i)
                    mine.append(item)
            if mine:
                return (path, int(chunk), only), mine
            self.send(type="done", path=path, chunk=int(chunk), only=only)

    def pytest_addoption(self, parser):
        group = parser.getgroup("pytest-turbo")
        group.addoption("--reruns", type=int, default=0, help="Run a failed test again, up to this many times")
        group.addoption("--only-rerun", action="append", default=[], help="Rerun only failures matching this regex")
        group.addoption("--timeout", type=float, default=None, help="Fail a test after this many seconds")
        parser.addini("timeout", "Fail a test after this many seconds", default=None)
        group.addoption("--snapshot-update", action="store_true", help="Write snapshots instead of comparing them")

    def pytest_configure(self, config):
        self.config = config
        self.rootpath = config.rootpath
        self.snapshot_update = config.getoption("snapshot_update")
        self.reruns = config.getoption("reruns")
        self.only_rerun = [re.compile(p) for p in config.getoption("only_rerun")]
        timeout = config.getoption("timeout")
        self.timeout = float(timeout if timeout is not None else config.getini("timeout") or 0)
        config.addinivalue_line("markers", "timeout(seconds): fail the test after this many seconds")
        signal.signal(signal.SIGALRM, self.on_timeout)

    def snapshot(self, request):
        """A Snapshot for the requesting node: what the `snapshot` fixture returns, for class-scoped fixtures too."""
        return Snapshot(self, request)

    @pytest.fixture(name="snapshot")
    def snapshot_fixture(self, request):
        return Snapshot(self, request)

    def match_snapshot(self, file, key, value):
        __tracebackhide__ = True
        entries = self.snapshot_files.get(file)
        if entries is None:
            entries = self.snapshot_files[file] = read_snapshots(file)
            if self.snapshot_update:
                self.send(type="snapfile", file=file, path=self.task_path, entries={k: src for k, (_, src) in entries.items()})
        old = entries.get(key)
        if self.snapshot_update:
            same = old is not None and type(old[0]) is type(value) and old[0] == value
            self.send(type="snapshot", file=file, path=self.task_path, key=key, entry=None if same else snapshot_statement(key, value))
        elif old is None:
            raise AssertionError(f"no snapshot {key!r} in {os.path.relpath(file)} (--snapshot-update writes it)")
        elif old[0] != value:
            raise AssertionError(snapshot_diff(key, old[0], value))

    def pytest_deselected(self, items):
        if self.collecting and self.snapshot_update:  # -k/-m: the file's unvisited snapshots may belong to deselected tests
            self.send(type="partial", path=self.collecting)

    def on_timeout(self, signum, frame):
        pytest.fail(f"Timeout (>{self.current_timeout}s) from pytest-turbo.")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        index = self.where.get(item.nodeid, (None, None))[1]
        self.send(type="start", index=index, nodeid=item.nodeid, reruns=self.reruns - item.turbo_attempt)
        marker = item.get_closest_marker("timeout")
        self.current_timeout = float(marker.args[0]) if marker and marker.args else self.timeout
        if not self.current_timeout:
            return (yield)
        # SIGALRM fails the test where it is; when it can't (a C call holding the GIL), the
        # faulthandler thread prints every stack and ends the worker, which the controller reports.
        signal.setitimer(signal.ITIMER_REAL, self.current_timeout)
        faulthandler.dump_traceback_later(self.current_timeout + HARD_TIMEOUT_GRACE, exit=True, file=self.stderr)
        try:
            return (yield)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            faulthandler.cancel_dump_traceback_later()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        if report.failed and not hasattr(report, "wasxfail") and item.turbo_attempt < self.reruns and self.rerunnable(call.excinfo):
            report.outcome = "rerun"
        elif report.when == "call" and report.passed and self.reruns > item.turbo_attempt and not self.only_rerun:
            if self.results.get(item.nodeid, {}).get("subtests", {}).get("failed"):
                report.outcome = "rerun"
        return report

    def rerunnable(self, excinfo):
        if not self.only_rerun:
            return True
        exc, seen = excinfo and excinfo.value, set()
        while exc is not None and id(exc) not in seen:  # the exception chain, as pytest-rerunfailures matches it
            seen.add(id(exc))
            if any(p.search(f"{type(exc).__name__}: {exc}") for p in self.only_rerun):
                return True
            exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        return False

    def pytest_report_teststatus(self, report):
        if report.outcome == "rerun":
            return "rerun", "R", ("RERUN", {"yellow": True})

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session):
        session.items = []
        return True

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        current = self.next_task(session)
        while current:
            task, items = current
            self.task_path = task[0]
            # Collect the next task before the last item's teardown, so fixtures it
            # shares with the next item (session scope) stay up.
            upcoming = None
            for i, item in enumerate(items):
                if i == len(items) - 1:
                    upcoming = self.next_task(session)
                nextitem = items[i + 1] if i + 1 < len(items) else (upcoming[1][0] if upcoming else None)
                item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
                if session.shouldfail or session.shouldstop:
                    return True
            path, chunk, only = task
            self.send(type="done", path=path, chunk=chunk, only=only)
            current = upcoming
        return True

    def pytest_collectreport(self, report):
        if report.failed:
            self.send(type="collect_error", path=report.nodeid, error=report.longreprtext)

    def pytest_runtest_logreport(self, report):
        r = self.results.setdefault(
            report.nodeid, {"outcome": "passed", "duration": 0.0, "failures": [], "subtests": Counter()}
        )
        r["duration"] += report.duration
        if type(report).__name__ == "SubtestReport":
            r["subtests"][report.outcome] += 1
            if report.failed:
                r["failures"].append(self.failure(report, f"subtest {report.context.msg or ''} {report.context.kwargs}"))
            return
        if report.outcome == "rerun":
            r["rerun"] = True
            return
        xfail = hasattr(report, "wasxfail")
        if report.when == "call":
            if report.passed:
                r["outcome"] = "xpassed" if xfail else "passed"
            elif report.skipped:
                r["outcome"] = "xfailed" if xfail else "skipped"
        elif report.skipped:
            r["outcome"] = "skipped"
        if report.failed:
            r["outcome"] = "failed" if report.when == "call" else "error"
            r["failures"].append(self.failure(report, report.when))

    @staticmethod
    def failure(report, when):
        return {"when": when, "text": report.longreprtext, "sections": report.sections}

    def imported(self):
        """Modules to preload next time: all but test modules and conftests, whose asserts pytest rewrites on import."""
        patterns = [*self.config.getini("python_files"), "conftest.py"]
        modules = []
        for name, module in list(sys.modules.items()):
            file = getattr(module, "__file__", None)
            if module is None or name == "__main__" or type(getattr(module, "__loader__", None)).__name__ == "AssertionRewritingHook":
                continue
            if not any(fnmatch.fnmatch(os.path.basename(file or ""), p) for p in patterns):
                modules.append([name, file])
        return {"modules": modules}

    def pytest_runtest_logfinish(self, nodeid):
        r = self.results.get(nodeid)
        if r is None:
            return
        del self.results[nodeid]
        path, index = self.where.pop(nodeid, (None, None))
        if r.get("rerun"):
            self.send(type="rerun", nodeid=nodeid, path=path)
        else:
            self.send(type="test", nodeid=nodeid, path=path, index=index, **r)


def pytest_args():
    # pytest-rerunfailures, pytest-timeout and snapshottest clash with TurboWorker's options and fixture.
    blocked = ["-p", "no:rerunfailures", "-p", "no:timeout", "-p", "no:snapshottest"]
    return ["-p", "no:cacheprovider", *blocked, *sys.argv[1:]]


def load_initial_conftests():
    """Run the conftests a worker loads before collecting, for what they set up on import (environment variables)."""
    from _pytest.config import _prepareconfig

    try:
        config = _prepareconfig(pytest_args(), plugins=[TurboWorker(None, None)])
    except BaseException as exc:  # the workers will report it
        print(f"pytest-turbo: loading the conftests failed: {exc!r}", file=sys.stderr)
        return
    config._ensure_unconfigure()  # removes pytest's import hook and fd capture


def preload(saved):
    if not saved:
        return
    load_initial_conftests()
    # Only what resolves to the same file without the test directories pytest adds to sys.path: what those
    # provide (helpers, the many packages named "tests") depends on which test files a worker collects.
    for name, file in saved["modules"]:
        try:
            spec = importlib.util.find_spec(name)
            if spec is not None and spec.origin == file:
                importlib.import_module(name)
        except (Exception, SystemExit):  # gone since the last run: the worker imports what it needs itself
            pass
    gc.collect()
    gc.freeze()  # otherwise every collection in a worker writes to the shared objects and copies their pages


def serve_forks(address):
    """Returns only in a forked worker, with its event and task streams."""
    preload(json.loads(sys.stdin.readline()))
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # the kernel reaps the workers
    for line in sys.stdin:
        command = json.loads(line)
        if "kill" in command:
            try:
                os.kill(command["kill"], signal.SIGKILL)
            except OSError:
                pass
        elif os.fork() == 0:
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            wid = command["fork"]
            os.environ["PYTEST_XDIST_WORKER"] = f"gw{wid}"
            if command["log"]:
                os.dup2(os.open(command["log"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC), 2)
            sock = socket.socket(socket.AF_UNIX)
            sock.connect(address)
            events = sock.makefile("w", buffering=1, encoding="utf-8")
            events.write(json.dumps({"type": "hello", "wid": wid, "pid": os.getpid()}) + "\n")
            return events, sock.makefile("r", encoding="utf-8")
    sys.exit(0)


def main():
    sys.argv[0] = os.path.join(os.path.dirname(sys.executable), "pytest")  # code that checks "am I under pytest" by argv[0]
    output = os.environ.pop("PYTEST_TURBO_OUTPUT", None)
    record = os.environ.pop("PYTEST_TURBO_RECORD", None)
    zygote = os.environ.pop("PYTEST_TURBO_ZYGOTE", None)
    if zygote:
        events, inbox = serve_forks(zygote)
    else:
        events = os.fdopen(os.dup(1), "w", buffering=1)
        inbox = os.fdopen(os.dup(0), "r")
    os.dup2(2 if output else os.open(os.devnull, os.O_WRONLY), 1)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    plugin = TurboWorker(events, inbox)
    code = pytest.main(pytest_args(), plugins=[plugin])
    plugin.send(type="exit", code=int(code), **(plugin.imported() if record and hasattr(plugin, "config") else {}))


main()
