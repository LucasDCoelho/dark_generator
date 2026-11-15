"""Camada de serviço compartilhada pelo painel web (REST) e pelo servidor MCP."""

from __future__ import annotations

import shutil
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from urllib.parse import quote

from . import db, pipeline
from .config import NICHE_PRESETS, coerce_settings, env, tz_of
from .scheduling import best_times

_WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


class ServiceError(ValueError):
    pass


def require_channel(channel_id: int) -> dict:
    ch = db.get_channel(channel_id)
    if not ch:
        raise ServiceError(f"Canal {channel_id} não encontrado. Use list_channels para ver os IDs.")
    return ch


def _local(channel: dict, dt_iso: str | None) -> str | None:
    dt = db.parse_iso(dt_iso)
    return dt.astimezone(tz_of(channel)).strftime("%Y-%m-%d %H:%M") if dt else None


def parse_when(channel: dict, value: str | None) -> datetime | None:
    """Aceita ISO com fuso, ou 'YYYY-MM-DD HH:MM' no fuso do canal."""
    if not value:
        return None
    value = value.strip().replace("T", " ")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ServiceError(f"Data inválida: {value!r}. Use 'AAAA-MM-DD HH:MM'.") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_of(channel))
    return dt.astimezone(timezone.utc)


def video_summary(v: dict, channel: dict | None = None) -> dict:
    channel = channel or db.get_channel(v["channel_id"])
    return {
        "id": v["id"],
        "channel_id": v["channel_id"],
        "channel_name": channel["name"] if channel else None,
        "kind": v["kind"],
        "status": v["status"],
        "title": v.get("title") or v.get("topic") or "(ideia automática)",
        "topic": v.get("topic"),
        "category": v.get("category"),
        "scheduled_at": v.get("scheduled_at"),
        "scheduled_local": _local(channel, v.get("scheduled_at")) if channel else None,
        "youtube_id": v.get("youtube_id"),
        "views": v.get("views"),
        "likes": v.get("likes"),
        "comments": v.get("comments"),
        "duration_s": v.get("duration_s"),
        "approved": v.get("approved"),
        "error": v.get("error"),
        "series_id": v.get("series_id"),
        "episode": v.get("episode"),
    }


def summaries(videos: list[dict]) -> list[dict]:
    cache: dict[int, dict | None] = {}
    out = []
    for v in videos:
        if v["channel_id"] not in cache:
            cache[v["channel_id"]] = db.get_channel(v["channel_id"])
        out.append(video_summary(v, cache[v["channel_id"]]))
    return out


# ---------------------------------------------------------------- canais

def list_presets() -> list[dict]:
    return [
        {"key": k, "label": p["label"], "description": p["description"],
         "language": p["settings"].get("language", "pt-BR"), "settings": p["settings"]}
        for k, p in NICHE_PRESETS.items()
    ]


def create_channel(name: str, preset: str | None = None, settings: dict | None = None,
                   notes: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ServiceError("Dê um nome ao canal.")
    base: dict = {}
    if preset:
        if preset not in NICHE_PRESETS:
            raise ServiceError(f"Modelo desconhecido: {preset}. Opções: {', '.join(NICHE_PRESETS)}")
        base = dict(NICHE_PRESETS[preset]["settings"])
    try:
        ch = db.create_channel(name, {**base, **(settings or {})}, preset=preset, notes=notes)
    except ValueError as e:
        raise ServiceError(str(e)) from e
    db.log(f"Canal criado: {name}" + (f" (modelo {preset})" if preset else ""), channel_id=ch["id"])
    return ch


def update_channel(channel_id: int, name: str | None = None, active: bool | None = None,
                   notes: str | None = None, settings: dict | None = None) -> dict:
    require_channel(channel_id)
    fields: dict = {}
    if name is not None and name.strip():
        fields["name"] = name.strip()
    if active is not None:
        fields["active"] = bool(active)
    if notes is not None:
        fields["notes"] = notes
    try:
        ch = db.update_channel(channel_id, settings=settings, **fields)
    except ValueError as e:
        raise ServiceError(str(e)) from e
    if active is not None:
        db.log("Canal reativado" if active else "Canal pausado", "info" if active else "warning", channel_id=channel_id)
    return ch  # type: ignore[return-value]


def clone_channel(channel_id: int, name: str) -> dict:
    src = require_channel(channel_id)
    return create_channel(name, settings=src["settings"], notes=f"Clonado de {src['name']}")


def delete_channel(channel_id: int) -> None:
    require_channel(channel_id)
    for v in db.list_videos(channel_id=channel_id, limit=100000):
        shutil.rmtree(env().videos_dir / f"{v['id']:05d}", ignore_errors=True)
    db.delete_channel(channel_id)
    shutil.rmtree(env().channels_dir / str(channel_id), ignore_errors=True)


def connect_url(channel_id: int) -> str:
    """Link para o usuário abrir no navegador e autorizar o YouTube deste canal."""
    require_channel(channel_id)
    url = f"{env().base_url()}/api/channels/{channel_id}/youtube/connect?redirect=1"
    if env().dashboard_token:
        url += f"&token={quote(env().dashboard_token)}"
    return url


def channel_public(ch: dict) -> dict:
    """Dados do canal + estado de conexão (sem segredos)."""
    from .youtube import client as yt

    return {
        **ch,
        "youtube_connected": yt.has_token(ch["id"]),
        "own_client_secret": yt.has_own_secret(ch["id"]),
        "quota_project": yt.quota_key(ch["id"]),
    }


# ---------------------------------------------------------------- saúde / manutenção

def channel_health(channel_id: int, check_token: bool = True) -> dict:
    """Diagnóstico do canal: problemas encontrados e a ação sugerida para cada um."""
    from .youtube import client as yt

    ch = require_channel(channel_id)
    s = ch["settings"]
    tz = tz_of(ch)
    issues: list[dict] = []

    def add(severity: str, code: str, message: str, action: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message, "action": action})

    if not ch["active"]:
        add("info", "paused", "Canal pausado: nada é produzido nem enviado.", "update_channel(active=true)")
    if not yt.has_token(channel_id):
        add("critical", "not_connected", "YouTube não conectado.", "connect_youtube → abrir o link")
    elif check_token and not yt.is_connected(channel_id):
        add("critical", "token_invalid", "Token do YouTube expirou ou foi revogado.", "connect_youtube → reconectar")
    if not yt.client_secret_for(channel_id):
        add("critical", "no_client_secret", "Nenhum client_secret.json (global ou do canal).", "enviar pelo painel")

    counts = db.status_counts(channel_id)
    if counts["failed"]:
        add("warning", "failed_videos", f"{counts['failed']} vídeo(s) com falha.", "retry_failed(channel_id)")
    if s["autopilot"] and not pipeline.has_claude():
        add("warning", "no_claude", "Piloto automático ligado, mas sem ANTHROPIC_API_KEY no servidor.",
            "configurar a chave ou criar vídeos com roteiro via create_video")

    awaiting = [v for v in db.list_videos(status="rendered", channel_id=channel_id, limit=500) if not v["approved"]]
    if s["approval_required"] and awaiting:
        add("warning", "awaiting_approval", f"{len(awaiting)} vídeo(s) aguardando aprovação.", "approve_video(video_id)")

    now = datetime.now(timezone.utc)
    late = [v for v in db.list_videos(status=["queued", "scripted", "rendered"], channel_id=channel_id, limit=500)
            if v.get("scheduled_at") and db.parse_iso(v["scheduled_at"]) < now]
    if late:
        add("warning", "late_videos", f"{len(late)} vídeo(s) passaram do horário sem ser enviados.",
            "serão reagendados automaticamente no upload; verifique worker e conexão")

    # agenda dos próximos 2 dias
    today = datetime.now(tz).date()
    start = datetime.combine(today, time(0), tzinfo=tz)
    upcoming = db.occupied_slots(channel_id, start, start + timedelta(days=2))
    expected = 2 * (s["shorts_per_day"] + s["long_per_day"])
    if ch["active"] and expected and len(upcoming) < expected // 2:
        sev = "info" if s["autopilot"] and pipeline.has_claude() else "warning"
        add(sev, "empty_schedule", f"Agenda dos próximos 2 dias com {len(upcoming)} de ~{expected} vídeos.",
            "plan_schedule(channel_id) ou create_video")

    last = db.last_published_at(channel_id)
    if last and now - last > timedelta(days=3) and ch["active"]:
        add("warning", "inactive", f"Último vídeo publicado há {(now - last).days} dias.", "plan_schedule(channel_id)")

    quota = pipeline.quota_status().get(yt.quota_key(channel_id))
    if quota and quota["blocked_until"]:
        add("warning", "quota_blocked", f"Cota do YouTube esgotada até {quota['blocked_until']}.",
            "usar client_secret próprio do canal (outro projeto do Google Cloud)")

    # queda de desempenho: últimos 5 publicados vs 5 anteriores
    pub = [v for v in db.list_videos(status="published", channel_id=channel_id, limit=10,
                                     order="scheduled_at DESC") if v.get("views") is not None]
    if len(pub) == 10:
        recent = sum(v["views"] for v in pub[:5]) / 5
        before = sum(v["views"] for v in pub[5:]) / 5
        if before > 0 and recent < before * 0.5:
            add("info", "views_drop", f"Views médias caíram {100 - recent * 100 / before:.0f}% nos últimos 5 vídeos.",
                "revisar nicho/títulos com analytics(channel_id)")

    for srs in db.list_series(channel_id=channel_id, status=["failed", "ready", "writing", "queued"], limit=50):
        if srs["status"] == "failed":
            add("warning", "series_failed", f"Série '{srs['title'] or srs['topic'] or srs['id']}' falhou ao ser escrita.",
                f"retry_series({srs['id']})")
            continue
        eps = db.episodes(srs["id"])
        stuck = next((v for v in eps if v["status"] == "failed"), None)
        if stuck:
            add("warning", "series_blocked",
                f"Série '{srs['title'] or srs['id']}': parte {stuck['episode']} falhou e segura as próximas.",
                f"retry_series({srs['id']}) ou retry_video({stuck['id']})")

    errors_24h = db.recent_activity(limit=20, channel_id=channel_id, level="error", since=now - timedelta(hours=24))
    rank = {"critical": 2, "warning": 1, "info": 0}
    worst = max((rank[i["severity"]] for i in issues), default=-1)
    return {
        "channel_id": channel_id,
        "name": ch["name"],
        "status": "critical" if worst == 2 else "warning" if worst == 1 else "ok",
        "issues": sorted(issues, key=lambda i: -rank[i["severity"]]),
        "counts": counts,
        "recent_errors": [e["message"] for e in errors_24h[:5]],
    }


# ---------------------------------------------------------------- visão geral

def _worker_alive() -> bool:
    hb = db.parse_iso(db.get_state("worker_heartbeat"))
    return bool(hb and datetime.now(timezone.utc) - hb < timedelta(minutes=3))


def portfolio() -> dict:
    """Resumo de todos os canais: números, saúde e próxima publicação de cada um."""
    rows = []
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    for ch in db.list_channels():
        health = channel_health(ch["id"], check_token=False)
        nxt = db.list_videos(status=["scheduled", *db.IN_PRODUCTION], channel_id=ch["id"], limit=1,
                             order="scheduled_at IS NULL, scheduled_at ASC")
        recent = [v for v in db.list_videos(status="published", channel_id=ch["id"], limit=200, order="scheduled_at DESC")
                  if db.parse_iso(v.get("scheduled_at")) and db.parse_iso(v["scheduled_at"]) >= week_ago]
        last = db.last_published_at(ch["id"])
        c = health["counts"]
        rows.append({
            "id": ch["id"], "name": ch["name"], "active": ch["active"], "preset": ch.get("preset"),
            "language": ch["settings"]["language"], "timezone": ch["settings"]["timezone"],
            "youtube_title": ch.get("youtube_title"), "youtube_thumb": ch.get("youtube_thumb"),
            "youtube_connected": not any(i["code"] in ("not_connected", "token_invalid") for i in health["issues"]),
            "subscribers": ch.get("subscribers"), "views": ch.get("views"),
            "published_7d": len(recent), "views_7d_videos": sum(v.get("views") or 0 for v in recent),
            "in_production": sum(c[s] for s in db.IN_PRODUCTION), "scheduled": c["scheduled"],
            "published": c["published"], "failed": c["failed"],
            "next_publication": _local(ch, nxt[0]["scheduled_at"]) if nxt and nxt[0].get("scheduled_at") else None,
            "last_publication": last.astimezone(tz_of(ch)).strftime("%Y-%m-%d %H:%M") if last else None,
            "health": health["status"], "issues": health["issues"],
            "autopilot": ch["settings"]["autopilot"],
            "daily_quota": ch["settings"]["shorts_per_day"] + ch["settings"]["long_per_day"],
        })
    totals = {
        "channels": len(rows),
        "active": sum(1 for r in rows if r["active"]),
        "subscribers": sum(r["subscribers"] or 0 for r in rows),
        "views": sum(r["views"] or 0 for r in rows),
        "in_production": sum(r["in_production"] for r in rows),
        "scheduled": sum(r["scheduled"] for r in rows),
        "published_7d": sum(r["published_7d"] for r in rows),
        "failed": sum(r["failed"] for r in rows),
        "daily_videos": sum(r["daily_quota"] for r in rows if r["active"]),
        "needs_attention": sum(1 for r in rows if r["health"] != "ok"),
    }
    return {
        "totals": totals,
        "channels": rows,
        "worker_alive": _worker_alive(),
        "claude_configured": pipeline.has_claude(),
        "pexels_configured": bool(env().pexels_api_key),
        "quota": pipeline.quota_status(),
    }


def overview(channel_id: int) -> dict:
    ch = require_channel(channel_id)
    upcoming = db.list_videos(status=["scheduled", *db.IN_PRODUCTION], channel_id=channel_id, limit=20,
                              order="scheduled_at IS NULL, scheduled_at ASC")
    awaiting = [v for v in db.list_videos(status="rendered", channel_id=channel_id, limit=50)
                if ch["settings"]["approval_required"] and not v["approved"]]
    return {
        "channel": channel_public(ch),
        "health": channel_health(channel_id),
        "worker_alive": _worker_alive(),
        "claude_configured": pipeline.has_claude(),
        "pexels_configured": bool(env().pexels_api_key),
        "counts": db.status_counts(channel_id),
        "upcoming": summaries(upcoming),
        "awaiting_approval": summaries(awaiting),
    }


# ---------------------------------------------------------------- vídeos

def _validate_scenes(scenes: list[dict]) -> list[dict]:
    out = []
    for i, s in enumerate(scenes or []):
        text = (s.get("narration") or "").strip()
        if not text:
            raise ServiceError(f"Cena {i + 1} sem narração.")
        out.append({"narration": text, "visual_query": (s.get("visual_query") or "dark atmosphere").strip()})
    return out


def create_video(
    channel_id: int,
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
    ch = require_channel(channel_id)
    if kind not in ("short", "long"):
        raise ServiceError("kind deve ser 'short' ou 'long'.")
    s = ch["settings"]
    when = parse_when(ch, scheduled_at) or best_times.next_free_slot(ch, kind)
    fields: dict = {"topic": topic, "category": category, "is_fiction": is_fiction, "scheduled_at": when,
                    "sources": sources or []}
    if scenes:
        if not title:
            raise ServiceError("Com roteiro pronto, informe também o título.")
        desc = pipeline.compose_description(ch, description or "", sources, is_fiction, kind, title)
        fields.update(
            status="scripted", title=title, description=desc,
            tags=list(dict.fromkeys([*(tags or []), *s["default_tags"]])),
            thumbnail_text=thumbnail_text or title, script={"scenes": _validate_scenes(scenes)},
        )
    elif not pipeline.has_claude():
        raise ServiceError("Sem ANTHROPIC_API_KEY o servidor não escreve roteiros: envie o roteiro pronto em 'scenes'.")
    else:
        fields["status"] = "queued"
    v = db.create_video(channel_id, kind, **fields)
    db.log(f"Vídeo criado ({kind}) para {_local(ch, v['scheduled_at'])}", video_id=v["id"], channel_id=channel_id)
    return db.get_video(v["id"])  # type: ignore[return-value]


def update_video(video_id: int, **changes) -> dict:
    v = db.get_video(video_id)
    if not v:
        raise ServiceError("Vídeo não encontrado.")
    ch = require_channel(v["channel_id"])
    fields: dict = {}
    for k in ("title", "description", "tags", "thumbnail_text", "topic"):
        if changes.get(k) is not None:
            fields[k] = changes[k]
    if v.get("youtube_id") and set(fields) & {"title", "description", "tags"}:
        raise ServiceError("Vídeo já enviado; edite título/descrição no YouTube Studio.")
    if changes.get("scheduled_at"):
        if v.get("youtube_id"):
            raise ServiceError("Vídeo já enviado; altere o agendamento no YouTube Studio.")
        fields["scheduled_at"] = parse_when(ch, changes["scheduled_at"])
    if changes.get("scenes"):
        if v.get("youtube_id"):
            raise ServiceError("Vídeo já enviado; não é possível trocar o roteiro.")
        fields["script"] = {"scenes": _validate_scenes(changes["scenes"])}
        if v["status"] in ("rendered", "failed", "scripted", "queued"):
            fields.update(status="scripted", video_path=None, thumb_path=None, error=None)
    return db.update_video(video_id, **fields)  # type: ignore[return-value]


def move_video(video_id: int, channel_id: int) -> dict:
    """Move um vídeo ainda não enviado para outro canal (vai para o próximo horário livre de lá)."""
    v = db.get_video(video_id)
    if not v:
        raise ServiceError("Vídeo não encontrado.")
    if v.get("youtube_id"):
        raise ServiceError("Vídeo já enviado ao YouTube; não pode trocar de canal.")
    ch = require_channel(channel_id)
    when = best_times.next_free_slot(ch, v["kind"])
    db.log(f"Movido para o canal {ch['name']}", video_id=video_id, channel_id=v["channel_id"])
    return db.update_video(video_id, channel_id=channel_id, scheduled_at=when, approved=False)  # type: ignore[return-value]


def approve(video_id: int) -> dict:
    if not db.get_video(video_id):
        raise ServiceError("Vídeo não encontrado.")
    db.log("Aprovado para publicação", video_id=video_id)
    return db.update_video(video_id, approved=True)  # type: ignore[return-value]


# ---------------------------------------------------------------- desempenho e horários

def analytics(channel_id: int) -> dict:
    ch = require_channel(channel_id)
    tz = tz_of(ch)
    published = [v for v in db.list_videos(status="published", channel_id=channel_id, limit=5000)
                 if v.get("views") is not None]
    top = sorted(published, key=lambda v: v["views"] or 0, reverse=True)[:10]
    per_hour: dict[str, list[dict]] = {}
    per_category: dict[str, list[int]] = defaultdict(list)
    for v in published:
        per_category[v.get("category") or "sem categoria"].append(v["views"])
    for kind in ("short", "long"):
        buckets: dict[int, list[int]] = defaultdict(list)
        for v in published:
            if v["kind"] == kind and v.get("scheduled_at"):
                buckets[db.parse_iso(v["scheduled_at"]).astimezone(tz).hour].append(v["views"])
        per_hour[kind] = [
            {"hour": h, "videos": len(xs), "avg_views": round(sum(xs) / len(xs))}
            for h, xs in sorted(buckets.items())
        ]
    return {
        "channel_id": channel_id,
        "totals": {
            "videos": len(published),
            "views": sum(v["views"] or 0 for v in published),
            "likes": sum(v["likes"] or 0 for v in published),
            "comments": sum(v["comments"] or 0 for v in published),
        },
        "top": summaries(top),
        "per_hour": per_hour,
        "per_category": sorted(
            ({"category": k, "videos": len(xs), "avg_views": round(sum(xs) / len(xs))} for k, xs in per_category.items()),
            key=lambda r: -r["avg_views"],
        ),
        "history": db.channel_stats(channel_id),
    }


def compare_channels() -> list[dict]:
    """Ranking dos canais por desempenho médio dos vídeos publicados."""
    out = []
    for ch in db.list_channels():
        pub = [v for v in db.list_videos(status="published", channel_id=ch["id"], limit=5000) if v.get("views") is not None]
        out.append({
            "channel_id": ch["id"], "name": ch["name"], "language": ch["settings"]["language"],
            "videos": len(pub), "views": sum(v["views"] for v in pub),
            "avg_views": round(sum(v["views"] for v in pub) / len(pub)) if pub else 0,
            "subscribers": ch.get("subscribers"),
        })
    return sorted(out, key=lambda r: -r["avg_views"])


def upcoming_slots(channel_id: int, kind: str, days: int = 7) -> list[str]:
    ch = require_channel(channel_id)
    s = ch["settings"]
    tz = tz_of(ch)
    today = datetime.now(tz).date()
    start = datetime.combine(today, time(0), tzinfo=tz)
    taken = [t for t, _ in db.occupied_slots(channel_id, start, start + timedelta(days=days + 1))]
    per_day = s["shorts_per_day"] if kind == "short" else s["long_per_day"]
    perf = best_times.performance_by_hour(ch, kind)
    out: list[str] = []
    for d in range(days):
        for slot in best_times.pick_slots(ch, today + timedelta(days=d), kind, max(1, per_day), taken, perf):
            taken.append(slot)
            local = slot.astimezone(tz)
            out.append(f"{_WEEKDAYS[local.weekday()]} {local.strftime('%d/%m %H:%M')}")
    return out


def env_status() -> dict:
    return {
        "anthropic_key": pipeline.has_claude(),
        "pexels_key": bool(env().pexels_api_key),
        "tts_provider": env().tts_provider,
        "model": env().claude_model,
        "data_dir": str(env().data_dir.resolve()),
        "global_client_secret": env().youtube_client_secret.exists(),
        "uploads_per_project_per_day": env().youtube_uploads_per_project_per_day,
        "base_url": env().base_url(),
    }


def validate_settings(changes: dict) -> dict:
    try:
        return coerce_settings(changes)
    except ValueError as e:
        raise ServiceError(str(e)) from e


# ---------------------------------------------------------------- séries

def series_summary(srs: dict, with_episodes: bool = True) -> dict:
    ch = db.get_channel(srs["channel_id"])
    eps = db.episodes(srs["id"])
    out = {
        **srs,
        "channel_name": ch["name"] if ch else None,
        "published": sum(1 for v in eps if v["status"] == "published"),
        "uploaded": sum(1 for v in eps if v.get("youtube_id")),
        "first_at": _local(ch, eps[0]["scheduled_at"]) if ch and eps else None,
        "last_at": _local(ch, eps[-1]["scheduled_at"]) if ch and eps else None,
        "playlist_url": f"https://www.youtube.com/playlist?list={srs['playlist_id']}" if srs.get("playlist_id") else None,
    }
    if with_episodes:
        out["episodes"] = [{**video_summary(v, ch), "episode": v["episode"]} for v in eps]
    return out


def _start_day(ch: dict, start_date: str | None):
    if not start_date:
        return None
    try:
        return datetime.fromisoformat(start_date.strip()[:10]).date()
    except ValueError as e:
        raise ServiceError("start_date deve ser 'AAAA-MM-DD'.") from e


def create_series(channel_id: int, parts: int = 3, kind: str = "short", topic: str | None = None,
                  angle: str | None = None, is_fiction: bool = False, start_date: str | None = None) -> dict:
    """Série escrita pelo servidor (Claude API). Horários reservados na hora."""
    ch = require_channel(channel_id)
    if not pipeline.has_claude():
        raise ServiceError("Sem ANTHROPIC_API_KEY no servidor: use create_series_from_script com os episódios prontos.")
    try:
        srs = pipeline.create_series_plan(ch, parts, kind, topic, angle, is_fiction, _start_day(ch, start_date))
    except ValueError as e:
        raise ServiceError(str(e)) from e
    return series_summary(srs)


def create_series_from_script(channel_id: int, title: str, episodes: list[dict], kind: str = "short",
                              logline: str = "", is_fiction: bool = False, sources: list[str] | None = None,
                              start_date: str | None = None) -> dict:
    """Série com roteiros prontos (ex.: escritos pelo Claude via MCP)."""
    ch = require_channel(channel_id)
    if not title:
        raise ServiceError("Informe o título da série.")
    if len(episodes) < 2:
        raise ServiceError("Uma série precisa de pelo menos 2 episódios.")
    clean = []
    for i, ep in enumerate(episodes, start=1):
        if not ep.get("title"):
            raise ServiceError(f"Episódio {i} sem título.")
        clean.append({**ep, "scenes": _validate_scenes(ep.get("scenes") or [])})
    try:
        srs = pipeline.create_series_plan(ch, len(clean), kind, title, None, is_fiction, _start_day(ch, start_date))
    except ValueError as e:
        raise ServiceError(str(e)) from e
    pipeline.fill_series(srs, ch, title, logline, clean, is_fiction, sources)
    return series_summary(db.get_series(srs["id"]))


def list_series(channel_id: int | None = None, status: str | None = None) -> list[dict]:
    statuses = status.split(",") if status else None
    return [series_summary(s, with_episodes=False) for s in db.list_series(channel_id, statuses)]


def get_series(series_id: int) -> dict:
    srs = db.get_series(series_id)
    if not srs:
        raise ServiceError("Série não encontrada.")
    return series_summary(srs)
