"use strict";

// ============================================================ utilidades
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("pt-BR"));
const STATUS_LABEL = {
  queued: "Na fila", scripting: "Escrevendo", scripted: "Roteiro pronto", rendering: "Renderizando",
  rendered: "Renderizado", uploading: "Enviando", scheduled: "Agendado", published: "Publicado", failed: "Falhou",
};
const KIND_LABEL = { short: "Short", long: "Longo" };
const HEALTH = { ok: ["good", "✓", "Saudável"], warning: ["warn", "!", "Atenção"], critical: ["bad", "✕", "Crítico"] };
const SEV_ICON = { critical: "✕", warning: "!", info: "i" };
const LIST_FIELDS = ["default_tags", "content_mix"];

function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { return null; } return null; }

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const token = store("dg_token");
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    const t = prompt("Token do painel (DASHBOARD_TOKEN):");
    if (t) { store("dg_token", t); return api(path, opts); }
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Erro ${res.status}`);
  return data;
}
function tokenQS(prefix = "?") { const t = store("dg_token"); return t ? `${prefix}token=${encodeURIComponent(t)}` : ""; }

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg; el.className = isError ? "error" : ""; el.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (el.hidden = true), 4500);
}
async function busy(btn, fn) {
  if (btn) btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
}
const badge = (status) => `<span class="badge ${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;
const healthChip = (h) => { const [cls, ic, label] = HEALTH[h] || HEALTH.ok; return `<span class="chip ${cls}"><span class="ic">${ic}</span>${label}</span>`; };
function fmtWhen(s) {
  if (!s) return "—";
  const [d, t] = s.split(" "), [y, m, day] = d.split("-");
  return `${day}/${m}${+y !== new Date().getFullYear() ? `/${y}` : ""} ${t}`;
}
const timeAgo = (iso) => new Date(iso).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
function compact(v) { return v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(v >= 1e4 ? 0 : 1)}k` : String(Math.round(v)); }
function niceMax(v) { if (v <= 0) return 1; const p = 10 ** Math.floor(Math.log10(v)); return Math.ceil(v / p) * p; }

const tip = $("#tooltip");
function showTip(e, html) { tip.innerHTML = html; tip.hidden = false; moveTip(e); }
function moveTip(e) {
  const r = tip.getBoundingClientRect();
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
  tip.style.left = `${x}px`; tip.style.top = `${y}px`;
}
function hideTip() { tip.hidden = true; }

// ============================================================ estado e roteamento
let channels = [];          // resumo de todos os canais (portfólio)
let currentChannel = null;  // id do canal selecionado
let refreshTimer;

const channelPages = { overview: loadOverview, videos: loadVideos, new: initNew, times: loadTimes, analytics: loadAnalytics, config: loadConfig };
const globalPages = { portfolio: loadPortfolio, "videos-all": loadAllVideos, "new-channel": loadNewChannel, settings: loadSettings };
const globalHash = { "": "portfolio", portfolio: "portfolio", videos: "videos-all", "new-channel": "new-channel", settings: "settings" };

async function refreshChannels() {
  channels = await api("/api/channels");
  const sel = $("#channel-select");
  sel.innerHTML = `<option value="">— escolha um canal —</option>` +
    channels.map((c) => `<option value="${c.id}">${c.active ? "" : "⏸ "}${esc(c.name)}</option>`).join("");
  sel.value = currentChannel ? String(currentChannel) : "";
  const all = $("#all-channel");
  const prev = all.value;
  all.innerHTML = `<option value="">Todos os canais</option>` + channels.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  all.value = prev;
}
const chById = (id) => channels.find((c) => c.id === +id);

async function route() {
  clearTimeout(refreshTimer);
  const raw = (location.hash || "#").slice(1);
  const m = raw.match(/^c(\d+)(?:\/(\w+))?$/);
  let page;
  if (m) {
    currentChannel = +m[1];
    store("dg_channel", String(currentChannel));
    page = channelPages[m[2]] ? m[2] : "overview";
  } else {
    page = globalHash[raw] || "portfolio";
  }
  try { await refreshChannels(); } catch (e) { toast(e.message, true); }
  if (m && !chById(currentChannel)) { location.hash = "#portfolio"; return; }

  $$(".page").forEach((p) => (p.hidden = p.id !== `page-${page}`));
  $$(".nav-global a").forEach((a) => a.classList.toggle("active", !m && a.dataset.page === page));
  const nav = $("#nav-channel");
  nav.hidden = !currentChannel || !chById(currentChannel);
  $$("a", nav).forEach((a) => {
    a.href = `#c${currentChannel}/${a.dataset.sub}`;
    a.classList.toggle("active", !!m && a.dataset.sub === page);
  });
  const ch = chById(currentChannel);
  $$(".ch-name").forEach((h) => (h.textContent = ch ? ch.name : ""));
  renderSideStatus();
  const loader = m ? channelPages[page] : globalPages[page];
  loader().catch((e) => toast(e.message, true));
}
addEventListener("hashchange", route);
$("#channel-select").addEventListener("change", (e) => {
  if (!e.target.value) { location.hash = "#portfolio"; return; }
  const sub = (location.hash.match(/^#c\d+\/(\w+)/) || [])[1] || "overview";
  location.hash = `#c${e.target.value}/${sub}`;
});

async function renderSideStatus() {
  try {
    const pf = await api("/api/portfolio");
    const dot = (ok) => `<span style="color:${ok ? "var(--good)" : "var(--critical)"}">●</span>`;
    $("#side-status").innerHTML = `
      <div>${dot(pf.worker_alive)} Worker</div>
      <div>${dot(pf.claude_configured)} Claude API</div>
      <div class="muted">${pf.totals.active}/${pf.totals.channels} canais ativos</div>`;
  } catch { /* ignore */ }
}

// ============================================================ portfólio
async function loadPortfolio() {
  const [pf, compare, feed] = await Promise.all([api("/api/portfolio"), api("/api/compare"), api("/api/activity?limit=40")]);
  const t = pf.totals;
  const chip = (ok, label, warn = false) => `<span class="chip ${ok ? "good" : warn ? "warn" : "bad"}"><span class="ic">${ok ? "✓" : warn ? "!" : "✕"}</span>${esc(label)}</span>`;
  $("#pf-chips").innerHTML = [
    chip(pf.worker_alive, pf.worker_alive ? "Worker ativo" : "Worker parado (rode python -m darkgen serve)"),
    chip(pf.claude_configured, pf.claude_configured ? "Claude API configurada" : "Sem Claude API (roteiros via MCP)", true),
    chip(pf.pexels_configured, pf.pexels_configured ? "Pexels ativo" : "Sem Pexels (fundos procedurais)", true),
    t.needs_attention ? chip(false, `${t.needs_attention} canal(is) precisam de atenção`, true) : chip(true, "Todos os canais saudáveis"),
  ].join("");
  $("#pf-tiles").innerHTML = [
    ["Canais ativos", `${t.active}/${t.channels}`], ["Inscritos (total)", fmt(t.subscribers)], ["Vídeos por dia", fmt(t.daily_videos)],
    ["Em produção", fmt(t.in_production)], ["Agendados", fmt(t.scheduled)], ["Publicados (7 dias)", fmt(t.published_7d)],
  ].map(([l, v]) => `<div class="tile"><div class="label">${l}</div><div class="value">${v}</div></div>`).join("");

  $("#pf-channels").innerHTML = pf.channels.length ? pf.channels.map(channelCard).join("") : `
    <div class="card empty-state"><h2>Nenhum canal ainda</h2><p class="muted">Crie o primeiro canal a partir de um modelo de nicho.</p>
    <a class="btn primary" href="#new-channel">+ Novo canal</a></div>`;
  $$("#pf-channels [data-toggle]").forEach((b) => b.addEventListener("click", (e) => busy(e.target, async () => {
    const c = chById(b.dataset.toggle);
    await api(`/api/channels/${c.id}`, { method: "PATCH", json: { active: !c.active } });
    toast(c.active ? "Canal pausado." : "Canal reativado."); route();
  })));

  renderCompare(compare);
  const quotaRows = Object.entries(pf.quota);
  $("#pf-quota").innerHTML = quotaRows.length ? `<div class="table-wrap"><table><thead><tr><th>Projeto</th><th>Canais</th><th class="num">Uploads</th><th>Situação</th></tr></thead><tbody>${
    quotaRows.map(([k, q]) => `<tr class="static"><td>${k === "none" ? '<span class="muted">sem credencial</span>' : `<code>${esc(k)}</code>`}</td><td>${q.channels.map((id) => esc(chById(id)?.name || id)).join(", ")}</td>
      <td class="num">${q.uploads_24h}/${q.limit}</td><td>${q.blocked_until ? `<span class="badge failed">bloqueado até ${esc(timeAgo(q.blocked_until))}</span>` : q.uploads_24h >= q.limit ? `<span class="badge rendered">no limite</span>` : `<span class="badge published">ok</span>`}</td></tr>`).join("")
  }</tbody></table></div>` : `<div class="empty">Sem canais.</div>`;
  renderFeed($("#pf-feed"), feed, true);
  refreshTimer = setTimeout(() => { if (!location.hash || location.hash === "#portfolio") loadPortfolio().catch(() => {}); }, 20000);
}

function channelCard(c) {
  const issues = c.issues.filter((i) => i.severity !== "info").slice(0, 2);
  return `<div class="card channel-card ${c.active ? "" : "paused"}">
    <div class="cc-head">
      <div class="cc-title">${c.youtube_thumb ? `<img src="${esc(c.youtube_thumb)}" alt="">` : `<span class="avatar">${esc(c.name.slice(0, 1).toUpperCase())}</span>`}
        <div><a href="#c${c.id}/overview" class="cc-name">${esc(c.name)}</a>
        <div class="muted small">${esc(c.youtube_title || "YouTube não conectado")} · ${esc(c.language)}</div></div></div>
      ${c.active ? healthChip(c.health) : `<span class="chip"><span class="ic">⏸</span>Pausado</span>`}
    </div>
    <div class="cc-stats">
      <div><span class="label">Inscritos</span><strong>${fmt(c.subscribers)}</strong></div>
      <div><span class="label">Agendados</span><strong>${fmt(c.scheduled)}</strong></div>
      <div><span class="label">Produção</span><strong>${fmt(c.in_production)}</strong></div>
      <div><span class="label">Falhas</span><strong>${fmt(c.failed)}</strong></div>
    </div>
    <div class="muted small">Próxima: ${esc(fmtWhen(c.next_publication))} · Última: ${esc(fmtWhen(c.last_publication))}</div>
    ${issues.length ? `<ul class="issues compact">${issues.map((i) => `<li class="${i.severity}"><span class="ic">${SEV_ICON[i.severity]}</span>${esc(i.message)}</li>`).join("")}</ul>` : ""}
    <div class="actions">
      <a class="btn" href="#c${c.id}/overview">Abrir</a>
      <a class="btn" href="#c${c.id}/config">Configurar</a>
      <button class="btn" data-toggle="${c.id}">${c.active ? "Pausar" : "Retomar"}</button>
    </div>
  </div>`;
}

function renderCompare(rows) {
  const el = $("#pf-compare");
  const data = rows.filter((r) => r.videos > 0);
  if (!data.length) { el.innerHTML = `<div class="empty">Ainda sem vídeos publicados com estatísticas.</div>`; return; }
  const max = niceMax(Math.max(...data.map((r) => r.avg_views)));
  el.innerHTML = data.map((r) => `<div class="hbar" data-id="${r.channel_id}">
      <span class="hb-label" title="${esc(r.name)}">${esc(r.name)}</span>
      <span class="hb-track"><span class="hb-fill" style="width:${Math.max(1, (r.avg_views / max) * 100)}%"></span></span>
      <span class="hb-value">${compact(r.avg_views)}</span></div>`).join("");
  $$(".hbar", el).forEach((row) => {
    const r = data.find((x) => x.channel_id === +row.dataset.id);
    row.addEventListener("mouseenter", (e) => showTip(e, `<strong>${esc(r.name)}</strong><br>${fmt(r.avg_views)} views/vídeo<br><span class="muted">${r.videos} vídeos · ${fmt(r.views)} views</span>`));
    row.addEventListener("mousemove", moveTip);
    row.addEventListener("mouseleave", hideTip);
  });
}

function renderFeed(el, feed, showChannel) {
  el.innerHTML = feed.length
    ? feed.map((a) => `<li><time>${timeAgo(a.ts)}</time><span class="${esc(a.level)}">${showChannel && a.channel_id ? `<a href="#c${a.channel_id}/overview" class="feed-ch">${esc(chById(a.channel_id)?.name || `#${a.channel_id}`)}</a> · ` : ""}${a.video_id ? `#${a.video_id} · ` : ""}${esc(a.message)}</span></li>`).join("")
    : `<li class="empty">Sem atividade ainda.</li>`;
}

$("#btn-plan-all").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await api("/api/plan", { method: "POST" });
  toast(r.length ? `${r.length} vídeo(s) planejado(s) em todos os canais.` : "As agendas já estão completas."); route();
}));
$("#btn-refresh-all").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await api("/api/stats/refresh", { method: "POST" });
  toast(`Estatísticas atualizadas em ${r.length} canal(is).`); route();
}));

// ============================================================ novo canal
let presets = [], selectedPreset = null;
async function loadNewChannel() {
  if (!presets.length) presets = await api("/api/presets");
  $("#preset-grid").innerHTML = presets.map((p) => `
    <button type="button" class="preset ${p.key === selectedPreset ? "active" : ""}" data-key="${p.key}">
      <strong>${esc(p.label)}</strong><span class="badge kind">${esc(p.language)}</span>
      <span class="muted small">${esc(p.description)}</span></button>`).join("") +
    `<button type="button" class="preset ${selectedPreset === "" ? "active" : ""}" data-key=""><strong>Personalizado</strong><span class="muted small">Comece do padrão e escreva seu próprio nicho.</span></button>`;
  $$("#preset-grid .preset").forEach((b) => b.addEventListener("click", () => { selectedPreset = b.dataset.key; fillPreset(); loadNewChannel(); }));
  if (selectedPreset === null) { selectedPreset = presets[0].key; fillPreset(); loadNewChannel(); }
}
const NEW_DEFAULTS = { language: "pt-BR", timezone: "America/Sao_Paulo", voice: "pt-BR-AntonioNeural", shorts_per_day: 2, long_per_day: 1, min_gap_hours: 3,
  niche: "", style: "narrador sério e atmosférico, frases curtas, suspense crescente" };
function fillPreset() {
  const p = presets.find((x) => x.key === selectedPreset);
  const vals = { ...NEW_DEFAULTS, ...(p ? p.settings : {}) };
  const f = $("#form-channel");
  for (const k of Object.keys(NEW_DEFAULTS)) if (f.elements[k]) f.elements[k].value = vals[k];
}
$("#form-channel").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, fd = new FormData(f);
  const settings = {};
  for (const k of ["language", "timezone", "voice", "niche", "style", "subtitle_color"]) if (fd.get(k)) settings[k] = fd.get(k);
  for (const k of ["shorts_per_day", "long_per_day", "min_gap_hours"]) if (fd.get(k) !== "") settings[k] = Number(fd.get(k));
  settings.approval_required = f.elements.approval_required.checked;
  settings.autopilot = f.elements.autopilot.checked;
  busy(e.submitter, async () => {
    const ch = await api("/api/channels", { method: "POST", json: { name: fd.get("name"), preset: selectedPreset || null, settings } });
    toast(`Canal “${ch.name}” criado. Agora conecte o YouTube.`);
    f.reset(); selectedPreset = null;
    location.hash = `#c${ch.id}/config`;
  });
});

// ============================================================ tabela de vídeos (compartilhada)
function videoTable(rows, cols) {
  if (!rows.length) return `<div class="empty">Nenhum vídeo.</div>`;
  const head = { id: "#", channel: "Canal", when: "Publicação", kind: "Tipo", title: "Título", status: "Status", views: "Views", likes: "Likes" };
  const cell = {
    id: (v) => `<td class="num">${v.id}</td>`,
    channel: (v) => `<td class="nowrap">${esc(v.channel_name || "")}</td>`,
    when: (v) => `<td class="num">${esc(fmtWhen(v.scheduled_local))}</td>`,
    kind: (v) => `<td><span class="badge kind">${KIND_LABEL[v.kind]}</span></td>`,
    title: (v) => `<td class="title" title="${esc(v.title)}">${esc(v.title)}</td>`,
    status: (v) => `<td>${badge(v.status)}</td>`,
    views: (v) => `<td class="num">${fmt(v.views)}</td>`,
    likes: (v) => `<td class="num">${fmt(v.likes)}</td>`,
  };
  const numCols = new Set(["id", "when", "views", "likes"]);
  return `<div class="table-wrap"><table><thead><tr>${cols.map((c) => `<th class="${numCols.has(c) ? "num" : ""}">${head[c]}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((v) => `<tr data-id="${v.id}">${cols.map((c) => cell[c](v)).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function bindRows(el) { $$("tbody tr[data-id]", el).forEach((tr) => tr.addEventListener("click", () => openVideo(+tr.dataset.id))); }

const STATUS_FILTERS = [["", "Todos"], ["queued,scripting,scripted,rendering,rendered,uploading", "Em produção"], ["rendered", "Renderizados"], ["scheduled", "Agendados"], ["published", "Publicados"], ["failed", "Falhas"]];
function statusChips(el, current, onPick) {
  el.innerHTML = STATUS_FILTERS.map(([v, l]) => `<span class="chip ${v === current ? "active" : ""}" data-v="${v}">${l}</span>`).join("");
  $$(".chip", el).forEach((c) => c.addEventListener("click", () => onPick(c.dataset.v)));
}

// ============================================================ todos os vídeos
let allFilter = "";
async function loadAllVideos() {
  statusChips($("#all-status"), allFilter, (v) => { allFilter = v; loadAllVideos(); });
  const qs = new URLSearchParams({ limit: 400 });
  if (allFilter) qs.set("status", allFilter);
  if ($("#all-kind").value) qs.set("kind", $("#all-kind").value);
  if ($("#all-channel").value) qs.set("channel_id", $("#all-channel").value);
  const rows = await api(`/api/videos?${qs}`);
  $("#all-table").innerHTML = videoTable(rows, ["id", "channel", "when", "kind", "title", "status", "views"]);
  bindRows($("#all-table"));
}
$("#all-kind").addEventListener("change", loadAllVideos);
$("#all-channel").addEventListener("change", loadAllVideos);

// ============================================================ canal: visão geral
async function loadOverview() {
  const id = currentChannel;
  const [o, feed] = await Promise.all([api(`/api/channels/${id}`), api(`/api/activity?limit=40&channel_id=${id}`)]);
  const ch = o.channel, s = ch.settings;
  const ytLabel = ch.youtube_connected ? `YouTube: ${ch.youtube_title || "conectado"}` : ch.youtube_title ? `YouTube: ${ch.youtube_title} (desconectado)` : "YouTube não conectado";
  $("#ov-sub").textContent = `${ytLabel} · ${s.language} · público em ${s.timezone}`;
  const chip = (ok, label, warn = false) => `<span class="chip ${ok ? "good" : warn ? "warn" : "bad"}"><span class="ic">${ok ? "✓" : warn ? "!" : "✕"}</span>${esc(label)}</span>`;
  $("#status-chips").innerHTML = [
    ch.active ? healthChip(o.health.status) : `<span class="chip"><span class="ic">⏸</span>Pausado</span>`,
    chip(ch.youtube_connected, ch.youtube_connected ? "YouTube conectado" : "YouTube desconectado"),
    chip(s.autopilot, s.autopilot ? "Piloto automático" : "Piloto automático desligado", true),
    chip(!s.approval_required, s.approval_required ? "Aprovação manual" : "Publica sem aprovação", true),
  ].join("");
  $("#health-card").hidden = !o.health.issues.length;
  $("#health-issues").innerHTML = o.health.issues.map((i) => `<li class="${i.severity}"><span class="ic">${SEV_ICON[i.severity]}</span><div>${esc(i.message)}<div class="muted small">Ação: ${esc(i.action)}</div></div></li>`).join("");

  const c = o.counts;
  const inProd = c.queued + c.scripting + c.scripted + c.rendering + c.rendered + c.uploading;
  $("#kpis").innerHTML = [
    ["Inscritos", fmt(ch.subscribers)], ["Views do canal", fmt(ch.views)], ["Em produção", fmt(inProd)],
    ["Agendados", fmt(c.scheduled)], ["Publicados", fmt(c.published)], ["Falhas", fmt(c.failed)],
  ].map(([l, v]) => `<div class="tile"><div class="label">${l}</div><div class="value">${v}</div></div>`).join("");
  $("#upcoming").innerHTML = o.upcoming.length ? videoTable(o.upcoming, ["when", "kind", "title", "status"])
    : `<div class="empty">Nada agendado. Clique em “Planejar agenda” ou crie um vídeo.</div>`;
  bindRows($("#upcoming"));
  $("#approval-card").hidden = !o.awaiting_approval.length;
  $("#approval").innerHTML = videoTable(o.awaiting_approval, ["when", "kind", "title", "status"]);
  bindRows($("#approval"));
  renderFeed($("#feed"), feed, false);
  refreshTimer = setTimeout(() => { if (location.hash === `#c${id}/overview`) loadOverview().catch(() => {}); }, 15000);
}
$("#btn-plan").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await api(`/api/channels/${currentChannel}/plan`, { method: "POST" });
  toast(r.length ? `${r.length} vídeo(s) planejado(s).` : "A agenda já está completa."); loadOverview();
}));
$("#btn-refresh-stats").addEventListener("click", (e) => busy(e.target, async () => {
  const [r] = await api(`/api/channels/${currentChannel}/stats/refresh`, { method: "POST" });
  if (!r) throw new Error("Canal não conectado ao YouTube.");
  if (r.error) throw new Error(r.error);
  toast(`Estatísticas atualizadas (${r.updated} vídeos).`); loadOverview();
}));

// ============================================================ canal: vídeos
let videoFilter = "";
async function loadVideos() {
  statusChips($("#flt-status"), videoFilter, (v) => { videoFilter = v; loadVideos(); });
  const qs = new URLSearchParams({ limit: 300, channel_id: currentChannel });
  if (videoFilter) qs.set("status", videoFilter);
  if ($("#flt-kind").value) qs.set("kind", $("#flt-kind").value);
  const rows = await api(`/api/videos?${qs}`);
  $("#videos-table").innerHTML = videoTable(rows, ["id", "when", "kind", "title", "status", "views", "likes"]);
  bindRows($("#videos-table"));
}
$("#flt-kind").addEventListener("change", loadVideos);
$("#btn-retry-failed").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await api(`/api/channels/${currentChannel}/retry-failed`, { method: "POST" });
  toast(`${r.requeued} vídeo(s) recolocados na fila.`); loadVideos();
}));

// ============================================================ detalhe do vídeo
const toLocalInput = (s) => (s ? s.replace(" ", "T") : "");
async function openVideo(id) {
  const v = await api(`/api/videos/${id}`);
  const uploaded = !!v.youtube_id;
  const scenes = (v.script && v.script.scenes) || [];
  const words = scenes.reduce((n, s) => n + s.narration.split(/\s+/).length, 0);
  const preview = v.has_file
    ? `<video controls preload="metadata" poster="/media/${v.id}/thumb${tokenQS()}" src="/media/${v.id}/video${tokenQS()}"></video>`
    : `<div class="preview-ph">${v.status === "failed" ? "Falhou antes do vídeo ficar pronto" : "O vídeo aparece aqui depois de renderizado"}</div>`;
  const canApprove = v.status === "rendered" && !v.approved;
  const others = channels.filter((c) => c.id !== v.channel_id);
  $("#modal-body").innerHTML = `
    <div class="modal-head">
      <div><strong>#${v.id}</strong> · ${esc(v.channel_name)} · ${KIND_LABEL[v.kind]} · ${badge(v.status)} ${v.duration_s ? `<span class="muted">${Math.round(v.duration_s)}s</span>` : ""}</div>
      <button class="btn" data-act="close" aria-label="Fechar">✕</button>
    </div>
    <div class="modal-content">
      <div>
        ${preview}
        ${uploaded ? `<p><a href="https://youtu.be/${esc(v.youtube_id)}" target="_blank" rel="noopener">Abrir no YouTube ↗</a></p>` : ""}
        ${v.views != null ? `<p class="muted">${fmt(v.views)} views · ${fmt(v.likes)} likes · ${fmt(v.comments)} comentários</p>` : ""}
      </div>
      <form class="form" id="edit-form">
        ${v.error ? `<div class="err">${esc(v.error)}</div>` : ""}
        <label>Título <input name="title" value="${esc(v.title || "")}" maxlength="100" ${uploaded ? "disabled" : ""}></label>
        <label>Publicação <span class="muted">(fuso do canal)</span><input type="datetime-local" name="scheduled_at" value="${esc(toLocalInput(v.scheduled_local))}" ${uploaded ? "disabled" : ""}></label>
        <label>Descrição <textarea name="description" rows="5" ${uploaded ? "disabled" : ""}>${esc(v.description || "")}</textarea></label>
        <label>Tags <input name="tags" value="${esc((v.tags || []).join(", "))}" ${uploaded ? "disabled" : ""}></label>
        <div class="actions">
          ${!uploaded ? `<button class="btn primary" data-act="save">Salvar</button>` : ""}
          ${canApprove ? `<button class="btn primary" data-act="approve">Aprovar para upload</button>` : ""}
          ${v.status === "failed" ? `<button class="btn" data-act="retry">Tentar de novo</button>` : ""}
          ${!uploaded && v.script ? `<button class="btn" data-act="rerender">Renderizar de novo</button>` : ""}
          <button class="btn danger" data-act="delete">Excluir</button>
        </div>
        ${!uploaded && others.length ? `<div class="actions"><select id="move-to" aria-label="Mover para canal">${others.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select><button class="btn" data-act="move">Mover para este canal</button></div>` : ""}
        ${v.topic ? `<p class="muted">Tema: ${esc(v.topic)}${v.is_fiction ? " · ficção" : ""}</p>` : ""}
        ${scenes.length ? `<h3>Roteiro <span class="muted small">${scenes.length} cenas · ${words} palavras</span></h3>
          <ol class="scene-list">${scenes.map((s, i) => `<li>${i + 1}. ${esc(s.narration)}<small>🎞 ${esc(s.visual_query)}</small></li>`).join("")}</ol>` : ""}
        ${(v.sources || []).length ? `<h3>Fontes</h3><ul>${v.sources.map((s) => `<li><a href="${esc(s)}" target="_blank" rel="noopener">${esc(s)}</a></li>`).join("")}</ul>` : ""}
      </form>
    </div>`;
  const dlg = $("#modal");
  dlg.showModal();
  $("#modal-body").onclick = async (e) => {
    const act = e.target.dataset.act;
    if (!act) return;
    e.preventDefault();
    if (act === "close") return dlg.close();
    await busy(e.target, async () => {
      if (act === "save") {
        const f = new FormData($("#edit-form"));
        await api(`/api/videos/${id}`, { method: "PATCH", json: {
          title: f.get("title"), description: f.get("description"),
          tags: String(f.get("tags") || "").split(",").map((t) => t.trim()).filter(Boolean),
          scheduled_at: f.get("scheduled_at") || null,
        } });
        toast("Salvo.");
      } else if (act === "delete") {
        if (!confirm("Excluir este vídeo da fila? (não apaga do YouTube)")) return;
        await api(`/api/videos/${id}`, { method: "DELETE" });
        toast("Excluído.");
      } else if (act === "move") {
        const target = $("#move-to").value;
        await api(`/api/videos/${id}/move`, { method: "POST", json: { channel_id: +target } });
        toast(`Movido para ${chById(target)?.name}.`);
      } else {
        await api(`/api/videos/${id}/${act}`, { method: "POST" });
        toast({ approve: "Aprovado.", retry: "Recolocado na fila.", rerender: "Vai renderizar de novo." }[act]);
      }
      dlg.close(); route();
    });
  };
}
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") e.target.close(); });

// ============================================================ canal: criar vídeo
let newInit = false;
async function initNew() {
  if (newInit) return;
  newInit = true;
  $$(".tab").forEach((t) => t.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
    $$(".tab-panel").forEach((p) => (p.hidden = p.id !== `tab-${t.dataset.tab}`));
  }));
  resetScenes();
  $("#btn-add-scene").addEventListener("click", () => addScene());
  $("#form-auto").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    busy(e.submitter, async () => {
      const v = await api("/api/videos", { method: "POST", json: {
        channel_id: currentChannel, kind: f.get("kind"), topic: f.get("topic") || null,
        is_fiction: !!f.get("is_fiction"), scheduled_at: f.get("scheduled_at") || null,
      } });
      toast(`Vídeo #${v.id} na fila para ${fmtWhen(v.scheduled_local)}.`); e.target.reset();
    });
  });
  $("#btn-ideas").addEventListener("click", (e) => busy(e.target, async () => {
    const kind = $("#form-auto [name=kind]").value;
    $("#ideas").innerHTML = `<p class="muted">Gerando ideias para este canal…</p>`;
    try {
      const ideas = await api(`/api/channels/${currentChannel}/ideas?kind=${kind}&count=6`, { method: "POST" });
      $("#ideas").innerHTML = `<div class="ideas">${ideas.map((i, n) => `
        <div class="idea"><div><strong>${esc(i.topic)}</strong> <span class="badge kind">${esc(i.category)}</span>
          <div class="muted">${esc(i.angle)}</div><div class="hook">“${esc(i.hook)}”</div></div>
          <button class="btn" data-n="${n}">Usar</button></div>`).join("")}</div>`;
      $$("#ideas button").forEach((b) => b.addEventListener("click", () => {
        const i = ideas[+b.dataset.n];
        $("#form-auto [name=topic]").value = `${i.topic} — ${i.angle}`;
        $("#form-auto [name=is_fiction]").checked = i.is_fiction;
        $("#form-auto [name=topic]").focus();
      }));
    } catch (err) { $("#ideas").innerHTML = ""; throw err; }
  }));
  $("#form-manual").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const scenes = $$("#scenes .scene").map((s) => ({ narration: $("textarea", s).value.trim(), visual_query: $("input", s).value.trim() || "dark atmosphere" })).filter((s) => s.narration);
    if (!scenes.length) return toast("Escreva ao menos uma cena.", true);
    busy(e.submitter, async () => {
      const v = await api("/api/videos", { method: "POST", json: {
        channel_id: currentChannel, kind: f.get("kind"), title: f.get("title"), description: f.get("description"),
        tags: String(f.get("tags") || "").split(",").map((t) => t.trim()).filter(Boolean),
        thumbnail_text: f.get("thumbnail_text") || null, is_fiction: !!f.get("is_fiction"),
        scheduled_at: f.get("scheduled_at") || null, scenes,
      } });
      toast(`Vídeo #${v.id} criado; renderização em seguida.`); e.target.reset(); resetScenes();
    });
  });
  $("#scenes").addEventListener("input", updateWordCount);
}
function resetScenes() { $("#scenes").innerHTML = ""; addScene(); addScene(); addScene(); updateWordCount(); }
function addScene() {
  const el = document.createElement("div");
  el.className = "scene";
  el.innerHTML = `<span class="n"></span><textarea placeholder="Narração da cena"></textarea>
    <input placeholder="visual (inglês): foggy forest"><button type="button" class="btn" aria-label="Remover cena">✕</button>`;
  $("button", el).addEventListener("click", () => { el.remove(); renumber(); updateWordCount(); });
  $("#scenes").appendChild(el); renumber();
}
function renumber() { $$("#scenes .scene .n").forEach((n, i) => (n.textContent = i + 1)); }
function updateWordCount() {
  const words = $$("#scenes textarea").reduce((n, t) => n + (t.value.trim() ? t.value.trim().split(/\s+/).length : 0), 0);
  $("#word-count").textContent = `${words} palavras · ~${Math.round(words / 2.5)}s de narração`;
}

// ============================================================ canal: horários (heatmap)
// Rampa sequencial de um só matiz (azul), escura → clara, sobre superfície escura.
const RAMP = ["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"];
let timesKind = "short";
$$("#times-kind button").forEach((b) => b.addEventListener("click", () => {
  timesKind = b.dataset.kind;
  $$("#times-kind button").forEach((x) => x.classList.toggle("active", x === b));
  loadTimes();
}));
async function loadTimes() {
  const d = await api(`/api/channels/${currentChannel}/best-times?kind=${timesKind}`);
  $("#times-sub").textContent = d.adaptive
    ? `Ajustado com o desempenho real de ${d.samples} vídeos deste canal`
    : "Curva base — passa a se ajustar sozinha após 6 vídeos publicados com 48h+";
  $("#times-tz").textContent = `(fuso do público: ${d.timezone})`;
  const max = Math.max(...d.scores.flat());
  const color = (s) => (s < 0.15 ? "#222221" : RAMP[Math.min(RAMP.length - 1, Math.floor((s / max) * RAMP.length))]);
  let html = `<div class="heatmap"><span></span>${Array.from({ length: 24 }, (_, h) => `<span class="hlabel">${h % 3 === 0 ? h : ""}</span>`).join("")}`;
  d.scores.forEach((row, di) => {
    const best = new Set([...row.keys()].sort((a, b) => row[b] - row[a]).slice(0, 3));
    html += `<span class="dlabel">${d.days[di]}</span>`;
    row.forEach((s, h) => { html += `<span class="cell ${best.has(h) ? "best" : ""}" style="background:${color(s)}" data-d="${di}" data-h="${h}" data-s="${s}"></span>`; });
  });
  $("#heatmap").innerHTML = html + "</div>";
  $$("#heatmap .cell").forEach((c) => {
    c.addEventListener("mouseenter", (e) => showTip(e, `<strong>${d.days[c.dataset.d]} ${String(c.dataset.h).padStart(2, "0")}h</strong><br>Potencial: ${Math.round((c.dataset.s / max) * 100)}%`));
    c.addEventListener("mousemove", moveTip);
    c.addEventListener("mouseleave", hideTip);
  });
  $("#heat-legend").innerHTML = `<span>Menor</span><span class="bar" style="background:linear-gradient(90deg,${RAMP.join(",")})"></span><span>Maior</span>
    <span style="margin-left:16px"><span class="best-key"></span> 3 melhores do dia</span>`;
  $("#next-slots").innerHTML = d.next_slots.map((s) => `<li>${esc(s)}</li>`).join("") || `<li class="muted">Sem horários livres.</li>`;
}

// ============================================================ canal: desempenho
let perfKind = "short", perfData = null;
$$("#perf-kind button").forEach((b) => b.addEventListener("click", () => {
  perfKind = b.dataset.kind;
  $$("#perf-kind button").forEach((x) => x.classList.toggle("active", x === b));
  renderPerfChart();
}));
async function loadAnalytics() {
  perfData = await api(`/api/channels/${currentChannel}/analytics`);
  const t = perfData.totals;
  $("#perf-tiles").innerHTML = [["Vídeos publicados", t.videos], ["Views", t.views], ["Likes", t.likes], ["Comentários", t.comments]]
    .map(([l, v]) => `<div class="tile"><div class="label">${l}</div><div class="value">${fmt(v)}</div></div>`).join("");
  $("#top-table").innerHTML = videoTable(perfData.top, ["when", "kind", "title", "views"]);
  bindRows($("#top-table"));
  $("#cat-table").innerHTML = perfData.per_category.length
    ? `<div class="table-wrap"><table><thead><tr><th>Categoria</th><th class="num">Vídeos</th><th class="num">Views médias</th></tr></thead><tbody>${
      perfData.per_category.map((r) => `<tr class="static"><td>${esc(r.category)}</td><td class="num">${r.videos}</td><td class="num">${fmt(r.avg_views)}</td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty">Sem dados ainda.</div>`;
  renderPerfChart();
}
function renderPerfChart() {
  if (!perfData) return;
  const rows = perfData.per_hour[perfKind];
  const el = $("#perf-chart");
  if (!rows.length) { el.innerHTML = `<div class="empty">Ainda sem vídeos publicados com estatísticas.</div>`; return; }
  const byHour = Object.fromEntries(rows.map((r) => [r.hour, r]));
  const W = 900, H = 260, m = { l: 56, r: 12, t: 12, b: 28 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const nice = niceMax(Math.max(...rows.map((r) => r.avg_views)));
  const bw = iw / 24, bar = Math.max(4, bw - 6);
  const y = (v) => m.t + ih - (v / nice) * ih;
  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Média de views por hora de publicação"><g class="grid">`;
  for (let i = 0; i <= 4; i++) svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y((nice / 4) * i)}" y2="${y((nice / 4) * i)}"></line>`;
  svg += `</g><g class="axis">`;
  for (let i = 0; i <= 4; i++) svg += `<text x="${m.l - 8}" y="${y((nice / 4) * i) + 4}" text-anchor="end">${compact((nice / 4) * i)}</text>`;
  for (let h = 0; h < 24; h += 2) svg += `<text x="${m.l + h * bw + bw / 2}" y="${H - 8}" text-anchor="middle">${h}h</text>`;
  svg += `</g>`;
  for (let h = 0; h < 24; h++) {
    const r = byHour[h];
    if (!r) continue;
    const x = m.l + h * bw + (bw - bar) / 2, top = y(r.avg_views), base = m.t + ih, rad = Math.min(4, bar / 2, base - top);
    svg += `<path class="bar" data-h="${h}" d="M${x},${base} V${top + rad} Q${x},${top} ${x + rad},${top} H${x + bar - rad} Q${x + bar},${top} ${x + bar},${top + rad} V${base} Z"></path>`;
    svg += `<rect data-h="${h}" class="hit" x="${m.l + h * bw}" y="${m.t}" width="${bw}" height="${ih}" fill="transparent"></rect>`;
  }
  el.innerHTML = svg + "</svg>";
  $$("rect.hit", el).forEach((hit) => {
    const r = byHour[hit.dataset.h], path = $(`path[data-h="${hit.dataset.h}"]`, el);
    hit.addEventListener("mouseenter", (e) => { path.classList.add("hover"); showTip(e, `<strong>${r.hour}h</strong><br>${fmt(r.avg_views)} views em média<br><span class="muted">${r.videos} vídeo(s)</span>`); });
    hit.addEventListener("mousemove", moveTip);
    hit.addEventListener("mouseleave", () => { path.classList.remove("hover"); hideTip(); });
  });
}

// ============================================================ canal: configurações
async function loadConfig() {
  const id = currentChannel;
  const o = await api(`/api/channels/${id}`);
  const ch = o.channel, s = ch.settings;
  const redirect = `${location.origin}/api/youtube/callback`;
  $("#yt-box").innerHTML = `
    ${ch.youtube_connected
      ? `<p>Conectado a <strong>${esc(ch.youtube_title || "canal")}</strong>${ch.subscribers != null ? ` · ${fmt(ch.subscribers)} inscritos` : ""}.</p>`
      : `<p class="muted">Crie o canal no YouTube (Conta de Marca), depois clique em Conectar e escolha esse canal na tela do Google.</p>`}
    <div class="actions">
      <button class="btn primary" id="yt-connect">${ch.youtube_connected ? "Reconectar" : "Conectar YouTube"}</button>
      ${ch.youtube_connected ? `<button class="btn danger" id="yt-disconnect">Desconectar</button>` : ""}
    </div>
    <div class="form" style="margin-top:14px">
      <label>Credencial OAuth própria <span class="muted">(opcional, divide a cota)</span> ${ch.own_client_secret ? '<span class="badge published">própria</span>' : '<span class="badge kind">usa a global</span>'}
        <input type="file" id="ch-secret" accept=".json,application/json"></label>
      ${ch.own_client_secret ? `<div class="actions"><button class="btn" id="ch-secret-del">Voltar a usar a credencial global</button></div>` : ""}
      <p class="muted small">Projeto de cota: ${ch.quota_project === "none" ? "sem credencial — envie a global em Configurações gerais ou uma própria aqui" : `<code>${esc(ch.quota_project)}</code>`} · Redirect: <code>${esc(redirect)}</code></p>
    </div>`;
  $("#yt-connect").addEventListener("click", (e) => busy(e.target, async () => { location.href = (await api(`/api/channels/${id}/youtube/connect`)).url; }));
  $("#yt-disconnect")?.addEventListener("click", (e) => busy(e.target, async () => {
    if (!confirm("Desconectar o YouTube deste canal?")) return;
    await api(`/api/channels/${id}/youtube/disconnect`, { method: "POST" }); loadConfig();
  }));
  $("#ch-secret").addEventListener("change", async (e) => {
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    try { await api(`/api/channels/${id}/youtube/client-secret`, { method: "POST", body: fd }); toast("Credencial do canal salva. Reconecte o YouTube."); loadConfig(); }
    catch (err) { toast(err.message, true); }
  });
  $("#ch-secret-del")?.addEventListener("click", (e) => busy(e.target, async () => { await api(`/api/channels/${id}/youtube/client-secret`, { method: "DELETE" }); loadConfig(); }));

  const meta = $("#form-meta");
  meta.elements.name.value = ch.name;
  meta.elements.notes.value = ch.notes || "";
  meta.elements.active.checked = ch.active;
  const form = $("#form-settings");
  for (const [k, v] of Object.entries(s)) {
    const input = form.elements[k];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = !!v;
    else input.value = LIST_FIELDS.includes(k) ? v.join(", ") : v;
  }
  $("#form-delete").elements.confirm.placeholder = `Digite: ${ch.name}`;
}
$("#form-meta").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  busy(e.submitter, async () => {
    await api(`/api/channels/${currentChannel}`, { method: "PATCH", json: { name: f.elements.name.value, notes: f.elements.notes.value, active: f.elements.active.checked } });
    toast("Canal salvo."); route();
  });
});
$("#form-settings").addEventListener("submit", (e) => {
  e.preventDefault();
  const out = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "number") out[el.name] = Number(el.value);
    else if (LIST_FIELDS.includes(el.name)) out[el.name] = el.value.split(",").map((t) => t.trim()).filter(Boolean);
    else out[el.name] = el.value;
  }
  busy(e.submitter, async () => { await api(`/api/channels/${currentChannel}`, { method: "PATCH", json: { settings: out } }); toast("Configurações salvas."); });
});
$("#form-clone").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    const ch = await api(`/api/channels/${currentChannel}/clone`, { method: "POST", json: { name: e.target.elements.name.value } });
    toast(`Canal “${ch.name}” criado com as mesmas configurações.`); e.target.reset(); location.hash = `#c${ch.id}/config`;
  });
});
$("#form-delete").addEventListener("submit", (e) => {
  e.preventDefault();
  const confirmName = e.target.elements.confirm.value;
  busy(e.submitter, async () => {
    await api(`/api/channels/${currentChannel}?confirm=${encodeURIComponent(confirmName)}`, { method: "DELETE" });
    toast("Canal removido do gerenciador."); currentChannel = null; store("dg_channel", ""); location.hash = "#portfolio";
  });
});

// ============================================================ configurações gerais
async function loadSettings() {
  const envs = await api("/api/env");
  const row = (ok, label, hint) => `<div class="chip ${ok ? "good" : "warn"}" style="margin:0 6px 6px 0"><span class="ic">${ok ? "✓" : "!"}</span>${esc(label)}</div>${hint ? `<div class="muted small" style="margin-bottom:8px">${hint}</div>` : ""}`;
  $("#env-box").innerHTML =
    row(envs.anthropic_key, `Claude API (${envs.model})`, envs.anthropic_key ? "" : "Defina ANTHROPIC_API_KEY no .env para o piloto automático. Sem ela, crie roteiros pelo MCP.") +
    row(envs.pexels_key, "Pexels", envs.pexels_key ? "" : "Opcional: PEXELS_API_KEY traz vídeos reais de fundo.") +
    row(envs.tts_provider === "edge", `Voz: ${envs.tts_provider}`, "") +
    `<p class="muted small">Limite de uploads por projeto do Google/dia: ${envs.uploads_per_project_per_day}<br>Dados em ${esc(envs.data_dir)}<br>MCP (HTTP): <code>${esc(location.origin)}/mcp</code></p>`;
  $("#redirect-uri").textContent = `${location.origin}/api/youtube/callback`;
  $("#global-secret-badge").innerHTML = envs.global_client_secret ? '<span class="badge published">enviado</span>' : '<span class="badge failed">faltando</span>';
}
$("#global-secret").addEventListener("change", async (e) => {
  const fd = new FormData(); fd.append("file", e.target.files[0]);
  try { await api("/api/youtube/client-secret", { method: "POST", body: fd }); toast("Credencial global salva."); loadSettings(); }
  catch (err) { toast(err.message, true); }
});

// ============================================================ início
if (!location.hash) {
  const last = store("dg_channel");
  currentChannel = last ? +last : null;
}
route();
