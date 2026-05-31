import os
import sqlite3
import time

DB_PATH = os.environ.get("DATABASE_URL", "/storage/app.db")
if DB_PATH.startswith("sqlite:///"):
    DB_PATH = DB_PATH[10:]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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
            accent_color TEXT DEFAULT ''
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
    """)
    conn.commit()
    conn.close()


def create_share(share_id, name, path, passcode="", cover_path="", accent_color=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO shares (id, name, path, passcode, created_at, cover_path, accent_color) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (share_id, name, path, passcode, int(time.time()), cover_path, accent_color),
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
    allowed = {"name", "passcode", "expires_at", "cover_path", "accent_color"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [share_id]
    conn = get_conn()
    conn.execute(f"UPDATE shares SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()
