"""Autorização única (executada pelo dono do canal no próprio computador).

Uso:  python -m factory.cli auth --client-secrets caminho/client_secret.json
Abre o navegador para consentimento e imprime o refresh token, que deve ser salvo
como GitHub Secret YT_REFRESH_TOKEN. O token NÃO é gravado em disco por este script.
"""
from __future__ import annotations

import json
from pathlib import Path

from factory.youtube.client import SCOPES


def run(client_secrets: Path) -> dict[str, str]:
    from google_auth_oauthlib.flow import InstalledAppFlow
    data = json.loads(Path(client_secrets).read_text(encoding="utf-8"))
    flow = InstalledAppFlow.from_client_config(data, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline", open_browser=True)
    if not creds.refresh_token:
        raise SystemExit("O Google não devolveu refresh token. Revogue o acesso em myaccount.google.com/permissions e tente de novo.")
    section = data.get("installed") or data.get("web") or {}
    return {"YT_CLIENT_ID": section.get("client_id", ""), "YT_CLIENT_SECRET": section.get("client_secret", ""),
            "YT_REFRESH_TOKEN": creds.refresh_token}
