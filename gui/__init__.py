"""Frontend layer for the ModelSetupHub MCP server.

Holds everything to do with the in-chat progress panel, kept out of ``main.py``:

- ``assets/`` — the panel's HTML, CSS and JavaScript, one file each.
- ``loader`` — inlines those assets into the single HTML document an MCP Apps
  ``ui://`` resource has to be.
- ``jobs`` — the job model and registry the panel reads progress from.
- ``logtail`` — incremental reader for the execution log core already writes.
- ``tracking`` — runs the unmodified core operations and publishes their progress.
- ``app`` — the Apps extension: the ``ui://`` resource plus the tools bound to it.

``main.py`` imports ``create_progress_app`` from here and passes the result to
``MCPServer(extensions=[...])``.
"""

from pathlib import Path
import sys

# This package imports core, and core imports itself as a top-level package, so
# the Core submodule root has to be on sys.path first. main.py does this before
# importing anything, but repeating it here — it is idempotent — keeps the gui
# package importable on its own, for a test or a REPL.
_CORE_ROOT = Path(__file__).resolve().parent.parent / "Core"

if str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

from .app import PROGRESS_URI, create_progress_app  # noqa: E402
from .tracking import note_download_ended  # noqa: E402

__all__ = ["PROGRESS_URI", "create_progress_app", "note_download_ended"]
