"""Séries em partes: reserva de horários, escrita, ordem de publicação, playlist e automação."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TTS_PROVIDER", "silent")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    from darkgen import config, db

    config.env.cache_clear()
    db._initialized = False
    yield
    config.env.cache_clear()


@pytest.fixture
def channel():
    from darkgen import service

    return service.create_channel("Contos", preset="contos_terror")


def _episodes(n):
    return [{"title": f"A casa (Parte {i})", "description": f"ep {i}", "tags": ["terror"], "thumbnail_text": "NÃO ABRA",
             "scenes": [{"narration": f"Cena do episódio {i}.", "visual_query": "dark house"}]} for i in range(1, n + 1)]


def test_plan_reserves_one_slot_per_day_in_order(channel):
    from darkgen import db, pipeline
    from darkgen.config import tz_of

    start = datetime.now(tz_of(channel)).date() + timedelta(days=2)
    srs = pipeline.create_series_plan(channel, 4, "short", topic="A casa", start_day=start)
    eps = db.episodes(srs["id"])
    assert [v["episode"] for v in eps] == [1, 2, 3, 4]
    assert all(v["status"] == "queued" and v["series_id"] == srs["id"] for v in eps)
    days = [db.parse_iso(v["scheduled_at"]).astimezone(tz_of(channel)).date() for v in eps]
    assert days == [start + timedelta(days=i) for i in range(4)]
    with pytest.raises(ValueError):
        pipeline.create_series_plan(channel, 1, "short")


def test_series_slots_count_toward_daily_quota(channel):
    from darkgen import db, pipeline, service
    from darkgen.config import tz_of

    service.update_channel(channel["id"], settings={"shorts_per_day": 2, "long_per_day": 0, "plan_days_ahead": 3})
    tomorrow = datetime.now(tz_of(channel)).date() + timedelta(days=1)
    pipeline.create_series_plan(channel, 2, "short", topic="x", start_day=tomorrow)
    pipeline.plan_channel(db.get_channel(channel["id"]))
    tz = tz_of(channel)
    per_day = {}
    for v in db.list_videos(channel_id=channel["id"], limit=100):
        d = db.parse_iso(v["scheduled_at"]).astimezone(tz).date()
        per_day[d] = per_day.get(d, 0) + 1
    assert per_day[tomorrow] == 2 and per_day[tomorrow + timedelta(days=1)] == 2


def test_from_script_needs_no_claude_and_composes_descriptions(channel):
    from darkgen import db, service

    s = service.create_series_from_script(channel["id"], "A casa", _episodes(3), logline="Uma casa que respira.",
                                          is_fiction=True)
    assert s["status"] == "ready" and len(s["episodes"]) == 3
    v = db.get_video(s["episodes"][0]["id"])
    assert v["status"] == "scripted" and v["description"].startswith("⚠️") and "#shorts" in v["description"]
    with pytest.raises(service.ServiceError):
        service.create_series(channel["id"], 3)  # sem API key no servidor
    with pytest.raises(service.ServiceError):
        service.create_series_from_script(channel["id"], "x", _episodes(1))


def test_worker_writes_series_before_single_videos(channel, monkeypatch):
    from darkgen import db, pipeline
    from darkgen.content import writer

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    single = db.create_video(channel["id"], "short", status="queued",
                             scheduled_at=datetime.now(timezone.utc) + timedelta(hours=3))
    srs = pipeline.create_series_plan(channel, 3, "short")
    calls = []

    class Msgs:
        def parse(self, **kw):  # ideia para a série
            calls.append("idea")
            assert "SÉRIE em 3 partes" in kw["messages"][0]["content"]
            idea = writer.Idea(topic="A casa da rua 8", category="creepypasta", angle="a", hook="h", is_fiction=True)
            return SimpleNamespace(stop_reason="end_turn", parsed_output=writer.IdeaList(ideas=[idea]))

        @contextmanager
        def stream(self, **kw):
            calls.append("series")
            assert "Exatamente 3 episódios" in kw["messages"][0]["content"]
            out = writer.SeriesScript(series_title="A casa da rua 8", logline="l", is_fiction=True,
                                      episodes=[writer.SeriesEpisode(**{**e, "scenes": [writer.Scene(**sc) for sc in e["scenes"]]})
                                                for e in _episodes(3)])
            yield SimpleNamespace(get_final_message=lambda: SimpleNamespace(stop_reason="end_turn", parsed_output=out))

    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Msgs())))
    monkeypatch.setattr(pipeline, "_stage_render", lambda v, ch: None)
    monkeypatch.setattr(pipeline, "_stage_script", lambda v, ch: calls.append(("single", v["id"])))
    monkeypatch.setattr(pipeline, "_next_upload", lambda: None)
    db.update_video(single["id"], status="queued")

    assert pipeline.work_tick() is True
    assert calls == ["idea", "series"]  # sem pesquisa: é ficção
    s = db.get_series(srs["id"])
    assert s["status"] == "ready" and s["title"] == "A casa da rua 8"
    assert all(v["status"] == "scripted" for v in db.episodes(srs["id"]))

    # episódios agora estão "scripted"; o render foi trocado por stub, então o próximo tick
    # renderiza um episódio, e o roteiro avulso só roda quando não há mais nada antes
    for _ in range(4):
        pipeline.work_tick()
    assert ("single", single["id"]) in calls


def test_upload_order_previous_link_and_playlist(channel, monkeypatch):
    from darkgen import db, pipeline, service
    from darkgen.youtube import client as yt

    s = service.create_series_from_script(channel["id"], "A casa", _episodes(2))
    ep1, ep2 = db.episodes(s["id"])
    for v in (ep1, ep2):
        db.update_video(v["id"], status="rendered", video_path="/tmp/x.mp4")

    assert pipeline._series_blocked(db.get_video(ep2["id"])) is True
    assert pipeline._series_blocked(db.get_video(ep1["id"])) is False

    uploads, playlist_items = [], []
    monkeypatch.setattr(yt, "upload", lambda cid, path, **kw: uploads.append(kw) or f"yt{len(uploads)}")
    monkeypatch.setattr(yt, "create_playlist", lambda cid, title, desc, lang: "PL1")
    monkeypatch.setattr(yt, "add_to_playlist", lambda cid, pl, vid, pos: playlist_items.append((pl, vid, pos)))
    ch = db.get_channel(channel["id"])
    pipeline._stage_upload(db.get_video(ep1["id"]), ch)
    pipeline._stage_upload(db.get_video(ep2["id"]), ch)

    assert "Parte anterior: https://youtu.be/yt1" in uploads[1]["description"]
    assert "playlist?list=PL1" in uploads[1]["description"]
    assert playlist_items == [("PL1", "yt1", 0), ("PL1", "yt2", 1)]
    t1, t2 = (db.parse_iso(db.get_video(v["id"])["scheduled_at"]) for v in (ep1, ep2))
    assert t2 > t1


def test_missing_playlist_scope_only_warns(channel, monkeypatch):
    from darkgen import db, pipeline, service
    from darkgen.youtube import client as yt

    s = service.create_series_from_script(channel["id"], "A casa", _episodes(2))
    ep1 = db.episodes(s["id"])[0]
    db.update_video(ep1["id"], status="rendered", video_path="/tmp/x.mp4")
    monkeypatch.setattr(yt, "upload", lambda cid, path, **kw: "yt1")
    monkeypatch.setattr(yt, "can_manage_playlists", lambda cid: False)
    pipeline._stage_upload(db.get_video(ep1["id"]), db.get_channel(channel["id"]))
    assert db.get_video(ep1["id"])["status"] == "scheduled"
    assert any("reconecte" in a["message"].lower() for a in db.recent_activity(channel_id=channel["id"]))


def test_out_of_order_slot_is_rescheduled_after_previous(channel, monkeypatch):
    from darkgen import db, pipeline, service
    from darkgen.youtube import client as yt

    s = service.create_series_from_script(channel["id"], "A casa", _episodes(2))
    ep1, ep2 = db.episodes(s["id"])
    later = datetime.now(timezone.utc) + timedelta(days=5)
    db.update_video(ep1["id"], youtube_id="yt1", status="scheduled", scheduled_at=later)
    db.update_video(ep2["id"], status="rendered", video_path="/tmp/x.mp4")
    monkeypatch.setattr(yt, "upload", lambda cid, path, **kw: "yt2")
    monkeypatch.setattr(yt, "can_manage_playlists", lambda cid: False)
    pipeline._stage_upload(db.get_video(ep2["id"]), db.get_channel(channel["id"]))
    assert db.parse_iso(db.get_video(ep2["id"])["scheduled_at"]) > later


def test_auto_series_respects_interval(channel, monkeypatch):
    from darkgen import db, pipeline

    ch = db.get_channel(channel["id"])
    assert ch["settings"]["auto_series"] is True  # vem do modelo contos_terror
    first = pipeline.maybe_start_series(ch)
    assert first and first["parts"] == ch["settings"]["series_parts"]
    assert pipeline.maybe_start_series(ch) is None  # já existe uma na fila
    db.update_series(first["id"], status="ready")
    assert pipeline.maybe_start_series(ch) is None  # dentro do intervalo de dias


def test_cancel_and_health(channel):
    from darkgen import db, pipeline, service

    s = service.create_series_from_script(channel["id"], "A casa", _episodes(3))
    ep1, ep2, ep3 = db.episodes(s["id"])
    db.update_video(ep1["id"], youtube_id="yt1", status="scheduled")
    db.update_video(ep2["id"], status="failed", error="x")
    h = service.channel_health(channel["id"], check_token=False)
    assert any(i["code"] == "series_blocked" for i in h["issues"])
    pipeline.cancel_series(s["id"])
    assert [v["id"] for v in db.episodes(s["id"])] == [ep1["id"]]  # o enviado fica
    assert db.get_series(s["id"])["status"] == "cancelled"
