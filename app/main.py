import os
import io
import re
import time
import zipfile
import hashlib
import asyncio
from pathlib import Path
import shutil

import mutagen
import colorsys
from PIL import Image
from collections import Counter
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .database import *

MEDIA_DIR = os.environ.get("MEDIA_DIR", "/storage/music")
COVERS_DIR = os.environ.get("COVERS_DIR", "/storage/covers")
BASE_URL = os.environ.get("BASE_URL", "https://mixtape.janhellion.com")

os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(COVERS_DIR, exist_ok=True)

app = FastAPI(title="Mixtape Share", version="1.0")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# Register custom template filters
from .config import timestamp_to_date, format_duration
templates.env.filters["timestamp_to_date"] = timestamp_to_date
templates.env.filters["format_duration"] = format_duration

init_db()


# ─── Helpers ──────────────────────────────────────────────────

def sanitize_filename(name):
    name = re.sub(r"[^\w\s-]", "", name).strip().lower()
    name = re.sub(r"[-\s]+", "_", name)
    return name or "mixtape"


def scan_audio(dir_path):
    """Scan a directory for audio files and return metadata."""
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE
    from mutagen.aiff import AIFF
    from mutagen.mp4 import MP4
    from mutagen.easyid3 import EasyID3

    exts = {".mp3", ".flac", ".ogg", ".wav", ".aiff", ".aif", ".m4a"}
    tracks = []
    for f in sorted(Path(dir_path).iterdir()):
        if f.suffix.lower() not in exts:
            continue
        try:
            mfile = mutagen.File(str(f))
            if mfile is None:
                continue

            duration = int(mfile.info.length) if mfile and mfile.info else 0
            title = ""
            artist = ""
            tags = getattr(mfile, "tags", None)

            if tags is not None:
                if isinstance(tags, dict):
                    title = tags.get("title", [""])[0] if tags.get("title") and tags["title"][0] else ""
                    artist = tags.get("artist", [""])[0] if tags.get("artist") and tags["artist"][0] else ""
                elif hasattr(tags, "get"):
                    title = tags.get("title", "")
                    artist = tags.get("artist", "")

            # Normalize: tags often return lists like ['Los Tres']
            if isinstance(title, list):
                title = str(title[0]) if title else ""
            if isinstance(artist, list):
                artist = str(artist[0]) if artist else ""
            title = str(title) if title else ""
            artist = str(artist) if artist else ""

            if not title:
                title = f.stem
            if not artist:
                artist = "Unknown"

            tracks.append({
                "filename": f.name,
                "title": title,
                "artist": artist,
                "duration": duration,
            })
        except Exception as e:
            tracks.append({
                "filename": f.name,
                "title": f.stem,
                "artist": "Unknown",
                "duration": 0,
            })
    return tracks


def extract_dominant_color(image_path, fallback="#15c7ff"):
    """Extract dominant color from an image. Returns hex string."""
    try:
        img = Image.open(image_path)
        img = img.convert("RGB")
        img = img.resize((150, 150))
        pixels = list(img.getdata())
        # Weight by saturation to prefer vibrant colors
        weighted = []
        for r, g, b in pixels:
            h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            weight = s * 0.7 + 0.3
            weighted.append((r, g, b, weight))
        # Quantize colors
        quantized = [(round(r / 32) * 32, round(g / 32) * 32, round(b / 32) * 32) for r, g, b, _ in weighted]
        counter = Counter()
        for (r, g, b), (_, _, _, w) in zip(quantized, weighted):
            counter[(r, g, b)] += w
        # Get top color, preferring non-greyscale
        for (r, g, b), _ in counter.most_common(10):
            if abs(r - g) > 20 or abs(g - b) > 20:
                return f"#{r:02x}{g:02x}{b:02x}"
        best = counter.most_common(1)[0][0]
        return f"#{best[0]:02x}{best[1]:02x}{best[2]:02x}"
    except Exception:
        return fallback


def create_token(name):
    raw = f"{name}{time.time_ns()}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


# ─── Routes ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    shares = get_all_shares()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "shares": shares,
        "base_url": BASE_URL,
    })


@app.post("/api/upload")
async def upload_mixtape(
    files: list[UploadFile] = File(...),
    name: str = Form(...),
    passcode: str = Form(""),
    cover: UploadFile = File(None),
):
    """Create a new mixtape from uploaded files. Accepts audio files, zip, or cover."""
    safe_name = sanitize_filename(name)
    share_id = create_token(name)
    folder = os.path.join(MEDIA_DIR, f"{share_id}_{safe_name}")
    os.makedirs(folder, exist_ok=True)
    cover_path = ""

    # Process uploaded files
    for f in files:
        content = await f.read()
        if f.filename and f.filename.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                zf.extractall(folder)
        elif f.filename and f.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            ext = f.filename.rsplit(".", 1)[-1]
            cover_name = f"{share_id}_cover.{ext}"
            cover_full = os.path.join(COVERS_DIR, cover_name)
            with open(cover_full, "wb") as cf:
                cf.write(content)
            cover_path = f"/api/cover/{cover_name}"
        else:
            filepath = os.path.join(folder, f.filename)
            with open(filepath, "wb") as af:
                af.write(content)

    # Handle explicit cover upload if no cover found in files
    if not cover_path and cover and cover.filename:
        content = await cover.read()
        ext = cover.filename.rsplit(".", 1)[-1].lower()
        if ext in ("png", "jpg", "jpeg", "gif", "webp"):
            cover_name = f"{share_id}_cover.{ext}"
            cover_full = os.path.join(COVERS_DIR, cover_name)
            with open(cover_full, "wb") as cf:
                cf.write(content)
            cover_path = f"/api/cover/{cover_name}"

    tracks = scan_audio(folder)
    if not tracks:
        shutil.rmtree(folder, ignore_errors=True)
        return JSONResponse({"error": "No audio files found"}, status_code=400)

    # Extract dominant color from cover for player theming
    accent_color = ""
    if cover_path:
        cover_full_path = os.path.join(COVERS_DIR, os.path.basename(cover_path))
        if os.path.isfile(cover_full_path):
            accent_color = extract_dominant_color(cover_full_path)

    create_share(share_id, name, folder, passcode, cover_path, accent_color)
    set_tracks(share_id, tracks)

    return JSONResponse({
        "id": share_id,
        "url": f"{BASE_URL}/share/{share_id}",
        "tracks": len(tracks),
    })


@app.get("/api/shares")
async def list_shares():
    shares = get_all_shares()
    result = []
    for s in shares:
        result.append({
            "id": s["id"],
            "name": s["name"],
            "created_at": s["created_at"],
            "expires_at": s["expires_at"],
            "download_count": s["download_count"],
            "track_count": s["track_count"],
            "cover_path": s.get("cover_path", ""),
            "accent_color": s.get("accent_color", ""),
            "url": f"{BASE_URL}/share/{s['id']}",
        })
    return JSONResponse(result)


@app.delete("/api/share/{share_id}")
async def remove_share(share_id: str):
    share = get_share(share_id)
    if not share:
        raise HTTPException(404)
    folder = share.get("path", "")
    if folder and os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
    delete_share(share_id)
    return JSONResponse({"ok": True})


@app.post("/api/share/{share_id}/tracks")
async def save_tracks(share_id: str, data: dict):
    set_tracks(share_id, data.get("tracks", []))
    return JSONResponse({"ok": True})


@app.post("/api/share/{share_id}/update")
async def update_share_route(share_id: str, data: dict):
    update_share(share_id, **data)
    return JSONResponse({"ok": True})


@app.get("/api/share/{share_id}/tracks")
async def get_tracks_api(share_id: str):
    share = get_share(share_id)
    if not share:
        raise HTTPException(404)
    tracks = get_tracks(share_id)
    return JSONResponse({
        "share": {
            "id": share["id"],
            "name": share["name"],
            "cover_path": share.get("cover_path", ""),
            "accent_color": share.get("accent_color", ""),
            "download_count": share["download_count"],
            "track_count": share["track_count"],
        },
        "tracks": tracks,
    })


@app.post("/api/share/{share_id}/verify")
async def verify_passcode(share_id: str, data: dict):
    share = get_share(share_id)
    if not share:
        raise HTTPException(404)
    passcode = data.get("passcode", "")
    if share.get("passcode") and share["passcode"] != passcode:
        return JSONResponse({"valid": False})
    return JSONResponse({"valid": True})


@app.get("/api/stream/{share_id}/{filename:path}")
async def stream_audio(share_id: str, filename: str, request: Request):
    share = get_share(share_id)
    if not share:
        raise HTTPException(404)
    filepath = os.path.join(share["path"], filename)
    if not os.path.isfile(filepath):
        raise HTTPException(404)

    stat = os.stat(filepath)
    file_size = stat.st_size
    mime_map = {
        "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg",
        "wav": "audio/wav", "aiff": "audio/aiff", "aif": "audio/aiff",
        "m4a": "audio/mp4",
    }
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = mime_map.get(ext, "application/octet-stream")

    range_header = request.headers.get("range", "")
    if range_header:
        start, end = 0, file_size - 1
        range_match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if range_match:
            start = int(range_match.group(1))
            if range_match.group(2):
                end = int(range_match.group(2))
        if start >= file_size:
            raise HTTPException(416)
        content_length = end - start + 1

        async def stream_range():
            with open(filepath, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
                    await asyncio.sleep(0)

        return StreamingResponse(
            stream_range(),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(content_length),
                "Accept-Ranges": "bytes",
            },
        )
    else:
        async def stream_full():
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
                    await asyncio.sleep(0)

        return StreamingResponse(
            stream_full(),
            media_type=content_type,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )


@app.get("/api/download/{share_id}")
async def download_zip(share_id: str):
    share = get_share(share_id)
    if not share:
        raise HTTPException(404)
    folder = share["path"]
    if not os.path.isdir(folder):
        raise HTTPException(404)
    tracks = get_tracks(share_id)
    increment_download(share_id)

    def zip_stream():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for t in tracks:
                fp = os.path.join(folder, t["filename"])
                if os.path.isfile(fp):
                    ext = t["filename"].rsplit(".", 1)[-1]
                    arcname = f"{t.get('title', t['filename'])}.{ext}"
                    zf.write(fp, arcname)
        buffer.seek(0)
        yield buffer.read()

    safe_name = re.sub(r"[^\w\s-]", "", share["name"]).strip().replace(" ", "_")
    return StreamingResponse(
        zip_stream(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


@app.get("/api/cover/{cover_name}")
async def serve_cover(cover_name: str):
    fp = os.path.join(COVERS_DIR, cover_name)
    if os.path.isfile(fp):
        return FileResponse(fp)
    raise HTTPException(404)


@app.post("/api/share/{share_id}/play/{track_id}")
async def log_play_event(share_id: str, track_id: int, request: Request):
    ip = request.client.host if request.client else ""
    log_play(track_id, ip)
    return JSONResponse({"ok": True})


@app.get("/share/{share_id}", response_class=HTMLResponse)
async def player(request: Request, share_id: str):
    share = get_share(share_id)
    if not share:
        return templates.TemplateResponse("player.html", {
            "request": request, "share": None, "tracks": [],
            "base_url": BASE_URL, "error": "Mixtape not found",
        })
    tracks = get_tracks(share_id)
    return templates.TemplateResponse("player.html", {
        "request": request, "share": share, "tracks": tracks,
        "base_url": BASE_URL, "error": None,
        "accent_color": share.get("accent_color", "") if share else "",
    })


@app.on_event("startup")
async def startup():
    for s in get_all_shares():
        if s.get("path") and not os.path.isdir(s["path"]):
            delete_share(s["id"])
            print(f"Cleaned up stale share: {s['id']} ({s['name']})")


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
