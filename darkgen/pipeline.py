"""Pipeline de produção: ideia → roteiro → render → upload agendado, e o planejador diário."""

from __future__ import annotations

import logging
import os
import shutil
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from . import db
from .config import env
from .scheduling import best_times

log = logging.getLogger(__name__)

_work_lock = threading.Lock()
UPLOAD_MIN_LEAD = timedelta(minutes=15)


def has_claude() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


# ---------------------------------------------------------------- etapas

def _stage_script(video: dict, settings: dict) -> None:
    from .content import writer

    vid = video["id"]
    topic = video.get("topic")
    category = video.get("category") or ""
    angle, hook = "", ""
    is_fiction = bool(video.get("is_fiction"))
    if not topic:
        idea = writer.generate_ideas(1, video["kind"], settings["niche"], settings["content_mix"], db.recent_topics())[0]
        topic, category, angle, hook, is_fiction = idea.topic, idea.category, idea.angle, idea.hook, idea.is_fiction
        db.update_video(vid, topic=topic, category=category, is_fiction=is_fiction)
        db.log(f"Ideia gerada: {topic}", video_id=vid)

    dossier, sources = "", []
    if settings["research_with_web"] and not is_fiction:
        r = writer.research(topic, angle)
        dossier, sources = r.dossier, r.sources
        db.log(f"Pesquisa concluída ({len(sources)} fontes)", video_id=vid)

    script = writer.write_script(
        topic, video["kind"], category=category, angle=angle, hook=hook, is_fiction=is_fiction,
        dossier=dossier, long_minutes=settings["long_target_minutes"],
    )
    description = script.description
    if sources:
        description += "\n\nFontes:\n" + "\n".join(f"• {s}" for s in sources)
    if script.is_fiction:
        description = "⚠️ História de ficção / lenda.\n\n" + description
    tags = list(dict.fromkeys([*script.tags, *settings["default_tags"]]))
    db.update_video(
        vid, status="scripted", title=script.title, description=description, tags=tags,
        thumbnail_text=script.thumbnail_text, is_fiction=script.is_fiction, sources=sources,
        script={"scenes": [s.model_dump() for s in script.scenes]}, error=None,
    )
    db.log(f"Roteiro pronto: {script.title}", video_id=vid)


def _stage_render(video: dict, settings: dict) -> None:
    from .media.render import render_video

    workdir = env().videos_dir / f"{video['id']:05d}"
    result = render_video(video, settings, workdir)
    db.update_video(
        video["id"], status="rendered", video_path=str(result.video_path), thumb_path=str(result.thumb_path),
        duration_s=round(result.duration, 1), error=None,
    )
    db.log(f"Vídeo renderizado ({result.duration:.0f}s)", video_id=video["id"])


def _stage_upload(video: dict, settings: dict) -> None:
    from .youtube import client as yt

    publish_at = db.parse_iso(video.get("scheduled_at"))
    if publish_at is None or publish_at < datetime.now(timezone.utc) + UPLOAD_MIN_LEAD:
        old = publish_at
        publish_at = best_times.next_free_slot(video["kind"], settings["min_gap_hours"])
        db.update_video(video["id"], scheduled_at=publish_at)
        if old:
            db.log("Horário original perdido; reagendado para o próximo melhor horário", "warning", video["id"])
    youtube_id = yt.upload(
        Path(video["video_path"]),
        title=video["title"], description=video["description"] or "", tags=video["tags"] or [],
        category_id=settings["category_id"], publish_at=publish_at,
        synthetic_media=settings["synthetic_media_disclosure"],
        thumbnail=Path(video["thumb_path"]) if video["kind"] == "long" and video.get("thumb_path") else None,
    )
    db.update_video(video["id"], status="scheduled", youtube_id=youtube_id, scheduled_at=publish_at, error=None)
    local = publish_at.astimezone(env().tz).strftime("%d/%m %H:%M")
    db.log(f"Enviado ao YouTube ({youtube_id}), publica em {local}", video_id=video["id"])


def _fail(video: dict, exc: Exception) -> None:
    log.exception("Falha no vídeo %s", video["id"])
    db.update_video(video["id"], status="failed", error=f"{type(exc).__name__}: {exc}"[:2000],
                    attempts=(video.get("attempts") or 0) + 1)
    db.log(f"Falhou: {exc}", "error", video["id"])


# ---------------------------------------------------------------- worker

def recover_interrupted() -> None:
    """Na inicialização, devolve à fila vídeos que estavam no meio de uma etapa."""
    back = {"scripting": "queued", "rendering": "scripted", "uploading": "rendered"}
    for v in db.list_videos(status=list(back), limit=1000):
        db.update_video(v["id"], status=back[v["status"]])


def work_tick() -> bool:
    """Executa uma etapa de um vídeo. Retorna True se fez algo."""
    if not _work_lock.acquire(blocking=False):
        return False
    try:
        db.set_state("worker_heartbeat", db.iso(db.now_utc()))
        settings = db.get_settings()
        _sync_published()

        # 1) upload (prioridade: o que já está pronto)
        from .youtube import client as yt

        if yt.is_connected():
            extra = "" if not settings["approval_required"] else "AND approved = 1"
            v = db.claim_next("rendered", "uploading", extra)
            if v:
                try:
                    _stage_upload(v, settings)
                except Exception as e:  # noqa: BLE001
                    _fail(v, e)
                return True
        # 2) render
        v = db.claim_next("scripted", "rendering")
        if v:
            try:
                _stage_render(v, settings)
            except Exception as e:  # noqa: BLE001
                _fail(v, e)
            return True
        # 3) roteiro (precisa da API do Claude)
        if has_claude():
            v = db.claim_next("queued", "scripting")
            if v:
                try:
                    _stage_script(v, settings)
                except Exception as e:  # noqa: BLE001
                    _fail(v, e)
                return True
        return False
    finally:
        _work_lock.release()


def _sync_published() -> None:
    now = db.iso(db.now_utc())
    for v in db.list_videos(status="scheduled", limit=200):
        if v["scheduled_at"] and v["scheduled_at"] <= now:
            db.update_video(v["id"], status="published")
            db.log("Publicado", video_id=v["id"])


def retry(video_id: int) -> dict | None:
    v = db.get_video(video_id)
    if not v:
        return None
    if v.get("youtube_id"):
        status = "scheduled"
    elif v.get("video_path") and os.path.exists(v["video_path"]):
        status = "rendered"
    elif v.get("script"):
        status = "scripted"
    else:
        status = "queued"
    return db.update_video(video_id, status=status, error=None)


def rerender(video_id: int) -> dict | None:
    v = db.get_video(video_id)
    if not v or not v.get("script") or v.get("youtube_id"):
        return None
    return db.update_video(video_id, status="scripted", video_path=None, thumb_path=None, error=None)


def remove(video_id: int) -> None:
    workdir = env().videos_dir / f"{video_id:05d}"
    shutil.rmtree(workdir, ignore_errors=True)
    db.delete_video(video_id)


# ---------------------------------------------------------------- planejador

def plan(days_ahead: int | None = None) -> list[dict]:
    """Garante a cota diária (shorts/longos) de hoje até `days_ahead` dias à frente.

    Cria vídeos na fila com horário já definido; o worker cuida do resto.
    """
    settings = db.get_settings()
    days_ahead = settings["plan_days_ahead"] if days_ahead is None else days_ahead
    tz = env().tz
    today = datetime.now(tz).date()
    created: list[dict] = []
    quotas = {"short": settings["shorts_per_day"], "long": settings["long_per_day"]}
    for offset in range(days_ahead + 1):
        day = today + timedelta(days=offset)
        start = datetime.combine(day, time(0), tzinfo=tz)
        occupied = db.occupied_slots(start - timedelta(hours=12), start + timedelta(hours=36))
        taken = [t for t, _ in occupied]
        for kind in ("long", "short"):
            have = sum(1 for t, k in occupied if k == kind and t.astimezone(tz).date() == day)
            missing = quotas[kind] - have
            if missing <= 0:
                continue
            slots = best_times.pick_slots(day, kind, missing, settings["min_gap_hours"], taken)
            for slot in slots:
                taken.append(slot)
                v = db.create_video(kind, status="queued", scheduled_at=slot)
                created.append(v)
                db.log(f"Planejado {kind} para {slot.astimezone(tz).strftime('%d/%m %H:%M')}", video_id=v["id"])
    return created


def autopilot_tick() -> None:
    settings = db.get_settings()
    if not settings["autopilot"]:
        return
    if not has_claude():
        if not db.get_state("warned_no_claude"):
            db.log("Piloto automático precisa de ANTHROPIC_API_KEY para gerar roteiros.", "warning")
            db.set_state("warned_no_claude", True)
        return
    plan()


def refresh_stats() -> dict:
    from .youtube import client as yt

    if not yt.is_connected():
        return {"updated": 0}
    info = yt.channel_info()
    db.save_channel_snapshot(info["subscribers"], info["views"], info["videos"])
    vids = [v for v in db.list_videos(status=["scheduled", "published"], limit=2000) if v.get("youtube_id")]
    stats = yt.video_stats([v["youtube_id"] for v in vids])
    for v in vids:
        s = stats.get(v["youtube_id"])
        if s:
            db.update_video(v["id"], views=s["views"], likes=s["likes"], comments=s["comments"],
                            stats_updated_at=db.now_utc())
    db.set_state("channel", info)
    return {"updated": len(stats), "channel": info}
