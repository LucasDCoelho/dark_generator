"""Painel web + API REST + servidor MCP (HTTP) + agendador em um único processo."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
    sched = BackgroundScheduler(timezone=env().tz)
    opts = {"max_instances": 1, "coalesce": True}
    sched.add_job(_safe(pipeline.work_tick), "interval", seconds=20, id="worker", **opts)
    sched.add_job(_safe(pipeline.autopilot_tick), "interval", minutes=30, id="autopilot", **opts)
    sched.add_job(_safe(pipeline.refresh_stats), "interval", hours=3, id="stats", **opts)
    return sched


# ---------------------------------------------------------------- autenticação simples por token

class TokenMiddleware:
    OPEN = ("/", "/static/", "/api/youtube/callback", "/favicon.ico")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = env().dashboard_token
        if token and scope["type"] == "http":
            path = scope["path"]
            open_path = path == "/" or any(path.startswith(p) for p in self.OPEN[1:])
            if not open_path:
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

    def err(e: Exception):
        raise HTTPException(400, str(e)) from e

    # ---------- visão geral
    @app.get("/api/overview")
    def overview():
        return service.overview()

    @app.get("/api/env")
    def env_status():
        return service.env_status()

    @app.get("/api/activity")
    def activity(limit: int = 50):
        return db.recent_activity(limit)

    # ---------- vídeos
    @app.get("/api/videos")
    def videos(status: str | None = None, kind: str | None = None, limit: int = 100, offset: int = 0):
        statuses = status.split(",") if status else None
        return [service.video_summary(v) for v in db.list_videos(status=statuses, kind=kind, limit=limit, offset=offset)]

    @app.get("/api/videos/{video_id}")
    def video(video_id: int):
        v = db.get_video(video_id)
        if not v:
            raise HTTPException(404, "Vídeo não encontrado")
        v["scheduled_local"] = service.video_summary(v)["scheduled_local"]
        v["has_file"] = bool(v.get("video_path") and Path(v["video_path"]).exists())
        return v

    @app.post("/api/videos")
    def create(body: VideoIn):
        try:
            return service.video_summary(service.create_video(**body.model_dump()))
        except ValueError as e:
            err(e)

    @app.patch("/api/videos/{video_id}")
    def patch(video_id: int, body: VideoPatch):
        try:
            return service.video_summary(service.update_video(video_id, **body.model_dump()))
        except ValueError as e:
            err(e)

    @app.post("/api/videos/{video_id}/approve")
    def approve(video_id: int):
        try:
            return service.video_summary(service.approve(video_id))
        except ValueError as e:
            err(e)

    @app.post("/api/videos/{video_id}/retry")
    def retry(video_id: int):
        v = pipeline.retry(video_id)
        if not v:
            raise HTTPException(404, "Vídeo não encontrado")
        return service.video_summary(v)

    @app.post("/api/videos/{video_id}/rerender")
    def rerender(video_id: int):
        v = pipeline.rerender(video_id)
        if not v:
            raise HTTPException(400, "Só é possível renderizar de novo antes do upload e com roteiro pronto.")
        return service.video_summary(v)

    @app.delete("/api/videos/{video_id}")
    def delete(video_id: int):
        pipeline.remove(video_id)
        return {"ok": True}

    @app.get("/media/{video_id}/{what}")
    def media(video_id: int, what: Literal["video", "thumb"]):
        v = db.get_video(video_id)
        path = v and v.get("video_path" if what == "video" else "thumb_path")
        if not path or not Path(path).exists():
            raise HTTPException(404, "Arquivo não encontrado")
        return FileResponse(path)

    # ---------- planejamento / horários
    @app.post("/api/plan")
    def plan(days_ahead: int | None = None):
        if not pipeline.has_claude():
            raise HTTPException(400, "Configure ANTHROPIC_API_KEY para o piloto automático.")
        return [service.video_summary(v) for v in pipeline.plan(days_ahead)]

    @app.get("/api/best-times")
    def times(kind: Literal["short", "long"] = "short"):
        return {**best_times.heatmap(kind), "next_slots": service.upcoming_slots(kind, 7)}

    @app.post("/api/ideas")
    def ideas(kind: Literal["short", "long"] = "short", count: int = 6):
        if not pipeline.has_claude():
            raise HTTPException(400, "Configure ANTHROPIC_API_KEY para gerar ideias.")
        from ..content import writer

        s = db.get_settings()
        return [i.model_dump() for i in writer.generate_ideas(count, kind, s["niche"], s["content_mix"], db.recent_topics())]

    # ---------- analytics
    @app.get("/api/analytics")
    def analytics():
        return service.analytics()

    @app.post("/api/stats/refresh")
    def refresh():
        try:
            return pipeline.refresh_stats()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e

    # ---------- configurações
    @app.get("/api/settings")
    def get_settings():
        return db.get_settings()

    @app.put("/api/settings")
    def put_settings(changes: dict):
        try:
            return db.update_settings(changes)
        except ValueError as e:
            err(e)

    # ---------- YouTube OAuth
    def _redirect_uri(request: Request) -> str:
        base = env().public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
        return f"{base}/api/youtube/callback"

    @app.post("/api/youtube/client-secret")
    async def upload_secret(file: UploadFile = File(...)):
        try:
            yt.save_client_secret(await file.read())
        except ValueError as e:
            err(e)
        return {"ok": True}

    @app.get("/api/youtube/connect")
    def connect(request: Request):
        try:
            return {"url": yt.start_auth(_redirect_uri(request))}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e

    @app.get("/api/youtube/callback")
    def callback(state: str, code: str | None = None, error: str | None = None):
        if error or not code:
            return HTMLResponse(f"<p>Autorização cancelada: {error}</p><a href='/'>Voltar</a>")
        try:
            yt.finish_auth(state, code)
            info = yt.channel_info()
            db.set_state("channel", info)
            db.log(f"Canal conectado: {info['title']}")
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(f"<p>Falha ao conectar: {e}</p><a href='/'>Voltar</a>", status_code=400)
        return RedirectResponse("/#config")

    @app.post("/api/youtube/disconnect")
    def disconnect():
        yt.disconnect()
        db.log("Canal desconectado", "warning")
        return {"ok": True}

    # ---------- painel
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/", mcp_app)  # expõe /mcp (Streamable HTTP)
    return app
