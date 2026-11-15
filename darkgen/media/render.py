"""Monta o vídeo final: narração + fundos + legendas + trilha ambiente + thumbnail."""

from __future__ import annotations

import logging
import random
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

from . import subtitles, tts, visuals

log = logging.getLogger(__name__)

TAIL_S = 1.2  # respiro depois da narração


@dataclass
class RenderResult:
    video_path: Path
    thumb_path: Path
    duration: float


def _run(args: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {proc.stderr[-2000:]}")


def _font_file() -> str:
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold"], capture_output=True, text=True)
        if out.stdout.strip():
            return out.stdout.strip()
    except FileNotFoundError:
        pass
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def scene_times(scenes: list[dict], words: list[tts.Word], total: float) -> list[tuple[float, float]]:
    """Calcula início/fim de cada cena alinhando a contagem de palavras do roteiro aos tempos do TTS."""
    counts = [max(1, len(s["narration"].split())) for s in scenes]
    total_words = sum(counts)
    starts, acc = [], 0
    for c in counts:
        if not words:
            starts.append(total * acc / total_words)
        else:
            idx = min(len(words) - 1, round(acc / total_words * len(words)))
            starts.append(words[idx].start)
        acc += c
    starts[0] = 0.0
    times = []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else total
        times.append((st, max(end, st + 0.5)))
    return times


def _music_input(duration: float) -> list[str]:
    from ..config import env

    tracks = [p for p in env().music_dir.glob("*") if p.suffix.lower() in {".mp3", ".wav", ".ogg", ".m4a"}]
    if tracks:
        return ["-stream_loop", "-1", "-i", str(random.choice(tracks))]
    # drone sombrio procedural: ruído marrom filtrado + notas graves
    src = (
        f"anoisesrc=color=brown:amplitude=0.6:d={duration:.2f},lowpass=f=320,"
        "volume=0.6[n];"
        f"sine=f=55:d={duration:.2f},volume=0.35[a];"
        f"sine=f=82.41:d={duration:.2f},volume=0.18,tremolo=f=0.15:d=0.7[b];"
        "[n][a][b]amix=inputs=3:normalize=0,aecho=0.8:0.7:900:0.35"
    )
    return ["-f", "lavfi", "-i", src]


def make_thumbnail(frame_src: Path, text: str, out: Path, kind: str) -> Path:
    w, h = visuals.size_for(kind) if kind == "long" else (1080, 1920)
    text = (text or "").upper().strip()
    wrap = textwrap.fill(text, width=14 if kind == "long" else 10)
    txt_file = out.with_suffix(".txt")
    txt_file.write_text(wrap, encoding="utf-8")
    size = 150 if kind == "long" else 130
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        "eq=contrast=1.3:brightness=-0.12:saturation=0.6,vignette=angle=PI/3,"
        f"drawtext=fontfile='{_font_file()}':textfile='{txt_file.name}':fontsize={size}:"
        "fontcolor=white:borderw=10:bordercolor=black:line_spacing=10:"
        "x=(w-text_w)/2:y=(h-text_h)/2,"
        "drawbox=x=0:y=ih-28:w=iw:h=28:color=0xB00012@0.95:t=fill"
    )
    _run(["ffmpeg", "-y", "-v", "error", "-ss", "1.0", "-i", frame_src.name, "-frames:v", "1",
          "-vf", vf, "-q:v", "3", out.name], cwd=out.parent)
    txt_file.unlink(missing_ok=True)
    return out


def render_video(video: dict, settings: dict, workdir: Path) -> RenderResult:
    workdir.mkdir(parents=True, exist_ok=True)
    kind = video["kind"]
    script = video["script"]
    scenes = script["scenes"]
    full_text = " ".join(s["narration"].strip() for s in scenes)

    # 1) narração
    narration = tts.synthesize(
        full_text, workdir / "narration.mp3",
        voice=settings["voice"], rate=settings["voice_rate"], pitch=settings["voice_pitch"],
    )
    total = narration.duration + TAIL_S
    log.info("Narração: %.1fs, %d palavras", narration.duration, len(narration.words))

    # 2) legendas
    subtitles.build_ass(narration.words, workdir / "subs.ass", kind, settings.get("subtitle_color", "#FF4B4B"))

    # 3) segmentos visuais por cena
    used: set[int] = set()
    seg_files = []
    first_source = None
    for i, ((start, end), scene) in enumerate(zip(scene_times(scenes, narration.words, total), scenes)):
        src = None
        try:
            src = visuals.search_pexels(scene.get("visual_query") or "dark night", kind, used)
        except Exception as e:  # noqa: BLE001 - visual de banco é opcional
            log.warning("Sem vídeo de banco para a cena %d: %s", i, e)
        seg = workdir / f"seg_{i:02d}.mp4"
        visuals.render_segment(src, seg, kind, end - start, seed=video["id"] * 100 + i)
        first_source = first_source or seg
        seg_files.append(seg)
    (workdir / "segments.txt").write_text("".join(f"file '{p.name}'\n" for p in seg_files))
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "segments.txt",
          "-c", "copy", "visual.mp4"], cwd=workdir)

    # 4) mixagem final: legendas + narração + trilha com ducking + loudness para YouTube (-14 LUFS)
    vol = float(settings.get("music_volume", 0.14))
    filter_complex = (
        "[0:v]ass=subs.ass[v];"
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={total:.2f},asplit=2[nar][sc];"
        f"[2:a]aresample=48000,aformat=channel_layouts=stereo,volume={vol}[mus];"
        "[mus][sc]sidechaincompress=threshold=0.04:ratio=5:attack=30:release=500[duck];"
        "[nar][duck]amix=inputs=2:duration=longest:normalize=0,"
        f"atrim=0:{total:.2f},afade=t=out:st={max(0, total - 1):.2f}:d=1,"
        "loudnorm=I=-14:TP=-1.5:LRA=11[a]"
    )
    out = workdir / "final.mp4"
    _run(["ffmpeg", "-y", "-v", "error", "-i", "visual.mp4", "-i", "narration.mp3",
          *_music_input(total), "-filter_complex", filter_complex,
          "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}",
          "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-maxrate", "10M", "-bufsize", "20M",
          "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", out.name],
         cwd=workdir)

    # 5) thumbnail
    thumb = make_thumbnail(first_source, video.get("thumbnail_text") or video.get("title") or "",
                           workdir / "thumb.jpg", kind)

    # limpa intermediários
    for p in [*seg_files, workdir / "visual.mp4", workdir / "segments.txt"]:
        p.unlink(missing_ok=True)
    return RenderResult(out, thumb, total)
