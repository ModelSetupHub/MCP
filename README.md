# ModelSetupHub MCP Server

MCP (Model Context Protocol) layer over the `core` package in the `Core`
submodule. Every tool is a thin pass-through to an existing core function — this
layer contains no business logic, and the submodule is treated as read-only.

## Layout

```text
<project root>/
├── main.py            # the entire MCP layer
├── requirements.txt
└── Core/              # submodule
    └── core/          # backend logic
```

`main.py` holds everything: `sys.path` wiring for the submodule, the error
forwarder, one `register_*_tools` function per core module, and the
`create_server` / `main` entry point. Sections are separated by banner comments
matching the core module they wrap.

Before importing `core`, `main.py` puts the `Core` submodule root on `sys.path` —
the directory *containing* `core`, not `core` itself — because core imports
itself as a top-level package (`from core.logging import write_log`). If the
submodule is not checked out, it raises at startup with the command needed to
fetch it, rather than failing later on an opaque `ModuleNotFoundError`.

## Install

```bash
git submodule update --init
pip install -r requirements.txt
```

Requires Python 3.10+. `psutil` is a core dependency, listed here because core
ships no requirements file of its own.

## Run

```bash
python main.py
```

The server speaks JSON-RPC over stdio. Client configuration:

```json
{
  "mcpServers": {
    "modelsetuphub": {
      "command": "python",
      "args": ["C:/path/to/main.py"]
    }
  }
}
```

## Tools

46 tools across seven groups. Names are prefixed by area, and each carries MCP
annotations so a client can tell a read-only call from a destructive one.

### system — `core.system`

| Tool | Core function |
| --- | --- |
| `system_scan` | `scanner.scan_system` |
| `system_get_memory_info` | `hardware.get_memory_info` |
| `system_get_storage_info` | `hardware.get_storage_info` |
| `system_get_gpu_info` | `hardware.get_nvidia_info` |
| `system_get_cuda_version` | `hardware.get_cuda_version` |

### Ollama runtime — `core.ollama.runtime`

| Tool | Core function |
| --- | --- |
| `ollama_get_status` | `get_status` |
| `ollama_start` | `start` |
| `ollama_stop` | `stop` |
| `ollama_install` | `install` |

The mutating three return `get_status()` afterwards, since the core functions
return `None`.

### Ollama models — `core.ollama.model`

| Tool | Core function |
| --- | --- |
| `ollama_list_models` | `list_models` |
| `ollama_list_running_models` | `list_running_models` |
| `ollama_show_model_info` | `show_model_info` |
| `ollama_run_model` | `run_model` |
| `ollama_load_model` | `load_model` |
| `ollama_stop_model` | `stop_model` |
| `ollama_add_model` | `add_model` |
| `ollama_configure_model` | `configure_model` |
| `ollama_remove_model` | `remove_model` |

### Benchmarking — `core.ollama.experiment`

| Tool | Core function |
| --- | --- |
| `ollama_run_test` | `run_test` |
| `ollama_compare_tests` | `compare_tests` |

### Python — `core.python`

| Tool | Core function |
| --- | --- |
| `python_get_status` | `installer.get_python_status` |
| `python_get_python_path` | `environment.get_python_path` |
| `python_create_environment` | `environment.create_environment` |
| `python_remove_environment` | `environment.remove_environment` |
| `python_list_packages` | `tools.list_packages` |
| `python_install_packages` | `tools.install_packages` |
| `python_uninstall_packages` | `tools.uninstall_packages` |
| `python_create_script` | `tools.create_script` |
| `python_edit_script` | `tools.edit_script` |
| `python_delete_script` | `tools.delete_script` |
| `python_run_script` | `tools.run_script` |
| `python_install_python` | `installer.install_python` |

### Downloads — `core.download_manager`

`DownloadManager` is stateful — build a queue, start it, then poll while a
background thread works — but MCP calls are individually stateless. So `main.py`
keeps named manager instances in a registry, and each tool acts on one by
`session_id`. That registry is the only state this layer adds; queueing, retry,
resume, and progress tracking all stay in core.

| Tool | Core member |
| --- | --- |
| `download_list_allowed_domains` | `manager.ALLOWED_DOMAINS` |
| `download_create_session` | `DownloadManager(...)` |
| `download_list_sessions` | `DownloadManager.get_status` (all sessions) |
| `download_add` | `DownloadManager.add` |
| `download_add_many` | `DownloadManager.add_many` |
| `download_start` | `DownloadManager.start` |
| `download_get_status` | `DownloadManager.get_status` |
| `download_pause` | `DownloadManager.pause` |
| `download_resume` | `DownloadManager.resume` |
| `download_skip` | `DownloadManager.skip` |
| `download_cancel` | `DownloadManager.cancel` |
| `download_close_session` | `DownloadManager.cancel` + registry removal |

`download_start` returns immediately; poll `download_get_status` for progress.
`DownloadManager.wait()` is not exposed, since blocking a tool call for the
length of a multi-gigabyte download would stall the client.

Core rejects any host outside its whitelist, so check
`download_list_allowed_domains` before queueing a URL.

### Logging — `core.logging`

| Tool | Core function |
| --- | --- |
| `logs_read` | `read_logs` |
| `logs_get_path` | `get_execution_log_path` |

`write_log` is not exposed: core writes its own entries, and letting a client
inject arbitrary records would pollute the execution history.

## Error handling

Argument validation is handled by the SDK from each tool's type annotations, so
a missing or mistyped argument fails before core is reached.

Beyond that, the SDK treats any exception other than `ToolError` as a crash: the
client gets a generic `Error executing tool <name>` and the real message stays on
the server. Since core raises descriptive exceptions, the `surface_core_errors`
decorator re-raises them with the type name and message intact and the original
chained as `__cause__`:

```text
ToolError: Error executing tool download_add:
    PermissionError: Access denied: domain 'evil.example.com' is not allowed.
```

Core exceptions are forwarded, never caught and replaced.
