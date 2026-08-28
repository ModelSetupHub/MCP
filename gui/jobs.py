"""Job model and registry backing the progress panel.

A job is one long-running operation, tracked so the panel can be polled while
the tool call that owns it is still blocked in core. Every job carries a state,
an optional set of steps, and a short log; the percentage is derived from the
steps unless a caller sets one directly.

A job that is still running can be controlled from the panel. It holds the
callables for its operation — core's cancellation token or the download manager's
own ``cancel``, plus ``pause``/``resume`` for downloads — so a request travels
from the panel's tool call to whichever thread is doing the work. Cancelling ends
the task: core performs the cleanup, and once it has finished the job removes
itself from the registry, since a cancelled operation is meant to leave nothing
behind except the entry core wrote to the execution log. Pausing only suspends a
download, so the job stays exactly where it was.

Jobs are mutated from a worker thread and read from whichever thread answers the
panel's poll, so each job guards its own fields with a lock and hands out plain
dictionaries — a snapshot is a copy, never a live view.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import itertools
import threading
import time
from typing import Any, Callable

MAX_LOG_LINES = 60
MAX_JOBS = 24

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
PAUSED = "paused"
WAITING = "waiting"
SKIPPED = "skipped"
CANCELLING = "cancelling"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED})

_counter = itertools.count(1)


@dataclass
class Step:
    """One unit of work inside a job, as shown in its own row on the panel."""

    name: str
    state: str = WAITING
    percent: float | None = None
    detail: str | None = None
    note: str | None = None
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        """Render the step for the panel.

        Returns:
            dict[str, Any]: Step fields, with ``percent`` normalised to 0-100.
        """
        return {
            "name": self.name,
            "state": self.state,
            "percent": _clamp(self.percent),
            "detail": self.detail,
            "note": self.note,
        }


class Job:
    """A long-running operation the progress panel can display."""

    def __init__(
        self,
        progress_id: str,
        title: str,
        subtitle: str | None = None,
        on_remove: Callable[[str], None] | None = None,
    ) -> None:
        """Create a running job.

        Args:
            progress_id: Identifier the panel polls with.
            title: Headline shown on the panel.
            subtitle: Optional secondary line, for example a target name.
            on_remove: Called with the identifier once a cancelled job has been
                cleaned up, so the registry can drop it.
        """
        self.progress_id = progress_id
        self.title = title
        self.subtitle = subtitle

        self.state = RUNNING
        self.started_at = time.monotonic()
        self.finished_at: float | None = None

        self._percent: float | None = None
        self._steps: list[Step] = []
        self._metrics: list[dict[str, Any]] = []
        self._log: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._lock = threading.Lock()

        self._canceller: Callable[[str], None] | None = None
        self._pause: Callable[[], None] | None = None
        self._resume: Callable[[], None] | None = None
        self._paused = False
        self._cancel_requested = False
        self._on_remove = on_remove

    # ========================================================
    # Mutation — called from the thread running the operation
    # ========================================================

    def add_steps(self, names: list[str], weight: float = 1.0) -> None:
        """Append waiting steps to the job.

        Args:
            names: Step names, in the order they will run.
            weight: Relative share of the job each step accounts for.
        """
        with self._lock:
            self._steps.extend(
                Step(name=name, weight=weight) for name in names
            )

    def set_percent(self, percent: float | None) -> None:
        """Set the overall percentage explicitly.

        Use this when the operation reports its own total — a byte count, say —
        rather than progressing through discrete steps.

        Args:
            percent: Completion from 0 to 100, or None to make the bar
                indeterminate again.
        """
        with self._lock:
            self._percent = percent

    def set_metrics(self, metrics: list[dict[str, Any]]) -> None:
        """Replace the metric chips shown under the bar.

        Args:
            metrics: Entries shaped as ``{"label": "downloaded", "value": "12 MB"}``.
        """
        with self._lock:
            self._metrics = list(metrics)

    def log(self, message: str) -> None:
        """Append a line to the job's log.

        Args:
            message: Text to show; the oldest lines are dropped past the cap.
        """
        with self._lock:
            self._log.append(message)

    def start_step(self, index: int, detail: str | None = None) -> None:
        """Mark a step as running.

        Args:
            index: Zero-based step index; out-of-range indices are ignored so a
                mis-counted step can never abort the operation itself.
            detail: Optional short status text for the step's right-hand column.
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
        note: str | None = None,
    ) -> None:
        """Update a running step's progress.

        Args:
            index: Zero-based step index.
            percent: Step completion from 0 to 100.
            detail: Short status text.
            note: Longer explanatory line under the step.
        """
        with self._lock:
            step = self._step(index)
            if step is None:
                return
            if percent is not None:
                step.percent = percent
            if detail is not None:
                step.detail = detail
            if note is not None:
                step.note = note

    def finish_step(
        self,
        index: int,
        state: str = COMPLETED,
        detail: str | None = None,
        note: str | None = None,
    ) -> None:
        """Mark a step as finished.

        Args:
            index: Zero-based step index.
            state: Final step state, normally ``completed`` or ``failed``.
            detail: Short status text.
            note: Longer explanatory line, for example an error message.
        """
        with self._lock:
            step = self._step(index)
            if step is None:
                return
            step.state = state
            step.percent = 100.0 if state == COMPLETED else step.percent
            if detail is not None:
                step.detail = detail
            if note is not None:
                step.note = note

    def finish(
        self,
        state: str = COMPLETED,
        subtitle: str | None = None,
    ) -> None:
        """Mark the whole job as finished.

        Args:
            state: Final job state: ``completed``, ``failed`` or ``cancelled``.
            subtitle: Optional closing line, for example an error summary.
        """
        with self._lock:
            self.state = state
            self.finished_at = time.monotonic()
            if subtitle is not None:
                self.subtitle = subtitle
            if state == COMPLETED:
                self._percent = 100.0

    # ========================================================
    # Cancelling
    # ========================================================

    def set_canceller(self, canceller: Callable[[str], None]) -> None:
        """Register how this job's operation is cancelled.

        Called by whichever wrapper started the operation. If a cancellation was
        already requested — the user can press the button before the operation has
        got far enough to register one — it is applied immediately, so the request
        is never silently dropped.

        Args:
            canceller: Callable taking the reason and cancelling the operation.
        """
        with self._lock:
            self._canceller = canceller
            pending = self._cancel_requested and self.state not in TERMINAL_STATES

        if pending:
            canceller("Cancelled from the progress panel")

    @property
    def cancellable(self) -> bool:
        """bool: Whether the panel should offer a cancel button for this job."""
        with self._lock:
            return (
                self.state not in TERMINAL_STATES and not self._cancel_requested
            )

    def request_cancel(
        self,
        reason: str = "Cancelled from the progress panel",
    ) -> bool:
        """Ask the operation to cancel.

        The job goes to ``cancelling`` and the operation is signalled; it reaches
        ``cancelled`` only once core has actually stopped and cleaned up. Calling
        this on a finished job does nothing.

        Args:
            reason: Explanation passed to core and shown on the panel.

        Returns:
            bool: True if a cancellation was requested, False if the job had
            already finished or was already cancelling.
        """
        with self._lock:
            if self.state in TERMINAL_STATES or self._cancel_requested:
                return False

            self._cancel_requested = True
            self.state = CANCELLING
            self.subtitle = "Cancelling…"
            canceller = self._canceller
            self._log.append("Cancellation requested.")

        if canceller is not None:
            canceller(reason)

        return True

    @property
    def cancel_requested(self) -> bool:
        """bool: Whether a cancellation has been requested for this job."""
        with self._lock:
            return self._cancel_requested

    # ========================================================
    # Pausing — downloads only
    # ========================================================

    def set_pause_controls(
        self,
        pause: Callable[[], None],
        resume: Callable[[], None],
    ) -> None:
        """Register how this job's operation is paused and resumed.

        Only downloads have this: a transfer can be suspended and picked up again
        from its partial data, which is a different thing from cancelling — the
        task and its queue stay intact. Benchmarks and installers have no
        equivalent, so they never register one and the panel offers no pause
        button for them.

        Args:
            pause: Callable suspending the operation.
            resume: Callable continuing it.
        """
        with self._lock:
            self._pause = pause
            self._resume = resume

    def toggle_pause(self) -> str:
        """Pause a running operation, or resume a paused one.

        Returns:
            str: ``"paused"``, ``"resumed"``, or ``"unavailable"`` when the
            operation cannot be paused or has already finished.
        """
        with self._lock:
            if (
                self._pause is None
                or self.state in TERMINAL_STATES
                or self._cancel_requested
            ):
                return "unavailable"

            if self._paused:
                action = self._resume
                self._paused = False
                outcome = "resumed"
                self._log.append("Resumed.")
            else:
                action = self._pause
                self._paused = True
                outcome = "paused"
                self._log.append("Paused.")

        if action is not None:
            action()

        return outcome

    @property
    def pausable(self) -> bool:
        """bool: Whether this job's operation can be paused at all."""
        with self._lock:
            return self._pause is not None

    def sync_paused(self, paused: bool) -> None:
        """Adopt the operation's own paused state.

        Core is the authority on whether a download is actually suspended — it can
        also be paused by the manager's keyboard listener, not just by this
        panel — so the watcher pushes that state in rather than letting the
        panel's flag drift from reality.

        Args:
            paused: Whether the operation reports itself as paused.
        """
        with self._lock:
            self._paused = paused

    def finish_cancelled(self, reason: str | None = None) -> None:
        """Record that the operation was cancelled, then remove the job.

        Core has already undone the operation's side effects and written the log
        entry by the time this runs. The job record is the last thing left, so it
        is dropped from the registry — nothing about a cancelled operation should
        outlive it except that log entry.

        The panel still learns the outcome: whoever requested the cancellation
        holds a direct reference to this job, so it can read the final snapshot
        after the removal and hand it back as the cancel call's result.

        Args:
            reason: Why the operation was cancelled, shown on the panel.
        """
        with self._lock:
            self.state = CANCELLED
            self.finished_at = time.monotonic()
            self.subtitle = reason or "Cancelled."
            self._log.append(
                "Cancelled; everything this operation created was removed."
            )

            for step in self._steps:
                if step.state in (RUNNING, WAITING):
                    step.state = CANCELLED
                    step.detail = "cancelled"

            on_remove = self._on_remove

        if on_remove is not None:
            on_remove(self.progress_id)

    def wait_until_finished(self, timeout: float) -> bool:
        """Block until the job reaches a terminal state.

        Used by the cancel tool so its result reflects what actually happened,
        rather than reporting a cancellation core may still be cleaning up after.

        Args:
            timeout: Seconds to wait at most.

        Returns:
            bool: True if the job finished within the timeout.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            with self._lock:
                if self.state in TERMINAL_STATES:
                    return True

            time.sleep(0.1)

        with self._lock:
            return self.state in TERMINAL_STATES

    def _step(self, index: int) -> Step | None:
        """Look up a step by index while the lock is held.

        Args:
            index: Zero-based step index.

        Returns:
            Step | None: The step, or None when the index is out of range.
        """
        if 0 <= index < len(self._steps):
            return self._steps[index]

        return None

    # ========================================================
    # Reading — called from the thread answering the panel
    # ========================================================

    def snapshot(self) -> dict[str, Any]:
        """Build the payload the panel renders.

        Returns:
            dict[str, Any]: Job state, overall percentage, per-step rows, metric
            chips, recent log lines, elapsed time and an ETA when one can be
            estimated. ``percent`` is None when progress is not measurable, which
            is what makes the bar indeterminate.
        """
        with self._lock:
            percent = self._percent
            if percent is None:
                percent = _steps_percent(self._steps)

            elapsed = (self.finished_at or time.monotonic()) - self.started_at

            return {
                "progress_id": self.progress_id,
                "tracked": True,
                "title": self.title,
                "subtitle": self.subtitle,
                "state": self.state,
                "percent": _clamp(percent),
                "determinate": percent is not None,
                "cancellable": (
                    self.state not in TERMINAL_STATES
                    and not self._cancel_requested
                ),
                "pausable": (
                    self._pause is not None
                    and self.state not in TERMINAL_STATES
                    and not self._cancel_requested
                ),
                "paused": self._paused,
                "cancelling": self.state == CANCELLING,
                "steps": [step.as_dict() for step in self._steps],
                "metrics": list(self._metrics),
                "log": list(self._log),
                "elapsed_seconds": round(elapsed, 1),
                "eta_seconds": _estimate_eta(percent, elapsed, self.state),
            }


def _steps_percent(steps: list[Step]) -> float | None:
    """Derive an overall percentage from a job's steps.

    Args:
        steps: The job's steps.

    Returns:
        float | None: Weighted completion from 0 to 100, or None when there are
        no steps to derive it from.
    """
    if not steps:
        return None

    total_weight = sum(step.weight for step in steps) or float(len(steps))
    done = 0.0

    for step in steps:
        if step.state in (COMPLETED, SKIPPED):
            fraction = 1.0
        elif step.state == FAILED:
            # A failed step stops counting where it stopped, so the bar does not
            # jump forward on failure.
            fraction = (step.percent or 0.0) / 100.0
        elif step.state == RUNNING:
            fraction = (step.percent or 0.0) / 100.0
        else:
            fraction = 0.0

        done += fraction * step.weight

    return 100.0 * done / total_weight


def _estimate_eta(
    percent: float | None,
    elapsed: float,
    state: str,
) -> float | None:
    """Estimate the seconds remaining from the completion rate so far.

    Args:
        percent: Overall completion from 0 to 100.
        elapsed: Seconds since the job started.
        state: Current job state.

    Returns:
        float | None: Estimated seconds remaining, or None when the job is not
        running or too little has happened to extrapolate from.
    """
    if state != RUNNING or percent is None or percent <= 1 or elapsed <= 1:
        return None

    total = elapsed * 100.0 / percent

    return round(max(0.0, total - elapsed), 1)


def _clamp(percent: float | None) -> float | None:
    """Constrain a percentage to the 0-100 range.

    Args:
        percent: Raw percentage, possibly None or out of range.

    Returns:
        float | None: Percentage rounded to one decimal, or None when the input
        was None.
    """
    if percent is None:
        return None

    return round(min(100.0, max(0.0, float(percent))), 1)


class JobRegistry:
    """Bounded registry of jobs, newest last."""

    def __init__(self, limit: int = MAX_JOBS) -> None:
        """Create an empty registry.

        Args:
            limit: How many finished jobs to keep before dropping the oldest.
        """
        self._limit = limit
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, title: str, subtitle: str | None = None) -> Job:
        """Register a new running job.

        Args:
            title: Headline shown on the panel.
            subtitle: Optional secondary line.

        Returns:
            Job: The registered job.
        """
        job = Job(
            progress_id=f"progress-{next(_counter)}",
            title=title,
            subtitle=subtitle,
            on_remove=self.remove,
        )

        with self._lock:
            self._jobs[job.progress_id] = job
            self._evict()

        return job

    def remove(self, progress_id: str) -> None:
        """Drop a job from the registry.

        Called when a cancelled operation has finished cleaning up, so no record
        of it is left in memory.

        Args:
            progress_id: Identifier of the job to forget.
        """
        with self._lock:
            self._jobs.pop(progress_id, None)

    def get(self, progress_id: str) -> Job | None:
        """Look up a job by identifier.

        Args:
            progress_id: Identifier from ``create``.

        Returns:
            Job | None: The job, or None when it is unknown or already evicted.
        """
        with self._lock:
            return self._jobs.get(progress_id)

    def latest(self) -> Job | None:
        """Return the most recently created job.

        The panel opens without knowing an identifier — the tool call that shows
        it is the one that created the job — so an unqualified poll resolves to
        the newest job.

        Returns:
            Job | None: Newest job, or None when none has been created yet.
        """
        with self._lock:
            for job in reversed(self._jobs.values()):
                return job

        return None

    def snapshots(self) -> list[dict[str, Any]]:
        """Snapshot every known job, oldest first.

        Returns:
            list[dict[str, Any]]: One snapshot per job.
        """
        with self._lock:
            jobs = list(self._jobs.values())

        return [job.snapshot() for job in jobs]

    def _evict(self) -> None:
        """Drop the oldest finished jobs once the registry is over its limit.

        Called with the lock held. Running jobs are never evicted: something is
        still writing to them, and the panel showing them would go blank.
        """
        while len(self._jobs) > self._limit:
            for progress_id, job in self._jobs.items():
                if job.state in TERMINAL_STATES:
                    del self._jobs[progress_id]
                    break
            else:
                return


registry = JobRegistry()

