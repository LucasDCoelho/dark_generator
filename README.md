# Dark Generator

Gerador de conteúdo **dark** (mistérios, casos reais, lendas, curiosidades sombrias) para YouTube —
vídeos longos e **Shorts** — com publicação diária nos melhores horários, controle total **via MCP**
e um **painel web** para gerenciar o canal.

```
ideia → pesquisa na web → roteiro → narração (TTS) → vídeo (fundos + legendas + trilha) → upload agendado
  Claude API ou você via MCP          edge-tts          ffmpeg                     YouTube Data API (publishAt)
```

## O que ele faz

- **Roteiros com Claude** (`claude-opus-5-5`): gera ideias inéditas, pesquisa casos reais na web
  (fontes vão para a descrição) e escreve o roteiro em cenas. Ficção é marcada como ficção.
- **Vídeo pronto para postar**: narração em pt-BR (edge-tts), legendas sincronizadas palavra a
  palavra (estilo Shorts), fundos de vídeo do Pexels com color grading sombrio (ou fundos procedurais,
  sem chave nenhuma), trilha ambiente com *ducking* e loudness em -14 LUFS, thumbnail.
- **Agenda nos melhores horários**: curva base para o público brasileiro (picos no almoço e à noite)
  que **se ajusta sozinha** com as views dos seus próprios vídeos. O upload é feito como privado com
  `publishAt`, então **o próprio YouTube publica no horário** — o servidor só precisa estar ligado
  para produzir e enviar com antecedência.
- **Piloto automático**: todo dia preenche a cota (padrão: 2 Shorts + 1 longo) e cuida do resto.
  Opcionalmente exige sua aprovação antes de cada upload.
- **MCP**: 18 ferramentas para o Claude (Desktop, Code ou qualquer cliente MCP) operar o canal —
  inclusive escrever os roteiros ele mesmo, sem precisar de API key no servidor.
- **Painel web**: visão geral, fila de produção com preview do vídeo, criação manual, heatmap de
  horários, desempenho e configurações.

## Instalação

Requisitos: Python 3.11+, `ffmpeg` e uma fonte DejaVu (`apt install ffmpeg fonts-dejavu-core`).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # preencha as chaves
python -m darkgen serve     # painel em http://127.0.0.1:8000
```

Ou com Docker:

```bash
docker build -t darkgen .
docker run -d --name darkgen -p 8000:8000 -v $PWD/data:/data --env-file .env darkgen
```

### Chaves

| Variável | Para quê | Obrigatória? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Piloto automático: ideias, pesquisa e roteiros | Só para o modo automático. Via MCP o Claude escreve os roteiros. |
| `PEXELS_API_KEY` | Vídeos reais de fundo (gratuito em pexels.com/api) | Não — sem ela usa fundos procedurais |
| `DASHBOARD_TOKEN` | Protege painel/API/MCP quando exposto fora do localhost | Recomendado em servidor |
| `PUBLIC_BASE_URL` | URL pública, para o callback do OAuth em servidor | Só em servidor remoto |

Trilha própria: coloque arquivos `.mp3`/`.wav` (sem direitos autorais) em `data/music/` e ela será
usada no lugar do drone procedural.

## Conectar o canal do YouTube

1. No [Google Cloud Console](https://console.cloud.google.com/), crie um projeto e ative a
   **YouTube Data API v3**.
2. Configure a tela de consentimento OAuth (adicione seu e-mail como usuário de teste).
3. Crie um **ID do cliente OAuth** do tipo *App para computador* (ou *Aplicativo da Web* com o
   redirect `http://localhost:8000/api/youtube/callback`).
4. No painel → **Configurações**, envie o `client_secret.json` e clique em **Conectar canal**.

> ⚠️ **Importante — auditoria da API:** vídeos enviados por projetos de API **não verificados**
> ficam **travados como privados** pelo YouTube. Para publicar de verdade, peça a
> [auditoria/verificação da YouTube API](https://support.google.com/youtube/contact/yt_api_form)
> para o seu projeto. Até lá, os uploads funcionam, mas ficam privados.
>
> **Cota:** a cota padrão é de 10.000 unidades/dia e o upload é a operação mais cara (historicamente
> ~1.600 unidades — confira a tabela de custos atual no Google Cloud). O padrão de 3 vídeos/dia cabe.

## Usar via MCP

**Claude Code** (nesta pasta): o arquivo `.mcp.json` já registra o servidor — basta abrir o Claude
Code no diretório do projeto e aprovar o servidor `dark-generator`.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "dark-generator": {
      "command": "/caminho/para/dark_generator/.venv/bin/python",
      "args": ["-m", "darkgen", "mcp"],
      "env": { "DATA_DIR": "/caminho/para/dark_generator/data" }
    }
  }
}
```

**Remoto (HTTP)**: com `python -m darkgen serve` rodando, o endpoint MCP é `http://HOST:PORT/mcp`
(envie `Authorization: Bearer <DASHBOARD_TOKEN>` se definido).

> O modo stdio apenas grava na fila e consulta o estado; quem renderiza e envia é o
> `python -m darkgen serve`. Mantenha-o rodando (ex.: Docker num VPS).

### Ferramentas MCP

| Ferramenta | O que faz |
|---|---|
| `channel_status` | Conexões, worker, contagens, próximos vídeos, configurações |
| `script_guidelines` | Regras de formato do roteiro (Short ou longo) |
| `list_recent_topics` | Temas já usados, para não repetir |
| `create_video` | Cria vídeo — com roteiro pronto (`scenes`) ou só tema (Claude API escreve) |
| `list_videos` / `get_video` | Consulta a fila e detalhes |
| `update_video` | Edita título, descrição, tags, horário ou roteiro (antes do upload) |
| `approve_video` | Libera upload quando a aprovação manual está ligada |
| `retry_video` / `rerender_video` / `delete_video` | Gestão da fila |
| `best_times` | Heatmap e próximos horários livres |
| `plan_schedule` | Preenche a agenda automaticamente (requer Claude API) |
| `analytics` / `refresh_stats` | Desempenho e atualização das estatísticas |
| `get_settings` / `update_settings` | Configurações do canal |
| `activity_log` | Últimos eventos |

Exemplo de pedido ao Claude: *"Veja o status do canal, escolha 3 casos reais pouco conhecidos que
ainda não usamos, pesquise e crie 3 Shorts para amanhã nos melhores horários."*

## Comandos

```bash
python -m darkgen serve          # painel + API + MCP HTTP + agendador (uso normal)
python -m darkgen mcp            # MCP via stdio
python -m darkgen plan --days 2  # preenche a agenda agora
python -m darkgen work           # processa a fila sem o agendador
python -m darkgen stats          # atualiza estatísticas do YouTube
pytest                           # testes
```

## Como funciona a agenda

- **Shorts**: picos às 12h e 19h–22h (fim de semana a partir das 10h).
- **Longos**: publicados ~1–2h antes do pico (17h–20h) para dar tempo de indexar.
- Depois de 6 vídeos publicados há mais de 48h, o sistema compara as views por hora de publicação
  com a média do canal e ajusta o heatmap (com peso crescente conforme acumula dados).
- Intervalo mínimo entre publicações configurável (padrão 3h). Se um vídeo perder o horário
  (ex.: servidor desligado), vai para o próximo melhor horário livre.

## Boas práticas e riscos (leia!)

- **Monetização**: o YouTube desmonetiza conteúdo *"inautêntico"* — produzido em massa, repetitivo
  ou com pouco valor agregado. Use a aprovação manual no começo, varie formatos e temas, revise
  roteiros e prefira qualidade a volume.
- **Casos reais**: o sistema pesquisa e cita fontes, mas revise fatos sobre pessoas reais antes de
  publicar. Erros sobre pessoas reais podem gerar problemas legais.
- **Conteúdo sintético**: se o vídeo tiver material realista que possa ser confundido com real,
  ative "Marcar como conteúdo alterado ou sintético" nas configurações.
- **edge-tts** usa o serviço de voz do Microsoft Edge (não oficial). Para produção séria, considere
  plugar um TTS comercial em `darkgen/media/tts.py`.
- **Músicas**: use apenas faixas livres de direitos em `data/music/`.

## Estrutura

```
darkgen/
  config.py            variáveis de ambiente + configurações padrão do canal
  db.py                SQLite: vídeos, configurações, atividade
  content/writer.py    Claude: ideias, pesquisa na web, roteiro estruturado
  media/               tts.py, subtitles.py, visuals.py, render.py (ffmpeg)
  youtube/client.py    OAuth, upload com publishAt, estatísticas
  scheduling/          melhores horários (curva base + adaptação)
  pipeline.py          worker da fila + planejador diário
  service.py           regras compartilhadas por painel e MCP
  mcp_server.py        servidor MCP
  web/                 FastAPI + painel (HTML/CSS/JS sem build)
```
