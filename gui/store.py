"""Persistent job store — the single source of truth for progress.

One run is one JSON file under ``Core/data/progress``, named after the job's
identifier. Writing goes through a temporary file and an atomic replace, so a
reader either sees the previous snapshot or the new one, never a partial write.

A run that produces a business result — a benchmark's measurements — keeps it in a
second file under ``results/``, written once when the job finishes. It lives apart
from the snapshot because a comparison result is far larger than the progress
around it, and a poll every second must not carry it.

The store is deliberately dumb: save a snapshot, load one by id. There is no
lookup by anything other than the exact identifier, so a progress request either
names a run that exists or it does not.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

from core.logging import get_execution_log_path

DIRECTORY_NAME = "progress"
RESULTS_NAME = "results"

# Age at which a record is dropped. Long enough that a conversation reopened days
# later still shows what happened.
MAX_AGE_SECONDS = 7 * 24 * 3600

# Records kept regardless of age, newest first, so a machine that runs a great
# many operations does not accumulate files without bound.
MAX_RECORDS = 200


def directory() -> Path:
    """Return the records directory, creating it if needed.

    Returns:
        Path: ``Core/data/progress``, alongside the execution log core writes.
    """
    path = get_execution_log_path().parent / DIRECTORY_NAME
    path.mkdir(parents=True, exist_ok=True)

    return path


def results_directory() -> Path:
    """Return the results directory, creating it if needed.

    Returns:
        Path: ``Core/data/progress/results``.
    """
    path = directory() / RESULTS_NAME
    path.mkdir(parents=True, exist_ok=True)

    return path


def _safe(job_id: str) -> bool:
    """Report whether an identifier is safe to use as a file name.

    Identifiers are minted by this package and contain no path characters, but a
    value arriving from a tool call is checked before it reaches the filesystem.

    Args:
        job_id: Identifier to check.

    Returns:
        bool: True when the identifier is a plain name.
    """
    return bool(job_id) and not ({"/", "\\", "."} & set(job_id))


def _write(path: Path, payload: Any) -> None:
    """Write JSON to a path atomically.

    Failures are swallowed: a progress record is not worth failing an operation
    over, and the next write will try again.

    Args:
        path: Destination file.
        payload: JSON-serialisable value.
    """
    try:
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        return


def _read(path: Path) -> Any | None:
    """Parse JSON from a path.

    Args:
        path: File to read.

    Returns:
        Any | None: The parsed value, or None when it is missing or unreadable.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save(snapshot: dict[str, Any]) -> None:
    """Write a snapshot as its job's record.

    Args:
        snapshot: Job snapshot carrying an ``id``.
    """
    job_id = snapshot.get("id")

    if not job_id:
        return

    _write(directory() / f"{job_id}.json", snapshot)


def load(job_id: str) -> dict[str, Any] | None:
    """Read one job's record.

    Args:
        job_id: Identifier of the run.

    Returns:
        dict[str, Any] | None: The snapshot, or None when there is no such record.
    """
    if not _safe(job_id):
        return None

    record = _read(directory() / f"{job_id}.json")

    return record if isinstance(record, dict) else None


def save_result(job_id: str, result: Any) -> None:
    """Write a job's business result, once, as it finishes.

    Args:
        job_id: Identifier of the run.
        result: The operation's own return value.
    """
    if not _safe(job_id):
        return

    _write(results_directory() / f"{job_id}.json", result)


def load_result(job_id: str) -> Any | None:
    """Read a job's business result.

    Args:
        job_id: Identifier of the run.

    Returns:
        Any | None: The result, or None when the run produced none — because it
        failed, was cancelled, or has not finished yet.
    """
    if not _safe(job_id):
        return None

    return _read(results_directory() / f"{job_id}.json")


def prune() -> None:
    """Drop records that are too old, and the oldest once there are too many.

    A record and its result are dropped together, so a snapshot never claims a
    result that is no longer there.

    Called when a job finishes, which is the only moment the directory grows.
    """
    try:
        paths = sorted(
            directory().glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return

    cutoff = time.time() - MAX_AGE_SECONDS

    for position, path in enumerate(paths):
        try:
            if position < MAX_RECORDS and path.stat().st_mtime >= cutoff:
                continue

            path.unlink(missing_ok=True)
            (results_directory() / path.name).unlink(missing_ok=True)
        except OSError:
            continue
