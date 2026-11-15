"""Publicação manual nas redes (TikTok, Kwai, Instagram Reels, YouTube Shorts).

Nenhuma dessas redes permite postar por API sem aprovação (TikTok e YouTube exigem auditoria do app;
Kwai não tem API pública). O sistema prepara tudo e você posta pelo celular:
vídeo pronto para baixar + legenda e hashtags no formato de cada rede + lembrete do rótulo de IA.
Depois de postar, marque aqui (com o link) e anote as views para comparar plataformas.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from urllib.parse import quote

from . import db
from .config import env, lang_text

PLATFORMS: dict[str, dict] = {
    "tiktok": {
        "label": "TikTok", "max_caption": 2200, "max_hashtags": 5,
        "ai_label": "Ative 'Conteúdo gerado por IA' (rótulo AIGC) em 'Mais opções' antes de publicar.",
        "tip": "Vídeos com mais de 1 minuto contam para o Creator Rewards. Use um som em alta em volume baixo, se quiser.",
    },
    "kwai": {
        "label": "Kwai", "max_caption": 1000, "max_hashtags": 5,
        "ai_label": "Informe que o conteúdo foi feito com IA, se o app pedir.",
        "tip": "No Kwai, continuidade é tudo: poste os capítulos em ordem e fixe a Parte 1 no perfil.",
    },
    "instagram": {
        "label": "Instagram Reels", "max_caption": 2200, "max_hashtags": 5,
        "ai_label": "Em 'Configurações avançadas', ative o rótulo 'Informação de IA'.",
        "tip": "Use a legenda curta e convide a salvar/compartilhar: são os sinais que mais pesam no Explorar.",
    },
    "youtube": {
        "label": "YouTube Shorts", "max_caption": 4900, "max_hashtags": 5,
        "ai_label": "Em 'Conteúdo alterado ou sintético', marque 'Sim' se houver material realista gerado por IA.",
        "tip": "Postando pelo app/Studio o vídeo sai público na hora (sem depender da auditoria da API).",
    },
}

_HASHTAG_RE = re.compile(r"#[\wÀ-ſ]+")


def _slug_tag(tag: str) -> str:
    t = unicodedata.normalize("NFKD", tag).encode("ascii", "ignore").decode()
    return "#" + re.sub(r"[^A-Za-z0-9]", "", t).lower() if t.strip() else ""


def _base_text(description: str) -> str:
    """Descrição sem as hashtags e sem a lista de fontes (vai num bloco separado)."""
    text = _HASHTAG_RE.sub("", description or "")
    for marker in ("Fontes:", "Sources:", "Fuentes:"):
        text = text.split(marker)[0]
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text)).strip()


def caption_for(video: dict, channel: dict, platform: str, series: dict | None = None) -> dict:
    cfg = PLATFORMS[platform]
    lt = lang_text(channel["settings"]["language"])
    found = [h.lower() for h in _HASHTAG_RE.findall(video.get("description") or "")]
    tags = found + [_slug_tag(t) for t in (video.get("tags") or [])]
    if platform == "youtube" and video["kind"] == "short":
        tags = ["#shorts", *tags]
    hashtags = [h for h in dict.fromkeys(tags) if len(h) > 2][: cfg["max_hashtags"]]

    title = video.get("title") or ""
    body = _base_text(video.get("description") or "")
    is_series = bool(series and video.get("episode"))
    lines = [title, "", body] if body else [title]
    if is_series:
        ep, parts = video["episode"], series["parts"]
        lines += ["", f"{lt['series']}: {series.get('title') or ''} — {lt['part']} {ep}/{parts}"]
        if ep < parts:
            lines.append(lt["follow"])
    if video.get("is_fiction") and lt["fiction"] not in body:
        lines += ["", lt["fiction"]]
    caption = "\n".join(lines).strip() + "\n\n" + " ".join(hashtags)
    out = {"platform": platform, "label": cfg["label"], "caption": caption[: cfg["max_caption"]],
           "hashtags": hashtags, "ai_label": cfg["ai_label"], "tip": cfg["tip"]}
    if platform == "youtube":
        out["title"] = title[:100]
    return out


def download_url(video_id: int) -> str:
    url = f"{env().base_url()}/media/{video_id}/video?download=1"
    if env().dashboard_token:
        url += f"&token={quote(env().dashboard_token)}"
    return url


def post_kit(video_id: int) -> dict:
    """Tudo para postar o vídeo à mão: link do MP4, legendas por rede, status de cada postagem."""
    v = db.get_video(video_id)
    if not v:
        raise ValueError("Vídeo não encontrado.")
    ch = db.get_channel(v["channel_id"])
    series = db.get_series(v["series_id"]) if v.get("series_id") else None
    posted = {p["platform"]: p for p in db.postings_for(video_id)}
    platforms = ch["settings"]["platforms"] or list(PLATFORMS)
    return {
        "video_id": v["id"], "channel_id": ch["id"], "channel_name": ch["name"], "status": v["status"],
        "title": v.get("title"), "episode": v.get("episode"), "parts": series["parts"] if series else None,
        "series_title": series.get("title") if series else None, "duration_s": v.get("duration_s"),
        "suggested_time": v.get("scheduled_at"), "download_url": download_url(v["id"]),
        "has_file": bool(v.get("video_path")),
        "platforms": [{**caption_for(v, ch, p, series), "posted": posted.get(p)} for p in platforms if p in PLATFORMS],
    }


def mark_posted(video_id: int, platform: str, url: str | None = None, posted_at: str | None = None) -> dict:
    if platform not in PLATFORMS:
        raise ValueError(f"Plataforma inválida. Opções: {', '.join(PLATFORMS)}")
    v = db.get_video(video_id)
    if not v:
        raise ValueError("Vídeo não encontrado.")
    fields: dict = {"url": url} if url else {}
    if posted_at:
        fields["posted_at"] = posted_at
    db.upsert_posting(video_id, platform, **fields)
    if v["status"] in ("ready", "rendered"):
        db.update_video(video_id, status="published")
    db.log(f"Postado no {PLATFORMS[platform]['label']}" + (f": {url}" if url else ""), video_id=video_id,
           channel_id=v["channel_id"])
    return post_kit(video_id)


def unmark_posted(video_id: int, platform: str) -> dict:
    db.delete_posting(video_id, platform)
    v = db.get_video(video_id)
    if v and v["status"] == "published" and not db.postings_for(video_id) and not v.get("youtube_id"):
        db.update_video(video_id, status="ready")
    return post_kit(video_id)


def update_stats(video_id: int, platform: str, views: int | None = None, likes: int | None = None,
                 comments: int | None = None, shares: int | None = None) -> dict:
    if not any(p["platform"] == platform for p in db.postings_for(video_id)):
        raise ValueError("Marque como postado nesta plataforma antes de anotar as estatísticas.")
    fields = {k: v for k, v in {"views": views, "likes": likes, "comments": comments, "shares": shares}.items()
              if v is not None}
    db.upsert_posting(video_id, platform, **fields)
    return post_kit(video_id)


def ready_queue(channel_id: int | None = None, limit: int = 50) -> list[dict]:
    """Vídeos prontos para postar, na ordem sugerida (horário), incluindo os parcialmente postados."""
    vids = db.list_videos(status=["ready"], channel_id=channel_id, limit=limit,
                          order="scheduled_at IS NULL, scheduled_at ASC, episode ASC")
    partial = []
    for v in db.list_videos(status="published", channel_id=channel_id, limit=200, order="updated_at DESC"):
        if v.get("youtube_id"):
            continue
        ch = db.get_channel(v["channel_id"])
        done = {p["platform"] for p in db.postings_for(v["id"])}
        if ch and set(ch["settings"]["platforms"]) - done:
            partial.append(v)
    return [post_kit(v["id"]) for v in [*vids, *partial][:limit]]


def platform_stats(channel_id: int | None = None) -> dict:
    """Desempenho por plataforma (a partir das views anotadas) e por série."""
    rows = db.postings_by_channel(channel_id)
    by_platform: dict[str, list[dict]] = defaultdict(list)
    by_series: dict[int, dict] = {}
    for r in rows:
        by_platform[r["platform"]].append(r)
        if r.get("series_id"):
            s = by_series.setdefault(r["series_id"], {"series_id": r["series_id"], "posts": 0, "views": 0,
                                                      "by_episode": defaultdict(int)})
            s["posts"] += 1
            s["views"] += r.get("views") or 0
            s["by_episode"][r["episode"]] += r.get("views") or 0
    platforms = []
    for p, items in by_platform.items():
        with_views = [i for i in items if i.get("views") is not None]
        platforms.append({
            "platform": p, "label": PLATFORMS.get(p, {}).get("label", p), "posts": len(items),
            "with_stats": len(with_views), "views": sum(i["views"] for i in with_views),
            "avg_views": round(sum(i["views"] for i in with_views) / len(with_views)) if with_views else None,
            "likes": sum(i.get("likes") or 0 for i in items),
        })
    series_rows = []
    for sid, s in by_series.items():
        srs = db.get_series(sid)
        eps = dict(sorted(s["by_episode"].items()))
        first = eps.get(1) or 0
        series_rows.append({
            "series_id": sid, "title": srs["title"] if srs else None, "posts": s["posts"], "views": s["views"],
            "views_by_episode": eps,
            # quanto do público da parte 1 chega à última parte publicada (sinal de que a novela prende)
            "retention_last_vs_first": round(list(eps.values())[-1] / first, 2) if first and len(eps) > 1 else None,
        })
    return {"platforms": sorted(platforms, key=lambda r: -(r["avg_views"] or 0)),
            "series": sorted(series_rows, key=lambda r: -r["views"])}
