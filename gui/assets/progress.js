/* ModelSetupHub progress panel — inlined into progress.html at import time.
 *
 * The panel is an MCP Apps view: it speaks the ext-apps postMessage JSON-RPC
 * dialect directly against window.parent, so it needs no bundler and no network
 * access. Progress itself comes from polling the app-visible
 * `progress_get_status` tool while the long-running tool call is still in
 * flight — the server answers it on a separate worker thread.
 */

(function () {
  "use strict";

  var PROTOCOL_VERSION = "2026-01-26";
  var APP_INFO = { name: "modelsetuphub-progress", version: "0.1.0" };
  var STATUS_TOOL = "progress_get_status";
  var CANCEL_TOOL = "progress_cancel";
  var PAUSE_TOOL = "progress_pause";

  // The host renders this panel as soon as the tool call is issued, which is
  // before the call has reached core and created its job. So the first polls are
  // fast, to pick the job up the moment it appears rather than sitting on a stale
  // "starting" frame for most of a second; the interval relaxes once real
  // progress is arriving and there is nothing to catch up to.
  //
  // Those first polls carry no id, and the server answers them by handing over the
  // newest *running* job no other panel has claimed — never simply the newest,
  // which during this window is the previous operation in the conversation.
  var FIRST_POLL_INTERVAL_MS = 150;
  var POLL_INTERVAL_MS = 600;

  // How long a panel waits on "Starting…" for a job to appear before concluding
  // there is not going to be one. A live operation's job exists within a few
  // hundred milliseconds of the panel opening, so anything past this is a panel
  // restored with its conversation — the tool call it belongs to ran long ago and
  // will not run again. Without a bound, such a panel waits for ever.
  var STARTUP_GRACE_MS = 4000;

  var TERMINAL = { completed: 1, failed: 1, cancelled: 1 };

  var pending = {};
  var nextId = 1;
  var hostCapabilities = {};
  var pinnedJobId = null;
  // Whether the pin came from this panel's own tool call, rather than from
  // claiming whichever job was unclaimed when polling began.
  var pinnedAuthoritatively = false;
  var pollTimer = null;
  var pollInterval = FIRST_POLL_INTERVAL_MS;
  var polling = false;
  var cancelling = false;
  var pausePending = false;
  var finished = false;
  var lastSnapshot = null;
  // When polling began, so a panel that never finds a job can stop waiting.
  var startedPollingAt = 0;
  // The tool result this panel was created with, as the host replays it when a
  // conversation is reopened. It carries the operation's own outcome, so a panel
  // whose job the server has since dropped can still show a real final frame.
  var storedResult = null;

  var dom = {};

  /* ---------------------------------------------------------------- transport */

  function send(message) {
    window.parent.postMessage(message, "*");
  }

  function request(method, params) {
    var id = nextId++;
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      send({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
    });
  }

  function notify(method, params) {
    send({ jsonrpc: "2.0", method: method, params: params || {} });
  }

  function respond(id, result) {
    send({ jsonrpc: "2.0", id: id, result: result || {} });
  }

  window.addEventListener("message", function (event) {
    var message = event.data;
    if (!message || message.jsonrpc !== "2.0") {
      return;
    }

    if (message.id !== undefined && message.method === undefined) {
      var waiter = pending[message.id];
      delete pending[message.id];
      if (!waiter) {
        return;
      }
      if (message.error) {
        waiter.reject(new Error(message.error.message || "Host error"));
      } else {
        waiter.resolve(message.result);
      }
      return;
    }

    if (message.method === undefined) {
      return;
    }

    // Requests from the host carry an id and must be answered; ui/ping and
    // ui/resource-teardown are the ones a passive view actually sees.
    if (message.id !== undefined) {
      respond(message.id, {});
      if (message.method === "ui/resource-teardown") {
        stopPolling();
      }
      return;
    }

    handleNotification(message.method, message.params || {});
  });

  function handleNotification(method, params) {
    if (method === "ui/notifications/tool-input") {
      applyToolInput(params.arguments || {});
      return;
    }

    if (method === "ui/notifications/tool-input-partial") {
      applyToolInput(params.arguments || {});
      return;
    }

    if (method === "ui/notifications/tool-result") {
      applyToolResult(params);
      return;
    }

    if (method === "ui/notifications/tool-cancelled") {
      finished = true;
      stopPolling();
      renderCancelled(params.reason);
      return;
    }

    if (method === "ui/notifications/host-context-changed") {
      applyHostContext(params);
    }
  }

  function applyHostContext(context) {
    if (!context) {
      return;
    }

    if (context.theme === "light" || context.theme === "dark") {
      document.documentElement.setAttribute("data-theme", context.theme);
    }

    var variables = context.styles && context.styles.variables;
    if (variables) {
      Object.keys(variables).forEach(function (name) {
        var value = variables[name];
        if (typeof value === "string" && name.indexOf("--") === 0) {
          document.documentElement.style.setProperty(name, value);
        }
      });
    }
  }

  function applyToolInput(args) {
    // An explicit progress_id is authoritative: the caller named the job it wants
    // this panel to show.
    if (typeof args.progress_id === "string" && args.progress_id) {
      pinTo(args.progress_id, true);
      return;
    }
    if (
      typeof args.session_id === "string" &&
      args.session_id &&
      !pinnedJobId &&
      dom.subtitle
    ) {
      setText(dom.subtitle, "session " + args.session_id);
    }
  }

  function applyToolResult(result) {
    // The result of the tool this panel belongs to carries that tool's own job
    // id, which is the one thing that identifies this panel's operation beyond
    // doubt. It arrives after polling has already started, so it is also the
    // correction for a panel that adopted the wrong job in the meantime.
    //
    // It is read from the envelope rather than through structuredOf: these tools
    // return {progress_id, result|status}, and structuredOf unwraps to the inner
    // payload, which does not carry the id.
    //
    // A host that re-renders a stored panel replays this notification, so the id
    // may name an operation that ended long ago. That is handled rather than
    // guarded against: the poll it triggers finds nothing on the server and the
    // panel says the operation has ended, instead of falling back to whatever is
    // running now.
    var envelope = result && result.structuredContent;
    if (
      envelope &&
      typeof envelope.progress_id === "string" &&
      envelope.progress_id
    ) {
      pinTo(envelope.progress_id, true);
    }

    // Kept so a restored panel can draw a real final frame from the operation's
    // own outcome once it learns the server no longer has the job.
    if (envelope) {
      storedResult = envelope;
    }

    // The tool returning does not always mean the work is over: a download tool
    // hands back a queue status and lets the transfer continue on a background
    // thread. So this only forces a refresh — polling stops when a snapshot
    // reports a terminal state, or when the host tears the panel down.
    if (finished) {
      // A replayed result for an operation already reported as over: refreshing
      // would only ask the server again for something it has forgotten.
      return;
    }

    poll();

    if (result && result.isError && lastSnapshot === null) {
      renderError(textOf(result));
    }
  }

  // Bind the panel to one job. `authoritative` marks an id that came from this
  // panel's own tool call rather than from claiming whatever was unclaimed, and
  // is allowed to correct an earlier guess; when it does, everything drawn for
  // the wrong job is cleared, so no part of another task's state survives.
  function pinTo(progressId, authoritative) {
    if (pinnedJobId === progressId) {
      if (authoritative) {
        pinnedAuthoritatively = true;
      }
      return;
    }

    if (pinnedAuthoritatively && !authoritative) {
      return;
    }

    var replacing = pinnedJobId !== null;

    pinnedJobId = progressId;
    pinnedAuthoritatively = Boolean(authoritative);

    if (replacing) {
      resetView();
    }
  }

  // Return the panel to its opening frame. Used when a pin is corrected: the
  // rows, metrics, badge and buttons on screen describe a different operation,
  // and leaving any of them would show one task's state under another's title.
  function resetView() {
    lastSnapshot = null;
    finished = false;
    cancelling = false;
    pausePending = false;

    // A host notification can arrive before the document is ready, in which case
    // there is nothing drawn to clear and the flags above are all that matter.
    if (!dom.title) {
      return;
    }

    dom.title.textContent = "Starting…";
    setText(dom.subtitle, null);

    dom.badge.dataset.state = "running";
    dom.badge.textContent = "running";

    dom.bar.dataset.state = "running";
    dom.bar.dataset.indeterminate = "true";
    dom.barFill.style.width = "0%";

    dom.cancel.classList.add("hidden");
    dom.cancel.disabled = false;
    dom.cancel.textContent = "Cancel";
    dom.pause.classList.add("hidden");
    dom.pause.disabled = false;

    renderMetrics({});
    renderSteps([]);

    if (hostCapabilities.serverTools) {
      startPolling();
    }
  }

  function textOf(result) {
    var blocks = (result && result.content) || [];
    for (var index = 0; index < blocks.length; index += 1) {
      if (blocks[index] && blocks[index].type === "text") {
        return blocks[index].text;
      }
    }
    return "The operation failed.";
  }

  /* ------------------------------------------------------------------ polling */

  function startPolling() {
    if (pollTimer !== null) {
      return;
    }
    startedPollingAt = Date.now();
    poll();
    schedule(FIRST_POLL_INTERVAL_MS);
  }

  function schedule(interval) {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
    }
    pollInterval = interval;
    pollTimer = window.setInterval(poll, interval);
  }

  function stopPolling() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function poll() {
    if (polling || !hostCapabilities.serverTools) {
      return Promise.resolve();
    }
    polling = true;

    var args = pinnedJobId ? { progress_id: pinnedJobId } : {};

    return request("tools/call", { name: STATUS_TOOL, arguments: args })
      .then(function (result) {
        var snapshot = structuredOf(result);
        if (!snapshot) {
          return;
        }

        // Nothing is tracked. Which of two very different situations this is
        // depends on whether the panel knows its own job yet.
        if (snapshot.tracked === false) {
          if (lastSnapshot !== null || cancelling) {
            // The job was there and is now gone: it was cancelled and purged.
            // Cancelling counts too — a cancellation that lands before the first
            // snapshot arrives still purges the job, and polling on would sit on
            // the placeholder forever.
            finished = true;
            stopPolling();
            if (lastSnapshot === null) {
              renderCancelled("Cancelled; everything it created was removed.");
            }
          } else if (pinnedAuthoritatively) {
            // This panel names its job and the server does not have it: the
            // conversation was reopened after the operation ended, or the server
            // has restarted since. Say so and stop. Showing whatever is running
            // now instead is exactly the leak this guards against.
            finished = true;
            stopPolling();
            renderUnavailable();
          } else if (Date.now() - startedPollingAt > STARTUP_GRACE_MS) {
            // No job has appeared and none is coming. This is a panel restored
            // with its conversation: the host re-renders it, but the tool call it
            // belongs to ran in the past and is not running again, so no job will
            // ever be created for it. Waiting longer would leave "Starting…" on
            // screen indefinitely.
            finished = true;
            stopPolling();
            renderUnavailable();
          }
          // Otherwise the tool call has not reached the server yet. Keep the
          // "starting" frame and keep polling fast; the placeholder would be
          // wrong here, since there is something to report, it just does not
          // exist yet.
          return;
        }

        // The first snapshot of an unpinned poll is the job the server handed
        // this panel, and no other panel will be given it. Pinning here is what
        // keeps every later poll on that same job even as newer ones start.
        //
        // But an unpinned poll is a guess until this panel's own tool result
        // confirms it, and one guess is never acceptable: a snapshot that is
        // already finished cannot belong to a panel that has only just opened, so
        // it is ignored rather than drawn. That is the rule that stops a panel in
        // any chat from adopting a previous task's results, however the server
        // came to offer them.
        if (!pinnedAuthoritatively && lastSnapshot === null && TERMINAL[snapshot.state]) {
          return;
        }

        pinTo(snapshot.progress_id, false);

        // A job exists, so there is no longer a gap to close.
        if (pollInterval !== POLL_INTERVAL_MS && !TERMINAL[snapshot.state]) {
          schedule(POLL_INTERVAL_MS);
        }

        render(snapshot);
      })
      .catch(function (error) {
        // A poll failure is transient by nature — the job keeps running and the
        // next tick retries — so it is only surfaced when nothing was ever drawn.
        if (lastSnapshot === null) {
          renderError(error.message);
        }
      })
      .then(function () {
        polling = false;
      });
  }

  function structuredOf(result) {
    if (!result) {
      return null;
    }
    if (result.structuredContent && typeof result.structuredContent === "object") {
      var wrapped = result.structuredContent;
      return wrapped.result !== undefined ? wrapped.result : wrapped;
    }
    var blocks = result.content || [];
    for (var index = 0; index < blocks.length; index += 1) {
      if (blocks[index] && blocks[index].type === "text") {
        try {
          return JSON.parse(blocks[index].text);
        } catch (error) {
          return null;
        }
      }
    }
    return null;
  }

  /* ------------------------------------------------------------ cancel & pause */

  function requestCancel() {
    if (cancelling || finished) {
      return;
    }

    cancelling = true;
    dom.cancel.disabled = true;
    dom.cancel.textContent = "Cancelling…";
    dom.pause.disabled = true;
    dom.badge.dataset.state = "cancelling";
    dom.badge.textContent = "cancelling";
    dom.bar.dataset.state = "cancelling";
    setText(dom.subtitle, "Cancelling and cleaning up…");

    // The cancel tool returns only once core has finished undoing the operation,
    // so its result is the authoritative final state. Polling continues in the
    // meantime, which is what keeps the panel live during the cleanup.
    var args = pinnedJobId ? { progress_id: pinnedJobId } : {};

    request("tools/call", { name: CANCEL_TOOL, arguments: args })
      .then(function (result) {
        var snapshot = structuredOf(result);
        if (snapshot) {
          render(snapshot);
        }
        finished = true;
        stopPolling();
      })
      .catch(function (error) {
        cancelling = false;
        dom.cancel.disabled = false;
        dom.cancel.textContent = "Cancel";
        setText(dom.subtitle, "Could not cancel: " + error.message);
      });
  }

  function requestPause() {
    if (pausePending || cancelling || finished) {
      return;
    }

    pausePending = true;
    dom.pause.disabled = true;

    var args = pinnedJobId ? { progress_id: pinnedJobId } : {};

    request("tools/call", { name: PAUSE_TOOL, arguments: args })
      .then(function (result) {
        var snapshot = structuredOf(result);
        if (snapshot && snapshot.tracked !== false) {
          render(snapshot);
        }
      })
      .catch(function (error) {
        setText(dom.subtitle, "Could not pause: " + error.message);
      })
      .then(function () {
        pausePending = false;
      });
  }

  /* ---------------------------------------------------------------- rendering */

  function render(snapshot) {
    // A snapshot for a job this panel is not showing is dropped rather than
    // drawn: a poll already in flight when the pin was corrected would otherwise
    // paint the old operation over the new one.
    if (
      pinnedJobId &&
      typeof snapshot.progress_id === "string" &&
      snapshot.progress_id !== pinnedJobId
    ) {
      return;
    }

    lastSnapshot = snapshot;

    var state = snapshot.state || "running";

    // A poll in flight when Cancel was pressed still describes a running
    // operation, and the server only reports "stopping" once its own state has
    // caught up. Without this the badge and subtitle would flip back to
    // "running" underneath the button that just said it was cancelling.
    if (cancelling && !TERMINAL[state]) {
      state = "cancelling";
    } else if (state === "stopping") {
      state = "cancelling";
    } else if (snapshot.paused && state === "running") {
      state = "paused";
    }

    var determinate = snapshot.determinate !== false && snapshot.percent !== null;

    dom.title.textContent = snapshot.title || "Working…";
    setText(
      dom.subtitle,
      state === "cancelling"
        ? "Cancelling and cleaning up…"
        : snapshot.subtitle
    );

    dom.badge.dataset.state = state;
    dom.badge.textContent = state;

    renderControls(snapshot, state);

    dom.bar.dataset.state = state;
    dom.bar.dataset.indeterminate = state === "running" && !determinate ? "true" : "false";
    dom.barFill.style.width = clampPercent(snapshot.percent) + "%";

    renderMetrics(snapshot);
    renderSteps(snapshot.steps || []);

    if (TERMINAL[state]) {
      finished = true;
      stopPolling();
    }
  }

  function renderControls(snapshot, state) {
    var live = !TERMINAL[state] && Boolean(hostCapabilities.serverTools);

    // Cancel ends the task and cleans up; it is offered for every operation.
    var canCancel = snapshot.cancellable === true && live && !cancelling;
    dom.cancel.classList.toggle("hidden", !canCancel && !cancelling);

    if (canCancel) {
      dom.cancel.disabled = false;
      dom.cancel.textContent = "Cancel";
      dom.cancel.title = "Cancel this task and undo what it has done";
    }

    // Stop only suspends the transfer, so it exists for downloads alone — they
    // are the only operation core can pause and resume.
    var canPause = snapshot.pausable === true && live && !cancelling;
    dom.pause.classList.toggle("hidden", !canPause);

    if (canPause) {
      dom.pause.disabled = pausePending;
      dom.pause.textContent = snapshot.paused ? "Resume" : "Stop";
      dom.pause.title = snapshot.paused
        ? "Continue the download from where it stopped"
        : "Stop the download without cancelling the task";
    }

    if (TERMINAL[state]) {
      dom.cancel.classList.add("hidden");
      dom.pause.classList.add("hidden");
    }
  }

  function renderMetrics(snapshot) {
    var metrics = snapshot.metrics || [];
    var percent = snapshot.percent;
    var parts = [];

    if (percent !== null && percent !== undefined) {
      parts.push(pair(Math.round(clampPercent(percent)) + "%", "complete"));
    }

    metrics.forEach(function (metric) {
      parts.push(pair(metric.value, metric.label));
    });

    dom.metrics.replaceChildren.apply(dom.metrics, parts);
    dom.metrics.classList.toggle("hidden", parts.length === 0);
  }

  function pair(value, label) {
    var wrapper = document.createElement("span");
    var strong = document.createElement("span");
    strong.className = "value";
    strong.textContent = String(value === undefined || value === null ? "—" : value);
    wrapper.appendChild(strong);
    if (label) {
      wrapper.appendChild(document.createTextNode(" " + label));
    }
    return wrapper;
  }

  function renderSteps(steps) {
    var items = steps.map(function (step) {
      var item = document.createElement("li");
      item.className = "step";
      item.dataset.state = step.state || "waiting";

      var name = document.createElement("span");
      name.className = "step-name";
      name.textContent = step.name || "step";
      item.appendChild(name);

      var value = document.createElement("span");
      value.className = "step-value";
      value.textContent = step.detail || (step.state || "waiting");
      item.appendChild(value);

      var determinate = step.percent !== null && step.percent !== undefined;
      if (determinate || step.state === "running") {
        var bar = document.createElement("div");
        bar.className = "bar step-bar";
        bar.dataset.state = step.state || "waiting";
        bar.dataset.indeterminate =
          step.state === "running" && !determinate ? "true" : "false";

        var fill = document.createElement("div");
        fill.className = "bar-fill";
        fill.style.width = clampPercent(step.percent) + "%";
        bar.appendChild(fill);
        item.appendChild(bar);
      }

      if (step.note) {
        var note = document.createElement("span");
        note.className = "step-note";
        note.dataset.level = step.state === "failed" ? "error" : "info";
        note.textContent = step.note;
        item.appendChild(note);
      }

      return item;
    });

    dom.steps.replaceChildren.apply(dom.steps, items);
    dom.steps.classList.toggle("hidden", items.length === 0);
  }

  function renderError(message) {
    dom.badge.dataset.state = "failed";
    dom.badge.textContent = "failed";
    dom.bar.dataset.state = "failed";
    dom.bar.dataset.indeterminate = "false";
    dom.barFill.style.width = "100%";
    dom.cancel.classList.add("hidden");
    dom.pause.classList.add("hidden");
    setText(dom.subtitle, message || "The operation failed.");
  }

  function renderCancelled(reason) {
    dom.badge.dataset.state = "cancelled";
    dom.badge.textContent = "cancelled";
    dom.bar.dataset.state = "cancelled";
    dom.bar.dataset.indeterminate = "false";
    dom.cancel.classList.add("hidden");
    dom.pause.classList.add("hidden");
    setText(dom.subtitle, reason || "Cancelled.");
  }

  // The panel's own operation is not on the server: the conversation was reopened
  // after it ended, or the server has restarted. The tool result the host replays
  // alongside the panel carries that operation's own outcome, so the final frame
  // is rebuilt from it — a filled bar with the rows it finished. Only when there
  // is no result to read does the panel fall back to saying so. Either way nothing
  // is taken from whatever else happens to be running.
  function renderUnavailable() {
    var restored = restoredFrame(storedResult);

    if (restored) {
      render(restored);
      return;
    }

    dom.title.textContent = "Progress no longer available";
    dom.badge.dataset.state = "completed";
    dom.badge.textContent = "ended";
    dom.bar.dataset.state = "completed";
    dom.bar.dataset.indeterminate = "false";
    dom.barFill.style.width = "0%";
    dom.cancel.classList.add("hidden");
    dom.pause.classList.add("hidden");
    renderMetrics({});
    renderSteps([]);
    setText(
      dom.subtitle,
      "This operation has ended. Its outcome is in the conversation, and " +
        "logs_read has the full record."
    );
  }

  // Rebuild a finished snapshot from the tool result the panel was created with.
  // Only the shapes these tools actually return are handled; anything else gives
  // null and the caller says the record is no longer available.
  function restoredFrame(envelope) {
    if (!envelope) {
      return null;
    }

    var steps = null;
    var title = null;

    var payload = envelope.result;
    var status = envelope.status;

    if (payload && Array.isArray(payload.results)) {
      // A single benchmark: one row per prompt.
      title = "Benchmarked " + (payload.model || "model");
      steps = payload.results.map(function (entry, index) {
        return {
          name: "prompt " + (entry.index || index + 1),
          state: entry.success === false ? "failed" : "completed",
          percent: 100,
          detail: rateOf(entry),
          note: entry.error || null,
        };
      });
    } else if (payload && Array.isArray(payload.tests)) {
      // A comparison: one row per configuration.
      title = "Compared " + payload.tests.length + " configuration(s)";
      steps = payload.tests.map(function (test) {
        var results = test.results || [];
        var summary = test.summary || {};
        var rate = summary.average_output_tokens_per_second;
        var count = results.length + "/" + results.length;
        return {
          name: test.name || "configuration",
          state: "completed",
          percent: 100,
          detail:
            typeof rate === "number" ? count + " · " + rate.toFixed(1) + " tok/s" : count,
          note: null,
        };
      });
    } else if (status && Array.isArray(status.downloads)) {
      // A download queue: one row per file.
      title = "Downloaded " + status.downloads.length + " file(s)";
      steps = status.downloads.map(function (item) {
        return {
          name: item.filename || "file",
          state: item.status === "completed" ? "completed" : item.status,
          percent: item.status === "completed" ? 100 : null,
          detail: item.status,
          note: item.error || null,
        };
      });
    }

    if (!steps || !steps.length) {
      return null;
    }

    return {
      progress_id: envelope.progress_id || null,
      tracked: true,
      title: title,
      subtitle: "Finished earlier in this conversation.",
      state: "completed",
      percent: 100,
      determinate: true,
      cancellable: false,
      pausable: false,
      paused: false,
      cancelling: false,
      steps: steps,
      metrics: [],
    };
  }

  function rateOf(entry) {
    if (typeof entry.output_tokens_per_second === "number") {
      return entry.output_tokens_per_second.toFixed(1) + " tok/s";
    }
    if (typeof entry.duration_seconds === "number") {
      return entry.duration_seconds.toFixed(1) + "s";
    }
    return null;
  }

  /* ---------------------------------------------------------------- utilities */

  function setText(element, value) {
    element.textContent = value || "";
    element.classList.toggle("hidden", !value);
  }

  function clampPercent(percent) {
    if (percent === null || percent === undefined || isNaN(percent)) {
      return 0;
    }
    return Math.max(0, Math.min(100, Number(percent)));
  }

  function reportSize() {
    var height = Math.ceil(document.documentElement.getBoundingClientRect().height);
    notify("ui/notifications/size-changed", { height: height });
  }

  /* ----------------------------------------------------------------- lifecycle */

  function main() {
    dom = {
      title: document.getElementById("title"),
      subtitle: document.getElementById("subtitle"),
      badge: document.getElementById("badge"),
      cancel: document.getElementById("cancel"),
      pause: document.getElementById("pause"),
      bar: document.getElementById("bar"),
      barFill: document.getElementById("bar-fill"),
      metrics: document.getElementById("metrics"),
      steps: document.getElementById("steps"),
    };

    dom.cancel.addEventListener("click", requestCancel);
    dom.pause.addEventListener("click", requestPause);

    request("ui/initialize", {
      appInfo: APP_INFO,
      appCapabilities: {},
      protocolVersion: PROTOCOL_VERSION,
    })
      .then(function (result) {
        hostCapabilities = (result && result.hostCapabilities) || {};
        applyHostContext((result && result.hostContext) || {});
        notify("ui/notifications/initialized", {});

        if (!hostCapabilities.serverTools) {
          renderError(
            "This client does not allow the panel to read progress, so the " +
              "result will arrive in the chat instead."
          );
          return;
        }

        if (!finished) {
          startPolling();
        }
      })
      .catch(function (error) {
        renderError(error.message);
      });

    if (typeof ResizeObserver === "function") {
      new ResizeObserver(reportSize).observe(document.documentElement);
    }
    window.addEventListener("beforeunload", stopPolling);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", main, { once: true });
  } else {
    main();
  }
})();
