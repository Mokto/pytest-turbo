"""pytest-turbo worker: one pytest session, fed tasks over a pipe.

A task line is "path<TAB>cuts<TAB>chunk": collect the whole file, run items[cuts[chunk-1]:cuts[chunk]].
An empty line means no more work. Events go out as JSON lines.
"""

import json
import os
import sys
from collections import Counter

import pytest


class TurboWorker:
    def __init__(self, events, inbox):
        self.events = events
        self.inbox = inbox
        self.where = {}
        self.results = {}

    def send(self, **msg):
        self.events.write(json.dumps(msg) + "\n")
        self.events.flush()

    def next_task(self, session):
        while True:
            self.send(type="next")
            line = self.inbox.readline().rstrip("\n")
            if not line:
                return None
            path, cuts, chunk = line.split("\t")
            try:
                items = list(session.perform_collect([path]))
            except Exception as exc:  # usage errors and the like, outside pytest's own reporting
                self.send(type="collect_error", path=path, error=repr(exc))
                items = []
            bounds = [0, *(min(int(c), len(items)) for c in cuts.split(",") if c), len(items)]
            lo, hi = bounds[int(chunk)], bounds[int(chunk) + 1]
            for i, item in enumerate(items[lo:hi], lo):
                self.where[item.nodeid] = (path, i)
            if lo < hi:
                return path, int(chunk), items[lo:hi]
            self.send(type="done", path=path, chunk=int(chunk))

    def pytest_configure(self, config):
        self.rootpath = config.rootpath

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session):
        session.items = []
        return True

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        current = self.next_task(session)
        while current:
            path, chunk, items = current
            # Collect the next task before the last item's teardown, so fixtures it
            # shares with the next item (session scope) stay up.
            upcoming = None
            for i, item in enumerate(items):
                if i == len(items) - 1:
                    upcoming = self.next_task(session)
                nextitem = items[i + 1] if i + 1 < len(items) else (upcoming[2][0] if upcoming else None)
                item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
                if session.shouldfail or session.shouldstop:
                    return True
            self.send(type="done", path=path, chunk=chunk)
            current = upcoming
        return True

    def pytest_collectreport(self, report):
        if report.failed:
            self.send(type="collect_error", path=report.nodeid, error=report.longreprtext)

    def pytest_runtest_logreport(self, report):
        r = self.results.setdefault(
            report.nodeid, {"outcome": "passed", "duration": 0.0, "reruns": 0, "failures": [], "subtests": Counter()}
        )
        r["duration"] += report.duration
        if report.when == "setup":
            r["retrying"] = False
        if type(report).__name__ == "SubtestReport":
            r["subtests"][report.outcome] += 1
            if report.failed:
                r["failures"].append(self.failure(report, f"subtest {report.context.msg or ''} {report.context.kwargs}"))
            return
        if report.outcome == "rerun":  # pytest-rerunfailures: this attempt is discarded
            r.update(outcome="passed", failures=[], subtests=Counter(), reruns=r["reruns"] + 1, retrying=True)
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
        if r.pop("retrying", False):  # pytest-rerunfailures finishes each attempt; wait for the last
            return
        del self.results[nodeid]
        path, index = self.where.pop(nodeid, (None, None))
        self.send(type="test", nodeid=nodeid, path=path, index=index, **r)


def main():
    events = os.fdopen(os.dup(1), "w", buffering=1)
    inbox = os.fdopen(os.dup(0), "r")
    os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    plugin = TurboWorker(events, inbox)
    code = pytest.main(["-p", "no:cacheprovider", *sys.argv[1:]], plugins=[plugin])
    plugin.send(type="exit", code=int(code))


main()
