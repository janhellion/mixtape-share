import os
import sqlite3
import time
import hashlib
import secrets
import string

DB_PATH = os.environ.get("DATABASE_URL", "/storage/app.db")
if DB_PATH.startswith("sqlite:///"):
    DB_PATH = DB_PATH[10:]


def hash_password(password):
    """Hash a password using PBKDF2-SHA256."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return salt.hex() + ":" + dk.hex()


def verify_password(password, stored):
    """Verify a password against its hash."""
    salt_hex, dk_hex = stored.split(":")
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return dk.hex() == dk_hex


def generate_token(length=32):
    """Generate a secure random token."""
    return secrets.token_hex(length)


def generate_invite_code():
    """Generate a short invite code."""
    chars = string.ascii_uppercase + string.digits
    return "MIX-" + "".join(secrets.choice(chars) for _ in range(8))


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shares (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            passcode TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            expires_at INTEGER DEFAULT 0,
            download_count INTEGER DEFAULT 0,
            track_count INTEGER DEFAULT 0,
            total_duration INTEGER DEFAULT 0,
            cover_path TEXT DEFAULT '',
            accent_color TEXT DEFAULT '',
            from_name TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            share_id TEXT NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            title TEXT,
            artist TEXT,
            duration REAL DEFAULT 0,
            track_number INTEGER DEFAULT 0,
            UNIQUE(share_id, filename)
        );

        CREATE TABLE IF NOT EXISTS play_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER REFERENCES tracks(id),
            played_at INTEGER NOT NULL,
            ip TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            is_admin INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS invitation_codes (
            code TEXT PRIMARY KEY,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at INTEGER NOT NULL,
            used_by INTEGER REFERENCES users(id),
            used_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS short_links (
            code TEXT PRIMARY KEY,
            share_id TEXT NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    # Create default admin user if no users exist
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    if row["cnt"] == 0:
        admin_pass = hash_password("admin")
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, is_admin) VALUES (?, ?, ?, 1)",
            ("admin", admin_pass, int(time.time())),
        )
        print("Created default admin user: admin / admin")
    conn.close()


def create_share(share_id, name, path, passcode="", cover_path="", accent_color="", expires_at=0, from_name=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO shares (id, name, path, passcode, created_at, cover_path, accent_color, expires_at, from_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (share_id, name, path, passcode, int(time.time()), cover_path, accent_color, expires_at, from_name),
    )
    conn.commit()
    conn.close()


def get_share(share_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM shares WHERE id = ?", (share_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_shares():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM shares ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_share(share_id):
    conn = get_conn()
    conn.execute("DELETE FROM play_events WHERE track_id IN (SELECT id FROM tracks WHERE share_id = ?)", (share_id,))
    conn.execute("DELETE FROM tracks WHERE share_id = ?", (share_id,))
    conn.execute("DELETE FROM shares WHERE id = ?", (share_id,))
    conn.commit()
    conn.close()


def get_tracks(share_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tracks WHERE share_id = ? ORDER BY track_number, filename",
        (share_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_tracks(share_id, tracks_list):
    conn = get_conn()
    conn.execute("DELETE FROM tracks WHERE share_id = ?", (share_id,))
    for i, t in enumerate(tracks_list):
        conn.execute(
            "INSERT OR IGNORE INTO tracks (share_id, filename, title, artist, duration, track_number) VALUES (?,?,?,?,?,?)",
            (share_id, t["filename"], t.get("title", ""), t.get("artist", ""), t.get("duration", 0), i + 1),
        )
    conn.execute(
        "UPDATE shares SET track_count = (SELECT COUNT(*) FROM tracks WHERE share_id = ?) WHERE id = ?",
        (share_id, share_id),
    )
    conn.commit()
    conn.close()


def update_track_meta(share_id, filename, title, artist):
    conn = get_conn()
    conn.execute(
        "UPDATE tracks SET title = ?, artist = ? WHERE share_id = ? AND filename = ?",
        (title, artist, share_id, filename),
    )
    conn.commit()
    conn.close()


def increment_download(share_id):
    conn = get_conn()
    conn.execute("UPDATE shares SET download_count = download_count + 1 WHERE id = ?", (share_id,))
    conn.commit()
    conn.close()


def increment_play(share_id):
    conn = get_conn()
    conn.execute("UPDATE shares SET play_count = COALESCE(play_count,0) + 1 WHERE id = ?", (share_id,))
    conn.commit()
    conn.close()


def log_play(track_id, ip=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO play_events (track_id, played_at, ip) VALUES (?, ?, ?)",
        (track_id, int(time.time()), ip),
    )
    conn.commit()
    conn.close()


def update_share(share_id, **kwargs):
    allowed = {"name", "passcode", "expires_at", "cover_path", "accent_color", "from_name"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [share_id]
    conn = get_conn()
    conn.execute(f"UPDATE shares SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()


# ─── Auth Functions ──────────────────────────────────────────

def create_user(username, password):
    conn = get_conn()
    pw_hash = hash_password(password)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, pw_hash, int(time.time())),
        )
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        return uid["id"] if uid else None
    except sqlite3.IntegrityError:
        conn.close()
        return None


def authenticate_user(username, password):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if row and verify_password(password, row["password_hash"]):
        return dict(row)
    return None


def create_session(user_id):
    token = generate_token()
    conn = get_conn()
    conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)", (token, user_id, int(time.time())))
    conn.commit()
    conn.close()
    return token


def get_user_by_session(token):
    conn = get_conn()
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token = ?",
        (token,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(token):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def create_invite_code(created_by):
    code = generate_invite_code()
    conn = get_conn()
    conn.execute(
        "INSERT INTO invitation_codes (code, created_by, created_at) VALUES (?, ?, ?)",
        (code, created_by, int(time.time())),
    )
    conn.commit()
    conn.close()
    return code


def use_invite_code(code, user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM invitation_codes WHERE code = ? AND used_by IS NULL", (code,)).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute("UPDATE invitation_codes SET used_by = ?, used_at = ? WHERE code = ?", (user_id, int(time.time()), code))
    conn.commit()
    conn.close()
    return True


def get_invite_codes():
    conn = get_conn()
    rows = conn.execute(
        """SELECT ic.*, u.username as creator_name, uu.username as used_by_name
           FROM invitation_codes ic
           JOIN users u ON ic.created_by = u.id
           LEFT JOIN users uu ON ic.used_by = uu.id
           ORDER BY ic.created_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_users():
    conn = get_conn()
    rows = conn.execute("SELECT id, username, created_at, is_admin FROM users ORDER BY created_at ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_admin_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row and row["is_admin"] == 1


# ─── Short Link Functions ────────────────────────────────────

import string as _string

def _generate_short_code(length=5):
    """Generate a random short alphanumeric code."""
    chars = _string.ascii_letters + _string.digits
    while True:
        code = "".join(secrets.choice(chars) for _ in range(length))
        yield code


def create_short_link(share_id):
    """Create a short link for a share. Returns the code."""
    conn = get_conn()
    code_gen = _generate_short_code()
    for _ in range(10):  # Try up to 10 times to avoid collisions
        code = next(code_gen)
        existing = conn.execute("SELECT 1 FROM short_links WHERE code = ?", (code,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO short_links (code, share_id, created_at) VALUES (?, ?, ?)",
                (code, share_id, int(time.time())),
            )
            conn.commit()
            conn.close()
            return code
    conn.close()
    # Fallback: use hash
    import hashlib
    return hashlib.md5(share_id.encode()).hexdigest()[:5]


def get_short_link(code):
    """Get the share_id for a short code."""
    conn = get_conn()
    row = conn.execute("SELECT share_id FROM short_links WHERE code = ?", (code,)).fetchone()
    conn.close()
    return row["share_id"] if row else None


def get_short_links_for_share(share_id):
    """Get all short codes for a share."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT code FROM short_links WHERE share_id = ? ORDER BY created_at DESC",
        (share_id,),
    ).fetchall()
    conn.close()
    return [r["code"] for r in rows]
