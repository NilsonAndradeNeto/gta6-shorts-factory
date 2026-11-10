"""Entrega dos vídeos para o TikTok via bot do Telegram (gratuito).

O TikTok proíbe apps que postam automaticamente na própria conta ("utility tool to help upload
contents to the account(s) you or your team manages") e, sem auditoria, a API só publica em
modo privado. Por isso o sistema entrega o vídeo pronto + legenda no seu Telegram perto do
horário do slot; no celular: compartilhar -> TikTok -> colar legenda -> publicar.

Configuração: crie um bot com @BotFather (TELEGRAM_BOT_TOKEN) e descubra seu chat id
(`python -m factory.cli telegram-setup`) -> TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from factory.config import settings
from factory.database.db import DB, now_iso
from factory.log import get_logger

log = get_logger("telegram")
BRT = ZoneInfo("America/Sao_Paulo")
API = "https://api.telegram.org/bot{token}/{method}"
MAX_BYTES = 49 * 1024 * 1024   # limite do Bot API para envio de arquivos


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _call(method: str, **kw: Any) -> dict[str, Any]:
    url = API.format(token=os.environ["TELEGRAM_BOT_TOKEN"], method=method)
    for attempt in range(4):
        try:
            r = requests.post(url, timeout=180, **kw)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            if r.status_code == 429:
                time.sleep(int(data.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        except requests.RequestException as e:
            log.warning("Telegram %s falhou (%s); tentativa %d", method, e, attempt + 1)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Telegram {method}: falhou após retentativas")


def tiktok_caption(meta: dict[str, Any]) -> str:
    tags = [h for h in meta.get("hashtags", [])][:5]
    extra = [t for t in settings().get("tiktok.extra_hashtags", []) if t not in tags]
    return f"{meta.get('title', '').strip()}\n\n{' '.join(tags + extra)}".strip()[:2000]


def send_video(path: Path, meta: dict[str, Any], slot_label: str, youtube_url: str | None) -> None:
    if path.stat().st_size > MAX_BYTES:
        raise RuntimeError(f"arquivo maior que o limite do Telegram ({path.stat().st_size / 1e6:.1f} MB)")
    head = f"🎬 TikTok — postar {slot_label}"
    if youtube_url:
        head += f"\nYouTube: {youtube_url}"
    with open(path, "rb") as fh:
        _call("sendVideo", data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "caption": head[:1000],
                                 "supports_streaming": "true", "width": 1080, "height": 1920},
              files={"video": (path.name, fh, "video/mp4")})
    # legenda em mensagem separada: fácil de copiar com um toque
    _call("sendMessage", data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": tiktok_caption(meta)})


def deliver_due(db: DB, lead_hours: float | None = None) -> int:
    """Envia vídeos aprovados cujo horário de postagem está próximo (ou já passou há pouco)."""
    if not configured():
        return 0
    lead = timedelta(hours=float(lead_hours or settings().get("tiktok.deliver_lead_hours", 4)))
    now = datetime.now(timezone.utc)
    sent = 0
    rows = db.q("SELECT id, publish_at_utc, video_path, metadata, youtube_url, slot_time FROM videos "
                "WHERE tiktok_sent_at IS NULL AND state IN ('VALIDATED','UPLOADED','SCHEDULED','PUBLISHED') "
                "AND slot_time NOT LIKE 'test-%' ORDER BY publish_at_utc")
    import json
    for r in rows:
        pa = datetime.fromisoformat(r["publish_at_utc"])
        if pa - now > lead or now - pa > timedelta(hours=6):
            continue
        p = Path(r["video_path"] or "")
        if not p.exists():
            continue   # arquivo só existe na execução que renderizou; a próxima re-renderização reenvia
        meta = json.loads(r["metadata"] or "{}")
        label = pa.astimezone(BRT).strftime("%d/%m às %H:%M")
        try:
            send_video(p, meta, label, r["youtube_url"])
            db.x("UPDATE videos SET tiktok_sent_at=? WHERE id=?", now_iso(), r["id"])
            db.event("vídeo entregue no Telegram para o TikTok", r["id"])
            sent += 1
        except Exception as e:
            db.record_error(r["id"], "telegram", e)
    return sent


def discover_chat_id() -> list[dict[str, Any]]:
    """Lista chats que mandaram mensagem ao bot (envie 'oi' ao bot antes)."""
    res = _call("getUpdates", data={"timeout": 0})
    out = []
    for u in res:
        msg = u.get("message") or u.get("channel_post") or {}
        ch = msg.get("chat") or {}
        if ch:
            out.append({"chat_id": ch.get("id"), "name": ch.get("first_name") or ch.get("title"), "type": ch.get("type")})
    return out
