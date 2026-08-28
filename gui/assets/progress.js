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
  var FIRST_POLL_INTERVAL_MS = 150;
  var POLL_INTERVAL_MS = 600;

  var TERMINAL = { completed: 1, failed: 1, cancelled: 1 };

  var pending = {};
  var nextId = 1;
  var hostCapabilities = {};
  var pinnedJobId = null;
  var pollTimer = null;
  var pollInterval = FIRST_POLL_INTERVAL_MS;
  var polling = false;
  var cancelling = false;
  var pausePending = false;
  var finished = false;
  var lastSnapshot = null;

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
    // A caller may pin the panel to one job explicitly; otherwise the server
    // reports whichever job is newest, and the first snapshot pins it.
    if (typeof args.progress_id === "string" && args.progress_id) {
      pinnedJobId = args.progress_id;
    }
    if (typeof args.session_id === "string" && args.session_id && !pinnedJobId) {
      dom.subtitle.textContent = "session " + args.session_id;
    }
  }

  function applyToolResult(result) {
    // The tool returning does not always mean the work is over: a download tool
    // hands back a queue status and lets the transfer continue on a background
    // thread. So this only forces a refresh — polling stops when a snapshot
    // reports a terminal state, or when the host tears the panel down.
    poll();

    if (result && result.isError && lastSnapshot === null) {
      renderError(textOf(result));
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

        // Nothing is tracked yet: the tool call that opened this panel has not
        // reached core. Keep the "starting" frame and keep polling fast — showing
        // the server's "Nothing to report" placeholder here would be wrong, since
        // there is something to report, it just does not exist yet.
        if (snapshot.tracked === false) {
          if (lastSnapshot !== null) {
            // The job was there and is now gone: it was cancelled and purged.
            stopPolling();
          }
          return;
        }

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
    lastSnapshot = snapshot;

    if (!pinnedJobId && typeof snapshot.progress_id === "string") {
      pinnedJobId = snapshot.progress_id;
    }

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
    renderLog(snapshot.log || []);

    dom.elapsed.textContent = snapshot.elapsed_seconds
      ? "elapsed " + formatDuration(snapshot.elapsed_seconds)
      : "";
    dom.eta.textContent =
      state === "running" && snapshot.eta_seconds
        ? "about " + formatDuration(snapshot.eta_seconds) + " left"
        : "";

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

  function renderLog(lines) {
    dom.log.textContent = lines.join("\n");
    dom.log.classList.toggle("hidden", lines.length === 0);
    dom.log.scrollTop = dom.log.scrollHeight;
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

  function formatDuration(seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var rest = total % 60;

    if (hours) {
      return hours + "h " + minutes + "m";
    }
    if (minutes) {
      return minutes + "m " + rest + "s";
    }
    return rest + "s";
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
      log: document.getElementById("log"),
      elapsed: document.getElementById("elapsed"),
      eta: document.getElementById("eta"),
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
