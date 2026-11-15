"""YouTube Data API v3 por canal: OAuth, upload com agendamento (publishAt) e estatísticas.

Cada canal do sistema guarda seu próprio token em data/channels/<id>/youtube_token.json.
A credencial OAuth (client_secret.json) pode ser global ou própria do canal — usar projetos
diferentes no Google Cloud divide a cota diária de uploads entre eles.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from ..config import env

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

_pending_flows: dict[str, tuple[Flow, int]] = {}


class YouTubeNotConnected(RuntimeError):
    pass


class QuotaExceeded(RuntimeError):
    pass


# ---------------------------------------------------------------- credenciais

def _token_path(channel_id: int) -> Path:
    return env().channel_dir(channel_id) / "youtube_token.json"


def _channel_secret_path(channel_id: int) -> Path:
    return env().channel_dir(channel_id) / "client_secret.json"


def client_secret_for(channel_id: int) -> Path | None:
    own = _channel_secret_path(channel_id)
    if own.exists():
        return own
    return env().youtube_client_secret if env().youtube_client_secret.exists() else None


def quota_key(channel_id: int) -> str:
    """Identifica o projeto do Google Cloud usado pelo canal (a cota é por projeto)."""
    path = client_secret_for(channel_id)
    if not path:
        return "none"
    data = json.loads(path.read_text())
    client_id = (data.get("installed") or data.get("web") or {}).get("client_id", "")
    return hashlib.sha1(client_id.encode()).hexdigest()[:12]


def has_own_secret(channel_id: int) -> bool:
    return _channel_secret_path(channel_id).exists()


def save_client_secret(content: bytes, channel_id: int | None = None) -> None:
    data = json.loads(content)
    if not ("installed" in data or "web" in data):
        raise ValueError("JSON inválido: baixe o arquivo 'OAuth client ID' no Google Cloud Console.")
    path = _channel_secret_path(channel_id) if channel_id else env().youtube_client_secret
    path.write_bytes(content)


def remove_channel_secret(channel_id: int) -> None:
    _channel_secret_path(channel_id).unlink(missing_ok=True)


def start_auth(channel_id: int, redirect_uri: str) -> str:
    """URL de consentimento do Google para conectar um canal. O callback chama `finish_auth`.

    Na tela do Google, escolha a conta/canal (Conta de Marca) correto para este canal.
    """
    secret = client_secret_for(channel_id)
    if not secret:
        raise YouTubeNotConnected("Envie um client_secret.json (global ou do canal) primeiro.")
    flow = Flow.from_client_secrets_file(str(secret), scopes=SCOPES, redirect_uri=redirect_uri)
    state = secrets.token_urlsafe(16)
    url, _ = flow.authorization_url(
        access_type="offline", prompt="consent select_account", include_granted_scopes="true", state=state
    )
    _pending_flows[state] = (flow, channel_id)
    return url


def finish_auth(state: str, code: str) -> int:
    pending = _pending_flows.pop(state, None)
    if pending is None:
        raise ValueError("Sessão de login expirada; tente conectar de novo.")
    flow, channel_id = pending
    flow.fetch_token(code=code)
    _token_path(channel_id).write_text(flow.credentials.to_json())
    return channel_id


def _credentials(channel_id: int) -> Credentials:
    path = _token_path(channel_id)
    if not path.exists():
        raise YouTubeNotConnected("Canal não conectado ao YouTube.")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json())
        else:
            raise YouTubeNotConnected("Token do YouTube inválido; reconecte o canal.")
    return creds


def has_token(channel_id: int) -> bool:
    return _token_path(channel_id).exists()


def is_connected(channel_id: int) -> bool:
    try:
        _credentials(channel_id)
        return True
    except Exception:  # noqa: BLE001
        return False


def disconnect(channel_id: int) -> None:
    _token_path(channel_id).unlink(missing_ok=True)


def _yt(channel_id: int):
    return build("youtube", "v3", credentials=_credentials(channel_id), cache_discovery=False)


def _is_quota_error(e: HttpError) -> bool:
    return e.resp.status == 403 and any(
        r in str(e) for r in ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded")
    )


# ---------------------------------------------------------------- canal / estatísticas

def channel_info(channel_id: int) -> dict:
    resp = _yt(channel_id).channels().list(part="snippet,statistics", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise YouTubeNotConnected("Esta conta Google não tem canal no YouTube.")
    ch = items[0]
    st = ch.get("statistics", {})
    return {
        "id": ch["id"],
        "title": ch["snippet"]["title"],
        "thumbnail": ch["snippet"].get("thumbnails", {}).get("default", {}).get("url"),
        "subscribers": int(st.get("subscriberCount", 0)),
        "views": int(st.get("viewCount", 0)),
        "videos": int(st.get("videoCount", 0)),
    }


def video_stats(channel_id: int, video_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not video_ids:
        return out
    yt = _yt(channel_id)
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = yt.videos().list(part="statistics,status", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            st = item.get("statistics", {})
            out[item["id"]] = {
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
                "privacy": item.get("status", {}).get("privacyStatus"),
                "upload_status": item.get("status", {}).get("uploadStatus"),
                "rejection": item.get("status", {}).get("rejectionReason"),
            }
    return out


# ---------------------------------------------------------------- upload

def upload(
    channel_id: int,
    video_path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    language: str,
    publish_at: datetime | None,
    synthetic_media: bool = False,
    thumbnail: Path | None = None,
) -> str:
    """Envia o vídeo. Com `publish_at` no futuro fica privado e o YouTube publica sozinho no horário."""
    now = datetime.now(timezone.utc)
    status: dict = {
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": bool(synthetic_media),
        "embeddable": True,
    }
    if publish_at and publish_at > now:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        status["privacyStatus"] = "public"

    # limites do YouTube: título 100 chars, descrição 5000 bytes, tags 500 chars no total
    tags_clean, total = [], 0
    for t in tags:
        t = t.replace("<", "").replace(">", "").strip()
        if t and total + len(t) + 2 <= 480:
            tags_clean.append(t)
            total += len(t) + 2
    body = {
        "snippet": {
            "title": title[:100],
            "description": description.replace("<", "").replace(">", "")[:4900],
            "tags": tags_clean,
            "categoryId": category_id,
            "defaultLanguage": language,
            "defaultAudioLanguage": language,
        },
        "status": status,
    }
    yt = _yt(channel_id)
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=True)
    response = None
    retries = 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as e:
            if _is_quota_error(e):
                raise QuotaExceeded(str(e)) from e
            if e.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                continue
            raise
    video_id = response["id"]

    if thumbnail and thumbnail.exists():
        try:
            yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail))).execute()
        except HttpError as e:  # thumbnails personalizadas exigem canal verificado
            log.warning("Não foi possível definir a thumbnail: %s", e)
    return video_id
