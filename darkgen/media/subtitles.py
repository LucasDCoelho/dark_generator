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


def chunk_words(words: list[Word], max_words: int, max_chars: int, max_gap: float = 0.25) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and w.start - cur[-1].end > max_gap:  # pausa (troca de fala): nunca mistura duas falas
            chunks.append(cur)
            cur = []
        cur.append(w)
        text = " ".join(x.text for x in cur)
        ends_sentence = w.text[-1:] in ".!?…;:,"
        if len(cur) >= max_words or len(text) >= max_chars or ends_sentence:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def ass_color(hex_color: str) -> str:
    """'#RRGGBB' -> '&H00BBGGRR' (formato de cor do ASS)."""
    h = (hex_color or "#FF4B4B").lstrip("#")
    if len(h) != 6:
        h = "FF4B4B"
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def build_ass(
    words: list[Word],
    out: Path,
    kind: str,
    highlight: str = "#FF4B4B",
    speakers: list[tuple[str, float, float]] | None = None,
    overlays: list[tuple[str, float, float]] | None = None,
) -> Path:
    """Legendas + (opcional) nome de quem fala e letreiros (ex.: 'PARTE 2', 'CONTINUA NA PARTE 3')."""
    hl = ass_color(highlight)
    if kind == "short":
        w, h = 1080, 1920
        # Primary = cor depois de falada (destaque do canal); Secondary = antes (branco)
        style = (
            f"Style: Default,{FONT},88,{hl},&H00FFFFFF,&H00000000,&H96000000,"
            "-1,0,0,0,100,100,0,0,1,7,3,5,80,80,0,1"
        )
        speaker_style = f"Style: Speaker,{FONT},50,{hl},&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,2,0,1,5,2,5,60,60,0,1"
        card_style = f"Style: Card,{FONT},96,&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,2,0,3,18,0,5,60,60,0,1"
        speaker_pos, card_pos = "\\pos(540,840)", "\\pos(540,300)"
        chunks = chunk_words(words, max_words=3, max_chars=18)
    else:
        w, h = 1920, 1080
        style = (
            f"Style: Default,{FONT},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
            "-1,0,0,0,100,100,0,0,1,4,2,2,160,160,70,1"
        )
        speaker_style = f"Style: Speaker,{FONT},40,{hl},&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,2,0,1,4,2,5,60,60,0,1"
        card_style = f"Style: Card,{FONT},80,&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,2,0,3,16,0,5,60,60,0,1"
        speaker_pos, card_pos = "\\pos(960,935)", "\\pos(960,140)"
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
        speaker_style,
        card_style,
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
    for name, start, end in speakers or []:
        lines.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Speaker,,0,0,0,,{{{speaker_pos}\\fad(80,80)}}{_escape(name.upper())}")
    for text, start, end in overlays or []:
        lines.append(f"Dialogue: 2,{_ts(start)},{_ts(end)},Card,,0,0,0,,{{{card_pos}\\fad(150,150)}}{_escape(text.upper())}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
