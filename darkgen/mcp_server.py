"""Servidor MCP: controle total do canal dark a partir do Claude (ou outro cliente MCP).

Modos:
- stdio:  `python -m darkgen mcp`   (Claude Desktop / Claude Code local)
- HTTP:   embutido no painel em http://HOST:PORT/mcp  (`python -m darkgen serve`)

O trabalho pesado (render, upload) é feito pelo worker do `serve`; as ferramentas daqui só
gravam na fila e consultam o estado.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer

from . import db, pipeline, service
from .scheduling import best_times as bt

INSTRUCTIONS = """Ferramentas para operar um canal dark do YouTube (vídeos longos e Shorts) em pt-BR.
Fluxo típico: channel_status → script_guidelines → list_recent_topics → create_video (com roteiro
completo em `scenes`) → o worker renderiza e agenda no YouTube no melhor horário.
Para piloto automático (Claude API no servidor), use plan_schedule.
Horários sem fuso são interpretados no fuso do canal."""

mcp = MCPServer("dark-generator", instructions=INSTRUCTIONS)


class SceneIn(BaseModel):
    narration: str = Field(description="Texto narrado nesta cena (pt-BR, linguagem falada).")
    visual_query: str = Field(description="2-4 palavras em inglês para buscar vídeo de fundo, ex.: 'abandoned hospital'.")


def _summ(v: dict | None) -> dict:
    if not v:
        raise ValueError("Vídeo não encontrado.")
    return service.video_summary(v)


@mcp.tool()
def channel_status() -> dict:
    """Visão geral: conexão com YouTube, worker, contagem por status, próximos vídeos e configurações."""
    return service.overview()


@mcp.tool()
def script_guidelines(kind: Literal["short", "long"] = "short") -> str:
    """Regras de formato para escrever um roteiro que será passado em create_video."""
    s = db.get_settings()
    if kind == "short":
        shape = "Short vertical: 120-150 palavras no total (45-58s), 4-7 cenas, gancho na 1ª frase, final com pergunta."
    else:
        mins = s["long_target_minutes"]
        shape = f"Longo horizontal: ~{mins * 140} palavras (~{mins} min), 12-25 cenas, gancho, contexto, tensão, revelação, CTA."
    return (
        f"{shape}\nNicho: {s['niche']}\n"
        "Cada cena = {narration, visual_query}. visual_query em inglês, concreto e visual.\n"
        "Fatos sobre casos reais precisam ser verdadeiros e verificáveis (passe as URLs em `sources`). "
        "Sem violência gráfica; respeite vítimas; ficção deve ser marcada com is_fiction=true.\n"
        "Título até 90 caracteres; descrição com hashtags (Shorts incluem #shorts); 10-15 tags; "
        "thumbnail_text com 2-4 palavras."
    )


@mcp.tool()
def list_recent_topics(limit: int = 100) -> list[str]:
    """Temas/títulos já produzidos — use para não repetir assuntos."""
    return db.recent_topics(limit)


@mcp.tool()
def create_video(
    kind: Literal["short", "long"],
    title: str | None = None,
    scenes: list[SceneIn] | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    thumbnail_text: str | None = None,
    topic: str | None = None,
    category: str | None = None,
    is_fiction: bool = False,
    sources: list[str] | None = None,
    scheduled_at: str | None = None,
) -> dict:
    """Cria um vídeo na fila de produção.

    - Com `scenes` + `title`: usa o roteiro informado (não precisa de API key no servidor).
    - Só com `topic` (ou nada): o servidor pesquisa e escreve o roteiro com a API do Claude.
    `scheduled_at` ('AAAA-MM-DD HH:MM', fuso do canal) é opcional; sem ele usa o próximo melhor horário livre.
    """
    v = service.create_video(
        kind, topic=topic, title=title, description=description, tags=tags,
        scenes=[s.model_dump() for s in scenes] if scenes else None,
        thumbnail_text=thumbnail_text, is_fiction=is_fiction, scheduled_at=scheduled_at,
        sources=sources, category=category,
    )
    return _summ(v)


@mcp.tool()
def list_videos(
    status: str | None = None,
    kind: Literal["short", "long"] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Lista vídeos. status: queued, scripting, scripted, rendering, rendered, uploading, scheduled, published, failed."""
    return [service.video_summary(v) for v in db.list_videos(status=status, kind=kind, limit=limit)]


@mcp.tool()
def get_video(video_id: int) -> dict:
    """Detalhes completos de um vídeo, incluindo roteiro, descrição e tags."""
    v = db.get_video(video_id)
    if not v:
        raise ValueError("Vídeo não encontrado.")
    v["scheduled_local"] = service.video_summary(v)["scheduled_local"]
    return v


@mcp.tool()
def update_video(
    video_id: int,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    thumbnail_text: str | None = None,
    scheduled_at: str | None = None,
    scenes: list[SceneIn] | None = None,
) -> dict:
    """Edita um vídeo que ainda não foi enviado. Trocar `scenes` força nova renderização."""
    return _summ(service.update_video(
        video_id, title=title, description=description, tags=tags, thumbnail_text=thumbnail_text,
        scheduled_at=scheduled_at, scenes=[s.model_dump() for s in scenes] if scenes else None,
    ))


@mcp.tool()
def approve_video(video_id: int) -> dict:
    """Aprova um vídeo renderizado para upload (quando approval_required está ativo)."""
    return _summ(service.approve(video_id))


@mcp.tool()
def retry_video(video_id: int) -> dict:
    """Recoloca um vídeo com falha na fila, a partir da última etapa concluída."""
    return _summ(pipeline.retry(video_id))


@mcp.tool()
def rerender_video(video_id: int) -> dict:
    """Renderiza de novo (ex.: depois de trocar vídeos de fundo ou voz). Só antes do upload."""
    return _summ(pipeline.rerender(video_id))


@mcp.tool()
def delete_video(video_id: int) -> str:
    """Remove o vídeo da fila e apaga os arquivos locais (não apaga do YouTube)."""
    pipeline.remove(video_id)
    return "removido"


@mcp.tool()
def best_times(kind: Literal["short", "long"] = "short", days: int = 7) -> dict:
    """Melhores horários de publicação (heatmap por dia/hora) e próximos horários livres sugeridos."""
    hm = bt.heatmap(kind)
    top = {
        day: [f"{h:02d}h" for h in sorted(range(24), key=lambda x: -scores[x])[:4]]
        for day, scores in zip(hm["days"], hm["scores"])
    }
    return {
        "adaptive": hm["adaptive"],
        "samples": hm["samples"],
        "top_hours_by_day": top,
        "next_free_slots": service.upcoming_slots(kind, days),
    }


@mcp.tool()
def plan_schedule(days_ahead: int = 1) -> list[dict]:
    """Preenche a cota diária (shorts_per_day / long_per_day) de hoje até N dias à frente com vídeos
    automáticos nos melhores horários. Requer ANTHROPIC_API_KEY no servidor."""
    if not pipeline.has_claude():
        raise ValueError("Servidor sem ANTHROPIC_API_KEY: crie vídeos com create_video passando o roteiro.")
    return [service.video_summary(v) for v in pipeline.plan(days_ahead)]


@mcp.tool()
def analytics() -> dict:
    """Desempenho: totais, top 10 vídeos, média de views por hora de publicação e evolução do canal."""
    return service.analytics()


@mcp.tool()
def refresh_stats() -> dict:
    """Atualiza agora as estatísticas do canal e dos vídeos a partir do YouTube."""
    return pipeline.refresh_stats()


@mcp.tool()
def get_settings() -> dict:
    """Configurações do canal (cotas diárias, nicho, voz, piloto automático, aprovação...)."""
    return db.get_settings()


@mcp.tool()
def update_settings(changes: dict) -> dict:
    """Altera configurações. Ex.: {"shorts_per_day": 3, "approval_required": true}."""
    return db.update_settings(changes)


@mcp.tool()
def activity_log(limit: int = 30) -> list[dict]:
    """Últimos eventos do pipeline (ideias, renders, uploads, falhas)."""
    return db.recent_activity(limit)


def run_stdio() -> None:
    mcp.run("stdio")
