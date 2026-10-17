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

const liveDetector   = $("live-detector");
const detectorBadge  = $("detector-badge");
const detectorTitle  = $("detector-title");
const detectorEngine = $("detector-engine");

const swCard   = $("software-card");
const swIcon   = $("sw-icon");
const swName   = $("sw-name");
const swType   = $("sw-type");
const swEngine = $("sw-engine");

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
const cancelDlBtn = $("cancel-dl-btn");

const filesSection = $("files-section");
const filesTitle  = $("files-title");
const fileList    = $("file-list");
const dlAllBtn    = $("dl-all-btn");
const dlZipBtn    = $("dl-zip-btn");
const dlCoverBtn  = $("dl-cover-btn");

const saveHero         = $("save-hero");
const saveHeroBadge    = $("save-hero-badge");
const saveHeroHeadline = $("save-hero-headline");
const saveHeroSub      = $("save-hero-sub");
const heroDlBtn        = $("hero-dl-btn");
const heroDlText       = $("hero-dl-text");

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

// ── Paste & Input Detection ──────────────────────────────────
pasteBtn.addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    urlInput.value = text.trim();
    urlInput.focus();
    onUrlInput();
    toast("Pasted from clipboard", "success", 2000);
  } catch {
    toast("Tap the input and long-press to paste", "info");
  }
});

function detectSoftware(raw) {
  const u = (raw || "").trim().toLowerCase();
  if (!u) return null;

  if (
    u.startsWith("spotify:") ||
    u.includes("spotify.com") ||
    u.includes("spoti.fi") ||
    u.includes("spotify.link") ||
    /^spotify:(?:track|playlist|album|artist):/i.test(u)
  ) {
    const isPl = u.includes("playlist") || u.includes("album") || u.includes("artist");
    let typeName = "Track";
    if (u.includes("playlist")) typeName = "Playlist";
    else if (u.includes("album")) typeName = "Album";
    else if (u.includes("artist")) typeName = "Artist";

    return {
      id: "spotify",
      name: "Spotify",
      icon: "🟢",
      color: "#1DB954",
      type: isPl ? typeName : "Track",
      engine: "Spotify Dedicated Downloader",
      desc: "Extracts official Spotify tracklist & high-res artwork; downloads 320kbps MP3s."
    };
  }
  if (u.includes("music.youtube.com")) {
    const isPl = u.includes("list=");
    return {
      id: "ytmusic",
      name: "YouTube Music",
      icon: "⭕",
      color: "#FF0000",
      type: isPl ? "Playlist / Album" : "Song",
      engine: "YouTube Music Dedicated Engine",
      desc: "Extracts highest bitrate YouTube Music stream with rich metadata."
    };
  }
  if (u.includes("youtube.com") || u.includes("youtu.be")) {
    const isPl = u.includes("list=");
    const isShort = u.includes("/shorts/");
    return {
      id: "youtube",
      name: "YouTube",
      icon: "🔴",
      color: "#FF0000",
      type: isPl ? "Playlist" : (isShort ? "Shorts" : "Video"),
      engine: "YouTube High-Fidelity Engine",
      desc: "Extracts direct YouTube audio in chosen bitrate with thumbnail embed."
    };
  }
  if (u.includes("music.apple.com") || u.includes("itunes.apple.com")) {
    const isPl = u.includes("/playlist/") || u.includes("/album/");
    return {
      id: "applemusic",
      name: "Apple Music",
      icon: "🍎",
      color: "#FA2D48",
      type: isPl ? "Playlist / Album" : "Song",
      engine: "Apple Music Resolver Engine",
      desc: "Parses Apple Music tracklist and album art; fetches matching 320kbps audio."
    };
  }
  if (u.includes("soundcloud.com")) {
    const isPl = u.includes("/sets/");
    return {
      id: "soundcloud",
      name: "SoundCloud",
      icon: "🟠",
      color: "#FF5500",
      type: isPl ? "Set / Playlist" : "Track",
      engine: "SoundCloud Stream Engine",
      desc: "Extracts high-quality SoundCloud streams with original artwork."
    };
  }
  if (u.includes("jiosaavn.com") || u.includes("saavn.com")) {
    const isPl = u.includes("/album/") || u.includes("/playlist/") || u.includes("/featured/");
    return {
      id: "jiosaavn",
      name: "JioSaavn",
      icon: "🟢",
      color: "#2bc5b4",
      type: isPl ? "Playlist / Album" : "Song",
      engine: "JioSaavn 320kbps CD Quality Engine",
      desc: "Direct 320kbps CD master extraction with zero DRM and official cover art."
    };
  }
  if (u.includes("bandcamp.com")) {
    const isPl = u.includes("/album/");
    return {
      id: "bandcamp",
      name: "Bandcamp",
      icon: "🔵",
      color: "#629AA9",
      type: isPl ? "Album" : "Track",
      engine: "Bandcamp Lossless Engine",
      desc: "Direct stream extraction from Bandcamp."
    };
  }
  if (u.includes("tiktok.com")) {
    return {
      id: "tiktok",
      name: "TikTok",
      icon: "🎵",
      color: "#00F2FE",
      type: "Video Audio",
      engine: "TikTok Audio Extractor",
      desc: "Extracts original audio track from TikTok video."
    };
  }
  if (u.includes("instagram.com")) {
    return {
      id: "instagram",
      name: "Instagram",
      icon: "📸",
      color: "#E1306C",
      type: "Reel / Video Audio",
      engine: "Instagram Reel Audio Extractor",
      desc: "Extracts audio track from Instagram reel or post."
    };
  }
  if (u.startsWith("http://") || u.startsWith("https://")) {
    return {
      id: "generic",
      name: "Web Media",
      icon: "🌐",
      color: "#8B5CF6",
      type: "Media Link",
      engine: "Universal Audio Engine",
      desc: "Supports 1000+ streaming sites via yt-dlp extractor."
    };
  }
  return {
    id: "search",
    name: "Search Query",
    icon: "🔍",
    color: "#3B82F6",
    type: "Song Search",
    engine: "Direct Search Engine",
    desc: "Searches top matching high-quality audio track."
  };
}

function onUrlInput() {
  const val = urlInput.value.trim();
  if (!val) {
    if (liveDetector) liveDetector.classList.add("hidden");
    document.querySelectorAll(".chip").forEach(c => c.classList.remove("chip-active"));
    return;
  }

  const p = detectSoftware(val);
  if (!p) {
    if (liveDetector) liveDetector.classList.add("hidden");
    document.querySelectorAll(".chip").forEach(c => c.classList.remove("chip-active"));
    return;
  }

  if (liveDetector) {
    if (detectorBadge) {
      detectorBadge.textContent = `${p.icon} ${p.name}`;
      detectorBadge.style.background = `${p.color}22`;
      detectorBadge.style.borderColor = `${p.color}55`;
      detectorBadge.style.color = p.color;
    }
    if (detectorTitle) detectorTitle.textContent = `${p.name} ${p.type}`;
    if (detectorEngine) detectorEngine.textContent = `Using ${p.engine}`;
    liveDetector.classList.remove("hidden");
  }

  // Highlight corresponding chip
  document.querySelectorAll(".chip").forEach(c => {
    if (c.dataset.platform === p.id) {
      c.classList.add("chip-active");
    } else {
      c.classList.remove("chip-active");
    }
  });
}

// Click on platform chips to get guidance / focus
document.querySelectorAll(".chip[data-platform]").forEach(c => {
  c.addEventListener("click", () => {
    const plat = c.dataset.platform;
    const hints = {
      spotify: "Paste Spotify song, album, or playlist link / URI…",
      youtube: "Paste YouTube video or playlist link…",
      ytmusic: "Paste YouTube Music link…",
      soundcloud: "Paste SoundCloud track or set link…",
      applemusic: "Paste Apple Music album or song link…",
      bandcamp: "Paste Bandcamp album or track link…"
    };
    if (hints[plat]) {
      urlInput.placeholder = hints[plat];
      urlInput.focus();
      toast(`Selected ${c.textContent} engine`, "info", 1800);
    }
  });
});

urlInput.addEventListener("input", onUrlInput);
urlInput.addEventListener("paste", () => setTimeout(onUrlInput, 50));

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
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) {
      const msg = data.error || `Server returned status ${res.status}`;
      console.error("fetchInfo error:", msg);
      throw new Error(msg);
    }
    state.currentInfo = data;
    showInfo(data);
  } catch (err) {
    console.error("fetchInfo catch:", err);
    toast(err.message, "error", 5000);
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

  // Software / Platform Engine Display
  if (info.platform && swCard) {
    const p = info.platform;
    if (swIcon) swIcon.textContent = p.icon || "🎵";
    if (swName) {
      swName.textContent = p.name || "Platform";
      swName.style.color = p.badge_color || "#fff";
    }
    if (swType) swType.textContent = p.type_label || (info.is_playlist ? "Playlist" : "Track");
    if (swEngine) swEngine.textContent = p.engine || p.software || "Dedicated Downloader";
    swCard.style.borderColor = p.badge_color ? `${p.badge_color}55` : "rgba(255,255,255,0.1)";
    swCard.classList.remove("hidden");
  } else if (swCard) {
    swCard.classList.add("hidden");
  }

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
if (cancelDlBtn) cancelDlBtn.addEventListener("click", cancelDownload);

function resetCancelBtn() {
  if (!cancelDlBtn) return;
  cancelDlBtn.disabled = false;
  cancelDlBtn.innerHTML = `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="13" height="13">
      <line x1="18" y1="6" x2="6" y2="18"/>
      <line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
    <span>Cancel</span>`;
}

async function cancelDownload() {
  if (!state.currentJobId) return;
  if (cancelDlBtn) {
    cancelDlBtn.disabled = true;
    cancelDlBtn.innerHTML = `<span>Stopping…</span>`;
  }

  try {
    await fetch(`/api/cancel/${encodeURIComponent(state.currentJobId)}`, { method: "POST" });
  } catch (err) {
    console.error("Cancel failed:", err);
  }

  if (state.activeSSE) {
    state.activeSSE.close();
    state.activeSSE = null;
  }

  progSection.classList.add("hidden");
  dlBtn.disabled = false;
  resetCancelBtn();
  toast("Download stopped", "info", 3000);
}

async function startDownload() {
  const url = urlInput.value.trim();
  if (!url || !state.currentInfo) return;

  dlBtn.disabled = true;
  resetCancelBtn();
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
        source: state.currentInfo.source || (state.currentInfo.platform ? state.currentInfo.platform.platform_id : "generic"),
        platform_id: state.currentInfo.platform ? state.currentInfo.platform.platform_id : null,
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

    if (msg.type === "cancelled") {
      state.activeSSE.close();
      state.activeSSE = null;
      progSection.classList.add("hidden");
      dlBtn.disabled = false;
      resetCancelBtn();
      toast(msg.message || "Download stopped", "info", 3000);
      return;
    }

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
        ? `Converting track ${state.tracksDone} of ${state.tracksTotal} (320kbps MP3)…`
        : "Encoding audio to 320kbps MP3…";
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
      toast("Audio conversion complete! Sending to your computer… 🎵", "success");
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
      toast("No audio files found — check that the link is valid", "error");
      return;
    }

    filesSection.classList.remove("hidden");
    fileList.innerHTML = "";

    const totalSizeStr = data.total_size ? ` (${fmtBytes(data.total_size)})` : "";
    const zipTitle = state.currentInfo ? (state.currentInfo.title || data.folder || "Playlist") : (data.folder || "Playlist");

    // ── Setup Direct Save Hero Banner ──
    if (saveHero) {
      const isMobile = /Android|iPhone|iPad|iPod|webOS|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
      const deviceLabel = isMobile ? "Phone" : "Device";

      if (files.length === 1) {
        const singleExt = (files[0].name.split('.').pop() || "mp3").toUpperCase();
        if (saveHeroBadge) saveHeroBadge.textContent = "Audio Ready";
        if (saveHeroHeadline) saveHeroHeadline.textContent = `Save "${files[0].name}" to Your Device`;
        if (saveHeroSub) saveHeroSub.textContent = `Your 320kbps MP3 with album artwork is ready. Tap the button below to save directly into your ${deviceLabel}'s Downloads folder.`;
        if (heroDlText) heroDlText.textContent = `Save to My ${deviceLabel} (${singleExt} · ${fmtBytes(files[0].size)})`;
        if (heroDlBtn) heroDlBtn.onclick = () => {
          const dlUrl = `/api/download-file?path=${encodeURIComponent(files[0].rel)}&name=${encodeURIComponent(files[0].name || "")}`;
          triggerBrowserDownload(dlUrl, files[0].name);
        };

        // Auto-trigger browser download to device for single song
        triggerSave(files[0].rel, files[0].name);
        toast(`Sending "${files[0].name}" to your ${deviceLabel}…`, "success", 4000);
      } else {
        if (saveHeroBadge) saveHeroBadge.textContent = `Playlist Ready (${files.length} Songs)`;
        if (saveHeroHeadline) saveHeroHeadline.textContent = `Save "${zipTitle}.zip" to Your Device`;
        if (saveHeroSub) saveHeroSub.textContent = `All ${files.length} songs and album art are packaged in a ZIP archive. Tap below to download to your ${deviceLabel}, or save individual songs below.`;
        if (heroDlText) heroDlText.textContent = `Save All to My ${deviceLabel} (.ZIP${totalSizeStr})`;
        if (heroDlBtn) heroDlBtn.onclick = () => {
          triggerZipDownload(data.folder, zipTitle);
        };

        // Auto-trigger browser download to device for playlist ZIP
        if (data.folder) {
          triggerZipDownload(data.folder, zipTitle);
          toast(`Sending "${zipTitle}.zip" to your ${deviceLabel}…`, "success", 5000);
        }
      }

      // Smooth scroll so the user immediately sees the download card
      setTimeout(() => {
        filesSection.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 100);
    }

    if (filesTitle) {
      const sizeStr = data.total_size ? ` · ${fmtBytes(data.total_size)}` : "";
      filesTitle.textContent = `All Files (${files.length} ${files.length === 1 ? "song" : "songs"}${sizeStr})`;
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
          triggerZipDownload(data.folder, zipTitle);
        };
      }
    } else {
      if (dlZipBtn) dlZipBtn.classList.add("hidden");
    }

    // Save All (individual files)
    if (files.length > 1) {
      dlAllBtn.classList.remove("hidden");
      dlAllBtn.onclick = () => {
        files.forEach((f, i) => setTimeout(() => triggerSave(f.rel, f.name), i * 350));
      };
    } else {
      dlAllBtn.classList.add("hidden");
    }
  } catch (err) {
    toast(`Couldn't load files: ${err.message}`, "error");
  }
}

function triggerSave(rel, name) {
  const url = `/api/download-file?path=${encodeURIComponent(rel)}&name=${encodeURIComponent(name || "")}`;
  triggerBrowserDownload(url, name);
}

function triggerZipDownload(folder, title) {
  const zipTitle = title || folder;
  const url = `/api/download-zip?folder=${encodeURIComponent(folder)}&title=${encodeURIComponent(zipTitle)}`;
  triggerBrowserDownload(url, `${zipTitle}.zip`);

  // Automatically delete temporary files from Render server 2 minutes after download starts
  setTimeout(() => {
    fetch("/api/cleanup-job", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder }),
    }).catch(() => {});
  }, 120000);
}

function triggerBrowserDownload(url, filename) {
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;

  if (isIOS) {
    // iOS Safari triggers native file download dialog reliably via window.location.assign
    window.location.assign(url);
    return;
  }

  // Android, Windows PC, Mac, Linux: single clean anchor click
  try {
    const a = document.createElement("a");
    a.href = url;
    if (filename) a.setAttribute("download", filename);
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      if (a.parentNode) a.parentNode.removeChild(a);
    }, 2000);
  } catch (e) {
    console.warn("Anchor download error, falling back to location.assign:", e);
    window.location.assign(url);
  }
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
  if (progFill) progFill.style.width = "0%";
  if (progBar) progBar.setAttribute("aria-valuenow", "0");
  if (progLabel) progLabel.textContent = "Starting…";
  if (progTrack) progTrack.textContent = "";
  if (progSpeed) progSpeed.textContent = "";
  if (progPct) progPct.textContent = "0%";
}

function setProgress(pct, speed) {
  if (progFill) progFill.style.width = `${pct}%`;
  if (progBar) progBar.setAttribute("aria-valuenow", pct);
  if (progLabel) progLabel.textContent = `Downloading… ${pct}%`;
  if (progPct) progPct.textContent = `${pct}%`;
  if (speed && progSpeed) progSpeed.textContent = speed;
}

function sanitizeFolderName(name) {
  return name.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").replace(/^[\s.]+|[\s.]+$/g, "") || "Playlist";
}

// ── Init ─────────────────────────────────────────────────────
renderHistory();
urlInput.focus();
