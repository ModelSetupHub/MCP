"""Incremental reader for the execution log core already writes.

Core logs every significant operation through ``core.logging.write_log`` —
including per-prompt benchmark results, which no core function returns while it
is still running. Reading those entries back is the only way this layer can
report fine-grained progress without changing core.

``core.logging.read_logs`` re-parses the whole file on every call, which is fine
for a one-off query but not for polling several times a second, so this module
keeps a byte offset and parses only what was appended since the last read. The
line format is core's, mirrored here rather than reimplemented: timestamp,
level, component, action, message, and a JSON details object, joined by ``" | "``.
"""

from __future__ import annotations

import json
from pathlib import Path

FIELD_SEPARATOR = " | "
FIELD_COUNT = 6


class LogTail:
    """Reader that yields execution log entries appended after it was created."""

    def __init__(self, path: Path) -> None:
        """Start tailing at the current end of the log.

        Entries already in the file belong to earlier operations, so the initial
        offset is the file's current size. A missing file starts at zero: core
        creates it on the first write, and the next read picks it up.

        Args:
            path: Execution log file path, from
                ``core.logging.get_execution_log_path``.
        """
        self.path = path

        try:
            self._offset = path.stat().st_size
        except OSError:
            self._offset = 0

        self._partial = ""

    def read_new(self) -> list[dict]:
        """Read and parse the entries appended since the previous call.

        A read can land mid-line while core is writing, so a trailing fragment
        is held back and completed on the next call rather than discarded.

        Returns:
            list[dict]: Parsed entries in file order, empty when nothing was
            appended or the file is not readable yet.
        """
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return []

        if not chunk:
            return []

        text = self._partial + chunk
        lines = text.split("\n")
        self._partial = lines.pop()

        entries = []

        for line in lines:
            entry = _parse(line)
            if entry is not None:
                entries.append(entry)

        return entries


def _parse(line: str) -> dict | None:
    """Parse one execution log line.

    Args:
        line: Raw line from the log file.

    Returns:
        dict | None: Entry with ``timestamp``, ``level``, ``component``,
        ``action``, ``message`` and ``details``, or None when the line is not a
        well-formed entry.
    """
    parts = line.strip().split(FIELD_SEPARATOR)

    if len(parts) != FIELD_COUNT:
        return None

    try:
        details = json.loads(parts[5])
    except json.JSONDecodeError:
        return None

    return {
        "timestamp": parts[0],
        "level": parts[1],
        "component": parts[2],
        "action": parts[3],
        "message": parts[4],
        "details": details if isinstance(details, dict) else {},
    }
