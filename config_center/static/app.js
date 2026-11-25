/* 配置中心操作页：状态渲染、来源抽屉、分步发布向导、批量回滚、模拟客户端 SSE。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.message || res.statusText), data);
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (t) => new Date(t * 1000).toLocaleString("zh-CN", { hour12: false });

let state = null;
let pendingChanges = [];
let currentPlan = null;
const sseClients = new Map(); // app -> {id, source, logEl}

// ---------------------------------------------------------------- 渲染
async function refresh() {
  state = await api("/api/state");
  renderViews();
  renderEvents();
  renderClients();
  $("#online-total").textContent = `在线客户端 ${state.online_clients_total}`;
  const keys = [...state.global_keys, ...new Set(
    Object.values(state.overrides).flatMap((m) => Object.keys(m)))];
  $("#key-list").innerHTML = [...new Set(keys)].map((k) => `<option value="${esc(k)}">`).join("");
}

function renderViews() {
  const wrap = $("#views");
  wrap.innerHTML = Object.entries(state.views).map(([app, view]) => {
    const rows = Object.entries(view.keys).map(([key, info]) => {
      const srcClass = info.source?.startsWith("override") ? "override"
        : info.source === "global" ? "global" : info.source;
      const constraint = info.constraint
        ? `<span class="${info.version && semverSatisfies(info.version, info.constraint)
            ? "constraint-ok" : "constraint-bad"}">约束 ${esc(info.constraint)}</span>`
        : "";
      return `<tr>
        <td><span class="link" data-source="${esc(app)}|${esc(key)}">${esc(key)}</span></td>
        <td><span class="kv">${esc(info.version ?? "—")}</span></td>
        <td class="kv">${esc(info.value ?? "")}</td>
        <td class="deps">${(info.depends_on || []).map(esc).join(" → ") || ""}</td>
        <td class="src"><span class="chip ${srcClass}">${esc(info.source)}</span> ${constraint}</td>
      </tr>`;
    }).join("");
    return `<div class="app-block">
      <div class="app-head">
        <b>📦 ${esc(app)}</b>
        <span class="req">在线客户端 ${view.online_clients} · 依赖图 ${Object.keys(view.graph).length} 节点</span>
      </div>
      <table class="keys"><thead><tr>
        <th>键</th><th>生效版本</th><th>值</th><th>依赖</th><th>来源 / 约束</th>
      </tr></thead><tbody>${rows}</tbody></table>
    </div>`;
  }).join("");

  $$("[data-source]", wrap).forEach((el) => el.addEventListener("click", () => {
    const [app, key] = el.dataset.source.split("|");
    openSource(app, key);
  }));
}

function renderEvents() {
  const tbody = $("#events-table tbody");
  const releases = state.events.filter((e) => e.type === "release");
  tbody.innerHTML = state.events.slice().reverse().map((ev) => {
    const affected = Object.entries(ev.affected_apps || {})
      .map(([app, keys]) => `${esc(app)} <span class="deps">(${keys.map(esc).join(", ")})</span>`)
      .join("<br>") || '<span class="muted">无</span>';
    const changes = (ev.changes || ev.inverse_changes || []).map((c) => {
      const label = c.action
        ? `${esc(c.action)} ${esc(c.key ?? "")}`
        : `${esc(c.scope)}${c.app ? ":" + esc(c.app) : ""}:${esc(c.key)}@${esc(c.version)}`;
      return `<div class="kv">${label}</div>`;
    }).join("");
    const checkbox = ev.type === "release"
      ? `<input type="checkbox" class="rollback-pick" value="${esc(ev.id)}">` : "";
    const typeChip = ev.type === "release"
      ? '<span class="chip global">发布</span>'
      : '<span class="chip override">回滚</span>';
    return `<tr>
      <td>${checkbox}</td><td class="deps">${fmtTime(ev.at)}</td><td>${typeChip}</td>
      <td>${changes}</td><td>${affected}</td>
      <td class="deps">${(ev.rolled_back_release_ids || []).length
        ? "回滚发布 " + ev.rolled_back_release_ids.map(esc).join(", ") : "—"}</td>
      <td>${esc(ev.status)}</td>
    </tr>`;
  }).join("");

  const picked = $$(".rollback-pick", tbody);
  const hint = $("#rollback-hint");
  const sync = () => {
    const n = picked.filter((c) => c.checked).length;
    $("#btn-rollback").disabled = n === 0;
    hint.textContent = n ? `已选 ${n} 次发布，将作为一批整体回滚` : "";
  };
  picked.forEach((c) => c.addEventListener("change", sync));
  void releases;
}

function renderClients() {
  const wrap = $("#clients");
  wrap.innerHTML = Object.keys(state.views).map((app) => `
    <div class="client" data-client="${esc(app)}">
      <div class="client-head">
        <span><span class="dot ${sseClients.has(app) ? "on" : ""}"></span><b>${esc(app)}</b></span>
        <button class="small" data-toggle="${esc(app)}">
          ${sseClients.has(app) ? "断开" : "连接"}
        </button>
      </div>
      <div class="log" data-log="${esc(app)}"></div>
    </div>`).join("");
  $$("[data-toggle]").forEach((btn) => btn.addEventListener("click", () => toggleClient(btn.dataset.toggle)));
  // 恢复已有日志的 DOM 引用
  for (const [app, client] of sseClients) client.logEl = $(`[data-log="${app}"]`, wrap);
}

function logLine(app, html, cls = "") {
  const client = sseClients.get(app);
  if (!client || !client.logEl) return;
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.innerHTML = `[${new Date().toLocaleTimeString("zh-CN", { hour12: false })}] ${html}`;
  client.logEl.appendChild(line);
  client.logEl.scrollTop = client.logEl.scrollHeight;
}

async function toggleClient(app) {
  if (sseClients.has(app)) {
    sseClients.get(app).source.close();
    sseClients.delete(app);
  } else {
    const es = new EventSource(`/api/subscribe?app=${encodeURIComponent(app)}`);
    const client = { source: es, logEl: null };
    sseClients.set(app, client);
    es.addEventListener("hello", (e) => {
      const d = JSON.parse(e.data);
      client.id = d.client_id;
      logLine(app, `SSE 已连接 client_id=${d.client_id}`);
    });
    es.addEventListener("refresh", (e) => {
      const d = JSON.parse(e.data);
      const keys = d.affected_apps[app] || [];
      logLine(app, `⚡ 收到 ${d.event_type} 刷新通知（事件 ${d.event_id}），刷新键: ${keys.join(", ")}`, "refresh");
    });
    es.onerror = () => logLine(app, "连接中断，浏览器将自动重连…");
  }
  renderClients();
}

// ---------------------------------------------------------------- 来源抽屉
async function openSource(app, key) {
  const data = await api(`/api/key-sources?app=${encodeURIComponent(app)}`);
  const info = data[key];
  $("#src-key").textContent = key;
  $("#src-app").textContent = app;
  if (!info || info.source === "unresolvable") {
    $("#src-body").innerHTML = '<p class="muted">该键在当前应用下无法解析。</p>';
  } else {
    const override = info.override
      ? `<div class="src-block"><b>应用覆盖（当前生效）</b><pre>${esc(JSON.stringify(info.override, null, 2))}</pre></div>`
      : '<p class="muted">该应用未覆盖此键，穿透使用全局值。</p>';
    $("#src-body").innerHTML = `
      <p>来源层级：<span class="chip ${info.source.startsWith("override") ? "override" : "global"}">${esc(info.source)}</span></p>
      <div class="src-block"><b>当前生效版本</b><pre>${esc(JSON.stringify(info.effective, null, 2))}</pre></div>
      ${override}
      <div class="src-block"><b>全局层全部版本（${info.global_versions.length}）</b>
        ${info.global_versions.map((r) => `<pre>v${esc(r.version)} = ${esc(r.value)}  依赖: [${(r.depends_on || []).join(", ")}]</pre>`).join("")}
      </div>`;
  }
  $("#source-dialog").showModal();
}

// ---------------------------------------------------------------- 发布向导
function openWizard() {
  pendingChanges = [];
  currentPlan = null;
  gotoStep(1);
  renderPendingChanges();
  $("#f-app").innerHTML = Object.keys(state.views).map((a) => `<option>${esc(a)}</option>`).join("");
  $("#wizard").showModal();
}

function gotoStep(n) {
  $$(".pane").forEach((p) => p.classList.toggle("hidden", Number(p.dataset.pane) !== n));
  $$(".step").forEach((s) => s.classList.toggle("active", Number(s.dataset.step) <= n));
}

function readFormChange() {
  const scope = $("#f-scope").value;
  return {
    scope,
    app: scope === "override" ? $("#f-app").value : null,
    key: $("#f-key").value.trim(),
    version: $("#f-version").value.trim(),
    value: $("#f-value").value,
    depends_on: $("#f-deps").value.split(",").map((s) => s.trim()).filter(Boolean),
    note: $("#f-note").value,
  };
}

function renderPendingChanges() {
  $("#change-list tbody").innerHTML = pendingChanges.map((c, i) => `
    <tr><td>${esc(c.scope)}</td><td>${esc(c.app ?? "—")}</td>
    <td class="kv">${esc(c.key)}</td><td class="kv">${esc(c.version)}</td>
    <td class="kv">${esc(c.value)}</td><td class="deps">${c.depends_on.map(esc).join(", ")}</td>
    <td><button class="small" data-remove="${i}">移除</button></td></tr>`).join("")
    || '<tr><td colspan="7" class="muted">清单为空，请先加入改动</td></tr>';
  $$("[data-remove]").forEach((b) => b.addEventListener("click", () => {
    pendingChanges.splice(Number(b.dataset.remove), 1);
    renderPendingChanges();
  }));
}

async function analyze() {
  if (!pendingChanges.length) { alert("请先把至少一项改动加入清单"); return; }
  gotoStep(2);
  const body = $("#plan-body");
  body.innerHTML = '<p class="muted">分析中…</p>';
  try {
    currentPlan = await api("/api/prepare", { method: "POST", body: { changes: pendingChanges } });
  } catch (e) {
    body.innerHTML = `<div class="plan-section bad"><b>请求失败</b><pre>${esc(e.message)}</pre></div>`;
    return;
  }
  const p = currentPlan;
  const blocks = [];
  const block = (cls, title, html) => `<div class="plan-section ${cls}"><h3 style="margin-top:0">${title}</h3>${html}</div>`;

  blocks.push(block("good", "直接命中",
    Object.entries(p.direct_hits).map(([app, hits]) =>
      `<div><b>${esc(app)}</b>: ${hits.map((h) =>
        `${esc(h.key)} ${esc(h.old_version ?? "∅")} → ${esc(h.new_version)}（${esc(h.source)}）`).join("；")}</div>`
    ).join("") || '<span class="muted">无</span>'));

  blocks.push(block(p.ok ? "good" : "bad", "完整影响范围（沿反向依赖传播）",
    Object.entries(p.affected_apps).map(([app, keys]) =>
      `<div><b>${esc(app)}</b> 受影响键: <span class="kv">${keys.map(esc).join(", ")}</span></div>`
    ).join("") || '<span class="muted">无应用受影响</span>'));

  if (p.cycles.length) {
    blocks.push(block("bad", "⛔ 循环引用（将阻止发布）",
      p.cycles.map((c) => `<pre>${esc(c.join(" → "))}</pre>`).join("")));
  }
  if (p.violations.length) {
    blocks.push(block("bad", "⛔ 版本不相容（将阻止发布）",
      p.violations.map((v) =>
        `<div class="kv">${esc(v.app)} 要求 ${esc(v.key)} ${esc(v.constraint)}，但目标版本为 ${esc(v.version)}</div>`
      ).join("")));
  }
  if (p.missing_deps.length) {
    blocks.push(block("bad", "⛔ 依赖的键不存在",
      p.missing_deps.map((m) => `<pre>${esc(JSON.stringify(m))}</pre>`).join("")));
  }
  blocks.push(`<div class="plan-section ${p.ok ? "good" : "bad"}">
    <b>${p.ok ? "✅ 校验通过，可以发布" : "❌ 校验未通过，发布将被阻止"}</b></div>`);
  body.innerHTML = blocks.join("");
  $("#btn-to-confirm").disabled = !p.ok;

  if (p.ok) {
    $("#confirm-body").innerHTML = `
      <p>将发布 <b>${pendingChanges.length}</b> 项改动，影响以下应用的客户端：</p>
      <pre>${esc(JSON.stringify(p.affected_apps, null, 2))}</pre>
      <p class="muted">发布后仅这些应用的在线客户端会收到 SSE 刷新通知。</p>`;
  }
}

async function commit() {
  try {
    const result = await api("/api/release", { method: "POST", body: { changes: pendingChanges } });
    $("#result-body").innerHTML = `
      <div class="plan-section good"><b>✅ 发布成功</b>
      <pre>事件 ID: ${esc(result.event.id)}
刷新客户端: ${esc(result.refreshed_clients.join(", ") || "（当前无在线客户端）")}</pre></div>`;
  } catch (e) {
    $("#result-body").innerHTML = `
      <div class="plan-section bad"><b>⛔ 发布被阻止</b><pre>${esc(e.message)}
${esc(JSON.stringify(e.cycle || e.violations || {}, null, 2))}</pre></div>`;
  }
  gotoStep(4);
  await refresh();
}

// ---------------------------------------------------------------- 回滚
async function doRollback() {
  const ids = $$(".rollback-pick").filter((c) => c.checked).map((c) => c.value);
  if (!ids.length) return;
  if (!confirm(`确认把选中的 ${ids.length} 次发布作为一批整体回滚？\n只会刷新这些发布原先影响过的客户端。`)) return;
  try {
    const result = await api("/api/rollback", { method: "POST", body: { release_ids: ids } });
    alert(`回滚完成（事件 ${result.event.id}），刷新客户端: ${result.refreshed_clients.join(", ") || "无在线客户端"}`);
  } catch (e) {
    alert(`回滚被阻止: ${e.message}\n${JSON.stringify(e.cycle || e.violations || "")}`);
  }
  await refresh();
}

// ---------------------------------------------------------------- 极简 SemVer 约束（前端展示用）
function parseSemver(s) {
  const m = /^(\d+)\.(\d+)\.(\d+)/.exec(s);
  return m ? m.slice(1).map(Number) : [0, 0, 0];
}
function semverSatisfies(version, spec) {
  const v = parseSemver(version);
  return spec.split(",").map((x) => x.trim()).filter(Boolean).every((part) => {
    const m = /^(\^|~|>=|<=|==|!=|>|<)\s*v?(\d+\.\d+\.\d+)/.exec(part);
    if (!m) return true;
    const [, op, t] = m;
    const tv = parseSemver(t);
    const cmp = (a, b) => a.reduce((r, x, i) => r || (x - b[i]), 0);
    const c = cmp(v, tv);
    switch (op) {
      case ">=": return c >= 0; case "<=": return c <= 0;
      case ">": return c > 0; case "<": return c < 0;
      case "==": return c === 0; case "!=": return c !== 0;
      case "^": return c >= 0 && v[0] === tv[0];
      case "~": return c >= 0 && v[0] === tv[0] && v[1] === tv[1];
      default: return true;
    }
  });
}

// ---------------------------------------------------------------- 事件绑定
$("#btn-new").addEventListener("click", openWizard);
$("#btn-rollback").addEventListener("click", doRollback);
$("#src-close").addEventListener("click", () => $("#source-dialog").close());
$("#f-scope").addEventListener("change", () =>
  $("#f-app-wrap").classList.toggle("hidden", $("#f-scope").value !== "override"));
$("#btn-add-change").addEventListener("click", () => {
  const c = readFormChange();
  if (!c.key || !c.version) { alert("键名与版本号必填"); return; }
  if (!/^\d+\.\d+\.\d+/.test(c.version)) { alert("版本号需为 SemVer，如 1.2.0"); return; }
  pendingChanges.push(c);
  renderPendingChanges();
  $("#f-key").value = ""; $("#f-version").value = "";
  $("#f-value").value = ""; $("#f-deps").value = ""; $("#f-note").value = "";
});
$$("[data-goto]").forEach((b) => b.addEventListener("click", () => {
  const n = Number(b.dataset.goto);
  if (n === 2) return analyze();
  gotoStep(n);
}));
$("#btn-commit").addEventListener("click", commit);
$("#btn-done").addEventListener("click", () => { $("#wizard").close(); refresh(); });

refresh().catch((e) => alert("初始化失败: " + e.message));
