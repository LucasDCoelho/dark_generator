"""Configuração estática (variáveis de ambiente) e configurações editáveis pelo painel."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    claude_model: str = "claude-opus-5-5"
    pexels_api_key: str = ""
    channel_timezone: str = "America/Sao_Paulo"
    data_dir: Path = Path("./data")
    host: str = "127.0.0.1"
    port: int = 8000
    dashboard_token: str = ""
    tts_provider: str = "edge"  # edge | silent
    public_base_url: str = ""  # ex.: https://meu-servidor.com (para o callback OAuth)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.channel_timezone)

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
    def youtube_client_secret(self) -> Path:
        return self.data_dir / "client_secret.json"

    @property
    def youtube_token(self) -> Path:
        return self.data_dir / "youtube_token.json"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.videos_dir, self.music_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def env() -> EnvSettings:
    s = EnvSettings()
    s.ensure_dirs()
    return s


# Valores padrão das configurações editáveis no painel / via MCP.
DEFAULT_SETTINGS: dict = {
    "autopilot": True,  # gera e agenda sozinho todos os dias
    "approval_required": False,  # se True, vídeos esperam aprovação antes do upload
    "shorts_per_day": 2,
    "long_per_day": 1,
    "plan_days_ahead": 1,  # quantos dias à frente manter preenchidos
    "min_gap_hours": 3,  # intervalo mínimo entre publicações
    "niche": (
        "canal dark brasileiro: casos reais perturbadores, mistérios não resolvidos, "
        "lendas urbanas, fenômenos inexplicáveis e curiosidades sombrias da história"
    ),
    "content_mix": ["caso_real", "misterio", "lenda_urbana", "curiosidade_sombria", "historia_sombria"],
    "language": "pt-BR",
    "voice": "pt-BR-AntonioNeural",
    "voice_rate": "-6%",
    "voice_pitch": "-4Hz",
    "category_id": "24",  # Entretenimento
    "default_tags": ["dark", "mistério", "terror", "casos reais", "curiosidades"],
    "synthetic_media_disclosure": False,
    "research_with_web": True,  # pesquisa na web antes de escrever casos reais
    "music_volume": 0.14,
    "long_target_minutes": 8,
}
