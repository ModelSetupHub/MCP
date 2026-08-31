"""Workers that run the tracked operations and keep their jobs current.

Every tracked tool follows the same shape. It creates a job, hands the work to a
background thread, and returns the job's identifier straight away — so the panel
has its id within milliseconds and the MCP request is never held open for the
length of the operation. That is what lets ``progress_get_status`` be answered
while a benchmark is still generating.

Each worker owns its job's whole lifecycle and reaches exactly one terminal
status:

    try:      run the core function, classify what it returned  → completed/failed
    except:   OperationCancelled → cancelled, anything else     → failed
    finally:  the job is finished either way

Where the progress figures come from is each worker's business and stops here.
Downloads read ``DownloadManager.get_status``, which is a real live API. Benchmarks
tail the execution log, because core returns nothing until every prompt has run —
if core later grows a progress callback, only ``_run_benchmark`` changes.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
import time
from typing import Any

# Core is imported as the top-level `core` package, the same way core imports
# itself. `Core.core.x` resolves to a second, distinct copy of the same file with
# its own copy of every class — so a token handed to core and the
# OperationCancelled caught back from it would come from different copies, and
# `except OperationCancelled` would silently miss the cancellation.
from core import logging as core_logging
from core.cancellation import CancellationToken, OperationCancelled
from core.download_manager.manager import DownloadManager
from core.ollama import experiment

from .jobs import (
    CANCELLED,
    COMPLETED,
    FAILED,
    RUNNING,
    SKIPPED,
    WAITING,
    Job,
    Metric,
    registry,
)
from .logtail import LogTail

# How often a worker reads its progress source.
POLL_SECONDS = 0.4

# How long a cancellation waits for core to finish cleaning up.
CANCEL_TIMEOUT = 60.0

# How long a download worker waits for the manager's thread to exit after a
# cancellation, so the final snapshot describes the state after cleanup.
WORKER_STOP_TIMEOUT = 30.0

# How long a benchmark waits for its log reader to drain. Core writes the last
# prompt's entry immediately before returning, routinely after the last read.
DRAIN_TIMEOUT = 5.0

# DownloadManager's per-item statuses, mapped onto step states.
DOWNLOAD_STEP_STATES = {
    "waiting": WAITING,
    "connecting": RUNNING,
    "downloading": RUNNING,
    "retrying": RUNNING,
    "paused": RUNNING,
    "completed": COMPLETED,
    "failed": FAILED,
    "skipped": SKIPPED,
    "cancelled": CANCELLED,
}

BENCHMARK_COMPONENT = "ollama/experiment"


def _spawn(job: Job, target: Callable[[], None], name: str) -> None:
    """Run a job's worker on a daemon thread.

    A thread that cannot be started — the process is out of them — would leave the
    job at ``starting`` with nothing to advance it, so that failure is the job's
    failure.

    Args:
        job: Job the worker belongs to.
        target: Zero-argument callable that runs the operation to a terminal state.
        name: Thread name, for debugging.
    """
    try:
        threading.Thread(target=target, name=name, daemon=True).start()
    except Exception as error:
        job.finish(
            FAILED,
            message="The operation could not be started.",
            error=str(error),
        )


def format_bytes(count: float | None) -> str:
    """Format a byte count for the panel.

    Args:
        count: Number of bytes, or None when the size is unknown.

    Returns:
        str: Size with a binary unit, or ``"?"`` when unknown.
    """
    if count is None:
        return "?"

    size = float(count)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.{0 if unit == 'B' else 1}f} {unit}"
        size /= 1024

    return f"{size:.1f} TB"


def format_speed(bytes_per_second: float | None) -> str | None:
    """Format a transfer rate for the panel.

    Args:
        bytes_per_second: Rate core measured, or None when it has none yet.

    Returns:
        str | None: Rate per second, or None when there is nothing to show —
        which is how a queue that has not started, or one that is paused,
        reports itself.
    """
    if not bytes_per_second:
        return None

    return f"{format_bytes(bytes_per_second)}/s"


# ============================================================
# Downloads
# ============================================================

def start_download(manager: DownloadManager, session_id: str) -> Job:
    """Begin a session's queue and follow it on a worker thread.

    Args:
        manager: Session manager to start.
        session_id: Session identifier, shown on the panel.

    Returns:
        Job: The job, already persisted, whose id the panel polls.
    """
    status = manager.get_status()
    filenames = [item["filename"] for item in status["downloads"]]

    job = Job(
        kind="download",
        title=f"Downloading {len(filenames)} file(s)",
        message=f"session {session_id}",
        session_id=session_id,
    )
    job.add_steps(filenames)
    # DownloadManager owns both controls: cancel() removes what the session
    # produced, pause()/resume() suspend the transfer and keep the queue.
    job.set_cancel(lambda reason: manager.cancel(reason=reason))
    job.set_pause(pause=manager.pause, resume=manager.resume)

    _spawn(
        job,
        lambda: _run_download(job, manager, session_id),
        name=f"download-{session_id}",
    )

    return job


def _run_download(job: Job, manager: DownloadManager, session_id: str) -> None:
    """Run one download to a terminal status.

    Args:
        job: Job to keep current.
        manager: Session to start and watch.
        session_id: Session identifier, for the session hook.
    """
    try:
        manager.start()
        job.begin()

        while True:
            status = manager.get_status()
            _apply_download_status(job, status)
            job.publish()

            if not status["running"]:
                break

            time.sleep(POLL_SECONDS)

        # A cancellation returns before the manager's thread has exited, and
        # core's cleanup runs on that thread. Waiting means the final snapshot
        # describes the state after the files were removed, not during.
        manager.wait_until_stopped(timeout=WORKER_STOP_TIMEOUT)

        final = manager.get_status()
        _apply_download_status(job, final)
        _finish_download(job, final)
    except Exception as error:
        # Including whatever manager.start() raises — an empty queue, a session
        # already cancelled. The job must reach a terminal status either way, or
        # the panel polls a running job for ever.
        job.finish(FAILED, message="The download failed.", error=str(error))
    finally:
        _release_session(session_id, manager)


def _finish_download(job: Job, final: dict) -> None:
    """Classify a stopped download and finish its job.

    Args:
        job: Job to finish.
        final: Manager status after the queue stopped and cleanup ran.
    """
    if final["cancelled"]:
        deleted = final.get("files_deleted", True)

        if deleted:
            # cancel() removes every file the session produced, including ones
            # that had finished, so a row still reading "completed" would point at
            # something no longer on disk.
            for index, item in enumerate(final["downloads"]):
                if item["status"] == "completed":
                    job.finish_step(index, state=CANCELLED, detail="removed")

        job.finish(
            CANCELLED,
            message=final.get("cancel_reason")
            or (
                "Cancelled; downloaded files removed."
                if deleted
                else "Cancelled; downloaded files kept."
            ),
        )
        return

    failed = [
        item["filename"]
        for item in final["downloads"]
        if item["status"] == "failed"
    ]

    if failed:
        job.finish(
            FAILED,
            message=f"{len(failed)} file(s) failed.",
            error=", ".join(failed),
        )
        return

    job.finish(COMPLETED, message="All files downloaded.")


def _apply_download_status(job: Job, status: dict) -> None:
    """Copy one manager reading onto a job.

    Args:
        job: Job to update.
        status: Return value of ``DownloadManager.get_status``.
    """
    downloaded_total = 0
    expected_total = 0
    sizes_known = True
    active_speed: float | None = None

    job.sync_paused(bool(status["paused"]))

    for index, item in enumerate(status["downloads"]):
        downloaded = item.get("downloaded") or 0
        total = item.get("total")
        state = item["status"]

        downloaded_total += downloaded

        if total:
            expected_total += total
        elif state not in ("completed", "skipped"):
            sizes_known = False

        percent = 100.0 if state == "completed" else (
            100.0 * downloaded / total if total else None
        )

        detail = (
            f"{format_bytes(downloaded)} / {format_bytes(total)}"
            if downloaded or total
            else state
        )

        # Core measures the rate per item, so the row carries its own and the
        # chip under the bar shows whichever row is transferring.
        speed = format_speed(item.get("speed")) if state == "downloading" else None

        if speed is not None:
            detail = f"{detail} · {speed}"
            active_speed = item.get("speed")

        if state == "paused":
            detail = f"{format_bytes(downloaded)} · stopped"

        step_state = DOWNLOAD_STEP_STATES.get(state, WAITING)

        if step_state == RUNNING:
            # Includes paused: the transfer is suspended, not finished, and
            # closing the row would make the bar look like it moved on.
            job.start_step(index)
            job.update_step(index, percent=percent, detail=detail, error=item.get("error"))
        elif step_state == WAITING:
            job.update_step(index, detail=detail)
        else:
            job.finish_step(index, state=step_state, detail=detail, error=item.get("error"))

    if sizes_known and expected_total:
        job.set_percent(100.0 * downloaded_total / expected_total)

    metrics = [
        Metric("downloaded", format_bytes(downloaded_total)),
        Metric("total", format_bytes(expected_total) if expected_total else "?"),
        Metric(
            "file",
            f"{min(status['current_index'] + 1, status['total_files'])}"
            f"/{status['total_files']}",
        ),
    ]

    # Only while something is actually transferring: a rate left on the panel
    # after the queue stopped would read as though it still were.
    rate = format_speed(active_speed)

    if rate is not None:
        metrics.append(Metric("speed", rate))

    job.set_metrics(metrics)


# Set by the server layer so a cancelled session can be dropped from wherever it
# lives. Kept as a hook rather than an import so this module does not depend on
# main.py's registry.
release_session: Callable[[str], Any] | None = None


def _release_session(session_id: str, manager: DownloadManager) -> None:
    """Drop a closed session from the owning registry.

    A cancelled or closed session has nothing left to continue from, so its id is
    freed. One that finished normally stays: its status is still worth reading.

    Args:
        session_id: Session that stopped.
        manager: Manager to ask whether it closed.
    """
    if release_session is None:
        return

    try:
        closed = manager.get_status()["closed"]
    except Exception:  # pragma: no cover - defensive
        closed = True

    if closed:
        release_session(session_id)


def note_download_ended(session_id: str, reason: str) -> bool:
    """Tell a session's progress bar that the session was ended elsewhere.

    ``download_cancel`` and ``download_close_session`` act on the session, not on
    the bar. The worker would notice on its next reading anyway, but until then the
    panel keeps offering Cancel and Stop for a queue that has gone.

    Args:
        session_id: Session that was ended.
        reason: Why, shown on the panel.

    Returns:
        bool: True when a job was tracking that session.
    """
    job = registry.find_download(session_id)

    if job is None:
        return False

    job.request_cancel(reason)
    job.wait(CANCEL_TIMEOUT)

    return True


# ============================================================
# Benchmarks
# ============================================================

def start_benchmark(
    model: str,
    prompts: list[str],
    configurations: list[dict],
    include_output: bool,
) -> Job:
    """Begin a benchmark or comparison and run it on a worker thread.

    One entry point for both shapes. A single test is one configuration with one
    row per prompt; a comparison is several, with one row per configuration. Both
    call ``experiment.compare_tests``, so core does the normalisation either way
    and this layer has one result shape to classify.

    Args:
        model: Model name or tag to benchmark.
        prompts: Prompts to run against every configuration.
        configurations: Normalised configurations, each ``{"name", "options"}``.
        include_output: Whether to include generated text in the results.

    Returns:
        Job: The job, already persisted, whose id the panel polls.
    """
    single = len(configurations) == 1

    job = Job(
        kind="benchmark",
        title=(
            f"Benchmarking {model}"
            if single
            else f"Comparing {len(configurations)} configuration(s)"
        ),
        message=(
            f"{len(prompts)} prompt(s) · {configurations[0]['name']}"
            if single
            else f"{model} · {len(prompts)} prompt(s) each"
        ),
    )

    # The rows exist before core runs, so the very first poll shows the shape of
    # the work rather than an empty indeterminate bar.
    if single:
        job.add_steps([f"prompt {index}" for index in range(1, len(prompts) + 1)])
    else:
        job.add_steps(
            [configuration["name"] for configuration in configurations],
            weight=float(len(prompts) or 1),
        )

    token = CancellationToken()
    job.set_cancel(token.cancel)

    _spawn(
        job,
        lambda: _run_benchmark(
            job=job,
            token=token,
            model=model,
            prompts=prompts,
            configurations=configurations,
            include_output=include_output,
            single=single,
        ),
        name="benchmark",
    )

    return job


def _run_benchmark(
    job: Job,
    token: CancellationToken,
    model: str,
    prompts: list[str],
    configurations: list[dict],
    include_output: bool,
    single: bool,
) -> None:
    """Run one benchmark to a terminal status.

    Args:
        job: Job to keep current.
        token: Cancellation token the panel's Cancel button sets.
        model: Model being benchmarked.
        prompts: Prompts to run.
        configurations: Configurations to run them against.
        include_output: Whether to keep generated text.
        single: Whether the rows are prompts rather than configurations.
    """
    stop = threading.Event()
    reader = threading.Thread(
        target=lambda: _tail_benchmark(
            job=job,
            stop=stop,
            names=[configuration["name"] for configuration in configurations],
            prompt_count=len(prompts),
            single=single,
        ),
        name="benchmark-log",
        daemon=True,
    )

    try:
        job.begin()
        reader.start()

        result = experiment.compare_tests(
            model=model,
            prompts=prompts,
            configurations=configurations,
            include_output=include_output,
            cancellation=token,
        )
    except OperationCancelled as error:
        _stop_reader(stop, reader)
        job.finish(CANCELLED, message=str(error))
        return
    except Exception as error:
        _stop_reader(stop, reader)
        job.finish(FAILED, message="The benchmark failed.", error=str(error))
        return

    # Core writes the last prompt's entry just before returning, so the reader is
    # drained before the rows are settled from the result.
    _stop_reader(stop, reader)

    _close_benchmark_steps(job, result=result, single=single)

    # Core records a failed prompt as a result entry rather than raising, so a run
    # against a model that does not exist returns normally with every prompt
    # failed. Classifying the result is what turns that into a failure.
    error = _benchmark_error(result)

    if error is not None:
        job.finish(FAILED, message="Every prompt failed.", error=error)
        return

    # The measurements are the point of the benchmark, so they are stored with the
    # job rather than discarded. The model fetches them once the status is
    # completed; they are deliberately not repeated in every progress snapshot.
    job.finish(
        COMPLETED,
        message="Finished. Retrieve the measurements with benchmark_get_result.",
        result=_business_result(result, single=single),
    )


def _business_result(result: dict, single: bool) -> dict:
    """Shape core's return value as the benchmark's deliverable.

    A single test is one configuration, so its result is unwrapped to the shape
    ``ollama_run_test`` returns. A comparison keeps core's own shape.

    Args:
        result: Return value of ``experiment.compare_tests``.
        single: Whether this was a single test rather than a comparison.

    Returns:
        dict: The measurements the model reads.
    """
    tests = result.get("tests") or []

    if single and tests:
        return {"model": result.get("model"), **tests[0]}

    return result


def _stop_reader(stop: threading.Event, reader: threading.Thread) -> None:
    """Stop the log reader and wait for it to drain.

    Draining before the rows are settled lets a late entry still land; joining
    before the job finishes stops one landing afterwards.

    Args:
        stop: Event the reader checks.
        reader: Thread to join.
    """
    stop.set()

    if reader.is_alive():
        reader.join(timeout=DRAIN_TIMEOUT)


def _benchmark_error(result: dict) -> str | None:
    """Report the error when a benchmark produced no successful prompt.

    A run where some prompts succeeded is a success: those measurements are real,
    and the failed rows carry their own errors.

    Args:
        result: Return value of ``experiment.compare_tests``.

    Returns:
        str | None: The first error found, or None when the run succeeded.
    """
    entries = [
        entry
        for test in result.get("tests") or []
        for entry in test.get("results") or []
    ]

    if not entries or any(entry.get("success") is not False for entry in entries):
        return None

    for entry in entries:
        if entry.get("error"):
            return str(entry["error"])

    return "No prompt produced a result."


def _close_benchmark_steps(job: Job, result: dict, single: bool) -> None:
    """Settle every row from the returned result.

    The rows advance from the execution log while the run is in flight, which is
    best-effort: an entry can be written after the last read, and a prompt that
    failed before core logged it produces no entry at all. The result is
    authoritative, so it closes every row once the run is over.

    Args:
        job: Job whose rows are being closed.
        result: Return value of ``experiment.compare_tests``.
        single: Whether the rows are prompts rather than configurations.
    """
    tests = result.get("tests") or []

    if single:
        entries = tests[0].get("results") or [] if tests else []

        for index, entry in enumerate(entries):
            job.finish_step(
                index,
                state=FAILED if entry.get("success") is False else COMPLETED,
                error=entry.get("error"),
            )

        # More rows than results means the run stopped early without raising.
        for index in range(len(entries), job.step_count()):
            job.finish_step(index, state=SKIPPED, detail="not run")

        return

    # One row per configuration, in the order they were queued — which is the
    # order core runs and returns them in.
    for index in range(job.step_count()):
        if index >= len(tests):
            job.finish_step(index, state=SKIPPED, detail="not run")
            continue

        entries = tests[index].get("results") or []
        failed = sum(1 for entry in entries if entry.get("success") is False)

        job.finish_step(
            index,
            state=FAILED if entries and failed == len(entries) else COMPLETED,
            detail=f"{len(entries)}/{len(entries)}",
        )


def _tail_benchmark(
    job: Job,
    stop: threading.Event,
    names: list[str],
    prompt_count: int,
    single: bool,
) -> None:
    """Advance a benchmark's rows from the entries core logs per prompt.

    This is the only place the execution log is read, and nothing above it knows
    that is where the figures come from. If core gains a progress callback, this
    function is what it replaces.

    Args:
        job: Job to update.
        stop: Set once the core call has returned.
        names: Configuration names, in row order.
        prompt_count: Prompts per configuration.
        single: Whether the rows are prompts rather than configurations.
    """
    tail = LogTail(core_logging.get_log_file_info()["path"])
    rows = {name: index for index, name in enumerate(names)}

    try:
        while True:
            _drain(job, tail, rows, prompt_count, single)
            job.publish()

            if stop.is_set():
                _drain(job, tail, rows, prompt_count, single)
                return

            time.sleep(POLL_SECONDS)
    except Exception:  # pragma: no cover - defensive
        # The reader only decorates the bar. Its failure must not stop the
        # benchmark, and the worker still reaches a terminal status without it.
        return


def _drain(
    job: Job,
    tail: LogTail,
    rows: dict[str, int],
    prompt_count: int,
    single: bool,
) -> None:
    """Apply every log entry appended since the last read.

    Args:
        job: Job to update.
        tail: Reader for the execution log.
        rows: Configuration name to row index.
        prompt_count: Prompts per configuration.
        single: Whether the rows are prompts rather than configurations.
    """
    for entry in tail.read_new():
        if entry["component"] != BENCHMARK_COMPONENT:
            continue

        details = entry["details"]
        name = details.get("name")
        index = details.get("prompt_index")

        if name not in rows or index is None:
            continue

        failed = details.get("success") is False
        error = details.get("error")

        if single:
            job.finish_step(
                index - 1,
                state=FAILED if failed else COMPLETED,
                error=error,
            )

            if index < job.step_count():
                job.start_step(index, detail="running")
            continue

        row = rows[name]

        job.start_step(row)
        job.update_step(
            row,
            percent=100.0 * index / max(1, prompt_count),
            detail=f"{index}/{prompt_count}",
            error=error,
        )

        if index >= prompt_count:
            job.finish_step(
                row,
                state=FAILED if failed else COMPLETED,
                detail=f"{index}/{prompt_count}",
            )


# ============================================================
# Controls
# ============================================================

def cancel(job: Job) -> dict:
    """Cancel a job and wait for core to finish cleaning up.

    Args:
        job: Job to cancel.

    Returns:
        dict: Final snapshot, taken after the cleanup so it describes what
        happened rather than what was asked for. ``cleanup_complete`` is false only
        when the wait ran out with core still working.
    """
    requested = job.request_cancel("Cancelled from the progress panel")
    complete = job.wait(CANCEL_TIMEOUT) if requested else True

    snapshot = job.snapshot()
    snapshot["cancel_requested"] = requested
    snapshot["cleanup_complete"] = complete

    if not complete:
        snapshot["message"] = (
            "Cancelled; core is still cleaning up. The task will not resume."
        )

    return snapshot


def pause(job: Job) -> dict:
    """Suspend a download, or continue a suspended one.

    Args:
        job: Job to suspend or continue.

    Returns:
        dict: Snapshot afterwards, with ``pause_action`` set to ``paused``,
        ``resumed`` or ``unavailable``.
    """
    action = job.toggle_pause()

    snapshot = job.snapshot()
    snapshot["pause_action"] = action

    if action == "unavailable":
        snapshot["message"] = (
            "This operation cannot be stopped without cancelling it."
        )

    return snapshot
