"""Descrição visual dos assets oficiais com modelo de visão gratuito (incremental).

Executa poucos assets por rodada para respeitar a cota diária gratuita. O resultado
(descrição, tags, ponto focal) é persistido em data/asset_tags.json no branch de estado.
As descrições servem só para ESCOLHER imagens; nunca são usadas como fonte de fatos.
"""
from __future__ import annotations

import io
import json
from typing import Any

from PIL import Image

from factory.assets.catalog import abs_path, build_catalog
from factory.config import settings
from factory.llm import LLM, LLMError
from factory.log import get_logger

log = get_logger("tagger")

SYSTEM = """Você descreve imagens oficiais de GTA 6 para um banco de imagens. Descreva SOMENTE o que é visível.
Não identifique personagens pelo nome. Responda JSON:
{"description_pt":"1 frase objetiva","tags":["5 a 10 palavras-chave em português: lugar, objetos, ação, clima"],
"shot":"close|medio|aberto|aereo","time":"dia|noite|por-do-sol|interior","people":0,
"focus":[x,y]}  // focus = centro do assunto principal, coordenadas 0..1 (x da esquerda, y do topo)"""


def tag_pending(llm: LLM, limit: int = 25) -> int:
    f = settings().path("data_dir") / "asset_tags.json"
    tags: dict[str, Any] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    if not llm.available:
        return 0
    done = 0
    for item in build_catalog():
        if item["kind"] == "video" or item["id"] in tags:
            continue
        if done >= limit:
            break
        with Image.open(abs_path(item)) as im:
            im = im.convert("RGB")
            im.thumbnail((640, 640))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
        try:
            out = llm.json(SYSTEM, f"Arquivo: {item['title']}", purpose="tag_asset", temperature=0.1,
                           max_tokens=400, images=[buf.getvalue()])
        except LLMError as e:
            log.warning("Tagger interrompido: %s", e)
            break
        if isinstance(out, dict):
            fx = out.get("focus") or [0.5, 0.45]
            try:
                fx = [min(1.0, max(0.0, float(fx[0]))), min(1.0, max(0.0, float(fx[1])))]
            except (TypeError, ValueError, IndexError):
                fx = [0.5, 0.45]
            tags[item["id"]] = {"description": str(out.get("description_pt", ""))[:300],
                                "tags": [str(t).lower() for t in (out.get("tags") or [])][:12],
                                "shot": out.get("shot"), "time": out.get("time"), "people": out.get("people"),
                                "focus": fx}
            done += 1
    f.write_text(json.dumps(tags, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Assets descritos nesta rodada: %d (total %d)", done, len(tags))
    return done
