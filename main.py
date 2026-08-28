"""ModelSetupHub MCP server.

Serves the read-only ``core`` package from the ``Core`` submodule over the Model
Context Protocol. Every tool is a thin pass-through to an existing core
function; no business logic lives here.

Run over stdio:

    python main.py

Client configuration:

    {
      "mcpServers": {
        "modelsetuphub": {
          "command": "python",
          "args": ["C:/path/to/main.py"]
        }
      }
    }
"""

from collections.abc import Callable
import functools
from pathlib import Path
import sys
import threading
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import MCPServerError, ToolError
from mcp.types import ToolAnnotations

# The core package imports itself as a top-level package (`from core.logging
# import write_log`), so the directory *containing* it — the Core submodule root,
# not `core` itself — goes on sys.path before it can be imported.
PROJECT_ROOT = Path(__file__).resolve().parent
CORE_ROOT = PROJECT_ROOT / "Core"

if not (CORE_ROOT / "core" / "__init__.py").is_file():
    raise RuntimeError(
        f"The core package was not found at {CORE_ROOT / 'core'}. The Core "
        f"submodule must be checked out: run 'git submodule update --init' "
        f"in {PROJECT_ROOT}."
    )

if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

# Core is imported the same way it imports itself — as the top-level `core`
# package — because `Core.core.x` and `core.x` are two distinct module objects
# holding two distinct copies of every class. Mixing the spellings means an
# exception raised inside core does not match the class this layer caught it with.
from core import logging as core_logging  # noqa: E402
from core.system import hardware, scanner  # noqa: E402
from core.download_manager.manager import (  # noqa: E402
    ALLOWED_DOMAINS,
    DownloadManager,
)
from core.ollama import experiment, model, runtime  # noqa: E402
from core.python import environment, installer, tools  # noqa: E402

# The in-chat progress panel and the tools bound to it. Every frontend file lives
# under gui/; this layer only hands it a way to resolve a download session.
from gui import create_progress_app, note_download_ended  # noqa: E402

SERVER_NAME = "modelsetuphub"
SERVER_TITLE = "ModelSetupHub"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
ModelSetupHub manages local AI environments on this machine: hardware
discovery, the Ollama service and its models, model benchmarking, Python
environments and scripts, and downloads from a fixed set of allowed domains.

Suggested order of operations:

- Before recommending a model, call system_scan (or system_get_gpu_info and
  system_get_storage_info for a quick check) to see what the hardware supports.
- Ollama tools need the service running: check ollama_get_status and call
  ollama_start if needed.
- To compare generation parameters, use ollama_run_test or
  ollama_compare_tests, which apply settings per request. Only use
  ollama_configure_model when a persistent model variant is wanted.
- Downloads are asynchronous: create a session, queue URLs, start it, then poll
  download_get_status. Only the domains listed by
  download_list_allowed_domains are accepted.
- Core logs every significant operation; logs_read surfaces detail that a
  tool's return value may not include, especially after a failure.

Downloads and benchmarks have a '_with_progress' variant that renders a live
progress bar in the conversation, with a Cancel button. Prefer those variants;
they return the same data as the plain tool, plus a progress_id. Clients that
cannot render the panel still get the return value. Installations have no
progress variant — call the plain tools for those.

Two different controls act on a running operation:

- progress_cancel ends the task and has core undo it — partial and completed
  downloads are deleted, a loaded model is unloaded — leaving only a
  'cancelled' entry in the execution log, which logs_read will show. It applies
  to downloads and benchmarks alike, and cannot be undone.
- progress_pause stops a download without cancelling it: the queue and the bytes
  already fetched are kept, and calling it again resumes from where it left off.
  Downloads only.

A cancelled operation is removed completely, not left in a cancelled state. For a
download that includes the session itself: its id becomes free, and downloading
the same files again means calling download_create_session and download_add
again. Do not try to restart or add to a cancelled session — it will refuse.

One operation is tracked by one progress bar. Starting a download or benchmark
that is already running is rejected rather than started twice; poll the
progress bar named in the error, or cancel it first.

Tools that delete models, environments, script files, or queued downloads are
irreversible and are annotated as destructive. Confirm the target before
calling them.
"""

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

CallableT = TypeVar("CallableT", bound=Callable[..., Any])


def surface_core_errors(function: CallableT) -> CallableT:
    """Forward exceptions raised by core to the MCP client verbatim.

    The SDK treats any exception other than ``ToolError`` as a crash: the client
    gets a generic ``Error executing tool <name>`` and the real message stays on
    the server. Core raises descriptive exceptions, so they are re-raised here
    with type name and message intact and the original chained as ``__cause__``.
    Core exceptions are forwarded, never replaced.

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
            # Already an MCP-level error, including argument validation failures.
            raise
        except Exception as error:
            raise ToolError(f"{type(error).__name__}: {error}") from error

    return wrapper  # type: ignore[return-value]


# ============================================================
# System — core.System
# ============================================================

def register_system_tools(server: MCPServer) -> None:
    """Register hardware discovery tools.

    Args:
        server: Server instance the tools are attached to.
    """

    @server.tool(
        name="system_scan",
        title="Scan system hardware",
        description=(
            "Collect the full machine profile: operating system, CPU model and "
            "instruction-set features, RAM capacity with physical module "
            "layout, NVIDIA GPUs with VRAM and CUDA version, and per-drive "
            "storage capacity. Shells out to PowerShell and nvidia-smi, so it "
            "takes a few seconds; prefer the narrower tools for a single metric."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def system_scan() -> dict:
        return scanner.scan_system()

    @server.tool(
        name="system_get_memory_info",
        title="Get RAM usage",
        description=(
            "Report total, available and used system RAM in bytes plus the "
            "usage percentage. Reads in-process counters, so it returns "
            "immediately."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def system_get_memory_info() -> dict:
        return hardware.get_memory_info()

    @server.tool(
        name="system_get_storage_info",
        title="Get storage capacity",
        description=(
            "List every mounted drive with its total, used and free bytes. "
            "Use it to check whether a model download will fit on disk."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def system_get_storage_info() -> list[dict]:
        return hardware.get_storage_info()

    @server.tool(
        name="system_get_gpu_info",
        title="Get NVIDIA GPU info",
        description=(
            "List NVIDIA GPUs with device name, driver version, total, used "
            "and free VRAM, and compute capability. Returns an empty list when "
            "no NVIDIA GPU or nvidia-smi is available."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def system_get_gpu_info() -> list[dict]:
        return hardware.get_nvidia_info()

    @server.tool(
        name="system_get_cuda_version",
        title="Get CUDA version",
        description=(
            "Return the CUDA version reported by the installed NVIDIA driver, "
            "or null when it cannot be determined."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def system_get_cuda_version() -> str | None:
        return hardware.get_cuda_version()


# ============================================================
# Ollama runtime — core.ollama.runtime
# ============================================================

def register_ollama_runtime_tools(server: MCPServer) -> None:
    """Register Ollama service lifecycle tools.

    The mutating tools return ``get_status()`` afterwards, since the underlying
    core functions return ``None``.

    Args:
        server: Server instance the tools are attached to.
    """

    @server.tool(
        name="ollama_get_status",
        title="Get Ollama status",
        description=(
            "Report whether the Ollama binary is installed, whether its local "
            "HTTP API is responding, and the installed version. Call this "
            "before any other Ollama tool."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def ollama_get_status() -> dict:
        return runtime.get_status()

    @server.tool(
        name="ollama_start",
        title="Start Ollama service",
        description=(
            "Start the Ollama background server and block until its API is "
            "ready. Does nothing when it is already running. Fails when Ollama "
            "is not installed or does not become ready within the timeout."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_start(timeout: float = runtime.START_TIMEOUT) -> dict:
        """Start the Ollama service.

        Args:
            timeout: Seconds to wait for the API to become ready.

        Returns:
            dict: Runtime status after the start attempt.
        """
        runtime.start(timeout=timeout)
        return runtime.get_status()

    @server.tool(
        name="ollama_stop",
        title="Stop Ollama service",
        description=(
            "Terminate the Ollama server process and block until its API stops "
            "responding. Any model currently loaded in memory is unloaded. "
            "Does nothing when Ollama is not installed or already stopped."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_stop(timeout: float = runtime.STOP_TIMEOUT) -> dict:
        """Stop the Ollama service.

        Args:
            timeout: Seconds to wait for shutdown.

        Returns:
            dict: Runtime status after the stop attempt.
        """
        runtime.stop(timeout=timeout)
        return runtime.get_status()

    @server.tool(
        name="ollama_install",
        title="Install Ollama",
        description=(
            "Run a local Ollama installer executable. Takes the path to an "
            "installer already present on disk; it does not download anything. "
            "The installer may prompt interactively."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_install(installer_path: str) -> dict:
        """Install Ollama from a local installer file.

        Args:
            installer_path: Path to the installer executable on disk.

        Returns:
            dict: Runtime status after installation.
        """
        runtime.install(installer_path=installer_path)
        return runtime.get_status()


# ============================================================
# Ollama models — core.ollama.model
# ============================================================

def register_ollama_model_tools(server: MCPServer) -> None:
    """Register Ollama model management tools.

    Args:
        server: Server instance the tools are attached to.
    """

    @server.tool(
        name="ollama_list_models",
        title="List installed models",
        description=(
            "List models installed locally, as the raw table printed by "
            "'ollama list' (name, ID, size, modified time)."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def ollama_list_models() -> str:
        return model.list_models()

    @server.tool(
        name="ollama_list_running_models",
        title="List running models",
        description=(
            "List models currently loaded in memory, as the raw table printed "
            "by 'ollama ps', including size and keep-alive expiry."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def ollama_list_running_models() -> str:
        return model.list_running_models()

    @server.tool(
        name="ollama_show_model_info",
        title="Show model details",
        description=(
            "Show architecture, parameter count, quantization, context length "
            "and the configured parameters for one installed model. Fails when "
            "the model is not installed."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def ollama_show_model_info(model_name: str) -> str:
        """Show details for an installed model.

        Args:
            model_name: Model name or tag, for example 'llama3' or 'llama3:8b'.

        Returns:
            str: Model metadata as reported by 'ollama show'.
        """
        return model.show_model_info(model=model_name)

    @server.tool(
        name="ollama_run_model",
        title="Run a prompt",
        description=(
            "Send one prompt to a model and return its generated text. "
            "Single-shot with no conversation history. For timing and "
            "tokens-per-second measurements use ollama_run_test instead."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_run_model(model_name: str, prompt: str) -> str:
        """Run a single prompt against a model.

        Args:
            model_name: Target model name or tag.
            prompt: Prompt text to send.

        Returns:
            str: Generated output text.
        """
        return model.run_model(model=model_name, prompt=prompt)

    @server.tool(
        name="ollama_load_model",
        title="Preload a model",
        description=(
            "Load a model into VRAM or system memory ahead of use and keep it "
            "resident for the keep-alive duration. Returns null when the model "
            "was already loaded. Requires the Ollama service to be running."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_load_model(
        model_name: str,
        keep_alive: str = "10m",
    ) -> dict | None:
        """Preload a model into memory.

        Args:
            model_name: Model name or tag to load.
            keep_alive: How long to keep it resident, for example '10m' or '1h'.

        Returns:
            dict | None: Ollama load response, or null if already loaded.
        """
        return model.load_model(model=model_name, keep_alive=keep_alive)

    @server.tool(
        name="ollama_stop_model",
        title="Unload a model",
        description=(
            "Unload a running model from memory, freeing its VRAM. The model "
            "stays installed on disk."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_stop_model(model_name: str) -> str:
        """Unload a running model.

        Args:
            model_name: Running model name to stop.

        Returns:
            str: Output from 'ollama stop'.
        """
        return model.stop_model(model=model_name)

    @server.tool(
        name="ollama_add_model",
        title="Import a local model file",
        description=(
            "Register a local model file (for example a .gguf) with Ollama "
            "under a new name. The file must already exist on disk; nothing is "
            "downloaded."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_add_model(model_name: str, model_path: str) -> str:
        """Import a local model file into Ollama.

        Args:
            model_name: Name to register the model under.
            model_path: Path to the model file on disk.

        Returns:
            str: Output from 'ollama create'.
        """
        return model.add_model(model_name=model_name, model_path=model_path)

    @server.tool(
        name="ollama_configure_model",
        title="Create a configured model variant",
        description=(
            "Create a new model from an existing one with Modelfile PARAMETER "
            "values baked in, for example temperature or num_ctx. The source "
            "model is left unchanged. Use this for a persistent variant; for a "
            "one-off parameter sweep use ollama_run_test, which applies "
            "settings per request without creating a model."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_configure_model(
        source_model: str,
        target_model: str,
        parameters: dict,
    ) -> str:
        """Create a configured variant of an existing model.

        Args:
            source_model: Existing model to base the variant on.
            target_model: Name for the new model.
            parameters: Modelfile PARAMETER key-values, for example
                {"temperature": 0.7, "num_ctx": 4096}.

        Returns:
            str: Output from 'ollama create'.
        """
        return model.configure_model(
            source_model=source_model,
            target_model=target_model,
            parameters=parameters,
        )

    @server.tool(
        name="ollama_remove_model",
        title="Delete a model",
        description=(
            "Permanently delete a model from local Ollama storage. This frees "
            "disk space and cannot be undone; the model must be downloaded or "
            "re-imported to get it back."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def ollama_remove_model(model_name: str) -> str:
        """Delete a model from local storage.

        Args:
            model_name: Model name to remove.

        Returns:
            str: Output from 'ollama rm'.
        """
        return model.remove_model(model=model_name)


# ============================================================
# Benchmarking — core.ollama.experiment
# ============================================================

def register_ollama_experiment_tools(server: MCPServer) -> None:
    """Register model benchmarking and comparison tools.

    Args:
        server: Server instance the tools are attached to.
    """
    benchmark = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )

    @server.tool(
        name="ollama_run_test",
        title="Benchmark one configuration",
        description=(
            "Run a set of prompts against one model with temporary generation "
            "parameters and measure performance: wall-clock duration, prompt "
            "and output token counts, and tokens per second per prompt, plus "
            "averages across the run. The parameters apply per request, so no "
            "model is created or modified. The model is preloaded before each "
            "prompt and that load time is excluded from the measurements. A "
            "prompt that fails is recorded with its error instead of aborting "
            "the run. Long-running: each prompt is a full generation."
        ),
        annotations=benchmark,
    )
    @surface_core_errors
    def ollama_run_test(
        model_name: str,
        prompts: list[str],
        config: dict | None = None,
        name: str = "test",
        include_output: bool = False,
    ) -> dict:
        """Benchmark a model under a single configuration.

        Args:
            model_name: Model name or tag to benchmark.
            prompts: Prompts to run, in order.
            config: Optional generation options, for example
                {"temperature": 0.7, "num_ctx": 4096}.
            name: Label recorded with the results.
            include_output: Whether to include generated text alongside metrics.

        Returns:
            dict: Per-prompt results and a summary of averaged metrics.
        """
        return experiment.run_test(
            model=model_name,
            prompts=prompts,
            config=config,
            name=name,
            include_output=include_output,
        )

    @server.tool(
        name="ollama_compare_tests",
        title="Compare configurations",
        description=(
            "Run the same prompts against one model under several parameter "
            "configurations and return the benchmark results side by side, so "
            "settings can be compared on speed and token throughput. Each "
            "configuration is a dict with a 'name' and an 'options' dict of "
            "generation parameters. Long-running: total time is the prompt "
            "count multiplied by the configuration count."
        ),
        annotations=benchmark,
    )
    @surface_core_errors
    def ollama_compare_tests(
        model_name: str,
        prompts: list[str],
        configurations: list[dict],
        include_output: bool = False,
    ) -> dict:
        """Compare a model across multiple configurations.

        Args:
            model_name: Model name or tag to benchmark.
            prompts: Prompts run against every configuration.
            configurations: Configurations to compare, each shaped as
                {"name": "warm", "options": {"temperature": 0.9}}.
            include_output: Whether to include generated text alongside metrics.

        Returns:
            dict: One benchmark result per configuration.
        """
        return experiment.compare_tests(
            model=model_name,
            prompts=prompts,
            configurations=configurations,
            include_output=include_output,
        )


# ============================================================
# Python — core.python
# ============================================================

def register_python_tools(server: MCPServer) -> None:
    """Register Python interpreter, environment, package, and script tools.

    Args:
        server: Server instance the tools are attached to.
    """

    @server.tool(
        name="python_get_status",
        title="List installed Python versions",
        description=(
            "List Python installations detected on the machine, each with its "
            "version and interpreter path. On Windows this reads the registry; "
            "the interpreter running this server is always included."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def python_get_status() -> list[dict]:
        return installer.get_python_status()

    @server.tool(
        name="python_get_python_path",
        title="Resolve interpreter path",
        description=(
            "Return the absolute path to a Python interpreter. With no "
            "environment given, returns the interpreter running this server; "
            "otherwise returns the interpreter inside that virtual "
            "environment and fails if it is missing."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def python_get_python_path(env_path: str | None = None) -> str:
        """Resolve an interpreter path.

        Args:
            env_path: Optional virtual environment directory.

        Returns:
            str: Absolute path to the interpreter executable.
        """
        return environment.get_python_path(environment=env_path)

    @server.tool(
        name="python_create_environment",
        title="Create a virtual environment",
        description=(
            "Create a new venv at the given path using the interpreter running "
            "this server. Fails if the path already exists, so it will never "
            "overwrite an existing environment."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def python_create_environment(env_path: str) -> str:
        """Create a virtual environment.

        Args:
            env_path: Directory to create the environment in.

        Returns:
            str: Absolute path to the created environment.
        """
        return environment.create_environment(path=env_path)

    @server.tool(
        name="python_remove_environment",
        title="Delete a virtual environment",
        description=(
            "Recursively delete a virtual environment directory and everything "
            "inside it, including installed packages. Irreversible: the path is "
            "removed without a backup, so confirm it is the intended "
            "environment first."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def python_remove_environment(env_path: str) -> str:
        """Delete a virtual environment.

        Args:
            env_path: Environment directory to remove.

        Returns:
            str: Confirmation message naming the removed path.
        """
        environment.remove_environment(path=env_path)
        return f"Environment removed: {env_path}"

    @server.tool(
        name="python_list_packages",
        title="List installed packages",
        description=(
            "List installed packages and versions via 'pip list'. Targets the "
            "given virtual environment, or the interpreter running this server "
            "when none is given."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def python_list_packages(env_path: str | None = None) -> str:
        """List installed packages.

        Args:
            env_path: Optional virtual environment to inspect.

        Returns:
            str: Output from 'pip list'.
        """
        return tools.list_packages(environment=env_path)

    @server.tool(
        name="python_install_packages",
        title="Install packages",
        description=(
            "Install one or more packages with pip into the given virtual "
            "environment, or into the interpreter running this server when "
            "none is given. Accepts version specifiers such as 'numpy==1.26.4'. "
            "Downloads from the configured package index."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    @surface_core_errors
    def python_install_packages(
        packages: list[str],
        env_path: str | None = None,
    ) -> str:
        """Install packages with pip.

        Args:
            packages: Package names or version specifiers.
            env_path: Optional target virtual environment.

        Returns:
            str: Output from 'pip install'.
        """
        return tools.install_packages(
            packages=packages,
            environment=env_path,
        )

    @server.tool(
        name="python_uninstall_packages",
        title="Uninstall packages",
        description=(
            "Uninstall one or more packages with pip, without prompting for "
            "confirmation. Targets the given virtual environment, or the "
            "interpreter running this server when none is given — note that "
            "removing a package from the server's own interpreter can break it."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def python_uninstall_packages(
        packages: list[str],
        env_path: str | None = None,
    ) -> str:
        """Uninstall packages with pip.

        Args:
            packages: Package names to remove.
            env_path: Optional target virtual environment.

        Returns:
            str: Output from 'pip uninstall'.
        """
        return tools.uninstall_packages(
            packages=packages,
            environment=env_path,
        )

    @server.tool(
        name="python_create_script",
        title="Create a script file",
        description=(
            "Write a new Python script to disk, creating parent directories as "
            "needed. Fails if the file already exists, so it will never "
            "overwrite; use python_edit_script to replace existing content."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def python_create_script(path: str, content: str) -> str:
        """Create a Python script file.

        Args:
            path: Destination file path.
            content: Script source text.

        Returns:
            str: Absolute path to the created script.
        """
        return tools.create_script(path=path, content=content)

    @server.tool(
        name="python_edit_script",
        title="Overwrite a script file",
        description=(
            "Replace the entire contents of an existing Python script. This is "
            "a full overwrite, not a patch: the previous content is lost. "
            "Fails if the file does not exist."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def python_edit_script(path: str, content: str) -> str:
        """Overwrite a Python script file.

        Args:
            path: Script file to rewrite.
            content: New script source text.

        Returns:
            str: Absolute path to the updated script.
        """
        return tools.edit_script(path=path, content=content)

    @server.tool(
        name="python_delete_script",
        title="Delete a script file",
        description=(
            "Delete a Python script file from disk. Irreversible: the file is "
            "unlinked rather than moved to a recycle bin."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def python_delete_script(path: str) -> str:
        """Delete a Python script file.

        Args:
            path: Script file to remove.

        Returns:
            str: Absolute path of the deleted script.
        """
        return tools.delete_script(path=path)

    @server.tool(
        name="python_run_script",
        title="Run a script",
        description=(
            "Execute a Python script and return its standard output, using the "
            "given virtual environment or the interpreter running this server. "
            "Blocks until the script exits and fails with the script's stderr "
            "on a non-zero exit code. The script runs with this server's "
            "privileges and can perform any action Python allows."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    @surface_core_errors
    def python_run_script(
        path: str,
        env_path: str | None = None,
    ) -> str:
        """Execute a Python script.

        Args:
            path: Script file to run.
            env_path: Optional virtual environment to run it with.

        Returns:
            str: Standard output from the script.
        """
        return tools.run_script(path=path, environment=env_path)

    @server.tool(
        name="python_install_python",
        title="Install Python from an installer",
        description=(
            "Run a local Windows Python installer in quiet mode with PATH "
            "prepending enabled. The installer must already be on disk; "
            "nothing is downloaded. Modifies system state outside this project "
            "and may require elevation when installing for all users. Returns "
            "the detected Python installations afterwards."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def python_install_python(
        installer_path: str,
        all_users: bool = False,
    ) -> list[dict]:
        """Install Python from a local installer.

        Args:
            installer_path: Path to the installer executable.
            all_users: Whether to install for all users instead of the current one.

        Returns:
            list[dict]: Detected Python versions and paths after installation.
        """
        return installer.install_python(
            installer_path=installer_path,
            all_users=all_users,
        )


# ============================================================
# Downloads — core.download_manager
# ============================================================

# How long a cancellation waits for the download worker to stop before the tool
# reports back. The worker exits at a chunk boundary, so this only has to cover
# one chunk read plus core's own cleanup.
CANCEL_WAIT_SECONDS = 60.0

# DownloadManager is stateful: a queue is built up, started, then controlled and
# polled while a background thread downloads. MCP tool calls are individually
# stateless, so named manager instances are kept here and each tool acts on one
# by session_id. This registry is the only state this layer adds; queueing,
# retrying, resuming, and progress tracking all stay in core.
#
# A cancelled session is dropped from the registry as part of the cancellation,
# not left behind in a cancelled state: its queue and files are gone, so keeping
# it would let a later download_add append to a queue that still held the
# original URLs and start the same transfer twice.
_sessions: dict[str, DownloadManager] = {}
_sessions_lock = threading.Lock()


def _get_session(session_id: str) -> DownloadManager:
    """Look up a live download session.

    Args:
        session_id: Identifier passed to ``download_create_session``.

    Returns:
        DownloadManager: The registered manager instance.

    Raises:
        ToolError: If no session is registered under that identifier, or if the
            one registered has been cancelled and is only awaiting removal.
    """
    with _sessions_lock:
        manager = _sessions.get(session_id)
        known = ", ".join(sorted(_sessions)) or "none"

    if manager is None:
        raise ToolError(
            f"Unknown download session: '{session_id}' (open sessions: {known})"
        )

    if manager.get_status()["closed"]:
        # Reached only if a cancellation's own removal has not run yet — a
        # cancelled session is otherwise gone from the registry entirely.
        _discard_session(session_id)
        raise ToolError(
            f"Download session '{session_id}' was cancelled and cannot be "
            f"reused. Create it again with download_create_session."
        )

    return manager


def _discard_session(session_id: str) -> dict | None:
    """Remove a session from the registry and release what it still holds.

    Called when a session is cancelled or closed, from whichever path noticed:
    the ``download_cancel`` tool, the progress panel's Cancel button, or
    ``download_close_session``. Removing the entry is what makes the identifier
    reusable, and ``purge`` drops the queue and the worker references the
    manager was still holding.

    Args:
        session_id: Session to forget.

    Returns:
        dict | None: The session's final status, or None when it was already
        gone.
    """
    with _sessions_lock:
        manager = _sessions.pop(session_id, None)

    if manager is None:
        return None

    status = manager.get_status()
    manager.purge()

    return status


def register_download_tools(server: MCPServer) -> None:
    """Register download queue tools.

    Args:
        server: Server instance the tools are attached to.
    """

    @server.tool(
        name="download_list_allowed_domains",
        title="List allowed download domains",
        description=(
            "List the domains downloads may come from. Core rejects any other "
            "host, so check this before queueing a URL."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def download_list_allowed_domains() -> list[str]:
        return sorted(ALLOWED_DOMAINS)

    @server.tool(
        name="download_create_session",
        title="Create a download session",
        description=(
            "Create a named download queue writing into the given directory, "
            "creating it if needed. The session id is used by every other "
            "download tool. Fails if the id is already in use."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def download_create_session(
        session_id: str,
        download_directory: str = "data/downloads",
        max_retries: int = 3,
    ) -> dict:
        """Create a download session.

        Args:
            session_id: Identifier for later calls.
            download_directory: Destination directory for completed files.
            max_retries: Retry attempts per file before it is marked failed.

        Returns:
            dict: Initial session status.
        """
        with _sessions_lock:
            if session_id in _sessions:
                raise ToolError(
                    f"Download session already exists: '{session_id}'"
                )

            manager = DownloadManager(
                download_directory=download_directory,
                max_retries=max_retries,
            )
            _sessions[session_id] = manager

        return manager.get_status()

    @server.tool(
        name="download_list_sessions",
        title="List download sessions",
        description=(
            "List open download sessions with each one's current status, "
            "including whether it is running and how many files are queued."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def download_list_sessions() -> dict:
        with _sessions_lock:
            items = list(_sessions.items())

        return {
            session_id: manager.get_status()
            for session_id, manager in items
        }

    @server.tool(
        name="download_add",
        title="Queue a file",
        description=(
            "Append a URL to a session's queue. The filename is taken from the "
            "URL unless one is given. The host must be in "
            "download_list_allowed_domains. Queue every file before calling "
            "download_start."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def download_add(
        session_id: str,
        url: str,
        filename: str | None = None,
    ) -> dict:
        """Queue one file for download.

        Args:
            session_id: Target session.
            url: HTTP or HTTPS URL on an allowed domain.
            filename: Optional destination filename.

        Returns:
            dict: Session status after queueing.
        """
        manager = _get_session(session_id)
        manager.add(url=url, filename=filename)
        return manager.get_status()

    @server.tool(
        name="download_add_many",
        title="Queue several files",
        description=(
            "Append multiple URLs to a session's queue in order, each named "
            "after its URL. Rejects the batch as soon as one host is not "
            "allowed, so URLs queued before that point remain queued."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def download_add_many(session_id: str, urls: list[str]) -> dict:
        """Queue several files for download.

        Args:
            session_id: Target session.
            urls: URLs on allowed domains.

        Returns:
            dict: Session status after queueing.
        """
        manager = _get_session(session_id)
        manager.add_many(urls=urls)
        return manager.get_status()

    @server.tool(
        name="download_start",
        title="Start downloading",
        description=(
            "Start processing a session's queue on a background thread and "
            "return immediately. Files download one at a time with automatic "
            "retry and resume. Poll download_get_status for progress. Fails if "
            "the queue is empty; does nothing if already running."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    @surface_core_errors
    def download_start(session_id: str) -> dict:
        """Start a session's download queue.

        Args:
            session_id: Target session.

        Returns:
            dict: Session status just after starting.
        """
        manager = _get_session(session_id)
        manager.start()
        return manager.get_status()

    @server.tool(
        name="download_get_status",
        title="Get download progress",
        description=(
            "Report a session's running, paused and cancelled state, which file "
            "is active, and per-file status with bytes downloaded, total size, "
            "and any error. Poll this to follow progress."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def download_get_status(session_id: str) -> dict:
        """Get a session's current status.

        Args:
            session_id: Target session.

        Returns:
            dict: Manager state and per-file progress.
        """
        return _get_session(session_id).get_status()

    @server.tool(
        name="download_pause",
        title="Pause downloading",
        description=(
            "Pause the active download, keeping the partial file so it can "
            "resume from where it stopped. Does nothing when the session is "
            "not running or is already paused."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def download_pause(session_id: str) -> dict:
        """Pause a session's active download.

        Args:
            session_id: Target session.

        Returns:
            dict: Session status after pausing.
        """
        manager = _get_session(session_id)
        manager.pause()
        return manager.get_status()

    @server.tool(
        name="download_resume",
        title="Resume downloading",
        description=(
            "Resume a paused session, continuing the active file from its "
            "partial data via an HTTP range request. Does nothing when the "
            "session is not paused."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    @surface_core_errors
    def download_resume(session_id: str) -> dict:
        """Resume a paused session.

        Args:
            session_id: Target session.

        Returns:
            dict: Session status after resuming.
        """
        manager = _get_session(session_id)
        manager.resume()
        return manager.get_status()

    @server.tool(
        name="download_skip",
        title="Skip the current file",
        description=(
            "Abandon the file being downloaded, mark it skipped, and move on to "
            "the next one in the queue. Its partial data is left on disk."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def download_skip(session_id: str) -> dict:
        """Skip a session's current file.

        Args:
            session_id: Target session.

        Returns:
            dict: Session status after requesting the skip.
        """
        manager = _get_session(session_id)
        manager.skip()
        return manager.get_status()

    @server.tool(
        name="download_cancel",
        title="Cancel all downloads",
        description=(
            "Stop the active download, abandon the rest of the queue, and delete "
            "the files this session produced — both partial data and files that "
            "had already completed, since the queue is one unit of work that did "
            "not finish. Files that existed before the session started are kept. "
            "The stop is recorded in the execution log, and the session is then "
            "removed: its id becomes free, and downloading the same files again "
            "means creating the session again. Cancelling a queue that already "
            "finished deletes nothing. Pass keep_files to abandon the queue "
            "without deleting anything."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def download_cancel(
        session_id: str,
        keep_files: bool = False,
    ) -> dict:
        """Cancel a session's remaining downloads and remove the session.

        Args:
            session_id: Target session.
            keep_files: Keep what the session downloaded instead of deleting it.

        Returns:
            dict: Session status after the cancellation and cleanup.
        """
        manager = _get_session(session_id)
        # Core is told first, because its first cancellation is the one that
        # decides whether the files go: notifying the panel's job first would
        # cancel through the job's own canceller, which always deletes.
        manager.cancel(cleanup=not keep_files)
        # A progress bar over this session would otherwise keep offering Cancel
        # and Stop for a queue that is being torn down.
        note_download_ended(
            session_id,
            reason=(
                "Cancelled with download_cancel; downloaded files kept."
                if keep_files
                else "Cancelled with download_cancel; downloaded files removed."
            ),
        )
        # The worker stops at a chunk boundary, so the status is only final once
        # it has actually exited and core's cleanup has run.
        manager.wait_until_stopped(timeout=CANCEL_WAIT_SECONDS)

        status = _discard_session(session_id) or manager.get_status()

        return status

    @server.tool(
        name="download_close_session",
        title="Close a download session",
        description=(
            "Drop a session from the registry, stopping it first if it is still "
            "running. Downloaded files are left on disk; only the in-memory queue "
            "and its progress history are discarded, and the session id becomes "
            "free again. Use download_cancel to stop a session and delete what it "
            "produced."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    @surface_core_errors
    def download_close_session(session_id: str) -> dict:
        """Close and forget a download session.

        Args:
            session_id: Target session.

        Returns:
            dict: Final status of the closed session.
        """
        manager = _get_session(session_id)
        # Closing a session is bookkeeping, not a request to undo the download,
        # so the files it produced are left alone. Core is told before the panel
        # for the same reason as in download_cancel: the job's own canceller
        # deletes, and the first cancellation is the one that decides.
        manager.close()
        note_download_ended(
            session_id,
            reason="Session closed; downloaded files kept.",
        )
        manager.wait_until_stopped(timeout=CANCEL_WAIT_SECONDS)

        return _discard_session(session_id) or manager.get_status()


# ============================================================
# Logging — core.logging
# ============================================================

# core.logging.write_log is deliberately not exposed: core writes its own
# entries, and letting a client inject records would pollute the history.

def register_logging_tools(server: MCPServer) -> None:
    """Register execution log tools.

    Args:
        server: Server instance the tools are attached to.
    """

    @server.tool(
        name="logs_read",
        title="Read the execution log",
        description=(
            "Read entries from the execution log that core writes for every "
            "significant operation. Each entry carries a timestamp, severity, "
            "component, action, message, and a details object. The filters are "
            "exact-match and combine, so level='ERROR' with "
            "component='download_manager' returns only failed downloads. "
            "Returns an empty list when the log file does not exist yet."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def logs_read(
        level: str | None = None,
        component: str | None = None,
        action: str | None = None,
    ) -> list[dict]:
        """Read filtered execution log entries.

        Args:
            level: Optional severity, for example 'INFO', 'WARNING', 'ERROR'.
            component: Optional component, for example 'ollama/runtime',
                'ollama/model', 'system/scanner', 'python', 'download_manager'.
            action: Optional action, for example 'start', 'run', 'download_failed'.

        Returns:
            list[dict]: Matching log entries in file order.
        """
        return core_logging.read_logs(
            level=level,
            component=component,
            action=action,
        )

    @server.tool(
        name="logs_get_path",
        title="Get the execution log path",
        description=(
            "Return the absolute path of the execution log file, creating its "
            "data directory if needed. Useful for reading or archiving the raw "
            "log outside these tools."
        ),
        annotations=READ_ONLY,
    )
    @surface_core_errors
    def logs_get_path() -> str:
        return str(core_logging.get_execution_log_path())


# ============================================================
# Server
# ============================================================

REGISTRARS = (
    register_system_tools,
    register_ollama_runtime_tools,
    register_ollama_model_tools,
    register_ollama_experiment_tools,
    register_python_tools,
    register_download_tools,
    register_logging_tools,
)


def create_server() -> MCPServer:
    """Build the MCP server with every tool registered.

    Returns:
        MCPServer: Configured server instance, ready to run.
    """
    server = MCPServer(
        name=SERVER_NAME,
        title=SERVER_TITLE,
        instructions=INSTRUCTIONS,
        version=SERVER_VERSION,
        # The progress panel is an additive MCP Apps extension: it contributes the
        # ui:// resource and the tools bound to it, and intercepts nothing. The
        # session resolver and remover are passed in so the download registry
        # above stays the single place sessions live — including when the panel's
        # Cancel button is what ended one.
        extensions=[
            create_progress_app(
                get_session=_get_session,
                release_session=_discard_session,
            )
        ],
    )

    for register in REGISTRARS:
        register(server)

    return server


def main() -> None:
    """Build the server and serve it over stdio."""
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
