"""Mininovela, vozes (ElevenLabs simulada) e publicação manual nas redes."""

from __future__ import annotations

import base64
import json
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TTS_PROVIDER", "silent")
    for k in ("ANTHROPIC_API_KEY", "DASHBOARD_TOKEN", "ELEVENLABS_API_KEY", "PEXELS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    from darkgen import config, db

    config.env.cache_clear()
    db._initialized = False
    yield
    config.env.cache_clear()


@pytest.fixture
def channel():
    from darkgen import service

    return service.create_channel("Novelas", preset="mininovela_suspense")


CHARACTERS = [
    {"name": "Helena", "gender": "female", "age": "35", "role": "mãe", "personality": "firme"},
    {"name": "Caio", "gender": "male", "age": "17", "role": "filho", "personality": "curioso"},
    {"name": "Marta", "gender": "female", "age": "70", "role": "avó", "personality": "misteriosa"},
]


def _episodes(n):
    return [{
        "title": f"A casa da avó (Parte {i})", "description": f"Capítulo {i} #novela #suspense", "tags": ["mininovela"],
        "thumbnail_text": "ELA SABIA",
        "scenes": [
            {"lines": [{"speaker": "NARRADOR", "text": "Naquela noite, a casa estava em silêncio."},
                       {"speaker": "Helena", "text": "Caio, quem abriu essa porta?"}], "visual_query": "dark house"},
            {"lines": [{"speaker": "Caio", "text": "Não fui eu, mãe."},
                       {"speaker": "Marta", "text": "Ela sempre abre sozinha."}], "visual_query": "old door"},
        ],
    } for i in range(1, n + 1)]


# ---------------------------------------------------------------- vozes

def test_alignment_to_words():
    from darkgen.media.tts import alignment_to_words

    chars = list("Oi, tudo bem?")
    starts = [i * 0.1 for i in range(len(chars))]
    ends = [s + 0.1 for s in starts]
    words = alignment_to_words(chars, starts, ends, offset=1.0)
    assert [w.text for w in words] == ["Oi,", "tudo", "bem?"]
    assert words[0].start == pytest.approx(1.0) and words[1].start == pytest.approx(1.4)
    assert words[-1].end == pytest.approx(2.3)


def _mp3_b64(tmp_path, seconds=1.0) -> str:
    p = tmp_path / "s.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(seconds),
                    "-c:a", "libmp3lame", str(p)], check=True)
    return base64.b64encode(p.read_bytes()).decode()


def test_eleven_tts_request_and_cache(tmp_path, monkeypatch):
    from darkgen import config
    from darkgen.media import tts

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    config.env.cache_clear()
    calls = []
    audio = _mp3_b64(tmp_path)
    text = "Olá mundo"

    def fake_post(url, params=None, headers=None, json=None, timeout=None):
        calls.append((url, params, headers, json))
        chars = list(text)
        return SimpleNamespace(status_code=200, json=lambda: {
            "audio_base64": audio,
            "alignment": {"characters": chars, "character_start_times_seconds": [i * 0.05 for i in range(len(chars))],
                          "character_end_times_seconds": [i * 0.05 + 0.05 for i in range(len(chars))]}})

    monkeypatch.setattr(tts.httpx, "post", fake_post)
    settings = {"eleven_model": "eleven_multilingual_v2", "eleven_stability": 0.4, "language": "pt-BR"}
    w1 = tts.eleven_tts(text, "VOICE1", tmp_path / "a.mp3", settings)
    w2 = tts.eleven_tts(text, "VOICE1", tmp_path / "b.mp3", settings)
    assert len(calls) == 1, "a segunda chamada deve vir do cache (não pagar de novo)"
    url, params, headers, body = calls[0]
    assert url.endswith("/v1/text-to-speech/VOICE1/with-timestamps")
    assert params == {"output_format": "mp3_44100_128"} and headers["xi-api-key"] == "k"
    assert body["model_id"] == "eleven_multilingual_v2" and body["voice_settings"]["stability"] == 0.4
    assert "language_code" not in body
    assert [w.text for w in w1] == ["Olá", "mundo"] == [w.text for w in w2]
    assert (tmp_path / "b.mp3").stat().st_size > 0


def test_provider_resolution(monkeypatch):
    from darkgen import config
    from darkgen.media import tts

    monkeypatch.setenv("TTS_PROVIDER", "edge")
    config.env.cache_clear()
    assert tts.resolve_provider({"tts_provider": "auto"}) == "edge"
    with pytest.raises(RuntimeError):
        tts.resolve_provider({"tts_provider": "elevenlabs"})
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    config.env.cache_clear()
    assert tts.resolve_provider({"tts_provider": "auto"}) == "elevenlabs"
    assert tts.resolve_provider({"tts_provider": "edge"}) == "edge"


def test_voice_assignment_is_stable_per_character(channel):
    from darkgen.media.voices import assign_voices, voice_for

    s = channel["settings"]
    voices = assign_voices(CHARACTERS, s)
    assert voices["Helena"] in s["eleven_female_voices"] and voices["Marta"] in s["eleven_female_voices"]
    assert voices["Helena"] != voices["Marta"]  # alterna dentro do gênero
    assert voices["Caio"] in s["eleven_male_voices"]
    bible = {"characters": CHARACTERS, "voices": voices}
    assert voice_for("NARRADOR", "elevenlabs", s, bible) == s["eleven_narrator_voice"]
    assert voice_for("Caio", "elevenlabs", s, bible) == voices["Caio"]
    assert voice_for("Helena", "edge", s, bible) == s["edge_female_voice"]
    assert voice_for("Caio", "edge", s, bible) == s["voice"]


def test_synthesize_lines_segments_in_order(tmp_path):
    from darkgen.media import tts

    lines = [tts.Line("NARRADOR", "Era uma vez uma casa.", "v"), tts.Line("Helena", "Quem está aí?", "v")]
    n = tts.synthesize_lines(lines, tmp_path / "out.mp3", {"tts_provider": "auto"})
    assert [s.speaker for s in n.segments] == ["NARRADOR", "Helena"]
    assert n.segments[1].start > n.segments[0].end
    assert n.words[-1].text == "aí?" and n.words[-1].start >= n.segments[1].start
    assert n.duration >= n.segments[-1].end


# ---------------------------------------------------------------- novela

def test_novela_from_script_and_render(channel, tmp_path):
    from darkgen import db, pipeline, service

    with pytest.raises(service.ServiceError):
        bad = _episodes(2)
        bad[0]["scenes"][0]["lines"][1]["speaker"] = "Desconhecido"
        service.create_series_from_script(channel["id"], "X", bad, characters=CHARACTERS)

    s = service.create_series_from_script(channel["id"], "A casa da avó", _episodes(2), characters=CHARACTERS,
                                          logline="Uma avó que sabe demais.")
    srs = db.get_series(s["id"])
    assert srs["format"] == "novela" and srs["is_fiction"] is True
    assert set(srs["bible"]["voices"]) == {"Helena", "Caio", "Marta"}

    service.update_channel(channel["id"], settings={"min_duration_s": 6})
    ch = db.get_channel(channel["id"])
    ep1 = db.episodes(s["id"])[0]
    pipeline._stage_render(db.get_video(ep1["id"]), ch)
    v = db.get_video(ep1["id"])
    assert v["status"] == "ready", "modo manual sem aprovação: vai direto para a fila de postagem"
    assert v["duration_s"] >= 6
    ass = (tmp_path / "videos" / f"{v['id']:05d}" / "subs.ass").read_text()
    speaker_lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:") and ",Speaker," in ln]
    assert any("HELENA" in ln for ln in speaker_lines) and any("MARTA" in ln for ln in speaker_lines)
    assert not any("NARRADOR" in ln for ln in speaker_lines), "narrador não ganha letreiro de nome"
    assert "PARTE 1" in ass and "CONTINUA NA PARTE 2" in ass


def test_novela_written_by_server(channel, monkeypatch):
    from darkgen import db, pipeline
    from darkgen.content import writer

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    srs = pipeline.create_series_plan(channel, 3, "short", topic="Uma herança maldita")
    assert srs["format"] == "novela"

    class Msgs:
        @contextmanager
        def stream(self, **kw):
            prompt = kw["messages"][0]["content"]
            assert "MININOVELA" in prompt and "61 a 110 segundos" in prompt
            out = writer.NovelaSeries(
                series_title="Herança", logline="l",
                characters=[writer.NovelaCharacter(**c) for c in CHARACTERS],
                episodes=[writer.NovelaEpisode(**{**e, "scenes": [
                    writer.NovelaScene(lines=[writer.NovelaLine(**ln) for ln in sc["lines"]] +
                                       [writer.NovelaLine(speaker="Fantasma", text="Uuuh.")],
                                       visual_query=sc["visual_query"]) for sc in e["scenes"]]})
                    for e in _episodes(3)])
            yield SimpleNamespace(get_final_message=lambda: SimpleNamespace(stop_reason="end_turn", parsed_output=out))

    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Msgs())))
    pipeline._stage_series(db.get_series(srs["id"]), channel)
    s = db.get_series(srs["id"])
    assert s["status"] == "ready" and s["bible"]["voices"]
    ep = db.episodes(srs["id"])[0]
    speakers = {ln["speaker"] for sc in ep["script"]["scenes"] for ln in sc["lines"]}
    assert "Fantasma" not in speakers and "NARRADOR" in speakers  # fala fora da ficha vira narração


# ---------------------------------------------------------------- publicação manual

def test_post_kit_mark_posted_and_stats(channel):
    from darkgen import db, publishing, service

    s = service.create_series_from_script(channel["id"], "A casa da avó", _episodes(2), characters=CHARACTERS)
    ep1, ep2 = db.episodes(s["id"])
    for v in (ep1, ep2):
        db.update_video(v["id"], status="ready", video_path="/tmp/x.mp4")

    kit = publishing.post_kit(ep1["id"])
    by = {p["platform"]: p for p in kit["platforms"]}
    assert set(by) == {"tiktok", "kwai", "instagram", "youtube"}
    assert "Parte 1/2" in by["tiktok"]["caption"] and "Siga para não perder" in by["tiktok"]["caption"]
    assert len(by["instagram"]["hashtags"]) <= 5 and all(h.startswith("#") for h in by["kwai"]["hashtags"])
    assert by["youtube"]["hashtags"][0] == "#shorts" and by["youtube"]["title"].startswith("A casa da avó")
    assert "IA" in by["tiktok"]["ai_label"]
    assert kit["download_url"].endswith(f"/media/{ep1['id']}/video?download=1")

    publishing.mark_posted(ep1["id"], "tiktok", "https://tiktok.com/@x/video/1")
    assert db.get_video(ep1["id"])["status"] == "published"
    queue_ids = [k["video_id"] for k in publishing.ready_queue(channel["id"])]
    assert ep1["id"] in queue_ids and ep2["id"] in queue_ids  # ep1 ainda falta em 3 redes

    with pytest.raises(ValueError):
        publishing.update_stats(ep1["id"], "kwai", views=10)  # não postado lá
    publishing.update_stats(ep1["id"], "tiktok", views=1000, likes=50)
    publishing.mark_posted(ep2["id"], "tiktok")
    publishing.update_stats(ep2["id"], "tiktok", views=600)
    st = publishing.platform_stats(channel["id"])
    assert st["platforms"][0]["platform"] == "tiktok" and st["platforms"][0]["avg_views"] == 800
    assert st["series"][0]["retention_last_vs_first"] == 0.6

    publishing.unmark_posted(ep2["id"], "tiktok")
    assert db.get_video(ep2["id"])["status"] == "ready"


def test_manual_mode_never_uploads_and_approval_moves_to_ready(channel, monkeypatch):
    from darkgen import db, pipeline, service

    service.update_channel(channel["id"], settings={"approval_required": True})
    v = db.create_video(channel["id"], "short", status="rendered", video_path="/tmp/x.mp4")
    assert pipeline._next_upload() is None
    assert service.approve(v["id"])["status"] == "ready"
    assert json.loads(json.dumps(service.channel_health(channel["id"], check_token=False)))["issues"] is not None
