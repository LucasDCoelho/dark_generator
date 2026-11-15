# Dark Generator — gerenciador de canais dark e mininovelas

Produza **mininovelas e conteúdo dark** para **TikTok, Kwai, Instagram Reels e YouTube Shorts**
(e vídeos longos no YouTube) e gerencie **vários canais** a partir de um só lugar:
cada canal com seu nicho, idioma, voz, público e agenda; produção e publicação diárias nos
melhores horários; manutenção de tudo **via MCP** (Claude) ou pelo **painel web**.

```
                       ┌─ Canal A (pt-BR, casos reais)  ─┐
 Claude (MCP) / painel ─┼─ Canal B (en-US, unsolved)     ─┼─ worker único → roteiro → narração → vídeo → upload agendado
                       └─ Canal C (pt-BR, contos terror) ─┘      prioriza o vídeo com horário mais próximo
```

## Começo rápido: mininovelas em todas as redes

1. Defina `ELEVENLABS_API_KEY` (vozes naturais) e, se quiser o piloto automático, `ANTHROPIC_API_KEY`.
2. Crie um canal com o modelo **Mininovela de suspense** ou **Mininovela de drama**: capítulos de
   61–110s (o TikTok só paga vídeos com mais de 1 minuto), 2–4 personagens fixos, uma voz da
   ElevenLabs por personagem, letreiros "PARTE N" e "CONTINUA NA PARTE N+1".
3. O piloto automático escreve uma novela nova a cada poucos dias; ou peça ao Claude via MCP:
   *"crie uma mininovela de 5 capítulos sobre uma herança maldita"* (`create_novela`).
4. Abra **Pronto para postar** no celular: baixe o MP4, copie a legenda de cada rede (TikTok,
   Kwai, Reels, Shorts), ative o rótulo de IA, poste e marque como postado. Anote as views depois
   para o sistema comparar redes e novelas.

> Por que manual? TikTok e YouTube só liberam postagem pública por API depois de auditar o app,
> e o Kwai não tem API de postagem. Postando pelo app, o vídeo sai público na hora em todas.

## O que ele faz

- **Vários canais, cada um com perfil próprio**: nicho/linha editorial, estilo de narração, idioma,
  fuso do público, voz, cor das legendas, cotas diárias (Shorts/longos), aprovação manual,
  piloto automático e conta do YouTube.
- **13 modelos de nicho prontos**, revisados com base na pesquisa de tendências: terror em série,
  história que não te contaram, sobreviventes e desastres, psicologia do comportamento,
  liminal/Backrooms (EN), Unsolved Mysteries (EN), Misterios (ES), entre outros. Canais em inglês
  costumam ter RPM bem maior.
- **Roteiros com Claude** (`claude-opus-5-5`) no idioma e estilo do canal, com pesquisa na web
  para casos reais (fontes na descrição). Ou o próprio Claude escreve via MCP, sem API key.
- **Mininovelas**: ficha de personagens, diálogos, uma voz fixa por personagem (ElevenLabs), nome de
  quem fala na tela, letreiros de capítulo e duração mínima configurável (61s para o TikTok).
- **Publicação manual multiplataforma** (padrão): fila "Pronto para postar" com download do MP4 e
  legenda/hashtags no formato de cada rede, lembrete do rótulo de IA, registro de onde foi postado e
  das views — com comparação por rede e retenção entre capítulos. Upload automático no YouTube
  continua disponível (`publish_mode = youtube_api`) para quando a auditoria da API sair.
- **Vídeo pronto**: voz (ElevenLabs ou edge-tts), legendas palavra a palavra na cor do canal, fundos
  Pexels com color grading sombrio (ou procedurais), trilha com *ducking*, -14 LUFS, thumbnail.
- **Séries em partes automáticas**: o Claude escreve todos os episódios de uma vez (mesma história,
  nomes e fatos), cada parte termina com gancho e a seguinte abre com um resumo rápido. Os episódios
  saem **um por dia, sempre em ordem**, nos melhores horários; a descrição traz o link da parte
  anterior e cada série ganha uma **playlist** no YouTube. Com `auto_series`, o piloto automático
  inicia uma série nova a cada N dias.
- **Melhores horários por canal**, no fuso do público, que se ajustam às views de cada canal.
  Upload como privado com `publishAt`: o próprio YouTube publica na hora.
- **Manutenção**: diagnóstico de saúde por canal (conexão, falhas, agenda vazia, aprovações
  pendentes, cota, inatividade, queda de views) com a ação sugerida para cada problema;
  pausar/retomar, duplicar canal, mover vídeo entre canais, refazer falhas, limpar arquivos.
- **Cota do YouTube sob controle**: a cota é por projeto do Google Cloud; o sistema conta os
  uploads por projeto, segura a fila quando chega no limite e permite uma credencial OAuth
  própria por canal para dividir a carga.
- **Scanner de tendências**: mede a demanda real no YouTube (views/dia e vídeos "outlier" de
  canais pequenos por palavra-chave e região) e varre a web atrás do que está em alta no TikTok,
  Kwai e Instagram. Pesquisa completa de nichos em
  [`docs/pesquisa-tendencias-2026-11.md`](docs/pesquisa-tendencias-2026-11.md).
- **Painel web**: portfólio com todos os canais, comparação de desempenho, criação de canal
  com modelos, e por canal: visão geral, fila com preview, criação de vídeos, heatmap de
  horários, desempenho e configurações.

## Instalação

Requisitos: Python 3.11+, `ffmpeg` e fonte DejaVu (`apt install ffmpeg fonts-dejavu-core`).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # preencha as chaves
python -m darkgen serve     # painel em http://127.0.0.1:8000
```

Docker (para rodar 24h num servidor):

```bash
docker build -t darkgen .
docker run -d --name darkgen -p 8000:8000 -v $PWD/data:/data --env-file .env darkgen
```

| Variável | Para quê |
|---|---|
| `ANTHROPIC_API_KEY` | Piloto automático (ideias, pesquisa, roteiros). Opcional se os roteiros vierem via MCP. |
| `ELEVENLABS_API_KEY` | Vozes naturais (narrador + uma voz por personagem). Sem ela, usa edge-tts gratuito. |
| `PEXELS_API_KEY` | Vídeos reais de fundo (opcional). |
| `DASHBOARD_TOKEN` | Protege painel/API/MCP fora do localhost. |
| `PUBLIC_BASE_URL` | URL pública (callback OAuth e links gerados pelo MCP) em servidor remoto. |
| `YOUTUBE_UPLOADS_PER_PROJECT_PER_DAY` | Limite de uploads por projeto do Google/dia (padrão 6). |
| `YOUTUBE_API_KEY` | Chave simples da YouTube Data API para o scanner de tendências — use **outro projeto** para não gastar a cota de uploads (cada palavra-chave custa ~100 unidades). Sem ela, usa o OAuth de um canal conectado. |

Trilhas próprias (livres de direitos) em `data/music/` substituem o drone procedural.

## Criar e conectar um canal

O YouTube **não permite criar canais pela API**. O fluxo é:

1. Crie o canal no YouTube — de preferência como **Conta de Marca**, assim vários canais ficam
   sob o mesmo login do Google.
2. No painel → **+ Novo canal** (ou `create_channel` via MCP), escolha um modelo de nicho e ajuste.
3. Na primeira vez, em **Configurações gerais**, envie o `client_secret.json`:
   Google Cloud Console → ative a **YouTube Data API v3** → crie um *ID do cliente OAuth*
   (“App para computador”, ou “Aplicativo da Web” com o redirect
   `http://localhost:8000/api/youtube/callback`).
4. Nas configurações do canal, clique em **Conectar YouTube** e, na tela do Google, **escolha o
   canal certo** (Conta de Marca). Via MCP, `connect_youtube` devolve o link.

> ⚠️ **Auditoria da API:** projetos do Google não verificados só conseguem enviar vídeos
> **privados**. Solicite a [auditoria da YouTube API](https://support.google.com/youtube/contact/yt_api_form)
> para publicar de verdade.
>
> **Cota:** cada projeto tem 10.000 unidades/dia, e o upload é a operação mais cara (historicamente
> ~1.600 unidades — confira a tabela atual). Com vários canais, crie **um projeto por grupo de
> canais** e envie a credencial própria nas configurações de cada canal; o painel mostra o uso
> por projeto.

## Usar via MCP

**Claude Code** (nesta pasta): o `.mcp.json` já registra o servidor `dark-generator`.

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

**Remoto (HTTP)**: com o `serve` rodando, o endpoint é `http://HOST:PORT/mcp`
(`Authorization: Bearer <DASHBOARD_TOKEN>` se definido).

> O modo stdio grava na fila e consulta o estado; quem renderiza e envia é o `serve`.

### Ferramentas MCP (50)

| Grupo | Ferramentas |
|---|---|
| **Portfólio** | `portfolio_overview`, `list_channels`, `compare_channels`, `quota_status` |
| **Canais** | `list_niche_presets`, `create_channel`, `get_channel`, `update_channel` (inclui pausar), `clone_channel`, `delete_channel`, `connect_youtube`, `disconnect_youtube` |
| **Manutenção** | `channel_health`, `retry_failed`, `retry_video`, `rerender_video`, `cleanup_files`, `activity_log`, `refresh_stats` |
| **Conteúdo** | `script_guidelines`, `list_recent_topics`, `create_video`, `list_videos`, `get_video`, `update_video`, `move_video`, `approve_video`, `delete_video` |
| **Agenda e dados** | `best_times`, `plan_schedule`, `analytics` |
| **Mininovelas** | `create_novela`, `create_series_from_script` (com `characters` e cenas com `lines`), `list_elevenlabs_voices` |
| **Postagem manual** | `list_ready_to_post`, `get_post_kit`, `mark_posted`, `unmark_posted`, `update_post_stats`, `platform_stats` |
| **Séries** | `create_series`, `create_series_from_script`, `list_series`, `get_series`, `retry_series`, `cancel_series` |
| **Tendências** | `scan_youtube_trends`, `scan_social_trends`, `list_trend_scans`, `get_trend_scan`, `compare_trend_scans` |

Exemplos de pedidos ao Claude:

- *"Crie um canal 'Unsolved Files' com o modelo em inglês, 3 Shorts por dia e aprovação manual, e me
  dê o link para conectar o YouTube."*
- *"Rode o diagnóstico de todos os canais e resolva o que der: refaça as falhas e preencha as agendas vazias."*
- *"Compare os canais. O nicho que mais rende: duplique para espanhol."*
- *"Crie uma série de 4 partes no canal de contos sobre um hotel que não aparece no mapa, começando amanhã."*
- *"Escreva você mesmo uma série de 3 Shorts sobre o desaparecimento do voo 19 para o canal 2 e agende."*
- *"Varra as tendências do YouTube nos EUA e no Brasil para sobrevivência, backrooms e casos não
  resolvidos, cruze com o que está em alta no TikTok e Kwai e me diga qual canal criar a seguir."*
- *"Para o canal 2, escreva 3 Shorts sobre casos pouco conhecidos que ainda não usamos e agende nos melhores horários."*

## Comandos

```bash
python -m darkgen serve                    # painel + API + MCP HTTP + agendador (uso normal)
python -m darkgen mcp                      # MCP via stdio
python -m darkgen channels                 # lista canais e problemas
python -m darkgen presets                  # modelos de nicho
python -m darkgen new-channel "Nome" --preset unsolved_en
python -m darkgen plan [--channel 2] [--days 2]
python -m darkgen work                     # processa a fila sem o agendador
python -m darkgen stats [--channel 2]
pytest                                     # testes
```

Atualizando da versão de canal único: o banco é migrado automaticamente na primeira execução
(o canal existente vira o canal #1, com vídeos, histórico e token do YouTube).

## Séries

- Os horários de todos os episódios são **reservados na criação** (um por dia, a partir da data de
  início), então contam na cota diária do canal e o planejador preenche o resto em volta.
- Um episódio só é enviado depois que o anterior já está no YouTube; se a parte 2 falhar, a 3
  espera (o diagnóstico do canal avisa e `retry_series` resolve). Se um horário ficar fora de ordem,
  o episódio é reagendado para depois do anterior.
- **Playlists** exigem uma permissão extra do YouTube: canais conectados antes desta versão
  continuam publicando normalmente, e o painel pede para **reconectar** quando quiser as playlists.
- Modelos com séries automáticas ligadas: contos de terror, sobreviventes e liminal (EN). Em
  qualquer canal: Configurações do canal → “Séries automáticas”.

## Vozes (ElevenLabs)

- Cada fala é gerada com a voz do personagem e o resultado fica em cache: renderizar de novo não
  gasta créditos.
- As vozes padrão são vozes prontas da ElevenLabs em inglês; o modelo multilíngue fala português
  com elas, mas o sotaque fica melhor com **vozes brasileiras**: adicione-as à sua conta pela Voice
  Library e escolha em Configurações do canal → Voz → "Escolher vozes da minha conta" (ou
  `list_elevenlabs_voices` + `update_channel` via MCP).
- Modelos: Multilingual v2 (padrão, estável), v3 (mais expressivo), Turbo/Flash v2.5 (mais baratos).
- A ElevenLabs cobra por caractere: um capítulo de ~200 palavras tem ~1.100 caracteres.

## Como funciona a agenda

- Horários no **fuso do público de cada canal**. Shorts: picos às 12h e 19h–22h (fim de semana a
  partir das 10h). Longos: ~1–2h antes do pico (17h–20h) para indexar.
- Após 6 vídeos publicados com 48h+, o canal passa a ter seu próprio heatmap ajustado pelas views.
- Intervalo mínimo entre publicações do mesmo canal (padrão 3h). Vídeo que perdeu o horário vai
  para o próximo melhor horário livre do canal.

## Boas práticas e riscos (leia!)

- **Conteúdo inautêntico / spam**: o YouTube desmonetiza conteúdo produzido em massa e repetitivo,
  e pode penalizar **redes de canais com conteúdo duplicado**. Cada canal deve ter identidade e
  conteúdo próprios: não republique o mesmo vídeo em vários canais, varie nichos, estilos e formatos,
  e use a aprovação manual no começo.
- **Casos reais**: o sistema pesquisa e cita fontes, mas revise fatos sobre pessoas reais.
- **Conteúdo sintético**: se houver material realista que possa ser confundido com real, ative
  "conteúdo alterado ou sintético" no canal.
- **edge-tts** é não oficial; para produção séria, plugue um TTS comercial em `darkgen/media/tts.py`.

## Estrutura

```
darkgen/
  config.py            variáveis de ambiente, padrões por canal e modelos de nicho
  db.py                SQLite: canais, vídeos, estado, atividade (+ migração da v1)
  content/writer.py    Claude: ideias, pesquisa na web, roteiro no idioma/estilo do canal
  media/               tts.py (ElevenLabs/edge), voices.py, subtitles.py, visuals.py, render.py (ffmpeg)
  youtube/client.py    OAuth por canal, upload com publishAt, estatísticas, erros de cota
  scheduling/          melhores horários por canal (curva base + adaptação)
  pipeline.py          worker multi-canal, séries/novelas, cota por projeto, planejador, estatísticas
  publishing.py        fila "Pronto para postar", legendas por rede, postagens e números por plataforma
  service.py           canais, portfólio, saúde, vídeos (compartilhado por painel e MCP)
  trends.py            scanner de tendências (YouTube Data API + pesquisa web das redes)
  mcp_server.py        servidor MCP
  web/                 FastAPI + painel (HTML/CSS/JS sem build)
```
