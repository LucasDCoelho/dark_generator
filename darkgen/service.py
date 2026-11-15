"""Camada de serviço compartilhada pelo painel web (REST) e pelo servidor MCP."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import db, pipeline
from .config import env
from .scheduling import best_times


_WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


class ServiceError(ValueError):
    pass


def _local(dt_iso: str | None) -> str | None:
    dt = db.parse_iso(dt_iso)
    return dt.astimezone(env().tz).strftime("%Y-%m-%d %H:%M") if dt else None


def parse_when(value: str | None) -> datetime | None:
    """Aceita ISO com fuso, ou 'YYYY-MM-DD HH:MM' no fuso do canal."""
    if not value:
        return None
    value = value.strip().replace("T", " ")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ServiceError(f"Data inválida: {value!r}. Use 'AAAA-MM-DD HH:MM'.") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=env().tz)
    return dt.astimezone(timezone.utc)


def video_summary(v: dict) -> dict:
    return {
        "id": v["id"],
        "kind": v["kind"],
        "status": v["status"],
        "title": v.get("title") or v.get("topic") or "(ideia automática)",
        "topic": v.get("topic"),
        "category": v.get("category"),
        "scheduled_at": v.get("scheduled_at"),
        "scheduled_local": _local(v.get("scheduled_at")),
        "youtube_id": v.get("youtube_id"),
        "views": v.get("views"),
        "likes": v.get("likes"),
        "comments": v.get("comments"),
        "duration_s": v.get("duration_s"),
        "approved": v.get("approved"),
        "error": v.get("error"),
    }


def overview() -> dict:
    from .youtube import client as yt

    hb = db.parse_iso(db.get_state("worker_heartbeat"))
    upcoming = db.list_videos(
        status=["queued", "scripting", "scripted", "rendering", "rendered", "uploading", "scheduled"],
        limit=20, order="scheduled_at IS NULL, scheduled_at ASC",
    )
    settings = db.get_settings()
    awaiting = [
        video_summary(v) for v in db.list_videos(status="rendered", limit=50)
        if settings["approval_required"] and not v["approved"]
    ]
    return {
        "channel": db.get_state("channel"),
        "youtube_connected": yt.is_connected(),
        "youtube_client_secret": yt.has_client_secret(),
        "claude_configured": pipeline.has_claude(),
        "pexels_configured": bool(env().pexels_api_key),
        "worker_alive": bool(hb and datetime.now(timezone.utc) - hb < timedelta(minutes=3)),
        "timezone": env().channel_timezone,
        "counts": db.status_counts(),
        "upcoming": [video_summary(v) for v in upcoming],
        "awaiting_approval": awaiting,
        "settings": settings,
    }


def _validate_scenes(scenes: list[dict]) -> list[dict]:
    out = []
    for i, s in enumerate(scenes or []):
        text = (s.get("narration") or "").strip()
        if not text:
            raise ServiceError(f"Cena {i + 1} sem narração.")
        out.append({"narration": text, "visual_query": (s.get("visual_query") or "dark atmosphere").strip()})
    return out


def create_video(
    kind: str,
    topic: str | None = None,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    scenes: list[dict] | None = None,
    thumbnail_text: str | None = None,
    is_fiction: bool = False,
    scheduled_at: str | None = None,
    sources: list[str] | None = None,
    category: str | None = None,
) -> dict:
    if kind not in ("short", "long"):
        raise ServiceError("kind deve ser 'short' ou 'long'.")
    settings = db.get_settings()
    when = parse_when(scheduled_at) or best_times.next_free_slot(kind, settings["min_gap_hours"])
    fields: dict = {
        "topic": topic, "category": category, "is_fiction": is_fiction, "scheduled_at": when,
        "sources": sources or [],
    }
    if scenes:
        if not title:
            raise ServiceError("Com roteiro pronto, informe também o título.")
        desc = description or ""
        if sources:
            desc += "\n\nFontes:\n" + "\n".join(f"• {s}" for s in sources)
        if kind == "short" and "#shorts" not in (title + desc).lower():
            desc += "\n\n#shorts"
        fields.update(
            status="scripted", title=title, description=desc.strip(),
            tags=list(dict.fromkeys([*(tags or []), *settings["default_tags"]])),
            thumbnail_text=thumbnail_text or title, script={"scenes": _validate_scenes(scenes)},
        )
    elif not pipeline.has_claude():
        raise ServiceError(
            "Sem ANTHROPIC_API_KEY o servidor não escreve roteiros: envie o roteiro pronto em 'scenes'."
        )
    else:
        fields["status"] = "queued"
    v = db.create_video(kind, **fields)
    db.log(f"Vídeo criado ({kind}) para {_local(v['scheduled_at'])}", video_id=v["id"])
    return db.get_video(v["id"])  # type: ignore[return-value]


def update_video(video_id: int, **changes) -> dict:
    v = db.get_video(video_id)
    if not v:
        raise ServiceError("Vídeo não encontrado.")
    fields: dict = {}
    for k in ("title", "description", "tags", "thumbnail_text", "topic"):
        if changes.get(k) is not None:
            fields[k] = changes[k]
    if changes.get("scheduled_at"):
        if v.get("youtube_id"):
            raise ServiceError("Vídeo já enviado; altere o agendamento no YouTube Studio.")
        fields["scheduled_at"] = parse_when(changes["scheduled_at"])
    if changes.get("scenes"):
        if v.get("youtube_id"):
            raise ServiceError("Vídeo já enviado; não é possível trocar o roteiro.")
        fields["script"] = {"scenes": _validate_scenes(changes["scenes"])}
        if v["status"] in ("rendered", "failed", "scripted", "queued"):
            fields.update(status="scripted", video_path=None, thumb_path=None, error=None)
    if v.get("youtube_id") and set(fields) & {"title", "description", "tags"}:
        raise ServiceError("Vídeo já enviado; edite título/descrição no YouTube Studio.")
    return db.update_video(video_id, **fields)  # type: ignore[return-value]


def approve(video_id: int) -> dict:
    v = db.get_video(video_id)
    if not v:
        raise ServiceError("Vídeo não encontrado.")
    db.log("Aprovado para publicação", video_id=video_id)
    return db.update_video(video_id, approved=True)  # type: ignore[return-value]


def analytics() -> dict:
    tz = env().tz
    published = [v for v in db.list_videos(status="published", limit=2000) if v.get("views") is not None]
    top = sorted(published, key=lambda v: v["views"] or 0, reverse=True)[:10]
    per_hour: dict[str, list[dict]] = {}
    for kind in ("short", "long"):
        buckets: dict[int, list[int]] = defaultdict(list)
        for v in published:
            if v["kind"] == kind and v.get("scheduled_at"):
                buckets[db.parse_iso(v["scheduled_at"]).astimezone(tz).hour].append(v["views"])
        per_hour[kind] = [
            {"hour": h, "videos": len(xs), "avg_views": round(sum(xs) / len(xs)) if xs else 0}
            for h, xs in sorted(buckets.items())
        ]
    return {
        "totals": {
            "videos": len(published),
            "views": sum(v["views"] or 0 for v in published),
            "likes": sum(v["likes"] or 0 for v in published),
            "comments": sum(v["comments"] or 0 for v in published),
        },
        "top": [video_summary(v) for v in top],
        "per_hour": per_hour,
        "snapshots": db.channel_snapshots(),
    }


def upcoming_slots(kind: str, days: int = 7) -> list[str]:
    settings = db.get_settings()
    tz = env().tz
    today = datetime.now(tz).date()
    out: list[str] = []
    start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    taken = [t for t, _ in db.occupied_slots(start, start + timedelta(days=days + 1))]
    per_day = settings["shorts_per_day"] if kind == "short" else settings["long_per_day"]
    for d in range(days):
        for s in best_times.pick_slots(today + timedelta(days=d), kind, max(1, per_day), settings["min_gap_hours"], taken):
            taken.append(s)
            local = s.astimezone(tz)
            out.append(f"{_WEEKDAYS[local.weekday()]} {local.strftime('%d/%m %H:%M')}")
    return out


def env_status() -> dict:
    return {
        "anthropic_key": pipeline.has_claude(),
        "pexels_key": bool(env().pexels_api_key),
        "tts_provider": env().tts_provider,
        "model": env().claude_model,
        "data_dir": str(env().data_dir.resolve()),
        "dashboard_token": bool(env().dashboard_token or os.environ.get("DASHBOARD_TOKEN")),
    }
