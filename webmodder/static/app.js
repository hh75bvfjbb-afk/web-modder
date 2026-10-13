"use strict";
const TOKEN = document.querySelector('meta[name="wm-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch("/api/" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-WM-Token": TOKEN },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const js = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(js.error || r.statusText);
  return js;
}

const STATUS = {
  running: ["работает", "run"], waiting_keys: ["ждёт ключ", "warn"], waiting_approval: ["ждёт подтверждения", "warn"],
  waiting_user: ["ждёт ответа", "warn"], done: ["готово", "ok"], stopped: ["остановлено", "idle"],
  error: ["ошибка", "err"], idle: ["ожидает", "idle"],
};
const KEYSTATUS = { active: ["активен", "ok"], cooldown: ["пауза", "warn"], dead: ["отключён", "err"], disabled: ["выключен", "idle"] };
const statusChip = (s, map = STATUS) => { const [t, c] = map[s] || [s || "?", "idle"]; return `<span class="chip ${c}">${esc(t)}</span>`; };
const fmtN = (n) => (n || 0) >= 1e6 ? (n / 1e6).toFixed(2) + "M" : (n || 0) >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n || 0);
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 60 ? "только что" : s < 3600 ? Math.floor(s / 60) + " мин назад" : s < 86400 ? Math.floor(s / 3600) + " ч назад" : new Date(t * 1000).toLocaleDateString(); };

// --------------------------------------------------------------------------- tiny safe markdown
function md(src) {
  const blocks = [];
  let s = String(src || "").replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => { blocks.push(`<pre><code>${esc(code)}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`; });
  s = esc(s)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  const out = []; let list = null;
  for (const line of s.split("\n")) {
    const h = line.match(/^(#{1,4})\s+(.*)/), li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (li) { if (!list) { list = []; } list.push(`<li>${li[1]}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    if (h) out.push(`<h${h[1].length + 2}>${h[2]}</h${h[1].length + 2}>`);
    else if (line.trim()) out.push(`<p>${line}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[i]);
}

// --------------------------------------------------------------------------- global state + sidebar
let STATE = { keys: [], sessions: [], providers: {} };
async function refreshState() {
  try { STATE = await api("state"); } catch (e) { return; }
  const ks = STATE.keys_summary || {};
  const kb = $("#keyBadge");
  kb.textContent = `ключи: ${ks.llm_ready}/${ks.llm_total}` + (ks.fal_total ? ` · fal ${ks.fal_ready}/${ks.fal_total}` : "");
  kb.className = "badge " + (ks.llm_ready ? "" : "warn");
  const cur = currentSessionId();
  $("#sessionList").innerHTML = STATE.sessions.map((s) => `
    <a href="#/s/${s.id}" class="sess ${s.id === cur ? "active" : ""}">
      <span class="dot ${(STATUS[s.status] || [0, "idle"])[1]}"></span>
      <span class="sess-t">${esc(s.title)}</span>
      <span class="sess-m">${esc((STATUS[s.status] || [s.status])[0])} · ${ago(s.updated)}</span>
    </a>`).join("") || `<div class="muted small pad">Пока пусто</div>`;
  const tk = STATE.toolkit || {};
  $("#toolkitInfo").innerHTML = tk.ready ? `universal-modder ${esc(tk.version || "")} · <a href="#" id="tkUpdate">обновить</a><br><span class="muted">${esc(STATE.shell)}</span>`
    : `<span class="warn-t">загружаю universal-modder…</span>`;
  if (location.hash.startsWith("#/keys") && !document.activeElement?.closest("#keyTable")) renderKeyTable();
}
setInterval(refreshState, 4000);
document.addEventListener("click", async (e) => {
  if (e.target.id !== "tkUpdate") return;
  e.preventDefault();
  e.target.textContent = "обновляю…";
  try { const r = await api("toolkit/update", { method: "POST" }); alert("universal-modder: " + (r.output || "ok") + "\nверсия " + r.version); }
  catch (err) { alert(err.message); }
  refreshState();
});
$("#menuBtn").onclick = () => document.body.classList.toggle("side-open");

// --------------------------------------------------------------------------- router
function currentSessionId() { const m = location.hash.match(/^#\/s\/(\w+)/); return m && m[1]; }
let teardown = null;
function route() {
  document.body.classList.remove("side-open");
  if (teardown) { teardown(); teardown = null; }
  const h = location.hash || "#/new";
  const main = $("#main");
  if (h.startsWith("#/s/")) return viewSession(main, currentSessionId());
  if (h.startsWith("#/keys")) return viewKeys(main);
  if (h.startsWith("#/games")) return viewGames(main);
  return viewNew(main, decodeURIComponent((h.match(/game=([^&]+)/) || [])[1] || ""));
}
window.addEventListener("hashchange", () => { route(); refreshState(); });

function mount(main, tpl) { main.innerHTML = ""; main.appendChild($(tpl).content.cloneNode(true)); }

// --------------------------------------------------------------------------- new mod
const EXAMPLES = [
  ["Terraria", "Добавь ракетницу с самонаводящимися ракетами и тактическую ядерную бомбу, которая оставляет кратер. Спрайты сделай через fal."],
  ["Age of Empires II DE", "Сделай новую цивилизацию с уникальным юнитом, отрендеренным из 3D-модели."],
  ["Minecraft", "Добавь новый биом с собственными мобами и блоками."],
  ["", "Какой движок у игры в C:\\Games\\Foo и моддил ли её кто-нибудь раньше?"],
  ["Stardew Valley", "Добавь новую культуру с уникальным спрайтом и рецептом."],
];
async function viewNew(main, game) {
  mount(main, "#tpl-new");
  const f = $("#newForm");
  if (game) f.game.value = game;
  $("#examples").innerHTML = EXAMPLES.map((e, i) => `<button type="button" class="chip-btn" data-i="${i}">${esc(e[0] || "Разведка")}: ${esc(e[1].slice(0, 46))}…</button>`).join("");
  $("#examples").onclick = (ev) => { const b = ev.target.closest("[data-i]"); if (!b) return; const e = EXAMPLES[b.dataset.i]; f.game.value = e[0]; f.prompt.value = e[1]; };
  const ks = STATE.keys_summary || {};
  if (!ks.llm_total) $("#newHint").innerHTML = `Сначала <a href="#/keys">добавьте ключ</a> — без него агент будет ждать.`;
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const b = Object.fromEntries(new FormData(f));
    try { const s = await api("sessions", { method: "POST", body: b }); location.hash = "#/s/" + s.id; }
    catch (e) { $("#newHint").textContent = e.message; }
  };
  api("games").then((r) => { $("#gameList").innerHTML = (r.games || []).map((g) => `<option value="${esc(g.name)}">${esc(g.store)} · ${esc(g.path)}</option>`).join(""); }).catch(() => {});
}

// --------------------------------------------------------------------------- session view
function summarizeInput(name, input) {
  input = input || {};
  if (name === "shell") return input.command;
  if (name === "um") return "um " + input.args;
  if (name === "read_skill") return [input.name, input.file].filter(Boolean).join(" / ");
  if (name === "fetch_url") return input.url;
  if (name === "ask_user") return input.question;
  return input.path || JSON.stringify(input).slice(0, 160);
}
const TOOL_ICON = { shell: "⌘", um: "▣", read_file: "📄", write_file: "✎", edit_file: "✎", list_dir: "📁", read_skill: "📘", view_image: "🖼", fetch_url: "🌐", ask_user: "❓" };

async function viewSession(main, sid) {
  mount(main, "#tpl-session");
  const tl = $("#timeline");
  let info, lastSeq = 0, es = null, pending = null;
  const tools = {};
  try { info = await api("sessions/" + sid); } catch (e) { tl.innerHTML = `<div class="sys err">${esc(e.message)}</div>`; return; }
  $("#sTitle").textContent = info.title;
  $("#sMode").value = info.mode || "smart";
  const setMeta = (u) => { u = u || {}; $("#sMeta").textContent = `${info.workdir} · токены: ${fmtN(u.input)} вход / ${fmtN(u.output)} выход · запросов: ${u.requests || 0}`; };
  setMeta(info.usage);
  const setStatus = (st) => {
    info.status = st;
    $("#sStatus").innerHTML = statusChip(st);
    const active = ["running", "waiting_keys", "waiting_approval", "waiting_user"].includes(st);
    $("#sStop").hidden = !active; $("#sResume").hidden = active;
    if (st !== "running") $("#live").hidden = true;
  };
  setStatus(info.status);

  const atBottom = () => tl.scrollHeight - tl.scrollTop - tl.clientHeight < 120;
  const add = (html, cls) => { const stick = atBottom(); const d = document.createElement("div"); d.className = cls; d.innerHTML = html; tl.appendChild(d); if (stick) tl.scrollTop = tl.scrollHeight; return d; };

  function toolCard(ev) {
    const d = add("", "tool");
    d.dataset.id = ev.id;
    d.innerHTML = `<div class="tool-h"><span class="ti">${TOOL_ICON[ev.name] || "•"}</span><b>${esc(ev.name)}</b>
      <code class="targ">${esc(summarizeInput(ev.name, ev.input))}</code><span class="tstate"></span></div>
      <div class="tool-approve" hidden></div><pre class="tout" hidden></pre>`;
    d.querySelector(".tool-h").onclick = () => { const o = d.querySelector(".tout"); if (o.textContent) o.hidden = !o.hidden; };
    tools[ev.id] = d;
    setToolState(d, ev.state);
    return d;
  }
  function setToolState(d, st) {
    const map = { queued: ["в очереди", "idle"], running: ["выполняется", "run"], done: ["готово", "ok"], error: ["ошибка", "err"], denied: ["запрещено", "err"], waiting: ["ждёт подтверждения", "warn"] };
    const [t, c] = map[st] || [st, "idle"];
    d.querySelector(".tstate").innerHTML = `<span class="chip ${c}">${t}</span>`;
    d.className = "tool " + c;
  }
  function showApproval(ev) {
    const d = tools[ev.id] || toolCard({ id: ev.id, name: ev.name, input: ev.input, state: "waiting" });
    setToolState(d, "waiting");
    const box = d.querySelector(".tool-approve");
    box.hidden = false;
    const full = ev.name === "shell" ? ev.input.command : ev.name === "um" ? "um " + ev.input.args : JSON.stringify(ev.input, null, 2);
    box.innerHTML = `<div class="small muted">Агент хочет выполнить:</div><pre>${esc(full)}</pre>
      <div class="row"><button class="btn primary" data-d="allow">Разрешить</button>
      <button class="btn" data-d="always">Разрешать «${esc(ev.name)}» всегда</button>
      <input class="note" placeholder="комментарий (необязательно)">
      <button class="btn danger" data-d="deny">Запретить</button></div>`;
    box.onclick = async (e) => {
      const b = e.target.closest("[data-d]"); if (!b) return;
      box.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try { await api(`sessions/${sid}/approve`, { method: "POST", body: { decision: b.dataset.d, note: box.querySelector(".note").value } }); box.hidden = true; }
      catch (err) { alert(err.message); box.querySelectorAll("button").forEach((x) => (x.disabled = false)); }
    };
    d.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  function showAsk(ev) {
    const d = add(`<div class="ask-q">❓ ${md(ev.question)}</div><div class="row opts">${(ev.options || []).map((o) => `<button class="btn" data-a="${esc(o)}">${esc(o)}</button>`).join("")}</div>
      <form class="row"><input placeholder="ваш ответ…"><button class="btn primary">Ответить</button></form>`, "ask");
    const send = async (text) => { if (!text) return; d.querySelectorAll("button,input").forEach((x) => (x.disabled = true)); try { await api(`sessions/${sid}/answer`, { method: "POST", body: { text } }); d.classList.add("answered"); } catch (e) { alert(e.message); d.querySelectorAll("button,input").forEach((x) => (x.disabled = false)); } };
    d.querySelector(".opts").onclick = (e) => { const b = e.target.closest("[data-a]"); if (b) send(b.dataset.a); };
    d.querySelector("form").onsubmit = (e) => { e.preventDefault(); send(d.querySelector("input").value.trim()); };
    d.scrollIntoView({ block: "center" });
    return d;
  }

  function handle(ev, live) {
    if (ev.seq) lastSeq = Math.max(lastSeq, ev.seq);
    switch (ev.type) {
      case "user": add(md(ev.text), "msg user"); break;
      case "assistant": add(md(ev.text), "msg bot"); break;
      case "progress": add(esc(ev.text), "progress"); break;
      case "tool": toolCard(ev); break;
      case "tool_update": {
        const d = tools[ev.id]; if (!d) break;
        setToolState(d, ev.state);
        if (ev.state !== "running") d.querySelector(".tool-approve").hidden = true;
        if (ev.output) { const o = d.querySelector(".tout"); o.textContent = ev.output; o.hidden = ev.state === "done" && !d.classList.contains("open"); }
        if (ev.state === "running" && live) { $("#live").hidden = false; $("#live").textContent = "выполняется: " + d.querySelector(".targ").textContent; }
        else if (live) $("#live").hidden = true;
        break;
      }
      case "tool_output": { const d = tools[ev.id]; if (d) { const o = d.querySelector(".tout"); o.hidden = false; o.textContent = ev.text; } break; }
      case "approval": pending = ev; showApproval(ev); break;
      case "ask": pending = ev; showAsk(ev); break;
      case "thinking": if (live) { $("#live").hidden = false; $("#live").textContent = "думает · " + ev.text; } break;
      case "usage": setMeta(ev.usage); break;
      case "status": setStatus(ev.status); if (live) refreshState(); if (ev.status !== "waiting_approval" && ev.status !== "waiting_user") pending = null; break;
      case "key": add("🔑 " + esc(ev.text), "sys"); break;
      case "key_error": add("⚠ " + esc(ev.text) + (ev.detail ? `<div class="small muted">${esc(ev.detail)}</div>` : ""), "sys warn"); break;
      case "wait": add("⏳ " + esc(ev.text) + (ev.seconds == null ? ` <a href="#/keys">Добавить ключ →</a>` : ""), "sys warn"); break;
      case "info": add("ℹ " + esc(ev.text), "sys"); break;
      case "error": add("✖ " + esc(ev.text), "sys err"); break;
    }
  }
  // history: answered approvals/questions must not show live buttons
  const evs = info.events;
  const resolvedTools = new Set(evs.filter((e) => e.type === "tool_update" && e.state !== "running" && e.state !== "queued").map((e) => e.id));
  const answered = new Set(evs.filter((e) => e.type === "tool_update").map((e) => e.id));
  for (const ev of evs) {
    if (ev.type === "approval" && (resolvedTools.has(ev.id) || evs.some((x) => x.type === "tool_update" && x.id === ev.id && x.state === "running"))) { lastSeq = ev.seq; continue; }
    if (ev.type === "ask" && answered.has(ev.id)) { const d = showAsk(ev); d.classList.add("answered"); d.querySelectorAll("button,input").forEach((x) => (x.disabled = true)); lastSeq = ev.seq; continue; }
    handle(ev, false);
  }
  tl.scrollTop = tl.scrollHeight;

  function connect() {
    es = new EventSource(`/api/sessions/${sid}/stream?after=${lastSeq}`);
    es.onmessage = (m) => { const ev = JSON.parse(m.data); if (ev.seq <= lastSeq && !ev.ephemeral) return; handle(ev, true); };
    es.onerror = () => { es.close(); setTimeout(() => { if (currentSessionId() === sid) connect(); }, 2000); };
  }
  connect();
  teardown = () => es && es.close();

  $("#composer").onsubmit = async (e) => {
    e.preventDefault();
    const t = $("#msg").value.trim(); if (!t) return;
    $("#msg").value = "";
    try {
      if (pending && pending.type === "ask") await api(`sessions/${sid}/answer`, { method: "POST", body: { text: t } });
      else await api(`sessions/${sid}/message`, { method: "POST", body: { text: t } });
    } catch (err) { alert(err.message); }
  };
  $("#msg").onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#composer").requestSubmit(); };
  $("#sStop").onclick = () => api(`sessions/${sid}/stop`, { method: "POST" });
  $("#sResume").onclick = () => api(`sessions/${sid}/resume`, { method: "POST" });
  $("#sMode").onchange = (e) => api(`sessions/${sid}/settings`, { method: "POST", body: { mode: e.target.value } });
  $("#sDelete").onclick = async () => { if (confirm("Удалить сессию? Файлы мода в рабочей папке останутся.")) { await api("sessions/" + sid, { method: "DELETE" }); location.hash = "#/new"; } };

  // files panel
  const panel = $("#filesPanel");
  async function openDir(rel) {
    const r = await api(`sessions/${sid}/files?path=${encodeURIComponent(rel)}`);
    $("#filesPath").textContent = r.root + (rel ? "/" + rel : "");
    const up = rel ? `<a href="#" data-dir="${esc(rel.split("/").slice(0, -1).join("/"))}">⬑ ..</a>` : "";
    $("#filesList").innerHTML = up + r.items.map((i) => i.dir ? `<a href="#" data-dir="${esc(i.path)}">📁 ${esc(i.name)}</a>`
      : `<a href="#" data-file="${esc(i.path)}">📄 ${esc(i.name)} <span class="muted">${fmtN(i.size)}B</span></a>`).join("");
  }
  $("#filesList").onclick = async (e) => {
    const a = e.target.closest("a"); if (!a) return; e.preventDefault();
    if (a.dataset.dir !== undefined) return openDir(a.dataset.dir);
    const url = `/api/sessions/${sid}/raw?path=${encodeURIComponent(a.dataset.file)}`;
    if (/\.(png|jpe?g|gif|webp|bmp)$/i.test(a.dataset.file)) $("#filePreview").innerHTML = `<a href="${url}" target="_blank"><img src="${url}"></a>`;
    else if (/\.(mp4|webm)$/i.test(a.dataset.file)) $("#filePreview").innerHTML = `<video src="${url}" controls></video>`;
    else { const t = await (await fetch(url)).text(); $("#filePreview").innerHTML = `<pre>${esc(t.slice(0, 60000))}</pre>`; }
  };
  $("#sFiles").onclick = () => { panel.hidden = !panel.hidden; if (!panel.hidden) openDir(""); };
  $("#filesClose").onclick = () => (panel.hidden = true);
}

// --------------------------------------------------------------------------- keys
function viewKeys(main) {
  mount(main, "#tpl-keys");
  const sel = $("#provSelect");
  sel.innerHTML += Object.entries(STATE.providers || {}).map(([k, v]) => `<option value="${k}">${esc(v.name)}</option>`).join("");
  const fl = $("#flForm");
  $("#flState").innerHTML = STATE.freellmapi_added ? `<span class="chip ok">подключён</span>`
    : STATE.freellmapi_running ? `<span class="chip ok">найден: ${esc(STATE.freellmapi_running)}</span>`
    : `<span class="chip idle">не запущен (ищу на портах 31415 и 3001)</span>`;
  if (STATE.freellmapi_running) fl.base.value = STATE.freellmapi_running;
  fl.onsubmit = async (e) => {
    e.preventDefault();
    const b = Object.fromEntries(new FormData(fl));
    b.provider = "freellmapi";
    $("#flMsg").textContent = "проверяю…";
    try {
      const r = await api("keys", { method: "POST", body: b });
      $("#flMsg").textContent = r.errors.length ? r.errors.join("; ") : "подключено — FreeLLMAPI стоит первым в очереди";
      if (r.added.length) fl.secret.value = "";
      STATE.keys = r.keys; renderKeyTable(); refreshState();
      for (const id of r.added) loadRouter(id);
    } catch (err) { $("#flMsg").textContent = err.message; }
  };
  const f = $("#keyForm");
  f.onsubmit = async (e) => {
    e.preventDefault();
    const b = Object.fromEntries(new FormData(f));
    $("#keyMsg").textContent = "проверяю…";
    try {
      const r = await api("keys", { method: "POST", body: b });
      $("#keyMsg").textContent = `добавлено: ${r.added.length}` + (r.errors.length ? ` · ошибки: ${r.errors.join("; ")}` : "");
      if (r.added.length) f.secret.value = "";
      STATE.keys = r.keys; renderKeyTable(); refreshState();
    } catch (err) { $("#keyMsg").textContent = err.message; }
  };
  renderKeyTable();
  for (const k of STATE.keys || []) if (k.provider === "freellmapi") loadRouter(k.id);
  $("#assetsCheck").onclick = loadAssets;
  loadAssets();
}
const ROUTER = {};  // key id -> last FreeLLMAPI status, kept across the periodic re-render
async function loadRouter(id) {
  try { ROUTER[id] = await api(`keys/${id}/router`); } catch (e) { ROUTER[id] = { ok: false, error: e.message }; }
  const box = document.querySelector(`.key[data-id="${id}"] .router-box`);
  if (box) box.innerHTML = renderRouter(ROUTER[id]);
}
function renderKeyTable() {
  const box = $("#keyTable"); if (!box) return;
  const keys = [...(STATE.keys || [])].sort((a, b) => (a.priority || 0) - (b.priority || 0));
  if (!keys.length) { box.innerHTML = `<div class="card muted">Ключей пока нет.</div>`; return; }
  box.innerHTML = `<div class="keys">` + keys.map((k, i) => {
    const used = k.month_tokens || 0;
    const refill = new Date(k.refill_at * 1000).toLocaleDateString("ru-RU");
    const pct = k.budget ? Math.min(100, Math.round(used / k.budget * 100)) : 0;
    const strategies = k.provider === "freellmapi" ? ["auto:smart", "auto", "auto:reliable", "auto:balanced", "auto:fast"] : [];
    if (strategies.length) k.models = [...strategies, ...(k.models || []).filter((m) => !strategies.includes(m))];
    const models = (k.models || []).length ? `<select data-f="model">${[k.model, ...k.models.filter((m) => m !== k.model)].map((m) => `<option ${m === k.model ? "selected" : ""}>${esc(m)}</option>`).join("")}</select>`
      : `<input data-f="model" value="${esc(k.model)}" placeholder="модель">`;
    return `<div class="key card" data-id="${k.id}">
      <div class="key-h"><span class="prio">#${i + 1}</span><b>${esc(k.label)}</b><span class="muted">${k.label !== k.provider_name ? esc(k.provider_name) + " · " : ""}<code>${esc(k.masked)}</code></span>
        <span class="spacer"></span>${statusChip(k.status, KEYSTATUS)}${k.cooldown_left ? `<span class="muted small">ещё ${k.cooldown_left} с</span>` : ""}</div>
      <div class="key-b">
        ${k.kind === "fal" ? `<span class="muted">для ассетов (um fal)</span>` : `<label>Модель ${models}</label>`}
        <label>Лимит токенов в месяц <input data-f="budget" type="number" min="0" step="1000000" value="${k.budget || ""}" placeholder="без лимита"></label>
        <div class="usage">
          <div>В этом месяце: <b>${fmtN(used)}</b>${k.budget ? ` из ${fmtN(k.budget)}` : ""} токенов · ${k.month_requests || 0} запросов</div>
          ${k.budget ? `<div class="meter ${pct >= 100 ? "full" : pct >= 80 ? "high" : ""}"><span style="width:${pct}%"></span></div>
          <div class="small">${pct >= 100 ? "исчерпан — " : ""}пополнится ${esc(refill)}</div>` : ""}
          <div class="small">всего: ${fmtN(k.usage?.input)} вход · ${fmtN(k.usage?.output)} выход · ${k.usage?.errors || 0} ошибок</div>
        </div>
      </div>
      ${k.last_error ? `<div class="small err-t">${esc(k.last_error)}</div>` : ""}
      <div class="row">
        <button class="btn" data-a="up" ${i === 0 ? "disabled" : ""}>↑</button><button class="btn" data-a="down" ${i === keys.length - 1 ? "disabled" : ""}>↓</button>
        <button class="btn" data-a="check">Проверить и включить</button>
        ${k.provider === "freellmapi" ? `<button class="btn" data-a="router">Состояние роутера</button>` : ""}
        ${k.status === "disabled" ? `<button class="btn" data-a="on">Включить</button>` : `<button class="btn" data-a="off">Выключить</button>`}
        <button class="btn danger" data-a="del">Удалить</button>
      </div><div class="router-box">${ROUTER[k.id] ? renderRouter(ROUTER[k.id]) : ""}</div></div>`;
  }).join("") + `</div>`;
  box.onchange = async (e) => {
    const el = e.target.closest("[data-f]"); if (!el) return;
    const id = el.closest(".key").dataset.id;
    STATE = { ...STATE, ...(await api("keys/" + id, { method: "PATCH", body: { [el.dataset.f]: el.value } })) };
    renderKeyTable(); refreshState();
  };
  box.onclick = async (e) => {
    const b = e.target.closest("[data-a]"); if (!b) return;
    const id = b.closest(".key").dataset.id, a = b.dataset.a;
    b.disabled = true;
    try {
      let r;
      if (a === "router") { await loadRouter(id); b.disabled = false; return; }
      if (a === "check") r = await api(`keys/${id}/check`, { method: "POST" });
      else if (a === "on" || a === "off") r = await api("keys/" + id, { method: "PATCH", body: { enabled: a === "on" } });
      else if (a === "del") { if (!confirm("Удалить ключ?")) { b.disabled = false; return; } r = await api("keys/" + id, { method: "DELETE" }); }
      else {
        const idx = keys.findIndex((k) => k.id === id), j = a === "up" ? idx - 1 : idx + 1;
        await api("keys/" + keys[j].id, { method: "PATCH", body: { priority: idx } });
        r = await api("keys/" + id, { method: "PATCH", body: { priority: j } });
        for (const [n, k] of keys.entries()) if (n !== idx && n !== j && k.priority !== n) r = await api("keys/" + k.id, { method: "PATCH", body: { priority: n } });
      }
      STATE.keys = r.keys; renderKeyTable(); refreshState();
    } catch (err) { alert(err.message); b.disabled = false; }
  };
}

async function loadAssets() {
  const box = $("#assetsStatus"); if (!box) return;
  box.innerHTML = `<span class="muted small">проверяю…</span>`;
  let st;
  try { st = await api("assets/status"); } catch (e) { box.textContent = e.message; return; }
  const c = st.comfy || {};
  const row = (ok, title, how) => `<div class="asset ${ok ? "ok" : ""}"><span class="dot ${ok ? "ok" : "idle"}"></span><b>${title}</b><span class="muted small">${how}</span></div>`;
  box.innerHTML = [
    row(st.freellmapi, "Картинки, голос, видео — FreeLLMAPI", st.freellmapi ? "подключён" : "добавьте ключ FreeLLMAPI выше"),
    row(c.up, "ComfyUI (ваша видеокарта)", c.up ? esc(c.url) : "не запущен — см. ИНСТРУКЦИЯ, часть «Ассеты»"),
    row(!!c.image_model, "Картинки локально — SDXL", c.image_model ? esc(c.image_model) : "нет модели в ComfyUI/models/checkpoints"),
    row(c.stable_audio, "Звуки локально — Stable Audio Open", c.stable_audio ? "есть" : "нет модели"),
    row(c.ace_step, "Музыка — ACE-Step", c.ace_step ? "есть" : "нет модели"),
    row(c.hunyuan3d, "3D — Hunyuan3D-2", c.hunyuan3d ? "есть" : "нет модели"),
    row(true, "Звуки — Openverse + ретро-генератор", "без ключей: записи из Openverse, а если их нет — 8-битный звук"),
    row(st.freesound, "Звуки — Freesound (по желанию)", st.freesound ? "ключ есть" : "необязательно"),
    row(st.edge_tts, "Голос — edge-tts", st.edge_tts ? "встроен" : "нет"),
    row(st.onnxruntime, "Удаление фона — ISNet", st.onnxruntime ? (st.rmbg_model ? "модель скачана" : "скачается при первом спрайте") : "нет onnxruntime"),
    row(true, "Увеличение — Real-ESRGAN", st.realesrgan ? "скачан" : "скачается при первом использовании"),
    row(st.ffmpeg, "Конвертация звука — ffmpeg", st.ffmpeg ? "есть" : "по желанию: winget install ffmpeg"),
    row(st.fal, "fal.ai (запасной)", st.fal ? "ключ есть" : "не обязателен"),
  ].join("");
}
function renderRouter(st) {
  if (!st.ok) return `<div class="small err-t">Роутер не ответил: ${esc(st.error)}</div>`;
  const c = st.counts || {};
  const prov = (st.providers || []).map((p) => `<span class="chip ${p.status === "healthy" ? "ok" : p.status === "rate_limited" ? "warn" : p.status === "invalid" ? "err" : "idle"}"
    title="${esc(p.resume_at ? "до " + new Date(p.resume_at).toLocaleString("ru-RU") : p.last_error || "")}">${esc(p.name)}</span>`).join(" ");
  const pools = (st.pools || []).filter((x) => x.limit).map((x) => {
    const pct = x.remaining_pct ?? (x.limit ? Math.round((x.remaining / x.limit) * 100) : 0);
    return `<div class="pool"><span>${esc(x.platform)}${x.pool && x.pool !== x.platform ? " · " + esc(x.pool) : ""}</span>
      <div class="meter ${x.low_balance ? "full" : ""}"><span style="width:${pct}%"></span></div>
      <span class="small muted">${fmtN(x.remaining)} / ${fmtN(x.limit)}${x.reset_at ? " · сброс " + new Date(x.reset_at).toLocaleString("ru-RU") : ""}</span></div>`;
  }).join("");
  return `<div class="small">Провайдеры: ${c.healthy || 0} в работе · ${c.rate_limited || 0} на паузе · ${c.invalid || 0} с ошибкой</div>
    <div class="row" style="margin:6px 0">${prov || '<span class="muted small">в панели FreeLLMAPI ещё нет ключей провайдеров</span>'}</div>${pools}`;
}

// --------------------------------------------------------------------------- games + knowledge base
function viewGames(main) {
  mount(main, "#tpl-games");
  const show = (games) => {
    $("#gamesList").innerHTML = games.length ? games.map((g) => `<div class="game card"><b>${esc(g.name)}</b><span class="muted small">${esc(g.store)} · ${esc(g.path)}</span>
      <a class="btn" href="#/new?game=${encodeURIComponent(g.name)}">Модифицировать</a></div>`).join("")
      : `<div class="card muted">Игры не найдены. Можно указать папку игры вручную при создании мода.</div>`;
  };
  const load = async (refresh) => { $("#scanMsg").textContent = "сканирую…"; try { const r = await api("games" + (refresh ? "?refresh=1" : "")); show(r.games || []); $("#scanMsg").textContent = `найдено: ${(r.games || []).length}`; } catch (e) { $("#scanMsg").textContent = e.message; } };
  $("#scanBtn").onclick = () => load(true);
  load(false);
  $("#kbForm").onsubmit = async (e) => {
    e.preventDefault();
    $("#kbResults").innerHTML = `<div class="muted">ищу…</div>`;
    const r = await api("kb?q=" + encodeURIComponent(e.target.q.value));
    $("#kbResults").innerHTML = (r.results || []).map((x) => `<div class="card kb"><b>${esc(x.title)}</b>
      <div class="muted small">${esc([x.game, x.engine, x.route, x.status].filter(Boolean).join(" · "))} — <code>${esc(x.path)}</code></div>
      ${(x.hits || []).slice(0, 3).map((h) => `<div class="small">${esc(h)}</div>`).join("")}
      ${x.game ? `<a class="btn" href="#/new?game=${encodeURIComponent(x.game)}">Модифицировать ${esc(x.game)}</a>` : ""}</div>`).join("")
      || `<div class="card muted">Ничего не найдено${r.error ? ": " + esc(r.error) : ""}.</div>`;
  };
}

refreshState().then(route);
