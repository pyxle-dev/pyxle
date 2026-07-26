/* Pyxle Studio — dependency-free ES module.
   Talks to the JSON API under /__pyxle/studio/api and the SSE stream at
   /__pyxle/studio/events. No framework, no build step: everything below is
   hand-rolled DOM construction over a tiny element helper. */

const API = "/__pyxle/studio/api";
const EVENTS_URL = "/__pyxle/studio/events";
const TABS = ["routes", "tester", "requests", "metrics", "config", "check"];
const REQUEST_ROWS_MAX = 200;

/* ------------------------------------------------------------------ state */

const state = {
  bootstrap: null,
  routes: null, // {pages, apis}
  requests: [],
  metricsTimer: null,
  eventSource: null,
  tester: { kind: null, page: null, action: null, schema: null },
  editor: localStorage.getItem("pyxle-studio-editor") || "vscode",
};

/* ---------------------------------------------------------------- helpers */

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2), value);
    } else if (key === "text") el.textContent = value;
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

function readCookie(name) {
  for (const part of document.cookie.split("; ")) {
    const eq = part.indexOf("=");
    if (eq > 0 && part.slice(0, eq) === name) {
      return decodeURIComponent(part.slice(eq + 1));
    }
  }
  return null;
}

function csrfHeaders() {
  const csrf = state.bootstrap?.csrf;
  if (!csrf?.enabled || !csrf.cookieName || !csrf.headerName) return {};
  const token = readCookie(csrf.cookieName);
  return token ? { [csrf.headerName]: token } : {};
}

async function getJSON(url) {
  const response = await fetch(url, { headers: { accept: "application/json" } });
  return response.json();
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
      ...csrfHeaders(),
    },
    body: JSON.stringify(body ?? {}),
  });
  const payload = await response.json().catch(() => ({}));
  return { status: response.status, headers: response.headers, payload };
}

function toast(message, { error = false, detail = null, ttl = 4200 } = {}) {
  const box = document.getElementById("toasts");
  const node = h(
    "div",
    { class: `toast${error ? " error" : ""}` },
    message,
    detail ? h("div", { class: "toast-detail", text: detail }) : null
  );
  box.append(node);
  setTimeout(() => node.remove(), ttl);
}

function statusClass(status) {
  if (status >= 500) return "status-5xx";
  if (status >= 400) return "status-4xx";
  if (status >= 300) return "status-3xx";
  return "status-2xx";
}

function fmtMs(value) {
  if (value == null) return "—";
  return value >= 100 ? `${Math.round(value)} ms` : `${value.toFixed(1)} ms`;
}

function fmtUptime(seconds) {
  if (seconds == null) return "—";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/* -------------------------------------------------------- open in editor */

function editorLink(absolutePath, line) {
  const suffix = line ? `:${line}` : "";
  if (state.editor === "copy") return null;
  const scheme = state.editor === "cursor" ? "cursor" : "vscode";
  return `${scheme}://file/${absolutePath}${suffix}`;
}

function sourceLink(label, absolutePath, line) {
  const href = editorLink(absolutePath, line);
  const title = state.editor === "copy" ? "Click to copy path" : `Open in ${state.editor === "cursor" ? "Cursor" : "VS Code"}`;
  return h(
    "a",
    {
      class: "src-link",
      href: href || "#",
      title,
      onclick(event) {
        if (href) return; // let the protocol handler take it
        event.preventDefault();
        navigator.clipboard?.writeText(line ? `${absolutePath}:${line}` : absolutePath);
        toast("Path copied");
      },
    },
    label
  );
}

/* -------------------------------------------------------------- rendering */

function panel() {
  return document.getElementById("panel");
}

function setPanel(...children) {
  const target = panel();
  target.replaceChildren(...children.flat().filter(Boolean));
}

function currentTab() {
  const raw = location.hash.replace(/^#\//, "");
  return TABS.includes(raw) ? raw : "routes";
}

function render() {
  const tab = currentTab();
  for (const link of document.querySelectorAll(".tabs a")) {
    link.classList.toggle("active", link.dataset.tab === tab);
  }
  stopMetricsPolling();
  const views = {
    routes: renderRoutes,
    tester: renderTester,
    requests: renderRequests,
    metrics: renderMetrics,
    config: renderConfig,
    check: renderCheck,
  };
  views[tab]();
}

/* ----------------------------------------------------------------- routes */

function loaderBadge(page) {
  if (!page.loader) return null;
  return h("span", { class: "badge badge-loader", title: `@server loader (line ${page.loader.line ?? "?"})` }, `ƒ ${page.loader.name}`);
}

function renderRoutes() {
  if (!state.routes) {
    setPanel(h("div", { class: "empty" }, "Loading routes…"));
    return;
  }
  const { pages, apis } = state.routes;

  const pageRows = pages.map((page) =>
    h(
      "tr",
      {},
      h("td", { class: "mono route-path" }, page.path),
      h("td", {}, sourceLink(page.source, page.sourceAbsolute, page.loader?.line)),
      h(
        "td",
        {},
        loaderBadge(page),
        page.actions.map((action) =>
          h("span", { class: "badge badge-action", title: `@action (line ${action.line ?? "?"})` }, `⚡ ${action.name}`)
        ),
        page.websocket ? h("span", { class: "badge badge-ws" }, `⇄ ${page.websocket.name}`) : null,
        page.usesSuspense ? h("span", { class: "badge" }, "suspense") : null
      ),
      h(
        "td",
        {},
        page.cache.revalidate != null
          ? h("span", { class: "badge badge-cache" }, `revalidate ${page.cache.revalidate}s`)
          : null,
        page.cache.edgeMaxAge != null
          ? h("span", { class: "badge badge-cache" }, `edge ${page.cache.edgeMaxAge}s`)
          : null,
        page.layouts.length
          ? h("span", { class: "badge", title: page.layouts.map((l) => l.source).join(", ") }, `${page.layouts.length} layout loader${page.layouts.length > 1 ? "s" : ""}`)
          : null,
        page.boundaries.error ? h("span", { class: "badge", title: page.boundaries.error }, "error.pyxl") : null,
        page.boundaries.loading ? h("span", { class: "badge", title: page.boundaries.loading }, "loading.pyxl") : null
      )
    )
  );

  const apiRows = apis.map((api) =>
    h(
      "tr",
      {},
      h("td", { class: "mono route-path" }, api.path),
      h("td", {}, sourceLink(api.source, api.sourceAbsolute)),
      h("td", {}, h("span", { class: "badge" }, "api module")),
      h("td", {})
    )
  );

  setPanel(
    h("h2", { class: "panel-title" }, "Routes"),
    h("p", { class: "panel-sub" }, `${pages.length} page route${pages.length === 1 ? "" : "s"}, ${apis.length} API route${apis.length === 1 ? "" : "s"}. Click a source file to open it in your editor.`),
    h(
      "table",
      { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", {}, "Route"), h("th", {}, "Source"), h("th", {}, "Server code"), h("th", {}, "Caching / layout"))),
      h("tbody", {}, pageRows, apiRows)
    )
  );
}

/* ----------------------------------------------------------------- tester */

function testerTargets() {
  const targets = [];
  for (const page of state.routes?.pages ?? []) {
    if (page.loader) targets.push({ kind: "loader", page });
    for (const action of page.actions) {
      targets.push({ kind: "action", page, action });
    }
  }
  return targets;
}

function renderTester() {
  if (!state.routes) {
    setPanel(h("div", { class: "empty" }, "Loading routes…"));
    return;
  }
  const targets = testerTargets();
  if (!targets.length) {
    setPanel(
      h("h2", { class: "panel-title" }, "Tester"),
      h("div", { class: "empty" }, "No loaders or actions found. Add a @server loader or an @action to a page and it will show up here.")
    );
    return;
  }

  const selected = state.tester.kind ? state.tester : null;
  const list = h(
    "div",
    { class: "tester-list" },
    targets.map((target) => {
      const label = target.kind === "loader" ? `${target.page.path}` : `${target.page.path} · ${target.action.name}`;
      const active =
        selected &&
        selected.kind === target.kind &&
        selected.page?.path === target.page.path &&
        (target.kind === "loader" || selected.action?.name === target.action.name);
      return h(
        "button",
        {
          class: `tester-item${active ? " active" : ""}`,
          onclick() {
            state.tester = { kind: target.kind, page: target.page, action: target.action ?? null, schema: null };
            renderTester();
            if (target.kind === "action") loadActionSchema();
          },
        },
        label,
        h("span", { class: "tester-item-kind" }, target.kind)
      );
    })
  );

  const detail = h("div", { class: "tester-detail" });
  if (!selected) {
    detail.append(h("div", { class: "empty" }, "Pick a loader or action on the left."));
  } else if (selected.kind === "loader") {
    detail.append(loaderForm(selected.page));
  } else {
    detail.append(actionForm(selected.page, selected.action));
  }

  setPanel(
    h("h2", { class: "panel-title" }, "Tester"),
    h("p", { class: "panel-sub" }, "Loaders run in-process with a synthetic GET request. Actions go through the real HTTP endpoint — CSRF, validation, auth hooks and all."),
    h("div", { class: "tester-grid" }, list, detail)
  );
}

function pathParamNames(pattern) {
  const names = [];
  for (const match of pattern.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)(?::[a-z]+)?\}/g)) {
    names.push(match[1]);
  }
  return names;
}

function loaderForm(page) {
  const params = pathParamNames(page.path);
  const paramInputs = new Map();
  const form = h("form", {
    onsubmit(event) {
      event.preventDefault();
      run();
    },
  });

  form.append(h("div", { class: "field" }, h("label", {}, "Route"), h("input", { value: page.path, readonly: "" })));
  for (const name of params) {
    const input = h("input", { placeholder: `value for {${name}}` });
    paramInputs.set(name, input);
    form.append(h("div", { class: "field" }, h("label", {}, `Path param · ${name}`), input));
  }
  const queryInput = h("textarea", { placeholder: '{"page": "2"}' });
  form.append(
    h(
      "div",
      { class: "field" },
      h("label", {}, "Query params (JSON object)"),
      queryInput,
      h("div", { class: "field-hint" }, "Sent as the request's query string. Leave empty for none.")
    )
  );

  const resultBox = h("div", { class: "result-box", hidden: "" });
  const runButton = h("button", { class: "btn", type: "submit" }, "Run loader");
  form.append(runButton, resultBox);

  async function run() {
    let query = {};
    if (queryInput.value.trim()) {
      try {
        query = JSON.parse(queryInput.value);
      } catch {
        toast("Query params must be valid JSON", { error: true });
        return;
      }
    }
    const paramsPayload = {};
    for (const [name, input] of paramInputs) paramsPayload[name] = input.value;
    runButton.disabled = true;
    try {
      const { payload } = await postJSON(`${API}/run-loader`, { path: page.path, params: paramsPayload, query });
      resultBox.hidden = false;
      resultBox.replaceChildren(
        h(
          "div",
          { class: "result-meta" },
          h("span", { class: payload.ok ? "ok" : "fail" }, payload.ok ? "OK" : payload.kind || "failed"),
          payload.durationMs != null ? h("span", {}, fmtMs(payload.durationMs)) : null,
          payload.status ? h("span", {}, `status ${payload.status}`) : null,
          payload.note ? h("span", {}, payload.note) : null
        ),
        h("pre", { class: "code" }, JSON.stringify(payload.ok ? payload.data : payload.error, null, 2) ?? "null")
      );
    } finally {
      runButton.disabled = false;
    }
  }

  return h(
    "div",
    {},
    h("h3", { style: "margin:0 0 12px;font-size:14px" }, `Loader · `, h("span", { class: "mono" }, page.loader.name), " ", sourceLink(page.source, page.sourceAbsolute, page.loader.line)),
    form
  );
}

async function loadActionSchema() {
  const { page, action } = state.tester;
  const query = new URLSearchParams({ path: page.path, name: action.name });
  const payload = await getJSON(`${API}/action-schema?${query}`);
  state.tester.schema = payload;
  renderTester();
}

function schemaFields(schema) {
  // Flat object schemas become individual fields; anything deeper falls back
  // to the raw JSON editor.
  if (!schema || schema.type !== "object" || !schema.properties) return null;
  const fields = [];
  const required = new Set(schema.required ?? []);
  for (const [name, spec] of Object.entries(schema.properties)) {
    const type = spec.type;
    if (spec.enum || type === "string" || type === "number" || type === "integer" || type === "boolean") {
      fields.push({ name, spec, required: required.has(name) });
    } else {
      return null; // nested/complex — raw JSON mode
    }
  }
  return fields;
}

function actionForm(page, action) {
  const schemaPayload = state.tester.schema;
  const container = h("div", {});
  container.append(
    h(
      "h3",
      { style: "margin:0 0 4px;font-size:14px" },
      "Action · ",
      h("span", { class: "mono" }, action.name),
      " ",
      sourceLink(page.source, page.sourceAbsolute, action.line)
    )
  );

  if (!schemaPayload) {
    container.append(h("div", { class: "empty" }, "Loading schema…"));
    return container;
  }
  if (!schemaPayload.ok) {
    container.append(h("div", { class: "field-error" }, schemaPayload.error));
    return container;
  }

  container.append(h("p", { class: "panel-sub", style: "margin-bottom:12px" }, "POST ", h("span", { class: "mono" }, schemaPayload.url)));
  if (schemaPayload.note) container.append(h("p", { class: "field-hint" }, schemaPayload.note));

  const fields = schemaFields(schemaPayload.schema);
  const inputs = new Map();
  const rawInput = h("textarea", { placeholder: "{}" });
  const form = h("form", {
    onsubmit(event) {
      event.preventDefault();
      run();
    },
  });

  if (fields) {
    for (const field of fields) {
      let input;
      if (field.spec.enum) {
        input = h("select", {}, field.spec.enum.map((option) => h("option", { value: String(option) }, String(option))));
      } else if (field.spec.type === "boolean") {
        input = h("select", {}, h("option", { value: "true" }, "true"), h("option", { value: "false" }, "false"));
      } else {
        input = h("input", {
          placeholder: field.spec.type,
          type: field.spec.type === "string" ? "text" : "number",
          step: field.spec.type === "number" ? "any" : null,
        });
      }
      inputs.set(field.name, { input, field });
      form.append(
        h(
          "div",
          { class: "field" },
          h("label", {}, `${field.name}${field.required ? " *" : ""}`),
          input,
          field.spec.description ? h("div", { class: "field-hint" }, field.spec.description) : null
        )
      );
    }
  } else {
    form.append(
      h(
        "div",
        { class: "field" },
        h("label", {}, schemaPayload.schema ? "Request body (JSON)" : "Request body (JSON, optional)"),
        rawInput,
        schemaPayload.schema
          ? h("div", { class: "field-hint" }, "This action's model is nested — edit the JSON directly.")
          : null
      )
    );
  }

  const resultBox = h("div", { class: "result-box", hidden: "" });
  const runButton = h("button", { class: "btn", type: "submit" }, "Send POST");
  form.append(runButton, resultBox);
  container.append(form);

  async function run() {
    let body = {};
    if (fields) {
      for (const [name, { input, field }] of inputs) {
        const raw = input.value;
        if (raw === "" && !field.required) continue;
        if (field.spec.type === "number" || field.spec.type === "integer") body[name] = Number(raw);
        else if (field.spec.type === "boolean") body[name] = raw === "true";
        else body[name] = raw;
      }
    } else if (rawInput.value.trim()) {
      try {
        body = JSON.parse(rawInput.value);
      } catch {
        toast("Body must be valid JSON", { error: true });
        return;
      }
    }
    runButton.disabled = true;
    try {
      const { status, headers, payload } = await postJSON(schemaPayload.url, body);
      const invalidate = headers.get("x-pyxle-invalidate");
      resultBox.hidden = false;
      resultBox.replaceChildren(
        h(
          "div",
          { class: "result-meta" },
          h("span", { class: payload.ok ? "ok" : "fail" }, `HTTP ${status}`),
          invalidate ? h("span", {}, `invalidates: ${invalidate}`) : null
        ),
        h("pre", { class: "code" }, JSON.stringify(payload, null, 2))
      );
    } finally {
      runButton.disabled = false;
    }
  }

  return container;
}

/* --------------------------------------------------------------- requests */

function requestRow(entry) {
  return h(
    "tr",
    {},
    h("td", { class: "method" }, entry.method),
    h("td", { class: "mono" }, entry.path),
    h("td", { class: `num ${statusClass(entry.status)}` }, entry.status),
    h("td", { class: "num" }, fmtMs(entry.durationMs)),
    h("td", { class: "mono", style: "color:var(--text-faint);font-size:11px" }, entry.routeTarget ?? ""),
    h("td", { class: "mono", style: "color:var(--text-faint);font-size:11px" }, entry.requestId ? entry.requestId.slice(0, 8) : "")
  );
}

function renderRequests() {
  const rows = [...state.requests].reverse().map(requestRow);
  setPanel(
    h("h2", { class: "panel-title" }, "Requests"),
    h("p", { class: "panel-sub" }, "Live feed of requests hitting the dev server, newest first. Studio's own traffic is excluded."),
    rows.length
      ? h(
          "table",
          { class: "tbl tbl-keep-cols", id: "requests-table" },
          h("thead", {}, h("tr", {}, h("th", {}, "Method"), h("th", {}, "Path"), h("th", {}, "Status"), h("th", {}, "Time"), h("th", {}, "Target"), h("th", {}, "Request id"))),
          h("tbody", { id: "requests-tbody" }, rows)
        )
      : h("div", { class: "empty" }, "No requests observed yet — hit any page of your app.")
  );
}

function onRequestEvent(entry) {
  state.requests.push(entry);
  if (state.requests.length > REQUEST_ROWS_MAX) state.requests.shift();
  if (currentTab() !== "requests") return;
  const tbody = document.getElementById("requests-tbody");
  if (!tbody) {
    renderRequests();
    return;
  }
  tbody.prepend(requestRow(entry));
  while (tbody.children.length > REQUEST_ROWS_MAX) tbody.lastChild.remove();
}

/* ---------------------------------------------------------------- metrics */

function histogram(title, buckets, total) {
  // Convert cumulative Prometheus-style buckets into per-bucket counts.
  const rows = [];
  let previous = 0;
  for (const [bound, cumulative] of buckets) {
    rows.push({ label: bound === Infinity || bound == null ? "+∞" : `≤${bound}ms`, count: cumulative - previous });
    previous = cumulative;
  }
  const max = Math.max(1, ...rows.map((row) => row.count));
  return h(
    "div",
    { class: "hist" },
    h("h3", {}, `${title} · ${total ?? previous}`),
    rows.map((row) =>
      h(
        "div",
        { class: "hist-bar" },
        h("span", { class: "hb-label" }, row.label),
        h("div", { class: "hb-track" }, h("div", { class: "hb-fill", style: `width:${(row.count / max) * 100}%` })),
        h("span", { class: "hb-count" }, row.count || "")
      )
    )
  );
}

async function refreshMetrics() {
  const payload = await getJSON(`${API}/metrics`).catch(() => null);
  if (!payload?.ok || currentTab() !== "metrics") return;
  const snap = payload.snapshot;
  const byStatus = snap.requests_by_status ?? {};
  setPanel(
    h("h2", { class: "panel-title" }, "Metrics"),
    h("p", { class: "panel-sub" }, "Per-process aggregates from the observability registry — the same numbers as the terminal dashboard and the Prometheus endpoint."),
    h(
      "div",
      { class: "stat-row" },
      stat("Requests", snap.requests_total ?? 0),
      stat("Uptime", fmtUptime(payload.uptimeSeconds)),
      stat("Avg request", fmtMs(snap.request_duration?.avg_ms)),
      stat("Avg SSR render", fmtMs(snap.render_duration?.avg_ms)),
      stat("Cache hit ratio", snap.cache ? `${Math.round((snap.cache.hit_ratio ?? 0) * 100)}%` : "—"),
      stat("Errors (5xx)", byStatus["5xx"] ?? 0)
    ),
    h(
      "div",
      { class: "hist-grid" },
      histogram("Request latency", payload.buckets.request, snap.request_duration?.count),
      histogram("SSR render", payload.buckets.render, snap.render_duration?.count),
      histogram("Loaders", payload.buckets.loader, snap.loader_duration?.count),
      histogram("Actions", payload.buckets.action, snap.action_duration?.count)
    )
  );

  function stat(label, value) {
    return h("div", { class: "stat" }, h("div", { class: "stat-label" }, label), h("div", { class: "stat-value" }, String(value)));
  }
}

function renderMetrics() {
  setPanel(h("div", { class: "empty" }, "Loading metrics…"));
  refreshMetrics();
  state.metricsTimer = setInterval(refreshMetrics, 2000);
}

function stopMetricsPolling() {
  if (state.metricsTimer) {
    clearInterval(state.metricsTimer);
    state.metricsTimer = null;
  }
}

/* ----------------------------------------------------------------- config */

async function renderConfig() {
  setPanel(h("div", { class: "empty" }, "Loading config…"));
  const payload = await getJSON(`${API}/config`).catch(() => null);
  if (currentTab() !== "config") return;
  if (!payload?.ok) {
    setPanel(h("div", { class: "empty" }, "Could not load configuration."));
    return;
  }
  const sections = [
    ["Server settings", payload.settings],
    ...Object.entries(payload.blocks ?? {}).map(([name, block]) => [`${name} block`, block]),
    ["Plugins", payload.plugins],
  ];
  setPanel(
    h("h2", { class: "panel-title" }, "Config"),
    h("p", { class: "panel-sub" }, "The effective, resolved configuration this server is running with. Secret-shaped values are redacted."),
    sections.map(([title, value]) =>
      h(
        "div",
        { class: "config-section" },
        h("h3", {}, title),
        h("pre", { class: "code" }, value == null ? "not configured (framework defaults)" : JSON.stringify(value, null, 2))
      )
    )
  );
}

/* ------------------------------------------------------------------ check */

function renderCheck() {
  const summary = h("span", { class: "check-summary" });
  const results = h("div", {});
  const runButton = h(
    "button",
    {
      class: "btn",
      async onclick() {
        runButton.disabled = true;
        summary.textContent = "Checking…";
        results.replaceChildren();
        try {
          const { payload } = await postJSON(`${API}/check`, {});
          if (!payload.ok) {
            summary.textContent = "Check failed to run.";
            return;
          }
          const count = payload.diagnostics.length;
          summary.textContent = `${payload.filesChecked} file${payload.filesChecked === 1 ? "" : "s"} · ${fmtMs(payload.durationMs)}${payload.jsxValidated ? "" : " · JSX validation skipped (Node not found)"}`;
          if (!count) {
            results.replaceChildren(h("div", { class: "check-clean" }, "✓ No problems found — same checks as `pyxle check`."));
            return;
          }
          results.replaceChildren(
            payload.diagnostics.map((diag) =>
              h(
                "div",
                { class: `diag ${diag.severity}` },
                h(
                  "div",
                  { class: "diag-file" },
                  sourceLink(diag.line ? `${diag.file}:${diag.line}` : diag.file, diag.fileAbsolute, diag.line),
                  h("span", { class: "diag-section" }, `[${diag.section}]`)
                ),
                h("div", { class: "diag-msg" }, diag.message)
              )
            )
          );
        } finally {
          runButton.disabled = false;
        }
      },
    },
    "Run check"
  );

  setPanel(
    h("h2", { class: "panel-title" }, "Check"),
    h("p", { class: "panel-sub" }, "Runs the same diagnostics as `pyxle check`: tolerant parse, JSX syntax via Babel, and Python semantics via pyflakes."),
    h("div", { class: "check-head" }, runButton, summary),
    results
  );
}

/* -------------------------------------------------------------------- SSE */

function setFeedState(mode, label) {
  const dot = document.getElementById("feed-dot");
  const text = document.getElementById("feed-label");
  dot.className = `feed-dot ${mode}`;
  dot.title = label;
  text.textContent = label;
}

function connectEvents() {
  const source = new EventSource(EVENTS_URL);
  state.eventSource = source;
  source.onopen = () => setFeedState("live", "live");
  source.onerror = () => setFeedState("", "reconnecting…");
  source.onmessage = (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }
    if (event.type === "request") onRequestEvent(event.payload);
    else if (event.type === "rebuild") onRebuildEvent(event.payload);
  };
}

async function onRebuildEvent(payload) {
  if (payload.ok) {
    const changed = payload.compiledPages?.length ? payload.compiledPages.join(", ") : payload.changedPaths?.join(", ");
    toast(`Rebuilt in ${payload.elapsedSeconds}s`, { detail: changed || null });
    await loadRoutes();
    if (currentTab() === "routes") renderRoutes();
    if (currentTab() === "tester") renderTester();
  } else {
    toast("Rebuild failed", { error: true, detail: payload.error, ttl: 8000 });
  }
  setFeedState("live", "live");
}

/* ------------------------------------------------------------------- boot */

async function loadRoutes() {
  const payload = await getJSON(`${API}/routes`).catch(() => null);
  if (payload?.ok) state.routes = payload;
}

async function boot() {
  const editorSelect = document.getElementById("editor-select");
  editorSelect.value = state.editor;
  editorSelect.addEventListener("change", () => {
    state.editor = editorSelect.value;
    localStorage.setItem("pyxle-studio-editor", state.editor);
    render();
  });

  window.addEventListener("hashchange", render);

  const bootstrap = await getJSON(`${API}/bootstrap`).catch(() => null);
  if (!bootstrap?.ok) {
    setPanel(h("div", { class: "empty" }, "Studio could not reach its API. Is the dev server still running?"));
    return;
  }
  state.bootstrap = bootstrap;
  const projectChip = document.getElementById("project-chip");
  projectChip.textContent = bootstrap.project;
  projectChip.hidden = false;
  const versionChip = document.getElementById("version-chip");
  versionChip.textContent = `pyxle ${bootstrap.version}`;
  versionChip.hidden = false;

  const requests = await getJSON(`${API}/requests`).catch(() => null);
  if (requests?.ok) state.requests = requests.requests;

  await loadRoutes();
  connectEvents();
  render();
}

boot();
