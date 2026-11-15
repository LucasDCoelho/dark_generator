"""Pipeline de produção multi-canal: ideia → roteiro → render → upload agendado + planejador diário.

Um único worker atende todos os canais ativos, sempre priorizando o vídeo com horário mais próximo.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from . import db
from .config import env, lang_text, tz_of
from .scheduling import best_times

log = logging.getLogger(__name__)

_work_lock = threading.Lock()
UPLOAD_MIN_LEAD = timedelta(minutes=15)
QUOTA_BACKOFF = timedelta(hours=6)


def has_claude() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _channel(video: dict) -> dict:
    ch = db.get_channel(video["channel_id"])
    if ch is None:
        raise RuntimeError("Canal do vídeo não existe mais.")
    return ch


# ---------------------------------------------------------------- etapas

def _stage_script(video: dict, channel: dict) -> None:
    from .content import writer

    s = channel["settings"]
    vid = video["id"]
    topic = video.get("topic")
    category = video.get("category") or ""
    angle, hook = "", ""
    is_fiction = bool(video.get("is_fiction"))
    if not topic:
        idea = writer.generate_ideas(channel, 1, video["kind"], db.recent_topics(channel["id"]))[0]
        topic, category, angle, hook, is_fiction = idea.topic, idea.category, idea.angle, idea.hook, idea.is_fiction
        db.update_video(vid, topic=topic, category=category, is_fiction=is_fiction)
        db.log(f"Ideia gerada: {topic}", video_id=vid, channel_id=channel["id"])

    dossier, sources = "", []
    if s["research_with_web"] and not is_fiction:
        r = writer.research(topic, angle)
        dossier, sources = r.dossier, r.sources
        db.log(f"Pesquisa concluída ({len(sources)} fontes)", video_id=vid, channel_id=channel["id"])

    script = writer.write_script(
        channel, topic, video["kind"], category=category, angle=angle, hook=hook,
        is_fiction=is_fiction, dossier=dossier,
    )
    lt = lang_text(s["language"])
    description = script.description
    if sources:
        description += f"\n\n{lt['sources']}:\n" + "\n".join(f"• {u}" for u in sources)
    if script.is_fiction:
        description = f"{lt['fiction']}\n\n" + description
    tags = list(dict.fromkeys([*script.tags, *s["default_tags"]]))
    db.update_video(
        vid, status="scripted", title=script.title, description=description, tags=tags,
        thumbnail_text=script.thumbnail_text, is_fiction=script.is_fiction, sources=sources,
        script={"scenes": [sc.model_dump() for sc in script.scenes]}, error=None,
    )
    db.log(f"Roteiro pronto: {script.title}", video_id=vid, channel_id=channel["id"])


def _stage_render(video: dict, channel: dict) -> None:
    from .media.render import render_video

    workdir = env().videos_dir / f"{video['id']:05d}"
    result = render_video(video, channel["settings"], workdir)
    db.update_video(
        video["id"], status="rendered", video_path=str(result.video_path), thumb_path=str(result.thumb_path),
        duration_s=round(result.duration, 1), error=None,
    )
    db.log(f"Vídeo renderizado ({result.duration:.0f}s)", video_id=video["id"], channel_id=channel["id"])


def _stage_upload(video: dict, channel: dict) -> None:
    from .youtube import client as yt

    s = channel["settings"]
    publish_at = db.parse_iso(video.get("scheduled_at"))
    if publish_at is None or publish_at < datetime.now(timezone.utc) + UPLOAD_MIN_LEAD:
        old = publish_at
        publish_at = best_times.next_free_slot(channel, video["kind"])
        db.update_video(video["id"], scheduled_at=publish_at)
        if old:
            db.log("Horário original perdido; reagendado para o próximo melhor horário", "warning",
                   video["id"], channel["id"])
    youtube_id = yt.upload(
        channel["id"], Path(video["video_path"]),
        title=video["title"], description=video["description"] or "", tags=video["tags"] or [],
        category_id=s["category_id"], language=s["language"], publish_at=publish_at,
        synthetic_media=s["synthetic_media_disclosure"],
        thumbnail=Path(video["thumb_path"]) if video["kind"] == "long" and video.get("thumb_path") else None,
    )
    db.update_video(video["id"], status="scheduled", youtube_id=youtube_id, scheduled_at=publish_at,
                    uploaded_at=db.now_utc(), error=None)
    local = publish_at.astimezone(tz_of(channel)).strftime("%d/%m %H:%M")
    db.log(f"Enviado ao YouTube ({youtube_id}), publica em {local}", video_id=video["id"], channel_id=channel["id"])


def _fail(video: dict, exc: Exception) -> None:
    log.exception("Falha no vídeo %s", video["id"])
    db.update_video(video["id"], status="failed", error=f"{type(exc).__name__}: {exc}"[:2000],
                    attempts=(video.get("attempts") or 0) + 1)
    db.log(f"Falhou: {exc}", "error", video["id"], video.get("channel_id"))


# ---------------------------------------------------------------- cota do YouTube (por projeto)

def quota_status() -> dict[str, dict]:
    """Uploads nas últimas 24h por projeto do Google Cloud e se está bloqueado."""
    from .youtube import client as yt

    groups: dict[str, list[int]] = {}
    for ch in db.list_channels():
        groups.setdefault(yt.quota_key(ch["id"]), []).append(ch["id"])
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    limit = env().youtube_uploads_per_project_per_day
    out = {}
    for key, ids in groups.items():
        blocked_until = db.parse_iso(db.get_state(f"quota_block:{key}"))
        used = db.uploads_since(ids, since)
        out[key] = {
            "channels": ids, "uploads_24h": used, "limit": limit,
            "blocked_until": db.iso(blocked_until) if blocked_until and blocked_until > db.now_utc() else None,
        }
    return out


def _upload_allowed(channel_id: int, quota: dict[str, dict]) -> bool:
    from .youtube import client as yt

    q = quota.get(yt.quota_key(channel_id))
    return bool(q) and q["blocked_until"] is None and q["uploads_24h"] < q["limit"]


# ---------------------------------------------------------------- worker

def recover_interrupted() -> None:
    """Na inicialização, devolve à fila vídeos que estavam no meio de uma etapa."""
    back = {"scripting": "queued", "rendering": "scripted", "uploading": "rendered"}
    for v in db.list_videos(status=list(back), limit=5000):
        db.update_video(v["id"], status=back[v["status"]])


def _next_upload() -> tuple[dict, dict] | None:
    from .youtube import client as yt

    quota = quota_status()
    channels = {c["id"]: c for c in db.list_channels(active_only=True)}
    connected: dict[int, bool] = {}
    for v in db.list_videos(status="rendered", active_channels_only=True, limit=500,
                            order="scheduled_at IS NULL, scheduled_at ASC"):
        ch = channels.get(v["channel_id"])
        if not ch or (ch["settings"]["approval_required"] and not v["approved"]):
            continue
        if ch["id"] not in connected:
            connected[ch["id"]] = yt.is_connected(ch["id"])
        if connected[ch["id"]] and _upload_allowed(ch["id"], quota):
            return v, ch
    return None


def _next_of(status: str) -> dict | None:
    rows = db.list_videos(status=status, active_channels_only=True, limit=1,
                          order="scheduled_at IS NULL, scheduled_at ASC, id ASC")
    return rows[0] if rows else None


def work_tick() -> bool:
    """Executa uma etapa de um vídeo (de qualquer canal ativo). Retorna True se fez algo."""
    if not _work_lock.acquire(blocking=False):
        return False
    try:
        db.set_state("worker_heartbeat", db.iso(db.now_utc()))
        _sync_published()

        # 1) upload — o que já está pronto e tem o horário mais próximo
        nxt = _next_upload()
        if nxt and db.transition(nxt[0]["id"], "rendered", "uploading"):
            v, ch = nxt
            from .youtube import client as yt

            try:
                _stage_upload(db.get_video(v["id"]), ch)
            except yt.QuotaExceeded:
                key = yt.quota_key(ch["id"])
                db.set_state(f"quota_block:{key}", db.iso(db.now_utc() + QUOTA_BACKOFF))
                db.update_video(v["id"], status="rendered")
                db.log("Cota diária do YouTube esgotada para este projeto; nova tentativa em 6h", "warning",
                       v["id"], ch["id"])
            except Exception as e:  # noqa: BLE001
                _fail(v, e)
            return True

        # 2) render
        v = _next_of("scripted")
        if v and db.transition(v["id"], "scripted", "rendering"):
            try:
                _stage_render(db.get_video(v["id"]), _channel(v))
            except Exception as e:  # noqa: BLE001
                _fail(v, e)
            return True

        # 3) roteiro (precisa da API do Claude)
        if has_claude():
            v = _next_of("queued")
            if v and db.transition(v["id"], "queued", "scripting"):
                try:
                    _stage_script(db.get_video(v["id"]), _channel(v))
                except Exception as e:  # noqa: BLE001
                    _fail(v, e)
                return True
        return False
    finally:
        _work_lock.release()


def _sync_published() -> None:
    now = db.iso(db.now_utc())
    for v in db.list_videos(status="scheduled", limit=500):
        if v["scheduled_at"] and v["scheduled_at"] <= now:
            db.update_video(v["id"], status="published")
            db.log("Publicado", video_id=v["id"], channel_id=v["channel_id"])


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


def retry_failed(channel_id: int | None = None) -> list[dict]:
    return [retry(v["id"]) for v in db.list_videos(status="failed", channel_id=channel_id, limit=1000)]


def rerender(video_id: int) -> dict | None:
    v = db.get_video(video_id)
    if not v or not v.get("script") or v.get("youtube_id"):
        return None
    return db.update_video(video_id, status="scripted", video_path=None, thumb_path=None, error=None)


def remove(video_id: int) -> None:
    shutil.rmtree(env().videos_dir / f"{video_id:05d}", ignore_errors=True)
    db.delete_video(video_id)


def cleanup_files(channel_id: int | None = None, older_than_days: int = 7) -> int:
    """Apaga os arquivos de vídeos já publicados há mais de N dias (o YouTube já tem a cópia)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    n = 0
    for v in db.list_videos(status="published", channel_id=channel_id, limit=10000):
        when = db.parse_iso(v.get("scheduled_at"))
        if v.get("video_path") and when and when < cutoff:
            shutil.rmtree(env().videos_dir / f"{v['id']:05d}", ignore_errors=True)
            db.update_video(v["id"], video_path=None, thumb_path=None)
            n += 1
    return n


# ---------------------------------------------------------------- planejador

def plan_channel(channel: dict, days_ahead: int | None = None) -> list[dict]:
    """Garante a cota diária do canal de hoje até `days_ahead` dias à frente (fuso do canal)."""
    s = channel["settings"]
    days_ahead = s["plan_days_ahead"] if days_ahead is None else days_ahead
    tz = tz_of(channel)
    today = datetime.now(tz).date()
    created: list[dict] = []
    quotas = {"short": s["shorts_per_day"], "long": s["long_per_day"]}
    perf = {k: best_times.performance_by_hour(channel, k) for k in quotas}
    for offset in range(days_ahead + 1):
        day = today + timedelta(days=offset)
        start = datetime.combine(day, time(0), tzinfo=tz)
        occupied = db.occupied_slots(channel["id"], start - timedelta(hours=12), start + timedelta(hours=36))
        taken = [t for t, _ in occupied]
        for kind in ("long", "short"):
            have = sum(1 for t, k in occupied if k == kind and t.astimezone(tz).date() == day)
            missing = quotas[kind] - have
            if missing <= 0:
                continue
            for slot in best_times.pick_slots(channel, day, kind, missing, taken, perf[kind]):
                taken.append(slot)
                v = db.create_video(channel["id"], kind, status="queued", scheduled_at=slot)
                created.append(v)
                db.log(f"Planejado {kind} para {slot.astimezone(tz).strftime('%d/%m %H:%M')}",
                       video_id=v["id"], channel_id=channel["id"])
    return created


def plan(channel_id: int | None = None, days_ahead: int | None = None) -> list[dict]:
    channels = [db.get_channel(channel_id)] if channel_id else db.list_channels(active_only=True)
    out: list[dict] = []
    for ch in channels:
        if ch:
            out += plan_channel(ch, days_ahead)
    return out


def autopilot_tick() -> None:
    if not has_claude():
        if not db.get_state("warned_no_claude"):
            db.log("Piloto automático precisa de ANTHROPIC_API_KEY para gerar roteiros.", "warning")
            db.set_state("warned_no_claude", True)
        return
    for ch in db.list_channels(active_only=True):
        if ch["settings"]["autopilot"]:
            try:
                plan_channel(ch)
            except Exception:  # noqa: BLE001
                log.exception("Planejamento falhou no canal %s", ch["id"])


# ---------------------------------------------------------------- estatísticas

def refresh_channel_stats(channel: dict) -> dict:
    from .youtube import client as yt

    info = yt.channel_info(channel["id"])
    db.save_channel_stats(channel["id"], info["subscribers"], info["views"], info["videos"])
    db.update_channel(channel["id"], youtube_channel_id=info["id"], youtube_title=info["title"],
                      youtube_thumb=info["thumbnail"], subscribers=info["subscribers"], views=info["views"],
                      video_count=info["videos"], stats_updated_at=db.now_utc())
    vids = [v for v in db.list_videos(status=["scheduled", "published"], channel_id=channel["id"], limit=5000)
            if v.get("youtube_id")]
    stats = yt.video_stats(channel["id"], [v["youtube_id"] for v in vids])
    for v in vids:
        st = stats.get(v["youtube_id"])
        if st:
            db.update_video(v["id"], views=st["views"], likes=st["likes"], comments=st["comments"],
                            stats_updated_at=db.now_utc())
            if st.get("rejection"):
                db.log(f"YouTube rejeitou o vídeo: {st['rejection']}", "error", v["id"], channel["id"])
    missing = [v for v in vids if v["youtube_id"] not in stats]
    for v in missing:
        db.log("Vídeo não encontrado no YouTube (removido?)", "warning", v["id"], channel["id"])
    return {"channel_id": channel["id"], "updated": len(stats), "subscribers": info["subscribers"]}


def refresh_stats(channel_id: int | None = None) -> list[dict]:
    from .youtube import client as yt

    channels = [db.get_channel(channel_id)] if channel_id else db.list_channels()
    out = []
    for ch in channels:
        if not ch or not yt.has_token(ch["id"]):
            continue
        try:
            out.append(refresh_channel_stats(ch))
        except Exception as e:  # noqa: BLE001
            db.log(f"Falha ao atualizar estatísticas: {e}", "warning", channel_id=ch["id"])
            out.append({"channel_id": ch["id"], "error": str(e)})
    return out
