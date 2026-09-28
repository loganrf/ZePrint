/* ZePrint web UI - plain JS, no build step. All URLs are relative so the UI
   also works behind a reverse proxy sub-path. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid);
  return n;
};

const state = { labels: [], printers: [], settings: null, info: null, current: null, size: null };

/* ---------------------------------------------------------------- api */

function token() { try { return localStorage.getItem("zeprint-token") || ""; } catch { return ""; } }
function setToken(t) { try { localStorage.setItem("zeprint-token", t); } catch { /* private mode */ } }

async function api(path, { method = "GET", json, body, raw = false } = {}) {
  const headers = {};
  if (token()) headers.Authorization = `Bearer ${token()}`;
  if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  const res = await fetch(`api/${path}`, { method, headers, body });
  if (res.status === 401) {
    await askToken();
    return api(path, { method, json, body, raw });
  }
  if (raw) {
    if (!res.ok) throw new Error(await errorText(res));
    return res;
  }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok && !(res.status === 502 && data && data.status === "error")) {
    throw new Error(detail(data) || `${res.status} ${res.statusText}`);
  }
  return data;
}

function detail(data) {
  if (!data) return "";
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map(d => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
  return "";
}
async function errorText(res) {
  try { return detail(await res.json()) || res.statusText; } catch { return res.statusText; }
}

function askToken() {
  const dlg = $("#token-dialog");
  return new Promise((resolve, reject) => {
    let submitted = false;
    $("#token-dialog-form").onsubmit = () => { submitted = true; setToken($("#token-dialog-input").value.trim()); };
    // fires for submit and for Esc; Esc must not leave the caller hanging
    dlg.onclose = () => (submitted ? resolve() : reject(new Error("an API token is required")));
    if (!dlg.open) dlg.showModal();
  });
}

function toast(msg, kind = "") {
  const t = el("div", { class: `toast ${kind}`, text: msg });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 9000 : 4500);
}

async function busy(btn, fn) {
  btn.disabled = true;
  try { return await fn(); } catch (e) { toast(e.message, "bad"); } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------- views */

function show(view) {
  $$(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "jobs") refreshJobs();
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
}

/* -------------------------------------------------------- label forms */

function resolveProp(prop) {
  if (prop.anyOf) {
    const real = prop.anyOf.find(p => p.type !== "null") || {};
    return { ...prop, ...real, nullable: prop.anyOf.some(p => p.type === "null") };
  }
  return { ...prop, nullable: false };
}

// JSON-schema regex -> HTML pattern attribute (compiled with the "v" flag; skip if it won't)
function htmlPattern(re) {
  if (!re) return null;
  const pat = re.replace(/^\^/, "").replace(/\$$/, "");
  try { new RegExp(pat, "v"); return pat; } catch { return null; }
}

function paramField(name, raw, value) {
  const p = resolveProp(raw);
  const title = p.title || name;
  const help = p.description ? el("span", { class: "field-help", text: p.description }) : null;
  let input;
  if (p.enum) {
    input = el("select", { name }, p.nullable ? el("option", { value: "", text: "(default)" }) : null,
      p.enum.map(v => el("option", { value: v, text: v })));
    input.value = value ?? "";
  } else if (p.type === "boolean") {
    input = el("input", { type: "checkbox", name });
    input.checked = !!value;
    return el("label", { class: "check" }, input, title, help);
  } else if (p.type === "integer" || p.type === "number") {
    input = el("input", {
      type: "number", name, step: p.type === "integer" ? "1" : "any",
      min: p.minimum ?? p.exclusiveMinimum, max: p.maximum ?? p.exclusiveMaximum,
      placeholder: p.nullable ? "auto" : "",
    });
    input.value = value ?? "";
  } else {
    input = el("input", {
      type: p.format === "date" ? "date" : "text", name,
      maxlength: p.maxLength, pattern: htmlPattern(p.pattern),
      placeholder: p.nullable ? (p.format === "date" ? "" : "optional") : " ",
    });
    input.value = value ?? "";
  }
  input.dataset.kind = p.type || "string";
  input.dataset.nullable = p.nullable ? "1" : "";
  return el("label", {}, title, input, help);
}

function collectParams() {
  const out = {};
  $$("#lf-params [name]").forEach(inp => {
    const kind = inp.dataset.kind;
    if (inp.type === "checkbox") { out[inp.name] = inp.checked; return; }
    const v = inp.value.trim();
    if (v === "") { if (inp.dataset.nullable) out[inp.name] = null; return; }
    out[inp.name] = kind === "integer" ? parseInt(v, 10) : kind === "number" ? parseFloat(v) : v;
  });
  return out;
}

function selectLabel(id) {
  const label = state.labels.find(l => l.id === id) || state.labels[0];
  if (!label) return;
  state.current = label;
  $$(".label-item").forEach(b => b.classList.toggle("active", b.dataset.id === label.id));
  $("#label-title").textContent = label.name;
  $("#label-desc").textContent = label.description;
  renderParams(label.effective_defaults || {});
  renderSizes();
  clearPreview();
  try { localStorage.setItem("zeprint-label", label.id); } catch { /* ignore */ }
}

function renderParams(values) {
  const props = state.current.schema.properties || {};
  $("#lf-params").replaceChildren(...Object.entries(props).map(([n, p]) => paramField(n, p, values[n])));
}

function currentPrinter() {
  const id = $("#lf-printer").value;
  return state.printers.find(p => p.id === id) || null;
}

function renderSizes() {
  const label = state.current;
  if (!label) return;
  const p = currentPrinter();
  const preferred = p ? p.label_size : "4x6";
  if (!state.size || !label.sizes.includes(state.size)) {
    state.size = label.sizes.includes(preferred) ? preferred : label.sizes[0];
  }
  $("#lf-size").replaceChildren(...state.info.sizes.map(s => el("button", {
    type: "button", class: s.id === state.size ? "on" : "", disabled: !label.sizes.includes(s.id),
    title: s.description, text: s.id,
    onclick: () => { state.size = s.id; renderSizes(); },
  })));
}

function labelRequest() {
  return {
    params: collectParams(),
    printer: $("#lf-printer").value || null,
    size: state.size,
    copies: parseInt($("#lf-copies").value, 10) || 1,
  };
}

function clearPreview() {
  $("#preview-box").replaceChildren(el("p", { class: "muted", text: "Preview renders here - nothing is printed." }));
  $("#preview-meta").textContent = "";
  $("#report-box").hidden = $("#data-box").hidden = true;
}

async function previewLabel() {
  const box = $("#preview-box");
  box.classList.add("loading");
  try {
    const req = labelRequest();
    const r = await api(`labels/${state.current.id}/render?preview=true`, { method: "POST", json: req });
    const p = currentPrinter();
    box.replaceChildren(el("img", { src: `data:image/png;base64,${r.preview_png}`, alt: `${state.current.name} preview` }));
    $("#preview-meta").textContent = `${req.size} · ${p ? p.dpi : 300} dpi · ${Math.round(r.zpl.length / 1024)} KiB ZPL`;
    $("#report-box").hidden = !r.report;
    $("#report").textContent = r.report || "";
    const hasData = r.data && Object.keys(r.data).length;
    $("#data-box").hidden = !hasData;
    $("#data").textContent = hasData ? JSON.stringify(r.data, null, 2) : "";
  } finally {
    box.classList.remove("loading");
  }
}

async function printLabel() {
  const job = await api(`labels/${state.current.id}/print`, { method: "POST", json: labelRequest() });
  toast(`Queued ${job.title} on ${job.printer}`);
  followJob(job.id);
}

async function followJob(id) {
  for (let i = 0; i < 120; i++) {
    const job = await api(`jobs/${id}?wait=5`).catch(() => null);
    if (!job) return;
    if (job.status === "done") { toast(`Printed ${job.title} (${Math.round(job.bytes / 1024)} KiB)`, "ok"); refreshJobs(); return; }
    if (job.status === "error") { toast(`${job.title} failed: ${job.error}`, "bad"); refreshJobs(); return; }
  }
}

/* ----------------------------------------------------------- printers */

function printerOptions(select) {
  const keep = select.value;
  select.replaceChildren(
    ...state.printers.map(p => el("option", { value: p.id, text: p.default ? `${p.name} (default)` : p.name })));
  if (!state.printers.length) select.append(el("option", { value: "", text: "no printer configured" }));
  const def = state.printers.find(p => p.default);
  select.value = state.printers.some(p => p.id === keep) ? keep : (def ? def.id : "");
}

function renderPrinters() {
  printerOptions($("#lf-printer"));
  printerOptions($("#zpl-printer"));
  printerOptions($("#set-default"));
  const def = state.printers.find(p => p.default);
  $("#default-printer").replaceChildren(
    el("span", { class: "dot", id: "default-dot" }),
    def ? `${def.name} · ${def.label_size} · ${def.dpi} dpi` : "no printer configured");

  const list = $("#printer-list");
  if (!state.printers.length) {
    list.replaceChildren(el("div", { class: "panel empty" },
      el("p", { text: "No printers yet." }),
      el("p", { class: "muted", text: "Add your Zebra by network address (tcp://ip:9100), USB device (usb:///dev/usb/lp0) or a CUPS queue (ipp://host:631/printers/NAME)." })));
    return;
  }
  list.replaceChildren(...state.printers.map(p => {
    const status = el("p", { class: "status-line muted" }, el("span", { class: "dot" }), "status not checked");
    const card = el("div", { class: "panel printer-card" },
      el("h3", {}, p.name, p.default ? el("span", { class: "badge default", text: "default" }) : null),
      el("dl", { class: "facts" },
        el("dt", { text: "ID" }), el("dd", {}, el("code", { text: p.id })),
        el("dt", { text: "Connection" }), el("dd", {}, el("code", { text: p.uri })),
        el("dt", { text: "Labels" }), el("dd", { text: `${p.label_size} · ${p.dpi} dpi · ${p.media === "ribbon" ? "ribbon" : "direct thermal"}` }),
        el("dt", { text: "Darkness / speed" }), el("dd", { text: `${p.darkness} / ${p.speed} ips` })),
      status,
      el("div", { class: "actions" },
        el("button", { class: "secondary", text: "Status", onclick: e => busy(e.target, () => checkStatus(p, status)) }),
        el("button", { class: "secondary", text: "Test label", onclick: e => busy(e.target, async () => {
          const job = await api(`printers/${p.id}/test`, { method: "POST" }); toast(`Test label queued on ${p.name}`); followJob(job.id);
        }) }),
        el("button", { class: "secondary", text: "Calibrate", title: "Save media size and run gap calibration (feeds a few labels)", onclick: e => busy(e.target, async () => {
          if (!confirm(`Calibrate ${p.name} for ${p.label_size} labels? It will feed a few blank labels.`)) return;
          const job = await api(`printers/${p.id}/calibrate`, { method: "POST" }); followJob(job.id);
        }) }),
        el("span", { class: "spacer" }),
        p.default ? null : el("button", { class: "ghost", text: "Make default", onclick: e => busy(e.target, async () => {
          await api("settings", { method: "PATCH", json: { default_printer: p.id } }); await loadPrinters();
        }) }),
        el("button", { class: "ghost", text: "Edit", onclick: () => openPrinterDialog(p) }),
        el("button", { class: "danger", text: "Delete", onclick: e => busy(e.target, async () => {
          if (!confirm(`Delete printer ${p.name}?`)) return;
          await api(`printers/${p.id}`, { method: "DELETE" }); await loadPrinters();
        }) })));
    return card;
  }));
}

const STATE_TEXT = {
  ready: "Ready", printing: "Printing", paper_out: "Out of labels", head_open: "Head open",
  ribbon_out: "Ribbon out", paused: "Paused", offline: "Offline", buffer_full: "Buffer full",
  over_temperature: "Head too hot", under_temperature: "Head too cold", unknown: "Unknown", error: "Error",
};

async function checkStatus(p, line) {
  const s = await api(`printers/${p.id}/status`);
  const kind = s.state === "ready" || s.state === "printing" ? "ok" : s.online === null ? "" : s.state === "offline" || s.state === "error" ? "bad" : "warn";
  const bits = [STATE_TEXT[s.state] || s.state];
  if (s.identity && s.identity.model) bits.push(`${s.identity.model} (${s.identity.firmware || "?"})`);
  if (s.identity && s.identity.dpi && s.identity.dpi !== p.dpi) bits.push(`reports ${s.identity.dpi} dpi - check the DPI setting`);
  if (s.error) bits.push(s.error);
  line.replaceChildren(el("span", { class: `dot ${kind}` }), bits.join(" · "));
  if (p.default) $("#default-dot").className = `dot ${kind}`;
}

// form.id / form.name are the <form>'s own attributes, so look inputs up explicitly
const field = name => $("#printer-form").elements.namedItem(name);

function openPrinterDialog(p) {
  const f = $("#printer-form");
  f.reset();
  $("#printer-error").hidden = true;
  field("label_size").replaceChildren(...state.info.sizes.map(s => el("option", { value: s.id, text: `${s.id} (${s.description})` })));
  $("#printer-dialog-title").textContent = p ? `Edit ${p.name}` : "Add printer";
  f.dataset.editing = p ? p.id : "";
  const v = p || { name: "", id: state.printers.length ? "" : "zebra", uri: "", dpi: 300, label_size: "4x6", media: "direct", darkness: 22, speed: 2, default: !state.printers.length };
  for (const k of ["name", "id", "uri", "dpi", "label_size", "media", "darkness", "speed"]) field(k).value = v[k];
  field("default").checked = !!v.default;
  $("#darkness-out").textContent = v.darkness;
  $("#printer-dialog").showModal();
}

async function savePrinter(ev) {
  ev.preventDefault();
  const f = $("#printer-form");
  const body = {
    name: field("name").value.trim(), id: field("id").value.trim(), uri: field("uri").value.trim(),
    dpi: parseInt(field("dpi").value, 10), label_size: field("label_size").value, media: field("media").value,
    darkness: parseInt(field("darkness").value, 10), speed: parseInt(field("speed").value, 10),
    default: field("default").checked,
  };
  const editing = f.dataset.editing;
  try {
    await api(editing ? `printers/${editing}` : "printers", { method: editing ? "PATCH" : "POST", json: body });
    $("#printer-dialog").close();
    toast(`Saved ${body.name}`, "ok");
    await loadPrinters();
  } catch (e) {
    $("#printer-error").textContent = e.message;
    $("#printer-error").hidden = false;
  }
}

/* --------------------------------------------------------------- jobs */

let jobsTimer = null;
let jobsLoading = false;
async function refreshJobs() {
  clearTimeout(jobsTimer);
  if (jobsLoading) return;           // one poll loop, however often this is called
  jobsLoading = true;
  const rows = $("#job-rows");
  try {
    const jobs = await api("jobs?limit=50");
    rows.replaceChildren(...(jobs.length ? jobs.map(j => el("tr", {},
      el("td", { text: new Date(j.created).toLocaleString() }),
      el("td", {}, el("strong", { text: j.title || j.label || j.kind }), el("div", { class: "muted", text: j.source })),
      el("td", { text: j.printer }),
      el("td", { text: j.size || "" }),
      el("td", {}, el("span", { class: `st ${j.status}`, text: j.status })),
      el("td", { class: j.error ? "error" : "muted", text: j.error || (j.bytes ? `${Math.round(j.bytes / 1024)} KiB` : "") }),
    )) : [el("tr", {}, el("td", { colspan: 6, class: "empty", text: "No jobs yet." }))]));
  } catch (e) { /* keep last view */ } finally { jobsLoading = false; }
  clearTimeout(jobsTimer);
  if ($("#view-jobs").classList.contains("active")) jobsTimer = setTimeout(refreshJobs, 3000);
}

/* ---------------------------------------------------------------- zpl */

async function zplPreview() {
  const p = state.printers.find(x => x.id === $("#zpl-printer").value);
  const q = p ? `?size=${p.label_size}&dpi=${p.dpi}` : "";
  const res = await api(`zpl/preview${q}`, { method: "POST", body: $("#zpl-text").value, raw: true });
  const url = URL.createObjectURL(await res.blob());
  $("#zpl-preview").replaceChildren(el("img", { src: url, alt: "ZPL preview" }));
}

async function zplSend() {
  const id = $("#zpl-printer").value;
  if (!id) throw new Error("add a printer first");
  const job = await api(`printers/${id}/raw`, { method: "POST", body: $("#zpl-text").value });
  toast(`Sent raw ZPL to ${job.printer}`);
  followJob(job.id);
}

/* ----------------------------------------------------------- settings */

function renderSettings() {
  const s = state.settings, i = state.info;
  $("#set-tz").value = s.timezone || "";
  $("#set-tz-effective").textContent = `In effect: ${s.effective_timezone}`;
  $("#about").replaceChildren(
    el("dt", { text: "Version" }), el("dd", { text: i.version }),
    el("dt", { text: "Labels" }), el("dd", { text: i.labels.join(", ") }),
    el("dt", { text: "Plugins" }), el("dd", { text: i.plugins.length ? i.plugins.join(", ") : "none" }),
    el("dt", { text: "Integrations" }), el("dd", { text: i.integrations.length ? i.integrations.join(", ") : "none (set MQTT_HOST for Home Assistant)" }),
    el("dt", { text: "API auth" }), el("dd", { text: i.auth ? "token required" : "open" }));
  try {
    const zones = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [];
    $("#tz-list").replaceChildren(...zones.map(z => el("option", { value: z })));
  } catch { /* old browser */ }
}

async function saveSettings(ev) {
  ev.preventDefault();
  await busy(ev.submitter || $("#settings-form button"), async () => {
    state.settings = await api("settings", {
      method: "PATCH", json: { default_printer: $("#set-default").value || null, timezone: $("#set-tz").value.trim() },
    });
    toast("Settings saved", "ok");
    await loadPrinters();
    renderSettings();
  });
}

/* ------------------------------------------------------------- boot */

async function loadPrinters() {
  state.printers = await api("printers");
  renderPrinters();
  renderSizes();
}

async function loadLabels() {
  state.labels = await api("labels");
  $("#label-list").replaceChildren(...state.labels.map(l => el("button", {
    class: "label-item", "data-id": l.id, onclick: () => selectLabel(l.id),
  }, el("strong", { text: l.name }), el("span", { text: l.sizes.join(" · ") }))));
  let last = null;
  try { last = localStorage.getItem("zeprint-label"); } catch { /* ignore */ }
  selectLabel(last && state.labels.some(l => l.id === last) ? last : (state.labels.find(l => l.id !== "test") || state.labels[0]).id);
}

async function boot() {
  $$(".tabs button").forEach(b => b.addEventListener("click", () => show(b.dataset.view)));
  $("#label-form").addEventListener("submit", ev => { ev.preventDefault(); busy($("#btn-print"), printLabel); });
  $("#btn-preview").addEventListener("click", ev => busy(ev.target, previewLabel));
  $("#btn-reset").addEventListener("click", () => {
    const props = state.current.schema.properties || {};
    renderParams(Object.fromEntries(Object.entries(props).map(([k, p]) => [k, p.default])));
  });
  $("#btn-save-defaults").addEventListener("click", ev => busy(ev.target, async () => {
    const params = Object.fromEntries(Object.entries(collectParams()).filter(([, v]) => v !== null));
    await api(`labels/${state.current.id}/defaults`, { method: "PUT", json: params });
    toast(`Saved defaults for ${state.current.name}`, "ok");
    const id = state.current.id;
    await loadLabels();
    selectLabel(id);
  }));
  $("#lf-printer").addEventListener("change", () => { state.size = null; renderSizes(); });
  $("#btn-add-printer").addEventListener("click", () => openPrinterDialog(null));
  $("#printer-form").addEventListener("submit", savePrinter);
  $("#printer-cancel").addEventListener("click", () => $("#printer-dialog").close());
  field("darkness").addEventListener("input", e => { $("#darkness-out").textContent = e.target.value; });
  $("#btn-zpl-preview").addEventListener("click", ev => busy(ev.target, zplPreview));
  $("#btn-zpl-send").addEventListener("click", ev => busy(ev.target, zplSend));
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#token-form").addEventListener("submit", ev => { ev.preventDefault(); setToken($("#token-input").value.trim()); toast("Token saved", "ok"); });

  try {
    state.info = await api("info");
    state.settings = await api("settings");
    await loadPrinters();
    await loadLabels();
    renderSettings();
    const def = state.printers.find(p => p.default);
    if (def) checkStatus(def, el("p")).catch(() => {});
  } catch (e) {
    toast(`Could not load: ${e.message}`, "bad");
  }
  const view = location.hash.slice(1);
  show(["labels", "printers", "jobs", "zpl", "settings"].includes(view) ? view : "labels");
}

boot();
