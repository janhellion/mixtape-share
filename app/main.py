import os
import io
import re
import time
import zipfile
import hashlib
import asyncio
import smtplib
import threading
from email.mime.text import MIMEText
from pathlib import Path
import shutil

import mutagen
import colorsys
from PIL import Image
from collections import Counter
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .database import *

MEDIA_DIR = os.environ.get("MEDIA_DIR", "/storage/music")
COVERS_DIR = os.environ.get("COVERS_DIR", "/storage/covers")
BASE_URL = os.environ.get("BASE_URL", "https://mixtape.janhellion.com")

# ─── SMTP Config ─────────────────────────────────────────────
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.ionos.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "hola@janhellion.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "CHANGE_ME")
SMTP_FROM = os.environ.get("SMTP_FROM", "hola@janhellion.com")

def send_email(to_email, subject, html_body):
    """Send an HTML email in a background thread."""
    def _send():
        try:
            msg = MIMEText(html_body, "html")
            msg["Subject"] = subject
            msg["From"] = SMTP_FROM
            msg["To"] = to_email
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
            print(f"Email sent to {to_email}")
        except Exception as e:
            print(f"Email failed to {to_email}: {e}")
    threading.Thread(target=_send, daemon=True).start()

os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(COVERS_DIR, exist_ok=True)

app = FastAPI(title="Mixtape", version="1.0")
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
            album = ""
            year = ""
            tags = getattr(mfile, "tags", None)

            if tags is not None:
                if isinstance(tags, dict):
                    title = tags.get("title", [""])[0] if tags.get("title") and tags["title"][0] else ""
                    artist = tags.get("artist", [""])[0] if tags.get("artist") and tags["artist"][0] else ""
                    album = tags.get("album", [""])[0] if tags.get("album") and tags["album"][0] else ""
                    year = tags.get("date", [""])[0] if tags.get("date") and tags["date"][0] else ""
                elif hasattr(tags, "get"):
                    title = tags.get("title", "")
                    artist = tags.get("artist", "")
                    album = tags.get("album", "")
                    year = tags.get("date", "")

            # Normalize: tags often return lists like ['Los Tres']
            for _field in ["title", "artist", "album", "year"]:
                _val = {"title": title, "artist": artist, "album": album, "year": year}[_field]
                if isinstance(_val, list):
                    _val = str(_val[0]) if _val else ""
                else:
                    _val = str(_val) if _val else ""
                if _field == "title": title = _val
                elif _field == "artist": artist = _val
                elif _field == "album": album = _val
                elif _field == "year": year = _val

            # Year might be a full date like "1999-01-01" — extract just the year
            if year and len(year) >= 4:
                year = year[:4]

            if not title:
                title = f.stem
            if not artist:
                artist = "Unknown"

            tracks.append({
                "filename": f.name,
                "title": title,
                "artist": artist,
                "album": album,
                "year": year,
                "duration": duration,
            })
        except Exception as e:
            tracks.append({
                "filename": f.name,
                "title": f.stem,
                "artist": "Unknown",
                "album": "",
                "year": "",
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
    token = request.cookies.get("session", "")
    user = get_user_by_session(token)
    if not user:
        return RedirectResponse(url="/login")
    shares = get_all_shares()
    is_admin = is_admin_user(user["id"])
    invites = get_invite_codes() if is_admin else []
    users_list = get_all_users() if is_admin else []
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "shares": shares,
        "base_url": BASE_URL,
        "user": user,
        "is_admin": is_admin,
        "invite_codes": invites,
        "users": users_list,
        "now": int(time.time()),
    })


@app.post("/api/upload")
async def upload_mixtape(
    files: list[UploadFile] = File(...),
    name: str = Form(...),
    passcode: str = Form(""),
    expiration: int = Form(0),
    cover: UploadFile = File(None),
    notify_email: str = Form(""),
    from_name: str = Form(""),
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

    # Auto-name from first track's album + artist if name is generic
    if not name or name.strip() == "" or name == "Mixtape":
        first = tracks[0]
        name = first.get("album", "") or first.get("title", "Mixtape")
        if first.get("artist", "Unknown") != "Unknown":
            name = f"{name} — {first['artist']}"

    # Extract dominant color from cover for player theming
    accent_color = ""
    if cover_path:
        cover_full_path = os.path.join(COVERS_DIR, os.path.basename(cover_path))
        if os.path.isfile(cover_full_path):
            accent_color = extract_dominant_color(cover_full_path)

    # Set expiration
    expires_at = 0
    if expiration > 0:
        expires_at = int(time.time()) + (expiration * 86400)

    create_share(share_id, name, folder, passcode, cover_path, accent_color, expires_at, from_name)
    set_tracks(share_id, tracks)

    # Send email notification if requested
    if notify_email:
        share_url = f"{BASE_URL}/share/{share_id}"
        tracks_list = tracks[:5]
        track_list_html = "".join(
            f"<li style='padding:4px 0;border-bottom:1px solid #eee;'>{t['title']} — {t['artist']}</li>"
            for t in tracks_list
        )
        if len(tracks) > 5:
            track_list_html += f"<li style='padding:4px 0;color:#888;'>… and {len(tracks)-5} more</li>"
        email_html = f"""
        <div style="font-family:sans-serif;max-width:480px;margin:0 auto;">
        <h1 style="font-size:1.3rem;">📼 {name}</h1>
        <p>Someone shared a mixtape with you!</p>
        <a href="{share_url}" style="display:inline-block;padding:10px 24px;background:#eb5e28;color:#fff;text-decoration:none;border-radius:4px;margin:12px 0;">Listen to Mixtape</a>
        <h3 style="margin-top:16px;">Tracks ({len(tracks)})</h3>
        <ul style="list-style:none;padding:0;">{track_list_html}</ul>
        <p style="color:#888;font-size:0.8rem;margin-top:16px;">Sent via mixtape.janhellion.com</p>
        </div>"""
        send_email(notify_email, f"📼 {name} — Un mixtape para ti", email_html)

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
        expired = s["expires_at"] > 0 and s["expires_at"] < time.time()
        result.append({
            "id": s["id"],
            "name": s["name"],
            "created_at": s["created_at"],
            "expires_at": s["expires_at"],
            "expired": expired,
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


@app.get("/api/download/track/{share_id}/{filename:path}")
async def download_track(share_id: str, filename: str):
    """Download a single track from a mixtape."""
    share = get_share(share_id)
    if not share:
        raise HTTPException(404)
    filepath = os.path.join(share["path"], filename)
    if not os.path.isfile(filepath):
        raise HTTPException(404)

    async def file_stream():
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk
                await asyncio.sleep(0)

    safe_name = re.sub(r"[^\w\s.-]", "", filename)
    return StreamingResponse(
        file_stream(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
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

    safe_name = re.sub(r"[^\w\s-]", "", share["name"]).strip().replace(" ", "_")

    # Build ZIP with a temp file — use STORED since FLAC/MP3 are already compressed
    zip_mode = zipfile.ZIP_STORED
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        with zipfile.ZipFile(tmp, "w", zip_mode) as zf:
            # Add audio files
            for t in tracks:
                fp = os.path.join(folder, t["filename"])
                if os.path.isfile(fp):
                    ext = t["filename"].rsplit(".", 1)[-1]
                    arcname = f"{t.get('title', t['filename'])}.{ext}"
                    zf.write(fp, arcname)

            # Add title.txt
            zf.writestr("title.txt", share["name"])

            # Add from.txt
            from_name = share.get("from_name", "").strip()
            if from_name:
                zf.writestr("from.txt", from_name)
            else:
                zf.writestr("from.txt", "mixtape")

            # Add playlist.m3u
            m3u_lines = ["#EXTM3U"]
            for t in tracks:
                ext = t["filename"].rsplit(".", 1)[-1]
                title = t.get("title", t["filename"])
                artist = t.get("artist", "Unknown")
                duration = int(t.get("duration", 0))
                arcname = f"{title}.{ext}"
                m3u_lines.append(f"#EXTINF:{duration},{artist} - {title}")
                m3u_lines.append(arcname)
            zf.writestr(f"{safe_name}.m3u", "\n".join(m3u_lines) + "\n")

        tmp.close()
        file_size = os.path.getsize(tmp.name)

        async def stream_zip():
            with open(tmp.name, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
                    await asyncio.sleep(0)
            os.unlink(tmp.name)

        return StreamingResponse(
            stream_zip(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}.zip"',
                "Content-Length": str(file_size),
            },
        )
    except Exception as e:
        os.unlink(tmp.name)
        raise HTTPException(500, detail=str(e))


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

    # Check expiration
    if share.get("expires_at", 0) > 0 and share["expires_at"] < time.time():
        return templates.TemplateResponse("player.html", {
            "request": request, "share": None, "tracks": [],
            "base_url": BASE_URL, "error": "This mixtape has expired",
        })

    tracks = get_tracks(share_id)
    return templates.TemplateResponse("player.html", {
        "request": request, "share": share, "tracks": tracks,
        "base_url": BASE_URL, "error": None,
        "accent_color": share.get("accent_color", "") if share else "",
    })


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "base_url": BASE_URL})


@app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    user = authenticate_user(data.get("username", ""), data.get("password", ""))
    if not user:
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    token = create_session(user["id"])
    resp = JSONResponse({"ok": True, "token": token, "is_admin": user["is_admin"]})
    resp.set_cookie(key="session", value=token, httponly=True, max_age=86400 * 7, samesite="lax")
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    token = request.cookies.get("session", "")
    if token:
        delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = "", code: str = ""):
    return templates.TemplateResponse("register.html", {
        "request": request, "error": error, "code": code, "base_url": BASE_URL,
    })


@app.post("/api/register")
async def api_register(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    invite = data.get("invite_code", "").strip().upper()

    if len(username) < 3 or len(password) < 4:
        return JSONResponse({"error": "Username (3+ chars) and password (4+ chars) required"}, status_code=400)

    # Verify invite code exists and is unused
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM invitation_codes WHERE code = ? AND used_by IS NULL", (invite,)
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"error": "Invalid or already used invite code"}, status_code=400)

    uid = create_user(username, password)
    if not uid:
        return JSONResponse({"error": "Username already taken"}, status_code=400)

    use_invite_code(invite, uid)
    return JSONResponse({"ok": True})


@app.get("/api/me")
async def api_me(request: Request):
    token = request.cookies.get("session", "")
    user = get_user_by_session(token)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return JSONResponse({
        "id": user["id"],
        "username": user["username"],
        "is_admin": user["is_admin"],
    })


@app.get("/api/admin/invites")
async def api_admin_invites(request: Request):
    token = request.cookies.get("session", "")
    user = get_user_by_session(token)
    if not user or not is_admin_user(user["id"]):
        raise HTTPException(401)
    codes = get_invite_codes()
    return JSONResponse(codes)


@app.post("/api/admin/invites")
async def api_create_invite(request: Request):
    token = request.cookies.get("session", "")
    user = get_user_by_session(token)
    if not user or not is_admin_user(user["id"]):
        raise HTTPException(401)
    code = create_invite_code(user["id"])
    return JSONResponse({"code": code})


@app.delete("/api/admin/invites/{code}")
async def api_delete_invite(request: Request, code: str):
    token = request.cookies.get("session", "")
    user = get_user_by_session(token)
    if not user or not is_admin_user(user["id"]):
        raise HTTPException(401)
    conn = get_conn()
    conn.execute("DELETE FROM invitation_codes WHERE code = ?", (code,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.delete("/api/admin/users/{user_id}")
async def api_delete_user(request: Request, user_id: int):
    token = request.cookies.get("session", "")
    admin = get_user_by_session(token)
    if not admin or not is_admin_user(admin["id"]):
        raise HTTPException(401)
    # Prevent self-deletion
    if admin["id"] == user_id:
        return JSONResponse({"error": "Cannot delete yourself"}, status_code=400)
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM invitation_codes WHERE used_by = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    token = request.cookies.get("session", "")
    user = get_user_by_session(token)
    if not user or not is_admin_user(user["id"]):
        raise HTTPException(401)
    users = get_all_users()
    codes = get_invite_codes()
    return JSONResponse({"users": users, "invite_codes": codes})


@app.on_event("startup")
async def startup():
    for s in get_all_shares():
        if s.get("path") and not os.path.isdir(s["path"]):
            delete_share(s["id"])
            print(f"Cleaned up stale share: {s['id']} ({s['name']})")


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
