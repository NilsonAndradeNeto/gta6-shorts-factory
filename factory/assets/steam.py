"""Imagens de divulgação de qualquer jogo via APIs públicas da loja Steam.

As capturas e artes da página da Steam são material de divulgação fornecido pelas
produtoras. Uso editorial com crédito (decisão do dono do canal; ver docs/LEGAL.md).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from factory.config import settings
from factory.log import get_logger
from factory.textutil import norm

log = get_logger("steam")
UA = {"User-Agent": "Mozilla/5.0 (compatible; games-shorts-factory/1.0)"}
LICENSE_PREFIX = "Divulgação oficial —"


def _dir() -> Path:
    d = settings().path("cache_dir") / "steam"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_json(url: str, params: dict[str, Any]) -> Any:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            log.info("Steam %s falhou (%s)", url, e)
            time.sleep(2)
    return None


def find_app(name: str) -> dict[str, Any] | None:
    """Encontra o app da Steam que melhor corresponde ao nome do jogo (cache em disco)."""
    cache = _dir() / "search.json"
    idx: dict[str, Any] = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    key = norm(name)
    if key in idx:
        return idx[key]
    data = _get_json("https://store.steampowered.com/api/storesearch/", {"term": name, "cc": "br", "l": "portuguese"})
    best = None
    if data and data.get("items") and not _is_gta6_term(key):
        from rapidfuzz import fuzz
        # comparação estrita do nome inteiro (sem sufixos de edição): "Vice City" NÃO pode virar
        # "Grand Theft Auto: Vice City – The Definitive Edition" — seria mostrar outro jogo.
        def clean(n: str) -> str:
            return re.sub(r"(definitive|deluxe|ultimate|complete|goty|game of the year|remastered|edition|enhanced)", "",
                          norm(n)).strip()
        scored = sorted(((fuzz.ratio(clean(key), clean(i["name"])), i) for i in data["items"] if i.get("type") == "app"),
                        key=lambda x: x[0], reverse=True)
        if scored and scored[0][0] >= 88:
            best = {"appid": scored[0][1]["id"], "name": scored[0][1]["name"]}
    idx[key] = best
    cache.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    return best


def assets_for(name: str, limit: int = 12) -> list[dict[str, Any]]:
    """Baixa (uma vez) capturas e arte de fundo do jogo e devolve no formato do catálogo."""
    app = find_app(name)
    if not app:
        return []
    appid = app["appid"]
    d = _dir() / str(appid)
    meta_f = d / "assets.json"
    if meta_f.exists():
        return json.loads(meta_f.read_text(encoding="utf-8"))
    details = _get_json("https://store.steampowered.com/api/appdetails", {"appids": appid, "cc": "br", "l": "portuguese"})
    info = (details or {}).get(str(appid), {}).get("data") or {}
    if not info:
        return []
    d.mkdir(parents=True, exist_ok=True)
    publisher = ", ".join(info.get("publishers") or info.get("developers") or ["produtora"])
    urls = [s["path_full"] for s in info.get("screenshots", [])][:limit]
    if info.get("background_raw"):
        urls.append(info["background_raw"])
    from factory.assets.catalog import detect_focus
    items = []
    for i, u in enumerate(urls):
        f = d / f"{i:02d}.jpg"
        if not f.exists():
            try:
                r = requests.get(u, headers=UA, timeout=60)
                r.raise_for_status()
                f.write_bytes(r.content)
            except requests.RequestException:
                continue
        try:
            with Image.open(f) as im:
                w, h = im.size
                thumb = im.convert("L").resize((96, 54))
        except Exception:
            continue
        if min(w, h) < 500:
            continue
        import numpy as np
        px = np.asarray(thumb, dtype=np.float32)
        if px.mean() < 38 or px.std() < 22:   # arte de fundo quase preta/chapada vira cena "vazia" no vídeo
            continue
        items.append({"id": f"steam/{appid}/{i:02d}", "path": str(f), "kind": "screenshot",
                      "entities": [f"game:{norm(name)}", f"game:{norm(info.get('name', name))}"],
                      "w": w, "h": h, "orientation": "portrait" if h > w else "landscape",
                      "source_url": f"https://store.steampowered.com/app/{appid}",
                      "license": f"{LICENSE_PREFIX} página do jogo na Steam ({publisher}). Uso editorial com crédito.",
                      "credit": f"Imagens: {publisher}", "title": f"{info.get('name', name)} {i}", "logo": False,
                      "group": "steam", **detect_focus(f)})
    meta_f.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Steam: %d imagens de %s (%s)", len(items), info.get("name"), publisher)
    return items


def _is_gta6_term(key: str) -> bool:
    """Nomes de GTA 6 (e seus lugares/personagens) usam SÓ o kit oficial da Rockstar, nunca a Steam."""
    from factory.entities import known_names
    return key in known_names() or key.startswith("gta") or key.startswith("grand theft auto")


def game_key(name: str) -> str:
    return "game:" + norm(re.sub(r"^game:", "", name))
