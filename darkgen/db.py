"""Persistência em SQLite: canais, vídeos, estado interno e log de atividade."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import coerce_settings, env, merge_settings

TABLES = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    preset TEXT,
    settings TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    youtube_channel_id TEXT,
    youtube_title TEXT,
    youtube_thumb TEXT,
    subscribers INTEGER,
    views INTEGER,
    video_count INTEGER,
    stats_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('short', 'long')),
    status TEXT NOT NULL DEFAULT 'queued',
    category TEXT,
    topic TEXT,
    is_fiction INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    description TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    script TEXT,              -- JSON do roteiro (cenas)
    sources TEXT NOT NULL DEFAULT '[]',
    thumbnail_text TEXT,
    scheduled_at TEXT,        -- ISO 8601 UTC
    approved INTEGER NOT NULL DEFAULT 0,
    video_path TEXT,
    thumb_path TEXT,
    duration_s REAL,
    youtube_id TEXT,
    uploaded_at TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    stats_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    channel_id INTEGER,
    video_id INTEGER,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'short',
    status TEXT NOT NULL DEFAULT 'queued',  -- queued, writing, ready, done, failed, cancelled
    title TEXT,
    topic TEXT,
    angle TEXT,
    logline TEXT,
    is_fiction INTEGER NOT NULL DEFAULT 0,
    parts INTEGER NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    playlist_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trend_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    platform TEXT NOT NULL,
    params TEXT NOT NULL,
    results TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_stats (
    channel_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    subscribers INTEGER,
    views INTEGER,
    videos INTEGER,
    PRIMARY KEY (channel_id, ts)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_sched ON videos(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id, status);
CREATE INDEX IF NOT EXISTS idx_activity_channel ON activity(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_videos_series ON videos(series_id, episode);
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
IN_PRODUCTION = ["queued", "scripting", "scripted", "rendering", "rendered", "uploading"]

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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Converte o banco da versão de canal único para multi-canal."""
    if "channel_id" in _columns(conn, "videos"):
        return
    conn.execute("ALTER TABLE videos ADD COLUMN channel_id INTEGER")
    if "uploaded_at" not in _columns(conn, "videos"):
        conn.execute("ALTER TABLE videos ADD COLUMN uploaded_at TEXT")
    if "channel_id" not in _columns(conn, "activity"):
        conn.execute("ALTER TABLE activity ADD COLUMN channel_id INTEGER")

    old = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT key, value FROM settings WHERE key NOT LIKE '\\_state:%' ESCAPE '\\'")}
    state_ch = conn.execute("SELECT value FROM settings WHERE key = '_state:channel'").fetchone()
    info = json.loads(state_ch[0]) if state_ch else {}
    settings = merge_settings(old)
    if "channel_timezone" in old:
        settings["timezone"] = old["channel_timezone"]
    ts = iso(now_utc())
    cur = conn.execute(
        "INSERT INTO channels (name, settings, youtube_channel_id, youtube_title, subscribers, views, video_count, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (info.get("title") or "Canal 1", json.dumps(settings, ensure_ascii=False), info.get("id"), info.get("title"),
         info.get("subscribers"), info.get("views"), info.get("videos"), ts, ts),
    )
    cid = cur.lastrowid
    conn.execute("UPDATE videos SET channel_id = ?", (cid,))
    conn.execute("UPDATE videos SET uploaded_at = updated_at WHERE youtube_id IS NOT NULL")
    conn.execute("UPDATE activity SET channel_id = ?", (cid,))
    conn.execute("DELETE FROM settings WHERE key NOT LIKE '\\_state:%' ESCAPE '\\' OR key = '_state:channel'")
    has_old_snap = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='channel_snapshots'"
    ).fetchone()
    if has_old_snap:
        conn.execute(
            "INSERT OR IGNORE INTO channel_stats (channel_id, ts, subscribers, views, videos) "
            "SELECT ?, ts, subscribers, views, videos FROM channel_snapshots", (cid,),
        )
        conn.execute("DROP TABLE channel_snapshots")
    old_token = env().data_dir / "youtube_token.json"
    if old_token.exists():
        shutil.move(str(old_token), str(env().channel_dir(cid) / "youtube_token.json"))


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Colunas adicionadas depois da v2 (bancos já existentes)."""
    cols = _columns(conn, "videos")
    if "series_id" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN series_id INTEGER")
    if "episode" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN episode INTEGER")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    global _initialized
    with _lock:
        conn = sqlite3.connect(env().db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if not _initialized:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(TABLES)
                _migrate_v1(conn)
                _ensure_columns(conn)
                conn.executescript(INDEXES)
                conn.commit()
                _initialized = True
            yield conn
            conn.commit()
        finally:
            conn.close()


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


# ---------------------------------------------------------------- canais

def _row_to_channel(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["active"] = bool(d["active"])
    d["settings"] = merge_settings(json.loads(d["settings"] or "{}"))
    return d


def create_channel(name: str, settings: dict | None = None, preset: str | None = None, notes: str | None = None) -> dict:
    ts = iso(now_utc())
    full = merge_settings(coerce_settings(settings or {}))
    with connect() as c:
        cur = c.execute(
            "INSERT INTO channels (name, preset, settings, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, preset, json.dumps(full, ensure_ascii=False), notes, ts, ts),
        )
        cid = cur.lastrowid
    env().channel_dir(cid)
    return get_channel(cid)  # type: ignore[return-value]


def get_channel(channel_id: int) -> dict | None:
    with connect() as c:
        return _row_to_channel(c.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone())


def list_channels(active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM channels" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
    with connect() as c:
        return [_row_to_channel(r) for r in c.execute(sql).fetchall()]  # type: ignore[misc]


def update_channel(channel_id: int, settings: dict | None = None, **fields: Any) -> dict | None:
    ch = get_channel(channel_id)
    if ch is None:
        return None
    if settings:
        merged = {**ch["settings"], **coerce_settings(settings)}
        fields["settings"] = json.dumps(merged, ensure_ascii=False)
    if fields:
        data = _encode({**fields, "updated_at": now_utc()})
        sets = ", ".join(f"{k} = ?" for k in data)
        with connect() as c:
            c.execute(f"UPDATE channels SET {sets} WHERE id = ?", [*data.values(), channel_id])
    return get_channel(channel_id)


def delete_channel(channel_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM videos WHERE channel_id = ?", (channel_id,))
        c.execute("DELETE FROM series WHERE channel_id = ?", (channel_id,))
        c.execute("DELETE FROM activity WHERE channel_id = ?", (channel_id,))
        c.execute("DELETE FROM channel_stats WHERE channel_id = ?", (channel_id,))
        c.execute("DELETE FROM channels WHERE id = ?", (channel_id,))


def save_channel_stats(channel_id: int, subscribers: int, views: int, videos: int) -> None:
    ts = now_utc()
    with connect() as c:
        # um ponto por dia: substitui o do mesmo dia
        c.execute("DELETE FROM channel_stats WHERE channel_id = ? AND substr(ts, 1, 10) = ?",
                  (channel_id, ts.date().isoformat()))
        c.execute("INSERT INTO channel_stats (channel_id, ts, subscribers, views, videos) VALUES (?, ?, ?, ?, ?)",
                  (channel_id, iso(ts), subscribers, views, videos))


def channel_stats(channel_id: int, limit: int = 90) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM channel_stats WHERE channel_id = ? ORDER BY ts DESC LIMIT ?",
                         (channel_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------- vídeos

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


def create_video(channel_id: int, kind: str, **fields: Any) -> dict:
    ts = iso(now_utc())
    data = _encode({"channel_id": channel_id, "kind": kind, **fields, "created_at": ts, "updated_at": ts})
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


def transition(video_id: int, from_status: str, to_status: str) -> bool:
    """Muda o status só se ainda estiver em `from_status` (evita dois workers pegarem o mesmo vídeo)."""
    with connect() as c:
        cur = c.execute("UPDATE videos SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                        (to_status, iso(now_utc()), video_id, from_status))
        return cur.rowcount == 1


def delete_video(video_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM videos WHERE id = ?", (video_id,))


def list_videos(
    status: str | list[str] | None = None,
    kind: str | None = None,
    channel_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    order: str = "COALESCE(scheduled_at, created_at) DESC",
    active_channels_only: bool = False,
    series_id: int | None = None,
    exclude_series: bool = False,
) -> list[dict]:
    where, args = [], []
    if series_id is not None:
        where.append("series_id = ?")
        args.append(series_id)
    if exclude_series:
        where.append("series_id IS NULL")
    if status:
        statuses = [status] if isinstance(status, str) else status
        where.append(f"status IN ({', '.join('?' for _ in statuses)})")
        args += statuses
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if channel_id is not None:
        where.append("channel_id = ?")
        args.append(channel_id)
    if active_channels_only:
        where.append("channel_id IN (SELECT id FROM channels WHERE active = 1)")
    sql = "SELECT * FROM videos"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
    with connect() as c:
        return [_row_to_video(r) for r in c.execute(sql, [*args, limit, offset]).fetchall()]  # type: ignore[misc]


def status_counts(channel_id: int | None = None) -> dict[str, int]:
    sql = "SELECT status, COUNT(*) AS n FROM videos"
    args: tuple = ()
    if channel_id is not None:
        sql += " WHERE channel_id = ?"
        args = (channel_id,)
    with connect() as c:
        rows = c.execute(sql + " GROUP BY status", args).fetchall()
    counts = {s: 0 for s in STATUSES}
    counts.update({r["status"]: r["n"] for r in rows})
    return counts


def recent_topics(channel_id: int | None = None, limit: int = 200) -> list[str]:
    sql = "SELECT COALESCE(title, topic) AS t FROM videos WHERE COALESCE(title, topic) IS NOT NULL"
    args: list = []
    if channel_id is not None:
        sql += " AND channel_id = ?"
        args.append(channel_id)
    with connect() as c:
        rows = c.execute(sql + " ORDER BY created_at DESC LIMIT ?", [*args, limit]).fetchall()
    return [r["t"] for r in rows]


def occupied_slots(channel_id: int, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    with connect() as c:
        rows = c.execute(
            "SELECT scheduled_at, kind FROM videos WHERE channel_id = ? AND scheduled_at >= ? AND scheduled_at < ? "
            "AND status != 'failed'",
            (channel_id, iso(start), iso(end)),
        ).fetchall()
    return [(parse_iso(r["scheduled_at"]), r["kind"]) for r in rows]  # type: ignore[misc]


def uploads_since(channel_ids: list[int], since: datetime) -> int:
    if not channel_ids:
        return 0
    with connect() as c:
        row = c.execute(
            f"SELECT COUNT(*) AS n FROM videos WHERE uploaded_at >= ? AND channel_id IN ({', '.join('?' for _ in channel_ids)})",
            (iso(since), *channel_ids),
        ).fetchone()
    return row["n"]


def last_published_at(channel_id: int) -> datetime | None:
    with connect() as c:
        row = c.execute(
            "SELECT MAX(scheduled_at) AS t FROM videos WHERE channel_id = ? AND status = 'published'", (channel_id,)
        ).fetchone()
    return parse_iso(row["t"])


# ---------------------------------------------------------------- estado interno

def get_state(key: str, default: Any = None) -> Any:
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (f"_state:{key}",)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value: Any) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"_state:{key}", json.dumps(value, ensure_ascii=False)),
        )


# ---------------------------------------------------------------- atividade

def log(message: str, level: str = "info", video_id: int | None = None, channel_id: int | None = None) -> None:
    if channel_id is None and video_id is not None:
        v = get_video(video_id)
        channel_id = v["channel_id"] if v else None
    with connect() as c:
        c.execute(
            "INSERT INTO activity (ts, level, channel_id, video_id, message) VALUES (?, ?, ?, ?, ?)",
            (iso(now_utc()), level, channel_id, video_id, message),
        )
        c.execute("DELETE FROM activity WHERE id <= (SELECT MAX(id) - 5000 FROM activity)")


def recent_activity(limit: int = 50, channel_id: int | None = None, level: str | None = None,
                    since: datetime | None = None) -> list[dict]:
    where, args = [], []
    if channel_id is not None:
        where.append("channel_id = ?")
        args.append(channel_id)
    if level:
        where.append("level = ?")
        args.append(level)
    if since:
        where.append("ts >= ?")
        args.append(iso(since))
    sql = "SELECT * FROM activity" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    with connect() as c:
        return [dict(r) for r in c.execute(sql, [*args, limit]).fetchall()]


# ---------------------------------------------------------------- varreduras de tendências

def save_trend_scan(platform: str, params: dict, results: Any) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO trend_scans (ts, platform, params, results) VALUES (?, ?, ?, ?)",
            (iso(now_utc()), platform, json.dumps(params, ensure_ascii=False), json.dumps(results, ensure_ascii=False)),
        )
        return cur.lastrowid  # type: ignore[return-value]


def list_trend_scans(limit: int = 20) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT id, ts, platform, params FROM trend_scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "params": json.loads(r["params"])} for r in rows]


def get_trend_scan(scan_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM trend_scans WHERE id = ?", (scan_id,)).fetchone()
    if not r:
        return None
    return {**dict(r), "params": json.loads(r["params"]), "results": json.loads(r["results"])}


# ---------------------------------------------------------------- séries

def _row_to_series(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["is_fiction"] = bool(d["is_fiction"])
    d["sources"] = json.loads(d["sources"] or "[]")
    return d


def create_series(channel_id: int, kind: str, parts: int, **fields: Any) -> dict:
    ts = iso(now_utc())
    data = _encode({"channel_id": channel_id, "kind": kind, "parts": parts, **fields, "created_at": ts, "updated_at": ts})
    if "sources" in data and not isinstance(data["sources"], str):
        data["sources"] = json.dumps(data["sources"], ensure_ascii=False)
    with connect() as c:
        cur = c.execute(f"INSERT INTO series ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})",
                        list(data.values()))
        sid = cur.lastrowid
    return get_series(sid)  # type: ignore[return-value]


def get_series(series_id: int) -> dict | None:
    with connect() as c:
        return _row_to_series(c.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone())


def update_series(series_id: int, **fields: Any) -> dict | None:
    if "sources" in fields and not isinstance(fields["sources"], str):
        fields["sources"] = json.dumps(fields["sources"], ensure_ascii=False)
    if fields:
        data = _encode({**fields, "updated_at": now_utc()})
        with connect() as c:
            c.execute(f"UPDATE series SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                      [*data.values(), series_id])
    return get_series(series_id)


def transition_series(series_id: int, from_status: str, to_status: str) -> bool:
    with connect() as c:
        cur = c.execute("UPDATE series SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                        (to_status, iso(now_utc()), series_id, from_status))
        return cur.rowcount == 1


def list_series(channel_id: int | None = None, status: str | list[str] | None = None, limit: int = 100) -> list[dict]:
    where, args = [], []
    if channel_id is not None:
        where.append("channel_id = ?")
        args.append(channel_id)
    if status:
        statuses = [status] if isinstance(status, str) else status
        where.append(f"status IN ({', '.join('?' for _ in statuses)})")
        args += statuses
    sql = "SELECT * FROM series" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    with connect() as c:
        return [_row_to_series(r) for r in c.execute(sql, [*args, limit]).fetchall()]  # type: ignore[misc]


def last_series_created_at(channel_id: int) -> datetime | None:
    with connect() as c:
        row = c.execute("SELECT MAX(created_at) AS t FROM series WHERE channel_id = ? AND status != 'cancelled'",
                        (channel_id,)).fetchone()
    return parse_iso(row["t"])


def episodes(series_id: int) -> list[dict]:
    return list_videos(series_id=series_id, limit=100, order="episode ASC")


def previous_episode(video: dict) -> dict | None:
    if not video.get("series_id") or not video.get("episode") or video["episode"] <= 1:
        return None
    with connect() as c:
        row = c.execute("SELECT * FROM videos WHERE series_id = ? AND episode < ? ORDER BY episode DESC LIMIT 1",
                        (video["series_id"], video["episode"])).fetchone()
    return _row_to_video(row)
