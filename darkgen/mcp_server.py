"""Servidor MCP: gerencie vários canais dark do YouTube a partir do Claude (ou outro cliente MCP).

Modos:
- stdio:  `python -m darkgen mcp`   (Claude Desktop / Claude Code local)
- HTTP:   embutido no painel em http://HOST:PORT/mcp  (`python -m darkgen serve`)

O trabalho pesado (render, upload) é feito pelo worker do `serve`; as ferramentas daqui gravam
na fila, configuram canais e consultam o estado.
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import db, pipeline, service, trends
from .content.writer import script_shape
from .scheduling import best_times as bt

INSTRUCTIONS = """Gerenciador de vários canais dark do YouTube (vídeos longos e Shorts).
Quase tudo recebe channel_id — comece com portfolio_overview ou list_channels.
Criar canal: list_niche_presets → create_channel → connect_youtube (o usuário abre o link e escolhe o
canal/Conta de Marca no Google). O YouTube não permite criar o canal em si pela API: o usuário cria
no YouTube e conecta aqui.
Produzir: script_guidelines → list_recent_topics → create_video com o roteiro em `scenes` (ou só tema,
se o servidor tiver Claude API) → o worker renderiza e agenda no melhor horário do canal.
Manutenção: channel_health (sem channel_id = todos) mostra problemas e a ação sugerida para cada um.
Séries em partes (formato que mais prende): create_series (Claude no servidor escreve) ou
create_series_from_script (você escreve todos os episódios) — episódios saem em dias seguidos, em ordem,
com gancho entre as partes, link para a parte anterior e playlist. auto_series no canal automatiza.
Tendências: scan_youtube_trends (dados reais do YouTube: views/dia e outliers de canais pequenos) e
scan_social_trends (TikTok/Kwai/Instagram via pesquisa na web) ajudam a escolher nichos antes de criar
canais; os resultados ficam salvos (list_trend_scans / compare_trend_scans).
Horários sem fuso são interpretados no fuso do público do canal."""

mcp = MCPServer("dark-generator", instructions=INSTRUCTIONS)


def tool():
    """Registra a ferramenta convertendo falhas esperadas em ToolError, para o modelo ler a mensagem."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ValueError, RuntimeError, FileNotFoundError) as e:
                raise ToolError(str(e)) from e
        return mcp.tool()(wrapper)
    return deco


class EpisodeIn(BaseModel):
    title: str = Field(description="Título do episódio terminando com '(Parte N)' / '(Part N)'.")
    scenes: list["SceneIn"]
    description: str = ""
    tags: list[str] = []
    thumbnail_text: str = ""


class SceneIn(BaseModel):
    narration: str = Field(description="Texto narrado nesta cena, no idioma do canal, linguagem falada.")
    visual_query: str = Field(description="2-4 palavras em inglês para buscar vídeo de fundo, ex.: 'abandoned hospital'.")


def _summ(v: dict | None) -> dict:
    if not v:
        raise ValueError("Vídeo não encontrado.")
    return service.video_summary(v)


# ================================================================ canais

@tool()
def portfolio_overview() -> dict:
    """Painel geral de TODOS os canais: totais, saúde, próxima publicação, cota do YouTube e worker."""
    return service.portfolio()


@tool()
def list_channels() -> list[dict]:
    """Lista os canais com ID, nome, idioma, status, conexão e saúde (resumo)."""
    return [
        {k: r[k] for k in ("id", "name", "active", "language", "preset", "youtube_title", "youtube_connected",
                           "subscribers", "scheduled", "failed", "next_publication", "health")}
        for r in service.portfolio()["channels"]
    ]


@tool()
def list_niche_presets() -> list[dict]:
    """Modelos de nicho prontos (inclui canais em inglês e espanhol) para usar em create_channel."""
    return service.list_presets()


@tool()
def get_channel(channel_id: int) -> dict:
    """Configuração completa de um canal, saúde, contagens e próximos vídeos."""
    return service.overview(channel_id)


@tool()
def create_channel(name: str, preset: str | None = None, settings: dict | None = None, notes: str | None = None) -> dict:
    """Cria um canal no gerenciador. `preset` = chave de list_niche_presets; `settings` sobrescreve campos
    (niche, style, language, timezone, voice, shorts_per_day, long_per_day, content_mix, default_tags,
    approval_required, autopilot, subtitle_color...). Depois chame connect_youtube."""
    ch = service.create_channel(name, preset=preset, settings=settings, notes=notes)
    return {**service.channel_public(ch), "next_step": service.connect_url(ch["id"])}


@tool()
def update_channel(
    channel_id: int,
    name: str | None = None,
    active: bool | None = None,
    notes: str | None = None,
    settings: dict | None = None,
) -> dict:
    """Edita um canal. active=false pausa (nada é produzido/enviado). `settings` aceita mudanças parciais,
    ex.: {"niche": "...", "shorts_per_day": 3, "voice": "pt-BR-FranciscaNeural"}."""
    return service.channel_public(service.update_channel(channel_id, name=name, active=active, notes=notes,
                                                         settings=settings))


@tool()
def clone_channel(channel_id: int, name: str) -> dict:
    """Cria um canal novo copiando as configurações de outro (útil para replicar um nicho que dá certo
    em outro idioma — ajuste language/voice/timezone depois com update_channel)."""
    return service.channel_public(service.clone_channel(channel_id, name))


@tool()
def delete_channel(channel_id: int, confirm_name: str) -> str:
    """Remove o canal do gerenciador com toda a fila e arquivos locais (NÃO apaga nada no YouTube).
    Exige `confirm_name` igual ao nome do canal."""
    ch = service.require_channel(channel_id)
    if confirm_name.strip() != ch["name"]:
        raise ValueError(f"Confirmação não confere. Para apagar, envie confirm_name='{ch['name']}'.")
    service.delete_channel(channel_id)
    return f"Canal '{ch['name']}' removido do gerenciador."


@tool()
def connect_youtube(channel_id: int) -> dict:
    """Gera o link que o usuário deve abrir no navegador para autorizar o YouTube deste canal.
    Requer `python -m darkgen serve` rodando. Na tela do Google, escolher o canal/Conta de Marca certo."""
    return {
        "url": service.connect_url(channel_id),
        "instructions": "Abra o link, escolha a conta Google e o canal correto e autorize. "
                        "Depois confira com get_channel.",
    }


@tool()
def disconnect_youtube(channel_id: int) -> str:
    """Remove o token do YouTube deste canal (para trocar de conta ou revogar acesso)."""
    from .youtube import client as yt

    service.require_channel(channel_id)
    yt.disconnect(channel_id)
    db.log("YouTube desconectado", "warning", channel_id=channel_id)
    return "desconectado"


@tool()
def channel_health(channel_id: int | None = None) -> dict | list[dict]:
    """Diagnóstico de manutenção: conexão, falhas, agenda vazia, aprovações pendentes, cota, inatividade,
    queda de views. Sem channel_id, verifica todos os canais."""
    if channel_id is not None:
        return service.channel_health(channel_id)
    return [service.channel_health(c["id"]) for c in db.list_channels()]


@tool()
def quota_status() -> dict:
    """Uploads nas últimas 24h por projeto do Google Cloud (a cota é por projeto, compartilhada entre canais)."""
    return pipeline.quota_status()


# ================================================================ conteúdo

@tool()
def script_guidelines(channel_id: int, kind: Literal["short", "long"] = "short") -> str:
    """Regras para escrever um roteiro para este canal (idioma, nicho, estilo, tamanho)."""
    ch = service.require_channel(channel_id)
    s = ch["settings"]
    return (
        f"Canal: {ch['name']} | idioma: {s['language']} | público no fuso {s['timezone']}\n"
        f"Formato: {script_shape(ch, kind)}\n"
        f"Nicho: {s['niche']}\nEstilo: {s['style']}\nCategorias: {', '.join(s['content_mix'])}\n"
        "Cada cena = {narration, visual_query}; visual_query sempre em inglês, concreto e visual.\n"
        "Fatos sobre casos reais precisam ser verdadeiros e verificáveis (passe as URLs em `sources`). "
        "Sem violência gráfica; respeite vítimas; ficção deve ir com is_fiction=true.\n"
        "Título até 90 caracteres; descrição com hashtags (Shorts incluem #shorts); 10-15 tags; "
        "thumbnail_text com 2-4 palavras. Tudo no idioma do canal."
    )


@tool()
def list_recent_topics(channel_id: int | None = None, limit: int = 100) -> list[str]:
    """Temas já produzidos (do canal, ou de todos) — use para não repetir assuntos."""
    return db.recent_topics(channel_id, limit)


@tool()
def create_video(
    channel_id: int,
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
    """Cria um vídeo na fila do canal.
    - Com `scenes` + `title`: usa o roteiro informado (não precisa de API key no servidor).
    - Só com `topic` (ou nada): o servidor pesquisa e escreve com a API do Claude.
    `scheduled_at` ('AAAA-MM-DD HH:MM', fuso do canal) é opcional; sem ele usa o próximo melhor horário."""
    v = service.create_video(
        channel_id, kind, topic=topic, title=title, description=description, tags=tags,
        scenes=[s.model_dump() for s in scenes] if scenes else None,
        thumbnail_text=thumbnail_text, is_fiction=is_fiction, scheduled_at=scheduled_at,
        sources=sources, category=category,
    )
    return _summ(v)


@tool()
def list_videos(
    channel_id: int | None = None,
    status: str | None = None,
    kind: Literal["short", "long"] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Lista vídeos (de um canal ou de todos). status: queued, scripting, scripted, rendering, rendered,
    uploading, scheduled, published, failed (aceita vários separados por vírgula)."""
    statuses = status.split(",") if status else None
    return service.summaries(db.list_videos(status=statuses, kind=kind, channel_id=channel_id, limit=limit))


@tool()
def get_video(video_id: int) -> dict:
    """Detalhes completos de um vídeo, incluindo roteiro, descrição, tags e erro."""
    v = db.get_video(video_id)
    if not v:
        raise ValueError("Vídeo não encontrado.")
    return {**v, **service.video_summary(v)}


@tool()
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


@tool()
def move_video(video_id: int, channel_id: int) -> dict:
    """Move um vídeo ainda não enviado para outro canal (ganha um horário livre lá)."""
    return _summ(service.move_video(video_id, channel_id))


@tool()
def approve_video(video_id: int) -> dict:
    """Aprova um vídeo renderizado para upload (quando approval_required está ativo no canal)."""
    return _summ(service.approve(video_id))


@tool()
def retry_video(video_id: int) -> dict:
    """Recoloca um vídeo com falha na fila, a partir da última etapa concluída."""
    return _summ(pipeline.retry(video_id))


@tool()
def retry_failed(channel_id: int | None = None) -> dict:
    """Recoloca na fila todos os vídeos com falha (de um canal ou de todos)."""
    return {"requeued": len(pipeline.retry_failed(channel_id))}


@tool()
def rerender_video(video_id: int) -> dict:
    """Renderiza de novo (ex.: depois de trocar voz ou cor do canal). Só antes do upload."""
    return _summ(pipeline.rerender(video_id))


@tool()
def delete_video(video_id: int) -> str:
    """Remove o vídeo da fila e apaga os arquivos locais (não apaga do YouTube)."""
    pipeline.remove(video_id)
    return "removido"


# ================================================================ agenda e desempenho

@tool()
def best_times(channel_id: int, kind: Literal["short", "long"] = "short", days: int = 7) -> dict:
    """Melhores horários do canal (no fuso do público) e próximos horários livres sugeridos."""
    ch = service.require_channel(channel_id)
    hm = bt.heatmap(ch, kind)
    top = {
        day: [f"{h:02d}h" for h in sorted(range(24), key=lambda x: -scores[x])[:4]]
        for day, scores in zip(hm["days"], hm["scores"])
    }
    return {
        "timezone": hm["timezone"], "adaptive": hm["adaptive"], "samples": hm["samples"],
        "top_hours_by_day": top, "next_free_slots": service.upcoming_slots(channel_id, kind, days),
    }


@tool()
def plan_schedule(channel_id: int | None = None, days_ahead: int | None = None) -> list[dict]:
    """Preenche a cota diária (shorts_per_day / long_per_day) nos melhores horários, de um canal ou de
    todos os ativos. Requer ANTHROPIC_API_KEY no servidor (os roteiros são escritos depois pelo worker)."""
    if not pipeline.has_claude():
        raise ValueError("Servidor sem ANTHROPIC_API_KEY: crie vídeos com create_video passando o roteiro.")
    return service.summaries(pipeline.plan(channel_id, days_ahead))


@tool()
def analytics(channel_id: int) -> dict:
    """Desempenho do canal: totais, top 10, views por hora de publicação, por categoria e histórico."""
    return service.analytics(channel_id)


@tool()
def compare_channels() -> list[dict]:
    """Ranking dos canais por views médias por vídeo — mostra quais nichos valem mais investimento."""
    return service.compare_channels()


@tool()
def refresh_stats(channel_id: int | None = None) -> list[dict]:
    """Atualiza agora as estatísticas do YouTube (de um canal ou de todos os conectados)."""
    return pipeline.refresh_stats(channel_id)


@tool()
def activity_log(channel_id: int | None = None, limit: int = 30, only_errors: bool = False) -> list[dict]:
    """Últimos eventos (ideias, renders, uploads, falhas), de um canal ou de todos."""
    return db.recent_activity(limit, channel_id=channel_id, level="error" if only_errors else None)


@tool()
def cleanup_files(channel_id: int | None = None, older_than_days: int = 7) -> dict:
    """Libera disco apagando os arquivos locais de vídeos já publicados há mais de N dias."""
    return {"cleaned": pipeline.cleanup_files(channel_id, older_than_days)}


# ================================================================ séries

@tool()
def create_series(
    channel_id: int,
    parts: int = 3,
    kind: Literal["short", "long"] = "short",
    topic: str | None = None,
    angle: str | None = None,
    is_fiction: bool = False,
    start_date: str | None = None,
) -> dict:
    """Cria uma série em partes que o servidor escreve com a API do Claude (pesquisa + todos os episódios
    de uma vez, com gancho no fim de cada parte). Os horários são reservados na hora: um episódio por dia,
    nos melhores horários, a partir de `start_date` (AAAA-MM-DD; padrão hoje). Sem topic, o Claude escolhe."""
    return service.create_series(channel_id, parts, kind, topic, angle, is_fiction, start_date)


@tool()
def create_series_from_script(
    channel_id: int,
    title: str,
    episodes: list[EpisodeIn],
    kind: Literal["short", "long"] = "short",
    logline: str = "",
    is_fiction: bool = False,
    sources: list[str] | None = None,
    start_date: str | None = None,
) -> dict:
    """Cria uma série com os episódios já escritos (não precisa de API key no servidor). Regras: a partir
    da parte 2, a 1ª cena resume a anterior em uma frase; todas menos a última terminam com cliffhanger;
    nomes e fatos idênticos entre as partes. Veja script_guidelines para o tamanho de cada episódio."""
    return service.create_series_from_script(
        channel_id, title, [e.model_dump() for e in episodes], kind, logline, is_fiction, sources, start_date)


@tool()
def list_series(channel_id: int | None = None, status: str | None = None) -> list[dict]:
    """Séries (de um canal ou de todos) com progresso. status: queued, writing, ready, done, failed, cancelled."""
    return service.list_series(channel_id, status)


@tool()
def get_series(series_id: int) -> dict:
    """Detalhes da série: episódios, status de cada um, horários e playlist."""
    return service.get_series(series_id)


@tool()
def retry_series(series_id: int) -> dict:
    """Tenta de novo uma série que falhou (ou os episódios com falha que estão travando a sequência)."""
    s = pipeline.retry_series(series_id)
    if not s:
        raise ValueError("Série não encontrada.")
    return service.series_summary(s)


@tool()
def cancel_series(series_id: int) -> dict:
    """Cancela a série e remove da fila os episódios ainda não enviados (os já enviados ficam no YouTube)."""
    s = pipeline.cancel_series(series_id)
    if not s:
        raise ValueError("Série não encontrada.")
    return service.series_summary(s)


# ================================================================ tendências

@tool()
def scan_youtube_trends(
    keywords: list[str] | None = None,
    region: str = "BR",
    language: str = "pt",
    days: int = 14,
    per_keyword: int = 25,
) -> dict:
    """Mede a demanda real no YouTube por tema: vídeos mais vistos dos últimos `days` dias para cada
    palavra-chave, com views/dia, outliers (canal pequeno com views >= 3x inscritos), concentração e uma
    nota de oportunidade 0-100. Sem keywords usa uma lista dark padrão do idioma.
    CUSTO: ~100 unidades de cota da API por palavra-chave (use YOUTUBE_API_KEY de outro projeto)."""
    return trends.scan_youtube(keywords, region=region, language=language, days=days, per_keyword=per_keyword)


@tool()
def scan_social_trends(
    platforms: list[str] | None = None,
    focus: str = "",
    market: str = "Brasil",
) -> dict:
    """Varre a web (via Claude + busca) atrás de temas e formatos sem rosto em alta no TikTok, Kwai,
    Instagram e YouTube, com evidência, link e confiança. Essas redes não têm API pública de tendências,
    então é pesquisa qualitativa. Requer ANTHROPIC_API_KEY no servidor. Leva 1-3 minutos."""
    if not pipeline.has_claude():
        raise ValueError("Requer ANTHROPIC_API_KEY no servidor. Alternativa: pesquise você mesmo na web.")
    return trends.scan_social(platforms, focus=focus, market=market)


@tool()
def list_trend_scans(limit: int = 20) -> list[dict]:
    """Varreduras de tendências já feitas (id, data, plataforma, parâmetros)."""
    return trends.list_scans(limit)


@tool()
def get_trend_scan(scan_id: int) -> dict:
    """Resultado completo de uma varredura de tendências."""
    return trends.get_scan(scan_id)


@tool()
def compare_trend_scans(scan_a: int, scan_b: int) -> list[dict]:
    """Compara duas varreduras do YouTube (antiga → nova): quais temas ganharam ou perderam oportunidade."""
    return trends.compare_scans(scan_a, scan_b)


def run_stdio() -> None:
    mcp.run("stdio")
