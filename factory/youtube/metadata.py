"""Título, descrição, hashtags e tags a partir do roteiro aprovado (fiéis ao conteúdo)."""
from __future__ import annotations

import re
from typing import Any

from factory.config import settings
from factory.entities import BY_ID, find_entities

STATUS_LINE = {
    "CONFIRMADO": "Status: informação confirmada por fonte oficial da Rockstar Games.",
    "REPORTADO": "Status: informação reportada pela imprensa especializada; ainda sem confirmação oficial da Rockstar.",
    "TEORIA": "Status: teoria/discussão da comunidade — não é informação oficial.",
}


def _clean_title(t: str) -> str:
    t = re.sub(r"#\w+", "", t or "").strip()
    t = re.sub(r"\s+", " ", t).strip(" -|")
    return t[:95]


def build(script: dict[str, Any], topic_evidence: list[dict[str, Any]], confirmation: str, video_id: str,
          credits: list[str] | None = None) -> dict[str, Any]:
    cfg = settings()
    used_ids = {c.get("evidence") for s in script["segments"] for c in (s.get("claims") or [])}
    sources, seen = [], set()
    for e in topic_evidence:
        if e["id"] in used_ids and e["url"] not in seen:
            seen.add(e["url"])
            sources.append(f"- {e['source']}: {e['url']}")
    tags_h = []
    for h in (script.get("hashtags") or []) + list(cfg.get("channel.default_hashtags", [])):
        h = "#" + re.sub(r"[^\wÀ-ú]", "", str(h).lstrip("#"))
        if len(h) > 2 and h.lower() not in {x.lower() for x in tags_h}:
            tags_h.append(h)
    tags_h = tags_h[:5]
    desc_parts = [script.get("description", "").strip()]
    if confirmation in STATUS_LINE:
        desc_parts.append(STATUS_LINE[confirmation])
    if any(seg.get("tipo") == "humor" for seg in script["segments"]):
        desc_parts.append("Contém trechos de humor e ficção: as cenas imaginárias são piada, não informação oficial.")
    if sources:
        desc_parts.append("Fontes:\n" + "\n".join(sources[:6]))
    owners = sorted({c.replace("Imagens: ", "") for c in (credits or []) if c})
    desc_parts.append((f"Imagens oficiais de divulgação: {', '.join(owners)}. " if owners else "")
                      + "Canal de fãs, sem vínculo oficial com as produtoras citadas. "
                        "Narração com voz sintética; roteiro e edição originais.")
    desc_parts.append(" ".join(tags_h))
    desc_parts.append(f"ref:{video_id}")
    description = "\n\n".join(p for p in desc_parts if p)
    narration = " ".join(s["text"] for s in script["segments"])
    ents = find_entities(narration + " " + script.get("title", ""))
    gta = bool(ents) or "gta" in narration.lower()
    tags = (["GTA 6", "GTA VI", "Grand Theft Auto VI", "Rockstar Games"] if gta else [])         + ["games", "jogos", "notícias de games"] + [h.lstrip("#") for h in tags_h] + [BY_ID[e].name for e in ents if e in BY_ID]
    tags = list(dict.fromkeys(tags))
    while sum(len(t) + 2 for t in tags) > 450:
        tags.pop()
    return {"title": _clean_title(script.get("title", "")), "description": description[:4900], "hashtags": tags_h,
            "tags": tags, "ref": video_id}
