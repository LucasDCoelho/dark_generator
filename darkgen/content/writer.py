"""Geração de ideias, pesquisa e roteiros com a API do Claude, conforme o perfil de cada canal."""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel, Field

from ..config import env, lang_text

log = logging.getLogger(__name__)

# Fallback server-side: se o modelo principal recusar, a API tenta outro modelo na mesma chamada.
_BETAS = ["server-side-fallback-2026-07-01"]


class Idea(BaseModel):
    topic: str = Field(description="Assunto específico e pesquisável (nome do caso, lugar, data).")
    category: str = Field(description="Uma das categorias permitidas.")
    angle: str = Field(description="O ângulo/enfoque que torna o vídeo único.")
    hook: str = Field(description="Primeira frase do vídeo, que prende nos 2 primeiros segundos.")
    is_fiction: bool = Field(description="True para lendas/creepypastas narradas como ficção.")


class IdeaList(BaseModel):
    ideas: list[Idea]


class Scene(BaseModel):
    narration: str = Field(description="Trecho da narração falado nesta cena, no idioma do canal.")
    visual_query: str = Field(
        description="2-4 palavras em INGLÊS para buscar vídeo de banco de imagens (ex.: 'foggy forest night')."
    )


class VideoScript(BaseModel):
    title: str = Field(description="Título do YouTube, até 90 caracteres, curiosidade sem clickbait mentiroso.")
    description: str = Field(description="Descrição do YouTube com 2-4 parágrafos e chamada para inscrição.")
    tags: list[str] = Field(description="10 a 15 tags relevantes.")
    thumbnail_text: str = Field(description="2 a 4 palavras de impacto para a thumbnail.")
    is_fiction: bool
    scenes: list[Scene]


class SeriesEpisode(BaseModel):
    title: str = Field(description="Título do episódio, até 90 caracteres, terminando com o indicador da parte (ex.: '(Parte 2)').")
    description: str = Field(description="Descrição do episódio com hashtags.")
    tags: list[str] = Field(description="10 a 15 tags.")
    thumbnail_text: str = Field(description="2 a 4 palavras de impacto.")
    scenes: list[Scene]


class SeriesScript(BaseModel):
    series_title: str = Field(description="Nome da série (vira o nome da playlist).")
    logline: str = Field(description="Resumo da série em 1-2 frases, sem spoiler do final.")
    is_fiction: bool
    episodes: list[SeriesEpisode]


class Research(BaseModel):
    dossier: str
    sources: list[str]


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def system_prompt(channel: dict) -> str:
    s = channel["settings"]
    lang = lang_text(s["language"])["name"]
    return f"""Você é roteirista-chefe do canal dark do YouTube "{channel['name']}".
Idioma de TODO o texto para o público (títulos, descrições, tags, narração): {lang}.
Linha editorial: {s['niche']}
Estilo de narração: {s['style']}

Regras editoriais:
- Fatos sobre pessoas e casos reais devem ser verdadeiros. Nunca invente nomes, datas, números,
  falas ou conclusões. Quando algo é incerto ou só existe em relatos, diga isso na narração.
- Respeite as vítimas: nada de detalhes gráficos gratuitos de violência; foque no mistério,
  no contexto e nas perguntas sem resposta. O conteúdo deve ser adequado para anunciantes.
- Histórias de ficção (lendas, creepypastas) devem ser apresentadas como lenda ou ficção.
- Linguagem falada, frases curtas, ritmo de suspense, sem emojis nem marcações na narração.
- Cada vídeo deve ser original: ângulo próprio, nada de reaproveitar estruturas repetitivas.
"""


def _check(resp) -> None:
    if resp.stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        raise RuntimeError(f"O modelo recusou o pedido ({getattr(details, 'category', None)}).")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Resposta truncada (max_tokens).")


def generate_ideas(channel: dict, count: int, kind: str, avoid: list[str], series_parts: int | None = None) -> list[Idea]:
    """Gera `count` ideias novas para o canal, evitando temas já usados."""
    s = channel["settings"]
    avoid_txt = "\n".join(f"- {t}" for t in avoid[:150]) or "(nenhum ainda)"
    fmt = "YouTube Shorts verticais de 45-60 segundos" if kind == "short" else "vídeos longos de 7-10 minutos"
    if series_parts:
        fmt = (f"SÉRIE em {series_parts} partes ({fmt} cada). A ideia precisa ter história suficiente para "
               f"{series_parts} episódios, com reviravoltas que sustentem um gancho no fim de cada parte")
    prompt = (
        f"Formato: {fmt}\n"
        f"Categorias permitidas: {', '.join(s['content_mix'])}\n\n"
        f"Sugira {count} ideias de vídeo inéditas, variadas entre as categorias, com forte potencial de "
        "retenção e busca para o público do canal. Prefira temas reais e documentados quando a linha "
        "editorial não for ficção. Não repita nem faça variações destes temas já produzidos:\n"
        f"{avoid_txt}"
    )
    resp = _client().beta.messages.parse(
        model=env().claude_model,
        max_tokens=16000,
        system=system_prompt(channel),
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        betas=_BETAS,
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
        output_format=IdeaList,
    )
    _check(resp)
    return resp.parsed_output.ideas[:count]


def research(topic: str, angle: str = "") -> Research:
    """Pesquisa o tema na web e devolve um dossiê factual com fontes."""
    client = _client()
    messages: list = [
        {
            "role": "user",
            "content": (
                f"Pesquise na web o tema: {topic}\n{('Enfoque: ' + angle) if angle else ''}\n\n"
                "Monte um dossiê factual com: contexto, linha do tempo, pessoas envolvidas "
                "(só o que for público), fatos verificados, pontos controversos/não resolvidos e teorias. "
                "Marque o que é confirmado e o que é especulação. Seja conciso (até 600 palavras)."
            ),
        }
    ]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    resp = None
    for _ in range(5):  # continua enquanto o servidor pausar o turno (pause_turn)
        resp = client.beta.messages.create(
            model=env().claude_model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            betas=_BETAS,
            fallbacks="default",
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = [*messages, {"role": "assistant", "content": resp.content}]
    assert resp is not None
    _check(resp)

    sources: list[str] = []
    text_parts: list[str] = []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
            for cit in getattr(block, "citations", None) or []:
                url = getattr(cit, "url", None)
                if url and url not in sources:
                    sources.append(url)
        elif block.type == "web_search_tool_result" and isinstance(block.content, list):
            for r in block.content:
                url = getattr(r, "url", None)
                if url and url not in sources:
                    sources.append(url)
    return Research(dossier="".join(text_parts).strip(), sources=sources[:8])


def script_shape(channel: dict, kind: str) -> str:
    s = channel["settings"]
    wpm = lang_text(s["language"])["wpm"]
    if kind == "short":
        lo, hi = int(wpm * 0.85), int(wpm * 1.05)
        return (
            f"YouTube Short vertical. Narração total de {lo} a {hi} palavras (45-58 segundos). "
            "4 a 7 cenas curtas. A primeira frase é o gancho e precisa prender em 2 segundos. "
            "Termine com uma pergunta ou revelação que faça a pessoa comentar ou rever o vídeo."
        )
    mins = s["long_target_minutes"]
    return (
        f"Vídeo longo horizontal. Narração total de cerca de {mins * wpm} palavras (~{mins} minutos). "
        "12 a 25 cenas. Estrutura: gancho forte (15s), contexto, desenvolvimento com tensão crescente, "
        "virada/revelação, perguntas em aberto e encerramento pedindo inscrição e comentário."
    )


def write_script(
    channel: dict,
    topic: str,
    kind: str,
    *,
    category: str = "",
    angle: str = "",
    hook: str = "",
    is_fiction: bool = False,
    dossier: str = "",
) -> VideoScript:
    prompt = (
        f"Escreva o roteiro completo.\nTema: {topic}\nCategoria: {category or 'livre'}\n"
        f"Enfoque: {angle or 'livre'}\nGancho sugerido: {hook or 'crie um'}\n"
        f"Ficção: {'sim, apresente como lenda/ficção' if is_fiction else 'não, conteúdo factual'}\n\n"
        f"Formato: {script_shape(channel, kind)}\n\n"
        "A descrição deve resumir o vídeo sem spoiler e terminar com 3 a 5 hashtags."
        + (" Inclua #shorts entre as hashtags." if kind == "short" else "")
        + ("\n\nDossiê de pesquisa (use apenas estes fatos para afirmações sobre o caso):\n" + dossier if dossier else "")
    )
    resp = _client().beta.messages.parse(
        model=env().claude_model,
        max_tokens=16000,
        system=system_prompt(channel),
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        betas=_BETAS,
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
        output_format=VideoScript,
    )
    _check(resp)
    script = resp.parsed_output
    script.is_fiction = script.is_fiction or is_fiction
    return script


def write_series(
    channel: dict,
    topic: str,
    parts: int,
    kind: str,
    *,
    angle: str = "",
    is_fiction: bool = False,
    dossier: str = "",
) -> SeriesScript:
    """Escreve todos os episódios de uma vez, para manter personagens, fatos e tom coerentes."""
    part_word = lang_text(channel["settings"]["language"])["part"]
    prompt = (
        f"Escreva uma SÉRIE de {parts} episódios sobre: {topic}\n"
        f"Enfoque: {angle or 'livre'}\n"
        f"Ficção: {'sim, apresente como lenda/ficção' if is_fiction else 'não, conteúdo factual'}\n\n"
        f"Formato de CADA episódio: {script_shape(channel, kind)}\n\n"
        "Regras da série:\n"
        f"- Exatamente {parts} episódios, na ordem. O título de cada um termina com '({part_word} N)'.\n"
        "- Arco único: apresente o mistério na parte 1, aprofunde e vire a história nas do meio, e "
        "entregue a resolução (ou, em casos reais sem solução, o que se sabe e as perguntas em aberto) na última.\n"
        "- A partir da parte 2, a PRIMEIRA cena começa com um resumo de uma frase do que aconteceu antes, "
        "já emendando num novo gancho.\n"
        f"- Todas as partes, menos a última, terminam com um cliffhanger e um convite para a próxima "
        f"('a {part_word.lower()} N+1 sai amanhã').\n"
        "- Cada episódio precisa funcionar sozinho para quem cair nele primeiro.\n"
        "- Personagens, nomes, datas e fatos idênticos em todas as partes.\n"
        "- A descrição de cada parte termina com 3 a 5 hashtags"
        + (" incluindo #shorts." if kind == "short" else ".")
        + ("\n\nDossiê de pesquisa (use apenas estes fatos para afirmações sobre o caso):\n" + dossier if dossier else "")
    )
    with _client().beta.messages.stream(
        model=env().claude_model,
        max_tokens=64000,
        system=system_prompt(channel),
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        betas=_BETAS,
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
        output_format=SeriesScript,
    ) as stream:
        resp = stream.get_final_message()
    _check(resp)
    script = resp.parsed_output
    if script is None or len(script.episodes) < parts:
        raise RuntimeError(f"A série veio com {0 if script is None else len(script.episodes)} de {parts} episódios.")
    script.episodes = script.episodes[:parts]
    script.is_fiction = script.is_fiction or is_fiction
    return script
