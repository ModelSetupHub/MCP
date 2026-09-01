# ModelSetupHub MCP Server

MCP (Model Context Protocol) layer over the `MSHCore` package, provided by the
`Core` submodule. Nearly every tool is a thin pass-through to an existing
MSHCore function; the only logic this layer adds is the in-chat progress panel
and the job bookkeeping behind it.

## Layout

```text
<project root>/
├── main.py            # the MCP tool layer
├── gui/               # the in-chat progress panel
│   ├── __init__.py
│   ├── app.py         # MCP Apps extension: ui:// resource + bound tools
│   ├── jobs.py        # job model and the registry of jobs running now
│   ├── store.py       # persisted snapshots — the source of truth
│   ├── workers.py     # threads that run the operations and update their jobs
│   ├── logtail.py     # incremental reader for MSHCore's execution log
│   ├── loader.py      # inlines the assets into one HTML document
│   └── assets/
│       ├── progress.html
│       ├── progress.css
│       └── progress.js
├── requirements.txt
├── utils/             # helper utilities
│   ├── claude_setup.py
│   ├── install_mshcore.py
│   └── icon.ico
└── Core/              # submodule providing the MSHCore package
```

`main.py` holds the MCP layer: the MSHCore availability check, the error
forwarder, one `register_*_tools` function per MSHCore module, and the
`create_server` / `main` entry point. Sections are separated by banner comments
matching the MSHCore module they wrap.

`main.py` imports `MSHCore` as an installed package. If pip cannot find it, the
server raises at startup with the command needed to install it, rather than
failing later on an opaque `ModuleNotFoundError`.

Everything imports `MSHCore` the same way — as the top-level installed package.
Importing the same files through two spellings (say, `MSHCore.x` in one place
and `Core.MSHCore.x` in another) would produce two separate module objects,
each with its own copy of every class — so a `CancellationToken` handed to
MSHCore and the `OperationCancelled` caught back from it would belong to
different classes, and `except OperationCancelled` would silently miss it.

## Install

MSHCore must be installed with pip before the server can start. Easiest is the
bundled utility, which installs it from the `Core` submodule into the current
interpreter:

```bash
git submodule update --init
python utils/install_mshcore.py
```

Or install it directly:

```bash
pip install ./Core
```

Then install the server's own dependencies:

```bash
pip install -r requirements.txt
```

Requires Python 3.10+. `psutil` is an MSHCore dependency, listed here because
MSHCore declares no dependencies of its own.

`python utils/install_mshcore.py --check` reports whether MSHCore is already
installed, `--editable` performs a development install, and `--force` reinstalls
an existing one — useful after pulling new submodule commits.

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

59 tools. 49 are thin pass-throughs to MSHCore, grouped by area below; the other
10 belong to the progress panel and are documented in
[Progress panel](#progress-panel). Names are prefixed by area, and each carries
MCP annotations so a client can tell a read-only call from a destructive one.

### system — `MSHCore.system`

| Tool | MSHCore function |
| --- | --- |
| `system_scan` | `scanner.scan_system` |
| `system_get_memory_info` | `hardware.get_memory_info` |
| `system_get_storage_info` | `hardware.get_storage_info` |
| `system_get_gpu_info` | `hardware.get_nvidia_info` |
| `system_get_cuda_version` | `hardware.get_cuda_version` |

### Ollama runtime — `MSHCore.ollama.runtime`

| Tool | MSHCore function |
| --- | --- |
| `ollama_get_status` | `get_status` |
| `list_ollama_logs` | `list_ollama_logs` |
| `read_ollama_logs` | `read_ollama_logs` |
| `ollama_start` | `start` |
| `ollama_stop` | `stop` |
| `ollama_install` | `install` |

The mutating three return `get_status()` afterwards, since the MSHCore functions
return `None`.

The two log tools read Ollama's own log files — the live `app.log` and
`server.log` plus every rotated copy — and are unrelated to `logs_read`, which
reads this project's execution log. They work as a pair: `list_ollama_logs`
returns one entry per file with its name, path, size in bytes, line count and
modification time, newest first, and `read_ollama_logs` takes one of those names
and returns that file. Splitting it that way keeps the caller from pulling
megabytes it did not ask for, and the size and line count are what make the
choice informed — `server.log` alone routinely passes a megabyte, and it is the
one file that says why a service start or model load failed. `read_ollama_logs`
accepts a bare file name only, so a path cannot reach outside the log
directories, and it returns the whole file unless `start_line` and `end_line`
(1-based and inclusive, defaulting to the first and last line) select a window
instead. The response reports `total_lines` for the whole file alongside the
`start_line` and `end_line` actually returned — both `null` when the range
matched nothing, which is what a range starting past the end of the file looks
like. Because the line count `list_ollama_logs` reports is counted the same way,
it can be passed straight back as `end_line`.

### Ollama models — `MSHCore.ollama.model`

| Tool | MSHCore function |
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

### Benchmarking — `MSHCore.ollama.experiment`

| Tool | MSHCore function |
| --- | --- |
| `ollama_run_test` | `run_test` |
| `ollama_compare_tests` | `compare_tests` |

### Python — `MSHCore.python`

| Tool | MSHCore function |
| --- | --- |
| `python_get_status` | `installer.get_python_status` |
| `python_get_python_path` | `environment.get_python_path` |
| `python_create_environment` | `environment.create_environment` |
| `python_remove_environment` | `environment.remove_environment` |
| `python_list_packages` | `tools.list_packages` |
| `python_install_packages` | `tools.install_packages` |
| `python_uninstall_packages` | `tools.uninstall_packages` |
| `python_create_script` | `tools.create_script` |
| `python_read_script` | `tools.read_script` |
| `python_edit_script` | `tools.edit_script` |
| `python_delete_script` | `tools.delete_script` |
| `python_run_script` | `tools.run_script` |
| `python_install_python` | `installer.install_python` |

`python_edit_script` is a full overwrite, so `python_read_script` is how the
current content is retrieved before it is replaced.

### Downloads — `MSHCore.download_manager`

`DownloadManager` is stateful — build a queue, start it, then poll while a
background thread works — but MCP calls are individually stateless. So `main.py`
keeps named manager instances in a registry, and each tool acts on one by
`session_id`. That registry is the only state this layer adds; queueing, retry,
resume, and progress tracking all stay in MSHCore.

| Tool | MSHCore member |
| --- | --- |
| `download_list_allowed_domains` | `sources.ALLOWED_DOMAINS` |
| `download_create_session` | `DownloadManager(...)` |
| `download_list_sessions` | `DownloadManager.get_status` (all sessions) |
| `download_add` | `DownloadManager.add` |
| `download_add_many` | `DownloadManager.add_many` |
| `download_start` | `DownloadManager.start` |
| `download_get_status` | `DownloadManager.get_status` |
| `download_pause` | `DownloadManager.pause` |
| `download_resume` | `DownloadManager.resume` |
| `download_skip` | `DownloadManager.skip` |
| `download_cancel` | `DownloadManager.cancel` + registry removal |
| `download_close_session` | `DownloadManager.close` + registry removal |

`download_start` returns immediately; poll `download_get_status` for progress.
`DownloadManager.wait()` is not exposed, since blocking a tool call for the
length of a multi-gigabyte download would stall the client.

`download_cancel` stops the queue and deletes what the session produced — see
[Cancelling and stopping](#cancelling-and-stopping). `download_close_session` is
bookkeeping and keeps the files.

Both of them end the session for good, and both remove it from the registry. A
cancelled `DownloadManager` refuses `add` and `start` with `SessionCancelled`,
because its queue and its files are gone and there is nothing left to continue
from. Downloading the same files again therefore means creating the session again,
under the same id if you like — which is what stops a cancelled queue from being
re-run alongside a fresh one and downloading everything twice.

Restarting a session that is still open only picks up what has not run:
`start` leaves completed, skipped and cancelled items alone and retries failed
ones, so no file is fetched twice. It raises `RuntimeError` when every item has
already run.

MSHCore rejects any host outside its whitelist, so check
`download_list_allowed_domains` before queueing a URL. The list itself lives in
`MSHCore/download_manager/sources.py`, which both validation points read — the
manager when a file is queued and the downloader when the transfer starts — so
the tool reports it from there rather than through either of them.

`download_get_status` also reports each item's current transfer speed in bytes
per second, which MSHCore measures per file.

### Logging — `MSHCore.logging`

| Tool | MSHCore function |
| --- | --- |
| `logs_read` | `read_logs` |
| `logs_get_file_info` | `get_log_file_info` |

`logs_read` takes an optional `line_count` alongside the `level`, `component`
and `action` filters. The filters decide which entries match; `line_count` caps
how many of those come back, keeping the newest — the log is appended
chronologically — while still returning them oldest first. `logs_get_file_info`
reports the log's path, entry count and size without reading it, which is how
the size of an uncapped read is known in advance.

`write_log` is not exposed: MSHCore writes its own entries, and letting a client
inject arbitrary records would pollute the execution history.

## Progress panel

Downloads and benchmarks have a `_with_progress` variant that renders a live
progress bar in the conversation itself, next to the assistant's message, with a
red **Cancel** button in its top-right corner and, for downloads, a **Stop**
button beside it. Everything for it lives in `gui/`; `main.py` imports
`create_progress_app` and passes it to `MCPServer(extensions=[...])`, and calls
`register_progress_tools` alongside its own registrars.

Installations and `system_scan` have no progress variant. They have nothing
measurable to report, so the bar was not carrying its weight there.

### How it reaches the chat

The panel is an [MCP Apps](https://modelcontextprotocol.io) extension
(`io.modelcontextprotocol/ui`), which the SDK ships as `mcp.server.apps`. A tool
advertises `_meta.ui.resourceUri` pointing at a `ui://` HTML resource, and the
client renders that resource in a sandboxed iframe inline in the conversation.
The extension is purely additive: it contributes one resource and six tools, and
intercepts nothing.

An app resource is a single self-contained HTML document — the iframe has no
origin to fetch siblings from — so `gui/loader.py` inlines `progress.css` and
`progress.js` into `progress.html` at import time. The three files stay separate
on disk, each individually valid, with comment placeholders (`/* INJECT:CSS */`)
marking the injection points. Inlining escapes `</` so a literal `</script>` in
the JavaScript cannot end the element early, and a missing placeholder or asset
raises at startup rather than rendering a blank panel later.

The panel defines its own palette rather than adopting the host's style
variables. Those describe a host's own chrome, and a colour meant for a label on
the host's background does not necessarily stay readable on the panel's surfaces;
every text colour here is checked against the surface it actually sits on and
clears the WCAG AA 4.5:1 threshold in both themes. Fonts and corner radii still
follow the host, since those cannot hurt legibility.

### One operation, one progress bar

The binding that draws a panel is what decides which tools may have one, because
**a client renders a panel for every tool result whose tool advertises the
resource**. So only the three tools that *start* an operation are bound to it —
one call, one job, one bar — and everything that merely reads or controls a job
that already exists is a plain tool with no UI of its own, registered by
`register_progress_tools` on the server rather than on the extension.

That split is the fix for the duplicate bars. `progress_get_status` used to be
bound to the panel, so every poll the model made rendered another one; each new
bar had no id of its own to poll and sat at "starting" for ever while the real
bar, the one belonging to the starting call, ran to completion. Two surfaces now
exist over one implementation:

| Surface | Tools | Bound to the panel |
| --- | --- | --- |
| The model | `progress_get_status`, `benchmark_get_result`, `progress_cancel`, `progress_pause` | No — polling draws nothing |
| The panel | `progress_panel_status`, `progress_panel_cancel`, `progress_panel_pause` | Yes, `visibility=["app"]` |

The panel's own tools are app-visible only: the view calls them over the
postMessage bridge, the model never sees them, and a call the view makes is
answered back into its iframe instead of being added to the conversation. That is
what lets the panel poll twice a second without the conversation growing a bar per
poll. Both surfaces call the same four functions in `gui/app.py`, so the model and
the view can never disagree about a job.

A poll is a read and nothing else. It creates no job, touches none, and answers
only for the id it was given: the running job when there is one, its persisted
record otherwise. There is no search, no fallback and no "newest job" guess, so a
request for one run can never be answered with another, and an unknown id reports
`found: false` rather than somebody else's progress. A snapshot names itself with
`id`, never `progress_id`, so a status result replayed at the view cannot be
mistaken for a starting call.

The view is deliberately dumb. It learns one `progress_id` from its own tool
result, polls with that id, and draws what comes back; the first id it adopts is
its own for good, so a later result naming a different operation is not that bar's
business. Only a snapshot whose status is `completed`, `failed` or `cancelled`
stops the polling — a timeout, a missing snapshot or a slow server means retry,
never "ended". A view that is never handed an operation at all polls nothing (it
has no id to poll with) and, after a few seconds, says so plainly instead of
animating "Starting…" indefinitely; a result arriving later — an approval prompt
can hold a call for minutes — is still adopted.

### How progress gets out while a tool is still running

Nothing has to. A starting tool creates its job, hands the work to a background
thread, and returns the `progress_id` within milliseconds, so the MCP request is
never held open for the length of the operation. The worker updates the job as it
goes, `gui/store.py` persists every snapshot, and both surfaces read that state
whenever they are asked. `gui/jobs.py`'s registry is a runtime cache over the
store, not a second source of truth: it exists so a Cancel button has an object to
call and so a reader mid-operation is not served a snapshot half a second old.
Because the record is written before the starting tool returns, a poll arriving
before the operation has really begun already finds a real snapshot.

Snapshots survive the process. A finished run is read back from its record, so
polling after the run ended — or after the server restarts — still answers, and
`gui/store.py` prunes records older than a week and keeps at most 200.

### Reopening a conversation

A restored panel takes the same path as a live one. The host replays the starting
tool's result, the view adopts the same `progress_id`, and the poll answers from
the persisted record: the bar draws its final state and stops. There is no
separate restoration path, and nothing is guessed, so a reopened conversation can
never adopt whatever happens to be running now.

### A benchmark's measurements are fetched, not polled

A download is fire-and-monitor: starting the transfer is the whole of the tool's
job, so there is nothing to collect afterwards. A benchmark is asked for
measurements, and its starting call cannot deliver them — it returns an
acknowledgement carrying a `progress_id`, with `result_available: false` and the
contract spelled out in the response. The measurements are written to
`MSHCore/data/progress/results/` when the run finishes and handed over by
`benchmark_get_result`, which is why they are not repeated in a snapshot polled
several times a second: a comparison result is far larger than the progress around
it.

All of this lives in `gui/`; MSHCore is untouched by it.

What each kind of operation reports is whatever MSHCore already exposes:

| Operation | Progress source |
| --- | --- |
| Downloads | `DownloadManager.get_status`, polled on a worker thread. Each item carries its own byte counts and transfer speed, so the rows show bytes and rate and the chip under the bar shows whichever file is transferring. `download_start_with_progress` returns as soon as the queue starts, like `download_start`, and the worker keeps the job current until the transfer ends. |
| Benchmarks | MSHCore logs an entry per prompt, so `gui/logtail.py` tails the execution log while `experiment.compare_tests` runs. For a comparison, the step names are the normalised configuration names. |

`gui/logtail.py` exists because `MSHCore.logging.read_logs` re-parses the whole
file per call, which is fine for a query but not for polling several times a
second. It keeps a byte offset, parses only what was appended, and holds back a
trailing fragment so a read landing mid-write is not lost. It splits a line the
way MSHCore does: only the four leading fields are free of the `" | "` separator,
so the message and the JSON details are separated by taking the longest trailing
segment that parses as JSON.

The log is a best-effort source of *live* progress, not the authority on the final
state. MSHCore writes a prompt's entry immediately before returning, so the
reader's last read routinely misses it, and a prompt that failed before MSHCore
got as far as logging produces no entry at all. Either way a row would be left
reading "waiting" under a job already marked complete — which is what left the
last configuration of a comparison stuck on "waiting". So when a benchmark
returns, its reader is stopped, joined, and every row is then closed from the
returned result, which is authoritative.

MSHCore also records a failed prompt as a result entry rather than raising, so a
run against a model that does not exist returns *normally* with every prompt
failed. Classifying the result is what turns that into a `failed` job rather than
a `completed` one with no measurements.

The panel shows no clock. `elapsed_seconds` is still in the snapshot, because
`progress_cancel` hands that snapshot to the model and how long an operation ran
before being stopped is worth knowing there, but nothing renders it: a bar, a
percentage and per-step rows already say where an operation is, and a second
ticking readout under them was noise.

### The tools

| Tool | Wraps | Panel | Progress shown |
| --- | --- | --- | --- |
| `download_start_with_progress` | `DownloadManager.start` | draws it | Per-file bars, bytes, overall percentage |
| `ollama_run_test_with_progress` | `experiment.compare_tests` | draws it | One row per prompt |
| `ollama_compare_tests_with_progress` | `experiment.compare_tests` | draws it | One row per configuration, advancing per prompt |
| `progress_get_status` | — | none | What the model polls |
| `benchmark_get_result` | — | none | Where a benchmark's measurements come from |
| `progress_cancel` | — | none | Cancel, by id |
| `progress_pause` | — | none | Stop or resume a download, by id |
| `progress_panel_status` / `_cancel` / `_pause` | — | app-visible | The view's own poll and buttons |

A client that did not negotiate Apps still gets every return value; it just does
not render the panel.

## Cancelling and stopping

The panel offers two controls, and they do different things.

**Cancel** ends the task and undoes it. It applies to both tracked operations —
download and benchmark — and behaves identically for each: the operation stops at
its next safe point, MSHCore removes everything it created, and the requirement it
is built around is that a cancelled operation leaves **no trace but the log
entry**. It cannot be undone.

**Stop** appears for downloads only and merely suspends the transfer. The queue,
the files already fetched and the partial data are all kept; pressing it again
resumes the active file from where it left off via an HTTP range request. It uses
`DownloadManager.pause` / `resume`, which MSHCore already had — nothing new was
written for it. Benchmarks have no equivalent, so no Stop button is shown for
them, and calling `progress_pause` on one reports `unavailable` rather than
cancelling it by surprise.

The two are styled to match that difference: Cancel is filled red, Stop is a
quiet outlined button. Side by side in the same red, they would invite pressing
the wrong one.

### How a cancellation travels

MSHCore owns the cancelling. `MSHCore/cancellation.py` adds a
`CancellationToken`: a thread-safe flag the caller passes into a long-running
function, which checks it at every point where it can stop without leaving
partial work behind and raises `OperationCancelled`. Cancelling is therefore
cooperative, and each operation cleans up on its way out.

It covers downloads and benchmarks, and only those — the two operations long
enough to be worth stopping mid-flight. Installations are not cancellable:
MSHCore runs an installer to completion, which is why `ollama_install` and
`python_install_python` have no progress variant and no Cancel.

`DownloadManager` holds a token of its own rather than a private flag, so its
`cancel`, the reason it was given, and the event its worker and keyboard
listener wake on are the same mechanism a benchmark uses. It is one-way by
design, which matches a session that cannot be restarted once cancelled.

The panel's side is in `gui/jobs.py`: a job holds a canceller — the token's
`cancel` for a benchmark, `DownloadManager.cancel` for a download — so the request
travels from the tool call to whichever thread is working. A Cancel pressed before
the operation registered its canceller is not dropped; it is applied as soon as one
arrives. `progress_cancel` waits for the cleanup to finish before returning, so its
result describes what actually happened rather than what was requested.

### What gets cleaned up

| Cancelled operation | What MSHCore removes |
| --- | --- |
| Download | Every file the session produced — `.part` files *and* files that had already completed, since the queue is one unit of work that did not finish. Files that were on disk before the session started are kept. The download directory goes too, if this session created it and it ends up empty. |
| Benchmark | The partial results are discarded, and the model the run loaded is unloaded, so the VRAM it was holding is released. |
| Comparison | Every finished configuration's results are discarded along with the interrupted one. |

Each of those writes one `WARNING` entry naming what it removed, which
`logs_read` will show. The job is then deregistered, so nothing about the cancelled
operation is left running in memory. Its record stays on disk in its final
`cancelled` state, which is what a later poll — or a reopened conversation — reads
back.

For a download, the session goes too. MSHCore's `cancel` closes the manager — its
queue is emptied and it refuses `add` and `start` afterwards — and `main.py`
removes it from the session registry and calls `purge`, which releases the queue
and the worker references. So after a cancellation there is no session, no job,
no queue entry, no worker thread and no downloader left: only the log entry.
That is what makes starting the same download again produce exactly one new task.

A cancellation is also safe at any stage. Before the queue was started there is
no worker, so `cancel` marks every item and runs the cleanup itself. Mid-transfer
it signals an event the worker and the downloader both stop on, and the worker
runs the cleanup as it exits. Paused counts as mid-transfer: the pause flag is
released first, or the chunk loop would never reach the check that stops it.
Cancelling twice is a no-op — the first call owns the outcome, so a later one
cannot widen a keep-files close into a deletion.

`download_close_session` deliberately does *not* clean up: closing a session is
bookkeeping, not a request to undo the download, so it keeps the files and only
drops the session. Cancelling a queue that already ran to completion also deletes
nothing — there is no unfinished work to undo.

### One bar per download

Starting a session that is already downloading is refused: `download_start_with_progress`
looks for a live job over that session and raises a `ToolError` naming the bar
already running. Two bars over one queue would each offer their own Cancel for the
same work, and whichever was pressed first would delete the files the other was
still reporting progress for. The session is free again as soon as its job
finishes, so starting a finished download again is a new task rather than a
duplicate.

Benchmarks have no such rule — running the same model twice is a legitimate thing
to ask for, and the two runs are separate operations with separate ids.

A job that has reached a terminal state also stops accepting updates. Its log
reader may still be draining one last entry, and without that a late reading could
reopen a step or overwrite `cancelled` with `completed` — leaving the bar claiming a
download succeeded after its files were deleted. Finishing a job drops its canceller
and pause controls too, so a late button press cannot call into an operation that
has already been cleaned up.

### Jobs and the store

`gui/store.py` is the source of truth: one run is one JSON file under
`MSHCore/data/progress`, written through a temporary file and an atomic replace, so
a reader sees either the previous snapshot or the new one and never a partial write.
A benchmark's measurements go in a second file under `results/`, written once as the
job finishes, because a comparison result is far larger than the progress around it
and must not be carried by a poll. Lookup is by exact identifier only — there is no
search — so a progress request either names a run that exists or it does not.

`gui/jobs.py` holds what the store persists. A job has a status, optional weighted
steps and metric chips; its percentage is derived from the steps unless a worker
sets one directly, and `None` means indeterminate. Jobs are mutated from a worker
thread and read from whichever thread answers a poll, so each guards its fields
with a lock and `snapshot()` returns a copy. Writes are throttled to twice a second
while a job runs, since progress arrives far faster than anyone reads it.

`finish` is the single terminal path and is idempotent: the first terminal status
wins, so a late reading cannot overwrite a cancellation with "completed". It stores
the result first, then the final snapshot — which is what advertises the result —
and only then deregisters the job, so a reader never sees `result_available: true`
with nothing to fetch, nor loses the job between memory and disk.

A job keeps no log of its own. MSHCore already writes an execution log entry for
everything worth recording, and `logs_read` is how that is read back.

A cancelling job passes through `cancelling` before `cancelled`: the request has
been sent but MSHCore is still cleaning up. The panel shows that as its own badge
and holds it, since a status poll already in flight when Cancel was pressed still
describes a running operation and would otherwise flip the badge back to
"running". A paused download stays `running` in MSHCore's own state and is
reported with `paused: true`, which the panel renders as its own badge — the
transfer is suspended, not over.

## Error handling

Argument validation is handled by the SDK from each tool's type annotations, so
a missing or mistyped argument fails before MSHCore is reached.

Beyond that, the SDK treats any exception other than `ToolError` as a crash: the
client gets a generic `Error executing tool <name>` and the real message stays on
the server. Since MSHCore raises descriptive exceptions, the `surface_core_errors`
decorator re-raises them with the type name and message intact and the original
chained as `__cause__`:

```text
ToolError: Error executing tool download_add:
    PermissionError: Access denied: domain 'evil.example.com' is not allowed.
```

MSHCore exceptions are forwarded, never caught and replaced.

## Claude Setup Utility

`utils/claude_setup.py` is a standalone desktop tool rather than an MCP tool: a
single-window Tkinter app that installs Claude Desktop and registers MCP
servers in `claude_desktop_config.json` — the same file as the client
configuration above, so pointing its form at `main.py` is the manual way to
give Claude Desktop this project's server. The installer is downloaded from
Anthropic's official per-architecture endpoints, with the links currently
advertised on the download page as a fallback, and entries are written for
both a classic install and the redirected location a Microsoft Store
(packaged) install reads. Claude, the Python interpreter and the config file
are all resolved at runtime from the registry, the `py` launcher and `PATH` —
nothing is hard-coded — and the config is backed up before every write.

It uses only the standard library, so it runs either as a script or as a
frozen bundle:

```bash
python utils/claude_setup.py
pyinstaller --onefile --noconsole utils/claude_setup.py
```

Detection and installation require Windows; on any other platform the window
still opens, but says detection is unavailable.
