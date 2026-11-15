"""Clipes de fundo: banco de vídeos Pexels (opcional) ou fundos sombrios procedurais."""

from __future__ import annotations

import logging
import random
import subprocess
from pathlib import Path

import httpx

from ..config import env

log = logging.getLogger(__name__)

# Paletas para fundos procedurais (escuras, com uma cor dominante sutil).
_PALETTES = [
    ("0x030303", "0x2a0508", "0x0a0a12"),  # vermelho-sangue
    ("0x020406", "0x061a2a", "0x0b0b0b"),  # azul gélido
    ("0x030503", "0x0d1f12", "0x050505"),  # verde pântano
    ("0x050403", "0x241a08", "0x080606"),  # sépia antiga
    ("0x040208", "0x1a0a2a", "0x060606"),  # roxo
]


def size_for(kind: str) -> tuple[int, int]:
    return (1080, 1920) if kind == "short" else (1920, 1080)


def _run(args: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {proc.stderr[-1500:]}")


def search_pexels(query: str, kind: str, used: set[int]) -> Path | None:
    key = env().pexels_api_key
    if not key:
        return None
    orientation = "portrait" if kind == "short" else "landscape"
    try:
        r = httpx.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "orientation": orientation, "per_page": 15, "size": "medium"},
            headers={"Authorization": key},
            timeout=20,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Pexels falhou para %r: %s", query, e)
        return None
    videos = [v for v in r.json().get("videos", []) if v["id"] not in used and v.get("duration", 0) >= 4]
    if not videos:
        return None
    video = random.choice(videos[:6])
    target_w, _ = size_for(kind)
    files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("width")]
    if not files:
        return None
    # arquivo com resolução mais próxima (acima) da necessária
    files.sort(key=lambda f: (f["width"] < min(target_w, 1080), abs(f["width"] - target_w)))
    cache = env().data_dir / "stock"
    cache.mkdir(exist_ok=True)
    dest = cache / f"pexels_{video['id']}_{files[0]['width']}.mp4"
    if not dest.exists():
        with httpx.stream("GET", files[0]["link"], timeout=120, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for part in resp.iter_bytes():
                    f.write(part)
    used.add(video["id"])
    return dest


def _grade(w: int, h: int, duration: float) -> str:
    fade_out = max(0.0, duration - 0.3)
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30,"
        "eq=contrast=1.15:brightness=-0.07:saturation=0.5,"
        "colorbalance=bs=0.05:bm=0.03:rh=0.02,"
        "noise=alls=3:allf=t,vignette=angle=PI/4,"
        f"fade=t=in:st=0:d=0.3,fade=t=out:st={fade_out:.2f}:d=0.3,format=yuv420p"
    )


def render_segment(source: Path | None, out: Path, kind: str, duration: float, seed: int) -> Path:
    """Renderiza um segmento de `duration` segundos (sem áudio) em `out`."""
    w, h = size_for(kind)
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-an", "-t", f"{duration:.3f}"]
    if source is not None:
        # começa num ponto aleatório do clipe para variar; faz loop se for curto
        _run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(source),
              "-vf", _grade(w, h, duration), *enc, str(out)])
        return out
    rng = random.Random(seed)
    c0, c1, c2 = rng.choice(_PALETTES)
    src = (
        f"gradients=s={w}x{h}:c0={c0}:c1={c1}:c2={c2}:nb_colors=3:speed={rng.uniform(0.004, 0.012):.4f}:"
        f"seed={seed}:r=30:d={duration:.3f}"
    )
    fade_out = max(0.0, duration - 0.3)
    vf = (
        "noise=alls=4:allf=t,vignette=angle=PI/3.5,"
        f"fade=t=in:st=0:d=0.3,fade=t=out:st={fade_out:.2f}:d=0.3,format=yuv420p"
    )
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src, "-vf", vf, *enc, str(out)])
    return out
