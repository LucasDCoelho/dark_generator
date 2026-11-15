"use strict";

// ------------------------------------------------------------ utilidades
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("pt-BR"));
const STATUS_LABEL = {
  queued: "Na fila", scripting: "Escrevendo", scripted: "Roteiro pronto", rendering: "Renderizando",
  rendered: "Renderizado", uploading: "Enviando", scheduled: "Agendado", published: "Publicado", failed: "Falhou",
};
const KIND_LABEL = { short: "Short", long: "Longo" };

function getToken() { try { return localStorage.getItem("dg_token") || ""; } catch { return ""; } }
function setToken(t) { try { localStorage.setItem("dg_token", t); } catch { /* sem storage */ } }

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    const t = prompt("Token do painel (DASHBOARD_TOKEN):");
    if (t) { setToken(t); return api(path, opts); }
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Erro ${res.status}`);
  return data;
}

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg; el.className = isError ? "error" : ""; el.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (el.hidden = true), 4500);
}

async function busy(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
}

function badge(status) {
  return `<span class="badge ${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;
}

// "AAAA-MM-DD HH:MM" (fuso do canal) -> "15/11 21:00" (ano só se diferente do atual)
function fmtWhen(s) {
  if (!s) return "—";
  const [d, t] = s.split(" "), [y, m, day] = d.split("-");
  return `${day}/${m}${+y !== new Date().getFullYear() ? `/${y}` : ""} ${t}`;
}

function timeAgo(iso) {
  const d = new Date(iso);
  return d.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

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

// ------------------------------------------------------------ roteamento
const pages = { overview: loadOverview, videos: loadVideos, new: initNew, times: loadTimes, analytics: loadAnalytics, config: loadConfig };
function route() {
  const page = (location.hash || "#overview").slice(1);
  const name = pages[page] ? page : "overview";
  $$(".page").forEach((p) => (p.hidden = p.id !== `page-${name}`));
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === name));
  pages[name]().catch((e) => toast(e.message, true));
}
addEventListener("hashchange", route);

// ------------------------------------------------------------ visão geral
let overviewTimer;
async function loadOverview() {
  const o = await api("/api/overview");
  renderSideStatus(o);
  const ch = o.channel;
  $("#ch-title").textContent = ch ? ch.title : "Visão geral";
  $("#ch-sub").textContent = ch ? "Status do canal e da produção" : "Conecte seu canal em Configurações para publicar";

  const chip = (ok, label, warn = false) =>
    `<span class="chip ${ok ? "good" : warn ? "warn" : "bad"}"><span class="ic">${ok ? "✓" : warn ? "!" : "✕"}</span>${esc(label)}</span>`;
  $("#status-chips").innerHTML = [
    chip(o.youtube_connected, o.youtube_connected ? "YouTube conectado" : "YouTube desconectado"),
    chip(o.worker_alive, o.worker_alive ? "Worker ativo" : "Worker parado"),
    chip(o.claude_configured, o.claude_configured ? "Claude API configurada" : "Sem Claude API (use MCP)", true),
    chip(o.pexels_configured, o.pexels_configured ? "Pexels (vídeos de fundo)" : "Sem Pexels (fundos procedurais)", true),
    chip(o.settings.autopilot, o.settings.autopilot ? "Piloto automático ligado" : "Piloto automático desligado", true),
  ].join("");

  const c = o.counts;
  const inProd = c.queued + c.scripting + c.scripted + c.rendering + c.rendered + c.uploading;
  const tiles = [
    ["Inscritos", ch ? fmt(ch.subscribers) : "—"],
    ["Views do canal", ch ? fmt(ch.views) : "—"],
    ["Em produção", fmt(inProd)],
    ["Agendados", fmt(c.scheduled)],
    ["Publicados", fmt(c.published)],
    ["Falhas", fmt(c.failed)],
  ];
  $("#kpis").innerHTML = tiles.map(([l, v]) => `<div class="tile"><div class="label">${l}</div><div class="value">${v}</div></div>`).join("");

  $("#upcoming").innerHTML = o.upcoming.length
    ? videoTable(o.upcoming, ["when", "kind", "title", "status"])
    : `<div class="empty">Nada agendado. Clique em “Planejar agenda agora” ou crie um vídeo.</div>`;
  bindRows($("#upcoming"));

  $("#approval-card").hidden = !o.awaiting_approval.length;
  $("#approval").innerHTML = videoTable(o.awaiting_approval, ["when", "kind", "title", "status"]);
  bindRows($("#approval"));

  const feed = await api("/api/activity?limit=40");
  $("#feed").innerHTML = feed.length
    ? feed.map((a) => `<li><time>${timeAgo(a.ts)}</time><span class="${esc(a.level)}">${a.video_id ? `#${a.video_id} · ` : ""}${esc(a.message)}</span></li>`).join("")
    : `<li class="empty">Sem atividade ainda.</li>`;

  clearTimeout(overviewTimer);
  overviewTimer = setTimeout(() => { if (location.hash === "#overview" || !location.hash) loadOverview().catch(() => {}); }, 15000);
}

function renderSideStatus(o) {
  const dot = (ok) => `<span style="color:${ok ? "var(--good)" : "var(--critical)"}">●</span>`;
  $("#side-status").innerHTML = `
    <div>${dot(o.youtube_connected)} YouTube</div>
    <div>${dot(o.worker_alive)} Worker</div>
    <div>${dot(o.claude_configured)} Claude API</div>
    <div class="muted">Fuso: ${esc(o.timezone)}</div>`;
}

$("#btn-plan").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await api("/api/plan", { method: "POST" });
  toast(r.length ? `${r.length} vídeo(s) planejado(s).` : "A agenda já está completa.");
  loadOverview();
}));
$("#btn-refresh-stats").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await api("/api/stats/refresh", { method: "POST" });
  toast(`Estatísticas atualizadas (${r.updated} vídeos).`);
  loadOverview();
}));

// ------------------------------------------------------------ tabela de vídeos
function videoTable(rows, cols) {
  if (!rows.length) return `<div class="empty">Nenhum vídeo.</div>`;
  const head = {
    id: "#", when: "Publicação", kind: "Tipo", title: "Título", status: "Status", views: "Views", likes: "Likes",
  };
  const cell = {
    id: (v) => `<td class="num">${v.id}</td>`,
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

let videoFilter = "";
async function loadVideos() {
  const filters = [["", "Todos"], ["queued,scripting,scripted,rendering,rendered,uploading", "Em produção"], ["rendered", "Renderizados"], ["scheduled", "Agendados"], ["published", "Publicados"], ["failed", "Falhas"]];
  $("#flt-status").innerHTML = filters.map(([v, l]) => `<span class="chip ${v === videoFilter ? "active" : ""}" data-v="${v}">${l}</span>`).join("");
  $$("#flt-status .chip").forEach((c) => c.addEventListener("click", () => { videoFilter = c.dataset.v; loadVideos(); }));
  const kind = $("#flt-kind").value;
  const qs = new URLSearchParams({ limit: 300 });
  if (videoFilter) qs.set("status", videoFilter);
  if (kind) qs.set("kind", kind);
  const rows = await api(`/api/videos?${qs}`);
  $("#videos-table").innerHTML = videoTable(rows, ["id", "when", "kind", "title", "status", "views", "likes"]);
  bindRows($("#videos-table"));
}
$("#flt-kind").addEventListener("change", loadVideos);

// ------------------------------------------------------------ detalhe do vídeo
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
  $("#modal-body").innerHTML = `
    <div class="modal-head">
      <div><strong>#${v.id}</strong> · ${KIND_LABEL[v.kind]} · ${badge(v.status)} ${v.duration_s ? `<span class="muted">${Math.round(v.duration_s)}s</span>` : ""}</div>
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
        <label>Publicação <input type="datetime-local" name="scheduled_at" value="${esc(toLocalInput(v.scheduled_local))}" ${uploaded ? "disabled" : ""}></label>
        <label>Descrição <textarea name="description" rows="5" ${uploaded ? "disabled" : ""}>${esc(v.description || "")}</textarea></label>
        <label>Tags <input name="tags" value="${esc((v.tags || []).join(", "))}" ${uploaded ? "disabled" : ""}></label>
        <div class="actions">
          ${!uploaded ? `<button class="btn primary" data-act="save">Salvar</button>` : ""}
          ${canApprove ? `<button class="btn primary" data-act="approve">Aprovar para upload</button>` : ""}
          ${v.status === "failed" ? `<button class="btn" data-act="retry">Tentar de novo</button>` : ""}
          ${!uploaded && v.script ? `<button class="btn" data-act="rerender">Renderizar de novo</button>` : ""}
          <button class="btn danger" data-act="delete">Excluir</button>
        </div>
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
        dlg.close(); toast("Excluído."); route(); return;
      } else {
        await api(`/api/videos/${id}/${act}`, { method: "POST" });
        toast({ approve: "Aprovado.", retry: "Recolocado na fila.", rerender: "Vai renderizar de novo." }[act]);
      }
      dlg.close(); route();
    });
  };
}
function tokenQS() { const t = getToken(); return t ? `?token=${encodeURIComponent(t)}` : ""; }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") e.target.close(); });

// ------------------------------------------------------------ criar
let newInit = false;
async function initNew() {
  if (newInit) return;
  newInit = true;
  $$(".tab").forEach((t) => t.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
    $$(".tab-panel").forEach((p) => (p.hidden = p.id !== `tab-${t.dataset.tab}`));
  }));
  addScene(); addScene(); addScene();
  $("#btn-add-scene").addEventListener("click", () => addScene());

  $("#form-auto").addEventListener("submit", (e) => {
    e.preventDefault();
    const btn = e.submitter, f = new FormData(e.target);
    busy(btn, async () => {
      const v = await api("/api/videos", { method: "POST", json: {
        kind: f.get("kind"), topic: f.get("topic") || null, is_fiction: !!f.get("is_fiction"),
        scheduled_at: f.get("scheduled_at") || null,
      } });
      toast(`Vídeo #${v.id} na fila para ${v.scheduled_local}.`);
      e.target.reset();
    });
  });

  $("#btn-ideas").addEventListener("click", (e) => busy(e.target, async () => {
    const kind = $("#form-auto [name=kind]").value;
    $("#ideas").innerHTML = `<p class="muted">Gerando ideias…</p>`;
    const ideas = await api(`/api/ideas?kind=${kind}&count=6`, { method: "POST" });
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
  }).catch(() => ($("#ideas").innerHTML = "")));

  $("#form-manual").addEventListener("submit", (e) => {
    e.preventDefault();
    const btn = e.submitter, f = new FormData(e.target);
    const scenes = $$("#scenes .scene").map((s) => ({
      narration: $("textarea", s).value.trim(), visual_query: $("input", s).value.trim() || "dark atmosphere",
    })).filter((s) => s.narration);
    if (!scenes.length) return toast("Escreva ao menos uma cena.", true);
    busy(btn, async () => {
      const v = await api("/api/videos", { method: "POST", json: {
        kind: f.get("kind"), title: f.get("title"), description: f.get("description"),
        tags: String(f.get("tags") || "").split(",").map((t) => t.trim()).filter(Boolean),
        thumbnail_text: f.get("thumbnail_text") || null, is_fiction: !!f.get("is_fiction"),
        scheduled_at: f.get("scheduled_at") || null, scenes,
      } });
      toast(`Vídeo #${v.id} criado; renderização em seguida.`);
      e.target.reset(); $("#scenes").innerHTML = ""; addScene(); addScene(); addScene();
    });
  });
  $("#scenes").addEventListener("input", updateWordCount);
}

function addScene() {
  const el = document.createElement("div");
  el.className = "scene";
  el.innerHTML = `<span class="n"></span><textarea placeholder="Narração da cena"></textarea>
    <input placeholder="visual (inglês): foggy forest"><button type="button" class="btn" aria-label="Remover cena">✕</button>`;
  $("button", el).addEventListener("click", () => { el.remove(); renumber(); updateWordCount(); });
  $("#scenes").appendChild(el);
  renumber();
}
function renumber() { $$("#scenes .scene .n").forEach((n, i) => (n.textContent = i + 1)); }
function updateWordCount() {
  const words = $$("#scenes textarea").reduce((n, t) => n + (t.value.trim() ? t.value.trim().split(/\s+/).length : 0), 0);
  $("#word-count").textContent = `${words} palavras · ~${Math.round(words / 2.5)}s de narração`;
}

// ------------------------------------------------------------ horários (heatmap)
// Rampa sequencial de um só matiz (azul), escura → clara, sobre superfície escura.
const RAMP = ["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"];
let timesKind = "short";
$$("#times-kind button").forEach((b) => b.addEventListener("click", () => {
  timesKind = b.dataset.kind;
  $$("#times-kind button").forEach((x) => x.classList.toggle("active", x === b));
  loadTimes();
}));

async function loadTimes() {
  const d = await api(`/api/best-times?kind=${timesKind}`);
  $("#times-sub").textContent = d.adaptive
    ? `Ajustado com o desempenho real de ${d.samples} vídeos do canal`
    : "Curva base para público brasileiro — passa a se ajustar sozinha após 6 vídeos publicados com 48h+";
  const max = Math.max(...d.scores.flat());
  const color = (s) => (s < 0.15 ? "#222221" : RAMP[Math.min(RAMP.length - 1, Math.floor((s / max) * RAMP.length))]);
  let html = `<div class="heatmap"><span></span>${Array.from({ length: 24 }, (_, h) => `<span class="hlabel">${h % 3 === 0 ? h : ""}</span>`).join("")}`;
  d.scores.forEach((row, di) => {
    const best = new Set([...row.keys()].sort((a, b) => row[b] - row[a]).slice(0, 3));
    html += `<span class="dlabel">${d.days[di]}</span>`;
    row.forEach((s, h) => {
      html += `<span class="cell ${best.has(h) ? "best" : ""}" style="background:${color(s)}" data-d="${di}" data-h="${h}" data-s="${s}"></span>`;
    });
  });
  $("#heatmap").innerHTML = html + "</div>";
  $$("#heatmap .cell").forEach((c) => {
    c.addEventListener("mouseenter", (e) => showTip(e, `<strong>${d.days[c.dataset.d]} ${String(c.dataset.h).padStart(2, "0")}h</strong><br>Potencial: ${Math.round((c.dataset.s / max) * 100)}%`));
    c.addEventListener("mousemove", moveTip);
    c.addEventListener("mouseleave", hideTip);
  });
  $("#heat-legend").innerHTML = `<span>Menor</span><span class="bar" style="background:linear-gradient(90deg,${RAMP.join(",")})"></span><span>Maior</span>
    <span style="margin-left:16px"><span style="display:inline-block;width:12px;height:12px;outline:2px solid var(--text);outline-offset:-2px;border-radius:3px;vertical-align:middle"></span> 3 melhores do dia</span>`;
  $("#next-slots").innerHTML = d.next_slots.map((s) => `<li>${esc(s)}</li>`).join("") || `<li class="muted">Sem horários livres.</li>`;
}

// ------------------------------------------------------------ desempenho
let perfKind = "short", perfData = null;
$$("#perf-kind button").forEach((b) => b.addEventListener("click", () => {
  perfKind = b.dataset.kind;
  $$("#perf-kind button").forEach((x) => x.classList.toggle("active", x === b));
  renderPerfChart();
}));

async function loadAnalytics() {
  perfData = await api("/api/analytics");
  const t = perfData.totals;
  $("#perf-tiles").innerHTML = [["Vídeos publicados", t.videos], ["Views", t.views], ["Likes", t.likes], ["Comentários", t.comments]]
    .map(([l, v]) => `<div class="tile"><div class="label">${l}</div><div class="value">${fmt(v)}</div></div>`).join("");
  $("#top-table").innerHTML = videoTable(perfData.top, ["when", "kind", "title", "views", "likes"]);
  bindRows($("#top-table"));
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
  const max = Math.max(...rows.map((r) => r.avg_views)) || 1;
  const nice = niceMax(max);
  const bw = iw / 24, bar = Math.max(4, bw - 6);
  const y = (v) => m.t + ih - (v / nice) * ih;
  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Média de views por hora de publicação"><g class="grid">`;
  for (let i = 0; i <= 4; i++) {
    const v = (nice / 4) * i;
    svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"></line>`;
  }
  svg += `</g><g class="axis">`;
  for (let i = 0; i <= 4; i++) { const v = (nice / 4) * i; svg += `<text x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`; }
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
function niceMax(v) { const p = 10 ** Math.floor(Math.log10(v)); return Math.ceil(v / p) * p; }
function compact(v) { return v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(v >= 1e4 ? 0 : 1)}k` : String(Math.round(v)); }

// ------------------------------------------------------------ configurações
const LIST_FIELDS = ["default_tags", "content_mix"];
async function loadConfig() {
  const [o, envs, s] = await Promise.all([api("/api/overview"), api("/api/env"), api("/api/settings")]);
  const ch = o.channel;
  let yt;
  if (o.youtube_connected) {
    yt = `<p>Conectado${ch ? ` a <strong>${esc(ch.title)}</strong>` : ""}.</p>
      <div class="actions"><button class="btn danger" id="yt-disconnect">Desconectar</button></div>`;
  } else {
    yt = `<ol class="muted" style="padding-left:18px;margin-top:0">
        <li>No Google Cloud Console, ative a <em>YouTube Data API v3</em>.</li>
        <li>Crie um <em>ID do cliente OAuth</em> (tipo “App para computador” ou “Aplicativo da Web” com o redirect <code>${esc(location.origin)}/api/youtube/callback</code>).</li>
        <li>Envie o JSON abaixo e clique em Conectar.</li></ol>
      <div class="form">
        <label>client_secret.json ${o.youtube_client_secret ? '<span class="badge published">enviado</span>' : ""}<input type="file" id="yt-secret" accept=".json,application/json"></label>
        <div class="actions"><button class="btn primary" id="yt-connect" ${o.youtube_client_secret ? "" : "disabled"}>Conectar canal</button></div>
      </div>`;
  }
  $("#yt-box").innerHTML = yt;
  $("#yt-secret")?.addEventListener("change", async (e) => {
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    try { await api("/api/youtube/client-secret", { method: "POST", body: fd }); toast("Credencial salva."); loadConfig(); }
    catch (err) { toast(err.message, true); }
  });
  $("#yt-connect")?.addEventListener("click", (e) => busy(e.target, async () => { location.href = (await api("/api/youtube/connect")).url; }));
  $("#yt-disconnect")?.addEventListener("click", (e) => busy(e.target, async () => { await api("/api/youtube/disconnect", { method: "POST" }); loadConfig(); }));

  const row = (ok, label, hint) => `<div class="chip ${ok ? "good" : "warn"}" style="margin:0 6px 6px 0"><span class="ic">${ok ? "✓" : "!"}</span>${esc(label)}</div>${hint ? `<div class="muted small" style="margin-bottom:8px">${hint}</div>` : ""}`;
  $("#env-box").innerHTML =
    row(envs.anthropic_key, `Claude API (${envs.model})`, envs.anthropic_key ? "" : "Defina ANTHROPIC_API_KEY no .env para o piloto automático. Sem ela, crie roteiros pelo MCP.") +
    row(envs.pexels_key, "Pexels", envs.pexels_key ? "" : "Opcional: PEXELS_API_KEY traz vídeos reais de fundo.") +
    row(envs.tts_provider === "edge", `Voz: ${envs.tts_provider}`, "") +
    `<p class="muted small">Dados em ${esc(envs.data_dir)}<br>MCP (HTTP): <code>${esc(location.origin)}/mcp</code></p>`;

  const form = $("#form-settings");
  for (const [k, v] of Object.entries(s)) {
    const input = form.elements[k];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = !!v;
    else input.value = LIST_FIELDS.includes(k) ? v.join(", ") : v;
  }
}
$("#form-settings").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target, out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "number") out[el.name] = Number(el.value);
    else if (LIST_FIELDS.includes(el.name)) out[el.name] = el.value.split(",").map((t) => t.trim()).filter(Boolean);
    else out[el.name] = el.value;
  }
  busy(e.submitter, async () => { await api("/api/settings", { method: "PUT", json: out }); toast("Configurações salvas."); });
});

route();
