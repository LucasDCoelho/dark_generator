"""Painel web + API REST + servidor MCP (HTTP) + agendador em um único processo."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import db, pipeline, service
from ..config import env
from ..mcp_server import mcp
from ..scheduling import best_times
from ..youtube import client as yt

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- agendador

def _safe(fn):
    def wrapper():
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("Job %s falhou", fn.__name__)
    wrapper.__name__ = fn.__name__
    return wrapper


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler()
    opts = {"max_instances": 1, "coalesce": True}
    sched.add_job(_safe(pipeline.work_tick), "interval", seconds=20, id="worker", **opts)
    sched.add_job(_safe(pipeline.autopilot_tick), "interval", minutes=30, id="autopilot", **opts)
    sched.add_job(_safe(pipeline.refresh_stats), "interval", hours=3, id="stats", **opts)
    sched.add_job(_safe(pipeline.cleanup_files), "interval", hours=24, id="cleanup", **opts)
    return sched


# ---------------------------------------------------------------- autenticação simples por token

class TokenMiddleware:
    OPEN_PREFIXES = ("/static/", "/api/youtube/callback", "/favicon.ico")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = env().dashboard_token
        if token and scope["type"] == "http":
            path = scope["path"]
            if path != "/" and not path.startswith(self.OPEN_PREFIXES):
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode()
                qs = scope.get("query_string", b"").decode()
                if auth != f"Bearer {token}" and f"token={token}" not in qs:
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": b'{"detail":"token invalido"}'})
                    return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------- modelos da API

class SceneIn(BaseModel):
    narration: str
    visual_query: str = "dark atmosphere"


class VideoIn(BaseModel):
    channel_id: int
    kind: Literal["short", "long"]
    topic: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    scenes: list[SceneIn] | None = None
    thumbnail_text: str | None = None
    is_fiction: bool = False
    scheduled_at: str | None = None


class VideoPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    thumbnail_text: str | None = None
    scheduled_at: str | None = None
    scenes: list[SceneIn] | None = None


class ChannelIn(BaseModel):
    name: str
    preset: str | None = None
    settings: dict | None = None
    notes: str | None = None


class ChannelPatch(BaseModel):
    name: str | None = None
    active: bool | None = None
    notes: str | None = None
    settings: dict | None = None


class CloneIn(BaseModel):
    name: str


class MoveIn(BaseModel):
    channel_id: int


# ---------------------------------------------------------------- app

def create_app(start_scheduler: bool = True) -> FastAPI:
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", host=env().host)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pipeline.recover_interrupted()
        sched = None
        if start_scheduler:
            sched = build_scheduler()
            sched.start()
            sched.add_job(_safe(pipeline.autopilot_tick), id="autopilot_boot")  # roda já na partida
        async with mcp.session_manager.run():
            yield
        if sched:
            sched.shutdown(wait=False)

    app = FastAPI(title="Dark Generator", lifespan=lifespan)
    app.add_middleware(TokenMiddleware)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def _video_or_404(video_id: int) -> dict:
        v = db.get_video(video_id)
        if not v:
            raise HTTPException(404, "Vídeo não encontrado")
        return v

    # ---------- geral
    @app.get("/api/portfolio")
    def portfolio():
        return service.portfolio()

    @app.get("/api/env")
    def env_status():
        return service.env_status()

    @app.get("/api/presets")
    def presets():
        return service.list_presets()

    @app.get("/api/activity")
    def activity(limit: int = 50, channel_id: int | None = None):
        return db.recent_activity(limit, channel_id=channel_id)

    @app.get("/api/compare")
    def compare():
        return service.compare_channels()

    @app.post("/api/plan")
    def plan_all():
        if not pipeline.has_claude():
            raise HTTPException(400, "Configure ANTHROPIC_API_KEY para o piloto automático.")
        return service.summaries(pipeline.plan())

    @app.post("/api/stats/refresh")
    def refresh_all():
        return pipeline.refresh_stats()

    # ---------- canais
    @app.get("/api/channels")
    def channels():
        return service.portfolio()["channels"]

    @app.post("/api/channels")
    def create_channel(body: ChannelIn):
        return service.channel_public(service.create_channel(body.name, body.preset, body.settings, body.notes))

    @app.get("/api/channels/{channel_id}")
    def channel(channel_id: int):
        return service.overview(channel_id)

    @app.patch("/api/channels/{channel_id}")
    def patch_channel(channel_id: int, body: ChannelPatch):
        return service.channel_public(service.update_channel(channel_id, **body.model_dump()))

    @app.delete("/api/channels/{channel_id}")
    def delete_channel(channel_id: int, confirm: str):
        ch = service.require_channel(channel_id)
        if confirm.strip() != ch["name"]:
            raise HTTPException(400, "Digite o nome exato do canal para confirmar.")
        service.delete_channel(channel_id)
        return {"ok": True}

    @app.post("/api/channels/{channel_id}/clone")
    def clone_channel(channel_id: int, body: CloneIn):
        return service.channel_public(service.clone_channel(channel_id, body.name))

    @app.get("/api/channels/{channel_id}/health")
    def health(channel_id: int):
        return service.channel_health(channel_id)

    @app.get("/api/channels/{channel_id}/best-times")
    def times(channel_id: int, kind: Literal["short", "long"] = "short"):
        ch = service.require_channel(channel_id)
        return {**best_times.heatmap(ch, kind), "next_slots": service.upcoming_slots(channel_id, kind, 7)}

    @app.get("/api/channels/{channel_id}/analytics")
    def analytics(channel_id: int):
        return service.analytics(channel_id)

    @app.post("/api/channels/{channel_id}/plan")
    def plan(channel_id: int):
        if not pipeline.has_claude():
            raise HTTPException(400, "Configure ANTHROPIC_API_KEY para o piloto automático.")
        return service.summaries(pipeline.plan(channel_id))

    @app.post("/api/channels/{channel_id}/ideas")
    def ideas(channel_id: int, kind: Literal["short", "long"] = "short", count: int = 6):
        if not pipeline.has_claude():
            raise HTTPException(400, "Configure ANTHROPIC_API_KEY para gerar ideias.")
        from ..content import writer

        ch = service.require_channel(channel_id)
        return [i.model_dump() for i in writer.generate_ideas(ch, count, kind, db.recent_topics(channel_id))]

    @app.post("/api/channels/{channel_id}/stats/refresh")
    def refresh(channel_id: int):
        return pipeline.refresh_stats(channel_id)

    @app.post("/api/channels/{channel_id}/retry-failed")
    def retry_failed(channel_id: int):
        return {"requeued": len(pipeline.retry_failed(channel_id))}

    # ---------- YouTube OAuth (por canal)
    def _redirect_uri(request: Request) -> str:
        base = env().public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
        return f"{base}/api/youtube/callback"

    @app.post("/api/youtube/client-secret")
    async def upload_secret(file: UploadFile = File(...)):
        yt.save_client_secret(await file.read())
        return {"ok": True}

    @app.post("/api/channels/{channel_id}/youtube/client-secret")
    async def upload_channel_secret(channel_id: int, file: UploadFile = File(...)):
        service.require_channel(channel_id)
        yt.save_client_secret(await file.read(), channel_id)
        return {"ok": True}

    @app.delete("/api/channels/{channel_id}/youtube/client-secret")
    def delete_channel_secret(channel_id: int):
        yt.remove_channel_secret(channel_id)
        return {"ok": True}

    @app.get("/api/channels/{channel_id}/youtube/connect")
    def connect(channel_id: int, request: Request, redirect: bool = False):
        service.require_channel(channel_id)
        try:
            url = yt.start_auth(channel_id, _redirect_uri(request))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e
        return RedirectResponse(url) if redirect else {"url": url}

    @app.get("/api/youtube/callback")
    def callback(state: str, code: str | None = None, error: str | None = None):
        if error or not code:
            return HTMLResponse(f"<p>Autorização cancelada: {error}</p><a href='/'>Voltar</a>")
        try:
            channel_id = yt.finish_auth(state, code)
            info = yt.channel_info(channel_id)
            dup = [c for c in db.list_channels() if c["youtube_channel_id"] == info["id"] and c["id"] != channel_id]
            db.update_channel(channel_id, youtube_channel_id=info["id"], youtube_title=info["title"],
                              youtube_thumb=info["thumbnail"], subscribers=info["subscribers"],
                              views=info["views"], video_count=info["videos"], stats_updated_at=db.now_utc())
            db.log(f"YouTube conectado: {info['title']}", channel_id=channel_id)
            if dup:
                db.log(f"Atenção: o canal '{info['title']}' também está conectado em '{dup[0]['name']}'",
                       "warning", channel_id=channel_id)
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(f"<p>Falha ao conectar: {e}</p><a href='/'>Voltar</a>", status_code=400)
        return RedirectResponse(f"/#c{channel_id}/config")

    @app.post("/api/channels/{channel_id}/youtube/disconnect")
    def disconnect(channel_id: int):
        yt.disconnect(channel_id)
        db.log("YouTube desconectado", "warning", channel_id=channel_id)
        return {"ok": True}

    # ---------- vídeos
    @app.get("/api/videos")
    def videos(status: str | None = None, kind: str | None = None, channel_id: int | None = None,
               limit: int = 100, offset: int = 0):
        statuses = status.split(",") if status else None
        return service.summaries(db.list_videos(status=statuses, kind=kind, channel_id=channel_id,
                                                limit=limit, offset=offset))

    @app.post("/api/videos")
    def create_video(body: VideoIn):
        return service.video_summary(service.create_video(**body.model_dump()))

    @app.get("/api/videos/{video_id}")
    def video(video_id: int):
        v = _video_or_404(video_id)
        return {**v, **service.video_summary(v),
                "has_file": bool(v.get("video_path") and Path(v["video_path"]).exists())}

    @app.patch("/api/videos/{video_id}")
    def patch_video(video_id: int, body: VideoPatch):
        return service.video_summary(service.update_video(video_id, **body.model_dump()))

    @app.post("/api/videos/{video_id}/approve")
    def approve(video_id: int):
        return service.video_summary(service.approve(video_id))

    @app.post("/api/videos/{video_id}/retry")
    def retry(video_id: int):
        return service.video_summary(pipeline.retry(_video_or_404(video_id)["id"]))

    @app.post("/api/videos/{video_id}/rerender")
    def rerender(video_id: int):
        v = pipeline.rerender(video_id)
        if not v:
            raise HTTPException(400, "Só é possível renderizar de novo antes do upload e com roteiro pronto.")
        return service.video_summary(v)

    @app.post("/api/videos/{video_id}/move")
    def move(video_id: int, body: MoveIn):
        return service.video_summary(service.move_video(video_id, body.channel_id))

    @app.delete("/api/videos/{video_id}")
    def delete_video(video_id: int):
        pipeline.remove(video_id)
        return {"ok": True}

    @app.get("/media/{video_id}/{what}")
    def media(video_id: int, what: Literal["video", "thumb"]):
        v = db.get_video(video_id)
        path = v and v.get("video_path" if what == "video" else "thumb_path")
        if not path or not Path(path).exists():
            raise HTTPException(404, "Arquivo não encontrado")
        return FileResponse(path)

    # ---------- painel
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/", mcp_app)  # expõe /mcp (Streamable HTTP)
    return app
