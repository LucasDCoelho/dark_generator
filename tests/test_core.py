"""Testes do núcleo (sem rede): canais, agenda, legendas, fila, migração e pipeline com Claude simulado."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TTS_PROVIDER", "silent")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    from darkgen import config, db

    config.env.cache_clear()
    db._initialized = False
    yield
    config.env.cache_clear()


@pytest.fixture
def channel():
    from darkgen import service

    return service.create_channel("Arquivos Sombrios", preset="misterios_historia")


# ---------------------------------------------------------------- canais

def test_create_channel_from_preset_and_override():
    from darkgen import service

    ch = service.create_channel("Unsolved", preset="unsolved_en", settings={"shorts_per_day": 4})
    s = ch["settings"]
    assert s["language"] == "en-US" and s["timezone"] == "America/New_York"
    assert s["shorts_per_day"] == 4
    assert s["voice"].startswith("en-US")
    with pytest.raises(service.ServiceError):
        service.create_channel("X", preset="nao_existe")
    with pytest.raises(service.ServiceError):
        service.create_channel("Y", settings={"timezone": "Marte/Base"})


def test_update_clone_pause_delete(channel):
    from darkgen import db, service

    ch = service.update_channel(channel["id"], settings={"shorts_per_day": "3", "approval_required": "true",
                                                         "default_tags": "a, b ,c"})
    assert ch["settings"]["shorts_per_day"] == 3 and ch["settings"]["approval_required"] is True
    assert ch["settings"]["default_tags"] == ["a", "b", "c"]
    with pytest.raises(service.ServiceError):
        service.update_channel(channel["id"], settings={"nao_existe": 1})

    clone = service.clone_channel(channel["id"], "Arquivos EN")
    assert clone["settings"]["niche"] == ch["settings"]["niche"] and clone["id"] != ch["id"]

    service.update_channel(channel["id"], active=False)
    assert [c["id"] for c in db.list_channels(active_only=True)] == [clone["id"]]

    service.delete_channel(clone["id"])
    assert db.get_channel(clone["id"]) is None


def test_health_flags_problems(channel):
    from darkgen import db, service

    h = service.channel_health(channel["id"], check_token=False)  # modo manual: YouTube é opcional
    assert next(i for i in h["issues"] if i["code"] == "not_connected")["severity"] == "info"
    service.update_channel(channel["id"], settings={"publish_mode": "youtube_api"})
    h = service.channel_health(channel["id"], check_token=False)
    codes = {i["code"] for i in h["issues"]}
    assert "not_connected" in codes and "no_client_secret" in codes and h["status"] == "critical"
    assert "no_claude" in codes  # piloto automático ligado sem chave

    db.create_video(channel["id"], "short", status="failed", error="x")
    h = service.channel_health(channel["id"], check_token=False)
    assert any(i["code"] == "failed_videos" for i in h["issues"])


def test_portfolio_totals(channel):
    from darkgen import service

    service.create_channel("Segundo", preset="contos_terror")
    pf = service.portfolio()
    assert pf["totals"]["channels"] == 2
    assert pf["totals"]["daily_videos"] == 6  # 2+1 em cada canal
    assert {r["name"] for r in pf["channels"]} == {"Arquivos Sombrios", "Segundo"}


# ---------------------------------------------------------------- agenda

def test_pick_slots_respect_channel_timezone_and_gap(channel):
    from darkgen import service
    from darkgen.config import tz_of
    from darkgen.scheduling import best_times

    en = service.create_channel("EN", preset="unsolved_en")
    for ch in (channel, en):
        tomorrow = datetime.now(tz_of(ch)).date() + timedelta(days=1)
        slots = best_times.pick_slots(ch, tomorrow, "short", 3)
        assert len(slots) == 3
        for a, b in zip(slots, slots[1:]):
            assert b - a >= timedelta(hours=ch["settings"]["min_gap_hours"])
        local_hours = {s.astimezone(tz_of(ch)).hour for s in slots}
        assert local_hours & {19, 20, 21, 22}, "deve priorizar o horário nobre do fuso do público"


def test_adaptive_scores_are_per_channel(channel):
    from darkgen import db, service
    from darkgen.config import tz_of
    from darkgen.scheduling import best_times

    other = service.create_channel("Outro")
    base = best_times.hour_scores("short", 0)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    for i in range(8):
        hour = 9 if i < 4 else 21
        when = old.astimezone(tz_of(channel)).replace(hour=hour, minute=0) - timedelta(days=i)
        db.create_video(channel["id"], "short", status="published", scheduled_at=when,
                        views=100_000 if hour == 9 else 100)
    perf, counts = best_times.performance_by_hour(channel, "short")
    assert perf[9] > 1 > perf[21]
    adapted = best_times.hour_scores("short", 0, perf, counts)
    assert adapted[9] > base[9] and adapted[21] < base[21]
    assert best_times.performance_by_hour(other, "short") == ({}, {})


def test_plan_fills_quota_per_channel_without_duplicates(channel):
    from darkgen import db, pipeline, service

    second = service.create_channel("Segundo", settings={"shorts_per_day": 1, "long_per_day": 0})
    service.update_channel(channel["id"], settings={"plan_days_ahead": 2})
    created = pipeline.plan()
    assert pipeline.plan() == [], "planejar duas vezes não pode duplicar"
    by_channel = {}
    for v in created:
        by_channel.setdefault(v["channel_id"], []).append(v)
    assert all(v["kind"] == "short" for v in by_channel[second["id"]])
    assert len([v for v in by_channel[channel["id"]] if v["kind"] == "short"]) >= 4

    service.update_channel(second["id"], active=False)
    db.update_channel(second["id"], settings={"plan_days_ahead": 5})
    assert all(v["channel_id"] != second["id"] for v in pipeline.plan())


# ---------------------------------------------------------------- mídia

def test_subtitle_chunks_and_channel_color(tmp_path):
    from darkgen.media import subtitles
    from darkgen.media.tts import _even_words

    words = _even_words("Em 1959, nove estudantes entraram nos Montes Urais. Nenhum voltou.", 6.0)
    chunks = subtitles.chunk_words(words, max_words=3, max_chars=18)
    assert all(len(c) <= 3 for c in chunks)
    assert chunks[0][-1].text == "1959,"
    assert subtitles.ass_color("#12ab9f") == "&H009FAB12"
    text = subtitles.build_ass(words, tmp_path / "s.ass", "short", "#12ab9f").read_text()
    assert "PlayResY: 1920" in text and "\\k" in text and "&H009FAB12" in text


def test_scene_times_cover_full_duration():
    from darkgen.media.render import scene_times_from_segments, script_lines
    from darkgen.media.tts import Segment

    scenes = [{"narration": "um dois três"}, {"lines": [{"speaker": "A", "text": "quatro"}, {"speaker": "B", "text": "cinco"}]},
              {"narration": "seis"}]
    raw = script_lines(scenes)
    assert [(sc, sp) for sc, sp, _ in raw] == [(0, "NARRADOR"), (1, "A"), (1, "B"), (2, "NARRADOR")]
    segs = [Segment(sp, i * 2.0, i * 2.0 + 1.5, i) for i, (_, sp, _) in enumerate(raw)]
    times = scene_times_from_segments(3, [sc for sc, _, _ in raw], segs, 9.0)
    assert times[0][0] == 0 and times[-1][1] == 9.0
    assert times == [(0.0, 2.0), (2.0, 6.0), (6.0, 9.0)]


# ---------------------------------------------------------------- fila e vídeos

def test_create_video_with_script_and_queue(channel):
    from darkgen import db, pipeline, service

    with pytest.raises(service.ServiceError):
        service.create_video(channel["id"], "short", topic="sem chave")
    with pytest.raises(service.ServiceError):
        service.create_video(9999, "short", title="x", scenes=[{"narration": "a"}])

    v = service.create_video(channel["id"], "short", title="Teste", is_fiction=True,
                             scenes=[{"narration": "Uma frase.", "visual_query": "fog"}],
                             scheduled_at="2030-01-01 21:00")
    assert v["status"] == "scripted" and "#shorts" in v["description"]
    assert v["description"].startswith("⚠️")
    assert v["scheduled_at"].startswith("2030-01-02T00:00")  # 21h em São Paulo (UTC-3) = 00h UTC

    assert db.transition(v["id"], "scripted", "rendering")
    assert not db.transition(v["id"], "scripted", "rendering")
    pipeline.recover_interrupted()
    assert db.get_video(v["id"])["status"] == "scripted"


def test_move_video_between_channels(channel):
    from darkgen import service

    other = service.create_channel("Outro")
    v = service.create_video(channel["id"], "short", title="T", scenes=[{"narration": "a"}])
    moved = service.move_video(v["id"], other["id"])
    assert moved["channel_id"] == other["id"]


def test_worker_skips_paused_channels(channel, monkeypatch):
    from darkgen import db, pipeline, service

    v = service.create_video(channel["id"], "short", title="T", scenes=[{"narration": "a"}])
    service.update_channel(channel["id"], active=False)
    rendered = []
    monkeypatch.setattr(pipeline, "_stage_render", lambda video, ch: rendered.append(video["id"]))
    assert pipeline.work_tick() is False and rendered == []
    service.update_channel(channel["id"], active=True)
    assert pipeline.work_tick() is True and rendered == [v["id"]]
    assert db.get_video(v["id"])["status"] == "rendering"  # o stub não avança o status


def test_quota_limits_uploads_per_project(channel, monkeypatch, tmp_path):
    from darkgen import db, pipeline
    from darkgen.config import env
    from darkgen.youtube import client as yt

    env().youtube_client_secret.write_text(json.dumps({"installed": {"client_id": "abc"}}))
    monkeypatch.setattr(yt, "is_connected", lambda cid: True)
    for _ in range(env().youtube_uploads_per_project_per_day):
        db.create_video(channel["id"], "short", status="scheduled", youtube_id="x", uploaded_at=db.now_utc())
    v = db.create_video(channel["id"], "short", status="rendered", video_path="/tmp/x.mp4")
    assert pipeline._next_upload() is None
    q = pipeline.quota_status()[yt.quota_key(channel["id"])]
    assert q["uploads_24h"] == q["limit"]
    db.update_video(v["id"], status="published")


# ---------------------------------------------------------------- migração

def test_migrates_single_channel_database(tmp_path):
    from darkgen import db
    from darkgen.config import env

    old = sqlite3.connect(env().db_path)
    old.executescript("""
        CREATE TABLE videos (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          category TEXT, topic TEXT, is_fiction INTEGER NOT NULL DEFAULT 0, title TEXT, description TEXT,
          tags TEXT NOT NULL DEFAULT '[]', script TEXT, sources TEXT NOT NULL DEFAULT '[]', thumbnail_text TEXT,
          scheduled_at TEXT, approved INTEGER NOT NULL DEFAULT 0, video_path TEXT, thumb_path TEXT, duration_s REAL,
          youtube_id TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0, views INTEGER, likes INTEGER,
          comments INTEGER, stats_updated_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
          video_id INTEGER, message TEXT NOT NULL);
        CREATE TABLE channel_snapshots (ts TEXT PRIMARY KEY, subscribers INTEGER, views INTEGER, videos INTEGER);
        INSERT INTO videos (kind, status, title, youtube_id, created_at, updated_at)
          VALUES ('short', 'published', 'Antigo', 'yt1', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        INSERT INTO settings VALUES ('shorts_per_day', '4');
        INSERT INTO settings VALUES ('_state:channel', '{"id": "UC1", "title": "Meu Canal", "subscribers": 10}');
        INSERT INTO settings VALUES ('_state:worker_heartbeat', '"x"');
        INSERT INTO activity (ts, level, message) VALUES ('2026-01-01T00:00:00+00:00', 'info', 'oi');
        INSERT INTO channel_snapshots VALUES ('2026-01-01T00:00:00+00:00', 10, 100, 1);
    """)
    old.commit()
    old.close()
    (env().data_dir / "youtube_token.json").write_text("{}")

    chans = db.list_channels()
    assert len(chans) == 1
    ch = chans[0]
    assert ch["name"] == "Meu Canal" and ch["youtube_channel_id"] == "UC1"
    assert ch["settings"]["shorts_per_day"] == 4
    v = db.list_videos()[0]
    assert v["channel_id"] == ch["id"] and v["uploaded_at"]
    assert db.recent_activity(channel_id=ch["id"])[0]["message"] == "oi"
    assert len(db.channel_stats(ch["id"])) == 1
    assert (env().channels_dir / str(ch["id"]) / "youtube_token.json").exists()
    assert db.get_state("worker_heartbeat") == "x"


# ---------------------------------------------------------------- Claude e YouTube simulados

def test_stage_script_uses_channel_profile(channel, monkeypatch):
    from darkgen import db, pipeline
    from darkgen.content import writer

    script = writer.VideoScript(
        title="O farol vazio", description="Três homens sumiram.", tags=["farol"],
        thumbnail_text="ONDE ESTÃO?", is_fiction=False,
        scenes=[writer.Scene(narration="Em 1900, três faroleiros sumiram.", visual_query="lighthouse storm")],
    )
    systems = []

    class FakeMessages:
        def parse(self, **kw):
            systems.append(kw["system"])
            assert kw["thinking"] == {"type": "adaptive"}
            if kw["output_format"] is writer.IdeaList:
                idea = writer.Idea(topic="Farol de Flannan", category="misterio", angle="a", hook="h", is_fiction=False)
                return SimpleNamespace(stop_reason="end_turn", parsed_output=writer.IdeaList(ideas=[idea]))
            return SimpleNamespace(stop_reason="end_turn", parsed_output=script)

        def create(self, **kw):
            assert kw["tools"][0]["type"].startswith("web_search")
            block = SimpleNamespace(type="text", text="Dossiê.", citations=[SimpleNamespace(url="https://exemplo.org")])
            return SimpleNamespace(stop_reason="end_turn", content=[block])

    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))
    v = db.create_video(channel["id"], "short", status="scripting")
    pipeline._stage_script(v, channel)
    v = db.get_video(v["id"])
    assert v["status"] == "scripted" and v["topic"] == "Farol de Flannan"
    assert v["sources"] == ["https://exemplo.org"] and "https://exemplo.org" in v["description"]
    assert all("Arquivos Sombrios" in s and "português" in s for s in systems)


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

    monkeypatch.setattr(yt, "_yt", lambda cid: SimpleNamespace(videos=lambda: Videos()))
    monkeypatch.setattr(yt, "MediaFileUpload", lambda *a, **k: None)
    when = datetime.now(timezone.utc) + timedelta(days=1)
    vid = yt.upload(1, tmp_path / "x.mp4", title="T" * 150, description="<b>oi</b>", tags=["a", "b"],
                    category_id="24", language="en-US", publish_at=when)
    assert vid == "vid123"
    body = captured["body"]
    assert body["status"]["privacyStatus"] == "private" and body["status"]["publishAt"].endswith("Z")
    assert len(body["snippet"]["title"]) == 100 and "<" not in body["snippet"]["description"]
    assert body["snippet"]["defaultAudioLanguage"] == "en-US"


# ---------------------------------------------------------------- tendências

class _Exec:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self.data


def _fake_youtube(now):
    pub = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    vids = {
        "v1": ("small", 2_000, 120_000, "PT58S"),   # canal pequeno explodindo -> outlier
        "v2": ("big", 5_000_000, 300_000, "PT12M"),  # canal grande
        "v3": ("small", 2_000, 900, "PT40S"),
    }

    class Search:
        def list(self, **kw):
            assert kw["order"] == "viewCount" and kw["regionCode"] == "BR"
            ids = ["v1", "v2", "v3"] if kw["q"] == "mistério" else ["v2"]
            return _Exec({"items": [{"id": {"videoId": i}} for i in ids]})

    class Videos:
        def list(self, **kw):
            return _Exec({"items": [{
                "id": i, "snippet": {"title": f"T{i}", "channelId": vids[i][0], "channelTitle": vids[i][0],
                                      "publishedAt": pub, "thumbnails": {}},
                "statistics": {"viewCount": str(vids[i][2])}, "contentDetails": {"duration": vids[i][3]},
            } for i in kw["id"].split(",")]})

    class Channels:
        def list(self, **kw):
            subs = {"small": 2_000, "big": 5_000_000}
            return _Exec({"items": [{"id": c, "statistics": {"subscriberCount": str(subs[c])}} for c in kw["id"].split(",")]})

    return SimpleNamespace(search=lambda: Search(), videos=lambda: Videos(), channels=lambda: Channels())


def test_scan_youtube_scores_outliers(monkeypatch):
    from darkgen import trends

    monkeypatch.setattr(trends, "_youtube_client", lambda: _fake_youtube(datetime.now(timezone.utc)))
    r = trends.scan_youtube(["mistério", "backrooms"], region="BR", language="pt", days=7)
    by_kw = {k["keyword"]: k for k in r["keywords"]}
    m = by_kw["mistério"]
    assert m["outlier_videos"] == 1 and m["top_outliers"][0]["id"] == "v1"
    assert m["top_outliers"][0]["is_short"] is True
    assert m["opportunity"] > by_kw["backrooms"]["opportunity"]  # só canal grande = menos oportunidade
    assert r["keywords"][0]["keyword"] == "mistério"  # ordenado por oportunidade
    saved = trends.get_scan(r["scan_id"])
    assert saved["params"]["keywords"] == ["mistério", "backrooms"]

    r2 = trends.scan_youtube(["mistério", "backrooms"], region="BR", language="pt", days=7)
    diff = trends.compare_scans(r["scan_id"], r2["scan_id"])
    assert {d["keyword"] for d in diff} == {"mistério", "backrooms"} and all(d["delta_opportunity"] == 0 for d in diff)
    with pytest.raises(ValueError):
        trends.scan_youtube([" "])


def test_iso_duration():
    from darkgen.trends import _iso_duration_seconds

    assert _iso_duration_seconds("PT1M5S") == 65
    assert _iso_duration_seconds("PT1H2M") == 3720
    assert _iso_duration_seconds("P1DT1S") == 86401
    assert _iso_duration_seconds("") == 0


def test_scan_social_structures_research(monkeypatch):
    from darkgen import trends
    from darkgen.content import writer

    class FakeMessages:
        def create(self, **kw):
            assert kw["tools"][0]["type"].startswith("web_search")
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="notas")])

        def parse(self, **kw):
            out = trends.SocialTrendList(summary="ok", trends=[trends.SocialTrend(
                platform="kwai", topic="mininovelas de suspense", format="série vertical", evidence="100 bi views",
                source_url="https://x", dark_fit=True, confidence="alta")])
            return SimpleNamespace(stop_reason="end_turn", parsed_output=out)

    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))
    r = trends.scan_social(["kwai"], focus="terror")
    assert r["trends"][0]["platform"] == "kwai" and trends.get_scan(r["scan_id"])["results"]["summary"] == "ok"


def test_new_presets_are_valid():
    from darkgen import service

    for p in service.list_presets():
        ch = service.create_channel(f"Teste {p['key']}", preset=p["key"])
        assert ch["settings"]["niche"]
