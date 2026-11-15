"""Configuração estática (variáveis de ambiente), padrões por canal e modelos de nicho."""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    claude_model: str = "claude-opus-5-5"
    pexels_api_key: str = ""
    data_dir: Path = Path("./data")
    host: str = "127.0.0.1"
    port: int = 8000
    dashboard_token: str = ""
    tts_provider: str = "edge"  # edge | silent
    public_base_url: str = ""  # ex.: https://meu-servidor.com (callback OAuth e links do MCP)
    # Uploads por dia por projeto do Google Cloud (a cota é por projeto, não por canal).
    youtube_uploads_per_project_per_day: int = 6

    @property
    def db_path(self) -> Path:
        return self.data_dir / "darkgen.sqlite3"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def music_dir(self) -> Path:
        return self.data_dir / "music"

    @property
    def channels_dir(self) -> Path:
        return self.data_dir / "channels"

    @property
    def youtube_client_secret(self) -> Path:
        """Credencial OAuth global (cada canal pode ter a sua, para dividir a cota)."""
        return self.data_dir / "client_secret.json"

    def channel_dir(self, channel_id: int) -> Path:
        d = self.channels_dir / str(channel_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def base_url(self) -> str:
        return self.public_base_url.rstrip("/") or f"http://{'localhost' if self.host in ('127.0.0.1', '0.0.0.0') else self.host}:{self.port}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.videos_dir, self.music_dir, self.channels_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def env() -> EnvSettings:
    s = EnvSettings()
    s.ensure_dirs()
    return s


def tz_of(channel: dict) -> ZoneInfo:
    return ZoneInfo(channel["settings"]["timezone"])


# Configurações de cada canal (editáveis no painel / via MCP).
DEFAULT_CHANNEL_SETTINGS: dict = {
    "autopilot": True,  # gera e agenda sozinho todos os dias
    "approval_required": False,  # se True, vídeos esperam aprovação antes do upload
    "shorts_per_day": 2,
    "long_per_day": 1,
    "plan_days_ahead": 1,  # quantos dias à frente manter preenchidos
    "min_gap_hours": 3,  # intervalo mínimo entre publicações do mesmo canal
    "niche": (
        "canal dark brasileiro: casos reais perturbadores, mistérios não resolvidos, lendas urbanas, "
        "fenômenos inexplicáveis e curiosidades sombrias da história"
    ),
    "style": "narrador sério e atmosférico, frases curtas, suspense crescente",
    "content_mix": ["caso_real", "misterio", "lenda_urbana", "curiosidade_sombria", "historia_sombria"],
    "language": "pt-BR",
    "timezone": "America/Sao_Paulo",  # fuso do PÚBLICO-ALVO (define os melhores horários)
    "voice": "pt-BR-AntonioNeural",
    "voice_rate": "-6%",
    "voice_pitch": "-4Hz",
    "category_id": "24",  # Entretenimento
    "default_tags": ["dark", "mistério", "terror", "casos reais", "curiosidades"],
    "synthetic_media_disclosure": False,
    "research_with_web": True,  # pesquisa na web antes de escrever casos reais
    "music_volume": 0.14,
    "long_target_minutes": 8,
    "subtitle_color": "#FF4B4B",  # cor de destaque das legendas dos Shorts (identidade visual)
}


def merge_settings(stored: dict | None) -> dict:
    base = copy.deepcopy(DEFAULT_CHANNEL_SETTINGS)
    base.update({k: v for k, v in (stored or {}).items() if k in DEFAULT_CHANNEL_SETTINGS})
    return base


def coerce_settings(changes: dict) -> dict:
    """Valida e converte tipos de um dicionário parcial de configurações."""
    unknown = set(changes) - set(DEFAULT_CHANNEL_SETTINGS)
    if unknown:
        raise ValueError(f"Configurações desconhecidas: {', '.join(sorted(unknown))}")
    out = {}
    for k, v in changes.items():
        default = DEFAULT_CHANNEL_SETTINGS[k]
        if isinstance(default, bool):
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "sim", "yes", "on")
        elif isinstance(default, int):
            v = int(v)
        elif isinstance(default, float):
            v = float(v)
        elif isinstance(default, list):
            v = [s.strip() for s in v.split(",")] if isinstance(v, str) else list(v)
            v = [s for s in v if s]
        elif isinstance(default, str):
            v = str(v)
        out[k] = v
    if "timezone" in out:
        try:
            ZoneInfo(out["timezone"])
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Fuso horário inválido: {out['timezone']}") from e
    return out


# Linguagem do texto gerado pelo sistema (descrição, avisos), por idioma do canal.
LANG_TEXT = {
    "pt": {"fiction": "⚠️ História de ficção / lenda.", "sources": "Fontes", "wpm": 140, "name": "português do Brasil"},
    "en": {"fiction": "⚠️ Fictional story / legend.", "sources": "Sources", "wpm": 150, "name": "English (US)"},
    "es": {"fiction": "⚠️ Historia de ficción / leyenda.", "sources": "Fuentes", "wpm": 150, "name": "español latinoamericano"},
}


def lang_text(language: str) -> dict:
    code = language.split("-")[0].lower()
    return LANG_TEXT.get(code) or {**LANG_TEXT["en"], "name": language}


# Modelos de nicho: ponto de partida para criar canais rapidamente.
NICHE_PRESETS: dict[str, dict] = {
    "casos_reais_br": {
        "label": "Casos reais (BR)",
        "description": "Crimes e investigações reais, foco no mistério e na investigação, sem sensacionalismo.",
        "settings": {
            "niche": "casos criminais reais e investigações famosas e pouco conhecidas, do Brasil e do mundo, "
                     "contados com respeito às vítimas e foco no mistério e na investigação",
            "content_mix": ["caso_real", "investigacao", "caso_nao_resolvido"],
            "default_tags": ["casos reais", "true crime", "investigação", "mistério", "crimes reais"],
        },
    },
    "misterios_historia": {
        "label": "Mistérios da história",
        "description": "Desaparecimentos, civilizações perdidas, enigmas arqueológicos e eventos inexplicados.",
        "settings": {
            "niche": "mistérios históricos não resolvidos: desaparecimentos, enigmas arqueológicos, "
                     "civilizações perdidas, documentos e artefatos inexplicados",
            "content_mix": ["misterio", "historia_sombria", "desaparecimento", "arqueologia"],
            "default_tags": ["mistérios", "história", "enigmas", "desaparecimentos", "inexplicável"],
        },
    },
    "lendas_br": {
        "label": "Lendas e folclore sombrio (BR)",
        "description": "Lendas urbanas e folclore brasileiro, com origem e relatos (ficção marcada).",
        "settings": {
            "niche": "lendas urbanas e folclore sombrio brasileiro: origem histórica, relatos e variações regionais",
            "content_mix": ["lenda_urbana", "folclore", "relato"],
            "default_tags": ["lendas urbanas", "folclore", "terror brasileiro", "lendas", "assombração"],
            "research_with_web": False,
        },
    },
    "contos_terror": {
        "label": "Contos de terror (ficção)",
        "description": "Histórias originais de terror narradas, estilo creepypasta. 100% ficção.",
        "settings": {
            "niche": "contos de terror originais narrados (creepypasta), sempre apresentados como ficção",
            "content_mix": ["creepypasta", "conto_de_terror"],
            "default_tags": ["creepypasta", "terror", "contos de terror", "histórias de terror", "medo"],
            "research_with_web": False,
            "style": "narrador em primeira pessoa, tom íntimo e tenso, descrições sensoriais",
        },
    },
    "curiosidades_sombrias": {
        "label": "Curiosidades sombrias",
        "description": "Fatos perturbadores e pouco conhecidos sobre ciência, história e o corpo humano.",
        "settings": {
            "niche": "curiosidades sombrias e fatos perturbadores pouco conhecidos sobre história, ciência, "
                     "medicina antiga, natureza e comportamento humano",
            "content_mix": ["curiosidade_sombria", "historia_sombria", "ciencia_sombria"],
            "default_tags": ["curiosidades", "fatos sombrios", "você sabia", "história", "bizarro"],
            "shorts_per_day": 3,
            "long_per_day": 0,
        },
    },
    "paranormal": {
        "label": "Paranormal e inexplicável",
        "description": "Casos paranormais famosos, lugares assombrados e fenômenos sem explicação.",
        "settings": {
            "niche": "casos paranormais documentados, lugares assombrados e fenômenos inexplicáveis, "
                     "apresentando relatos e explicações céticas",
            "content_mix": ["paranormal", "lugar_assombrado", "fenomeno_inexplicavel"],
            "default_tags": ["paranormal", "assombrado", "sobrenatural", "inexplicável", "mistério"],
        },
    },
    "oceano_profundo": {
        "label": "Oceano profundo e espaço",
        "description": "O lado assustador do oceano profundo e do universo (fatos científicos).",
        "settings": {
            "niche": "o lado assustador e desconhecido do oceano profundo e do universo, com fatos científicos reais",
            "content_mix": ["oceano_profundo", "espaco", "criatura_real"],
            "default_tags": ["oceano", "espaço", "universo", "medo do desconhecido", "ciência"],
        },
    },
    "unsolved_en": {
        "label": "Unsolved Mysteries (EN-US)",
        "description": "Canal em inglês (RPM maior): mistérios e casos reais para público dos EUA.",
        "settings": {
            "language": "en-US",
            "timezone": "America/New_York",
            "voice": "en-US-ChristopherNeural",
            "voice_rate": "-4%",
            "voice_pitch": "-2Hz",
            "niche": "unsolved mysteries, strange disappearances and real unexplained cases from around the world",
            "style": "calm, deep documentary narrator, short sentences, rising suspense",
            "content_mix": ["unsolved_case", "disappearance", "mystery", "dark_history"],
            "default_tags": ["unsolved mysteries", "true crime", "mystery", "creepy", "dark history"],
        },
    },
    "misterios_es": {
        "label": "Misterios (ES-LatAm)",
        "description": "Canal em espanhol para América Latina: mistérios e casos reais.",
        "settings": {
            "language": "es-MX",
            "timezone": "America/Mexico_City",
            "voice": "es-MX-JorgeNeural",
            "voice_rate": "-5%",
            "voice_pitch": "-3Hz",
            "niche": "misterios sin resolver, casos reales y leyendas oscuras de América Latina y del mundo",
            "style": "narrador serio y atmosférico, frases cortas, suspenso creciente",
            "content_mix": ["caso_real", "misterio", "leyenda", "historia_oscura"],
            "default_tags": ["misterios", "casos reales", "leyendas", "terror", "historias oscuras"],
        },
    },
}
