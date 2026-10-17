import os
import re
import json
import uuid
import threading
import queue
import tempfile
from pathlib import Path

from flask import (
    Flask, request, jsonify, Response,
    render_template, send_from_directory, send_file
)
import yt_dlp

app = Flask(__name__, static_folder="static", template_folder="templates")

# ── Storage ──────────────────────────────────────────────────────────────────
# On Railway/cloud: use /tmp (ephemeral but works)
# On local: use ./downloads
IS_CLOUD = os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER") or os.environ.get("FLY_APP_NAME")

if IS_CLOUD:
    BASE_DIR = Path(tempfile.gettempdir()) / "wavesnap"
else:
    BASE_DIR = Path("downloads")

BASE_DIR.mkdir(parents=True, exist_ok=True)

# Per-job progress queues
_job_queues: dict[str, queue.Queue] = {}
_job_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize_folder(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name or "Unknown"


def _make_hook(job_id: str):
    def hook(d):
        q = _job_queues.get(job_id)
        if q is None:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            percent = round((downloaded / total) * 100, 1) if total else 0
            speed = d.get("speed") or 0
            speed_str = (
                f"{speed / 1_000_000:.2f} MB/s" if speed >= 1_000_000
                else f"{speed / 1024:.1f} KB/s"
            )
            q.put({
                "type": "progress",
                "percent": percent,
                "speed": speed_str,
                "eta": d.get("eta") or 0,
                "filename": d.get("filename", ""),
            })
        elif status == "finished":
            q.put({"type": "track_done", "filename": d.get("filename", "")})
        elif status == "error":
            q.put({"type": "error", "message": str(d.get("error", "Unknown error"))})
    return hook


def _ydl_opts(job_id: str, out_dir: Path) -> dict:
    """Best-quality MP3 with full metadata + embedded album art."""
    return {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",   # V0 = highest VBR quality
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
            {
                "key": "EmbedThumbnail",
            },
        ],
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
        "progress_hooks": [_make_hook(job_id)],
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return jsonify({"error": "Could not fetch info — check the URL"}), 400

        is_playlist = info.get("_type") == "playlist"
        entries = info.get("entries", [])

        return jsonify({
            "title":       info.get("title") or "Unknown",
            "thumbnail":   info.get("thumbnail") or (entries[0].get("thumbnail") if entries else ""),
            "channel":     info.get("uploader") or info.get("channel") or "",
            "is_playlist": is_playlist,
            "count":       len(entries) if is_playlist else 1,
            "duration":    info.get("duration"),
            "webpage_url": info.get("webpage_url") or url,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True, silent=True) or {}
    url         = (data.get("url") or "").strip()
    info_title  = (data.get("title") or "").strip()
    is_playlist = bool(data.get("is_playlist"))

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())

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
        opts = _ydl_opts(job_id, out_dir)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
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
    data = request.get_json(force=True, silent=True) or {}
    out_dir_str = data.get("out_dir", "")
    out_dir = Path(out_dir_str) if out_dir_str else BASE_DIR

    if not out_dir.exists():
        return jsonify({"files": []})

    audio_exts = {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"}
    files = []
    for path in sorted(out_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in audio_exts:
            files.append({
                "name":   path.name,
                "size":   path.stat().st_size,
                "rel":    str(path.relative_to(BASE_DIR)),
                "folder": out_dir.name if out_dir != BASE_DIR else "",
            })
    return jsonify({"files": files})


@app.route("/api/download-file")
def download_file():
    rel = request.args.get("path", "")
    if not rel or ".." in rel:
        return "Invalid path", 400
    full = BASE_DIR / rel
    if not full.exists():
        return "File not found", 404
    return send_from_directory(
        str(full.parent.resolve()),
        full.name,
        as_attachment=True,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = not IS_CLOUD
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
