"""The progress panel as an MCP Apps extension.

MCP Apps (``io.modelcontextprotocol/ui``) is how a tool result carries a UI: the
tool advertises ``_meta.ui.resourceUri``, which points at a ``ui://`` HTML
resource the client renders inline in the conversation — the same chat page the
tool was called from, in a sandboxed iframe.

Two kinds of tool live here:

- The long-running operations, each bound to the panel. They delegate to
  ``gui.tracking``, which runs the unmodified core function and publishes
  progress alongside it.
- ``progress_get_status``, which the panel polls. It is marked
  ``visibility=["app"]`` so it serves the panel without cluttering the model's
  tool list.

Every tool here degrades to plain text: a client that did not negotiate Apps
receives the same return value it would from the corresponding tool in
``main.py``, minus the panel.
"""

from __future__ import annotations

from collections.abc import Callable
import functools
from typing import Any, TypeVar

from mcp.server.apps import Apps
from mcp.server.mcpserver.exceptions import MCPServerError, ToolError
from mcp.types import ToolAnnotations

from core.ollama import runtime
from core.python import installer, tools as python_tools

from . import tracking
from .jobs import registry
from .loader import load_progress_app_html

PROGRESS_URI = "ui://modelsetuphub/progress.html"

LONG_RUNNING = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

CallableT = TypeVar("CallableT", bound=Callable[..., Any])


def surface_core_errors(function: CallableT) -> CallableT:
    """Forward exceptions raised by core to the MCP client verbatim.

    Mirrors the decorator in ``main.py``: the SDK reports any exception other
    than ``ToolError`` as a generic tool crash, which would hide the descriptive
    messages core raises.

    Args:
        function: Tool function that calls into the core package.

    Returns:
        CallableT: Wrapped function preserving the original error text.
    """

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except MCPServerError:
            raise
        except Exception as error:
            raise ToolError(f"{type(error).__name__}: {error}") from error

    return wrapper  # type: ignore[return-value]


def create_progress_app(get_session: Callable[[str], Any]) -> Apps:
    """Build the Apps extension carrying the panel and its tools.

    Args:
        get_session: Resolver for a download session id, supplied by the layer
            that owns the session registry — ``main.py`` — so this module does
            not duplicate that state.

    Returns:
        Apps: Extension to pass as ``MCPServer(extensions=[...])``.
    """
    apps = Apps()

    apps.add_html_resource(
        PROGRESS_URI,
        load_progress_app_html(),
        name="progress-panel",
        title="Operation progress",
        description=(
            "Live progress for downloads, installations and benchmarks, "
            "rendered inline in the conversation."
        ),
        prefers_border=False,
    )

    register_status_tool(apps)
    register_control_tools(apps)
    register_download_tools(apps, get_session)
    register_benchmark_tools(apps)
    register_install_tools(apps)

    return apps


def register_status_tool(apps: Apps) -> None:
    """Register the tool the panel polls for progress.

    Args:
        apps: Extension the tool is added to.
    """

    @apps.tool(
        resource_uri=PROGRESS_URI,
        visibility=["app"],
        name="progress_get_status",
        title="Get operation progress",
        description=(
            "Report a tracked operation's progress: state, overall percentage, "
            "per-step rows, metrics and recent log lines. With no id, reports "
            "the most recently started operation. The progress panel polls this "
            "while an operation runs; it is not normally called directly."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def progress_get_status(progress_id: str | None = None) -> dict:
        """Return one operation's progress snapshot.

        Args:
            progress_id: Identifier reported by a tracked tool; omit for the
                newest operation.

        Returns:
            dict: Progress snapshot, or a placeholder when nothing is tracked.
        """
        job = registry.get(progress_id) if progress_id else registry.latest()

        if job is None:
            return _untracked(progress_id)

        return job.snapshot()


def _untracked(progress_id: str | None) -> dict:
    """Build the placeholder for a job that does not exist.

    Returned both before an operation has registered its job and after a
    cancelled one has been purged. ``tracked`` tells the panel which case it is
    looking at, so it can keep showing "starting" in the first and hold the final
    state in the second, instead of flashing this placeholder in either.

    Args:
        progress_id: Identifier that was asked for, if any.

    Returns:
        dict: Snapshot-shaped placeholder.
    """
    return {
        "progress_id": progress_id,
        "tracked": False,
        "title": "Nothing to report",
        "subtitle": None,
        "state": "completed",
        "percent": None,
        "determinate": False,
        "cancellable": False,
        "pausable": False,
        "paused": False,
        "cancelling": False,
        "steps": [],
        "metrics": [],
        "log": [],
        "elapsed_seconds": 0.0,
        "eta_seconds": None,
    }


def register_control_tools(apps: Apps) -> None:
    """Register the panel's Cancel and Stop buttons.

    The two are deliberately different operations. Cancel ends the task and has
    core undo it, and applies to all three long-running operations. Stop only
    suspends a download and leaves the task intact, so it exists for downloads
    alone — they are the only operation core can pause and resume.

    Args:
        apps: Extension the tools are added to.
    """

    @apps.tool(
        resource_uri=PROGRESS_URI,
        name="progress_cancel",
        title="Cancel a running operation",
        description=(
            "Cancel a tracked operation and undo what it had done so far. The "
            "download, benchmark or installation stops at its next safe point, "
            "core removes everything the operation created — partial and "
            "completed downloads, a half-finished installation, packages the run "
            "added, a loaded model — and records a cancelled entry in the "
            "execution log, which is the only trace left behind. Cannot be "
            "undone; to suspend a download and keep it, use progress_pause "
            "instead. With no id, cancels the most recently started operation. "
            "The progress panel's Cancel button calls this."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def progress_cancel(progress_id: str | None = None) -> dict:
        """Cancel a tracked operation and wait for its cleanup to finish.

        Args:
            progress_id: Identifier reported by a tracked tool; omit for the
                newest operation.

        Returns:
            dict: Final progress snapshot after the cleanup, with
            ``cancel_requested`` false when the operation had already finished.
        """
        job = registry.get(progress_id) if progress_id else registry.latest()

        if job is None:
            snapshot = _untracked(progress_id)
            snapshot["title"] = "Nothing to cancel"
            snapshot["subtitle"] = (
                "That operation is not running; nothing was changed."
            )
            snapshot["cancel_requested"] = False
            return snapshot

        return tracking.cancel_job(job)

    @apps.tool(
        resource_uri=PROGRESS_URI,
        name="progress_pause",
        title="Stop or resume a download",
        description=(
            "Stop a running download without cancelling it, or resume one that "
            "was stopped. The queue, the files already fetched and the partial "
            "data are all kept, and resuming continues the active file from where "
            "it left off via an HTTP range request. Downloads only: benchmarks "
            "and installations cannot be suspended, and calling this for one "
            "reports that it is unavailable rather than cancelling it. With no "
            "id, targets the most recently started operation. The progress "
            "panel's Stop button calls this."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def progress_pause(progress_id: str | None = None) -> dict:
        """Stop or resume a tracked download.

        Args:
            progress_id: Identifier reported by a tracked tool; omit for the
                newest operation.

        Returns:
            dict: Progress snapshot afterwards, with ``pause_action`` set to
            ``paused``, ``resumed`` or ``unavailable``.
        """
        job = registry.get(progress_id) if progress_id else registry.latest()

        if job is None:
            snapshot = _untracked(progress_id)
            snapshot["title"] = "Nothing to stop"
            snapshot["subtitle"] = (
                "That operation is not running; nothing was changed."
            )
            snapshot["pause_action"] = "unavailable"
            return snapshot

        return tracking.pause_job(job)


def register_download_tools(
    apps: Apps,
    get_session: Callable[[str], Any],
) -> None:
    """Register the download start tool that shows the panel.

    Args:
        apps: Extension the tool is added to.
        get_session: Resolver for a download session id.
    """

    @apps.tool(
        resource_uri=PROGRESS_URI,
        name="download_start_with_progress",
        title="Start downloading with a progress bar",
        description=(
            "Start processing a session's queue and show a live progress bar in "
            "the conversation: per-file bars with transferred and total bytes, "
            "an overall percentage and an ETA. Returns immediately, like "
            "download_start; the panel keeps updating while the background "
            "thread works. Queue every file first, and prefer this over "
            "download_start whenever a human is watching."
        ),
        annotations=LONG_RUNNING,
    )
    @surface_core_errors
    def download_start_with_progress(session_id: str) -> dict:
        """Start a session's queue with progress reporting.

        Args:
            session_id: Session created by download_create_session.

        Returns:
            dict: Session status just after starting, plus the ``progress_id``
            the panel polls.
        """
        manager = get_session(session_id)
        job = tracking.track_download(manager, session_id=session_id)

        return {
            "progress_id": job.progress_id,
            "status": manager.get_status(),
        }


def register_benchmark_tools(apps: Apps) -> None:
    """Register the benchmarking tools that show the panel.

    Args:
        apps: Extension the tools are added to.
    """

    @apps.tool(
        resource_uri=PROGRESS_URI,
        name="ollama_run_test_with_progress",
        title="Benchmark one configuration with a progress bar",
        description=(
            "Run a set of prompts against one model with temporary generation "
            "parameters and show a live progress bar in the conversation, one "
            "row per prompt with its tokens-per-second as it finishes. Returns "
            "the same measurements as ollama_run_test. Long-running: each prompt "
            "is a full generation, which is why the panel is worth showing."
        ),
        annotations=LONG_RUNNING,
    )
    @surface_core_errors
    def ollama_run_test_with_progress(
        model_name: str,
        prompts: list[str],
        config: dict | None = None,
        name: str = "test",
        include_output: bool = False,
    ) -> dict:
        """Benchmark a model under a single configuration, with progress.

        Args:
            model_name: Model name or tag to benchmark.
            prompts: Prompts to run, in order.
            config: Optional generation options, for example
                {"temperature": 0.7, "num_ctx": 4096}.
            name: Label recorded with the results.
            include_output: Whether to include generated text alongside metrics.

        Returns:
            dict: Per-prompt results and summary, plus the ``progress_id``.
        """
        result, job = tracking.run_test_tracked(
            model=model_name,
            prompts=prompts,
            config=config,
            name=name,
            include_output=include_output,
        )

        return {"progress_id": job.progress_id, "result": result}

    @apps.tool(
        resource_uri=PROGRESS_URI,
        name="ollama_compare_tests_with_progress",
        title="Compare configurations with a progress bar",
        description=(
            "Run the same prompts against one model under several parameter "
            "configurations and show a live progress bar in the conversation, "
            "one row per configuration advancing as its prompts complete. "
            "Returns the same side-by-side results as ollama_compare_tests. "
            "Long-running: total time is the prompt count multiplied by the "
            "configuration count, so prefer this variant."
        ),
        annotations=LONG_RUNNING,
    )
    @surface_core_errors
    def ollama_compare_tests_with_progress(
        model_name: str,
        prompts: list[str],
        configurations: list[dict],
        include_output: bool = False,
    ) -> dict:
        """Compare a model across configurations, with progress.

        Args:
            model_name: Model name or tag to benchmark.
            prompts: Prompts run against every configuration.
            configurations: Configurations to compare, each shaped as
                {"name": "warm", "options": {"temperature": 0.9}}.
            include_output: Whether to include generated text alongside metrics.

        Returns:
            dict: One benchmark result per configuration, plus the
            ``progress_id``.
        """
        result, job = tracking.compare_tests_tracked(
            model=model_name,
            prompts=prompts,
            configurations=configurations,
            include_output=include_output,
        )

        return {"progress_id": job.progress_id, "result": result}


def register_install_tools(apps: Apps) -> None:
    """Register the installation tools that show the panel.

    An installer reports nothing measurable while it runs, so these jobs stay
    indeterminate: the panel shows which step is active and that the server is
    still alive, which is the whole reason a bar is wanted here.

    Args:
        apps: Extension the tools are added to.
    """

    @apps.tool(
        resource_uri=PROGRESS_URI,
        name="ollama_install_with_progress",
        title="Install Ollama with a progress bar",
        description=(
            "Run a local Ollama installer and show a live progress bar in the "
            "conversation while it runs, then start the service and report its "
            "status. Takes the path to an installer already on disk; it does not "
            "download anything, and the installer may prompt interactively. The "
            "bar is indeterminate because the installer reports no percentage."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_install_with_progress(installer_path: str) -> dict:
        """Install Ollama and start it, with progress.

        Args:
            installer_path: Path to the installer executable on disk.

        Returns:
            dict: Runtime status afterwards, plus the ``progress_id``.
        """
        _, job = tracking.track_steps(
            title="Installing Ollama",
            subtitle=installer_path,
            steps=[
                (
                    "Run installer",
                    lambda token: runtime.install(
                        installer_path=installer_path,
                        cancellation=token,
                    ),
                ),
                ("Start service", lambda token: runtime.start()),
            ],
        )

        return {"progress_id": job.progress_id, "status": runtime.get_status()}

    @apps.tool(
        resource_uri=PROGRESS_URI,
        name="python_install_python_with_progress",
        title="Install Python with a progress bar",
        description=(
            "Run a local Windows Python installer in quiet mode with PATH "
            "prepending and show a live progress bar in the conversation while "
            "it runs. The installer must already be on disk; nothing is "
            "downloaded. Modifies system state outside this project and may "
            "require elevation for an all-users install. Returns the detected "
            "Python installations afterwards."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def python_install_python_with_progress(
        installer_path: str,
        all_users: bool = False,
    ) -> dict:
        """Install Python from a local installer, with progress.

        Args:
            installer_path: Path to the installer executable.
            all_users: Whether to install for all users instead of the current one.

        Returns:
            dict: Detected Python versions afterwards, plus the ``progress_id``.
        """
        results, job = tracking.track_steps(
            title="Installing Python",
            subtitle=installer_path,
            steps=[
                (
                    "Run installer",
                    lambda token: installer.install_python(
                        installer_path=installer_path,
                        all_users=all_users,
                        cancellation=token,
                    ),
                ),
            ],
        )

        return {"progress_id": job.progress_id, "result": results[0]}

    @apps.tool(
        resource_uri=PROGRESS_URI,
        name="python_install_packages_with_progress",
        title="Install packages with a progress bar",
        description=(
            "Install packages with pip into a virtual environment — or into the "
            "interpreter running this server when none is given — showing a live "
            "progress bar in the conversation with one row per package. Accepts "
            "version specifiers such as 'numpy==1.26.4' and downloads from the "
            "configured package index."
        ),
        annotations=LONG_RUNNING,
    )
    @surface_core_errors
    def python_install_packages_with_progress(
        packages: list[str],
        env_path: str | None = None,
    ) -> dict:
        """Install packages with pip, with progress.

        Each package is installed by its own pip call so the panel can close one
        row at a time; pip resolves dependencies per call either way.

        Args:
            packages: Package names or version specifiers.
            env_path: Optional target virtual environment.

        Returns:
            dict: Per-package pip output, plus the ``progress_id``.
        """
        outputs, job = tracking.track_steps(
            title=f"Installing {len(packages)} package(s)",
            subtitle=env_path or "server interpreter",
            steps=[
                (
                    package,
                    lambda token, package=package: python_tools.install_packages(
                        packages=[package],
                        environment=env_path,
                        cancellation=token,
                    ),
                )
                for package in packages
            ],
        )

        return {
            "progress_id": job.progress_id,
            "result": dict(zip(packages, outputs)),
        }

