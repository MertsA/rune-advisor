"""Background jobs for slow planner calls: start, poll, cancel, cache.

Planner calls take seconds to minutes and share mutable caches (a Context memoises as it plans), so jobs run
one at a time on their own threads while the HTTP server stays responsive. Cancellation is cooperative: the
progress callback raises `Cancelled` between planner steps, and a job cancelled mid-step simply has its result
dropped when the step returns.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Callable

Compute = Callable[[str, dict, Callable[[int, int, str], None]], dict]


class Cancelled(Exception):
    """Raised inside a job when it was cancelled."""


class Job:
    def __init__(self, view: str, params: dict, key: str):
        self.id = uuid.uuid4().hex[:12]
        self.view, self.params, self.key = view, params, key
        self.state = "queued"  # queued, running, done, error, cancelled
        self.progress = {"done": 0, "total": 0, "message": ""}
        self.result: dict | None = None
        self.error: str | None = None
        self.cached = False
        self.cancel_requested = False
        self.created = time.time()
        self.finished: float | None = None

    def snapshot(self, with_result: bool = True) -> dict:
        out = {"id": self.id, "view": self.view, "state": self.state, "progress": dict(self.progress),
               "cached": self.cached, "elapsed": round((self.finished or time.time()) - self.created, 1)}
        if self.state == "error":
            out["error"] = self.error
        if self.state == "done" and with_result:
            out["result"] = self.result
        return out


class JobManager:
    MAX_JOBS = 50  # finished jobs kept for polling

    def __init__(self, compute: Compute):
        self._compute = compute
        self._jobs: dict[str, Job] = {}
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()  # guards the tables
        self._run_lock = threading.Lock()  # one planner call at a time

    @staticmethod
    def key_of(view: str, params: dict) -> str:
        return json.dumps([view, params], sort_keys=True)

    def submit(self, view: str, params: dict, refresh: bool = False) -> Job:
        """A job for (view, params): answered from the cache unless `refresh`, else computed in the background."""
        job = Job(view, params, self.key_of(view, params))
        with self._lock:
            if not refresh and job.key in self._cache:
                job.state, job.result, job.cached, job.finished = "done", self._cache[job.key], True, time.time()
                self._remember(job)
                return job
            self._remember(job)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _remember(self, job: Job) -> None:
        self._jobs[job.id] = job
        for old in [j for j in self._jobs.values() if j.state in ("done", "error", "cancelled")][:-self.MAX_JOBS]:
            self._jobs.pop(old.id, None)

    def _run(self, job: Job) -> None:
        def progress(done: int, total: int, message: str) -> None:
            if job.cancel_requested:
                raise Cancelled()
            job.progress = {"done": done, "total": total, "message": message}

        with self._run_lock:
            if job.cancel_requested:
                job.state, job.finished = "cancelled", time.time()
                return
            job.state = "running"
            try:
                result = self._compute(job.view, job.params, progress)
            except Cancelled:
                result = None
            except Exception as e:  # shown to the user, so keep it to one readable line
                if not job.cancel_requested:
                    job.error = str(e) or type(e).__name__
                    job.state, job.finished = "error", time.time()
                    return
                result = None
        job.finished = time.time()
        if job.cancel_requested or result is None:
            job.state = "cancelled"  # a result that arrives after a cancel is discarded
            return
        with self._lock:
            self._cache[job.key] = result
        job.result, job.state = result, "done"

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job and job.state in ("queued", "running"):
            job.cancel_requested = True  # a running step finishes unseen; its result is dropped
            job.state, job.finished = "cancelled", time.time()
        return job

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
