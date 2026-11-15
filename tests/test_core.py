"""Testes do núcleo (sem rede): agenda, legendas, fila, serviço e pipeline com Claude simulado."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TTS_PROVIDER", "silent")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    from darkgen import config, db

    config.env.cache_clear()
    db._initialized = False
    yield
    config.env.cache_clear()


def test_pick_slots_respects_gap_and_lead():
    from darkgen.config import env
    from darkgen.scheduling import best_times

    tomorrow = datetime.now(env().tz).date() + timedelta(days=1)
    slots = best_times.pick_slots(tomorrow, "short", 3, min_gap_hours=3)
    assert len(slots) == 3
    for a, b in zip(slots, slots[1:]):
        assert b - a >= timedelta(hours=3)
    assert all(s > datetime.now(timezone.utc) for s in slots)
    local_hours = {s.astimezone(env().tz).hour for s in slots}
    assert local_hours & {19, 20, 21, 22}, "deve priorizar o horário nobre"


def test_heatmap_shape():
    from darkgen.scheduling import best_times

    hm = best_times.heatmap("long")
    assert len(hm["scores"]) == 7 and all(len(r) == 24 for r in hm["scores"])
    assert hm["adaptive"] is False


def test_adaptive_scores_shift_with_performance():
    from darkgen import db
    from darkgen.config import env
    from darkgen.scheduling import best_times

    base = best_times.hour_scores("short", 0)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    for i in range(8):
        hour = 9 if i < 4 else 21
        when = old.astimezone(env().tz).replace(hour=hour, minute=0) - timedelta(days=i)
        db.create_video("short", status="published", scheduled_at=when, views=100_000 if hour == 9 else 100)
    perf, counts = best_times.performance_by_hour("short")
    assert perf[9] > 1 > perf[21]
    adapted = best_times.hour_scores("short", 0, perf, counts)
    assert adapted[9] > base[9]
    assert adapted[21] < base[21]


def test_subtitle_chunks_and_ass(tmp_path):
    from darkgen.media import subtitles
    from darkgen.media.tts import _even_words

    words = _even_words("Em 1959, nove estudantes entraram nos Montes Urais. Nenhum voltou.", 6.0)
    chunks = subtitles.chunk_words(words, max_words=3, max_chars=18)
    assert all(len(c) <= 3 for c in chunks)
    assert chunks[0][-1].text == "1959,"  # quebra na pontuação
    out = subtitles.build_ass(words, tmp_path / "s.ass", "short")
    text = out.read_text()
    assert "PlayResY: 1920" in text and "\\k" in text


def test_scene_times_cover_full_duration():
    from darkgen.media.render import scene_times
    from darkgen.media.tts import _even_words

    scenes = [{"narration": "um dois três"}, {"narration": "quatro cinco"}, {"narration": "seis"}]
    words = _even_words("um dois três quatro cinco seis", 6.0)
    times = scene_times(scenes, words, 7.0)
    assert times[0][0] == 0
    assert times[-1][1] == 7.0
    for (a0, a1), (b0, _) in zip(times, times[1:]):
        assert a1 == b0 and a1 > a0


def test_service_create_with_script_and_queue_flow():
    from darkgen import db, pipeline, service

    with pytest.raises(service.ServiceError):
        service.create_video("short", topic="sem chave")  # sem API key e sem roteiro

    v = service.create_video(
        "short", title="Teste", scenes=[{"narration": "Uma frase.", "visual_query": "fog"}],
        scheduled_at="2030-01-01 21:00",
    )
    assert v["status"] == "scripted"
    assert "#shorts" in v["description"]
    assert v["scheduled_at"].startswith("2030-01-02T00:00")  # 21h em São Paulo (UTC-3) = 00h UTC

    claimed = db.claim_next("scripted", "rendering")
    assert claimed["id"] == v["id"]
    assert db.claim_next("scripted", "rendering") is None
    pipeline.recover_interrupted()
    assert db.get_video(v["id"])["status"] == "scripted"


def test_settings_validation():
    from darkgen import db

    s = db.update_settings({"shorts_per_day": "3", "approval_required": 1})
    assert s["shorts_per_day"] == 3 and s["approval_required"] is True
    with pytest.raises(ValueError):
        db.update_settings({"nao_existe": 1})


def test_plan_fills_daily_quota():
    from darkgen import db, pipeline

    db.update_settings({"shorts_per_day": 2, "long_per_day": 1, "plan_days_ahead": 2})
    created = pipeline.plan()
    again = pipeline.plan()
    assert again == [], "planejar duas vezes não pode duplicar"
    tomorrow_items = [v for v in created if v["kind"] == "short"]
    assert len(tomorrow_items) >= 4  # amanhã e depois de amanhã com 2 shorts cada


def test_stage_script_with_fake_claude(monkeypatch):
    from darkgen import db, pipeline
    from darkgen.content import writer

    script = writer.VideoScript(
        title="O farol vazio", description="Três homens sumiram.", tags=["farol"],
        thumbnail_text="ONDE ESTÃO?", is_fiction=False,
        scenes=[writer.Scene(narration="Em 1900, três faroleiros sumiram.", visual_query="lighthouse storm")],
    )

    class FakeMessages:
        def parse(self, **kw):
            fmt = kw["output_format"]
            assert kw["model"] and kw["thinking"] == {"type": "adaptive"}
            if fmt is writer.IdeaList:
                idea = writer.Idea(topic="Farol de Flannan", category="misterio", angle="a",
                                   hook="h", is_fiction=False)
                parsed = writer.IdeaList(ideas=[idea])
            else:
                parsed = script
            return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed)

        def create(self, **kw):
            assert kw["tools"][0]["type"].startswith("web_search")
            block = SimpleNamespace(type="text", text="Dossiê.", citations=[SimpleNamespace(url="https://exemplo.org")])
            return SimpleNamespace(stop_reason="end_turn", content=[block])

    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))
    v = db.create_video("short", status="scripting")
    pipeline._stage_script(v, db.get_settings())
    v = db.get_video(v["id"])
    assert v["status"] == "scripted"
    assert v["topic"] == "Farol de Flannan"
    assert v["sources"] == ["https://exemplo.org"]
    assert "https://exemplo.org" in v["description"]
    assert v["script"]["scenes"][0]["visual_query"] == "lighthouse storm"


def test_youtube_upload_body(monkeypatch, tmp_path):
    from darkgen.youtube import client as yt

    captured = {}

    class Req:
        def next_chunk(self):
            return None, {"id": "vid123"}

    class Videos:
        def insert(self, **kw):
            captured.update(kw)
            return Req()

    monkeypatch.setattr(yt, "_yt", lambda: SimpleNamespace(videos=lambda: Videos()))
    monkeypatch.setattr(yt, "MediaFileUpload", lambda *a, **k: None)
    when = datetime.now(timezone.utc) + timedelta(days=1)
    vid = yt.upload(tmp_path / "x.mp4", title="T" * 150, description="<b>oi</b>", tags=["a", "b"],
                    category_id="24", publish_at=when)
    assert vid == "vid123"
    body = captured["body"]
    assert body["status"]["privacyStatus"] == "private" and body["status"]["publishAt"].endswith("Z")
    assert len(body["snippet"]["title"]) == 100
    assert "<" not in body["snippet"]["description"]
