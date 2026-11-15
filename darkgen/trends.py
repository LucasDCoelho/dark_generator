"""Scanner de tendências para escolher nichos com dados.

- YouTube (dados reais, via Data API): vídeos mais vistos dos últimos N dias por palavra-chave e região,
  com views/dia e "outlier" (views ÷ inscritos). Canal pequeno com vídeo explodindo = demanda não atendida.
- TikTok, Kwai e Instagram: essas plataformas não têm API pública de tendências; o Claude varre a web
  (notícias, relatórios, criadores) e devolve temas e formatos citados recentemente, com links.

Cada varredura fica salva para comparar ao longo do tempo.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from statistics import median

from pydantic import BaseModel, Field

from . import db
from .config import env

# Palavras-chave padrão por idioma (dark/faceless).
DEFAULT_KEYWORDS = {
    "pt": ["mistério", "caso real", "história de terror", "história sombria", "desaparecimento",
           "lenda urbana", "sobrevivência", "backrooms", "curiosidades sombrias", "paranormal"],
    "en": ["unsolved mystery", "true scary story", "disappearance", "dark history", "survival story",
           "backrooms", "creepy facts", "paranormal", "deep sea", "cold case"],
    "es": ["misterio", "caso real", "historia de terror", "desaparición", "leyenda urbana",
           "historia oscura", "supervivencia", "paranormal", "datos perturbadores", "backrooms"],
}

SMALL_CHANNEL = 200_000  # inscritos — abaixo disso, um vídeo viral indica demanda pelo tema
OUTLIER_MIN = 3.0  # views >= 3x inscritos


def _iso_duration_seconds(value: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _youtube_client():
    from googleapiclient.discovery import build

    if env().youtube_api_key:
        return build("youtube", "v3", developerKey=env().youtube_api_key, cache_discovery=False)
    from .youtube import client as yt

    for ch in db.list_channels():
        if yt.is_connected(ch["id"]):
            return yt._yt(ch["id"])
    raise RuntimeError(
        "Scanner do YouTube precisa de YOUTUBE_API_KEY no .env ou de pelo menos um canal conectado."
    )


def _score_keyword(videos: list[dict]) -> dict:
    if not videos:
        return {"videos": 0, "opportunity": 0}
    vpd = [v["views_per_day"] for v in videos]
    total = sum(v["views"] for v in videos) or 1
    by_channel: dict[str, int] = {}
    for v in videos:
        by_channel[v["channel_id"]] = by_channel.get(v["channel_id"], 0) + v["views"]
    top_share = max(by_channel.values()) / total
    outliers = [v for v in videos if v["is_outlier"]]
    small = [v for v in videos if (v["subscribers"] or 0) < SMALL_CHANNEL]
    med = median(vpd)
    # Heurística transparente (0-100): demanda (views/dia) + espaço para canais pequenos + pouca concentração.
    opportunity = (
        min(40.0, math.log10(med + 1) * 8)
        + 35 * (len(outliers) / len(videos))
        + 15 * (len(small) / len(videos))
        + 10 * (1 - top_share)
    )
    return {
        "videos": len(videos),
        "median_views_per_day": round(med),
        "max_views_per_day": round(max(vpd)),
        "outlier_videos": len(outliers),
        "small_channel_share": round(len(small) / len(videos), 2),
        "distinct_channels": len(by_channel),
        "top_channel_share": round(top_share, 2),
        "shorts_share": round(sum(1 for v in videos if v["is_short"]) / len(videos), 2),
        "opportunity": round(opportunity),
    }


def scan_youtube(
    keywords: list[str] | None = None,
    region: str = "BR",
    language: str = "pt",
    days: int = 14,
    per_keyword: int = 25,
) -> dict:
    """Varre o YouTube. Custo de cota: ~102 unidades por palavra-chave (search.list custa 100)."""
    lang = language.split("-")[0].lower()
    keywords = [k.strip() for k in (keywords or DEFAULT_KEYWORDS.get(lang, DEFAULT_KEYWORDS["en"])) if k.strip()]
    if not keywords:
        raise ValueError("Informe ao menos uma palavra-chave.")
    if len(keywords) > 25:
        raise ValueError("Máximo de 25 palavras-chave por varredura (cada uma custa ~100 unidades de cota).")
    yt = _youtube_client()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    per_kw: dict[str, list[str]] = {}
    for kw in keywords:
        resp = yt.search().list(
            part="id", q=kw, type="video", order="viewCount", publishedAfter=since,
            regionCode=region, relevanceLanguage=lang, maxResults=min(50, per_keyword),
        ).execute()
        per_kw[kw] = [it["id"]["videoId"] for it in resp.get("items", []) if it.get("id", {}).get("videoId")]

    all_ids = list(dict.fromkeys(i for ids in per_kw.values() for i in ids))
    videos: dict[str, dict] = {}
    for i in range(0, len(all_ids), 50):
        resp = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(all_ids[i : i + 50])).execute()
        for it in resp.get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            published = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
            age_days = max((now - published).total_seconds() / 86400, 0.25)
            views = int(st.get("viewCount", 0))
            duration = _iso_duration_seconds(it.get("contentDetails", {}).get("duration", ""))
            videos[it["id"]] = {
                "id": it["id"], "title": sn["title"], "channel_id": sn["channelId"], "channel": sn["channelTitle"],
                "published_at": sn["publishedAt"], "age_days": round(age_days, 1), "views": views,
                "likes": int(st.get("likeCount", 0)), "comments": int(st.get("commentCount", 0)),
                "views_per_day": round(views / age_days), "duration_s": duration, "is_short": 0 < duration <= 180,
                "thumbnail": sn.get("thumbnails", {}).get("medium", {}).get("url"),
                "url": f"https://youtu.be/{it['id']}",
            }

    channel_ids = list({v["channel_id"] for v in videos.values()})
    subs: dict[str, int | None] = {}
    for i in range(0, len(channel_ids), 50):
        resp = yt.channels().list(part="statistics", id=",".join(channel_ids[i : i + 50])).execute()
        for it in resp.get("items", []):
            st = it.get("statistics", {})
            subs[it["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    for v in videos.values():
        s = subs.get(v["channel_id"])
        v["subscribers"] = s
        v["outlier"] = round(v["views"] / max(s or 0, 1000), 1) if s is not None else None
        v["is_outlier"] = bool(s is not None and s < SMALL_CHANNEL and v["views"] >= OUTLIER_MIN * max(s, 1000))

    results = []
    for kw, ids in per_kw.items():
        vids = [videos[i] for i in ids if i in videos]
        results.append({
            "keyword": kw, **_score_keyword(vids),
            "top_videos": sorted(vids, key=lambda v: -v["views_per_day"])[:5],
            "top_outliers": sorted([v for v in vids if v["is_outlier"]], key=lambda v: -(v["outlier"] or 0))[:5],
        })
    results.sort(key=lambda r: -r["opportunity"])
    params = {"keywords": keywords, "region": region, "language": lang, "days": days, "per_keyword": per_keyword}
    scan_id = db.save_trend_scan("youtube", params, results)
    return {"scan_id": scan_id, "platform": "youtube", "params": params, "keywords": results,
            "quota_cost_estimate": len(keywords) * 100 + math.ceil(len(all_ids) / 50) + math.ceil(len(channel_ids) / 50)}


# ---------------------------------------------------------------- redes sem API (varredura na web)

class SocialTrend(BaseModel):
    platform: str = Field(description="tiktok, kwai, instagram ou youtube")
    topic: str = Field(description="Tema ou sub-nicho em alta.")
    format: str = Field(description="Formato que está funcionando (ex.: série em partes, narração sobre B-roll).")
    evidence: str = Field(description="O que indica que está em alta (números, datas, quem reportou).")
    source_url: str = Field(description="URL da fonte principal.")
    dark_fit: bool = Field(description="True se dá para fazer sem aparecer, com pegada dark/mistério.")
    confidence: str = Field(description="alta, média ou baixa — conforme a qualidade da evidência.")


class SocialTrendList(BaseModel):
    trends: list[SocialTrend]
    summary: str = Field(description="3-5 frases com a leitura geral para quem vai escolher nichos.")


def scan_social(platforms: list[str] | None = None, focus: str = "", market: str = "Brasil") -> dict:
    """Varre a web atrás de temas/formatos sem rosto em alta nas redes (via Claude + web search)."""
    from .content import writer

    platforms = platforms or ["tiktok", "kwai", "instagram", "youtube"]
    since = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%d/%m/%Y")
    client = writer._client()
    question = (
        f"Pesquise na web o que está em alta AGORA (desde {since}) em conteúdo SEM APARECER (faceless) nas "
        f"plataformas: {', '.join(platforms)}, para o mercado {market}. "
        f"Foco: {focus or 'nichos dark — mistério, terror, casos reais, história sombria, curiosidades, sobrevivência'}.\n"
        "Procure notícias, relatórios oficiais das plataformas (ex.: Kwai, TikTok Creative Center, YouTube "
        "Culture & Trends), e análises de criadores. Para cada tendência, traga tema, formato, a evidência "
        "concreta (números/datas) e a URL. Diferencie dado oficial de opinião de blog."
    )
    messages: list = [{"role": "user", "content": question}]
    resp = None
    for _ in range(5):
        resp = client.beta.messages.create(
            model=env().claude_model, max_tokens=16000, thinking={"type": "adaptive"},
            output_config={"effort": "medium"}, betas=writer._BETAS, fallbacks="default",
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 10}],
            messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = [*messages, {"role": "assistant", "content": resp.content}]
    assert resp is not None
    writer._check(resp)
    notes = "".join(b.text for b in resp.content if b.type == "text")

    parsed = client.beta.messages.parse(
        model=env().claude_model, max_tokens=16000, thinking={"type": "adaptive"},
        output_config={"effort": "low"}, betas=writer._BETAS, fallbacks="default",
        messages=[{"role": "user", "content": "Organize estas anotações de pesquisa em tendências estruturadas, "
                                              "sem inventar nada que não esteja nelas:\n\n" + notes}],
        output_format=SocialTrendList,
    )
    writer._check(parsed)
    data = parsed.parsed_output.model_dump()
    params = {"platforms": platforms, "focus": focus, "market": market}
    scan_id = db.save_trend_scan("social", params, data)
    return {"scan_id": scan_id, "platform": "social", "params": params, **data}


def list_scans(limit: int = 20) -> list[dict]:
    return [{k: s[k] for k in ("id", "ts", "platform", "params")} for s in db.list_trend_scans(limit)]


def get_scan(scan_id: int) -> dict:
    s = db.get_trend_scan(scan_id)
    if not s:
        raise ValueError("Varredura não encontrada.")
    return s


def compare_scans(scan_a: int, scan_b: int) -> list[dict]:
    """Variação de oportunidade por palavra-chave entre duas varreduras do YouTube."""
    a, b = get_scan(scan_a), get_scan(scan_b)
    if a["platform"] != "youtube" or b["platform"] != "youtube":
        raise ValueError("Comparação disponível só para varreduras do YouTube.")
    old = {r["keyword"]: r for r in a["results"]}
    out = []
    for r in b["results"]:
        prev = old.get(r["keyword"])
        out.append({
            "keyword": r["keyword"], "opportunity": r["opportunity"],
            "delta_opportunity": r["opportunity"] - prev["opportunity"] if prev else None,
            "median_views_per_day": r.get("median_views_per_day"),
            "delta_views_per_day": (r.get("median_views_per_day", 0) - prev.get("median_views_per_day", 0)) if prev else None,
        })
    return sorted(out, key=lambda x: -(x["delta_opportunity"] or 0))
