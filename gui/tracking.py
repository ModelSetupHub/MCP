"""Progress-reporting wrappers around the long-running core operations.

Each wrapper creates a job, runs the unmodified core function, and reports
progress from whatever the operation already exposes:

- Downloads publish a live queue status, so ``DownloadManager.get_status`` is
  polled on a watcher thread. The tool call returns as soon as the queue starts,
  and the watcher keeps the job current until the transfer ends.
- Benchmarks return only when every prompt has run, but core logs each prompt as
  it completes, so a watcher tails the execution log for per-prompt progress.
- Installers report nothing measurable while they work, so their jobs stay
  indeterminate and only track which step is active.

Every wrapper also gives its job the controls the panel offers. Cancelling uses
a ``CancellationToken`` for the benchmarks and installers, and the manager's own
``cancel`` for downloads; when it lands, core raises ``OperationCancelled`` after
undoing the operation's side effects, and the wrapper turns that into
``Job.finish_cancelled`` — which drops the job, leaving core's log entry as the
only trace. Downloads additionally expose ``pause``/``resume``, which suspend the
transfer without touching the queue, so the panel can offer Stop alongside Cancel
for them.

Nothing here reaches into core's internals: it calls the same public functions
the plain tools call, and reads the status and log surfaces core already writes.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
import time
from typing import Any

# Core is imported as the top-level `core` package, the same way core imports
# itself. `Core.core.x` resolves to a second, distinct copy of the same file, with
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
    PAUSED,
    RUNNING,
    SKIPPED,
    Job,
    registry,
)
from .logtail import LogTail

POLL_SECONDS = 0.4

# How long a cancellation waits for core's cleanup before reporting back.
CANCEL_TIMEOUT = 60.0

# States DownloadManager reports per queue item, mapped onto step states.
DOWNLOAD_STEP_STATES = {
    "waiting": "waiting",
    "connecting": RUNNING,
    "downloading": RUNNING,
    "retrying": RUNNING,
    "paused": PAUSED,
    "completed": COMPLETED,
    "failed": FAILED,
    "skipped": SKIPPED,
    "cancelled": CANCELLED,
}


def _spawn(target: Callable[[], None], name: str) -> None:
    """Run a watcher on a daemon thread.

    Args:
        target: Zero-argument callable to run.
        name: Thread name, for debugging.
    """
    threading.Thread(target=target, name=name, daemon=True).start()


def format_bytes(count: float | None) -> str:
    """Format a byte count for the panel.

    Args:
        count: Number of bytes, or None when the size is unknown.

    Returns:
        str: Size with a binary unit suffix, or ``"?"`` when unknown.
    """
    if count is None:
        return "?"

    size = float(count)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024

    return f"{size:.1f} TB"


def track_download(
    manager: DownloadManager,
    session_id: str,
) -> Job:
    """Start a session's queue and follow it on a watcher thread.

    ``DownloadManager.start`` returns as soon as its worker thread is running, so
    the tool call that triggered this does not block for the length of the
    transfer. The watcher polls the manager's own status until the queue stops.

    Args:
        manager: Session manager to start.
        session_id: Session identifier, shown on the panel.

    Returns:
        Job: The job the panel polls.

    Raises:
        Exception: Whatever ``DownloadManager.start`` raises, for example when
            the queue is empty. The job is marked failed first.
    """
    status = manager.get_status()
    filenames = [item["filename"] for item in status["downloads"]]

    job = registry.create(
        title=f"Downloading {len(filenames)} file(s)",
        subtitle=f"session {session_id}",
    )
    job.add_steps(filenames)
    # DownloadManager owns both controls already: cancel() removes the files the
    # session produced, and pause()/resume() suspend the transfer while keeping
    # the queue and its partial data intact.
    job.set_canceller(lambda reason: manager.cancel(reason=reason))
    job.set_pause_controls(pause=manager.pause, resume=manager.resume)

    try:
        manager.start()
    except Exception as error:
        job.log(str(error))
        job.finish(state=FAILED, subtitle=str(error))
        raise

    _spawn(
        lambda: _watch_download(job, manager),
        name=f"progress-download-{session_id}",
    )

    return job


def _watch_download(job: Job, manager: DownloadManager) -> None:
    """Mirror a download session's status onto a job until it stops.

    ``DownloadManager.start`` marks the session running before it returns, so a
    reading of ``running`` is trustworthy from the first poll: the watcher can
    stop as soon as it sees the queue stop, with no settling delay.

    Args:
        job: Job to update.
        manager: Session being watched.
    """
    while True:
        status = manager.get_status()
        _apply_download_status(job, status)

        if not status["running"]:
            break

        time.sleep(POLL_SECONDS)

    final = manager.get_status()
    _apply_download_status(job, final)

    states = {item["status"] for item in final["downloads"]}

    if final["cancelled"]:
        # cancel() removes every file the session produced, including the ones
        # that had finished, so a row still reading "completed" would be pointing
        # at something no longer on disk.
        for index, item in enumerate(final["downloads"]):
            if item["status"] == "completed":
                job.finish_step(index, state=CANCELLED, detail="removed")

        # The cancellation is already logged and the files are gone, so dropping
        # the job leaves nothing about the download behind.
        job.finish_cancelled(
            final.get("cancel_reason") or "Cancelled; downloaded files removed."
        )
    elif "failed" in states:
        failed = [
            item["filename"]
            for item in final["downloads"]
            if item["status"] == "failed"
        ]
        job.finish(
            state=FAILED,
            subtitle=f"Failed: {', '.join(failed)}",
        )
    else:
        job.finish(state=COMPLETED, subtitle="All files downloaded.")


def _apply_download_status(job: Job, status: dict) -> None:
    """Copy one download status reading onto a job.

    Args:
        job: Job to update.
        status: Return value of ``DownloadManager.get_status``.
    """
    downloaded_total = 0
    expected_total = 0
    sizes_known = True

    job.sync_paused(bool(status["paused"]))

    for index, item in enumerate(status["downloads"]):
        downloaded = item.get("downloaded") or 0
        total = item.get("total")

        downloaded_total += downloaded

        if total:
            expected_total += total
        elif item["status"] not in (COMPLETED, SKIPPED):
            sizes_known = False

        percent = 100.0 * downloaded / total if total else None
        if item["status"] == "completed":
            percent = 100.0

        job.update_step(
            index,
            percent=percent,
            detail=(
                f"{format_bytes(downloaded)} / {format_bytes(total)}"
                if downloaded or total
                else item["status"]
            ),
            note=item.get("error"),
        )

        step_state = DOWNLOAD_STEP_STATES.get(item["status"], item["status"])
        if step_state == RUNNING:
            job.start_step(index)
        elif step_state == PAUSED:
            # Keep the row running: the transfer is suspended, not finished, and
            # closing the step here would make the bar look like it moved on.
            job.start_step(index)
            job.update_step(index, detail=f"{format_bytes(downloaded)} · stopped")
        elif step_state != "waiting":
            job.finish_step(index, state=step_state)

    if sizes_known and expected_total:
        job.set_percent(100.0 * downloaded_total / expected_total)

    job.set_metrics(
        [
            {"label": "downloaded", "value": format_bytes(downloaded_total)},
            {
                "label": "total",
                "value": format_bytes(expected_total) if expected_total else "?",
            },
            {
                "label": "file",
                "value": f"{min(status['current_index'] + 1, status['total_files'])}"
                f"/{status['total_files']}",
            },
        ]
    )


def run_test_tracked(
    model: str,
    prompts: list[str],
    config: dict | None,
    name: str,
    include_output: bool,
) -> tuple[dict, Job]:
    """Benchmark one configuration while reporting per-prompt progress.

    ``experiment.run_test`` returns only once every prompt has run, so the job is
    advanced from the execution log: core writes an entry per prompt, and a
    watcher thread tails it while the benchmark is in flight.

    Args:
        model: Model name or tag to benchmark.
        prompts: Prompts to run, in order.
        config: Optional generation options.
        name: Label recorded with the results.
        include_output: Whether to include generated text in the results.

    Returns:
        tuple[dict, Job]: Core's benchmark result and the job the panel polled.

    Raises:
        Exception: Whatever ``experiment.run_test`` raises. The job is marked
            failed first.
    """
    job = registry.create(
        title=f"Benchmarking {model}",
        subtitle=f"{len(prompts)} prompt(s) · {name}",
    )
    job.add_steps([f"prompt {index}" for index in range(1, len(prompts) + 1)])

    token = CancellationToken()
    job.set_canceller(token.cancel)

    tail = LogTail(core_logging.get_execution_log_path())
    stop = threading.Event()
    _spawn(
        lambda: _watch_test(job=job, tail=tail, stop=stop, name=name),
        name="progress-benchmark",
    )

    try:
        result = experiment.run_test(
            model=model,
            prompts=prompts,
            config=config,
            name=name,
            include_output=include_output,
            cancellation=token,
        )
    except OperationCancelled as error:
        stop.set()
        # Core has unloaded the model, discarded the partial results and logged
        # the cancellation; dropping the job removes the last trace.
        job.finish_cancelled(str(error))
        raise
    except Exception as error:
        stop.set()
        job.finish(state=FAILED, subtitle=str(error))
        raise
    finally:
        stop.set()

    job.finish(state=COMPLETED, subtitle=_summarise(result))

    return result, job


def compare_tests_tracked(
    model: str,
    prompts: list[str],
    configurations: list[dict],
    include_output: bool,
) -> tuple[dict, Job]:
    """Compare configurations while reporting progress per configuration.

    ``experiment.compare_tests`` is called unchanged, so the validation,
    normalisation and result shape all stay core's. The panel's steps are built
    from the entry core logs when it starts — that entry carries the normalised
    configuration list — so the names shown are the ones core actually used.

    Args:
        model: Model name or tag to benchmark.
        prompts: Prompts run against every configuration.
        configurations: Configurations to compare, each ``{"name", "options"}``.
        include_output: Whether to include generated text in the results.

    Returns:
        tuple[dict, Job]: Core's comparison result and the job the panel polled.

    Raises:
        Exception: Whatever ``experiment.compare_tests`` raises. The job is
            marked failed first.
    """
    job = registry.create(
        title=f"Comparing {len(configurations)} configuration(s)",
        subtitle=f"{model} · {len(prompts)} prompt(s) each",
    )

    token = CancellationToken()
    job.set_canceller(token.cancel)

    tail = LogTail(core_logging.get_execution_log_path())
    stop = threading.Event()
    _spawn(
        lambda: _watch_comparison(
            job=job,
            tail=tail,
            stop=stop,
            model=model,
            prompt_count=len(prompts),
        ),
        name="progress-compare",
    )

    try:
        result = experiment.compare_tests(
            model=model,
            prompts=prompts,
            configurations=configurations,
            include_output=include_output,
            cancellation=token,
        )
    except OperationCancelled as error:
        stop.set()
        job.finish_cancelled(str(error))
        raise
    except Exception as error:
        stop.set()
        job.finish(state=FAILED, subtitle=str(error))
        raise
    finally:
        stop.set()

    job.finish(state=COMPLETED, subtitle="Comparison complete.")

    return result, job


def _watch_test(
    job: Job,
    tail: LogTail,
    stop: threading.Event,
    name: str,
) -> None:
    """Advance a benchmark job from the entries core logs per prompt.

    Args:
        job: Job to update.
        tail: Reader positioned at the end of the log when the job started.
        stop: Set once the benchmark call has returned.
        name: Test label, used to ignore entries from a concurrent benchmark.
    """
    while True:
        for entry in tail.read_new():
            _apply_prompt_entry(job, entry, name=name, step_offset=0)

        if stop.is_set():
            # Drain whatever core wrote between the last read and returning.
            for entry in tail.read_new():
                _apply_prompt_entry(job, entry, name=name, step_offset=0)
            return

        time.sleep(POLL_SECONDS)


def _watch_comparison(
    job: Job,
    tail: LogTail,
    stop: threading.Event,
    model: str,
    prompt_count: int,
) -> None:
    """Advance a comparison job from core's log entries.

    Core logs the normalised configuration list when the comparison starts, so
    the job's steps are built from that entry rather than guessed here; each
    configuration then advances as its prompts are logged.

    Args:
        job: Job to update.
        tail: Reader positioned at the end of the log when the job started.
        stop: Set once the comparison call has returned.
        model: Model being benchmarked, used to ignore unrelated entries.
        prompt_count: Prompts per configuration, for per-step percentages.
    """
    # Configuration name to step index, filled in from the "Tests started" entry.
    steps: dict[str, int] = {}

    while True:
        for entry in tail.read_new():
            details = entry["details"]

            if entry["action"] == "compare" and not steps:
                if details.get("model") != model:
                    continue
                names = [
                    configuration.get("name", f"configuration_{index}")
                    for index, configuration in enumerate(
                        details.get("configurations") or [], start=1
                    )
                ]
                if names:
                    job.add_steps(names, weight=float(prompt_count or 1))
                    steps = {name: index for index, name in enumerate(names)}
                continue

            name = details.get("name")
            if name in steps:
                _apply_prompt_entry(
                    job,
                    entry,
                    name=name,
                    step_offset=steps[name],
                    prompt_count=prompt_count,
                )

        if stop.is_set():
            for entry in tail.read_new():
                name = entry["details"].get("name")
                if name in steps:
                    _apply_prompt_entry(
                        job,
                        entry,
                        name=name,
                        step_offset=steps[name],
                        prompt_count=prompt_count,
                    )
            return

        time.sleep(POLL_SECONDS)


def _apply_prompt_entry(
    job: Job,
    entry: dict,
    name: str,
    step_offset: int,
    prompt_count: int | None = None,
) -> None:
    """Apply one benchmark log entry to a job.

    With ``prompt_count`` unset the job has one step per prompt, so each entry
    closes its own step. With it set the job has one step per configuration, and
    an entry advances that step's percentage instead.

    Args:
        job: Job to update.
        entry: Parsed execution log entry.
        name: Test label the job is following.
        step_offset: Index of the first step this label owns.
        prompt_count: Prompts per configuration, in configuration mode.
    """
    if entry["component"] != "ollama/experiment":
        return

    details = entry["details"]

    if details.get("name") != name:
        return

    index = details.get("prompt_index")

    if index is None:
        return

    rate = details.get("output_tokens_per_second")
    duration = details.get("duration_seconds")
    failed = details.get("success") is False

    detail = f"{rate:.1f} tok/s" if isinstance(rate, (int, float)) else None
    if detail is None and isinstance(duration, (int, float)):
        detail = f"{duration:.1f}s"

    if prompt_count is None:
        job.finish_step(
            step_offset + index - 1,
            state=FAILED if failed else COMPLETED,
            detail=detail,
            note=details.get("error"),
        )
        if step_offset + index < _step_count(job):
            job.start_step(step_offset + index, detail="running")
    else:
        job.start_step(step_offset)
        job.update_step(
            step_offset,
            percent=100.0 * index / max(1, prompt_count),
            detail=(
                f"{index}/{prompt_count} · {detail}"
                if detail
                else f"{index}/{prompt_count}"
            ),
            note=details.get("error"),
        )
        if index >= prompt_count:
            job.finish_step(step_offset, detail=f"{index}/{prompt_count}")

    if failed:
        job.log(f"{name} prompt {index} failed: {details.get('error')}")
    elif detail:
        job.log(f"{name} prompt {index}: {detail}")


def _step_count(job: Job) -> int:
    """Count a job's steps.

    Args:
        job: Job to inspect.

    Returns:
        int: Number of steps currently registered.
    """
    return len(job.snapshot()["steps"])


def _summarise(result: dict) -> str:
    """Describe a benchmark result in one line.

    Args:
        result: Return value of ``experiment.run_test``.

    Returns:
        str: Averaged output rate and duration, or a plain completion note when
        no prompt succeeded.
    """
    summary = result.get("summary") or {}
    rate = summary.get("average_output_tokens_per_second")
    duration = summary.get("average_duration_seconds")

    parts = []

    if isinstance(rate, (int, float)):
        parts.append(f"{rate:.1f} tok/s average")
    if isinstance(duration, (int, float)):
        parts.append(f"{duration:.1f}s per prompt")

    return " · ".join(parts) if parts else "Finished with no successful prompt."


def track_steps(
    title: str,
    subtitle: str | None,
    steps: list[tuple[str, Callable[[CancellationToken], Any]]],
) -> tuple[list[Any], Job]:
    """Run a sequence of core calls as one cancellable job with a step each.

    For operations that report nothing measurable while they work — an installer
    running silently, pip resolving a package — the value of the panel is showing
    which step is active, that the server has not stalled, and offering a way out.
    Those steps stay indeterminate and are closed as each call returns.

    Each callable receives the job's cancellation token and must pass it to the
    core function it calls, so a cancellation reaches the work rather than only
    taking effect between steps.

    Args:
        title: Headline shown on the panel.
        subtitle: Optional secondary line.
        steps: ``(step name, callable taking the token)`` pairs, run in order.

    Returns:
        tuple[list[Any], Job]: Each callable's return value, and the job.

    Raises:
        OperationCancelled: If the job is cancelled. Core has already undone the
            interrupted step, and the job is dropped, so nothing survives but
            core's log entry.
        Exception: Whatever a step raises. The failing step and the job are both
            marked failed first, and the remaining steps are not run.
    """
    job = registry.create(title=title, subtitle=subtitle)
    job.add_steps([name for name, _ in steps])

    token = CancellationToken()
    job.set_canceller(token.cancel)

    results = []

    for index, (name, call) in enumerate(steps):
        job.start_step(index, detail="running")

        try:
            results.append(call(token))
        except OperationCancelled as error:
            job.finish_cancelled(str(error))
            raise
        except Exception as error:
            job.finish_step(index, state=FAILED, detail="failed", note=str(error))
            job.finish(state=FAILED, subtitle=str(error))
            raise

        job.finish_step(index, detail="done")
        job.log(f"{name}: done")

    job.finish(state=COMPLETED, subtitle="Finished.")

    return results, job


def cancel_job(job: Job) -> dict:
    """Cancel a running job and wait for core to finish cleaning up.

    Args:
        job: Job to cancel.

    Returns:
        dict: Final snapshot, taken after the cleanup so it reflects what
        actually happened rather than what was requested.
    """
    requested = job.request_cancel()

    if requested:
        job.wait_until_finished(CANCEL_TIMEOUT)

    snapshot = job.snapshot()
    snapshot["cancel_requested"] = requested

    return snapshot


def pause_job(job: Job) -> dict:
    """Suspend a running download, or resume a suspended one.

    Args:
        job: Job to pause or resume.

    Returns:
        dict: Snapshot afterwards, with ``pause_action`` set to ``paused``,
        ``resumed``, or ``unavailable`` when the operation cannot be suspended.
    """
    action = job.toggle_pause()

    snapshot = job.snapshot()
    snapshot["pause_action"] = action

    if action == "unavailable":
        snapshot["subtitle"] = (
            "This operation cannot be stopped without cancelling it."
            if not job.pausable
            else snapshot["subtitle"]
        )

    return snapshot


