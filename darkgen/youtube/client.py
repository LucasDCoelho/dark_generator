"""YouTube Data API v3: OAuth, upload com agendamento (publishAt) e estatísticas."""

from __future__ import annotations

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

_pending_flows: dict[str, Flow] = {}


class YouTubeNotConnected(RuntimeError):
    pass


# ---------------------------------------------------------------- OAuth

def has_client_secret() -> bool:
    return env().youtube_client_secret.exists()


def save_client_secret(content: bytes) -> None:
    data = json.loads(content)
    if not ("installed" in data or "web" in data):
        raise ValueError("JSON inválido: baixe o arquivo 'OAuth client ID' no Google Cloud Console.")
    env().youtube_client_secret.write_bytes(content)


def start_auth(redirect_uri: str) -> str:
    """Retorna a URL de consentimento do Google. O callback chama `finish_auth`."""
    if not has_client_secret():
        raise YouTubeNotConnected("Envie o client_secret.json primeiro.")
    flow = Flow.from_client_secrets_file(str(env().youtube_client_secret), scopes=SCOPES, redirect_uri=redirect_uri)
    state = secrets.token_urlsafe(16)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true", state=state)
    _pending_flows[state] = flow
    return url


def finish_auth(state: str, code: str) -> None:
    flow = _pending_flows.pop(state, None)
    if flow is None:
        raise ValueError("Sessão de login expirada; tente conectar de novo.")
    flow.fetch_token(code=code)
    env().youtube_token.write_text(flow.credentials.to_json())


def _credentials() -> Credentials:
    path: Path = env().youtube_token
    if not path.exists():
        raise YouTubeNotConnected("Canal do YouTube não conectado. Conecte pelo painel.")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json())
        else:
            raise YouTubeNotConnected("Token do YouTube inválido. Reconecte pelo painel.")
    return creds


def is_connected() -> bool:
    try:
        _credentials()
        return True
    except Exception:  # noqa: BLE001
        return False


def disconnect() -> None:
    env().youtube_token.unlink(missing_ok=True)


def _yt():
    return build("youtube", "v3", credentials=_credentials(), cache_discovery=False)


# ---------------------------------------------------------------- canal / estatísticas

def channel_info() -> dict:
    resp = _yt().channels().list(part="snippet,statistics", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise YouTubeNotConnected("Nenhum canal encontrado nesta conta Google.")
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


def video_stats(video_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    yt = _yt()
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
            }
    return out


# ---------------------------------------------------------------- upload

def upload(
    video_path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    publish_at: datetime | None,
    synthetic_media: bool = False,
    thumbnail: Path | None = None,
) -> str:
    """Envia o vídeo. Com `publish_at` no futuro ele fica privado e o YouTube publica sozinho no horário."""
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
            "defaultLanguage": "pt-BR",
            "defaultAudioLanguage": "pt-BR",
        },
        "status": status,
    }
    yt = _yt()
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=True)
    response = None
    retries = 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as e:
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
