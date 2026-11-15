"""Persistência em SQLite: vídeos, configurações e log de atividade."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DEFAULT_SETTINGS, env

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('short', 'long')),
    status TEXT NOT NULL DEFAULT 'queued',
    category TEXT,
    topic TEXT,
    is_fiction INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    description TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    script TEXT,              -- JSON do roteiro (narração + cenas)
    sources TEXT NOT NULL DEFAULT '[]',
    thumbnail_text TEXT,
    scheduled_at TEXT,        -- ISO 8601 UTC
    approved INTEGER NOT NULL DEFAULT 0,
    video_path TEXT,
    thumb_path TEXT,
    duration_s REAL,
    youtube_id TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    stats_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_sched ON videos(scheduled_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    video_id INTEGER,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_snapshots (
    ts TEXT PRIMARY KEY,
    subscribers INTEGER,
    views INTEGER,
    videos INTEGER
);
"""

# Estados do pipeline, na ordem em que acontecem.
STATUSES = [
    "queued",      # criado; aguardando roteiro
    "scripting",
    "scripted",    # roteiro pronto; aguardando renderização
    "rendering",
    "rendered",    # vídeo pronto; aguardando aprovação/upload
    "uploading",
    "scheduled",   # no YouTube como privado com publishAt no futuro
    "published",
    "failed",
]

_JSON_FIELDS = {"tags", "sources", "script"}
_lock = threading.RLock()
_initialized = False


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    global _initialized
    with _lock:
        conn = sqlite3.connect(env().db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if not _initialized:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                _initialized = True
            yield conn
            conn.commit()
        finally:
            conn.close()


def _row_to_video(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in _JSON_FIELDS:
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except json.JSONDecodeError:
                pass
    d["is_fiction"] = bool(d["is_fiction"])
    d["approved"] = bool(d["approved"])
    return d


def _encode(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k in _JSON_FIELDS and v is not None and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            v = int(v)
        elif isinstance(v, datetime):
            v = iso(v)
        out[k] = v
    return out


# ---------------------------------------------------------------- vídeos

def create_video(kind: str, **fields: Any) -> dict:
    ts = iso(now_utc())
    data = _encode({"kind": kind, **fields, "created_at": ts, "updated_at": ts})
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    with connect() as c:
        cur = c.execute(f"INSERT INTO videos ({cols}) VALUES ({marks})", list(data.values()))
        vid = cur.lastrowid
    return get_video(vid)  # type: ignore[return-value]


def get_video(video_id: int) -> dict | None:
    with connect() as c:
        return _row_to_video(c.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone())


def update_video(video_id: int, **fields: Any) -> dict | None:
    if fields:
        data = _encode({**fields, "updated_at": now_utc()})
        sets = ", ".join(f"{k} = ?" for k in data)
        with connect() as c:
            c.execute(f"UPDATE videos SET {sets} WHERE id = ?", [*data.values(), video_id])
    return get_video(video_id)


def delete_video(video_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM videos WHERE id = ?", (video_id,))


def list_videos(
    status: str | list[str] | None = None,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
    order: str = "COALESCE(scheduled_at, created_at) DESC",
) -> list[dict]:
    where, args = [], []
    if status:
        statuses = [status] if isinstance(status, str) else status
        where.append(f"status IN ({', '.join('?' for _ in statuses)})")
        args += statuses
    if kind:
        where.append("kind = ?")
        args.append(kind)
    sql = "SELECT * FROM videos"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
    with connect() as c:
        return [_row_to_video(r) for r in c.execute(sql, [*args, limit, offset]).fetchall()]  # type: ignore[misc]


def claim_next(from_status: str, to_status: str, extra_where: str = "", args: tuple = ()) -> dict | None:
    """Move atomicamente o vídeo mais antigo de um estado para outro (fila de trabalho)."""
    with connect() as c:
        row = c.execute(
            f"SELECT id FROM videos WHERE status = ? {extra_where} "
            "ORDER BY COALESCE(scheduled_at, created_at) ASC LIMIT 1",
            (from_status, *args),
        ).fetchone()
        if row is None:
            return None
        c.execute(
            "UPDATE videos SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (to_status, iso(now_utc()), row["id"], from_status),
        )
    return get_video(row["id"])


def status_counts() -> dict[str, int]:
    with connect() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status").fetchall()
    counts = {s: 0 for s in STATUSES}
    counts.update({r["status"]: r["n"] for r in rows})
    return counts


def recent_topics(limit: int = 200) -> list[str]:
    with connect() as c:
        rows = c.execute(
            "SELECT COALESCE(title, topic) AS t FROM videos WHERE COALESCE(title, topic) IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["t"] for r in rows]


def occupied_slots(start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    with connect() as c:
        rows = c.execute(
            "SELECT scheduled_at, kind FROM videos WHERE scheduled_at >= ? AND scheduled_at < ? "
            "AND status != 'failed'",
            (iso(start), iso(end)),
        ).fetchall()
    return [(parse_iso(r["scheduled_at"]), r["kind"]) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------- configurações

def get_settings() -> dict:
    with connect() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: json.loads(r["value"]) for r in rows}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}


def update_settings(changes: dict) -> dict:
    unknown = set(changes) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError(f"Configurações desconhecidas: {', '.join(sorted(unknown))}")
    with connect() as c:
        for k, v in changes.items():
            default = DEFAULT_SETTINGS[k]
            if isinstance(default, bool):
                v = bool(v)
            elif isinstance(default, int):
                v = int(v)
            elif isinstance(default, float):
                v = float(v)
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, json.dumps(v, ensure_ascii=False)),
            )
    return get_settings()


def get_state(key: str, default: Any = None) -> Any:
    """Estado interno (fora das configurações editáveis), ex.: heartbeat do worker."""
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (f"_state:{key}",)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value: Any) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"_state:{key}", json.dumps(value, ensure_ascii=False)),
        )


# ---------------------------------------------------------------- atividade

def log(message: str, level: str = "info", video_id: int | None = None) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO activity (ts, level, video_id, message) VALUES (?, ?, ?, ?)",
            (iso(now_utc()), level, video_id, message),
        )
        c.execute("DELETE FROM activity WHERE id <= (SELECT MAX(id) - 2000 FROM activity)")


def recent_activity(limit: int = 50) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def save_channel_snapshot(subscribers: int, views: int, videos: int) -> None:
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO channel_snapshots (ts, subscribers, views, videos) VALUES (?, ?, ?, ?)",
            (iso(now_utc()), subscribers, views, videos),
        )


def channel_snapshots(limit: int = 90) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM channel_snapshots ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]
