/* ═══════════════════════════════════════════════════════════
   WaveSnap — Frontend logic
   ═══════════════════════════════════════════════════════════ */

// ── State ────────────────────────────────────────────────────
let state = {
  currentInfo:  null,
  currentJobId: null,
  currentOutDir: null,
  activeSSE:    null,
  tracksTotal:  0,
  tracksDone:   0,
};

const history = JSON.parse(localStorage.getItem("ws_history") || "[]");

// ── DOM ──────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const urlInput    = $("url-input");
const fetchBtn    = $("fetch-btn");
const fetchLabel  = $("fetch-label");
const pasteBtn    = $("paste-btn");

const infoPanel   = $("info-panel");
const infoThumb   = $("info-thumb");
const infoTypeBdg = $("info-type-badge");
const infoTitle   = $("info-title");
const infoCh      = $("info-channel");
const infoCountTag = $("info-count-tag");
const infoSizeTag  = $("info-size-tag");
const qualitySelect = $("quality-select");
const infoFolderRow = $("info-folder-row");
const infoFolderName = $("info-folder-name");
const dlBtn       = $("download-btn");
const dlLabel     = $("download-label");

const progSection = $("progress-section");
const progLabel   = $("prog-label");
const progTrack   = $("prog-track");
const progSpeed   = $("prog-speed");
const progFill    = $("prog-fill");
const progPct     = $("prog-percent");
const progBar     = $("prog-track-bar");

const filesSection = $("files-section");
const filesTitle  = $("files-title");
const fileList    = $("file-list");
const dlAllBtn    = $("dl-all-btn");
const dlZipBtn    = $("dl-zip-btn");
const dlCoverBtn  = $("dl-cover-btn");

const histSect    = $("history-section");
const histList    = $("history-list");
const toasts      = $("toasts");

// ── Toast ────────────────────────────────────────────────────
function toast(msg, type = "info", ms = 4000) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `<div class="t-dot"></div><span>${msg}</span>`;
  toasts.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ── Format helpers ───────────────────────────────────────────
function fmtBytes(b) {
  if (!b) return "–";
  if (b < 1024) return `${b} B`;
  if (b < 1048576) return `${(b/1024).toFixed(1)} KB`;
  if (b < 1073741824) return `${(b/1048576).toFixed(1)} MB`;
  return `${(b/1073741824).toFixed(2)} GB`;
}

function fmtDuration(s) {
  if (!s) return "";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}:${String(m % 60).padStart(2,"0")}:${String(sec).padStart(2,"0")}`;
  }
  return `${m}:${String(sec).padStart(2,"0")}`;
}

function esc(s) {
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// ── Paste ────────────────────────────────────────────────────
pasteBtn.addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    urlInput.value = text.trim();
    urlInput.focus();
    toast("Pasted from clipboard", "success", 2000);
  } catch {
    toast("Tap the input and long-press to paste", "info");
  }
});

// ── Fetch info ───────────────────────────────────────────────
fetchBtn.addEventListener("click", fetchInfo);
urlInput.addEventListener("keydown", e => { if (e.key === "Enter") fetchInfo(); });

async function fetchInfo() {
  const url = urlInput.value.trim();
  if (!url) { toast("Paste a link first", "error"); return; }

  setFetching(true);
  resetUI();

  try {
    const res  = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "Unknown error");
    state.currentInfo = data;
    showInfo(data);
  } catch (err) {
    toast(`Couldn't fetch info: ${err.message}`, "error");
  } finally {
    setFetching(false);
  }
}

function updateEstimatedSize() {
  if (!state.currentInfo) return;
  const q = qualitySelect ? qualitySelect.value : "mp3_320";
  let kbps = 320;
  if (q.includes("256")) kbps = 256;
  else if (q.includes("192")) kbps = 192;
  else if (q.includes("128")) kbps = 128;

  const bytesPerSec = (kbps * 1000) / 8;
  const info = state.currentInfo;

  let totalSec = 0;
  if (info.duration) {
    totalSec = info.duration;
  } else if (info.tracks && info.tracks.length) {
    totalSec = info.tracks.reduce((acc, t) => acc + (t.duration || 210), 0);
  } else if (info.count) {
    totalSec = info.count * 210;
  } else {
    totalSec = 210;
  }

  const estBytes = totalSec * bytesPerSec;
  if (infoSizeTag) {
    infoSizeTag.textContent = `Est. ~${fmtBytes(estBytes)}`;
    infoSizeTag.style.display = "";
  }
}

if (qualitySelect) {
  qualitySelect.addEventListener("change", updateEstimatedSize);
}

function showInfo(info) {
  // Thumbnail
  if (info.thumbnail) {
    infoThumb.src = info.thumbnail;
    infoThumb.onerror = () => { infoThumb.style.display = "none"; };
    infoThumb.style.display = "";
  } else {
    infoThumb.style.display = "none";
  }

  // Badge
  infoTypeBdg.textContent = info.is_playlist
    ? (info.count ? `PLAYLIST · ${info.count} tracks` : "PLAYLIST")
    : (info.duration ? fmtDuration(info.duration) : "AUDIO");

  infoTitle.textContent = info.title;
  infoCh.textContent    = info.channel || "";

  // Tags
  infoCountTag.textContent = info.is_playlist
    ? (info.count ? `${info.count} songs` : "Playlist")
    : "1 track";
  infoCountTag.style.display = "";

  // Estimated Size
  updateEstimatedSize();

  // Folder indicator (only for playlists)
  if (info.is_playlist) {
    const fname = sanitizeFolderName(info.title);
    infoFolderName.textContent = fname;
    infoFolderRow.classList.remove("hidden");
  } else {
    infoFolderRow.classList.add("hidden");
  }

  // Download button label
  dlLabel.textContent = info.is_playlist
    ? (info.count ? `Download All ${info.count} Songs` : "Download Playlist")
    : "Download Song";

  infoPanel.classList.remove("hidden");

  // Update history display
  renderHistory();
}

// ── Download ─────────────────────────────────────────────────
dlBtn.addEventListener("click", startDownload);

async function startDownload() {
  const url = urlInput.value.trim();
  if (!url || !state.currentInfo) return;

  dlBtn.disabled = true;
  resetProgress();
  progSection.classList.remove("hidden");
  filesSection.classList.add("hidden");
  fileList.innerHTML = "";

  state.tracksTotal = state.currentInfo.count || 1;
  state.tracksDone  = 0;

  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        title: state.currentInfo.title,
        is_playlist: state.currentInfo.is_playlist,
        source: state.currentInfo.source || "ytdlp",
        tracks: state.currentInfo.tracks || [],
        thumbnail: state.currentInfo.thumbnail || "",
        quality: qualitySelect ? qualitySelect.value : "mp3_320",
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "Unknown error");

    state.currentJobId  = data.job_id;
    state.currentOutDir = data.out_dir;

    saveHistory(state.currentInfo, url);
    listenSSE(data.job_id, data.out_dir);
  } catch (err) {
    toast(`Download error: ${err.message}`, "error");
    dlBtn.disabled = false;
    progSection.classList.add("hidden");
  }
}

// ── SSE progress ─────────────────────────────────────────────
function listenSSE(jobId, outDir) {
  if (state.activeSSE) state.activeSSE.close();
  state.activeSSE = new EventSource(`/api/progress/${jobId}`);

  state.activeSSE.onmessage = e => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }

    if (msg.type === "ping") return;

    if (msg.type === "progress") {
      const pct = msg.percent || 0;
      setProgress(pct, msg.speed || "");
      if (msg.filename) {
        progTrack.textContent = msg.filename.split(/[\\/]/).pop();
      }
    }

    if (msg.type === "track_done") {
      state.tracksDone++;
      progLabel.textContent = state.tracksTotal > 1
        ? `Downloading ${state.tracksDone} / ${state.tracksTotal} tracks…`
        : "Processing…";
      setProgress(100, "");
    }

    if (msg.type === "error") {
      toast(`Error: ${msg.message}`, "error");
    }

    if (msg.type === "done") {
      state.activeSSE.close();
      state.activeSSE = null;
      progSection.classList.add("hidden");
      dlBtn.disabled = false;
      loadFiles(outDir);
      toast("Download complete! 🎵", "success");
    }
  };

  state.activeSSE.onerror = () => {
    if (state.activeSSE) { state.activeSSE.close(); state.activeSSE = null; }
    progSection.classList.add("hidden");
    dlBtn.disabled = false;
    if (state.currentOutDir) loadFiles(state.currentOutDir);
  };
}

// ── Load files ───────────────────────────────────────────────
async function loadFiles(outDir) {
  try {
    const res  = await fetch("/api/files-for-job", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ out_dir: outDir }),
    });
    const data = await res.json();
    const files = data.files || [];

    if (!files.length) {
      toast("No audio files found — check ffmpeg is installed", "error");
      return;
    }

    filesSection.classList.remove("hidden");
    fileList.innerHTML = "";

    if (filesTitle) {
      const sizeStr = data.total_size ? ` · ${fmtBytes(data.total_size)}` : "";
      filesTitle.textContent = `Ready to Save (${files.length} ${files.length === 1 ? "song" : "songs"}${sizeStr})`;
    }

    files.forEach((f, i) => {
      const ext = (f.name.split('.').pop() || "audio").toUpperCase();
      const item = document.createElement("div");
      item.className = "file-item";
      item.style.animationDelay = `${i * 0.05}s`;
      const thumbContent = (state.currentInfo && state.currentInfo.thumbnail)
        ? `<img src="${esc(state.currentInfo.thumbnail)}" alt="Cover" class="file-thumb-img" onerror="this.style.display='none'" />`
        : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>
          </svg>`;

      item.innerHTML = `
        <div class="file-thumb">
          ${thumbContent}
        </div>
        <div class="file-meta">
          <div class="file-name">${esc(f.name)}</div>
          <div class="file-sub">
            <span>${fmtBytes(f.size)}</span>
            <span>·</span>
            <span>${ext} · High Fidelity</span>
            ${f.folder ? `<span>·</span><span class="file-folder">📁 ${esc(f.folder)}</span>` : ""}
          </div>
        </div>
        <button class="save-btn" data-rel="${esc(f.rel)}" data-name="${esc(f.name)}" aria-label="Save ${esc(f.name)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          Save
        </button>`;
      fileList.appendChild(item);
    });

    // Per-file save
    document.querySelectorAll(".save-btn").forEach(btn => {
      btn.addEventListener("click", () => triggerSave(btn.dataset.rel, btn.dataset.name));
    });

    // Save Cover Art (.jpg)
    if (data.cover) {
      if (dlCoverBtn) {
        dlCoverBtn.classList.remove("hidden");
        dlCoverBtn.onclick = () => {
          const coverTitle = state.currentInfo ? `${sanitizeFolderName(state.currentInfo.title)} - Cover.jpg` : "cover.jpg";
          triggerSave(data.cover, coverTitle);
        };
      }
    } else {
      if (dlCoverBtn) dlCoverBtn.classList.add("hidden");
    }

    // Download as ZIP for playlists/folders
    if (data.folder && files.length > 0) {
      if (dlZipBtn) {
        dlZipBtn.classList.remove("hidden");
        const zipSizeStr = data.total_size ? ` (${fmtBytes(data.total_size)})` : ` (${files.length} songs)`;
        dlZipBtn.textContent = `Download as ZIP${zipSizeStr}`;
        dlZipBtn.onclick = () => {
          const zipTitle = state.currentInfo ? (state.currentInfo.title || data.folder) : data.folder;
          window.location.href = `/api/download-zip?folder=${encodeURIComponent(data.folder)}&title=${encodeURIComponent(zipTitle)}`;
        };
      }
    } else {
      if (dlZipBtn) dlZipBtn.classList.add("hidden");
    }

    // Save All (individual files)
    if (files.length > 1) {
      dlAllBtn.classList.remove("hidden");
      dlAllBtn.onclick = () => {
        files.forEach((f, i) => setTimeout(() => triggerSave(f.rel, f.name), i * 250));
      };
    } else {
      dlAllBtn.classList.add("hidden");
    }
  } catch (err) {
    toast(`Couldn't load files: ${err.message}`, "error");
  }
}

function triggerSave(rel, name) {
  const a = document.createElement("a");
  a.href = `/api/download-file?path=${encodeURIComponent(rel)}`;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ── History ──────────────────────────────────────────────────
function saveHistory(info, url) {
  const entry = { title: info.title, count: info.count || 1, isPlaylist: info.is_playlist, url, ts: Date.now() };
  const idx = history.findIndex(h => h.url === url);
  if (idx !== -1) history.splice(idx, 1);
  history.unshift(entry);
  if (history.length > 15) history.pop();
  localStorage.setItem("ws_history", JSON.stringify(history));
}

function renderHistory() {
  if (!history.length) { histSect.classList.add("hidden"); return; }
  histSect.classList.remove("hidden");
  histList.innerHTML = "";
  history.forEach((h, i) => {
    const item = document.createElement("div");
    item.className = "history-item";
    item.style.animationDelay = `${i * 0.04}s`;
    item.setAttribute("role", "button");
    item.setAttribute("tabindex", "0");
    item.setAttribute("aria-label", `Re-use: ${h.title}`);
    item.innerHTML = `
      <div class="hi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div>
      <span class="hi-title">${esc(h.title)}</span>
      <span class="hi-count">${h.isPlaylist ? `${h.count} songs` : "1 song"}</span>
      <div class="hi-arrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></div>`;
    const handler = () => { urlInput.value = h.url; fetchInfo(); window.scrollTo({top: 0, behavior: "smooth"}); };
    item.addEventListener("click", handler);
    item.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") handler(); });
    histList.appendChild(item);
  });
}

// ── UI helpers ───────────────────────────────────────────────
function setFetching(on) {
  fetchBtn.disabled = on;
  fetchLabel.textContent = on ? "Checking…" : "Check Link";
}

function resetUI() {
  infoPanel.classList.add("hidden");
  progSection.classList.add("hidden");
  filesSection.classList.add("hidden");
  dlAllBtn.classList.add("hidden");
}

function resetProgress() {
  progFill.style.width = "0%";
  progBar.setAttribute("aria-valuenow", "0");
  progLabel.textContent = "Starting…";
  progTrack.textContent = "";
  progSpeed.textContent = "";
  progPct.textContent = "0%";
}

function setProgress(pct, speed) {
  progFill.style.width = `${pct}%`;
  progBar.setAttribute("aria-valuenow", pct);
  progLabel.textContent = `Downloading… ${pct}%`;
  progPct.textContent = `${pct}%`;
  if (speed) progSpeed.textContent = speed;
}

function sanitizeFolderName(name) {
  return name.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").replace(/^[\s.]+|[\s.]+$/g, "") || "Playlist";
}

// ── Init ─────────────────────────────────────────────────────
renderHistory();
urlInput.focus();
