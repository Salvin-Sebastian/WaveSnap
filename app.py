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
from urllib.parse import quote

import requests as http_requests
from bs4 import BeautifulSoup
import time
from flask import (
    Flask, request, jsonify, Response,
    render_template, send_from_directory, after_this_request
)
import yt_dlp

# Ensure static ffmpeg is in PATH for Render / Cloud / Local environments
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception as _e:
    print(f"static_ffmpeg note: {_e}")

FFMPEG_PATH = shutil.which("ffmpeg")
FFPROBE_PATH = shutil.which("ffprobe")
FFMPEG_DIR = str(Path(FFMPEG_PATH).parent) if FFMPEG_PATH else None

# Check for user-provided YouTube cookies (via cookies.txt or YOUTUBE_COOKIES env var)
COOKIE_FILE = None
if os.path.exists("cookies.txt"):
    COOKIE_FILE = "cookies.txt"
elif os.environ.get("YOUTUBE_COOKIES"):
    try:
        _c_path = Path(tempfile.gettempdir()) / "wavesnap_cookies.txt"
        _c_path.write_text(os.environ["YOUTUBE_COOKIES"].strip(), encoding="utf-8")
        COOKIE_FILE = str(_c_path)
    except Exception as _ce:
        print(f"Cookie env setup note: {_ce}")

app = Flask(__name__, static_folder="static", template_folder="templates")

# ── Storage ───────────────────────────────────────────────────────────────────
IS_CLOUD = (
    os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RENDER")
    or os.environ.get("FLY_APP_NAME")
)

BASE_DIR = Path(tempfile.gettempdir()) / "wavesnap" if IS_CLOUD else Path("downloads")
BASE_DIR.mkdir(parents=True, exist_ok=True)

# Per-job SSE queues & cancellation state
_job_queues: dict[str, queue.Queue] = {}
_job_cancelled: dict[str, bool] = {}
_job_lock = threading.Lock()


def _cleanup_old_files():
    """Periodically purges files older than 10 minutes so Render server disk stays clean."""
    while True:
        try:
            time.sleep(180)  # check every 3 minutes
            now = time.time()
            if BASE_DIR.exists():
                for p in BASE_DIR.iterdir():
                    try:
                        # Clean up folders/files older than 10 mins (600s)
                        if (now - p.stat().st_mtime) > 600:
                            if p.is_dir():
                                shutil.rmtree(p, ignore_errors=True)
                            else:
                                p.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            pass

threading.Thread(target=_cleanup_old_files, daemon=True).start()


# ── URL Detection & Helpers ───────────────────────────────────────────────────

def resolve_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""

    # Check for Spotify URI scheme (e.g., spotify:track:..., spotify:playlist:...)
    m_sp_uri = re.search(r'spotify:(track|playlist|album|artist):([a-zA-Z0-9]+)', url, re.IGNORECASE)
    if m_sp_uri:
        return f"https://open.spotify.com/{m_sp_uri.group(1).lower()}/{m_sp_uri.group(2)}"

    # Extract clean http(s) URL if user pasted text with surrounding words
    m = re.search(r'https?://[^\s"\'<>]+', url)
    if m:
        url = m.group(0)
    elif any(d in url.lower() for d in ("spotify.com", "spotify.link", "spoti.fi", "youtube.com", "youtu.be", "music.youtube.com", "soundcloud.com", "apple.com", "bandcamp.com")):
        url = "https://" + url.lstrip("/")

    # Resolve shortlinks (spotify.link, spoti.fi, youtu.be, etc.)
    short_domains = ("spotify.link", "spoti.fi", "youtu.be", "tinyurl.com", "bit.ly", "t.co")
    if any(sd in url.lower() for sd in short_domains):
        try:
            resp = http_requests.head(url, allow_redirects=True, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            if resp.url and resp.url.startswith("http"):
                url = resp.url
        except Exception:
            try:
                resp = http_requests.get(url, allow_redirects=True, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
                if resp.url and resp.url.startswith("http"):
                    url = resp.url
            except Exception:
                pass
    return url


def is_spotify(url: str) -> bool:
    u = url.lower().strip()
    return (
        u.startswith("spotify:")
        or bool(re.search(r'spotify:(?:track|playlist|album|artist):[a-zA-Z0-9]+', u, re.IGNORECASE))
        or any(k in u for k in ("spotify.com", "open.spotify", "spotify.link", "spoti.fi"))
    )


def get_spotify_id_and_type(url: str) -> tuple[str, str]:
    """Extract (type, id) from Spotify URL or URI reliably across all formats."""
    # 1. Spotify URI format: spotify:track:4cOdK2wGLETKBW3PvgPWqT
    m_uri = re.search(r'spotify:(track|playlist|album|artist):([a-zA-Z0-9]+)', url, re.IGNORECASE)
    if m_uri:
        return m_uri.group(1).lower(), m_uri.group(2)

    # 2. Web URL format (including /intl-xx/ paths, embed paths, and query parameters)
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    for t in ("playlist", "album", "track", "artist"):
        m = re.search(rf'/(?:intl-[a-z]{{2,4}}/)?(?:embed/)?{t}/([a-zA-Z0-9]+)', clean, re.IGNORECASE)
        if m:
            return t, m.group(1)

    parts = [p for p in clean.split("/") if p]
    return ("track", parts[-1]) if parts else ("track", "")


def spotify_type(url: str) -> str:
    t, _ = get_spotify_id_and_type(url)
    return t


def analyze_software_link(raw_input: str) -> dict:
    """
    Analyzes any input link or query, identifies the originating music software/platform,
    and returns its dedicated execution engine metadata.
    """
    url = resolve_url(raw_input)
    u = url.lower()

    # 1. Spotify
    if is_spotify(raw_input) or is_spotify(url):
        stype, sid = get_spotify_id_and_type(url)
        is_pl = stype in ("playlist", "album", "artist")
        type_label = "Playlist" if stype == "playlist" else ("Album" if stype == "album" else ("Artist Discography" if stype == "artist" else "Track"))
        return {
            "platform_id": "spotify",
            "name": "Spotify",
            "software": "Spotify Music Downloader",
            "engine": "Spotify Dedicated Engine",
            "icon": "🟢",
            "badge_color": "#1DB954",
            "type": stype,
            "type_label": type_label,
            "is_playlist": is_pl,
            "url": url,
            "id": sid,
            "description": f"Spotify {type_label} detected. Executes dedicated Spotify tracklist extractor & batch MP3 downloader."
        }

    # 2. YouTube Music
    if "music.youtube.com" in u:
        is_pl = "list=" in u or "/playlist" in u
        type_label = "Playlist / Album" if is_pl else "Song"
        return {
            "platform_id": "ytmusic",
            "name": "YouTube Music",
            "software": "YouTube Music Downloader",
            "engine": "YouTube Music Dedicated Engine",
            "icon": "⭕",
            "badge_color": "#FF0000",
            "type": "playlist" if is_pl else "track",
            "type_label": type_label,
            "is_playlist": is_pl,
            "url": url,
            "description": f"YouTube Music {type_label} detected. Executes YouTube Music high-fidelity audio pipeline."
        }

    # 3. YouTube
    if any(k in u for k in ("youtube.com", "youtu.be")):
        is_pl = "list=" in u or "/playlist" in u
        is_short = "/shorts/" in u
        type_label = "Playlist" if is_pl else ("Shorts" if is_short else "Video")
        return {
            "platform_id": "youtube",
            "name": "YouTube",
            "software": "YouTube Audio Downloader",
            "engine": "YouTube High-Fidelity Engine",
            "icon": "🔴",
            "badge_color": "#FF0000",
            "type": "playlist" if is_pl else ("short" if is_short else "video"),
            "type_label": type_label,
            "is_playlist": is_pl,
            "url": url,
            "description": f"YouTube {type_label} detected. Executes YouTube audio extraction & metadata tagging."
        }

    # 4. Apple Music
    if any(k in u for k in ("music.apple.com", "itunes.apple.com")):
        is_pl = "/playlist/" in u or "/album/" in u
        type_label = "Playlist" if "/playlist/" in u else ("Album" if "/album/" in u else "Song")
        return {
            "platform_id": "applemusic",
            "name": "Apple Music",
            "software": "Apple Music Downloader",
            "engine": "Apple Music Resolver Engine",
            "icon": "🍎",
            "badge_color": "#FA2D48",
            "type": "playlist" if "/playlist/" in u else ("album" if "/album/" in u else "track"),
            "type_label": type_label,
            "is_playlist": is_pl,
            "url": url,
            "description": f"Apple Music {type_label} detected. Executes Apple Music catalog resolver & batch audio downloader."
        }

    # 5. SoundCloud
    if any(k in u for k in ("soundcloud.com", "on.soundcloud.com")):
        is_pl = "/sets/" in u
        type_label = "Set / Playlist" if is_pl else "Track"
        return {
            "platform_id": "soundcloud",
            "name": "SoundCloud",
            "software": "SoundCloud Downloader",
            "engine": "SoundCloud Stream Engine",
            "icon": "🟠",
            "badge_color": "#FF5500",
            "type": "set" if is_pl else "track",
            "type_label": type_label,
            "is_playlist": is_pl,
            "url": url,
            "description": f"SoundCloud {type_label} detected. Executes direct SoundCloud high-bitrate stream extraction."
        }

    # 6. JioSaavn
    if any(k in u for k in ("jiosaavn.com", "saavn.com")):
        is_pl = "/album/" in u or "/playlist/" in u or "/featured/" in u
        type_label = "Playlist" if "/playlist/" in u else ("Album" if "/album/" in u else "Song")
        return {
            "platform_id": "jiosaavn",
            "name": "JioSaavn",
            "software": "JioSaavn 320kbps Downloader",
            "engine": "JioSaavn CD Quality Engine",
            "icon": "🟢",
            "badge_color": "#2bc5b4",
            "type": "playlist" if is_pl else "track",
            "type_label": type_label,
            "is_playlist": is_pl,
            "url": url,
            "description": f"JioSaavn {type_label} detected. Executes direct 320kbps CD audio extraction."
        }

    # 7. Bandcamp
    if "bandcamp.com" in u:
        is_pl = "/album/" in u
        type_label = "Album" if is_pl else "Track"
        return {
            "platform_id": "bandcamp",
            "name": "Bandcamp",
            "software": "Bandcamp Downloader",
            "engine": "Bandcamp Lossless Engine",
            "icon": "🔵",
            "badge_color": "#629AA9",
            "type": "album" if is_pl else "track",
            "type_label": type_label,
            "is_playlist": is_pl,
            "url": url,
            "description": f"Bandcamp {type_label} detected. Executes Bandcamp lossless/high-bitrate extraction."
        }

    # 7. TikTok
    if "tiktok.com" in u:
        return {
            "platform_id": "tiktok",
            "name": "TikTok",
            "software": "TikTok Audio Downloader",
            "engine": "TikTok Audio Extractor",
            "icon": "🎵",
            "badge_color": "#00F2FE",
            "type": "video",
            "type_label": "TikTok Audio",
            "is_playlist": False,
            "url": url,
            "description": "TikTok link detected. Executes TikTok audio track extractor."
        }

    # 8. Instagram
    if "instagram.com" in u:
        return {
            "platform_id": "instagram",
            "name": "Instagram",
            "software": "Instagram Audio Downloader",
            "engine": "Instagram Reel Extractor",
            "icon": "📸",
            "badge_color": "#E1306C",
            "type": "video",
            "type_label": "Instagram Audio",
            "is_playlist": False,
            "url": url,
            "description": "Instagram link detected. Executes Instagram reel audio extractor."
        }

    # 9. Generic Web URL vs Search Query
    is_http = u.startswith("http://") or u.startswith("https://")
    if is_http:
        return {
            "platform_id": "generic",
            "name": "Web Media",
            "software": "Universal Media Downloader",
            "engine": "Universal Audio Extractor",
            "icon": "🌐",
            "badge_color": "#8B5CF6",
            "type": "stream",
            "type_label": "Web Audio",
            "is_playlist": False,
            "url": url,
            "description": "Web media link detected. Executes universal yt-dlp audio extractor."
        }
    else:
        return {
            "platform_id": "search",
            "name": "Direct Search",
            "software": "Audio Search Engine",
            "engine": "Direct Search & Match Engine",
            "icon": "🔍",
            "badge_color": "#3B82F6",
            "type": "query",
            "type_label": "Song Search",
            "is_playlist": False,
            "url": url,
            "description": "Search query detected. Executes direct audio search and downloads the top result."
        }


def sanitize_folder(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip(". ") or "Unknown"


def _make_ytdlp_hook(job_id: str, out_dir: Path = None):
    def hook(d):
        if _job_cancelled.get(job_id):
            raise Exception("Download cancelled by user")
        q = _job_queues.get(job_id)
        if not q:
            return

        status = d.get("status")
        info_dict = d.get("info_dict") or {}
        p_index = d.get("playlist_index") or info_dict.get("playlist_index") or 1
        p_count = d.get("playlist_count") or info_dict.get("playlist_count") or info_dict.get("n_entries") or 1
        is_pl = bool(p_count and p_count > 1)

        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done  = d.get("downloaded_bytes", 0)
            pct   = round((done / total) * 100, 1) if total else 0
            spd   = d.get("speed") or 0
            spd_s = f"{spd/1_000_000:.2f} MB/s" if spd >= 1_000_000 else f"{spd/1024:.1f} KB/s"
            fname = Path(d.get("filename", "")).name

            done_mb = round(done / (1024 * 1024), 1)
            total_mb = round(total / (1024 * 1024), 1) if total else 0

            if is_pl:
                track_pct = (pct / 100.0) if pct else 0
                overall_pct = min(92.0, round((((p_index - 1) + track_pct) / p_count) * 92.0, 1))

                prev_bytes = 0
                if out_dir and out_dir.exists():
                    prev_bytes = sum(f.stat().st_size for f in out_dir.iterdir() if f.is_file() and f.name != fname)
                pl_done_bytes = prev_bytes + done
                pl_done_mb = round(pl_done_bytes / (1024 * 1024), 1)

                if total and p_count:
                    pl_total_mb = round((total * p_count) / (1024 * 1024), 1)
                else:
                    pl_total_mb = round(p_count * 8.5, 1)

                data_text = f"{pl_done_mb:.1f} MB / ~{pl_total_mb:.1f} MB"
                q.put({
                    "type": "playlist_progress",
                    "percent": overall_pct,
                    "speed": spd_s,
                    "done": p_index,
                    "total": p_count,
                    "done_mb": pl_done_mb,
                    "total_mb": pl_total_mb,
                    "data_text": data_text,
                    "filename": fname,
                    "status_text": f"Packaging song {p_index} of {p_count} into ZIP ({overall_pct}%)"
                })
            else:
                data_text = f"{done_mb:.1f} MB / {total_mb:.1f} MB" if total_mb else f"{done_mb:.1f} MB"
                q.put({
                    "type": "progress",
                    "percent": pct,
                    "speed": spd_s,
                    "eta": d.get("eta") or 0,
                    "done_mb": done_mb,
                    "total_mb": total_mb,
                    "data_text": data_text,
                    "filename": fname
                })

        elif status == "finished":
            fname = Path(d.get("filename", "")).name
            if is_pl:
                overall_pct = min(92.0, round((p_index / p_count) * 92.0, 1))
                cur_bytes = sum(f.stat().st_size for f in out_dir.iterdir() if f.is_file()) if out_dir and out_dir.exists() else 0
                cur_mb = round(cur_bytes / (1024 * 1024), 1)
                est_mb = round(p_count * 8.5, 1)
                data_text = f"{cur_mb:.1f} MB / ~{est_mb:.1f} MB"
                q.put({
                    "type": "playlist_progress",
                    "percent": overall_pct,
                    "speed": "",
                    "done": p_index,
                    "total": p_count,
                    "done_mb": cur_mb,
                    "total_mb": est_mb,
                    "data_text": data_text,
                    "filename": fname,
                    "status_text": f"Packaging song {p_index} of {p_count} into ZIP ({overall_pct}%)"
                })
            else:
                f_path = (out_dir / fname) if out_dir else None
                f_size = f_path.stat().st_size if f_path and f_path.exists() else 0
                f_mb = round(f_size / (1024 * 1024), 1) if f_size else 0
                data_text = f"{f_mb:.1f} MB / {f_mb:.1f} MB" if f_mb else ""
                q.put({
                    "type": "track_done",
                    "filename": fname,
                    "done_mb": f_mb,
                    "total_mb": f_mb,
                    "data_text": data_text
                })

        elif status == "error":
            if is_pl:
                print(f"[SKIP] Video {p_index} error in playlist skipped: {d.get('error')}")
            else:
                q.put({"type": "error", "message": str(d.get("error", "Unknown"))})
    return hook


def _ydl_opts(job_id: str, out_dir: Path, codec: str = "mp3", quality: str = "320") -> dict:
    has_ffmpeg = bool(FFMPEG_PATH or shutil.which("ffmpeg"))
    ffmpeg_loc = FFMPEG_DIR or (str(Path(shutil.which("ffmpeg")).parent) if shutil.which("ffmpeg") else None)
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
        "progress_hooks": [_make_ytdlp_hook(job_id, out_dir)],
        "geo_bypass": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "visionos"],
                "player_skip": ["web", "mweb", "tv"]
            }
        },
    }
    if ffmpeg_loc:
        opts["ffmpeg_location"] = ffmpeg_loc
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    if has_ffmpeg:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality, "nopostoverwrites": False},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ]
    return opts


# ── Metadata tagging via mutagen (MP3 & M4A/AAC) ──────────────────────────────

def _tag_audio(file_path: Path, title: str, artist: str, album: str = "", cover_url: str = "", cover_data: bytes = None):
    try:
        if not cover_data and cover_url:
            try:
                resp = http_requests.get(cover_url, timeout=5)
                if resp.status_code == 200:
                    cover_data = resp.content
            except Exception:
                pass

        ext = file_path.suffix.lower()
        if ext == ".mp3":
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
            from mutagen.mp3 import MP3
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
            if cover_data:
                try:
                    audio.tags.add(APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=cover_data
                    ))
                except Exception:
                    pass
            audio.save()

        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(file_path)
            if title:
                audio["\xa9nam"] = [title]
            if artist:
                audio["\xa9ART"] = [artist]
            if album:
                audio["\xa9alb"] = [album]
            if cover_data:
                try:
                    fmt = MP4Cover.FORMAT_JPEG if (b"JFIF" in cover_data[:20] or b"\xff\xd8" in cover_data[:4]) else MP4Cover.FORMAT_PNG
                    audio["covr"] = [MP4Cover(cover_data, imageformat=fmt)]
                except Exception:
                    pass
            audio.save()

    except Exception as exc:
        print(f"Error tagging {file_path}: {exc}")


# ── Spotify Info Extractor ───────────────────────────────────────────────────

def _spotify_info(url: str) -> dict:
    url = resolve_url(url)
    stype, item_id = get_spotify_id_and_type(url)
    is_playlist = stype in ("playlist", "album", "artist")
    canonical_url = f"https://open.spotify.com/{stype}/{item_id}" if item_id else url

    # Method 1: Extract full entity and trackList from Spotify embed
    if item_id:
        try:
            embed_url = f"https://open.spotify.com/embed/{stype}/{item_id}"
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
                        
                        # Extract artist names from entity.artists or subtitle
                        artists = entity.get("artists", [])
                        artist_names = ", ".join([a.get("name") for a in artists if a.get("name")]) if artists else ""
                        subtitle = entity.get("subtitle") or artist_names or "Spotify"

                        # High-resolution cover art (check visualIdentity.image and coverArt.sources)
                        thumb = ""
                        images = (
                            entity.get("visualIdentity", {}).get("image", [])
                            or entity.get("coverArt", {}).get("sources", [])
                        )
                        if images:
                            images_sorted = sorted(
                                images,
                                key=lambda x: (x.get("maxWidth", 0) or x.get("width", 0) or x.get("maxHeight", 0)),
                                reverse=True
                            )
                            thumb = images_sorted[0].get("url", "")

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
                            tracks = [{
                                "title": title,
                                "artist": subtitle,
                                "duration": entity.get("duration", 0) / 1000 if entity.get("duration") else None
                            }]

                        duration = entity.get("duration", 0) / 1000 if entity.get("duration") else None
                        count = len(tracks) if is_playlist else 1
                        channel_label = f"{subtitle} · Spotify" if subtitle != "Spotify" else "Spotify"

                        return {
                            "title": title,
                            "thumbnail": thumb,
                            "channel": channel_label,
                            "is_playlist": is_playlist,
                            "count": count,
                            "duration": duration,
                            "webpage_url": canonical_url,
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
            params={"url": canonical_url},
            timeout=10,
            headers={"User-Agent": "WaveSnap/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        title = data.get("title", "Spotify Track")
        thumb = data.get("thumbnail_url", "")
        author = data.get("author_name") or data.get("provider_name") or "Spotify"
        tracks = [{"title": title, "artist": author}] if not is_playlist else []
        return {
            "title": title,
            "thumbnail": thumb,
            "channel": f"{author} · Spotify" if author != "Spotify" else "Spotify",
            "is_playlist": is_playlist,
            "count": 1 if not is_playlist else None,
            "duration": None,
            "webpage_url": canonical_url,
            "source": "spotify",
            "spotify_type": stype,
            "tracks": tracks
        }
    except Exception as exc:
        raise Exception(f"Could not fetch Spotify info: {exc}")


# ── JioSaavn Search Helper (Direct 320kbps CD Audio, Zero Bot/DRM Check) ─────

def find_jiosaavn_url(title: str, artist: str = "") -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    # 1. Clean brackets and markdown/quote symbols
    t_clean = re.sub(r'\(.*?\)|\[.*?\]', '', title)
    t_clean = re.sub(r'[\"_~*]', ' ', t_clean)
    # 2. Extract core title by cutting off " - From ...", " - OST", " - Remastered", " - Theme", etc.
    t_core = re.split(r'\s+[-–—]\s+(?:from|ost|original|remaster|feat|bonus|theme|soundtrack)', t_clean, flags=re.IGNORECASE)[0].strip()
    t_core_pure = t_core.split("-")[0].strip()

    clean_artist = artist.split(",")[0].split("&")[0].split("feat.")[0].strip() if artist else ""

    queries = []
    if t_core_pure and clean_artist:
        queries.append(f"{t_core_pure} {clean_artist}")
    if t_core and clean_artist and f"{t_core} {clean_artist}" not in queries:
        queries.append(f"{t_core} {clean_artist}")
    if t_core_pure and t_core_pure not in queries:
        queries.append(t_core_pure)
    if clean_artist and f"{t_clean.strip()} {clean_artist}" not in queries:
        queries.append(f"{t_clean.strip()} {clean_artist}")
    if t_clean.strip() and t_clean.strip() not in queries:
        queries.append(t_clean.strip())

    for q in queries:
        try:
            r = http_requests.get(
                "https://www.jiosaavn.com/api.php",
                params={
                    "__call": "search.getResults",
                    "_format": "json",
                    "_marker": "0",
                    "api_version": "4",
                    "ctx": "web6dot0",
                    "n": "3",
                    "p": "1",
                    "q": q
                },
                headers=headers,
                timeout=5
            )
            if r.status_code == 200:
                data = r.json()
                results = data.get("results", [])
                if results and results[0].get("perma_url"):
                    return results[0]["perma_url"]
        except Exception:
            continue
    return None


# ── Playlist ZIP Packaging Helper ─────────────────────────────────────────────

def _finalize_playlist_zip(job_id: str, out_dir: Path, title: str = "") -> dict | None:
    """Pre-packages downloaded audio files and artwork into a ZIP archive for instant 1-click delivery."""
    q = _job_queues.get(job_id)
    folder_name = out_dir.name
    clean_title = sanitize_folder(title or folder_name or "Playlist")
    zip_filename = f"{clean_title}.zip"
    zip_path = BASE_DIR / f"{sanitize_folder(folder_name)}.zip"

    valid_audio_exts = {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"}
    valid_exts = valid_audio_exts | {".jpg", ".jpeg", ".png"}

    if not out_dir.exists():
        return None

    audio_files = [f for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() in valid_audio_exts and f.stat().st_size > 10000]
    if not audio_files:
        return None

    if q:
        q.put({
            "type": "playlist_progress",
            "percent": 96,
            "speed": "",
            "done": len(audio_files),
            "total": len(audio_files),
            "filename": f"Packaging {len(audio_files)} songs into {zip_filename}…",
            "status_text": f"Packaging {len(audio_files)} songs into {zip_filename} (96%)…"
        })

    # ZIP_STORED is instantaneous (<0.1s for 200MB) since audio files are already compressed MP3s
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for f in out_dir.iterdir():
            if f.is_file() and f.suffix.lower() in valid_exts and f.stat().st_size > 500:
                zf.write(f, arcname=f.name)

    zip_size = zip_path.stat().st_size
    zip_mb = round(zip_size / (1024 * 1024), 1)
    data_text = f"{zip_mb:.1f} MB / {zip_mb:.1f} MB"
    zip_url = f"/api/download-zip?folder={quote(folder_name)}&title={quote(clean_title)}"

    if q:
        q.put({
            "type": "playlist_progress",
            "percent": 100,
            "speed": "",
            "done": len(audio_files),
            "total": len(audio_files),
            "done_mb": zip_mb,
            "total_mb": zip_mb,
            "data_text": data_text,
            "filename": zip_filename,
            "status_text": f"🎉 All {len(audio_files)} songs packaged into {zip_filename}! Sending to your device…"
        })

    return {
        "zip_url": zip_url,
        "zip_filename": zip_filename,
        "zip_size": zip_size,
        "done_mb": zip_mb,
        "total_mb": zip_mb,
        "data_text": data_text,
        "count": len(audio_files),
        "folder": folder_name,
        "title": clean_title,
    }


# ── Batch Tracklist Downloader (Spotify, Apple Music, etc.) ───────────────────

def _download_tracklist_batch(job_id: str, out_dir: Path, title: str = "", tracks: list = None, cover_url: str = "", codec: str = "mp3", quality: str = "320", default_source: str = "Audio"):
    q = _job_queues[job_id]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        has_ffmpeg = bool(shutil.which("ffmpeg"))

        # Save cover art if provided
        cover_data = None
        if cover_url:
            cover_path = out_dir / "cover.jpg"
            if not cover_path.exists():
                try:
                    c_resp = http_requests.get(cover_url, timeout=10)
                    if c_resp.status_code == 200:
                        cover_path.write_bytes(c_resp.content)
                        cover_data = c_resp.content
                except Exception as e:
                    print(f"Error saving cover.jpg: {e}")
            else:
                try:
                    cover_data = cover_path.read_bytes()
                except Exception:
                    pass

        if not tracks:
            q.put({"type": "error", "message": f"No tracks found to download for this {default_source} link. Please verify the link is public."})
            return

        total_tracks = len(tracks)
        completed_lock = threading.Lock()
        completed_count = 0
        successful_count = 0
        est_total_mb = round(total_tracks * 8.5, 1)

        q.put({
            "type": "playlist_progress",
            "percent": 0,
            "speed": "",
            "done": 0,
            "total": total_tracks,
            "done_mb": 0.0,
            "total_mb": est_total_mb,
            "data_text": f"0.0 MB / ~{est_total_mb:.1f} MB",
            "filename": f"Starting {default_source} download for {total_tracks} tracks into ZIP…",
            "status_text": f"Packaging 0 of {total_tracks} songs into ZIP (0%)…"
        })

        def download_single_track(idx: int, tr: dict):
            if _job_cancelled.get(job_id):
                return
            nonlocal completed_count, successful_count

            t_title = tr.get("title", "").strip()
            t_artist = tr.get("artist", "").strip()
            sanitized_artist = sanitize_folder(t_artist)
            sanitized_title = sanitize_folder(t_title or f"Track_{idx}")
            base_name = f"{sanitized_artist} - {sanitized_title}" if sanitized_artist else sanitized_title

            success = False
            try:
                if not t_title:
                    raise ValueError("Empty track title")

                target_file = out_dir / f"{base_name}.{codec}"
                if target_file.exists() and target_file.stat().st_size > 50000:
                    success = True
                else:
                    outtmpl = str(out_dir / f"{base_name}.%(ext)s")

                    # Multi-tier resilient search & download pipeline:
                    # 1. JioSaavn (direct 320kbps CD audio, 0 bot check, 0 DRM, instant CDN stream)
                    # 2. YouTube Music / YouTube with android & tv_embedded clients
                    # 3. SoundCloud
                    search_targets = []
                    jio_url = find_jiosaavn_url(t_title, t_artist)
                    if jio_url:
                        search_targets.append(("jiosaavn", jio_url))

                    # Clean query strings for fallback searches
                    clean_t = re.sub(r'\(.*?\)|\[.*?\]', '', t_title)
                    clean_t = re.split(r'\s+[-–—]\s+(?:from|ost|original|remaster|feat|bonus|theme|soundtrack)', clean_t, flags=re.IGNORECASE)[0].strip()
                    clean_a = t_artist.split(",")[0].split("&")[0].strip() if t_artist else ""

                    search_targets.append(("youtube_music", f"ytsearch1:{clean_t} {clean_a} audio"))
                    search_targets.append(("youtube", f"ytsearch1:{clean_t} {clean_a}"))
                    search_targets.append(("soundcloud", f"scsearch1:{clean_t} {clean_a}"))

                    # Slight thread staggering to prevent cloud bursting
                    time.sleep(min(idx * 0.12, 1.0))

                    ffmpeg_loc = FFMPEG_DIR or (str(Path(shutil.which("ffmpeg")).parent) if shutil.which("ffmpeg") else None)

                    for provider, target in search_targets:
                        if _job_cancelled.get(job_id) or success:
                            break

                        ydl_opts = {
                            "format": "bestaudio/best",
                            "outtmpl": outtmpl,
                            "quiet": True,
                            "no_warnings": True,
                            "ignoreerrors": True,
                            "retries": 2,
                            "socket_timeout": 15,
                            "geo_bypass": True,
                        }
                        if ffmpeg_loc:
                            ydl_opts["ffmpeg_location"] = ffmpeg_loc
                        if COOKIE_FILE:
                            ydl_opts["cookiefile"] = COOKIE_FILE

                        if "youtube" in provider:
                            ydl_opts["extractor_args"] = {
                                "youtube": {
                                    "player_client": ["android", "ios", "visionos"],
                                    "player_skip": ["web", "mweb", "tv"]
                                }
                            }

                        if has_ffmpeg:
                            ydl_opts["postprocessors"] = [
                                {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality, "nopostoverwrites": False},
                                {"key": "FFmpegMetadata", "add_metadata": True},
                            ]

                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                ydl.download([target])
                        except Exception as exc:
                            print(f"[{provider}] Download note on '{base_name}': {exc}")

                        # Check if audio file was saved on disk (even if post-processing had warnings)
                        actual_file = target_file
                        if not actual_file.exists() and out_dir.exists():
                            for f in out_dir.iterdir():
                                if f.is_file() and (f.stem == base_name or base_name in f.stem) and f.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"} and f.stat().st_size > 25000:
                                    actual_file = f
                                    break

                        if actual_file.exists() and actual_file.stat().st_size > 25000:
                            success = True
                            if not _job_cancelled.get(job_id):
                                try:
                                    _tag_audio(actual_file, title=t_title, artist=t_artist, album=title, cover_url=cover_url, cover_data=cover_data)
                                except Exception:
                                    pass
                            break

                if not success:
                    print(f"[SKIP] Song '{base_name}' could not be downloaded from any source. Skipping to next song.")

            except Exception as outer_exc:
                print(f"[SKIP] Exception on track '{base_name}': {outer_exc}. Skipping.")
                success = False

            finally:
                # GUARANTEED to execute for every single track:
                with completed_lock:
                    completed_count += 1
                    if success:
                        successful_count += 1

                    cur_bytes = sum(f.stat().st_size for f in out_dir.iterdir() if f.is_file()) if out_dir.exists() else 0
                    cur_mb = round(cur_bytes / (1024 * 1024), 1)
                    if successful_count > 0:
                        avg_track_mb = cur_mb / successful_count
                        est_mb = round(avg_track_mb * total_tracks, 1)
                    else:
                        est_mb = round(total_tracks * 8.5, 1)

                    data_text = f"{cur_mb:.1f} MB / ~{est_mb:.1f} MB"
                    # Overall progress climbs steadily from 0% to 92%
                    overall_pct = min(92.0, round((completed_count / total_tracks) * 92.0, 1))
                    status_msg = f"Packaging song {completed_count} of {total_tracks} into ZIP ({overall_pct}%)"
                    fname_display = f"{base_name}.{codec}" if success else f"⚠️ Skipped: {base_name}"
                    q.put({
                        "type": "playlist_progress",
                        "percent": overall_pct,
                        "speed": "",
                        "done": completed_count,
                        "total": total_tracks,
                        "done_mb": cur_mb,
                        "total_mb": est_mb,
                        "data_text": data_text,
                        "filename": fname_display,
                        "status_text": status_msg
                    })

        workers = min(3, max(1, total_tracks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = []
            for i, tr in enumerate(tracks, 1):
                if _job_cancelled.get(job_id):
                    break
                futures.append(executor.submit(download_single_track, i, tr))
            for fut in concurrent.futures.as_completed(futures):
                if _job_cancelled.get(job_id):
                    for f in futures:
                        f.cancel()
                    break

        if _job_cancelled.get(job_id):
            shutil.rmtree(out_dir, ignore_errors=True)
            q.put({"type": "cancelled", "message": "Download stopped by user"})
            return

        # Package all downloaded tracks into a pre-built ZIP archive
        zip_info = _finalize_playlist_zip(job_id, out_dir, title=title)
        if not zip_info:
            q.put({
                "type": "error",
                "message": "None of the tracks in this playlist could be downloaded. They may be unavailable or private."
            })
            return

        is_pl = total_tracks > 1
        done_payload = {"type": "done", "out_dir": str(out_dir), "is_playlist": is_pl}
        done_payload.update(zip_info)

        if not is_pl:
            valid_audio_exts = {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"}
            audio_files = [f for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() in valid_audio_exts and f.stat().st_size > 10000]
            if audio_files:
                f0 = audio_files[0]
                rel_path = str(f0.relative_to(BASE_DIR)).replace("\\", "/")
                done_payload["file_url"] = f"/api/download-file?path={quote(rel_path)}&name={quote(f0.name)}"
                done_payload["file_name"] = f0.name
                done_payload["file_size"] = f0.stat().st_size

        q.put(done_payload)

    except Exception as exc:
        if not _job_cancelled.get(job_id):
            q.put({"type": "error", "message": str(exc)})
    finally:
        if _job_cancelled.get(job_id):
            shutil.rmtree(out_dir, ignore_errors=True)


# ── Specific Software Download Execution Handlers ─────────────────────────────

def _download_spotify(job_id: str, url: str, out_dir: Path, title: str = "", tracks: list = None, cover_url: str = "", codec: str = "mp3", quality: str = "320"):
    """Dedicated Spotify execution handler: downloads tracklist with official Spotify metadata and artwork."""
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
    _download_tracklist_batch(job_id, out_dir, title=title, tracks=tracks, cover_url=cover_url, codec=codec, quality=quality, default_source="Spotify")


def _download_applemusic(job_id: str, url: str, out_dir: Path, title: str = "", tracks: list = None, cover_url: str = "", codec: str = "mp3", quality: str = "320"):
    """Dedicated Apple Music execution handler: resolves Apple catalog metadata and downloads high-fidelity audio."""
    if not tracks:
        info = _applemusic_info(url)
        tracks = info.get("tracks", [])
        if not cover_url:
            cover_url = info.get("thumbnail", "")
        if not title:
            title = info.get("title", "")
    _download_tracklist_batch(job_id, out_dir, title=title, tracks=tracks, cover_url=cover_url, codec=codec, quality=quality, default_source="Apple Music")


def _build_done_payload(out_dir: Path, is_playlist: bool, zip_info: dict | None, valid_audio: list) -> dict:
    is_pl = is_playlist or len(valid_audio) > 1
    done_payload = {"type": "done", "out_dir": str(out_dir), "is_playlist": is_pl}
    if zip_info:
        done_payload.update(zip_info)
    if valid_audio:
        f0 = valid_audio[0]
        rel_path = str(f0.relative_to(BASE_DIR)).replace("\\", "/")
        done_payload["file_url"] = f"/api/download-file?path={quote(rel_path)}&name={quote(f0.name)}"
        done_payload["file_name"] = f0.name
        done_payload["file_size"] = f0.stat().st_size
    return done_payload


def _download_youtube(job_id: str, url: str, out_dir: Path, title: str = "", is_playlist: bool = False, cover_url: str = "", codec: str = "mp3", quality: str = "320"):
    """Dedicated YouTube & YouTube Music execution handler: uses optimized stream extraction with live progress."""
    q = _job_queues[job_id]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ydl_opts = _ydl_opts(job_id, out_dir, codec=codec, quality=quality)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as yt_err:
            err_str = str(yt_err).lower()
            if ("bot" in err_str or "sign in" in err_str or "403" in err_str) and title:
                print(f"YouTube cloud bot check on '{title}', activating JioSaavn/SoundCloud fallback...")
                jio_url = find_jiosaavn_url(title)
                fb_target = jio_url or f"scsearch1:{title}"
                ffmpeg_loc = FFMPEG_DIR or (str(Path(shutil.which("ffmpeg")).parent) if shutil.which("ffmpeg") else None)
                fb_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                    "ignoreerrors": True,
                    "progress_hooks": [_make_ytdlp_hook(job_id, out_dir)],
                }
                if ffmpeg_loc:
                    fb_opts["ffmpeg_location"] = ffmpeg_loc
                if COOKIE_FILE:
                    fb_opts["cookiefile"] = COOKIE_FILE
                if bool(FFMPEG_PATH or shutil.which("ffmpeg")):
                    fb_opts["postprocessors"] = [
                        {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality, "nopostoverwrites": False},
                        {"key": "FFmpegMetadata", "add_metadata": True},
                    ]
                with yt_dlp.YoutubeDL(fb_opts) as ydl_fb:
                    ydl_fb.download([fb_target])
            else:
                raise yt_err
    except Exception as exc:
        if not _job_cancelled.get(job_id):
            q.put({"type": "error", "message": str(exc)})
    finally:
        if _job_cancelled.get(job_id):
            shutil.rmtree(out_dir, ignore_errors=True)
            q.put({"type": "cancelled", "message": "Download stopped by user"})
        else:
            zip_info = None
            valid_audio = []
            if out_dir.exists():
                valid_audio = [f for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"} and f.stat().st_size > 10000]
                if is_playlist or len(valid_audio) > 1:
                    zip_info = _finalize_playlist_zip(job_id, out_dir, title=title)
            q.put(_build_done_payload(out_dir, is_playlist, zip_info, valid_audio))


def _download_soundcloud(job_id: str, url: str, out_dir: Path, title: str = "", is_playlist: bool = False, cover_url: str = "", codec: str = "mp3", quality: str = "320"):
    """Dedicated SoundCloud execution handler: extracts high-bitrate audio and original artwork."""
    q = _job_queues[job_id]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ydl_opts = _ydl_opts(job_id, out_dir, codec=codec, quality=quality)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        if not _job_cancelled.get(job_id):
            q.put({"type": "error", "message": str(exc)})
    finally:
        if _job_cancelled.get(job_id):
            shutil.rmtree(out_dir, ignore_errors=True)
            q.put({"type": "cancelled", "message": "Download stopped by user"})
        else:
            zip_info = None
            valid_audio = []
            if out_dir.exists():
                valid_audio = [f for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"} and f.stat().st_size > 10000]
                if is_playlist or len(valid_audio) > 1:
                    zip_info = _finalize_playlist_zip(job_id, out_dir, title=title)
            q.put(_build_done_payload(out_dir, is_playlist, zip_info, valid_audio))


def _download_generic(job_id: str, url: str, out_dir: Path, title: str = "", is_playlist: bool = False, cover_url: str = "", codec: str = "mp3", quality: str = "320"):
    """Universal downloader for Bandcamp, TikTok, Instagram, Vimeo, and web media."""
    q = _job_queues[job_id]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ydl_opts = _ydl_opts(job_id, out_dir, codec=codec, quality=quality)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        if not _job_cancelled.get(job_id):
            q.put({"type": "error", "message": str(exc)})
    finally:
        if _job_cancelled.get(job_id):
            shutil.rmtree(out_dir, ignore_errors=True)
            q.put({"type": "cancelled", "message": "Download stopped by user"})
        else:
            zip_info = None
            valid_audio = []
            if out_dir.exists():
                valid_audio = [f for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"} and f.stat().st_size > 10000]
                if is_playlist or len(valid_audio) > 1:
                    zip_info = _finalize_playlist_zip(job_id, out_dir, title=title)
            q.put(_build_done_payload(out_dir, is_playlist, zip_info, valid_audio))


# ── Specific Software Info Extractors ─────────────────────────────────────────

def _applemusic_info(url: str) -> dict:
    url = resolve_url(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    title = ""
    artist = ""
    thumb = ""
    is_playlist = "/playlist/" in url or "/album/" in url
    tracks = []

    try:
        resp = http_requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                title = og_title["content"].strip()
            og_image = soup.find("meta", property="og:image")
            if og_image and og_image.get("content"):
                thumb = re.sub(r'/\d+x\d+bb\.', '/600x600bb.', og_image["content"].strip())

            for s in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(s.string)
                    if isinstance(data, list):
                        data = data[0]
                    if isinstance(data, dict):
                        if not title:
                            title = data.get("name", "")
                        if not thumb and data.get("image"):
                            thumb = data.get("image")
                        items = data.get("tracks") or data.get("itemListElement") or []
                        for item in items:
                            t_item = item.get("item", item)
                            t_name = t_item.get("name")
                            t_artist = ""
                            if t_item.get("byArtist"):
                                by_a = t_item["byArtist"]
                                t_artist = by_a.get("name", "") if isinstance(by_a, dict) else str(by_a)
                            if t_name:
                                tracks.append({"title": t_name, "artist": t_artist or "Apple Music"})
                except Exception:
                    pass

            if not title and soup.title:
                title = re.sub(r'(\s*on Apple Music|\s*-\s*Apple Music).*$', '', soup.title.string).strip()
    except Exception as exc:
        print(f"Apple Music parser error: {exc}")

    if not title:
        title = "Apple Music Audio"

    count = len(tracks) if tracks else (1 if not is_playlist else None)
    return {
        "title": title,
        "thumbnail": thumb,
        "channel": f"{artist or 'Apple Music'} • Apple Music",
        "is_playlist": is_playlist,
        "count": count,
        "duration": None,
        "webpage_url": url,
        "source": "applemusic",
        "tracks": tracks
    }


def _soundcloud_info(url: str) -> dict:
    url = resolve_url(url)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    is_playlist = info.get("_type") == "playlist" or "/sets/" in url
    entries = info.get("entries", [])
    thumb = info.get("thumbnail") or (entries[0].get("thumbnail") if entries else "")
    if thumb:
        thumb = thumb.replace("-large.", "-t500x500.")
    return {
        "title": info.get("title") or "SoundCloud Audio",
        "thumbnail": thumb,
        "channel": f"{info.get('uploader') or 'SoundCloud'} • SoundCloud",
        "is_playlist": is_playlist,
        "count": len(entries) if is_playlist else 1,
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or url,
        "source": "soundcloud",
        "tracks": []
    }


def _youtube_info(url: str) -> dict:
    url = resolve_url(url)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "extract_flat": "in_playlist",
        "geo_bypass": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "tv_embedded"],
                "player_skip": ["web", "mweb"]
            }
        }
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    is_playlist = info.get("_type") == "playlist" or "list=" in url
    entries = info.get("entries", [])
    is_ytmusic = "music.youtube.com" in url
    platform_name = "YouTube Music" if is_ytmusic else "YouTube"
    return {
        "title": info.get("title") or f"{platform_name} Audio",
        "thumbnail": info.get("thumbnail") or (entries[0].get("thumbnail") if entries else ""),
        "channel": f"{info.get('uploader') or info.get('channel') or platform_name} • {platform_name}",
        "is_playlist": is_playlist,
        "count": len(entries) if is_playlist else 1,
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or url,
        "source": "ytmusic" if is_ytmusic else "youtube",
        "tracks": []
    }


def _generic_info(url: str) -> dict:
    url = resolve_url(url)
    is_http = url.startswith("http://") or url.startswith("https://")
    fetch_target = url if is_http else f"ytsearch1:{url}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(fetch_target, download=False)
    if info.get("_type") == "playlist" and "entries" in info and not is_http:
        entries = info.get("entries", [])
        if entries:
            info = entries[0]
    is_playlist = info.get("_type") == "playlist"
    entries = info.get("entries", [])
    return {
        "title": info.get("title") or "Audio",
        "thumbnail": info.get("thumbnail") or (entries[0].get("thumbnail") if entries else ""),
        "channel": info.get("uploader") or info.get("channel") or ("Search Result" if not is_http else "Web Media"),
        "is_playlist": is_playlist,
        "count": len(entries) if is_playlist else 1,
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or url,
        "source": "search" if not is_http else "generic",
        "tracks": []
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze-link", methods=["POST", "GET"])
def analyze_link_route():
    data = {}
    if request.is_json:
        data = request.get_json(silent=True) or {}
    if not data:
        data = request.get_json(force=True, silent=True) or {}
    if not data:
        data = request.form.to_dict() or request.args.to_dict() or {}

    raw_url = (data.get("url") or request.args.get("url") or "").strip()
    if not raw_url:
        return jsonify({"platform": None})
    return jsonify({"platform": analyze_software_link(raw_url)})


@app.route("/api/info", methods=["POST", "GET"])
def get_info():
    data = {}
    if request.is_json:
        data = request.get_json(silent=True) or {}
    if not data:
        data = request.get_json(force=True, silent=True) or {}
    if not data:
        data = request.form.to_dict() or request.args.to_dict() or {}

    raw_url = (data.get("url") or request.args.get("url") or "").strip()
    if not raw_url:
        return jsonify({"error": "No link or search term provided"}), 400

    url = resolve_url(raw_url)
    platform = analyze_software_link(url)
    pid = platform["platform_id"]

    try:
        if pid == "spotify":
            info = _spotify_info(url)
        elif pid == "applemusic":
            info = _applemusic_info(url)
        elif pid == "soundcloud":
            info = _soundcloud_info(url)
        elif pid in ("youtube", "ytmusic"):
            info = _youtube_info(url)
        else:
            info = _generic_info(url)

        info["platform"] = platform
        return jsonify(info)
    except Exception as exc:
        err_msg = str(exc)
        print(f"Error fetching info for '{url}' via {platform.get('engine', 'engine')}: {err_msg}")
        return jsonify({"error": err_msg}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data        = request.get_json(force=True, silent=True) or {}
    raw_url     = (data.get("url") or "").strip()
    url         = resolve_url(raw_url) if raw_url else ""
    info_title  = (data.get("title") or "").strip()
    is_playlist = bool(data.get("is_playlist"))
    tracks      = data.get("tracks", [])
    cover_url   = (data.get("thumbnail") or data.get("cover_url") or "").strip()
    quality_param = data.get("quality", "mp3_320")
    if "_" in quality_param:
        codec, quality = quality_param.split("_", 1)
    else:
        codec, quality = "mp3", "320"

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    platform = analyze_software_link(url)
    pid = platform["platform_id"]
    engine_name = platform["engine"]

    job_id = str(uuid.uuid4())
    folder_name = sanitize_folder(info_title) if info_title else job_id
    out_dir = BASE_DIR / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    q: queue.Queue = queue.Queue()
    with _job_lock:
        _job_queues[job_id] = q
        _job_cancelled[job_id] = False

    def run():
        try:
            if _job_cancelled.get(job_id):
                return

            # Announce engine initialization for this specific software
            q.put({
                "type": "progress",
                "percent": 0,
                "speed": "",
                "filename": f"Initialized {engine_name} for {platform['name']} ({platform['type_label']})…"
            })

            # Save cover image if present
            if cover_url:
                cover_path = out_dir / "cover.jpg"
                if not cover_path.exists():
                    try:
                        c_resp = http_requests.get(cover_url, timeout=10)
                        if c_resp.status_code == 200:
                            cover_path.write_bytes(c_resp.content)
                    except Exception as e:
                        print(f"Error saving cover.jpg: {e}")

            # Specific software execution dispatch
            if pid == "spotify":
                _download_spotify(job_id, url, out_dir, title=info_title, tracks=tracks, cover_url=cover_url, codec=codec, quality=quality)
            elif pid == "applemusic":
                _download_applemusic(job_id, url, out_dir, title=info_title, tracks=tracks, cover_url=cover_url, codec=codec, quality=quality)
            elif pid in ("youtube", "ytmusic"):
                _download_youtube(job_id, url, out_dir, title=info_title, is_playlist=is_playlist, cover_url=cover_url, codec=codec, quality=quality)
            elif pid == "soundcloud":
                _download_soundcloud(job_id, url, out_dir, title=info_title, is_playlist=is_playlist, cover_url=cover_url, codec=codec, quality=quality)
            else:
                _download_generic(job_id, url, out_dir, title=info_title, is_playlist=is_playlist, cover_url=cover_url, codec=codec, quality=quality)

        except Exception as exc:
            if _job_cancelled.get(job_id):
                shutil.rmtree(out_dir, ignore_errors=True)
                q.put({"type": "cancelled", "message": "Download stopped by user"})
            else:
                print(f"Download thread exception: {exc}")
                q.put({"type": "error", "message": str(exc)})
                q.put({"type": "done", "out_dir": str(out_dir)})

    threading.Thread(target=run, daemon=True).start()

    return jsonify({
        "job_id": job_id,
        "out_dir": str(out_dir),
        "folder": folder_name,
        "platform": platform,
    })


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id: str):
    with _job_lock:
        _job_cancelled[job_id] = True
    q = _job_queues.get(job_id)
    if q:
        q.put({"type": "cancelled", "message": "Download stopped by user"})
    return jsonify({"status": "cancelled", "job_id": job_id})


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
                if msg.get("type") in ("done", "cancelled"):
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
        return jsonify({"files": [], "folder": "", "cover": "", "total_size": 0})

    audio_exts = {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac"}
    files = []
    is_folder = out_dir.resolve() != BASE_DIR.resolve()
    folder_name = out_dir.name if is_folder else ""

    cover_rel = ""
    for img_name in ("cover.jpg", "cover.png", "cover.jpeg"):
        img_path = out_dir / img_name
        if img_path.exists() and img_path.stat().st_size > 500:
            cover_rel = str(img_path.relative_to(BASE_DIR)).replace("\\", "/")
            break

    for path in sorted(out_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in audio_exts:
            files.append({
                "name":   path.name,
                "size":   path.stat().st_size,
                "rel":    str(path.relative_to(BASE_DIR)).replace("\\", "/"),
                "folder": folder_name,
            })
    return jsonify({
        "files": files,
        "folder": folder_name,
        "cover": cover_rel,
        "total_size": sum(f["size"] for f in files)
    })


@app.route("/api/download-file")
def download_file():
    rel = request.args.get("path", "").strip()
    if not rel or ".." in rel:
        return "Invalid path", 400
    full = (BASE_DIR / rel).resolve()
    try:
        full.relative_to(BASE_DIR.resolve())
    except ValueError:
        return "Access denied", 403

    if not full.exists() or not full.is_file():
        return "File not found", 404

    custom_name = request.args.get("name", "").strip() or full.name
    response = send_from_directory(
        str(full.parent),
        full.name,
        as_attachment=True,
        download_name=custom_name
    )
    # Ensure all mobile and desktop browsers treat as direct file download
    response.headers["Content-Description"] = "File Transfer"
    response.headers["Content-Transfer-Encoding"] = "binary"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/download-zip")
def download_zip():
    folder = request.args.get("folder", "").strip()
    title  = request.args.get("title", folder).strip()
    if not folder or ".." in folder:
        return "Invalid folder", 400

    target_dir = (BASE_DIR / folder).resolve()
    try:
        target_dir.relative_to(BASE_DIR.resolve())
    except ValueError:
        return "Access denied", 403

    clean_title = sanitize_folder(title or folder)
    zip_filename = f"{clean_title}.zip"
    zip_path = BASE_DIR / f"{sanitize_folder(folder)}.zip"

    # If pre-built zip doesn't exist, build it now from directory
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        if not target_dir.exists() or not target_dir.is_dir():
            return "Folder not found", 404

        valid_exts = {".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".aac", ".jpg", ".jpeg", ".png"}
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for f in target_dir.iterdir():
                if f.is_file() and f.suffix.lower() in valid_exts and f.stat().st_size > 500:
                    zf.write(f, arcname=f.name)

    if not zip_path.exists() or zip_path.stat().st_size == 0:
        return "Failed to prepare ZIP archive", 500

    file_size = zip_path.stat().st_size
    response = send_from_directory(
        str(BASE_DIR.resolve()),
        zip_path.name,
        as_attachment=True,
        download_name=zip_filename
    )
    # Ensure all browsers (desktop & mobile) treat as a direct binary attachment with known length
    response.headers["Content-Length"] = str(file_size)
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Description"] = "File Transfer"
    response.headers["Content-Transfer-Encoding"] = "binary"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/cleanup-job", methods=["POST"])
def cleanup_job():
    data = request.get_json(force=True, silent=True) or {}
    folder = data.get("folder", "").strip()
    if folder and ".." not in folder:
        target_dir = (BASE_DIR / folder).resolve()
        try:
            target_dir.relative_to(BASE_DIR.resolve())
            if target_dir != BASE_DIR.resolve() and target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
        except Exception:
            pass
    return jsonify({"status": "cleaned"})


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = not IS_CLOUD
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
