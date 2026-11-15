"""Narração e diálogos por TTS, com tempos por palavra (para legendas sincronizadas).

Provedores:
- elevenlabs: vozes naturais, uma por personagem (precisa de ELEVENLABS_API_KEY);
- edge: edge-tts gratuito (reserva);
- silent: áudio mudo com tempos estimados (testes, sem rede).

Cada fala é sintetizada separadamente com a voz do seu personagem e depois as falas são emendadas
com uma pausa curta. O resultado traz os tempos de cada palavra e de cada fala (quem fala quando).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import env

ELEVEN_URL = "https://api.elevenlabs.io"
PAUSE_S = 0.28  # pausa entre falas


@dataclass
class Word:
    text: str
    start: float  # segundos
    end: float


@dataclass
class Line:
    speaker: str
    text: str
    voice: str  # id de voz do provedor


@dataclass
class Segment:
    speaker: str
    start: float
    end: float
    line_index: int


@dataclass
class Narration:
    audio_path: Path
    duration: float
    words: list[Word]
    segments: list[Segment] = field(default_factory=list)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _even_words(text: str, duration: float, offset: float = 0.0) -> list[Word]:
    """Distribui as palavras proporcionalmente ao número de letras (sem tempos do provedor)."""
    tokens = text.split()
    if not tokens:
        return []
    weights = [len(t) + 2 for t in tokens]
    total = sum(weights)
    words, t = [], offset
    for tok, w in zip(tokens, weights):
        d = duration * w / total
        words.append(Word(tok, t, t + d))
        t += d
    return words


def resolve_provider(settings: dict) -> str:
    if env().tts_provider == "silent":
        return "silent"
    choice = settings.get("tts_provider", "auto")
    if choice == "elevenlabs" or (choice == "auto" and env().elevenlabs_api_key):
        if not env().elevenlabs_api_key:
            raise RuntimeError("Canal configurado para ElevenLabs, mas ELEVENLABS_API_KEY não está definida.")
        return "elevenlabs"
    return "edge" if choice in ("auto", "edge") else choice


# ---------------------------------------------------------------- ElevenLabs

def alignment_to_words(chars: list[str], starts: list[float], ends: list[float], offset: float = 0.0) -> list[Word]:
    """Converte o alinhamento por caractere da ElevenLabs em palavras com início e fim."""
    words: list[Word] = []
    buf, w_start, w_end = "", None, 0.0
    for ch, st, en in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                words.append(Word(buf, offset + w_start, offset + w_end))
            buf, w_start = "", None
            continue
        if w_start is None:
            w_start = st
        buf += ch
        w_end = en
    if buf:
        words.append(Word(buf, offset + w_start, offset + w_end))
    return words


def _eleven_cache_key(text: str, voice: str, settings: dict) -> str:
    payload = json.dumps([text, voice, settings.get("eleven_model"), settings.get("eleven_stability"),
                          settings.get("eleven_similarity"), settings.get("eleven_style"),
                          settings.get("eleven_speed")], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def eleven_tts(text: str, voice: str, out: Path, settings: dict) -> list[Word]:
    """Sintetiza uma fala na ElevenLabs (com cache em disco) e devolve as palavras com tempos."""
    cache_dir = env().data_dir / "tts_cache"
    cache_dir.mkdir(exist_ok=True)
    key = _eleven_cache_key(text, voice, settings)
    audio_cache, align_cache = cache_dir / f"{key}.mp3", cache_dir / f"{key}.json"
    if audio_cache.exists() and align_cache.exists():
        data = json.loads(align_cache.read_text())
        out.write_bytes(audio_cache.read_bytes())
    else:
        body = {
            "text": text,
            "model_id": settings.get("eleven_model", "eleven_multilingual_v2"),
            "voice_settings": {
                "stability": settings.get("eleven_stability", 0.45),
                "similarity_boost": settings.get("eleven_similarity", 0.8),
                "style": settings.get("eleven_style", 0.25),
                "use_speaker_boost": True,
                "speed": settings.get("eleven_speed", 1.0),
            },
        }
        lang = (settings.get("language") or "").split("-")[0].lower()
        if lang and settings.get("eleven_model", "").startswith(("eleven_turbo_v2_5", "eleven_flash_v2_5")):
            body["language_code"] = lang  # só modelos que aceitam forçar idioma
        resp = httpx.post(
            f"{ELEVEN_URL}/v1/text-to-speech/{voice}/with-timestamps",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": env().elevenlabs_api_key, "Content-Type": "application/json"},
            json=body, timeout=120,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"ElevenLabs {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        audio = base64.b64decode(payload["audio_base64"])
        align = payload.get("alignment") or payload.get("normalized_alignment") or {}
        data = {k: align.get(k, []) for k in ("characters", "character_start_times_seconds",
                                               "character_end_times_seconds")}
        out.write_bytes(audio)
        audio_cache.write_bytes(audio)
        align_cache.write_text(json.dumps(data, ensure_ascii=False))
    words = alignment_to_words(data["characters"], data["character_start_times_seconds"],
                               data["character_end_times_seconds"])
    return words or _even_words(text, probe_duration(out))


def list_eleven_voices(search: str | None = None) -> list[dict]:
    """Vozes da conta ElevenLabs (inclui as da Voice Library adicionadas à conta)."""
    if not env().elevenlabs_api_key:
        raise RuntimeError("Defina ELEVENLABS_API_KEY para listar as vozes.")
    params: dict = {"page_size": 100}
    if search:
        params["search"] = search
    out: list[dict] = []
    while True:
        resp = httpx.get(f"{ELEVEN_URL}/v2/voices", params=params,
                         headers={"xi-api-key": env().elevenlabs_api_key}, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"ElevenLabs {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for v in data.get("voices", []):
            labels = v.get("labels") or {}
            out.append({
                "voice_id": v["voice_id"], "name": v.get("name"), "category": v.get("category"),
                "gender": labels.get("gender"), "age": labels.get("age"), "accent": labels.get("accent"),
                "description": v.get("description") or labels.get("description"),
                "languages": sorted({lang.get("language") for lang in v.get("verified_languages") or [] if lang.get("language")}),
                "preview_url": v.get("preview_url"),
            })
        if not data.get("has_more") or not data.get("next_page_token"):
            return out
        params["next_page_token"] = data["next_page_token"]


# ---------------------------------------------------------------- edge-tts e silencioso

async def _edge(text: str, out: Path, voice: str, rate: str, pitch: str) -> list[Word]:
    import edge_tts

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary", proxy=proxy)
    words: list[Word] = []
    with open(out, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 10_000_000
                words.append(Word(chunk["text"], start, start + chunk["duration"] / 10_000_000))
    return words


def _silent(text: str, out: Path) -> None:
    seconds = max(1.0, len(text.split()) / 2.6)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", f"{seconds:.2f}", "-c:a", "libmp3lame", "-q:a", "6", str(out)],
        check=True,
    )


def _synth_one(provider: str, line: Line, out: Path, settings: dict) -> list[Word]:
    text = re.sub(r"\s+", " ", line.text).strip()
    if provider == "elevenlabs":
        return eleven_tts(text, line.voice, out, settings)
    if provider == "edge":
        return asyncio.run(_edge(text, out, line.voice, settings.get("voice_rate", "+0%"),
                                 settings.get("voice_pitch", "+0Hz")))
    _silent(text, out)
    return []


# ---------------------------------------------------------------- API pública

def synthesize_lines(lines: list[Line], out: Path, settings: dict) -> Narration:
    """Sintetiza cada fala com sua voz, emenda com pausas e devolve palavras e falas com tempos."""
    provider = resolve_provider(settings)
    parts_dir = out.parent / "tts_parts"
    parts_dir.mkdir(exist_ok=True)
    files, words, segments = [], [], []
    offset = 0.0
    for i, line in enumerate(lines):
        if not line.text.strip():
            continue
        part = parts_dir / f"line_{i:03d}.mp3"
        line_words = _synth_one(provider, line, part, settings)
        dur = probe_duration(part)
        if not line_words:
            line_words = _even_words(line.text, dur)
        words += [Word(w.text, w.start + offset, w.end + offset) for w in line_words]
        segments.append(Segment(line.speaker, offset, offset + dur, i))
        files.append(part)
        offset += dur + PAUSE_S
    if not files:
        raise RuntimeError("Roteiro sem falas.")

    inputs: list[str] = []
    chains = []
    for i, f in enumerate(files):
        inputs += ["-i", str(f)]
        chains.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=mono,apad=pad_dur={PAUSE_S}[a{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[a{i}]" for i in range(len(files))) + \
        f"concat=n={len(files)}:v=0:a=1[out]"
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
                           "-c:a", "libmp3lame", "-q:a", "2", str(out)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg (áudio) falhou: {proc.stderr[-800:]}")
    for f in files:
        f.unlink(missing_ok=True)
    return Narration(out, probe_duration(out), words, segments)


def synthesize(text: str, out: Path, voice: str, rate: str = "+0%", pitch: str = "+0Hz") -> Narration:
    """Compatibilidade: uma única fala com uma voz (edge-tts / silencioso)."""
    settings = {"tts_provider": "edge", "voice_rate": rate, "voice_pitch": pitch}
    return synthesize_lines([Line("NARRADOR", text, voice)], out, settings)
