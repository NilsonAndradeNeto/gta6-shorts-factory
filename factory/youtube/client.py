"""Integração com a YouTube Data API v3 (oficial). Sem automação de navegador.

Credenciais: OAuth 2.0 (app "Desktop"); o refresh token fica SOMENTE em GitHub Secrets
(YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN). O access token é renovado em memória.
"""
from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from factory.config import settings
from factory.log import get_logger

log = get_logger("youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]
RETRIABLE_STATUS = {500, 502, 503, 504}


class YouTubeError(RuntimeError):
    pass


class QuotaExceeded(YouTubeError):
    pass


def configured() -> bool:
    return all(os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))


def _credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not configured():
        raise YouTubeError("Credenciais do YouTube ausentes (YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN)")
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"], client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"], token_uri="https://oauth2.googleapis.com/token",
                        scopes=SCOPES)
    try:
        creds.refresh(Request())
    except Exception as e:
        raise YouTubeError(f"Falha ao renovar token OAuth (refresh token revogado/expirado?): {e}") from e
    return creds


_services: dict[str, Any] = {}


def service(name: str = "youtube"):
    if name not in _services:
        from googleapiclient.discovery import build
        creds = _credentials()
        if name == "youtube":
            _services[name] = build("youtube", "v3", credentials=creds, cache_discovery=False)
        else:
            _services[name] = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    return _services[name]


def _is_quota(err: Exception) -> bool:
    return "quotaExceeded" in str(err) or "uploadLimitExceeded" in str(err) or "dailyLimitExceeded" in str(err)


def find_by_ref(ref: str, max_items: int = 50) -> str | None:
    """Procura nos uploads recentes do canal um vídeo com 'ref:<id>' na descrição (anti-duplicata)."""
    yt = service()
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items", [])
    if not items:
        return None
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    res = yt.playlistItems().list(part="snippet", playlistId=uploads, maxResults=min(50, max_items)).execute()
    for it in res.get("items", []):
        if f"ref:{ref}" in it["snippet"].get("description", ""):
            return it["snippet"]["resourceId"]["videoId"]
    return None


def upload(path: Path, meta: dict[str, Any], publish_at_utc: str | None) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    cfg = settings()
    status: dict[str, Any] = {"privacyStatus": cfg.get("channel.upload_privacy", "private"),
                              "selfDeclaredMadeForKids": bool(cfg.get("channel.made_for_kids", False)),
                              "containsSyntheticMedia": bool(cfg.get("channel.contains_synthetic_media", False))}
    if publish_at_utc:
        pa = datetime.fromisoformat(publish_at_utc)
        min_pa = datetime.now(timezone.utc) + timedelta(minutes=15)
        if pa < min_pa:
            pa = min_pa
        status["privacyStatus"] = "private"   # exigido pela API para agendar
        status["publishAt"] = pa.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {"snippet": {"title": meta["title"], "description": meta["description"], "tags": meta["tags"],
                        "categoryId": str(cfg.get("channel.category_id", "20")), "defaultLanguage": "pt-BR",
                        "defaultAudioLanguage": "pt-BR"},
            "status": status}
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = service().videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=True)
    response, retry = None, 0
    while response is None:
        try:
            st, response = req.next_chunk()
            if st:
                log.info("Upload %d%%", int(st.progress() * 100))
        except HttpError as e:
            if _is_quota(e):
                raise QuotaExceeded(str(e)) from e
            if e.resp.status not in RETRIABLE_STATUS:
                raise YouTubeError(f"Upload recusado: {e}") from e
            retry += 1
        except (ConnectionError, TimeoutError, OSError) as e:
            retry += 1
            log.warning("Erro de rede no upload: %s", e)
        else:
            continue
        if retry > 8:
            raise YouTubeError("Upload falhou após 8 retentativas")
        wait = min(120, (2 ** retry) + random.random())
        log.warning("Retentativa %d do upload em %.1fs", retry, wait)
        time.sleep(wait)
    if "id" not in response:
        raise YouTubeError(f"Resposta inesperada do upload: {response}")
    return response["id"]


def get_status(video_id: str) -> dict[str, Any] | None:
    res = service().videos().list(part="status,processingDetails,snippet", id=video_id).execute()
    items = res.get("items", [])
    if not items:
        return None
    it = items[0]
    return {"uploadStatus": it["status"].get("uploadStatus"), "privacyStatus": it["status"].get("privacyStatus"),
            "publishAt": it["status"].get("publishAt"), "rejectionReason": it["status"].get("rejectionReason"),
            "failureReason": it["status"].get("failureReason"),
            "processing": (it.get("processingDetails") or {}).get("processingStatus"),
            "title": it["snippet"].get("title")}


def statistics(video_ids: list[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for i in range(0, len(video_ids), 50):
        res = service().videos().list(part="statistics", id=",".join(video_ids[i:i + 50])).execute()
        for it in res.get("items", []):
            s = it.get("statistics", {})
            out[it["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                             "comments": int(s.get("commentCount", 0))}
    return out


def analytics(video_ids: list[str], start: str, end: str) -> dict[str, dict[str, float]]:
    """Duração média e % assistida via YouTube Analytics API (pode ter atraso de 24-72h)."""
    out: dict[str, dict[str, float]] = {}
    if not video_ids:
        return out
    for i in range(0, len(video_ids), 200):
        res = service("analytics").reports().query(
            ids="channel==MINE", startDate=start, endDate=end, dimensions="video",
            metrics="views,averageViewDuration,averageViewPercentage", filters="video==" + ",".join(video_ids[i:i + 200]),
            maxResults=200).execute()
        for row in res.get("rows", []) or []:
            out[row[0]] = {"views": row[1], "avg_view_duration": row[2], "avg_view_pct": row[3]}
    return out
