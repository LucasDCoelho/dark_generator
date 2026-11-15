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


# ---------------------------------------------------------------- textos

def compose_description(channel: dict, description: str, sources: list[str] | None, is_fiction: bool,
                        kind: str, title: str = "") -> str:
    lt = lang_text(channel["settings"]["language"])
    desc = (description or "").strip()
    if sources:
        desc += f"\n\n{lt['sources']}:\n" + "\n".join(f"• {u}" for u in sources)
    if is_fiction and lt["fiction"] not in desc:
        desc = f"{lt['fiction']}\n\n{desc}"
    if kind == "short" and "#shorts" not in (title + desc).lower():
        desc += "\n\n#shorts"
    return desc.strip()


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
    description = compose_description(channel, script.description, sources, script.is_fiction, video["kind"], script.title)
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
    extras: dict = {"labels": lang_text(channel["settings"]["language"])}
    if video.get("series_id"):
        srs = db.get_series(video["series_id"])
        if srs:
            extras.update(episode=video.get("episode"), parts=srs["parts"], bible=srs.get("bible"))
    result = render_video(video, channel["settings"], workdir, extras)
    db.update_video(
        video["id"], status=after_render_status(video, channel), video_path=str(result.video_path),
        thumb_path=str(result.thumb_path), duration_s=round(result.duration, 1), error=None,
    )
    db.log(f"Vídeo renderizado ({result.duration:.0f}s)", video_id=video["id"], channel_id=channel["id"])


def after_render_status(video: dict, channel: dict) -> str:
    """Modo manual: vai direto para 'Pronto para postar' (se não precisar de aprovação)."""
    s = channel["settings"]
    if s["publish_mode"] == "manual" and (not s["approval_required"] or video.get("approved")):
        return "ready"
    return "rendered"


def _stage_upload(video: dict, channel: dict) -> None:
    from .youtube import client as yt

    s = channel["settings"]
    lt = lang_text(s["language"])
    prev = db.previous_episode(video)
    prev_at = db.parse_iso(prev["scheduled_at"]) if prev else None
    gap = timedelta(hours=s["min_gap_hours"])
    publish_at = db.parse_iso(video.get("scheduled_at"))
    too_soon = publish_at is None or publish_at < datetime.now(timezone.utc) + UPLOAD_MIN_LEAD
    out_of_order = bool(prev_at and publish_at and publish_at <= prev_at + gap)
    if too_soon or out_of_order:
        old = publish_at
        publish_at = best_times.next_free_slot(channel, video["kind"], not_before=prev_at + gap if prev_at else None)
        db.update_video(video["id"], scheduled_at=publish_at)
        if old:
            db.log("Horário reagendado para manter a ordem da série" if out_of_order else
                   "Horário original perdido; reagendado para o próximo melhor horário", "warning",
                   video["id"], channel["id"])
    description = video["description"] or ""
    series = db.get_series(video["series_id"]) if video.get("series_id") else None
    if prev and prev.get("youtube_id"):
        description = f"▶ {lt['previous']}: https://youtu.be/{prev['youtube_id']}\n\n{description}"
    if series and series.get("playlist_id"):
        description += f"\n\n📺 {lt['playlist']}: https://www.youtube.com/playlist?list={series['playlist_id']}"
    youtube_id = yt.upload(
        channel["id"], Path(video["video_path"]),
        title=video["title"], description=description, tags=video["tags"] or [],
        category_id=s["category_id"], language=s["language"], publish_at=publish_at,
        synthetic_media=s["synthetic_media_disclosure"],
        thumbnail=Path(video["thumb_path"]) if video["kind"] == "long" and video.get("thumb_path") else None,
    )
    db.update_video(video["id"], status="scheduled", youtube_id=youtube_id, scheduled_at=publish_at,
                    uploaded_at=db.now_utc(), error=None)
    if series and s["series_playlists"]:
        _add_to_series_playlist(series, channel, youtube_id, video.get("episode"))
    local = publish_at.astimezone(tz_of(channel)).strftime("%d/%m %H:%M")
    db.log(f"Enviado ao YouTube ({youtube_id}), publica em {local}", video_id=video["id"], channel_id=channel["id"])


def _add_to_series_playlist(series: dict, channel: dict, youtube_id: str, episode: int | None) -> None:
    from .youtube import client as yt

    lt = lang_text(channel["settings"]["language"])
    try:
        playlist_id = series.get("playlist_id")
        if not playlist_id:
            playlist_id = yt.create_playlist(channel["id"], f"{lt['series']}: {series['title']}",
                                             series.get("logline") or "", channel["settings"]["language"])
            db.update_series(series["id"], playlist_id=playlist_id)
            db.log(f"Playlist da série criada: {series['title']}", channel_id=channel["id"])
        yt.add_to_playlist(channel["id"], playlist_id, youtube_id, (episode - 1) if episode else None)
    except yt.MissingScope:
        if not db.get_state(f"warned_playlist_scope:{channel['id']}"):
            db.log("Para criar playlists das séries, reconecte o YouTube deste canal (permissão nova).",
                   "warning", channel_id=channel["id"])
            db.set_state(f"warned_playlist_scope:{channel['id']}", True)
    except Exception as e:  # noqa: BLE001 - playlist é complemento, não bloqueia a publicação
        db.log(f"Não foi possível atualizar a playlist da série: {e}", "warning", channel_id=channel["id"])


def _fail(video: dict, exc: Exception) -> None:
    log.exception("Falha no vídeo %s", video["id"])
    db.update_video(video["id"], status="failed", error=f"{type(exc).__name__}: {exc}"[:2000],
                    attempts=(video.get("attempts") or 0) + 1)
    db.log(f"Falhou: {exc}", "error", video["id"], video.get("channel_id"))


# ---------------------------------------------------------------- séries

def create_series_plan(
    channel: dict,
    parts: int,
    kind: str,
    topic: str | None = None,
    angle: str | None = None,
    is_fiction: bool = False,
    start_day=None,
    fmt: str | None = None,
) -> dict:
    """Cria a série e já reserva os horários dos episódios (um por dia, em dias seguidos).
    Os roteiros são escritos depois pelo worker (etapa de série). fmt: narrated | novela."""
    fmt = fmt or channel["settings"].get("series_format", "narrated")
    if fmt not in ("narrated", "novela"):
        raise ValueError("Formato deve ser 'narrated' ou 'novela'.")
    if fmt == "novela":
        is_fiction = True
    if not 2 <= parts <= 10:
        raise ValueError("Uma série deve ter de 2 a 10 partes.")
    if kind not in ("short", "long"):
        raise ValueError("kind deve ser 'short' ou 'long'.")
    tz = tz_of(channel)
    start_day = start_day or datetime.now(tz).date()
    slots = best_times.series_slots(channel, kind, parts, start_day)
    series = db.create_series(channel["id"], kind, parts, status="queued", topic=topic, angle=angle,
                              is_fiction=is_fiction, title=topic, format=fmt)
    for n, slot in enumerate(slots, start=1):
        db.create_video(channel["id"], kind, status="queued", series_id=series["id"], episode=n,
                        scheduled_at=slot, topic=topic, is_fiction=is_fiction)
    first = slots[0].astimezone(tz).strftime("%d/%m %H:%M")
    db.log(f"Série planejada ({parts} partes, começa {first})", channel_id=channel["id"])
    return series


def fill_series(series: dict, channel: dict, title: str, logline: str, episodes: list[dict],
                is_fiction: bool, sources: list[str] | None = None, characters: list[dict] | None = None) -> None:
    """Grava os roteiros nos episódios reservados e marca a série como pronta."""
    s = channel["settings"]
    eps = {v["episode"]: v for v in db.episodes(series["id"])}
    for n, ep in enumerate(episodes, start=1):
        v = eps.get(n)
        if not v or v["status"] not in ("queued", "scripting", "failed"):
            continue
        desc = compose_description(channel, ep.get("description") or "", sources, is_fiction, series["kind"], ep["title"])
        db.update_video(
            v["id"], status="scripted", title=ep["title"], description=desc,
            tags=list(dict.fromkeys([*(ep.get("tags") or []), *s["default_tags"]])),
            thumbnail_text=ep.get("thumbnail_text") or ep["title"], is_fiction=is_fiction,
            sources=sources or [], script={"scenes": ep["scenes"]}, error=None,
        )
    bible = None
    if characters:
        from .media.voices import assign_voices

        bible = {"characters": characters, "voices": assign_voices(characters, s)}
    db.update_series(series["id"], status="ready", title=title, logline=logline, is_fiction=is_fiction,
                     sources=sources or [], error=None, bible=bible)
    db.log(f"Série pronta: {title} ({len(episodes)} partes)", channel_id=channel["id"])


def _stage_series(series: dict, channel: dict) -> None:
    from .content import writer

    topic, angle, is_fiction = series.get("topic"), series.get("angle") or "", bool(series["is_fiction"])
    if not topic:
        idea = writer.generate_ideas(channel, 1, series["kind"], db.recent_topics(channel["id"]),
                                     series_parts=series["parts"])[0]
        topic, angle, is_fiction = idea.topic, idea.angle, idea.is_fiction
        db.update_series(series["id"], topic=topic, angle=angle, is_fiction=is_fiction)
    if series.get("format") == "novela":
        novela = writer.write_novela(channel, topic, series["parts"], series["kind"], angle=angle)
        fill_series(series, channel, novela.series_title, novela.logline,
                    [e.model_dump() for e in novela.episodes], True,
                    characters=[c.model_dump() for c in novela.characters])
        return
    dossier, sources = "", []
    if channel["settings"]["research_with_web"] and not is_fiction:
        r = writer.research(topic, angle)
        dossier, sources = r.dossier, r.sources
    script = writer.write_series(channel, topic, series["parts"], series["kind"], angle=angle,
                                 is_fiction=is_fiction, dossier=dossier)
    fill_series(series, channel, script.series_title, script.logline,
                [e.model_dump() for e in script.episodes], script.is_fiction, sources)


def _fail_series(series: dict, exc: Exception) -> None:
    log.exception("Falha na série %s", series["id"])
    db.update_series(series["id"], status="failed", error=f"{type(exc).__name__}: {exc}"[:2000])
    for v in db.episodes(series["id"]):
        if v["status"] in ("queued", "scripting"):
            db.update_video(v["id"], status="failed", error="A série não pôde ser escrita.")
    db.log(f"Série falhou: {exc}", "error", channel_id=series["channel_id"])


def retry_series(series_id: int) -> dict | None:
    s = db.get_series(series_id)
    if not s:
        return None
    if s["status"] == "failed":
        for v in db.episodes(series_id):
            if v["status"] == "failed" and not v.get("script"):
                db.update_video(v["id"], status="queued", error=None)
        return db.update_series(series_id, status="queued", error=None)
    for v in db.episodes(series_id):
        if v["status"] == "failed":
            retry(v["id"])
    return db.get_series(series_id)


def cancel_series(series_id: int) -> dict | None:
    """Cancela a série: remove da fila os episódios que ainda não foram enviados ao YouTube."""
    s = db.get_series(series_id)
    if not s:
        return None
    removed = 0
    for v in db.episodes(series_id):
        if not v.get("youtube_id"):
            remove(v["id"])
            removed += 1
    db.log(f"Série cancelada ({removed} episódio(s) removido(s) da fila)", "warning", channel_id=s["channel_id"])
    return db.update_series(series_id, status="cancelled")


def _sync_series() -> None:
    for s in db.list_series(status=["ready", "writing"], limit=500):
        eps = db.episodes(s["id"])
        if eps and all(v["status"] == "published" for v in eps):
            db.update_series(s["id"], status="done")
            db.log(f"Série concluída: {s['title']}", channel_id=s["channel_id"])


def _series_blocked(video: dict) -> bool:
    """Episódio só sobe depois que o anterior já está no YouTube (garante a ordem)."""
    prev = db.previous_episode(video)
    return bool(prev and not prev.get("youtube_id"))


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
    for s in db.list_series(status="writing", limit=500):
        db.update_series(s["id"], status="queued")


def _next_upload() -> tuple[dict, dict] | None:
    from .youtube import client as yt

    quota = quota_status()
    channels = {c["id"]: c for c in db.list_channels(active_only=True)}
    connected: dict[int, bool] = {}
    for v in db.list_videos(status="rendered", active_channels_only=True, limit=500,
                            order="scheduled_at IS NULL, scheduled_at ASC"):
        ch = channels.get(v["channel_id"])
        if not ch or ch["settings"]["publish_mode"] != "youtube_api":
            continue
        if ch["settings"]["approval_required"] and not v["approved"]:
            continue
        if ch["id"] not in connected:
            connected[ch["id"]] = yt.is_connected(ch["id"])
        if connected[ch["id"]] and _upload_allowed(ch["id"], quota) and not _series_blocked(v):
            return v, ch
    return None


def _next_of(status: str, exclude_series: bool = False) -> dict | None:
    rows = db.list_videos(status=status, active_channels_only=True, limit=1, exclude_series=exclude_series,
                          order="scheduled_at IS NULL, scheduled_at ASC, id ASC")
    return rows[0] if rows else None


def work_tick() -> bool:
    """Executa uma etapa de um vídeo (de qualquer canal ativo). Retorna True se fez algo."""
    if not _work_lock.acquire(blocking=False):
        return False
    try:
        db.set_state("worker_heartbeat", db.iso(db.now_utc()))
        _sync_published()
        _sync_series()

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

        # 3) séries: escreve todos os episódios de uma vez (precisa da API do Claude)
        if has_claude():
            active = {c["id"] for c in db.list_channels(active_only=True)}
            for srs in reversed(db.list_series(status="queued", limit=50)):  # mais antiga primeiro
                if srs["channel_id"] in active and db.transition_series(srs["id"], "queued", "writing"):
                    try:
                        _stage_series(db.get_series(srs["id"]), db.get_channel(srs["channel_id"]))
                    except Exception as e:  # noqa: BLE001
                        _fail_series(srs, e)
                    return True

        # 4) roteiro de vídeos avulsos (precisa da API do Claude)
        if has_claude():
            v = _next_of("queued", exclude_series=True)
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
        ch = db.get_channel(v["channel_id"])
        status = after_render_status(v, ch) if ch else "rendered"
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


def maybe_start_series(channel: dict) -> dict | None:
    """Com auto_series ligado, começa uma série nova a cada `series_every_days` dias.
    Ela começa depois do horizonte já planejado, então os episódios contam na cota diária."""
    s = channel["settings"]
    if not s["auto_series"]:
        return None
    if db.list_series(channel_id=channel["id"], status=["queued", "writing"], limit=1):
        return None
    last = db.last_series_created_at(channel["id"])
    if last and datetime.now(timezone.utc) - last < timedelta(days=s["series_every_days"]):
        return None
    start = datetime.now(tz_of(channel)).date() + timedelta(days=s["plan_days_ahead"] + 1)
    try:
        return create_series_plan(channel, s["series_parts"], s["series_kind"], start_day=start,
                                  fmt=s["series_format"])
    except ValueError as e:
        db.log(f"Série automática não criada: {e}", "warning", channel_id=channel["id"])
        return None


def autopilot_tick() -> None:
    if not has_claude():
        if not db.get_state("warned_no_claude"):
            db.log("Piloto automático precisa de ANTHROPIC_API_KEY para gerar roteiros.", "warning")
            db.set_state("warned_no_claude", True)
        return
    for ch in db.list_channels(active_only=True):
        if ch["settings"]["autopilot"]:
            try:
                maybe_start_series(ch)
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
