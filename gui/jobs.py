"""Job model and registry backing the progress panel.

A job is one long-running operation, tracked so the panel can be polled while
the tool call that owns it is still blocked in core. Every job carries a state
and an optional set of steps; the percentage is derived from the steps unless a
caller sets one directly.

A job that is still running can be controlled from the panel. It holds the
callables for its operation — core's cancellation token or the download manager's
own ``cancel``, plus ``pause``/``resume`` for downloads — so a request travels
from the panel's tool call to whichever thread is doing the work. Cancelling ends
the task: core performs the cleanup, and once it has finished the job removes
itself from the registry, since a cancelled operation is meant to leave nothing
behind except the entry core wrote to the execution log. Pausing only suspends a
download, so the job stays exactly where it was.

A job keeps no log of its own. Core already writes an execution log entry for
everything worth recording, and ``logs_read`` is how that is read back; a second
transcript on the panel duplicated it in a window too small to be read.

State isolation
---------------

This server is one process shared by every conversation, and a panel is part of a
conversation's record — a host that re-renders a stored panel replays the tool
input and result it was created with. So a job left reachable outlives the chat it
belongs to, and the panel of another chat can ask for it by id. Three rules keep
tasks from leaking into each other:

- **A finished job is not retained.** Once an operation ends, the panel showing it
  has already rendered its final frame from the last snapshot and stopped polling,
  so nothing needs the server's copy. It is kept only for a short grace period —
  long enough for an in-flight poll and the tool's own result to land — and then
  dropped. Nothing stale is left to serve.
- **Identifiers are unguessable and never reused.** Each carries a namespace minted
  when this process started, so an id from an earlier run of the server is rejected
  outright rather than colliding with a current job.
- **A job belongs to one panel.** An id-less poll — which is all a panel can send
  before its tool call has returned — is answered with the newest *running* job no
  other panel has claimed, never simply the newest.

Jobs are mutated from a worker thread and read from whichever thread answers the
panel's poll, so each job guards its own fields with a lock and hands out plain
dictionaries — a snapshot is a copy, never a live view.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable
import uuid

# Backstop only: with finished jobs expiring, a registry this large means
# something is genuinely running that many operations at once.
MAX_JOBS = 24

# How long a finished job stays readable. It has to cover the round trip between
# the operation ending and the panel's last poll — the tool call returning, the
# result reaching the panel, one more poll landing — and nothing beyond that.
FINISHED_GRACE_SECONDS = 20.0

# How long after a job is created it can still be adopted by a panel that polled
# without an identifier. A panel starts polling within a few hundred milliseconds
# of its tool call being issued, and the job appears a moment later, so a genuine
# claim happens almost immediately. Past this window a job is either already shown
# by its own panel or orphaned — and either way it belongs to a conversation that
# is not the one now asking, so it is never handed over.
CLAIMABLE_SECONDS = 10.0

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
PAUSED = "paused"
WAITING = "waiting"
SKIPPED = "skipped"
CANCELLING = "cancelling"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED})

# Minted per process. A progress id from an earlier run of this server carries a
# different namespace and is refused, so a panel restored from an older
# conversation cannot be answered with a current job that happens to share a
# number — which sequential ids made not just possible but likely.
INSTANCE = uuid.uuid4().hex[:8]

ID_PREFIX = "progress"


def new_progress_id() -> str:
    """Mint an identifier for one job.

    Returns:
        str: ``progress-<instance>-<random>``, unique across this process's life
        and unguessable from another conversation.
    """
    return f"{ID_PREFIX}-{INSTANCE}-{uuid.uuid4().hex[:12]}"


def belongs_to_this_instance(progress_id: str) -> bool:
    """Report whether an identifier was minted by this process.

    Args:
        progress_id: Identifier presented by a panel.

    Returns:
        bool: True when the id carries this process's namespace.
    """
    return progress_id.startswith(f"{ID_PREFIX}-{INSTANCE}-")


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
        key: str | None = None,
    ) -> None:
        """Create a running job.

        Args:
            progress_id: Identifier the panel polls with.
            title: Headline shown on the panel.
            subtitle: Optional secondary line, for example a target name.
            on_remove: Called with the identifier once a cancelled job has been
                cleaned up, so the registry can drop it.
            key: Optional identity of the underlying operation — a download
                session id, say. Two jobs never share a key while both are live,
                which is what stops one operation being tracked twice.
        """
        self.progress_id = progress_id
        self.title = title
        self.subtitle = subtitle
        self.key = key
        # Set once a panel has adopted this job, so the next panel to open looks
        # for its own rather than adopting this one as well.
        self.claimed = False

        self.state = RUNNING
        self.started_at = time.monotonic()
        self.finished_at: float | None = None

        self._percent: float | None = None
        self._steps: list[Step] = []
        self._metrics: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # Set when the job reaches a terminal state, so a waiter is released the
        # moment the cleanup ends rather than on the next poll tick.
        self._finished = threading.Event()

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
            if self.state in TERMINAL_STATES:
                return

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
            if self.state in TERMINAL_STATES:
                return

            self._percent = percent

    def set_metrics(self, metrics: list[dict[str, Any]]) -> None:
        """Replace the metric chips shown under the bar.

        Args:
            metrics: Entries shaped as ``{"label": "downloaded", "value": "12 MB"}``.
        """
        with self._lock:
            if self.state in TERMINAL_STATES:
                return

            self._metrics = list(metrics)

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

        A job that has already finished is left alone: the first terminal state
        wins. Without that, a watcher thread still draining its last reading
        could overwrite a cancellation with "completed" and leave the panel
        claiming an operation succeeded after its files were deleted.

        Args:
            state: Final job state: ``completed``, ``failed`` or ``cancelled``.
            subtitle: Optional closing line, for example an error summary.
        """
        with self._lock:
            if self.state in TERMINAL_STATES:
                return

            self.state = state
            self.finished_at = time.monotonic()
            if subtitle is not None:
                self.subtitle = subtitle
            if state == COMPLETED:
                self._percent = 100.0

            self._release()

        self._finished.set()

    def _release(self) -> None:
        """Drop the operation's control callables.

        Called with the lock held once the job is finished. Those callables close
        over the operation — a cancellation token, a download manager — so
        clearing them is what stops a finished job from keeping it alive, and
        makes a late cancel or pause a no-op rather than a call into something
        that has already been cleaned up.
        """
        self._canceller = None
        self._pause = None
        self._resume = None

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
            if self.state in TERMINAL_STATES:
                # The operation is over: holding its canceller would keep it
                # reachable and let a late click call into cleaned-up state.
                return

            self._canceller = canceller
            pending = self._cancel_requested

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
        task and its queue stay intact. Benchmarks have no
        equivalent, so they never register one and the panel offers no pause
        button for them.

        Args:
            pause: Callable suspending the operation.
            resume: Callable continuing it.
        """
        with self._lock:
            if self.state in TERMINAL_STATES:
                return

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
            else:
                action = self._pause
                self._paused = True
                outcome = "paused"

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
            if self.state in TERMINAL_STATES:
                return

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
            if self.state in TERMINAL_STATES:
                # Already finished — including by an earlier cancellation, which
                # has already removed the job.
                return

            self.state = CANCELLED
            self.finished_at = time.monotonic()
            self.subtitle = reason or "Cancelled."

            for step in self._steps:
                if step.state in (RUNNING, WAITING, PAUSED):
                    step.state = CANCELLED
                    step.detail = "cancelled"

            self._paused = False
            self._release()

            on_remove = self._on_remove
            self._on_remove = None

        self._finished.set()

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
        if self._finished.wait(timeout):
            return True

        with self._lock:
            return self.state in TERMINAL_STATES

    def _step(self, index: int) -> Step | None:
        """Look up a step by index while the lock is held.

        A finished job hands back nothing: its steps describe what a cancellation
        undid or what a failure stopped at, and a watcher draining its last
        reading must not move them back to running.

        Args:
            index: Zero-based step index.

        Returns:
            Step | None: The step, or None when the index is out of range or the
            job has already finished.
        """
        if self.state in TERMINAL_STATES:
            return None

        if 0 <= index < len(self._steps):
            return self._steps[index]

        return None

    # ========================================================
    # Reading — called from the thread answering the panel
    # ========================================================

    def snapshot(self) -> dict[str, Any]:
        """Build the payload the panel renders.

        ``elapsed_seconds`` is not shown on the panel any more, but it is kept
        because ``progress_cancel`` hands this snapshot to the model, where how
        long an operation ran before it was stopped is worth knowing.

        Returns:
            dict[str, Any]: Job state, overall percentage, per-step rows, metric
            chips and elapsed seconds. ``percent`` is None when progress is not
            measurable, which is what makes the bar indeterminate.
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
                "elapsed_seconds": round(elapsed, 1),
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


class DuplicateJob(RuntimeError):
    """Raised when an operation that is already tracked is started again.

    The registry allows one live job per key, so a second attempt to track the
    same download session — or the same benchmark — is refused rather than
    given a job of its own. Two jobs for one operation would each offer their own
    Cancel button over the same work, and cancelling one would leave the other
    reporting progress for something that had been cleaned up.
    """

    def __init__(self, key: str, job: Job) -> None:
        """Record which job already owns the key.

        Args:
            key: Operation identity that is already tracked.
            job: The live job holding it.
        """
        super().__init__(
            f"This operation is already running and tracked as "
            f"{job.progress_id}. Cancel it or wait for it to finish before "
            f"starting it again."
        )
        self.key = key
        self.job = job


class JobRegistry:
    """Registry of live jobs, newest last.

    Holds the operations currently running, plus those that finished within the
    last :data:`FINISHED_GRACE_SECONDS`. Nothing older is kept: a finished job's
    final state has already reached its panel, so retaining the server's copy only
    creates something a later conversation can be served by mistake.
    """

    def __init__(
        self,
        limit: int = MAX_JOBS,
        grace_seconds: float = FINISHED_GRACE_SECONDS,
        claimable_seconds: float = CLAIMABLE_SECONDS,
    ) -> None:
        """Create an empty registry.

        Args:
            limit: Backstop on how many jobs to hold at once.
            grace_seconds: How long a finished job stays readable.
            claimable_seconds: How long a new job can be adopted by a panel that
                polled without an identifier.
        """
        self._limit = limit
        self._grace = grace_seconds
        self._claimable = claimable_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(
        self,
        title: str,
        subtitle: str | None = None,
        key: str | None = None,
    ) -> Job:
        """Register a new running job.

        Args:
            title: Headline shown on the panel.
            subtitle: Optional secondary line.
            key: Optional identity of the underlying operation. When given, the
                registry refuses to create a second live job for it.

        Returns:
            Job: The registered job, with an identifier unique to this process.

        Raises:
            DuplicateJob: If a live job already holds this key.
        """
        with self._lock:
            self._expire()

            if key is not None:
                existing = self._live_with_key(key)
                if existing is not None:
                    raise DuplicateJob(key, existing)

            job = Job(
                progress_id=new_progress_id(),
                title=title,
                subtitle=subtitle,
                on_remove=self.remove,
                key=key,
            )

            self._jobs[job.progress_id] = job
            self._evict()

        return job

    def _expire(self) -> None:
        """Drop finished jobs past their grace period.

        Called with the lock held, at the start of every lookup, so a stale job is
        gone before anything can be answered with it. Running jobs are never
        touched, however long they take.
        """
        cutoff = time.monotonic() - self._grace

        stale = [
            progress_id
            for progress_id, job in self._jobs.items()
            if job.state in TERMINAL_STATES
            and (job.finished_at is None or job.finished_at <= cutoff)
        ]

        for progress_id in stale:
            del self._jobs[progress_id]

    def _live_with_key(self, key: str) -> Job | None:
        """Find an unfinished job tracking a given operation.

        Called with the lock held.

        Args:
            key: Operation identity to look for.

        Returns:
            Job | None: The live job holding the key, or None. A job that has
            finished does not count: its operation is over, so starting the same
            one again is a new task rather than a duplicate.
        """
        for job in self._jobs.values():
            if job.key == key and job.state not in TERMINAL_STATES:
                return job

        return None

    def find(self, key: str) -> Job | None:
        """Look up the live job tracking an operation.

        Args:
            key: Operation identity, for example a download session id.

        Returns:
            Job | None: The live job, or None when the operation is not tracked.
        """
        with self._lock:
            self._expire()

            return self._live_with_key(key)

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

        An identifier from an earlier run of this server is refused outright: it
        cannot name anything current, and answering it with a job that happened to
        match would be exactly the leak this guards against. The job is also marked
        claimed, so a panel that opens later and asks for "whichever job is mine"
        will not be handed this one.

        Args:
            progress_id: Identifier from ``create``.

        Returns:
            Job | None: The job, or None when the identifier is unknown, expired,
            or was not minted by this process.
        """
        if not belongs_to_this_instance(progress_id):
            return None

        with self._lock:
            self._expire()

            job = self._jobs.get(progress_id)

            if job is not None:
                job.claimed = True

            return job

    def claim_unclaimed(self) -> Job | None:
        """Adopt the job this panel was opened for, if it can be identified.

        A panel is rendered by the host as soon as its tool call is *issued*,
        before the server has run the tool and created the job, so its first polls
        arrive with no identifier and something has to decide what they mean.

        Three restrictions make that decision safe. The job must be **running**,
        because a panel that has only just opened cannot have been opened for an
        operation already over. It must be **unclaimed**, so two operations
        starting together do not both land on the first. And it must have been
        created **within the last few seconds**, because a panel polls within
        milliseconds of opening — anything older is another conversation's, and a
        long download from an hour ago must not be adopted by a panel opening now.

        Returns:
            Job | None: The claimable job, now claimed, or None — in which case
            the caller's own job does not exist yet and its panel keeps waiting.
        """
        with self._lock:
            self._expire()

            cutoff = time.monotonic() - self._claimable

            for job in reversed(self._jobs.values()):
                if job.claimed or job.state in TERMINAL_STATES:
                    continue

                if job.started_at <= cutoff:
                    # Older jobs are older still, so nothing beyond this can be
                    # claimable either.
                    return None

                job.claimed = True

                return job

        return None

    def latest_active(self) -> Job | None:
        """Return the newest job that has not finished.

        What an unqualified Cancel or Stop means: a finished job has nothing left
        to act on, so acting on the newest *running* one is both what the caller
        intended and the only thing that can have an effect.

        Returns:
            Job | None: Newest unfinished job, or None when nothing is running.
        """
        with self._lock:
            self._expire()

            for job in reversed(self._jobs.values()):
                if job.state not in TERMINAL_STATES:
                    return job

        return None

    def snapshots(self) -> list[dict[str, Any]]:
        """Snapshot every job still held, oldest first.

        Returns:
            list[dict[str, Any]]: One snapshot per job.
        """
        with self._lock:
            self._expire()

            jobs = list(self._jobs.values())

        return [job.snapshot() for job in jobs]

    def clear(self) -> None:
        """Forget every job.

        For a test that needs a clean registry, and for a caller that wants to
        drop everything this process was tracking.
        """
        with self._lock:
            self._jobs.clear()

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

