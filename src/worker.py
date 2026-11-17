"""pytest-turbo worker: one pytest session, fed tasks over a pipe.

A task line is "path<TAB>cuts<TAB>chunk<TAB>nodeid<TAB>attempt<TAB>from": collect the whole file,
run items[cuts[chunk-1]:cuts[chunk]] from index `from` on, or only `nodeid` when it is set (a rerun).
An empty line means no more work. Events go out as JSON lines.

Reruns and timeouts replace pytest-rerunfailures and pytest-timeout, with the same options:
`--reruns`, `--only-rerun`, `--timeout`, the `timeout` ini key and `@pytest.mark.timeout`.
A failed attempt is reported with outcome "rerun", as pytest-rerunfailures does, and the
controller runs it again as its own task.
"""

import bisect
import faulthandler
import json
import os
import re
import signal
import sys
from collections import Counter

import pytest

HARD_TIMEOUT_GRACE = 10


class TurboWorker:
    def __init__(self, events, inbox):
        self.events = events
        self.inbox = inbox
        self.where = {}
        self.results = {}
        self.stderr = os.fdopen(os.dup(2), "w")  # pytest's fd capture swaps fd 2 while a test runs

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
            try:
                items = list(session.perform_collect([path]))
            except Exception as exc:  # usage errors and the like, outside pytest's own reporting
                self.send(type="collect_error", path=path, error=repr(exc))
                items = []
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

    def pytest_configure(self, config):
        self.rootpath = config.rootpath
        self.reruns = config.getoption("reruns")
        self.only_rerun = [re.compile(p) for p in config.getoption("only_rerun")]
        timeout = config.getoption("timeout")
        self.timeout = float(timeout if timeout is not None else config.getini("timeout") or 0)
        config.addinivalue_line("markers", "timeout(seconds): fail the test after this many seconds")
        signal.signal(signal.SIGALRM, self.on_timeout)

    def on_timeout(self, signum, frame):
        pytest.fail(f"Timeout (>{self.current_timeout}s) from pytest-turbo.")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        self.send(type="start", index=self.where.get(item.nodeid, (None, None))[1], nodeid=item.nodeid)
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


def main():
    sys.argv[0] = os.path.join(os.path.dirname(sys.executable), "pytest")  # code that checks "am I under pytest" by argv[0]
    events = os.fdopen(os.dup(1), "w", buffering=1)
    inbox = os.fdopen(os.dup(0), "r")
    os.dup2(2 if os.environ.pop("PYTEST_TURBO_OUTPUT", None) else os.open(os.devnull, os.O_WRONLY), 1)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    plugin = TurboWorker(events, inbox)
    # pytest-rerunfailures and pytest-timeout would rerun and time out a second time, and clash with the options above.
    code = pytest.main(["-p", "no:cacheprovider", "-p", "no:rerunfailures", "-p", "no:timeout", *sys.argv[1:]], plugins=[plugin])
    plugin.send(type="exit", code=int(code))


main()
