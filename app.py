import os
import re
import json
import uuid
import shutil
import zipfile
import threading
import queue
import tempfile
import subprocess
import concurrent.futures
from pathlib import Path

import requests as http_requests
from bs4 import BeautifulSoup
from flask import (
    Flask, request, jsonify, Response,
    render_template, send_from_directory
)
import yt_dlp

app = Flask(__name__, static_folder="static", template_folder="templates")

# ── Storage ───────────────────────────────────────────────────────────────────
IS_CLOUD = (
    os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RENDER")
    or os.environ.get("FLY_APP_NAME")
)

BASE_DIR = Path(tempfile.gettempdir()) / "wavesnap" if IS_CLOUD else Path("downloads")
BASE_DIR.mkdir(parents=True, exist_ok=True)

# Per-job SSE queues
_job_queues: dict[str, queue.Queue] = {}
_job_lock = threading.Lock()


# ── URL Detection & Helpers ───────────────────────────────────────────────────

def is_spotify(url: str) -> bool:
    return "open.spotify.com" in url or "spotify.com" in url


def spotify_type(url: str) -> str:
    """Return 'track', 'playlist', 'album', or 'artist'."""
    for t in ("playlist", "album", "track", "artist"):
        if f"/{t}/" in url:
            return t
    return "track"


def sanitize_folder(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip(". ") or "Unknown"


def _make_ytdlp_hook(job_id: str):
    def hook(d):
        q = _job_queues.get(job_id)
        if not q:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done  = d.get("downloaded_bytes", 0)
            pct   = round((done / total) * 100, 1) if total else 0
            spd   = d.get("speed") or 0
            spd_s = f"{spd/1_000_000:.2f} MB/s" if spd >= 1_000_000 else f"{spd/1024:.1f} KB/s"
            fname = Path(d.get("filename", "")).name
            q.put({"type": "progress", "percent": pct, "speed": spd_s,
                   "eta": d.get("eta") or 0, "filename": fname})
        elif status == "finished":
            fname = Path(d.get("filename", "")).name
            q.put({"type": "track_done", "filename": fname})
        elif status == "error":
            q.put({"type": "error", "message": str(d.get("error", "Unknown"))})
    return hook


def _ydl_opts(job_id: str, out_dir: Path) -> dict:
    has_ffmpeg = bool(shutil.which("ffmpeg"))
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "writethumbnail": True,
        "convert_thumbnails": "jpg",
        "addmetadata": True,
        "embedthumbnail": True,
        "ignoreerrors": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_make_ytdlp_hook(job_id)],
    }
    if has_ffmpeg:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ]
    return opts


# ── Metadata tagging via mutagen ──────────────────────────────────────────────

def _tag_mp3(file_path: Path, title: str, artist: str, album: str = "", cover_url: str = ""):
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
        from mutagen.mp3 import MP3
        if file_path.suffix.lower() != ".mp3":
            return
        try:
            audio = MP3(file_path, ID3=ID3)
        except ID3NoHeaderError:
            audio = MP3(file_path)
            audio.add_tags()
        if title:
            audio.tags.add(TIT2(encoding=3, text=title))
        if artist:
            audio.tags.add(TPE1(encoding=3, text=artist))
        if album:
            audio.tags.add(TALB(encoding=3, text=album))
        if cover_url:
            try:
                resp = http_requests.get(cover_url, timeout=5)
                if resp.status_code == 200:
                    audio.tags.add(APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=resp.content
                    ))
            except Exception:
                pass
        audio.save()
    except Exception as exc:
        print(f"Error tagging {file_path}: {exc}")


# ── Spotify Info Extractor ───────────────────────────────────────────────────

def _spotify_info(url: str) -> dict:
    clean_url = url.split("?")[0].rstrip("/")
    stype = spotify_type(url)
    is_playlist = stype in ("playlist", "album", "artist")

    # Method 1: Extract full entity and trackList from Spotify embed
    try:
        parts = clean_url.split("/")
        if len(parts) >= 2:
            item_id = parts[-1]
            item_type = parts[-2]
            embed_url = f"https://open.spotify.com/embed/{item_type}/{item_id}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = http_requests.get(embed_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                script = soup.find("script", id="__NEXT_DATA__")
                if script and script.string:
                    data = json.loads(script.string)
                    entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                    if entity:
                        title = entity.get("name") or entity.get("title") or "Spotify Audio"
                        subtitle = entity.get("subtitle") or "Spotify"

                        # High-resolution cover art
                        thumb = ""
                        covers = entity.get("coverArt", {}).get("sources", [])
                        if covers:
                            thumb = covers[0].get("url", "")

                        # Track list
                        raw_tracks = entity.get("trackList", [])
                        tracks = []
                        for t in raw_tracks:
                            t_title = t.get("title") or t.get("name") or ""
                            t_artist = t.get("subtitle") or subtitle
                            if t_title:
                                tracks.append({
                                    "title": t_title,
                                    "artist": t_artist,
                                    "duration": t.get("duration", 0) / 1000 if t.get("duration") else None
                                })

                        # If single track embed without trackList
                        if not is_playlist and not tracks:
                            tracks = [{"title": title, "artist": subtitle, "duration": entity.get("duration", 0) / 1000 if entity.get("duration") else None}]

                        duration = entity.get("duration", 0) / 1000 if entity.get("duration") else None
                        count = len(tracks) if is_playlist else 1

                        return {
                            "title": title,
                            "thumbnail": thumb,
                            "channel": f"{subtitle} • Spotify",
                            "is_playlist": is_playlist,
                            "count": count,
                            "duration": duration,
                            "webpage_url": url,
                            "source": "spotify",
                            "spotify_type": stype,
                            "tracks": tracks
                        }
    except Exception as exc:
        print(f"Spotify embed parse error: {exc}")

    # Method 2: Fallback to oEmbed (official API endpoint)
    try:
        resp = http_requests.get(
            "https://open.spotify.com/oembed",
            params={"url": url},
            timeout=10,
            headers={"User-Agent": "WaveSnap/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        title = data.get("title", "Spotify Track")
        return {
            "title": title,
            "thumbnail": data.get("thumbnail_url", ""),
            "channel": "Spotify",
            "is_playlist": is_playlist,
            "count": 1 if not is_playlist else None,
            "duration": None,
            "webpage_url": url,
            "source": "spotify",
            "spotify_type": stype,
            "tracks": [{"title": title, "artist": "Spotify"}] if not is_playlist else []
        }
    except Exception as exc:
        raise Exception(f"Could not fetch Spotify info: {exc}")


# ── Spotify Downloader ────────────────────────────────────────────────────────

def _download_spotify(job_id: str, url: str, out_dir: Path, title: str = "", tracks: list = None, cover_url: str = ""):
    q = _job_queues[job_id]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        has_ffmpeg = bool(shutil.which("ffmpeg"))

        # If tracks were not passed from frontend, scrape them now
        if not tracks:
            try:
                info = _spotify_info(url)
                tracks = info.get("tracks", [])
                if not cover_url:
                    cover_url = info.get("thumbnail", "")
                if not title:
                    title = info.get("title", "")
            except Exception as e:
                print(f"Failed to fetch Spotify tracks for download: {e}")
                tracks = []

        # If we have the track list, download each track at maximum quality in parallel!
        if tracks:
            total_tracks = len(tracks)
            completed_lock = threading.Lock()
            completed_count = 0

            q.put({
                "type": "progress",
                "percent": 0,
                "speed": "",
                "filename": f"Starting download for {total_tracks} tracks…"
            })

            def download_single_track(idx: int, tr: dict):
                nonlocal completed_count
                t_title = tr.get("title", "").strip()
                t_artist = tr.get("artist", "").strip()
                if not t_title:
                    return

                sanitized_artist = sanitize_folder(t_artist)
                sanitized_title = sanitize_folder(t_title)
                base_name = f"{sanitized_artist} - {sanitized_title}"

                # Target file path
                mp3_path = out_dir / f"{base_name}.mp3"
                if mp3_path.exists() and mp3_path.stat().st_size > 50000:
                    with completed_lock:
                        completed_count += 1
                        pct = round((completed_count / total_tracks) * 100, 1)
                        q.put({"type": "progress", "percent": pct, "speed": "", "filename": f"{base_name}.mp3"})
                        q.put({"type": "track_done", "filename": f"{base_name}.mp3"})
                    return

                query = f"ytsearch1:{t_title} {t_artist} audio"
                outtmpl = str(out_dir / f"{base_name}.%(ext)s")

                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": outtmpl,
                    "quiet": True,
                    "no_warnings": True,
                    "ignoreerrors": True,
                    "retries": 3,
                    "socket_timeout": 25,
                }
                if has_ffmpeg:
                    ydl_opts["postprocessors"] = [
                        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
                        {"key": "FFmpegMetadata", "add_metadata": True},
                    ]

                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([query])

                    # Tag the MP3 with official metadata
                    if mp3_path.exists():
                        _tag_mp3(mp3_path, title=t_title, artist=t_artist, album=title, cover_url=cover_url)

                    with completed_lock:
                        completed_count += 1
                        pct = round((completed_count / total_tracks) * 100, 1)
                        q.put({"type": "progress", "percent": pct, "speed": "", "filename": f"{base_name}.mp3"})
                        q.put({"type": "track_done", "filename": f"{base_name}.mp3"})
                except Exception as exc:
                    print(f"Error downloading {base_name}: {exc}")
                    with completed_lock:
                        completed_count += 1
                        pct = round((completed_count / total_tracks) * 100, 1)
                        q.put({"type": "progress", "percent": pct, "speed": "", "filename": f"Skipped: {base_name}"})

            # Download with 3 concurrent workers for high throughput without rate limits
            workers = min(3, max(1, total_tracks))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(download_single_track, i, tr) for i, tr in enumerate(tracks, 1)]
                concurrent.futures.wait(futures)

        else:
            # Fallback to spotdl CLI if installed
            spotdl_bin = shutil.which("spotdl")
            if not spotdl_bin:
                # Try python -m spotdl
                spotdl_bin = sys.executable

            cmd = [
                spotdl_bin,
                "download",
                url,
                "--output", str(out_dir / "{title} - {artists}.{output-ext}"),
                "--format", "mp3",
                "--bitrate", "256k",
                "--threads", "4",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for raw in proc.stdout:
                line = raw.strip()
                if line:
                    q.put({"type": "progress", "percent": 50, "speed": "", "filename": line})
            proc.wait()

    except Exception as exc:
        q.put({"type": "error", "message": str(exc)})
    finally:
        q.put({"type": "done", "out_dir": str(out_dir)})


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json(force=True, silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        if is_spotify(url):
            return jsonify(_spotify_info(url))

        # yt-dlp for YouTube, SoundCloud, Bandcamp, Vimeo, etc.
        opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "extract_flat": "in_playlist",
            "socket_timeout": 20,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return jsonify({"error": "Could not fetch info — check the URL"}), 400

        is_playlist = info.get("_type") == "playlist"
        entries     = info.get("entries", [])
        return jsonify({
            "title":       info.get("title") or "Unknown",
            "thumbnail":   info.get("thumbnail") or (entries[0].get("thumbnail") if entries else ""),
            "channel":     info.get("uploader") or info.get("channel") or "",
            "is_playlist": is_playlist,
            "count":       len(entries) if is_playlist else 1,
            "duration":    info.get("duration"),
            "webpage_url": info.get("webpage_url") or url,
            "source":      "ytdlp",
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    data        = request.get_json(force=True, silent=True) or {}
    url         = (data.get("url") or "").strip()
    info_title  = (data.get("title") or "").strip()
    is_playlist = bool(data.get("is_playlist"))
    source      = data.get("source", "ytdlp")
    tracks      = data.get("tracks", [])
    cover_url   = data.get("thumbnail", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())

    # Folder logic: playlists get a named subfolder; singles go flat
    if is_playlist:
        folder_name = sanitize_folder(info_title) if info_title else job_id
        out_dir = BASE_DIR / folder_name
    else:
        out_dir = BASE_DIR

    out_dir.mkdir(parents=True, exist_ok=True)

    q: queue.Queue = queue.Queue()
    with _job_lock:
        _job_queues[job_id] = q

    def run():
        if source == "spotify" or is_spotify(url):
            _download_spotify(job_id, url, out_dir, title=info_title, tracks=tracks, cover_url=cover_url)
        else:
            try:
                with yt_dlp.YoutubeDL(_ydl_opts(job_id, out_dir)) as ydl:
                    ydl.download([url])
            except Exception as exc:
                q.put({"type": "error", "message": str(exc)})
            finally:
                q.put({"type": "done", "out_dir": str(out_dir)})

    threading.Thread(target=run, daemon=True).start()

    return jsonify({
        "job_id": job_id,
        "out_dir": str(out_dir),
        "folder": out_dir.name if is_playlist else None,
    })


@app.route("/api/progress/<job_id>")
def progress_stream(job_id: str):
    q = _job_queues.get(job_id)
    if q is None:
        return Response(
            'data: {"type":"error","message":"Job not found"}\n\n',
            mimetype="text/event-stream",
        )

    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/files-for-job", methods=["POST"])
def files_for_job():
    data        = request.get_json(force=True, silent=True) or {}
    out_dir_str = data.get("out_dir", "")
    out_dir     = Path(out_dir_str) if out_dir_str else BASE_DIR

    if not out_dir.exists():
        return jsonify({"files": [], "folder": ""})

    audio_exts = {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"}
    files = []
    is_folder = out_dir != BASE_DIR
    folder_name = out_dir.name if is_folder else ""

    for path in sorted(out_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in audio_exts:
            files.append({
                "name":   path.name,
                "size":   path.stat().st_size,
                "rel":    str(path.relative_to(BASE_DIR)),
                "folder": folder_name,
            })
    return jsonify({
        "files": files,
        "folder": folder_name,
        "total_size": sum(f["size"] for f in files)
    })


@app.route("/api/download-file")
def download_file():
    rel = request.args.get("path", "")
    if not rel or ".." in rel:
        return "Invalid path", 400
    full = BASE_DIR / rel
    if not full.exists():
        return "File not found", 404
    return send_from_directory(str(full.parent.resolve()), full.name, as_attachment=True)


@app.route("/api/download-zip")
def download_zip():
    folder = request.args.get("folder", "").strip()
    title  = request.args.get("title", folder).strip()
    if not folder or ".." in folder or "/" in folder or "\\" in folder:
        return "Invalid folder", 400

    target_dir = BASE_DIR / folder
    if not target_dir.exists() or not target_dir.is_dir():
        return "Folder not found", 404

    zip_filename = f"{sanitize_folder(title or folder)}.zip"
    zip_path = BASE_DIR / f"{folder}.zip"

    audio_exts = {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in target_dir.iterdir():
            if f.is_file() and f.suffix.lower() in audio_exts:
                zf.write(f, arcname=f.name)

    return send_from_directory(
        str(BASE_DIR.resolve()),
        zip_path.name,
        as_attachment=True,
        download_name=zip_filename
    )


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = not IS_CLOUD
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
