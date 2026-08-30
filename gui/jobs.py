"""The job model and the registry of jobs currently running.

A job is one run of one long-running operation. It holds everything the panel
renders — status, percentage, a message, per-step rows, an error — and the
callables its Cancel and Stop buttons need.

Lifecycle
---------

``starting`` → ``running`` → one of ``completed``, ``failed``, ``cancelled``.

Nothing else is a status. A slow poll, a reopened conversation or a missing record
are conditions on the reader's side, and none of them is a state a job can be in.

Every change persists the whole snapshot through :mod:`gui.store`, which is the
source of truth. The registry below is a runtime cache: it exists so a Cancel
button has an object to call, and so a reader mid-operation does not have to wait
for the next throttled write. A job writes its final snapshot *before* leaving the
registry, so there is no instant at which neither holds it.

Jobs are mutated from a worker thread and read from whichever thread answers a
poll, so each guards its fields with a lock and hands out copies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import threading
import time
from typing import Any, Callable
import uuid

from . import store

STARTING = "starting"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})

# Step states. A step is a row on the panel, not a job.
WAITING = "waiting"
SKIPPED = "skipped"

# Shortest gap between two writes for a running job. Progress arrives several
# times a second; the record is only read when someone polls.
WRITE_INTERVAL = 0.5


def new_job_id(kind: str) -> str:
    """Mint an identifier for one run.

    Args:
        kind: Operation type, ``download`` or ``benchmark``.

    Returns:
        str: ``<kind>-<timestamp>-<random>``. Sortable, unique across processes,
        and safe as a file name.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    return f"{kind}-{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class Step:
    """One unit of work inside a job, shown as its own row."""

    name: str
    state: str = WAITING
    percent: float | None = None
    detail: str | None = None
    error: str | None = None
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        """Render the step for the panel.

        Returns:
            dict[str, Any]: Step fields, percentage clamped to 0-100.
        """
        return {
            "name": self.name,
            "state": self.state,
            "percent": _clamp(self.percent),
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class Metric:
    """One labelled figure shown under the bar, for example bytes transferred."""

    label: str
    value: str


def _clamp(percent: float | None) -> float | None:
    """Constrain a percentage to 0-100.

    Args:
        percent: Raw percentage, possibly None or out of range.

    Returns:
        float | None: Rounded percentage, or None when the input was None.
    """
    if percent is None:
        return None

    return round(min(100.0, max(0.0, float(percent))), 1)


def _weighted_percent(steps: list[Step]) -> float | None:
    """Derive a job's percentage from its steps.

    Args:
        steps: The job's steps.

    Returns:
        float | None: Weighted completion, or None when there are no steps.
    """
    if not steps:
        return None

    total = sum(step.weight for step in steps) or float(len(steps))
    done = 0.0

    for step in steps:
        if step.state in (COMPLETED, SKIPPED):
            fraction = 1.0
        else:
            # A failed or running step counts only as far as it got.
            fraction = (step.percent or 0.0) / 100.0

        done += fraction * step.weight

    return 100.0 * done / total


class Job:
    """One run of a tracked operation."""

    def __init__(
        self,
        kind: str,
        title: str,
        message: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Create a job in the ``starting`` state and persist it.

        The record exists before this returns, so a poll arriving between the tool
        call being issued and the operation actually beginning already finds a real
        snapshot rather than nothing.

        Args:
            kind: Operation type, ``download`` or ``benchmark``.
            title: Headline shown on the panel.
            message: Optional secondary line.
            session_id: Download session this job belongs to, for a download. It
                is how the plain download tools reach the right progress bar; it
                is never an identifier the panel uses.
        """
        self.id = new_job_id(kind)
        self.kind = kind
        self.title = title
        self.session_id = session_id

        self._status = STARTING
        self._message = message
        self._error: str | None = None
        self._percent: float | None = None
        self._steps: list[Step] = []
        self._metrics: list[Metric] = []
        self._paused = False
        self._cancelling = False
        # The operation's own return value, set once when it finishes. It is kept
        # out of every snapshot — a comparison result is far larger than the
        # progress around it — and served by its own retrieval tool.
        self._result: Any | None = None

        self._started_at = time.time()
        self._finished_at: float | None = None
        self._written_at = 0.0

        self._cancel: Callable[[str], None] | None = None
        self._pause: Callable[[], None] | None = None
        self._resume: Callable[[], None] | None = None

        self._lock = threading.RLock()
        self._done = threading.Event()

        self._write()
        registry.add(self)

    # ========================================================
    # Progress — called from the worker
    # ========================================================

    def begin(self, message: str | None = None) -> None:
        """Move the job from ``starting`` to ``running``.

        Args:
            message: Optional replacement for the secondary line.
        """
        with self._lock:
            if self._status in TERMINAL:
                return

            self._status = RUNNING

            if message is not None:
                self._message = message

        self.publish(force=True)

    def add_steps(self, names: list[str], weight: float = 1.0) -> None:
        """Append waiting steps.

        Args:
            names: Step names, in the order they will run.
            weight: Relative share of the job each step accounts for.
        """
        with self._lock:
            if self._status in TERMINAL:
                return

            self._steps.extend(Step(name=name, weight=weight) for name in names)

    def start_step(self, index: int, detail: str | None = None) -> None:
        """Mark a step as running.

        Args:
            index: Zero-based index; out of range is ignored.
            detail: Optional short status text.
        """
        with self._lock:
            step = self._step(index)

            if step is None:
                return

            step.state = RUNNING
            step.percent = step.percent if step.percent is not None else 0.0

            if detail is not None:
                step.detail = detail

    def update_step(
        self,
        index: int,
        percent: float | None = None,
        detail: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update a running step.

        Args:
            index: Zero-based index.
            percent: Step completion from 0 to 100.
            detail: Short status text.
            error: Error line shown under the row.
        """
        with self._lock:
            step = self._step(index)

            if step is None:
                return

            if percent is not None:
                step.percent = percent
            if detail is not None:
                step.detail = detail
            if error is not None:
                step.error = error

    def finish_step(
        self,
        index: int,
        state: str = COMPLETED,
        detail: str | None = None,
        error: str | None = None,
    ) -> None:
        """Mark a step as finished.

        Args:
            index: Zero-based index.
            state: ``completed``, ``failed``, ``skipped`` or ``cancelled``.
            detail: Short status text.
            error: Error line shown under the row.
        """
        with self._lock:
            step = self._step(index)

            if step is None:
                return

            step.state = state
            step.percent = 100.0 if state == COMPLETED else step.percent

            if detail is not None:
                step.detail = detail
            if error is not None:
                step.error = error

    def set_percent(self, percent: float | None) -> None:
        """Set the overall percentage, overriding the one derived from steps.

        Args:
            percent: Completion from 0 to 100, or None for indeterminate.
        """
        with self._lock:
            if self._status in TERMINAL:
                return

            self._percent = percent

    def set_metrics(self, metrics: list[Metric]) -> None:
        """Replace the figures shown under the bar.

        Args:
            metrics: Metrics to show.
        """
        with self._lock:
            if self._status in TERMINAL:
                return

            self._metrics = list(metrics)

    def _step(self, index: int) -> Step | None:
        """Look up a step while the lock is held.

        A finished job hands back nothing: its rows describe what happened, and a
        watcher draining a late reading must not reopen them.

        Args:
            index: Zero-based index.

        Returns:
            Step | None: The step, or None when out of range or already finished.
        """
        if self._status in TERMINAL:
            return None

        if 0 <= index < len(self._steps):
            return self._steps[index]

        return None

    def step_count(self) -> int:
        """int: How many steps the job has."""
        with self._lock:
            return len(self._steps)

    # ========================================================
    # Persisting
    # ========================================================

    def publish(self, force: bool = False) -> None:
        """Write the current snapshot, at most twice a second unless forced.

        Args:
            force: Write regardless of when the last write happened.
        """
        with self._lock:
            now = time.monotonic()

            if not force and now - self._written_at < WRITE_INTERVAL:
                return

            self._written_at = now
            self._write()

    def _write(self) -> None:
        """Persist the snapshot while the lock is held."""
        store.save(self._snapshot())

    # ========================================================
    # Finishing — the single terminal path
    # ========================================================

    def finish(
        self,
        status: str,
        message: str | None = None,
        error: str | None = None,
        result: Any | None = None,
    ) -> None:
        """Assign the job's one terminal status, persist it, and deregister.

        Idempotent: the first terminal status wins, so a watcher draining a late
        reading cannot overwrite a cancellation with "completed".

        The order matters. The result is stored first, then the final snapshot —
        which is what advertises the result — and only then is the job deregistered.
        A reader therefore never sees ``result_available`` true with nothing to
        fetch, nor loses the job between memory and disk.

        Args:
            status: ``completed``, ``failed`` or ``cancelled``.
            message: Closing line for the panel.
            error: Error text, for a failure.
            result: The operation's own return value, for a run that produces one.
        """
        with self._lock:
            if self._status in TERMINAL:
                return

            self._status = status
            self._finished_at = time.time()
            self._cancelling = False
            self._paused = False
            self._result = result

            if message is not None:
                self._message = message
            if error is not None:
                self._error = error

            if status == COMPLETED:
                self._percent = 100.0

            if status == CANCELLED:
                for step in self._steps:
                    if step.state in (WAITING, RUNNING):
                        step.state = CANCELLED
                        step.detail = "cancelled"

            # These close over the operation, so clearing them lets it be
            # collected and makes a late Cancel a no-op.
            self._cancel = None
            self._pause = None
            self._resume = None

            if result is not None:
                store.save_result(self.id, result)

            self._write()

        self._done.set()
        registry.remove(self.id)
        store.prune()

    def result(self) -> Any | None:
        """Return the operation's own return value.

        Read from memory while the job is still registered and from its stored file
        afterwards, so the same call works during the run — where it is None — and
        long after.

        Returns:
            Any | None: The result, or None when the run produced none.
        """
        with self._lock:
            if self._result is not None:
                return self._result

        return store.load_result(self.id)

    def wait(self, timeout: float) -> bool:
        """Block until the job reaches a terminal status.

        Args:
            timeout: Seconds to wait at most.

        Returns:
            bool: True if the job finished within the timeout.
        """
        return self._done.wait(timeout)

    # ========================================================
    # Controls
    # ========================================================

    def set_cancel(self, cancel: Callable[[str], None]) -> None:
        """Register how the operation is cancelled.

        Applied immediately if a cancellation was requested before the operation
        got far enough to register one.

        Args:
            cancel: Callable taking a reason.
        """
        with self._lock:
            if self._status in TERMINAL:
                return

            self._cancel = cancel
            pending = self._cancelling

        if pending:
            cancel("Cancelled from the progress panel")

    def set_pause(
        self,
        pause: Callable[[], None],
        resume: Callable[[], None],
    ) -> None:
        """Register how the operation is suspended and continued.

        Downloads only: a transfer can be picked up from its partial data, which a
        benchmark cannot, so benchmarks never register these and the panel offers
        no Stop button for them.

        Args:
            pause: Callable suspending the operation.
            resume: Callable continuing it.
        """
        with self._lock:
            if self._status in TERMINAL:
                return

            self._pause = pause
            self._resume = resume

    def request_cancel(self, reason: str) -> bool:
        """Ask the operation to stop.

        The status stays ``running`` with ``cancelling`` set, and becomes
        ``cancelled`` only once core has actually stopped and cleaned up — which
        the worker reports by calling ``finish``.

        Args:
            reason: Explanation passed to core and shown on the panel.

        Returns:
            bool: True if this call requested the cancellation, False if the job
            was already finishing or already cancelling.
        """
        with self._lock:
            if self._status in TERMINAL or self._cancelling:
                return False

            self._cancelling = True
            self._message = "Cancelling…"
            cancel = self._cancel

            self._write()

        if cancel is not None:
            cancel(reason)

        return True

    def toggle_pause(self) -> str:
        """Suspend a running operation, or continue a suspended one.

        Returns:
            str: ``paused``, ``resumed``, or ``unavailable`` when the operation
            cannot be suspended or has finished.
        """
        with self._lock:
            if self._pause is None or self._status in TERMINAL or self._cancelling:
                return "unavailable"

            if self._paused:
                action, self._paused, outcome = self._resume, False, "resumed"
            else:
                action, self._paused, outcome = self._pause, True, "paused"

            self._write()

        if action is not None:
            action()

        return outcome

    def sync_paused(self, paused: bool) -> None:
        """Adopt the operation's own paused flag.

        Core is the authority: a download can also be suspended by the manager's
        keyboard listener, not only by this panel.

        Args:
            paused: Whether the operation reports itself suspended.
        """
        with self._lock:
            if self._status in TERMINAL:
                return

            self._paused = paused

    # ========================================================
    # Reading
    # ========================================================

    def snapshot(self) -> dict[str, Any]:
        """dict[str, Any]: The current snapshot, as the panel renders it."""
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        """Build the snapshot while the lock is held.

        Returns:
            dict[str, Any]: Everything the panel needs and nothing else.
        """
        percent = self._percent

        if percent is None:
            percent = _weighted_percent(self._steps)

        live = self._status not in TERMINAL
        end = self._finished_at or time.time()

        return {
            "id": self.id,
            "type": self.kind,
            "title": self.title,
            "status": self._status,
            "progress": _clamp(percent),
            "message": self._message,
            "error": self._error,
            # Whether a business result is waiting to be fetched. A benchmark's
            # measurements are not carried in the snapshot: the model retrieves
            # them once, after this turns true.
            "result_available": self._result is not None,
            "steps": [step.as_dict() for step in self._steps],
            "metrics": [
                {"label": metric.label, "value": metric.value}
                for metric in self._metrics
            ],
            "paused": self._paused,
            "cancelling": self._cancelling,
            "can_cancel": live and not self._cancelling,
            "can_pause": live and not self._cancelling and self._pause is not None,
            "elapsed_seconds": round(end - self._started_at, 1),
        }


class JobRegistry:
    """The jobs running now, by identifier.

    A runtime cache, not a second source of truth: it exists so Cancel and Stop
    have an object to call, and so a poll mid-operation reads the live job rather
    than a snapshot up to half a second old. Everything in it is also on disk.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def add(self, job: Job) -> None:
        """Register a running job.

        Args:
            job: Job to hold until it finishes.
        """
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Job | None:
        """Look up a running job by identifier.

        Never returns a different job: an unknown identifier is unknown.

        Args:
            job_id: Identifier of the run.

        Returns:
            Job | None: The job, or None when it is not running.
        """
        with self._lock:
            return self._jobs.get(job_id)

    def remove(self, job_id: str) -> None:
        """Drop a finished job.

        Args:
            job_id: Identifier of the run.
        """
        with self._lock:
            self._jobs.pop(job_id, None)

    def find_download(self, session_id: str) -> Job | None:
        """Find the job downloading a session, if one is.

        The one lookup that is not by identifier, because the plain download tools
        act on a session and have to tell its progress bar that the session ended.

        Args:
            session_id: Download session identifier.

        Returns:
            Job | None: The job, or None when that session is not downloading.
        """
        with self._lock:
            jobs = list(self._jobs.values())

        for job in jobs:
            if job.kind == "download" and job.session_id == session_id:
                return job

        return None


registry = JobRegistry()
