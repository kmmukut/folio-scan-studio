"""Background tasks for slow work: PDF export, OCR and print preparation.

A request that could outlast a reverse proxy's timeout starts a task and
returns at once; the browser polls for progress, then downloads the file or
reads the print receipt. Each lane runs one task at a time, so a long OCR
export never holds up a print, and a small host never runs more than one
heavy job per lane.

Tasks live in memory. A restart forgets them and removes their files at
startup; the browser reports that as an interrupted job to retry.

Each person may have a limited number of tasks waiting or running (their
job limit, set by administrators; see security.job_limit_for), and all of
them together at most MAX_ACTIVE, so no one can fill the queue or the disk
for everyone. Finished tasks are kept for download per person too, so one
busy account can't push out someone else's finished export.
"""
from __future__ import annotations

import logging
import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

LANES = ('print', 'export')
RETENTION_SECONDS = 30 * 60   # Finished tasks and their files are kept this long
MAX_FINISHED = 40             # ...and at most this many, oldest removed first
MAX_FINISHED_PER_OWNER = 10   # ...of which at most this many are one person's
MAX_ACTIVE = 50               # Everyone's tasks waiting or running: a backstop for the disk
FINISHED = ('done', 'failed', 'cancelled')
log = logging.getLogger('folio.tasks')


class Cancelled(Exception):
    """Raised inside a task's work when its owner asked to cancel it."""


class Busy(Exception):
    """Too many tasks are waiting; the message says why, for the person."""


@dataclass
class Task:
    id: str
    kind: str
    lane: str
    owner: str
    directory: Path
    work: Callable[['Task'], Any]
    status: str = 'queued'
    message: str = 'Waiting to start…'
    progress: float | None = None
    result: Any = None
    error: str | None = None
    file: Path | None = None
    filename: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def update(self, message: str | None = None, progress: float | None = None) -> None:
        """Report progress (0–1). Also the cancellation point for cooperative work."""
        self.check_cancelled()
        if message is not None:
            self.message = message
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def public(self, position: int = 0) -> dict:
        return {'id': self.id, 'kind': self.kind, 'status': self.status,
                'message': f'Waiting for {position} other job(s) to finish…' if position else self.message,
                'progress': self.progress, 'result': self.result, 'error': self.error,
                'has_file': bool(self.file and self.status == 'done'), 'filename': self.filename,
                'created': self.created, 'finished': self.finished}


def failure_message(exc: Exception) -> str:
    """User-facing reason: HTTPException details are written for people;
    anything else could expose internals, so it is logged instead."""
    detail = getattr(exc, 'detail', None)
    if isinstance(detail, str) and detail:
        return detail
    log.exception('Background task failed', exc_info=exc)
    return 'Something went wrong while preparing this job. Please try again.'


class TaskRunner:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._mutex = threading.Lock()
        self._queues = {lane: queue.Queue() for lane in LANES}
        self._threads: list[threading.Thread] = []
        self.root: Path | None = None

    def start(self, root: Path) -> None:
        """Clear leftovers from a previous run and make sure the lanes are running."""
        with self._mutex:
            for task in self._tasks.values():
                task.cancel_event.set()
            self._tasks.clear()
            self.root = root
            shutil.rmtree(root, ignore_errors=True)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self._threads:
                for lane in LANES:
                    thread = threading.Thread(target=self._worker, args=(lane,), daemon=True,
                                              name=f'folio-{lane}-lane')
                    thread.start()
                    self._threads.append(thread)

    def submit(self, kind: str, lane: str, owner: str, work: Callable[[Task], Any],
               prepare: Callable[[Path], None] | None = None, limit: int | None = None) -> Task:
        """Queue ``work``. ``prepare`` runs first, in the caller's thread, to put
        inputs into the task's private directory before any worker can start.

        ``limit``: how many tasks ``owner`` may have waiting or running (None:
        no limit of their own). Raises Busy, before anything is written, when
        that or MAX_ACTIVE is reached."""
        if lane not in LANES or self.root is None:
            raise RuntimeError('Task runner is not started')
        self.prune()
        task_id = uuid4().hex
        task = Task(id=task_id, kind=kind, lane=lane, owner=owner,
                    directory=self.root / task_id, work=work)
        with self._mutex:  # Counted and reserved together, so two requests can't both take the last place
            active = [t for t in self._tasks.values() if t.status not in FINISHED]
            mine = sum(t.owner == owner for t in active)
            if limit is not None and mine >= limit:
                raise Busy(f"You already have {mine} {'job' if mine == 1 else 'jobs'} waiting or running "
                           f'(your limit is {limit}). Wait for one to finish, or cancel one.')
            if len(active) >= MAX_ACTIVE:
                raise Busy('Folio is busy with many jobs right now. Try again in a minute.')
            self._tasks[task_id] = task
        try:
            task.directory.mkdir(mode=0o700)
            if prepare:
                prepare(task.directory)
        except BaseException:
            with self._mutex:
                self._tasks.pop(task_id, None)
            shutil.rmtree(task.directory, ignore_errors=True)
            raise
        self._queues[lane].put(task)
        return task

    def get(self, task_id: str, owner: str) -> Task | None:
        with self._mutex:
            task = self._tasks.get(task_id)
        # Someone else's task is indistinguishable from a missing one.
        return task if task and task.owner == owner else None

    def position(self, task: Task) -> int:
        """How many queued or running tasks in the same lane are ahead of this one."""
        if task.status != 'queued':
            return 0
        with self._mutex:
            return sum(1 for other in self._tasks.values()
                       if other.lane == task.lane and other.created <= task.created
                       and other.id != task.id and other.status in ('queued', 'running'))

    def cancel(self, task: Task) -> None:
        task.cancel_event.set()
        if task.status == 'queued':
            self._finish(task, 'cancelled', 'Cancelled')
        elif task.status == 'running':
            task.message = 'Cancelling…'

    def _finish(self, task: Task, status: str, message: str) -> None:
        task.status = status
        task.message = message
        task.finished = time.time()
        if status != 'done':
            task.file = None
            shutil.rmtree(task.directory, ignore_errors=True)

    def _worker(self, lane: str) -> None:
        while True:
            task = self._queues[lane].get()
            try:
                if task.status != 'queued':
                    continue  # Cancelled while waiting
                task.status = 'running'
                task.message = 'Starting…'
                try:
                    task.check_cancelled()
                    task.result = task.work(task)
                    task.progress = 1.0
                    self._finish(task, 'done', 'Done')
                except Cancelled:
                    self._finish(task, 'cancelled', 'Cancelled')
                except Exception as exc:  # noqa: BLE001 - reported to the owner
                    task.error = failure_message(exc)
                    self._finish(task, 'failed', task.error)
            finally:
                self._queues[lane].task_done()

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._mutex:
            finished = sorted((t for t in self._tasks.values() if t.status in FINISHED),
                              key=lambda t: t.finished or 0)
            expired = [t for t in finished if now - (t.finished or now) > RETENTION_SECONDS]
            kept = [t for t in finished if t not in expired]
            by_owner: dict[str, list[Task]] = {}
            for task in kept:
                by_owner.setdefault(task.owner, []).append(task)
            for owned in by_owner.values():  # Each person's oldest first, beyond their share
                expired += owned[:max(0, len(owned) - MAX_FINISHED_PER_OWNER)]
            kept = [t for t in kept if t not in expired]
            expired += kept[:max(0, len(kept) - MAX_FINISHED)]
            for task in expired:
                self._tasks.pop(task.id, None)
        for task in expired:
            shutil.rmtree(task.directory, ignore_errors=True)


runner = TaskRunner()
