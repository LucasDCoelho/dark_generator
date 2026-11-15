"""Gera legendas .ass (estilo Shorts com destaque palavra a palavra, ou legenda clássica)."""

from __future__ import annotations

from pathlib import Path

from .tts import Word

FONT = "DejaVu Sans"


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def chunk_words(words: list[Word], max_words: int, max_chars: int) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        cur.append(w)
        text = " ".join(x.text for x in cur)
        ends_sentence = w.text[-1:] in ".!?…;:,"
        if len(cur) >= max_words or len(text) >= max_chars or ends_sentence:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words: list[Word], out: Path, kind: str) -> Path:
    if kind == "short":
        w, h = 1080, 1920
        # Primary = cor depois de falada (vermelho-sangue claro); Secondary = antes (branco)
        style = (
            f"Style: Default,{FONT},88,&H004B4BFF,&H00FFFFFF,&H00000000,&H96000000,"
            "-1,0,0,0,100,100,0,0,1,7,3,5,80,80,0,1"
        )
        chunks = chunk_words(words, max_words=3, max_chars=18)
    else:
        w, h = 1920, 1080
        style = (
            f"Style: Default,{FONT},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
            "-1,0,0,0,100,100,0,0,1,4,2,2,160,160,70,1"
        )
        chunks = chunk_words(words, max_words=8, max_chars=48)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for i, chunk in enumerate(chunks):
        start = chunk[0].start
        nxt = chunks[i + 1][0].start if i + 1 < len(chunks) else chunk[-1].end + 0.4
        end = max(chunk[-1].end, min(nxt, chunk[-1].end + 0.6))
        if kind == "short":
            parts = []
            for j, word in enumerate(chunk):
                nxt_start = chunk[j + 1].start if j + 1 < len(chunk) else word.end
                cs = max(1, round((nxt_start - word.start) * 100))
                parts.append(f"{{\\k{cs}}}{_escape(word.text.upper())}")
            text = "{\\fad(60,0)}" + " ".join(parts)
        else:
            text = _escape(" ".join(x.text for x in chunk))
        lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{text}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
