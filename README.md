# ModelSetupHub MCP Server

MCP (Model Context Protocol) layer over the `core` package in the `Core`
submodule. Nearly every tool is a thin pass-through to an existing core
function; the only logic this layer adds is the in-chat progress panel and the
job bookkeeping behind it.

## Layout

```text
<project root>/
├── main.py            # the MCP tool layer
├── gui/               # the in-chat progress panel
│   ├── __init__.py
│   ├── app.py         # MCP Apps extension: ui:// resource + bound tools
│   ├── tracking.py    # runs core operations, publishes progress, wires stopping
│   ├── jobs.py        # job model and registry
│   ├── logtail.py     # incremental reader for core's execution log
│   ├── loader.py      # inlines the assets into one HTML document
│   └── assets/
│       ├── progress.html
│       ├── progress.css
│       └── progress.js
├── requirements.txt
└── Core/              # submodule
    └── core/          # backend logic
```

`main.py` holds the MCP layer: `sys.path` wiring for the submodule, the error
forwarder, one `register_*_tools` function per core module, and the
`create_server` / `main` entry point. Sections are separated by banner comments
matching the core module they wrap.

Before importing `core`, `main.py` puts the `Core` submodule root on `sys.path` —
the directory *containing* `core`, not `core` itself — because core imports
itself as a top-level package (`from core.logging import write_log`). If the
submodule is not checked out, it raises at startup with the command needed to
fetch it, rather than failing later on an opaque `ModuleNotFoundError`.

Core is then imported as `core.x`, never `Core.core.x`. Both spellings resolve to
the same file but produce two separate module objects, each with its own copy of
every class — so a `CancellationToken` handed to core and the
`OperationCancelled` caught back from it would belong to different classes, and
`except OperationCancelled` would silently miss it.

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

54 tools. 48 are thin pass-throughs to core, grouped by area below; the other 6
belong to the progress panel and are documented in [Progress panel](#progress-panel).
Names are prefixed by area, and each carries MCP annotations so a client can tell
a read-only call from a destructive one.

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
| `list_ollama_logs` | `list_ollama_logs` |
| `read_ollama_logs` | `read_ollama_logs` |
| `ollama_start` | `start` |
| `ollama_stop` | `stop` |
| `ollama_install` | `install` |

The mutating three return `get_status()` afterwards, since the core functions
return `None`.

The two log tools read Ollama's own log files — the live `app.log` and
`server.log` plus every rotated copy — and are unrelated to `logs_read`, which
reads this project's execution log. They work as a pair: `list_ollama_logs`
returns a `{file name: size in bytes}` dict, alongside per-file paths and
modification times, and `read_ollama_logs` takes one of those names and returns
that file in full. Splitting it that way keeps the caller from pulling megabytes
it did not ask for, and the sizes are what make the choice informed —
`server.log` alone routinely passes a megabyte, and it is the one file that says
why a service start or model load failed. `read_ollama_logs` accepts a bare file
name only, so a path cannot reach outside the log directories.

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

Core rejects any host outside its whitelist, so check
`download_list_allowed_domains` before queueing a URL.

### Logging — `core.logging`

| Tool | Core function |
| --- | --- |
| `logs_read` | `read_logs` |
| `logs_get_path` | `get_execution_log_path` |

`write_log` is not exposed: core writes its own entries, and letting a client
inject arbitrary records would pollute the execution history.

## Progress panel

Anything slow — downloads, installers, benchmarks — has a `_with_progress`
variant that renders a live progress bar in the conversation itself, next to the
assistant's message, with a red **Cancel** button in its top-right corner and,
for downloads, a **Stop** button beside it. Everything for it lives in `gui/`;
`main.py` only imports `create_progress_app` and passes it to
`MCPServer(extensions=[...])`.

`system_scan` has no progress variant. It takes a few seconds and has nothing
worth watching, so the bar was not carrying its weight there.

### How it reaches the chat

The panel is an [MCP Apps](https://modelcontextprotocol.io) extension
(`io.modelcontextprotocol/ui`), which the SDK ships as `mcp.server.apps`. A tool
advertises `_meta.ui.resourceUri` pointing at a `ui://` HTML resource, and the
client renders that resource in a sandboxed iframe inline in the conversation.
The extension is purely additive: it contributes one resource and six tools,
and intercepts nothing.

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

### How progress gets out while a tool is still running

The panel polls `progress_get_status` over the Apps postMessage bridge. That
works because the SDK dispatches each request on its own task and runs a sync
tool function on a worker thread, so a status poll is answered while the tool
call that owns the operation is still blocked inside core.

### State isolation: which job a panel shows

This is the part of the design that needed the most care, because two facts about
the environment work against the obvious implementation. The server is **one
process shared by every conversation** — there is no per-chat scope to hang state
on. And a panel is **part of a conversation's record**: a host that re-renders a
stored one replays the tool input and result it was created with, so an identifier
can arrive naming an operation that finished long ago, in a chat that is no longer
open.

On top of that, the host renders a panel as soon as its tool call is *issued* —
before the call has reached the server and created its job — so the panel's first
polls carry no identifier at all and something has to decide what they mean.

Answering those with "the newest job" is what leaked. During that opening window
the newest job is the *previous* task's, so a panel adopted a finished operation,
drew its final state, and — the state being terminal — stopped polling and never
saw the job it was opened for. Four rules replace that guess, and each closes a
different path:

1. **A finished job is not retained.** Once an operation ends, the panel showing it
   has already drawn its last frame, so the server's copy has no further reader. It
   is kept for `FINISHED_GRACE_SECONDS` — enough for an in-flight poll and the
   tool's own result to land — and then dropped. Nothing stale is left to serve.
2. **Identifiers are unguessable and namespaced per process.** Each is
   `progress-<instance>-<random>`, the instance minted at startup. An id from an
   earlier run of the server is refused outright instead of colliding with a
   current job — which sequential `progress-1` ids made not merely possible but
   likely across a restart.
3. **A job is claimable only briefly, and only once.** An id-less poll is answered
   with a job that is running, unclaimed, *and* seconds old, because a panel polls
   within milliseconds of opening. A download that was already underway when the
   panel opened belongs to another conversation and is never handed over. Asking by
   id claims the job too, so a pinned panel's job is not offered to the next panel.
4. **The panel refuses a finished job it did not ask for.** Until its own tool
   result confirms the pin, a snapshot in a terminal state cannot belong to a panel
   that has only just opened, so it is ignored rather than drawn — a backstop that
   holds even if the server offers one.

The panel keeps its end of it. Once a snapshot arrives it pins to that
`progress_id` and names it in every later poll, so it stays on its own job as newer
ones start. When its tool call returns, the `progress_id` in the result is
authoritative and overrides an earlier guess; if it names a different job, the bar,
badge, buttons, metrics and step rows are all cleared before the new one is drawn,
so no fragment of one task appears under another's title. A snapshot for any other
job is dropped rather than rendered.

### Reopening a conversation

A restored panel has no job to poll: the tool call it belongs to ran in the past
and is not running again, so the server will never create one. Left alone the panel
sits on "Starting…" indefinitely, waiting for something that is not coming.

So a panel gives up after `STARTUP_GRACE_MS` — a live operation's job exists within
a few hundred milliseconds of the panel opening, so anything past a few seconds is
a restored panel — and then rebuilds a final frame from the tool result the host
replays alongside it. That result carries the operation's own outcome: the per-prompt
rates of a benchmark, the per-configuration averages of a comparison, the queue of a
download. The bar fills, the rows show what each step produced, and the subtitle says
it finished earlier in the conversation. Nothing is read from the server, so nothing
can be borrowed from another task.

When there is no result to rebuild from — a panel whose tool errored, say — it says
"Progress no longer available" with an empty bar instead. Either way the panel shows
its *own* operation or nothing, never whatever happens to be running now.

`progress_cancel` and `progress_pause` called without an id act on the newest
operation still *running*, which is what the model means by "cancel the download"
and the only thing that can still have an effect.

All of this lives in `gui/`; `core` is untouched by it.

What each kind of operation reports is whatever core already exposes:

| Operation | Progress source |
| --- | --- |
| Downloads | `DownloadManager.get_status`, polled on a watcher thread. `download_start_with_progress` returns as soon as the queue starts, like `download_start`, and the watcher keeps the panel current until the transfer ends. |
| Benchmarks | Core logs an entry per prompt, so `gui/logtail.py` tails the execution log while `experiment.run_test` / `compare_tests` run. For a comparison, the step names come from the normalised configuration list core logs when it starts. |
| Installers and package installs | Nothing measurable to report, so the bar stays indeterminate and the panel tracks which step is active. |

`gui/logtail.py` exists because `core.logging.read_logs` re-parses the whole file
per call, which is fine for a query but not for polling several times a second.
It keeps a byte offset, parses only what was appended, and holds back a trailing
fragment so a read landing mid-write is not lost.

The log is a best-effort source of *live* progress, not the authority on the final
state. Core writes a prompt's entry immediately before returning, so the watcher's
last read routinely misses it, and a prompt that failed before core got as far as
logging produces no entry at all. Either way a row would be left reading "waiting"
under a job the panel had already marked complete — which is what left the last
configuration of a comparison stuck on "waiting". So when a benchmark or comparison
returns, its watcher is stopped, joined, and every row is then closed from the
returned result: one entry per prompt for a run, matched by configuration name for
a comparison. A comparison short enough that the watcher never saw the entry its
rows are built from has them built from the result instead.

The panel shows no clock. `elapsed_seconds` is still in the snapshot, because
`progress_cancel` hands that snapshot to the model and how long an operation ran
before being stopped is worth knowing there, but nothing renders it: a bar, a
percentage and per-step rows already say where an operation is, and a second
ticking readout under them was noise.

### The tools

| Tool | Wraps | Progress shown |
| --- | --- | --- |
| `download_start_with_progress` | `DownloadManager.start` | Per-file bars, bytes, overall percentage |
| `ollama_run_test_with_progress` | `experiment.run_test` | One row per prompt with its tokens per second |
| `ollama_compare_tests_with_progress` | `experiment.compare_tests` | One row per configuration, advancing per prompt |
| `progress_get_status` | — | The panel's own poll target |
| `progress_cancel` | — | The Cancel button's target |
| `progress_pause` | — | The Stop button's target (downloads only) |

Each returns the same data as its plain counterpart under a `result` key, plus
the `progress_id` the panel polls with. `progress_get_status` is marked
`visibility=["app"]` so it serves the panel without appearing in the model's tool
list; `progress_cancel` and `progress_pause` stay visible, so the model can act
on an operation when asked to in words rather than by clicking.

A client that did not negotiate Apps still gets the return value; it just does
not render the panel. `progress_get_status` answers for the newest operation when
called without an id, and returns a placeholder rather than failing when nothing
is tracked.

## Cancelling and stopping

The panel offers two controls, and they do different things.

**Cancel** ends the task and undoes it. It applies to all three long-running
operations — download, benchmark, installation — and behaves identically for each:
the operation stops at its next safe point, core removes everything it created,
and the requirement it is built around is that a cancelled operation leaves **no
trace but the log entry**. It cannot be undone.

**Stop** appears for downloads only and merely suspends the transfer. The queue,
the files already fetched and the partial data are all kept; pressing it again
resumes the active file from where it left off via an HTTP range request. It uses
`DownloadManager.pause` / `resume`, which core already had — nothing new was
written for it. Benchmarks and installations have no equivalent, so no Stop button
is shown for them, and calling `progress_pause` on one reports `unavailable`
rather than cancelling it by surprise.

The two are styled to match that difference: Cancel is filled red, Stop is a
quiet outlined button. Side by side in the same red, they would invite pressing
the wrong one.

### How a cancellation travels

Core owns the cancelling. `core/cancellation.py` adds a `CancellationToken`: a
thread-safe flag the caller passes into a long-running function, which checks it
at every point where it can stop without leaving partial work behind and raises
`OperationCancelled`. Cancelling is therefore cooperative, and each operation
cleans up on its way out.

A subprocess cannot be stopped by checking a flag, so `run_cancellable` runs it
with a watcher: on cancellation it terminates the child's *whole process tree*
(`taskkill /T` on Windows, the process group elsewhere), because an installer is
usually a launcher and killing only the launcher leaves the real work running.

The panel's side is in `gui/jobs.py`: a job holds a canceller — the token's
`cancel` for benchmarks and installers, `DownloadManager.cancel` for downloads —
so the request travels from the panel's tool call to whichever thread is working.
A Cancel pressed before the operation registered its canceller is not dropped; it
is applied as soon as one arrives. `progress_cancel` waits for the cleanup to
finish before returning, so its result describes what actually happened rather
than what was requested.

### What gets cleaned up

| Cancelled operation | What core removes |
| --- | --- |
| Download | Every file the session produced — `.part` files *and* files that had already completed, since the queue is one unit of work that did not finish. Files that were on disk before the session started are kept. The download directory goes too, if this session created it and it ends up empty. |
| Benchmark | The partial results are discarded, and the model the run loaded is unloaded, so the VRAM it was holding is released. |
| Comparison | Every finished configuration's results are discarded along with the interrupted one. |
| Ollama install | The installer's process tree is terminated, and if Ollama was absent beforehand but the interrupted installer had already registered it, the service is stopped and the registered uninstaller is run. An installation that was already there is left alone. |
| Python install | The process tree is terminated, and any interpreter this run registered is uninstalled with the same installer's `/uninstall`. Interpreters that were already present are untouched. |
| Package install | Pip is terminated, and every distribution the run added — dependencies included, found by comparing against a snapshot taken before it started — is uninstalled. Packages that were already installed stay. |

Each of those writes one `WARNING` entry naming what it removed, which
`logs_read` will show. The job record itself is then dropped from the registry,
so nothing about the cancelled operation survives in memory either. The panel
keeps its final "cancelled" state on screen because the cancel call's own result
carries that last snapshot.

For a download, the session goes too. Core's `cancel` closes the manager — its
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

### One job per operation

Every job carries a key identifying the operation it tracks: the download session
id, the installer path, the model and label of a benchmark. `JobRegistry.create`
refuses a second *live* job for a key and raises `DuplicateJob`, which the tools
surface as a `ToolError` naming the bar already running. Two bars over one
operation would each offer their own Cancel over the same work, and whichever was
pressed first would undo what the other was still reporting progress for.

The key is released as soon as the job finishes, so starting a finished operation
again is a new task rather than a duplicate.

A job that has reached a terminal state also stops accepting updates. Its watcher
thread may still be draining one last reading, and without that a poll landing
after a cancellation could reopen a step or overwrite `cancelled` with
`completed` — leaving the panel claiming a download succeeded after its files were
deleted. Finishing a job drops its canceller and pause controls too, so a late
button press cannot call into an operation that has already been cleaned up.

### Jobs

`gui/jobs.py` holds the state the panel reads. A job has a state, optional
weighted steps and metric chips; its percentage is derived from the steps unless a
caller sets one directly, and `None` means indeterminate. Jobs are written from a
worker thread and read from whichever thread answers a poll, so each guards its
fields with a lock and `snapshot()` returns a copy.

The registry holds what is running, plus what finished in the last few seconds; it
never holds a finished job indefinitely, which is what keeps one conversation's
results out of another's panel (see
[State isolation](#state-isolation-which-job-a-panel-shows)). Running jobs are
never dropped, however long they take.

A job keeps no log of its own. Core already writes an execution log entry for
everything worth recording, and `logs_read` is how that is read back; the panel's
transcript duplicated it in a window too small to read comfortably, so it is gone.

A cancelling job passes through `cancelling` before `cancelled`: the request has
been sent but core is still cleaning up. The panel shows that state and holds it,
since a status poll already in flight when Cancel was pressed still describes a
running operation and would otherwise flip the badge back to "running". A paused
download stays `running` in core's own state and is reported with `paused: true`,
which the panel renders as its own state — the transfer is suspended, not over.

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
