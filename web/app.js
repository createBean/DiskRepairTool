const $ = (s) => document.querySelector(s);

const state = {
  disks: [],
  disk: null,
  offset: 0,
  crumbs: [],
  parent: 5,
  selected: new Set(),
  scanTimer: null,
  exTimer: null,
  filter: "all",
  lastKids: [],
};

const EXT_GROUPS = {
  image: [".jpg",".jpeg",".png",".gif",".bmp",".webp",".heic",".heif",".tif",".tiff",".psd",".dng",".cr2",".nef",".arw"],
  video: [".mp4",".mov",".m4v",".avi",".mkv",".wmv",".lrf"],
  doc:   [".doc",".docx",".xls",".xlsx",".ppt",".pptx",".pdf",".txt",".md",".csv",".zip",".rar",".7z",".json",".xml"],
};

function matchFilter(name) {
  if (state.filter === "all") return true;
  const ext = ("." + name.split(".").pop()).toLowerCase();
  if (state.filter === "other") {
    return !EXT_GROUPS.image.includes(ext) && !EXT_GROUPS.video.includes(ext) && !EXT_GROUPS.doc.includes(ext);
  }
  return (EXT_GROUPS[state.filter] || []).includes(ext);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  return res.json();
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}

/* ---------------- 磁盘 ---------------- */

async function loadDisks() {
  const data = await api("/api/disks");
  state.disks = data.disks || [];
  const tbody = $("#diskTable tbody");
  tbody.innerHTML = "";
  if (!state.disks.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">没有读到磁盘，请用管理员身份运行</td></tr>';
    return;
  }
  for (const d of state.disks) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${d.number}</td><td>${d.name || "-"}</td>
      <td>${d.size_gb ? d.size_gb + " GB" : "-"}</td>
      <td>${d.style || "-"}</td>
      <td>${d.status || "-"} / ${d.health || "-"}</td>
      <td>${d.bus || "-"}</td>`;
    tr.onclick = () => selectDisk(d, tr);
    tbody.appendChild(tr);
  }
}

async function selectDisk(d, tr) {
  document.querySelectorAll("#diskTable tbody tr").forEach((x) => x.classList.remove("on"));
  tr.classList.add("on");
  state.disk = d.number;
  const data = await api(`/api/partitions?disk=${d.number}`);
  const sel = $("#partSelect");
  sel.innerHTML = "";
  (data.partitions || []).forEach((p, i) => {
    const opt = document.createElement("option");
    opt.value = p.offset;
    opt.dataset.letter = p.letter || "";
    opt.textContent = `${p.label || "分区 " + (i + 1)} · 偏移 ${fmtBytes(p.offset)} · ${fmtBytes(p.size)}`;
    sel.appendChild(opt);
  });
  $("#partRow").hidden = !(data.partitions || []).length;
  $("#sec-diag").hidden = true;
  $("#sec-scan").hidden = true;
  $("#sec-browse").hidden = true;
  $("#sec-extract").hidden = true;
}

/* ---------------- 诊断 ---------------- */

async function diagnose() {
  const btn = $("#btnDiagnose");
  btn.disabled = true;
  btn.textContent = "诊断中…";
  const sel = $("#partSelect");
  const offset = parseInt(sel.value || "0", 10);
  const letter = (sel.selectedOptions[0] && sel.selectedOptions[0].dataset.letter) || "";
  state.offset = offset;
  state.letter = letter;
  try {
    const data = await api("/api/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disk: state.disk, offset, letter }),
    });
    renderDiag(data);
  } finally {
    btn.disabled = false;
    btn.textContent = "诊断这个分区";
  }
}

function renderDiag(data) {
  const sec = $("#sec-diag");
  const body = $("#diagBody");
  sec.hidden = false;
  if (!data.ok) {
    body.innerHTML = `<div class="verdict">诊断失败：${data.error || "未知错误"}</div>`;
    return;
  }
  const r = data.result;
  if (!r.ok) {
    body.innerHTML = `<div class="verdict">${r.reason}</div>`;
    return;
  }
  const bad = r.bad_records || [];
  body.innerHTML = `
    <div class="diag-grid">
      <div class="stat"><div class="k">簇大小</div><div class="v">${r.boot.cluster_size} B</div></div>
      <div class="stat"><div class="k">记录大小</div><div class="v">${r.boot.record_size} B</div></div>
      <div class="stat"><div class="k">卷容量</div><div class="v">${r.boot.total_gb} GB</div></div>
      <div class="stat"><div class="k">MFT 碎片段</div><div class="v">${r.mft_runs}</div></div>
      <div class="stat"><div class="k">记录总数</div><div class="v">${r.total_records.toLocaleString()}</div></div>
      <div class="stat"><div class="k">读取超时</div><div class="v">${r.timeouts}</div></div>
    </div>
    <div class="rec-strip">
      ${r.checked.map((c) => `<div class="rec ${c.ok ? "" : "bad"}" title="MFT 记录 ${c.index}">${c.index}</div>`).join("")}
    </div>
    <div class="verdict ${r.root_ok ? "ok" : ""}">${r.verdict}</div>
    ${r.mft_source && r.mft_source !== "dissect"
      ? `<div class="verdict">提示：当前用内置解析器读取 $MFT，未安装 dissect.ntfs。MFT 碎片化严重时可能读不全，建议执行 <code>pip install dissect.ntfs</code> 后重新诊断。</div>`
      : ""}
  `;
  $("#sec-scan").hidden = false;
  $("#sec-browse").hidden = true;
  $("#sec-extract").hidden = true;
}

/* ---------------- 扫描 ---------------- */

async function startScan() {
  const btn = $("#btnScan");
  btn.disabled = true;
  btn.textContent = "扫描中…";
  $("#scanBar").hidden = false;
  $("#sec-browse").hidden = true;
  await api("/api/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ disk: state.disk, offset: state.offset, letter: state.letter || "" }),
  });
  state.scanTimer = setInterval(pollScan, 800);
}

async function pollScan() {
  const data = await api("/api/scan/status");
  const s = data.status;
  const pct = s.total ? Math.min(100, (s.scanned / s.total) * 100) : 0;
  $("#scanBar .bar i").style.width = pct + "%";
  $("#scanText").textContent =
    `已扫描 ${s.scanned.toLocaleString()} / ${s.total.toLocaleString()} 条 · 有效 ${s.kept.toLocaleString()} 条 · ${s.elapsed}s`;
  if (s.state === "done") {
    clearInterval(state.scanTimer);
    $("#btnScan").disabled = false;
    $("#btnScan").textContent = "重新扫描";
    $("#scanText").textContent += s.timeouts ? ` · 跳过坏块 ${s.timeouts}` : "";
    openBrowse(5, []);
  } else if (s.state === "error") {
    clearInterval(state.scanTimer);
    $("#btnScan").disabled = false;
    $("#btnScan").textContent = "重试";
    $("#scanText").textContent = "扫描失败：" + s.error;
  }
}

/* ---------------- 浏览 ---------------- */

async function openBrowse(parent, crumbs) {
  $("#sec-browse").hidden = false;
  $("#sec-extract").hidden = false;
  state.parent = parent;
  state.crumbs = crumbs;
  renderCrumbs();
  await renderList(parent);
  loadChips();
}

function renderCrumbs() {
  const box = $("#crumbs");
  const items = [{ index: 5, name: "根目录" }, ...state.crumbs];
  box.innerHTML = items
    .map((c, i) => `<span data-i="${i}">${c.name}</span>`)
    .join('<i>/</i>');
  box.querySelectorAll("span").forEach((el) => {
    el.onclick = () => {
      const i = parseInt(el.dataset.i, 10);
      if (i === items.length - 1) return;
      const next = state.crumbs.slice(0, i);
      openBrowse(i === 0 ? 5 : state.crumbs[i - 1].index, next);
    };
  });
}

async function renderList(parent) {
  const data = await api(`/api/tree?parent=${parent}`);
  const box = $("#browseList");
  const all = data.children || [];
  const kids = all.filter((k) => k.dir || matchFilter(k.name));
  if (!all.length) {
    box.innerHTML = '<div class="empty">这个目录没有子项（可能是空目录，或记录在损坏区域）</div>';
    return;
  }
  if (!kids.length) {
    box.innerHTML = '<div class="empty">当前筛选条件下没有文件，切换筛选试试</div>';
    return;
  }
  box.innerHTML = "";
  for (const k of kids) {
    const row = document.createElement("div");
    row.className = "item" + (k.dir ? " dir" : "");
    if (k.dir) {
      row.innerHTML = `<div class="ico"></div><div class="nm">${k.name}</div><div class="meta">目录</div>`;
      row.onclick = () => openBrowse(k.index, [...state.crumbs, { index: k.index, name: k.name }]);
    } else {
      const on = state.selected.has(k.index);
      row.innerHTML = `<input type="checkbox" ${on ? "checked" : ""}>
        <div class="ico"></div><div class="nm">${k.name}</div><div class="meta">${fmtBytes(k.size)}</div>`;
      const cb = row.querySelector("input");
      cb.onchange = () => {
        if (cb.checked) state.selected.add(k.index);
        else state.selected.delete(k.index);
        updateCount();
      };
      row.onclick = (e) => {
        if (e.target.tagName === "INPUT") return;
        cb.checked = !cb.checked;
        cb.onchange();
      };
    }
    box.appendChild(row);
  }
  const more = all.length > kids.length
    ? `（筛选后 ${kids.length} 项，本目录共 ${all.length} 项）` : "";
  if (more) box.insertAdjacentHTML("beforeend", `<div class="empty">${more}</div>`);
}

async function loadChips() {
  const data = await api("/api/folders");
  const box = $("#folderChips");
  const list = data.folders || [];
  if (!list.length) { box.innerHTML = ""; return; }
  box.innerHTML = list.slice(0, 10).map((f) =>
    `<div class="chip" data-i="${f.index}"><b>${f.images}</b> 张 · ${f.name}</div>`).join("");
  box.querySelectorAll(".chip").forEach((el) => {
    el.onclick = () => {
      const idx = parseInt(el.dataset.i, 10);
      openBrowse(idx, [{ index: idx, name: el.textContent.split("· ")[1] || "目录" }]);
    };
  });
}

function updateCount() {
  $("#selCount").textContent = `已选 ${state.selected.size} 项`;
}

async function selectAllInDir() {
  const data = await api(`/api/collect?parent=${state.parent}`);
  (data.records || []).forEach((i) => state.selected.add(i));
  updateCount();
  renderList(state.parent);
}

/* ---------------- 提取 ---------------- */

async function startExtract() {
  if (!state.selected.size) { alert("先选中要恢复的文件"); return; }
  const dest = $("#destInput").value.trim();
  const btn = $("#btnExtract");
  btn.disabled = true;
  btn.textContent = "提取中…";
  $("#exBar").hidden = false;
  $("#exDone").hidden = true;
  await api("/api/extract", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ records: [...state.selected], dest }),
  });
  state.exTimer = setInterval(pollExtract, 700);
}

async function pollExtract() {
  const data = await api("/api/extract/status");
  const s = data.status;
  const pct = s.total ? (s.done / s.total) * 100 : 0;
  $("#exBar .bar i").style.width = pct + "%";
  $("#exText").textContent =
    `${s.done} / ${s.total} · ${fmtBytes(s.bytes)} · 失败 ${s.failed} · ${s.elapsed}s · ${s.current || ""}`;
  if (s.state === "done") {
    clearInterval(state.exTimer);
    $("#btnExtract").disabled = false;
    $("#btnExtract").textContent = "开始提取";
    $("#exDone").hidden = false;
    $("#exDone").textContent =
      `完成：成功 ${s.done - s.failed} 个，失败 ${s.failed} 个，共 ${fmtBytes(s.bytes)}，耗时 ${s.elapsed} 秒。目标：${$("#destInput").value}`;
  }
}

/* ---------------- 绑定 ---------------- */

$("#btnRefresh").onclick = loadDisks;
$("#btnDiagnose").onclick = diagnose;
$("#btnScan").onclick = startScan;
$("#btnSelectAll").onclick = selectAllInDir;
$("#btnClearSel").onclick = () => { state.selected.clear(); updateCount(); renderList(state.parent); };
$("#btnExtract").onclick = startExtract;
$("#typeFilter").onchange = (e) => {
  state.filter = e.target.value;
  renderList(state.parent);
};

$("#destInput").value = "";

(async function init() {
  const demo = new URLSearchParams(location.search).get("demo");
  if (demo === "1") {
    await api("/api/demo");
    $("#sec-disk").hidden = true;
    $("#sec-diag").hidden = true;
    $("#sec-scan").hidden = true;
    $("#modeBadge").textContent = "演示数据 · 不是真实磁盘";
    $("#destInput").value = "D:\\恢复文件";
    document.querySelector(".browse").classList.add("demo");
    await openBrowse(102, [
      { index: 100, name: "照片" },
      { index: 102, name: "2026-06-12 生日" },
    ]);
    return;
  }
  await loadDisks();
  const home = await api("/api/home");
  if (home && home.dest) $("#destInput").value = home.dest;
})();
