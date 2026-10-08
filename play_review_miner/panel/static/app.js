"use strict";
// Review Miner panel. No dependencies. All dynamic text goes through textContent; the only innerHTML
// is the report body, which the server renders from Markdown with everything HTML-escaped.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const state = { meta: null, endpoints: [], tab: "run", jobId: null, reportName: null, endpointId: null, models: [], poll: null };

// replaceChildren() would print "null" for skipped optional nodes; this drops them
const fill = (node, ...kids) => node.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) n.append(c.nodeType ? c : String(c));
  return n;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Panel": "1" }, credentials: "same-origin" };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const res = await fetch("/api/" + path, init);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && path !== "login") { showLogin(); throw new Error("Oturum gerekli"); }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
}

const STATUS = { queued: "sırada", running: "çalışıyor", done: "bitti", failed: "hata", cancelled: "iptal" };
const fmtTime = (iso) => iso ? new Date(iso).toLocaleString("tr-TR", { dateStyle: "short", timeStyle: "short" }) : "—";
function duration(a, b) {
  if (!a) return "";
  const s = Math.max(0, Math.round(((b ? new Date(b) : new Date()) - new Date(a)) / 1000));
  return s < 60 ? `${s} sn` : `${Math.floor(s / 60)} dk ${s % 60} sn`;
}
function setBar(bar, value, max) {
  const pct = max ? Math.min(100, Math.round((value / max) * 100)) : 0;
  bar.firstChild.style.width = pct + "%";
}
function bar(value, max, cls = "") { const b = el("div", { class: "bar " + cls }, el("span")); setBar(b, value, max); return b; }

// ---- login --------------------------------------------------------------------------------------
function showLogin(hint) {
  $("#app").classList.add("hidden"); $("#login").classList.remove("hidden");
  if (hint) $("#login-hint").textContent = hint;
  $("#login-password").focus();
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("login", { method: "POST", body: { password: $("#login-password").value } });
    $("#login-password").value = "";
    await boot();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", async () => { await api("logout", { method: "POST" }).catch(() => {}); showLogin(); });

// ---- tabs ---------------------------------------------------------------------------------------
$$("#tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
function switchTab(tab) {
  state.tab = tab;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + tab));
  if (tab === "jobs") loadJobs();
  if (tab === "reports") loadReports();
  if (tab === "endpoints") loadEndpoints();
  if (tab === "run") refreshRunEndpoints();
}

// ---- boot ---------------------------------------------------------------------------------------
async function boot() {
  const s = await fetch("/api/session", { credentials: "same-origin" }).then((r) => r.json());
  if (!s.authenticated) {
    return showLogin(s.password_set ? "Panele girmek için parolanızı yazın." :
      "Parola ayarlanmamış. Sunucuda: python -m play_review_miner panel-password");
  }
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  state.meta = await api("meta");
  $("#version").textContent = "v" + state.meta.version;
  if (state.meta.proxy_dashboard) { const a = $("#proxy-link"); a.href = state.meta.proxy_dashboard; a.classList.remove("hidden"); }
  const kind = $("#kind"); kind.replaceChildren(...Object.entries(state.meta.kinds).map(([k, v]) => el("option", { value: k }, v)));
  const cat = $("#category");
  cat.replaceChildren(...state.meta.categories.map((c) => el("option", { value: c, selected: c === "PRODUCTIVITY" }, c)));
  await refreshRunEndpoints();
  updateRunForm();
  startPolling();
}

function startPolling() {
  clearInterval(state.poll);
  state.poll = setInterval(async () => {
    try {
      const jobs = await api("jobs?limit=30");
      const running = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
      const badge = $("#running-badge"); badge.textContent = running; badge.classList.toggle("hidden", !running);
      if (state.tab === "jobs") { renderJobs(jobs); if (state.jobId) loadJob(state.jobId, true); }
    } catch { /* login screen handles 401 */ }
  }, 3000);
}

// ---- run form -----------------------------------------------------------------------------------
const form = $("#run-form");
form.addEventListener("change", updateRunForm);
function updateRunForm() {
  const kind = form.kind.value, llm = form.analyzer.value === "llm";
  $$(".crawl-only", form).forEach((n) => n.classList.toggle("hidden", !(kind === "run" || kind === "crawl")));
  $(".analysis", form).classList.toggle("hidden", kind === "crawl");
  $$(".llm-only", form).forEach((n) => n.classList.toggle("hidden", !llm));
  form.model.required = llm && kind !== "crawl";
}
async function refreshRunEndpoints() {
  state.endpoints = await api("endpoints");
  const sel = $("#run-endpoint"), prev = sel.value;
  sel.replaceChildren(...state.endpoints.map((e) => el("option", { value: e.id }, e.name)));
  if (!state.endpoints.length) sel.append(el("option", { value: "" }, "— önce Uç noktalar sekmesinden ekleyin —"));
  if (prev && state.endpoints.some((e) => String(e.id) === prev)) sel.value = prev;
  await onRunEndpoint();
}
$("#run-endpoint").addEventListener("change", onRunEndpoint);
async function onRunEndpoint() {
  const e = state.endpoints.find((x) => String(x.id) === $("#run-endpoint").value);
  const dl = $("#run-models");
  if (!e) { dl.replaceChildren(); $("#quota-line").textContent = ""; return; }
  const models = await api(`endpoints/${e.id}/models`);
  dl.replaceChildren(...models.map((m) => el("option", { value: m.model_id }, m.source === "manual" ? "manuel" : "")));
  if (!form.model.value || !models.some((m) => m.model_id === form.model.value)) form.model.value = e.default_model || "";
  const q = $("#quota-line");
  if (e.daily_limit) {
    const left = e.daily_limit - e.used_today - e.reserved;
    q.textContent = `Bugünkü kota: ${e.used_today} kullanıldı, ${e.reserved} çalışan işlere ayrıldı, ${Math.max(0, left)} / ${e.daily_limit} kaldı. ` +
      "İstek tavanı boşsa kalan kota kadar kullanılır.";
  } else q.textContent = `${models.length} kayıtlı model · günlük sınır yok`;
}
$("#run-add-model").addEventListener("click", async () => {
  const id = $("#run-endpoint").value, model = form.model.value.trim();
  if (!id || !model) return toast("Önce uç nokta ve model yazın");
  try { await api(`endpoints/${id}/models`, { method: "POST", body: { model_id: model } }); toast("Model kaydedildi"); onRunEndpoint(); }
  catch (err) { toast(err.message); }
});
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#run-error").textContent = "";
  const f = new FormData(form), body = {};
  for (const [k, v] of f.entries()) body[k] = v;
  for (const k of ["exclude_big", "strict_genre", "refresh_apps", "reanalyze"]) body[k] = form[k].checked;
  if (body.analyzer !== "llm") { delete body.endpoint_id; delete body.model; }
  try {
    const job = await api("jobs", { method: "POST", body });
    toast(`İş #${job.id} sıraya alındı`);
    state.jobId = job.id;
    switchTab("jobs");
  } catch (err) { $("#run-error").textContent = err.message; }
});

// ---- jobs ---------------------------------------------------------------------------------------
async function loadJobs() { renderJobs(await api("jobs?limit=50")); if (state.jobId) loadJob(state.jobId); }
function jobTitle(j) { const p = j.params; return `#${j.id} · ${p.category} ${p.lang}/${p.country.toUpperCase()}${p.list_name !== "top" ? " · " + p.list_name : ""}`; }
function jobProgress(j) {
  const pr = j.progress || {};
  if (j.status !== "running") return null;
  if (pr.analyze) return bar(pr.analyze[0], pr.analyze[1]);
  if (pr.crawl) return bar(pr.crawl[0], pr.crawl[1]);
  return bar(0, 1);
}
function renderJobs(jobs) {
  const list = $("#job-list");
  if (!jobs.length) return list.replaceChildren(el("div", { class: "empty" }, "Henüz iş yok. Çalıştır sekmesinden başlatın."));
  list.replaceChildren(...jobs.map((j) => el("button", { class: "item" + (j.id === state.jobId ? " active" : ""), onclick: () => loadJob(j.id) },
    el("div", { class: "title" }, el("span", {}, jobTitle(j)), el("span", { class: "status " + j.status }, STATUS[j.status] || j.status)),
    el("div", { class: "sub" }, `${state.meta.kinds[j.kind] || j.kind} · ${j.model || "anahtar kelime"}`),
    el("div", { class: "sub" }, j.status === "running" ? `${j.progress.stage} · ${duration(j.started_at)}` : fmtTime(j.created_at)),
    jobProgress(j))));
}
async function loadJob(id, quiet = false) {
  state.jobId = id;
  let j;
  try { j = await api(`jobs/${id}`); } catch (err) { if (!quiet) toast(err.message); return; }
  $$("#job-list .item").forEach((n, i) => n.classList.toggle("active", n.textContent.startsWith(`#${id} `)));
  const d = $("#job-detail"); d.classList.remove("hidden");
  const pre = $("pre.log", d), atBottom = !pre || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  const p = j.params, pr = j.progress || {};
  const kv = el("dl", { class: "kv" },
    el("dt", {}, "Durum"), el("dd", {}, `${STATUS[j.status] || j.status}${j.status === "running" ? " · " + pr.stage : ""}`),
    el("dt", {}, "İş"), el("dd", {}, state.meta.kinds[j.kind] || j.kind),
    el("dt", {}, "Analiz"), el("dd", {}, j.model ? `${j.model}` : "anahtar kelime"),
    el("dt", {}, "Ayarlar"), el("dd", {}, `ilk ${p.top} uygulama · ${p.reviews_per_app} yorum/uyg. · ≤${p.max_stars}★${p.since ? " · " + p.since + " sonrası" : ""}${p.exclude_big ? " · büyükler hariç" : ""}`),
    el("dt", {}, "İstek"), el("dd", {}, j.requests != null ? `${j.requests}${j.request_cap ? " / tavan " + j.request_cap : ""}` : (j.request_cap ? `tavan ${j.request_cap}` : "—")),
    el("dt", {}, "Zaman"), el("dd", {}, `${fmtTime(j.started_at || j.created_at)} · ${duration(j.started_at, j.finished_at)}`),
  );
  if (pr.crawl) kv.append(el("dt", {}, "Tarama"), el("dd", {}, `${pr.crawl[0]} / ${pr.crawl[1]} uygulama`));
  if (pr.analyze) kv.append(el("dt", {}, "Analiz"), el("dd", {}, `${pr.analyze[0]} / ${pr.analyze[1]} yorum`));
  const actions = el("div", { class: "report-actions" },
    ...(j.reports || []).filter((n) => n.endsWith(".md")).map((n) => el("button", { class: "small", onclick: () => { switchTab("reports"); openReport(n); } }, "Raporu aç: " + n)),
    (j.status === "running" || j.status === "queued") ? el("button", { class: "small danger", onclick: () => cancelJob(j.id) }, "İptal et") : null,
    el("button", { class: "small ghost", onclick: () => rerun(j) }, "Aynı ayarlarla forma al"));
  fill(d,
    el("h2", {}, el("span", {}, jobTitle(j)), el("span", { class: "status " + j.status }, STATUS[j.status] || j.status)),
    kv, j.error ? el("p", { class: "error" }, j.error) : null, actions,
    el("pre", { class: "log" }, j.log || "(günlük boş)"));
  const npre = $("pre.log", d); if (atBottom) npre.scrollTop = npre.scrollHeight;
}
async function cancelJob(id) {
  try { await api(`jobs/${id}/cancel`, { method: "POST" }); toast("İptal isteği gönderildi; o ana kadarki sonuçlar kaydedilir"); loadJob(id); }
  catch (err) { toast(err.message); }
}
function rerun(j) {
  const p = j.params;
  for (const [k, v] of Object.entries(p)) {
    const f = form.elements[k];
    if (!f || f instanceof RadioNodeList) continue;
    if (f.type === "checkbox") f.checked = !!v;
    else f.value = Array.isArray(v) ? v.join(", ") : (v ?? "");
  }
  form.analyzer.value = p.analyzer;
  switchTab("run");
  setTimeout(() => { if (p.endpoint_id) { $("#run-endpoint").value = p.endpoint_id; onRunEndpoint().then(() => { form.model.value = p.model || ""; }); } updateRunForm(); }, 50);
}

// ---- reports ------------------------------------------------------------------------------------
async function loadReports() {
  const items = await api("reports");
  const list = $("#report-list");
  if (!items.length) return list.replaceChildren(el("div", { class: "empty" }, "Henüz rapor yok."));
  list.replaceChildren(...items.map((r) => {
    const p = r.params || {}, t = r.totals || {};
    return el("button", { class: "item" + (r.name === state.reportName ? " active" : ""), onclick: () => openReport(r.name) },
      el("div", { class: "title" }, el("span", {}, p.model || p.analyzer || r.name),
        el("span", { class: "tag" }, p.category ? `${p.category} ${p.lang}/${(p.country || "").toUpperCase()}` : "")),
      el("div", { class: "sub" }, `${p.list && p.list !== "top" ? "liste " + p.list + " · " : ""}${t.reviews != null ? t.reviews + " yorum" : ""}${p.since ? " · " + p.since + " sonrası" : ""} · ${r.modified}`),
      el("div", { class: "chips" }, ...(r.top_themes || []).map((x) => el("span", { class: "chip" }, `${x.label} (${x.reviews})`))));
  }));
}
async function openReport(name) {
  state.reportName = name;
  const v = $("#report-view"); v.classList.remove("hidden"); v.replaceChildren(el("p", { class: "muted" }, "Yükleniyor…"));
  try {
    const r = await api(`reports/${encodeURIComponent(name)}`);
    const body = el("div");
    body.innerHTML = r.html; // server-rendered, HTML-escaped Markdown
    const json = name.replace(/\.md$/, ".json");
    v.replaceChildren(el("div", { class: "report-actions" },
      el("a", { class: "ghost small", href: `/api/reports/${encodeURIComponent(name)}?raw=1` }, "⬇ .md"),
      el("a", { class: "ghost small", href: `/api/reports/${encodeURIComponent(json)}?raw=1` }, "⬇ .json")), body);
    $$("#report-list .item").forEach((n) => n.classList.remove("active"));
    v.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) { v.replaceChildren(el("p", { class: "error" }, err.message)); }
}

// ---- endpoints ----------------------------------------------------------------------------------
async function loadEndpoints() {
  state.endpoints = await api("endpoints");
  const list = $("#endpoint-list");
  if (!state.endpoints.length) list.replaceChildren(el("div", { class: "empty" }, "Uç nokta yok. OpenRouter, bir LLM gateway veya yerel sunucu gibi OpenAI uyumlu bir adres ekleyin."));
  else list.replaceChildren(...state.endpoints.map((e) => el("button", { class: "item" + (e.id === state.endpointId ? " active" : ""), onclick: () => openEndpoint(e.id) },
    el("div", { class: "title" }, el("span", {}, e.name), el("span", { class: "tag" }, e.has_key ? "anahtar " + e.api_key_masked : "anahtarsız")),
    el("div", { class: "sub" }, e.base_url),
    e.daily_limit ? el("div", { class: "sub" }, `bugün ${e.used_today + e.reserved} / ${e.daily_limit} istek`) : null,
    e.daily_limit ? bar(e.used_today + e.reserved, e.daily_limit, (e.used_today + e.reserved) / e.daily_limit > 0.9 ? "full" : (e.used_today + e.reserved) / e.daily_limit > 0.7 ? "warn" : "") : null)));
  if (state.endpointId) openEndpoint(state.endpointId);
}
$("#new-endpoint").addEventListener("click", () => openEndpoint(null));
function openEndpoint(id) {
  state.endpointId = id;
  const e = state.endpoints.find((x) => x.id === id) || {};
  const f = $("#endpoint-form");
  $("#endpoint-detail").classList.remove("hidden");
  $("#endpoint-title").textContent = id ? e.name : "Yeni uç nokta";
  for (const k of ["name", "base_url", "daily_limit", "default_model", "note"]) f[k].value = e[k] ?? "";
  f.api_key.value = ""; f.clear_key.checked = false;
  f.api_key.placeholder = id ? (e.has_key ? `kayıtlı (${e.api_key_masked}) — değiştirmek için yazın` : "anahtar yok") : "sk-…";
  $("#endpoint-delete").classList.toggle("hidden", !id);
  $("#endpoint-models").classList.toggle("hidden", !id);
  $("#endpoint-error").textContent = "";
  $$("#endpoint-list .item").forEach((n, i) => n.classList.toggle("active", state.endpoints[i] && state.endpoints[i].id === id));
  if (id) loadModels(id);
}
$("#endpoint-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target, body = {};
  for (const k of ["name", "base_url", "api_key", "daily_limit", "default_model", "note"]) body[k] = f[k].value;
  body.clear_key = f.clear_key.checked;
  try {
    const saved = state.endpointId ? await api(`endpoints/${state.endpointId}`, { method: "PUT", body }) : await api("endpoints", { method: "POST", body });
    state.endpointId = saved.id; toast("Kaydedildi"); loadEndpoints();
  } catch (err) { $("#endpoint-error").textContent = err.message; }
});
$("#endpoint-delete").addEventListener("click", async () => {
  if (!state.endpointId || !confirm("Bu uç nokta, anahtarı ve modelleriyle silinsin mi?")) return;
  try { await api(`endpoints/${state.endpointId}`, { method: "DELETE" }); state.endpointId = null; $("#endpoint-detail").classList.add("hidden"); loadEndpoints(); }
  catch (err) { toast(err.message); }
});
async function loadModels(id) {
  state.models = await api(`endpoints/${id}/models`);
  renderModels();
  $("#endpoint-model-list").replaceChildren(...state.models.map((m) => el("option", { value: m.model_id })));
}
function renderModels() {
  const q = $("#model-filter").value.trim().toLowerCase();
  const shown = state.models.filter((m) => !q || m.model_id.toLowerCase().includes(q));
  const manual = state.models.filter((m) => m.source === "manual").length;
  $("#model-count").textContent = `${state.models.length} model (${manual} manuel)${q ? ` · ${shown.length} eşleşme` : ""}`;
  $("#model-list").replaceChildren(...shown.slice(0, 400).map((m) => el("li", {},
    el("span", {}, el("code", {}, m.model_id), " ", el("span", { class: "tag " + m.source }, m.source === "manual" ? "manuel" : "uç noktadan"),
      m.note ? el("span", { class: "muted small" }, " " + m.note) : null),
    el("span", { class: "row" },
      el("button", { class: "ghost small", title: "Varsayılan model yap", onclick: () => { $("#endpoint-form").default_model.value = m.model_id; toast("Kaydet'e basınca varsayılan olur"); } }, "★"),
      m.source === "manual" ? el("button", { class: "ghost small danger", title: "Sil", onclick: () => deleteModel(m.model_id) }, "✕") : null))));
}
$("#model-filter").addEventListener("input", renderModels);
$("#fetch-models").addEventListener("click", async (ev) => {
  const b = ev.target; b.disabled = true; b.textContent = "Çekiliyor…";
  try { const r = await api(`endpoints/${state.endpointId}/models/fetch`, { method: "POST" }); toast(`${r.fetched} model alındı`); loadModels(state.endpointId); }
  catch (err) { toast(err.message); }
  finally { b.disabled = false; b.textContent = "↻ Uç noktadan çek"; }
});
$("#add-model-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  try { state.models = await api(`endpoints/${state.endpointId}/models`, { method: "POST", body: { model_id: f.model_id.value.trim(), note: f.note.value } }); f.reset(); renderModels(); toast("Model eklendi"); }
  catch (err) { toast(err.message); }
});
async function deleteModel(mid) {
  try { state.models = await api(`endpoints/${state.endpointId}/models/${encodeURIComponent(mid)}`, { method: "DELETE" }); renderModels(); }
  catch (err) { toast(err.message); }
}

boot().catch((err) => showLogin(err.message));
