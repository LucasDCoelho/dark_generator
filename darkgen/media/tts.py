"""Narração por TTS com tempos por palavra (para legendas sincronizadas)."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import env


@dataclass
class Word:
    text: str
    start: float  # segundos
    end: float


@dataclass
class Narration:
    audio_path: Path
    duration: float
    words: list[Word]


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _even_words(text: str, duration: float, offset: float = 0.0) -> list[Word]:
    """Distribui as palavras proporcionalmente ao número de letras (fallback sem word boundaries)."""
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
    """Áudio silencioso com duração estimada (≈2.6 palavras/s) — só para testes sem rede."""
    seconds = max(2.0, len(text.split()) / 2.6)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
         "-t", f"{seconds:.2f}", "-c:a", "libmp3lame", "-q:a", "6", str(out)],
        check=True,
    )


def synthesize(text: str, out: Path, voice: str, rate: str = "+0%", pitch: str = "+0Hz") -> Narration:
    text = re.sub(r"\s+", " ", text).strip()
    provider = env().tts_provider
    words: list[Word] = []
    if provider == "silent":
        _silent(text, out)
    else:
        words = asyncio.run(_edge(text, out, voice, rate, pitch))
    duration = probe_duration(out)
    if not words:
        words = _even_words(text, duration)
    return Narration(out, duration, words)
