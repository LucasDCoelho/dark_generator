"""Melhores horários de publicação.

Começa com uma curva base para o público brasileiro (picos no almoço e à noite; conteúdo dark rende
melhor tarde da noite) e vai se ajustando com o desempenho real dos vídeos do próprio canal.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from .. import db
from ..config import env

# Curvas base (0..1) por hora local. Vídeos longos: publicar ~1-2h antes do pico para indexar.
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


def prior(kind: str, weekday: int) -> list[float]:
    weekend = weekday >= 5
    if kind == "short":
        return list(_SHORT_WEEKEND if weekend else _SHORT_WEEKDAY)
    return list(_LONG_WEEKEND if weekend else _LONG_WEEKDAY)


def performance_by_hour(kind: str) -> tuple[dict[int, float], dict[int, int]]:
    """Desempenho relativo por hora local (1.0 = média do canal), usando vídeos com 48h+."""
    tz = env().tz
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    vids = [
        v for v in db.list_videos(status="published", kind=kind, limit=500)
        if v.get("views") is not None and v.get("scheduled_at") and db.parse_iso(v["scheduled_at"]) < cutoff
    ]
    if len(vids) < MIN_SAMPLES:
        return {}, {}
    logs = [math.log1p(v["views"]) for v in vids]
    mean = sum(logs) / len(logs) or 1.0
    per_hour: dict[int, list[float]] = defaultdict(list)
    for v, lv in zip(vids, logs):
        h = db.parse_iso(v["scheduled_at"]).astimezone(tz).hour
        per_hour[h].append(lv / mean)
    perf = {h: sum(xs) / len(xs) for h, xs in per_hour.items()}
    counts = {h: len(xs) for h, xs in per_hour.items()}
    return perf, counts


def hour_scores(kind: str, weekday: int, perf: dict | None = None, counts: dict | None = None) -> list[float]:
    if perf is None or counts is None:
        perf, counts = performance_by_hour(kind)
    base = prior(kind, weekday)
    scores = []
    for h, p in enumerate(base):
        if h in perf:
            w = counts[h] / (counts[h] + 3)  # confiança cresce com o nº de vídeos naquela hora
            p = p * (1 + w * (perf[h] - 1))
        scores.append(round(max(0.0, min(1.5, p)), 3))
    return scores


def heatmap(kind: str) -> dict:
    perf, counts = performance_by_hour(kind)
    return {
        "kind": kind,
        "adaptive": bool(perf),
        "samples": sum(counts.values()) if counts else 0,
        "days": ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"],
        "scores": [hour_scores(kind, d, perf, counts) for d in range(7)],
    }


def pick_slots(
    day: date,
    kind: str,
    count: int,
    min_gap_hours: float,
    taken: list[datetime] | None = None,
) -> list[datetime]:
    """Escolhe `count` horários (UTC) no dia local `day`, respeitando o intervalo mínimo."""
    if count <= 0:
        return []
    tz = env().tz
    now = datetime.now(timezone.utc)
    taken = list(taken or [])
    scores = hour_scores(kind, day.weekday())
    gap = timedelta(hours=min_gap_hours)
    chosen: list[datetime] = []
    for h in sorted(range(24), key=lambda x: -scores[x]):
        if scores[h] < 0.2:
            break
        slot = datetime.combine(day, time(h, 0), tzinfo=tz).astimezone(timezone.utc)
        if slot < now + MIN_LEAD:
            continue
        if any(abs(slot - t) < gap for t in taken + chosen):
            continue
        chosen.append(slot)
        if len(chosen) == count:
            break
    return sorted(chosen)


def next_free_slot(kind: str, min_gap_hours: float, horizon_days: int = 14) -> datetime:
    """Próximo bom horário livre a partir de agora (usado quando um vídeo perdeu seu horário)."""
    tz = env().tz
    today = datetime.now(tz).date()
    start = datetime.combine(today, time(0), tzinfo=tz)
    taken = [t for t, _ in db.occupied_slots(start, start + timedelta(days=horizon_days + 1))]
    for d in range(horizon_days):
        slots = pick_slots(today + timedelta(days=d), kind, 1, min_gap_hours, taken)
        if slots:
            return slots[0]
    return datetime.now(timezone.utc) + MIN_LEAD
