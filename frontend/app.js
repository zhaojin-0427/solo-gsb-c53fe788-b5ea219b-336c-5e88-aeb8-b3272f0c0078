/* 针织版型分码与行程编译台 —— 原生 JS */
"use strict";

const $ = (id) => document.getElementById(id);
const api = async (url, opts = {}) => {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    let data = null;
    try { data = await res.json(); msg = data.detail || msg; } catch (e) {}
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return res.json();
};

// 目标字段定义：[key, 中文标签, 类型 width(半幅)/circ(围度)/length]
const FIELDS = [
  ["bust", "胸围(围)", "circ"], ["hem", "下摆围", "circ"],
  ["shoulder", "肩宽", "width"], ["neck", "领口宽", "width"],
  ["fn_depth", "前领深", "length"], ["bn_depth", "后领深", "length"],
  ["length", "衣长", "length"], ["armhole", "袖窿直深", "length"],
  ["sleeve_len", "袖下长", "length"], ["cap_height", "袖山高", "length"],
  ["upper_arm", "上臂围", "circ"], ["cuff", "袖口围", "circ"],
];
const RIB_FIELDS = [["hem_rib", "下摆罗纹长"], ["cuff_rib", "袖口罗纹长"]];

let state = { payload: null, result: null, projectId: null, labels: {} };
let currentPid = null;

// ---------------- 编辑器 ----------------

function renderSizeTables() {
  const wrap = $("sizeTables");
  wrap.innerHTML = "";
  state.payload.sizes.forEach((sz, i) => {
    const block = document.createElement("div");
    block.className = "size-block";
    block.innerHTML = `
      <div class="size-head">
        <input type="text" data-name="${i}" value="${sz.name}" style="font-weight:600">
        <button class="btn small" data-del="${i}">删除</button>
      </div>
      <table class="inputgrid"><tbody>${buildFieldRows(i, sz)}</tbody></table>`;
    wrap.appendChild(block);
  });
  wrap.querySelectorAll("input[data-k]").forEach((inp) => {
    inp.addEventListener("input", () => {
      const i = +inp.dataset.i, k = inp.dataset.k;
      const v = parseFloat(inp.value);
      if (k === "hem_rib" || k === "cuff_rib") state.payload.sizes[i][k] = v;
      else state.payload.sizes[i].targets[k] = v;
    });
  });
  wrap.querySelectorAll("input[data-name]").forEach((inp) => {
    inp.addEventListener("input", () => { state.payload.sizes[+inp.dataset.name].name = inp.value; });
  });
  wrap.querySelectorAll("button[data-del]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.payload.sizes.splice(+btn.dataset.del, 1);
      renderSizeTables();
    });
  });
}

function buildFieldRows(i, sz) {
  let html = "";
  for (let k = 0; k < FIELDS.length; k += 2) {
    html += "<tr>";
    for (let j = k; j < k + 2; j++) {
      if (j < FIELDS.length) {
        const [key, lab] = FIELDS[j];
        html += `<td class="lab">${lab}</td>
          <td><input type="number" step="0.1" data-i="${i}" data-k="${key}" value="${sz.targets[key] ?? ""}"></td>`;
      } else { html += "<td></td><td></td>"; }
    }
    html += "</tr>";
  }
  html += `<tr><td class="lab">下摆罗纹长(cm)</td>
      <td><input type="number" step="0.5" data-i="${i}" data-k="hem_rib" value="${sz.hem_rib}"></td>
      <td class="lab">袖口罗纹长(cm)</td>
      <td><input type="number" step="0.5" data-i="${i}" data-k="cuff_rib" value="${sz.cuff_rib}"></td></tr>`;
  return html;
}

function readHeader() {
  const p = state.payload;
  p.name = $("pName").value;
  p.tolerance_cm = parseFloat($("pTol").value);
  p.gauge.stitches_per_10cm = parseFloat($("gSP").value);
  p.gauge.rows_per_10cm = parseFloat($("gRP").value);
  p.ease.bust_ease = parseFloat($("eBust").value);
  p.ease.sleeve_ease = parseFloat($("eSleeve").value);
  p.rib.hem_rib_type = $("rHem").value;
  p.rib.cuff_rib_type = $("rCuff").value;
  p.pattern.p_horizontal = parseInt($("patP").value, 10);
  p.pattern.q_vertical = parseInt($("patQ").value, 10);
  p.pattern.seam_phase_lock = $("patLock").checked;
}

function fillHeader() {
  const p = state.payload;
  $("pName").value = p.name;
  $("pTol").value = p.tolerance_cm;
  $("gSP").value = p.gauge.stitches_per_10cm;
  $("gRP").value = p.gauge.rows_per_10cm;
  $("eBust").value = p.ease?.bust_ease ?? 8;
  $("eSleeve").value = p.ease?.sleeve_ease ?? 4;
  $("rHem").value = p.rib?.hem_rib_type ?? "1x1";
  $("rCuff").value = p.rib?.cuff_rib_type ?? "1x1";
  $("patP").value = p.pattern?.p_horizontal ?? 1;
  $("patQ").value = p.pattern?.q_vertical ?? 1;
  $("patLock").checked = !!p.pattern?.seam_phase_lock;
}

function clearConflictMarks() {
  document.querySelectorAll("input.conflict").forEach((el) => el.classList.remove("conflict"));
}

function markConflicts(conflicts) {
  if (!conflicts || !conflicts.length) return;
  conflicts.forEach((c) => {
    // 全局字段
    const globalMap = {
      tolerance_cm: "pTol", p_horizontal: "patP", q_vertical: "patQ",
      seam_phase_lock: "patLock",
      hem_rib_type: "rHem", cuff_rib_type: "rCuff",
      stitches_per_10cm: "gSP", rows_per_10cm: "gRP",
    };
    if (globalMap[c.field]) {
      const el = $(globalMap[c.field]);
      if (el) el.classList.add("conflict");
      return;
    }
    // 尺码字段
    const idx = state.payload.sizes.findIndex((s) => s.name === c.size);
    if (idx >= 0) {
      const el = document.querySelector(`input[data-i="${idx}"][data-k="${c.field}"]`);
      if (el) el.classList.add("conflict");
    } else if (c.size === "*") {
      document.querySelectorAll(`input[data-k="${c.field}"]`).forEach((el) => el.classList.add("conflict"));
    }
  });
}

// ---------------- 求解 ----------------

async function solve(persist = true) {
  readHeader();
  clearConflictMarks();
  const btn = $("btnSolve");
  btn.disabled = true; btn.textContent = "求解中…";
  setStatus("warn", "⏳ CP-SAT 正在联合求解前后片/袖片整数针行…（多尺码可能需要数十秒）");
  try {
    let result;
    if (state.projectId) {
      // 先保存再求解
      await api(`/api/projects/${state.projectId}`, { method: "PUT", body: { payload: state.payload } });
      result = await api(`/api/projects/${state.projectId}/solve`, { method: "POST", body: { persist } });
    } else {
      result = await api("/api/solve", { method: "POST", body: state.payload });
    }
    state.result = result;
    if (result.status === "ok") {
      renderResult();
      const o = result.objective;
      setStatus("ok", `✅ ${result.message}｜最大误差 ${(o.max_err_100cm / 100).toFixed(2)} cm｜` +
        `塑形事件 ${o.total_events}｜尺码跳变 ${o.total_jump}｜接缝偏差 ${(o.seam_dev_100 / 100).toFixed(2)} cm｜` +
        `耗时 ${o.wall_time_s}s${state.projectId ? "（结果已入库）" : "（未保存项目，结果未入库）"}`);
      if (!state.projectId) await maybeSaveProject();
      await refreshProjectList();
    } else {
      showFailure(result);
    }
  } catch (e) {
    setStatus("bad", "❌ 请求失败：" + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "▶ 联合求解并编译";
  }
}

async function maybeSaveProject() {
  // 首次求解成功后自动入库（结果 JSON 随本次求解一并保存）
  try {
    const created = await api("/api/projects", { method: "POST", body: { payload: state.payload } });
    state.projectId = created.id;
    await api(`/api/projects/${state.projectId}/solve`, { method: "POST", body: { persist: true } });
  } catch (e) { /* 忽略自动保存失败 */ }
}

function showFailure(result) {
  $("resultArea").classList.add("hidden");
  $("emptyHint").classList.add("hidden");
  const st = $("solveStatus");
  st.className = "status bad";
  let html = `<b>❌ ${result.message || "无可行解"}</b>`;
  if (result.status === "timeout") {
    html += `<div>${result.message}</div>`;
  } else if (result.conflicts?.length) {
    html += `<div style="margin-top:8px">标出的冲突字段（红色高亮于左侧表单）：</div>`;
    result.conflicts.forEach((c) => {
      const label = state.labels[c.field] || c.field;
      html += `<div class="conflict-item"><b>${label}</b>（尺码：${c.size === "*" ? "全部" : c.size}）— ${c.reason}</div>`;
    });
  }
  st.innerHTML = html;
  markConflicts(result.conflicts);
}

function setStatus(kind, html) {
  const st = $("solveStatus");
  st.className = `status ${kind}`;
  st.innerHTML = html;
  st.classList.remove("hidden");
}

// ---------------- 结果渲染 ----------------

function renderResult() {
  $("emptyHint").classList.add("hidden");
  $("resultArea").classList.remove("hidden");
  renderOverview();
  renderMeasures();
  renderSim();
  renderOutline();
  renderInstructionsInit();
}

function compiledSize(name) {
  return state.result.compiled.find((c) => c.name === name);
}

function renderOverview() {
  const r = state.result;
  let html = `<h2 style="margin-top:0">择优目标与求解结果</h2>
  <div class="kvgrid">
    <div class="kv"><div class="k">求解状态</div><div class="v"><span class="pill ok">${r.objective.optimal ? "最优解" : "可行解"}</span></div></div>
    <div class="kv"><div class="k">最大尺寸误差</div><div class="v">${(r.objective.max_err_100cm / 100).toFixed(2)} <small>cm</small></div></div>
    <div class="kv"><div class="k">接缝吃势偏差</div><div class="v">${(r.objective.seam_dev_100 / 100).toFixed(2)} <small>cm</small></div></div>
    <div class="kv"><div class="k">塑形事件总数</div><div class="v">${r.objective.total_events}</div></div>
    <div class="kv"><div class="k">相邻尺码跳变</div><div class="v">${r.objective.total_jump}</div></div>
    <div class="kv"><div class="k">求解耗时</div><div class="v">${r.objective.wall_time_s} <small>s</small></div></div>
  </div>
  <h2>各尺码关键决策（针/行）</h2>
  <div class="table-wrap"><table class="data"><thead><tr>
    <th class="name">尺码</th><th>下摆起针</th><th>腋下胸宽</th><th>肩宽</th><th>领宽</th>
    <th>袖窿行</th><th>肩阶梯</th><th>前领深(行)</th><th>袖口起针</th><th>袖肥</th><th>袖山行</th><th>山顶针</th>
  </tr></thead><tbody>`;
  r.compiled.forEach((c) => {
    const s = c.solution;
    html += `<tr><td class="name"><b>${s.name}</b></td>
      <td>${s.cast_on}</td><td>${s.chest}</td><td>${s.w2}</td><td>${s.nb}</td>
      <td>${s.arm_rows}</td><td>${s.nsh}×${s.ksh}</td><td>${s.fn_depth}</td>
      <td>${s.cuff_on}</td><td>${s.upper}</td><td>${s.cap_rows}</td><td>${s.cap_top}</td></tr>`;
  });
  html += `</tbody></table></div>`;
  html += `<h2>领罗纹挑针</h2>`;
  r.compiled.forEach((c) => {
    html += `<div class="collar-box"><b>${c.name}</b>：挑针 <b>${c.collar.pickup_stitches}</b> 针
      （前领 ${c.collar.front_pick} / 后领 ${c.collar.back_pick}），估算领围 ${c.collar.estimated_circumference_cm} cm。
      <div class="muted" style="color:var(--muted);font-size:12px">${c.collar.note}</div></div>`;
  });
  $("tab-overview").innerHTML = html;
}

function renderMeasures() {
  const labels = Object.fromEntries(FIELDS.map(([k, l]) => [k, l]));
  let html = `<div class="table-wrap"><table class="data"><thead><tr>
    <th class="name">指标</th>${state.result.compiled.map((c) => `<th>${c.name}<br><small>目标→实际</small></th>`).join("")}
  </tr></thead><tbody>`;
  const keys = state.result.compiled[0].measurements.map((m) => m.key);
  const tol = state.payload.tolerance_cm ?? 0.6;
  keys.forEach((key) => {
    html += `<tr><td class="name">${labels[key] || key}</td>`;
    state.result.compiled.forEach((c) => {
      const m = c.measurements.find((x) => x.key === key);
      const cls = m.error_cm <= tol ? "good" : "over";
      html += `<td><small>${m.target_cm?.toFixed(1)}→</small> <b>${m.actual_cm.toFixed(2)}</b>
        <div class="${cls}">Δ ${m.error_cm.toFixed(2)}</div></td>`;
    });
    html += "</tr>";
  });
  html += `</tbody></table></div>`;
  $("tab-measures").innerHTML = html;
}

function renderSim() {
  let html = "";
  state.result.compiled.forEach((c) => {
    const allOk = c.checks.every((x) => x.ok);
    html += `<div class="checksize"><h3>${c.name}
      <span class="pill ${allOk ? "ok" : "bad"}">${allOk ? "全部通过" : "存在告警"}</span></h3><div class="body">`;
    c.checks.forEach((ck) => {
      html += `<div class="check-row"><span class="ico">${ck.ok ? "✅" : "⚠️"}</span>
        <span class="txt"><b>${ck.name}</b><div>${ck.detail}</div>`;
      const pairs = ck.pairs || [];
      if (pairs.length) {
        html += `<table class="phasepairs"><thead><tr>
          <th>事件对</th><th>a 侧事件（绝对行）</th><th>b 侧事件（绝对行）</th>
          <th>相位差 mod ${ck.Q || "Q"}</th><th>状态</th></tr></thead><tbody>`;
        pairs.forEach((pr) => {
          html += `<tr class="${pr.ok ? "good" : "over"}">
            <td>第 ${pr.index} 对</td>
            <td>${pr.label_a || pr.row_a}</td>
            <td>${pr.label_b || pr.row_b}</td>
            <td>${pr.phase_diff}</td><td>${pr.ok ? "✅ 同相" : "⚠️ 错位"}</td></tr>`;
        });
        html += `</tbody></table>`;
      }
      const unpaired = ck.unpaired || [];
      if (unpaired.length) {
        const sideName = (s) => (s === "a" ? (ck.edge_name_a || "a 侧") : (ck.edge_name_b || "b 侧"));
        html += `<div style="margin-top:4px;color:var(--muted);font-size:12px">未配对事件（另一侧无对应序数，信息项不计入判定）：</div>`;
        html += `<table class="phasepairs"><thead><tr><th>侧</th><th>序数</th><th>事件</th><th>绝对行</th></tr></thead><tbody>`;
        unpaired.forEach((u) => {
          html += `<tr><td>${sideName(u.side)}</td><td>第 ${u.index} 个</td><td>${u.label || ""}</td><td>${u.row}</td></tr>`;
        });
        html += `</tbody></table>`;
      }
      html += `</span></div>`;
    });
    html += `</div></div>`;
  });
  $("tab-sim").innerHTML = html;
}

// ---------------- 轮廓叠图（SVG） ----------------

const SIZE_COLORS = ["#b05a3a", "#2e6e7e", "#5a7d3a", "#8a4f8f", "#b8860b", "#445"];

function renderOutline() {
  const piece = $("ocPiece").value;
  const svg = $("outlineSvg");
  const W = 600, H = 700;
  const polys = state.result.compiled.map((c) => c.polygons[piece]);
  // 统一坐标范围（cm）
  let minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;
  polys.forEach((pts) => pts.forEach(([x, y]) => {
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
  }));
  const pad = 2.5;
  minX -= pad; maxX += pad; minY -= pad; maxY += pad;
  const sx = W / (maxX - minX), sy = H / (maxY - minY);
  const sc = Math.min(sx, sy);
  const ox = (W - (maxX - minX) * sc) / 2 - minX * sc;
  const oy = H - ((H - (maxY - minY) * sc) / 2 + maxY * sc);
  const X = (x) => ox + x * sc, Y = (y) => H - (oy + y * sc);

  let g = "";
  if ($("ocGrid").checked) {
    for (let gx = Math.ceil(minX); gx <= Math.floor(maxX); gx++) {
      g += `<line x1="${X(gx)}" y1="${Y(minY)}" x2="${X(gx)}" y2="${Y(maxY)}" stroke="#eee9df" stroke-width="1"/>`;
      g += `<text x="${X(gx) + 1}" y="${Y(minY) + 11}" font-size="9" fill="#bbb">${gx}</text>`;
    }
    for (let gy = Math.ceil(minY); gy <= Math.floor(maxY); gy++) {
      g += `<line x1="${X(minX)}" y1="${Y(gy)}" x2="${X(maxX)}" y2="${Y(gy)}" stroke="#eee9df" stroke-width="1"/>`;
      g += `<text x="${X(minX) + 2}" y="${Y(gy) - 1}" font-size="9" fill="#bbb">${gy}</text>`;
    }
  }
  // 0 轴线
  g += `<line x1="${X(0)}" y1="${Y(minY)}" x2="${X(0)}" y2="${Y(maxY)}" stroke="#d9cfbd" stroke-dasharray="4 3"/>`;

  state.result.compiled.forEach((c, i) => {
    const pts = c.polygons[piece];
    const d = pts.map(([x, y], k) => `${k === 0 ? "M" : "L"}${X(x).toFixed(1)},${Y(y).toFixed(1)}`).join(" ") + " Z";
    const col = SIZE_COLORS[i % SIZE_COLORS.length];
    g += `<path d="${d}" fill="${col}14" stroke="${col}" stroke-width="1.6" fill-opacity="0.5"/>`;
    // 尺码标签放在轮廓顶部中心
    const topY = Math.max(...pts.map((p) => p[1]));
    g += `<text x="${X(0)}" y="${Y(topY) - 4 - i * 13}" font-size="12" font-weight="700" fill="${col}" text-anchor="middle">${c.name}</text>`;
  });
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = g;
}

// ---------------- 逐行指令 ----------------

function renderInstructionsInit() {
  const sel = $("inSize");
  const cur = sel.value;
  sel.innerHTML = state.result.compiled.map((c) => `<option>${c.name}</option>`).join("");
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  renderInstructions();
}

function renderInstructions() {
  const name = $("inSize").value;
  const pieceKey = $("inPiece").value;
  const sectionFilter = $("inSection").value;
  const eventsOnly = $("inEventsOnly").checked;
  const c = compiledSize(name);
  if (!c) return;
  const piece = c.pieces[pieceKey];
  const s = c.solution;

  $("instrSummary").innerHTML =
    `<b>${name} · ${pieceKey === "front" ? "前片" : pieceKey === "back" ? "后片" : "袖片"}</b>｜` +
    `共 ${piece.total_rows} 行｜起针 ${piece.cast_on} 针` +
    (pieceKey === "sleeve"
      ? `｜袖下加针 ${s.total_inc} 次/侧｜袖山收 ${s.ncap_used} 段｜山顶 ${s.cap_top} 针`
      : `｜侧放 ${s.total_side_inc} 次/侧｜袖窿收 ${s.nah_used} 段｜肩 ${s.nsh}×${s.ksh}/侧`);

  let html = `<thead><tr><th>行号</th><th>区段</th><th class="c">本行后活针</th><th class="l">指令</th></tr></thead><tbody>`;
  piece.rows.forEach((r) => {
    if (sectionFilter !== "all" && r.section !== sectionFilter) return;
    const hasEvt = r.events.length > 0;
    if (eventsOnly && !hasEvt) return;
    const secName = { rib: "罗纹", side: "侧区", arm: "挂肩", inc: "袖下", cap: "袖山" }[r.section] || r.section;
    const ins = hasEvt ? r.events.join("；") : "平织（下针/花样照图）";
    html += `<tr class="${hasEvt ? "evt" : ""} sec-${r.section}">
      <td>${r.row}</td><td>${secName}</td><td class="c">${r.live_stitches}</td><td class="l">${ins}</td></tr>`;
  });
  html += "</tbody>";
  $("instrTable").innerHTML = html;
}

// ---------------- Tab 切换 ----------------

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tabpane").forEach((x) => x.classList.add("hidden"));
    t.classList.add("active");
    $("tab-" + t.dataset.tab).classList.remove("hidden");
    if (t.dataset.tab === "outline") renderOutline();
    if (t.dataset.tab === "revision") onRevisionTabOpen();
  });
});

// ---------------- 项目管理 ----------------

async function refreshProjectList() {
  const list = await api("/api/projects");
  const sel = $("projectSelect");
  sel.innerHTML = `<option value="">— 选择已保存项目 —</option>` +
    list.map((p) => `<option value="${p.id}" ${p.id === state.projectId ? "selected" : ""}>${p.name} #${p.id}</option>`).join("");
}

async function loadProject(id) {
  const p = await api(`/api/projects/${id}`);
  state.payload = p.payload; state.projectId = p.id; state.result = null;
  rvState.resultId = null; rvState.result = null; rvState.resultList = [];
  rvState.registerRows = null;
  fillHeader(); renderSizeTables(); clearConflictMarks();
  $("resultArea").classList.add("hidden"); $("emptyHint").classList.remove("hidden");
  setStatus("warn", `已载入项目 #${id}，修改后请重新求解。`);
  try {
    const res = await api(`/api/projects/${id}/result`);
    state.result = res.payload;
    renderResult();
    const o = state.result.objective;
    setStatus("ok", `已载入项目 #${id} 的最近求解结果（最大误差 ${(o.max_err_100cm / 100).toFixed(2)} cm）。修改参数后请重新求解。`);
  } catch (e) { /* 无结果 */ }
}

async function init() {
  const def = await api("/api/default");
  state.payload = def.payload; state.labels = def.labels;
  fillHeader(); renderSizeTables();
  await refreshProjectList();

  $("projectSelect").addEventListener("change", (e) => {
    if (e.target.value) loadProject(+e.target.value);
  });
  $("btnNew").addEventListener("click", async () => {
    const def = await api("/api/default");
    state.payload = def.payload; state.projectId = null; state.result = null;
    fillHeader(); renderSizeTables(); clearConflictMarks();
    $("resultArea").classList.add("hidden"); $("emptyHint").classList.remove("hidden");
    $("solveStatus").classList.add("hidden");
    $("projectSelect").value = "";
  });
  $("btnSave").addEventListener("click", async () => {
    readHeader();
    try {
      if (state.projectId) {
        await api(`/api/projects/${state.projectId}`, { method: "PUT", body: { payload: state.payload } });
        setStatus("ok", `项目 #${state.projectId} 已更新。`);
      } else {
        const created = await api("/api/projects", { method: "POST", body: { payload: state.payload } });
        state.projectId = created.id;
        setStatus("ok", `项目已保存为 #${created.id}。`);
        await refreshProjectList();
      }
    } catch (e) { setStatus("bad", "保存失败：" + e.message); }
  });
  $("btnSolve").addEventListener("click", () => solve(true));

  ["ocPiece", "ocGrid"].forEach((id) => $(id).addEventListener("change", renderOutline));
  ["inSize", "inPiece", "inSection", "inEventsOnly"].forEach((id) =>
    $(id).addEventListener("change", renderInstructions));

  // 试织纠偏控件（脚本在 body 末加载，可直接绑定）
  $("rvResult").addEventListener("change", rvSelectResult);
  $("rvPiece").addEventListener("change", rvResetWorkspace);
  $("rvSize").addEventListener("change", rvResetWorkspace);
  $("rvBuild").addEventListener("click", rvBuildRegister);
  $("rvRecalc").addEventListener("click", rvRecalcLive);
  $("rvSubmit").addEventListener("click", rvSubmit);
}

init();

// ============================================================
// 试织纠偏（余下行程重排）
// ============================================================

const rvState = {
  resultId: null,          // 当前选择的已保存求解结果 id
  result: null,            // 该结果 payload
  registerRows: null,      // 当前登记表（数组）
  resultList: [],
};

const RV_FIELDS = {
  front: [
    ["inc_per_side", "侧放/侧"],
    ["arm_dec_per_side", "袖窿收/侧"],
    ["shoulder_bind_per_side", "肩收/侧"],
    ["neck_center_bind", "领中心"],
    ["neck_edge_per_side", "领缘收/侧"],
  ],
  back: [
    ["inc_per_side", "侧放/侧"],
    ["arm_dec_per_side", "袖窿收/侧"],
    ["shoulder_bind_per_side", "肩收/侧"],
    ["neck_center_bind", "领中心"],
    ["neck_edge_per_side", "领缘/侧"],
  ],
  sleeve: [
    ["inc_per_side", "袖下放/侧"],
    ["cap_dec_per_side", "袖山收/侧"],
  ],
};
const RV_SEC_NAME = { rib: "罗纹", side: "侧区", arm: "挂肩", inc: "袖下", cap: "袖山" };

async function onRevisionTabOpen() {
  $("revIntro").innerHTML =
    "从<b>已保存的求解结果</b>选择尺码与衣片，逐行登记实际活针数、实际塑形操作与" +
    "<b>意外增减针</b>，确认已织前缀后，仅对该衣片的<b>未织行</b>重排塑形事件：固定总行数与" +
    "其他衣片，修订须通过针数守恒、成品容差、接缝匹配与启用时的花样相位锁定，按" +
    "「改动事件行数 → 针数偏差」择优。<b>已完成的行不会被改写；无解时只给冲突说明，不覆盖原指令。</b>" +
    "修订绑定生成它的那一次求解结果，项目再次求解后仍可查看。";
  await rvRefreshResults();
  if (rvState.resultList.length &&
      !rvState.resultList.some((x) => x.id === rvState.resultId)) {
    rvState.resultId = rvState.resultList[0].id;
    rvState.result = null;
  }
  rvRenderResultSelector();
  if (rvState.resultId) await rvEnsureResultLoaded();
  rvFillSizePiece();
  await rvLoadSaved();
}

async function rvRefreshResults() {
  if (!state.projectId) {
    rvState.resultList = [];
    return;
  }
  rvState.resultList = await api(`/api/projects/${state.projectId}/results`);
}

function rvRenderResultSelector() {
  const sel = $("rvResult");
  if (!state.projectId) {
    sel.innerHTML = `<option value="">（请先保存项目并求解）</option>`;
    $("rvSize").innerHTML = "";
    ["rvBuild", "rvRecalc", "rvSubmit"].forEach((id) => $(id).disabled = true);
    $("rvHint").textContent = "纠偏必须基于已保存的求解结果：点击「▶ 联合求解并编译」即可自动保存。";
    return;
  }
  ["rvBuild", "rvRecalc", "rvSubmit"].forEach((id) => $(id).disabled = false);
  $("rvHint").textContent = "";
  const cur = rvState.resultId;
  sel.innerHTML = rvState.resultList.map((x, i) =>
    `<option value="${x.id}" ${x.id === cur ? "selected" : ""}>` +
    `#${x.id} ${i === 0 ? "（最近一次）" : ""} ${new Date(x.created_at * 1000).toLocaleString()}</option>`
  ).join("");
  if (!cur && rvState.resultList.length) rvState.resultId = rvState.resultList[0].id;
  if (!rvState.resultId) rvState.resultId = +sel.value || null;
  rvFillSizePiece();
}

async function rvEnsureResultLoaded() {
  if (!rvState.resultId) return null;
  if (rvState.result && rvState.result._rid === rvState.resultId) return rvState.result;
  const row = await api(`/api/projects/${state.projectId}/results/${rvState.resultId}`);
  rvState.result = row.payload;
  rvState.result._rid = rvState.resultId;
  return rvState.result;
}

function rvFillSizePiece() {
  const result = rvState.result && rvState.result._rid === rvState.resultId
    ? rvState.result : state.result;
  const sizeSel = $("rvSize");
  const curSize = sizeSel.value;
  sizeSel.innerHTML = (result?.compiled || [])
    .map((c) => `<option ${c.name === curSize ? "selected" : ""}>${c.name}</option>`).join("");
  rvSetPrefixDefault();
}

async function rvSelectResult() {
  rvState.resultId = +$("rvResult").value || null;
  await rvEnsureResultLoaded();
  rvFillSizePiece();
  rvResetWorkspace();
  await rvLoadSaved();
}

function rvResetWorkspace() {
  rvState.registerRows = null;
  $("rvRegisterWrap").classList.add("hidden");
  $("rvOutcome").innerHTML = "";
  $("rvStatus").classList.add("hidden");
}

function rvCurrentRows() {
  const result = rvState.result && rvState.result._rid === rvState.resultId
    ? rvState.result : state.result;
  const size = $("rvSize").value;
  const piece = $("rvPiece").value;
  const c = (result?.compiled || []).find((x) => x.name === size);
  return { result, c, piece, rows: c ? c.pieces[piece].rows : [],
           total: c ? c.pieces[piece].total_rows : 0 };
}

function rvSetPrefixDefault() {
  const { total } = rvCurrentRows();
  if (!total) return;
  const v = parseInt($("rvPrefix").value, 10);
  if (!Number.isInteger(v) || v < 1 || v >= total) {
    $("rvPrefix").value = Math.max(1, Math.min(total - 1, total - 20));
  }
  $("rvPrefix").max = total - 1;
}

function rvBuildRegister() {
  const { c, rows, total } = rvCurrentRows();
  if (!c) { rvSetStatus("warn", "该结果没有可登记的行表，请重新选择求解结果。"); return; }
  let prefix = parseInt($("rvPrefix").value, 10);
  if (!Number.isInteger(prefix) || prefix < 1 || prefix >= total) {
    prefix = Math.max(1, Math.min(total - 1, total - 20));
    $("rvPrefix").value = prefix;
  }
  // 用当前结果的原指令预填操作与活针；意外针默认 0
  const fields = Object.keys(Object.fromEntries(RV_FIELDS[$("rvPiece").value]));
  rvState.registerRows = rows.slice(0, prefix).map((r) => {
    const d = { row: r.row, accidental: 0, live: r.live_stitches };
    fields.forEach((f) => { d[f] = r[f] || 0; });
    d._section = r.section;
    d._events = r.events;
    return d;
  });
  rvRenderRegister();
  $("rvRegisterWrap").classList.remove("hidden");
  rvSetStatus("warn", `已按原指令预填前 ${prefix} 行。请逐行核对：修改实际塑形操作、填写意外增减针后，` +
    "点「按操作+意外针重算活针」，实际活针与重算值一致（无红底）即可提交。");
}

function rvRenderRegister() {
  const piece = $("rvPiece").value;
  const cols = RV_FIELDS[piece];
  const fields = cols.map(([k]) => k);
  let html = `<thead><tr><th>行号</th><th>区段</th>` +
    cols.map(([, lab]) => `<th>${lab}</th>`).join("") +
    `<th>意外增减针</th><th>本行后活针</th><th>原指令</th></tr></thead><tbody>`;
  rvState.registerRows.forEach((d, i) => {
    const mismatch = d.live !== rvSimWidth(i);
    html += `<tr class="${mismatch ? "badrow" : ""} prefix-locked" data-i="${i}">
      <td>${d.row}</td>
      <td class="rv-sec-${d._section}">${RV_SEC_NAME[d._section] || d._section}</td>` +
      fields.map((f) => `<td><input data-f="${f}" value="${d[f] ?? 0}" inputmode="numeric"></td>`).join("") +
      `<td><input class="acc" data-f="accidental" value="${d.accidental ?? 0}" inputmode="numeric"></td>` +
      `<td><input data-f="live" class="${mismatch ? "conflict" : ""}" value="${d.live}" inputmode="numeric"></td>` +
      `<td class="ev">${(d._events || []).join("；") || "平织"}</td></tr>`;
  });
  html += "</tbody>";
  $("rvRegister").innerHTML = html;
  $("rvRegister").querySelectorAll("input[data-f]").forEach((inp) => {
    inp.addEventListener("input", () => {
      const tr = inp.closest("tr");
      const d = rvState.registerRows[+tr.dataset.i];
      const v = parseInt(inp.value, 10);
      d[inp.dataset.f] = Number.isNaN(v) ? 0 : v;
      // 就地更新该行及其后各行的“推算一致”标记，避免整表重绘导致输入焦点丢失
      const idx = +tr.dataset.i;
      for (let i = idx; i < rvState.registerRows.length; i++) {
        const rowTr = $("rvRegister").querySelector(`tr[data-i="${i}"]`);
        if (!rowTr) continue;
        const mismatch = rvState.registerRows[i].live !== rvSimWidth(i);
        rowTr.classList.toggle("badrow", mismatch);
        const liveInp = rowTr.querySelector('input[data-f="live"]');
        if (liveInp) liveInp.classList.toggle("conflict", mismatch);
      }
    });
  });
}

function rvDeltaFields(piece) {
  return piece === "sleeve"
    ? ["inc_per_side", "cap_dec_per_side"]
    : ["inc_per_side", "arm_dec_per_side", "shoulder_bind_per_side",
       "neck_center_bind", "neck_edge_per_side"];
}

function rvSimWidth(upTo) {
  // 从首行登记的活针起，按操作+意外针逐行推算 upTo 行（含）的活针数
  const piece = $("rvPiece").value;
  const arr = rvState.registerRows;
  if (upTo < 0) return arr[0].live;
  let w = arr[0].live;
  for (let i = 1; i <= upTo; i++) {
    const d = arr[i];
    if (piece === "sleeve") {
      w += 2 * (d.inc_per_side || 0) - 2 * (d.cap_dec_per_side || 0) + (d.accidental || 0);
    } else {
      w += 2 * (d.inc_per_side || 0) - 2 * (d.arm_dec_per_side || 0)
        - 2 * (d.shoulder_bind_per_side || 0)
        - (d.neck_center_bind || 0) - 2 * (d.neck_edge_per_side || 0) + (d.accidental || 0);
    }
  }
  return w;
}

function rvRecalcLive() {
  if (!rvState.registerRows) return;
  for (let i = 0; i < rvState.registerRows.length; i++) {
    rvState.registerRows[i].live = rvSimWidth(i);
  }
  rvRenderRegister();
  rvSetStatus("ok", "已按「操作 + 意外增减针」逐行重算活针数。红底行表示仍有手填差异。");
}

function rvMismatchRows() {
  const bad = [];
  for (let i = 0; i < rvState.registerRows.length; i++) {
    if (rvState.registerRows[i].live !== rvSimWidth(i)) bad.push(rvState.registerRows[i].row);
  }
  return bad;
}

async function rvSubmit() {
  if (!state.projectId || !rvState.resultId) {
    rvSetStatus("bad", "请先在已保存项目上选择一个求解结果。");
    return;
  }
  if (!rvState.registerRows) { rvSetStatus("bad", "请先载入前缀登记表。"); return; }
  const bad = rvMismatchRows();
  if (bad.length) {
    rvSetStatus("bad", `第 ${bad.slice(0, 8).join("、")} 行的实际活针与按操作推算值不一致，` +
      "请修正登记或点「重算活针」。原指令不会被改动。");
    return;
  }
  const piece = $("rvPiece").value;
  const keep = ["row", "accidental", "live", ...rvDeltaFields(piece)];
  const register = rvState.registerRows.map((d) => {
    const o = {};
    keep.forEach((k) => { o[k] = d[k] ?? 0; });
    return o;
  });
  const body = {
    result_id: rvState.resultId,
    size: $("rvSize").value,
    piece,
    prefix_rows: rvState.registerRows.length,
    register,
  };
  const btn = $("rvSubmit");
  btn.disabled = true; btn.textContent = "重排中…（CP-SAT）";
  $("rvOutcome").innerHTML = "";
  try {
    const res = await api(`/api/projects/${state.projectId}/revisions`,
      { method: "POST", body });
    if (res.status === "infeasible") {
      rvRenderInfeasible(res);
    } else {
      rvRenderRevision(res.revision, res.revision_id);
      await rvLoadSaved();
    }
  } catch (e) {
    if (e.status === 422 && e.data?.detail?.row_errors) {
      const detail = e.data.detail;
      const byRow = {};
      detail.row_errors.forEach((x) => {
        (byRow[x.row] = byRow[x.row] || []).push(`${x.field}：${x.reason}`);
      });
      Object.keys(byRow).forEach((rowStr) => {
        const i = rvState.registerRows.findIndex((d) => d.row === +rowStr);
        if (i >= 0) {
          const tr = $("rvRegister").querySelector(`tr[data-i="${i}"]`);
          if (tr) tr.classList.add("badrow");
        }
      });
      const firstRows = Object.keys(byRow).slice(0, 6)
        .map((r) => `第 ${r} 行：${byRow[r].join("；")}`).join("<br>");
      rvSetStatus("bad", `<b>❌ ${detail.message}</b><div class="conflict-item">${firstRows}</div>`);
    } else {
      rvSetStatus("bad", "请求失败：" + e.message);
    }
  } finally {
    btn.disabled = false; btn.textContent = "▶ 锁定前缀并重排余下行程";
  }
}

function rvSetStatus(kind, html) {
  const st = $("rvStatus");
  st.className = `status ${kind}`;
  st.innerHTML = html;
  st.classList.remove("hidden");
}

function rvRenderInfeasible(res) {
  let html = `<b>❌ ${res.message || "无可行重排"}</b>` +
    `<div style="margin-top:6px">冲突约束（原指令未改动，未保存任何修订）：</div>`;
  (res.conflicts || []).forEach((c) => {
    const label = state.labels[c.field] || (c.field === "*" ? "综合约束" : c.field);
    html += `<div class="conflict-item"><b>${label}</b>（尺码：${c.size === "*" ? "全部" : c.size}）— ${c.reason}</div>`;
  });
  rvSetStatus("bad", html);
}

function rvPieceName(p) {
  return p === "front" ? "前片" : p === "back" ? "后片" : "袖片";
}

function rvRenderRevision(rev, revId) {
  const o = rev.objective;
  let html = `<div class="rev-section">
    <div class="rev-metrics">
      <div class="rev-metric">修订 #<b>${revId ?? ""}</b></div>
      <div class="rev-metric">改动事件行 <b>${o.changed_event_rows}</b></div>
      <div class="rev-metric">针数偏差 <b>${o.stitch_deviation}</b></div>
      <div class="rev-metric">意外增减针 <b>${rev.accidental_total >= 0 ? "+" : ""}${rev.accidental_total}</b></div>
      <div class="rev-metric">前缀锁定 <b>${rev.prefix_rows}</b> 行</div>
      <div class="rev-metric">求解 ${o.optimal ? "最优" : "可行（未证最优）"} · ${o.wall_time_s}s</div>
    </div></div>`;

  // 新旧余下行程对照
  html += `<div class="rev-section"><b>新旧余下行程对照（自第 ${rev.prefix_rows + 1} 行起，红色底纹=与原指令不同）</b>
    <div class="oldnew" style="margin-top:6px">
      <div class="col old"><h4>原指令（未织行）</h4>${rvTailTable(rev.tail_old, null)}</div>
      <div class="col new"><h4>纠偏后重排</h4>${rvTailTable(rev.tail_new, rev.tail_old)}</div>
    </div></div>`;

  // 成稿校验
  const allOk = rev.checks.every((c) => c.ok);
  html += `<div class="rev-section rev-checks"><b>修订成稿校验</b>
    <span class="pill ${allOk ? "ok" : "bad"}">${allOk ? "全部通过" : "存在告警"}</span>`;
  rev.checks.forEach((c) => {
    html += `<div class="check-row"><span class="ico">${c.ok ? "✅" : "⚠️"}</span>
      <span class="txt"><b>${c.name}</b><div>${c.detail}</div>`;
    if (c.pairs?.length) {
      html += `<table class="phasepairs"><thead><tr><th>事件对</th><th>a 行</th><th>b 行</th>
        <th>相位差 mod ${c.Q || "Q"}</th><th>状态</th></tr></thead><tbody>`;
      c.pairs.forEach((pr) => {
        html += `<tr class="${pr.ok ? "good" : "over"}"><td>第 ${pr.index} 对</td>
          <td>${pr.row_a}</td><td>${pr.row_b}</td><td>${pr.phase_diff}</td>
          <td>${pr.ok ? "✅ 同相" : "⚠️ 错位"}</td></tr>`;
      });
      html += "</tbody></table>";
    }
    html += "</span></div>";
  });
  html += "</div>";
  $("rvOutcome").innerHTML = html;
  rvSetStatus("ok", `✅ 已生成并保存修订 #${revId}：${rvPieceName(rev.piece)} ${rev.size}，` +
    `改动 ${o.changed_event_rows} 个事件行，针数偏差 ${o.stitch_deviation}。`);
}

function rvTailTable(tail, oldForDiff) {
  let html = `<div class="table-wrap" style="max-height:360px"><table><thead><tr>
    <th>行</th><th>区段</th><th>活针</th><th>指令</th></tr></thead><tbody>`;
  tail.forEach((r, i) => {
    const old = oldForDiff ? oldForDiff[i] : null;
    const diff = old && (old.live_stitches !== r.live_stitches
      || JSON.stringify(old.events) !== JSON.stringify(r.events));
    const ins = r.events?.length ? r.events.join("；") : "平织（下针/花样照图）";
    html += `<tr class="${diff ? "diff" : ""}"><td>${r.row}</td>
      <td class="rv-sec-${r.section}">${RV_SEC_NAME[r.section] || r.section}</td>
      <td class="live">${r.live_stitches}</td><td style="text-align:left">${ins}</td></tr>`;
  });
  return html + "</tbody></table></div>";
}

async function rvLoadSaved() {
  const wrap = $("rvSaved");
  if (!state.projectId) { wrap.innerHTML = `<span class="rev-hint">（保存项目后可用）</span>`; return; }
  const qs = rvState.resultId ? `?result_id=${rvState.resultId}` : "";
  const list = await api(`/api/projects/${state.projectId}/revisions${qs}`);
  if (!list.length) {
    wrap.innerHTML = `<span class="rev-hint">该求解结果暂无修订。修订一旦生成即永久绑定本次结果，项目再次求解后仍可在此查看。</span>`;
    return;
  }
  wrap.innerHTML = list.map((x) => {
    const p = x.payload;
    return `<div class="rev-card" data-id="${x.id}">
      <div class="head">
        <b>#${x.id}</b>
        <span>${rvPieceName(p.piece)} · ${p.size}</span>
        <span class="muted">绑定结果 #${x.result_id} · ${new Date(x.created_at * 1000).toLocaleString()}</span>
        <span class="pill ok">改动 ${p.objective.changed_event_rows} 行 / 偏差 ${p.objective.stitch_deviation}</span>
        <span class="toggle">展开对照 ▾</span>
        <button class="del">删除</button>
      </div>
      <div class="body hidden"></div>
    </div>`;
  }).join("");
  wrap.querySelectorAll(".rev-card").forEach((card) => {
    const id = +card.dataset.id;
    const rec = list.find((x) => x.id === id).payload;
    const body = card.querySelector(".body");
    card.querySelector(".toggle").addEventListener("click", () => {
      if (body.classList.contains("hidden")) {
        body.classList.remove("hidden");
        body.innerHTML = `<b>新旧余下行程对照</b>
          <div class="oldnew" style="margin-top:6px">
            <div class="col old"><h4>原指令</h4>${rvTailTable(rec.tail_old, null)}</div>
            <div class="col new"><h4>纠偏后</h4>${rvTailTable(rec.tail_new, rec.tail_old)}</div>
          </div>
          <div class="rev-checks">${rec.checks.map((c) =>
            `<div class="check-row"><span class="ico">${c.ok ? "✅" : "⚠️"}</span>
              <span class="txt"><b>${c.name}</b><div>${c.detail}</div></span></div>`).join("")}</div>`;
        card.querySelector(".toggle").textContent = "收起对照 ▴";
      } else {
        body.classList.add("hidden");
        card.querySelector(".toggle").textContent = "展开对照 ▾";
      }
    });
    card.querySelector(".del").addEventListener("click", async () => {
      if (!confirm(`确定删除修订 #${id}？（不影响原求解结果）`)) return;
      await api(`/api/projects/${state.projectId}/revisions/${id}`, { method: "DELETE" });
      await rvLoadSaved();
    });
  });
}
