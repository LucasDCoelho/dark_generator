"""Melhores horários de publicação, por canal.

Usa uma curva base no fuso do público-alvo do canal (picos no almoço e à noite; conteúdo dark rende
melhor tarde da noite) e vai se ajustando com o desempenho real dos vídeos daquele canal.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from .. import db
from ..config import tz_of

# Curvas base (0..1) por hora local do público. Longos: publicar ~1-2h antes do pico para indexar.
_SHORT_WEEKDAY = [.25, .12, .06, .04, .03, .05, .15, .30, .35, .35, .40, .55,
                  .75, .70, .50, .50, .55, .65, .80, .90, .95, 1.0, .85, .55]
_SHORT_WEEKEND = [.35, .20, .10, .05, .04, .05, .10, .20, .30, .35, .55, .70,
                  .80, .80, .70, .75, .75, .75, .80, .90, .95, 1.0, .90, .65]
_LONG_WEEKDAY = [.15, .08, .04, .03, .03, .04, .10, .20, .25, .30, .35, .45,
                 .55, .50, .40, .45, .60, .80, .95, 1.0, .90, .75, .50, .30]
_LONG_WEEKEND = [.20, .10, .05, .03, .03, .04, .08, .20, .35, .45, .60, .70,
                 .70, .65, .75, .85, .90, .95, 1.0, .95, .85, .70, .45, .30]

MIN_LEAD = timedelta(minutes=90)  # tempo mínimo para roteiro + render + upload
MIN_SAMPLES = 6  # vídeos com estatística antes de começar a adaptar
DAYS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def prior(kind: str, weekday: int) -> list[float]:
    weekend = weekday >= 5
    if kind == "short":
        return list(_SHORT_WEEKEND if weekend else _SHORT_WEEKDAY)
    return list(_LONG_WEEKEND if weekend else _LONG_WEEKDAY)


def performance_by_hour(channel: dict, kind: str) -> tuple[dict[int, float], dict[int, int]]:
    """Desempenho relativo por hora local (1.0 = média do canal), usando vídeos com 48h+."""
    tz = tz_of(channel)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    vids = [
        v for v in db.list_videos(status="published", kind=kind, channel_id=channel["id"], limit=500)
        if v.get("views") is not None and v.get("scheduled_at") and db.parse_iso(v["scheduled_at"]) < cutoff
    ]
    if len(vids) < MIN_SAMPLES:
        return {}, {}
    logs = [math.log1p(v["views"]) for v in vids]
    mean = sum(logs) / len(logs) or 1.0
    per_hour: dict[int, list[float]] = defaultdict(list)
    for v, lv in zip(vids, logs):
        per_hour[db.parse_iso(v["scheduled_at"]).astimezone(tz).hour].append(lv / mean)
    perf = {h: sum(xs) / len(xs) for h, xs in per_hour.items()}
    counts = {h: len(xs) for h, xs in per_hour.items()}
    return perf, counts


def hour_scores(kind: str, weekday: int, perf: dict | None = None, counts: dict | None = None) -> list[float]:
    perf, counts = perf or {}, counts or {}
    scores = []
    for h, p in enumerate(prior(kind, weekday)):
        if h in perf:
            w = counts[h] / (counts[h] + 3)  # confiança cresce com o nº de vídeos naquela hora
            p = p * (1 + w * (perf[h] - 1))
        scores.append(round(max(0.0, min(1.5, p)), 3))
    return scores


def heatmap(channel: dict, kind: str) -> dict:
    perf, counts = performance_by_hour(channel, kind)
    return {
        "kind": kind,
        "timezone": channel["settings"]["timezone"],
        "adaptive": bool(perf),
        "samples": sum(counts.values()) if counts else 0,
        "days": DAYS,
        "scores": [hour_scores(kind, d, perf, counts) for d in range(7)],
    }


def pick_slots(
    channel: dict,
    day: date,
    kind: str,
    count: int,
    taken: list[datetime] | None = None,
    perf: tuple[dict, dict] | None = None,
    not_before: datetime | None = None,
) -> list[datetime]:
    """Escolhe `count` horários (UTC) no dia `day` (fuso do canal), respeitando o intervalo mínimo."""
    if count <= 0:
        return []
    tz = tz_of(channel)
    now = datetime.now(timezone.utc)
    taken = list(taken or [])
    p, c = perf if perf is not None else performance_by_hour(channel, kind)
    scores = hour_scores(kind, day.weekday(), p, c)
    gap = timedelta(hours=channel["settings"]["min_gap_hours"])
    chosen: list[datetime] = []
    for h in sorted(range(24), key=lambda x: -scores[x]):
        if scores[h] < 0.2:
            break
        slot = datetime.combine(day, time(h, 0), tzinfo=tz).astimezone(timezone.utc)
        if slot < now + MIN_LEAD or (not_before and slot <= not_before):
            continue
        if any(abs(slot - t) < gap for t in taken + chosen):
            continue
        chosen.append(slot)
        if len(chosen) == count:
            break
    return sorted(chosen)


def next_free_slot(channel: dict, kind: str, horizon_days: int = 14, not_before: datetime | None = None) -> datetime:
    """Próximo bom horário livre do canal a partir de agora (ou depois de `not_before`)."""
    tz = tz_of(channel)
    first = max(datetime.now(timezone.utc), not_before or datetime.now(timezone.utc))
    today = first.astimezone(tz).date()
    start = datetime.combine(today, time(0), tzinfo=tz)
    taken = [t for t, _ in db.occupied_slots(channel["id"], start, start + timedelta(days=horizon_days + 1))]
    perf = performance_by_hour(channel, kind)
    for d in range(horizon_days):
        slots = pick_slots(channel, today + timedelta(days=d), kind, 1, taken, perf, not_before)
        if slots:
            return slots[0]
    return first + MIN_LEAD


def series_slots(channel: dict, kind: str, parts: int, start_day: date) -> list[datetime]:
    """Um horário por dia, em dias seguidos a partir de `start_day`, cada episódio depois do anterior."""
    tz = tz_of(channel)
    start = datetime.combine(start_day, time(0), tzinfo=tz)
    taken = [t for t, _ in db.occupied_slots(channel["id"], start - timedelta(days=1), start + timedelta(days=parts + 30))]
    perf = performance_by_hour(channel, kind)
    out: list[datetime] = []
    day = start_day
    for _ in range(parts + 30):  # pula dias sem horário livre
        if len(out) == parts:
            break
        got = pick_slots(channel, day, kind, 1, taken, perf, out[-1] if out else None)
        if got:
            out.append(got[0])
            taken.append(got[0])
        day += timedelta(days=1)
    if len(out) < parts:
        raise ValueError("Não há horários livres suficientes para a série; reduza as partes ou a agenda do canal.")
    return out
