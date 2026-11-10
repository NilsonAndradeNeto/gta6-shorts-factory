"""Pontuação e seleção de pautas.

score (0-100) = novidade 25 + interesse 20 + confiabilidade 20 + retenção 10 + visual 10 + diferença 15
Pautas NAO_VERIFICADO nunca são escolhidas. TEORIA só se marcada explicitamente (limite 1/dia).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from rapidfuzz import fuzz

from factory.config import settings
from factory.database.db import DB
from factory.log import get_logger
from factory.textutil import norm

log = get_logger("editorial")

CONF_POINTS = {"CONFIRMADO": 20, "REPORTADO": 13, "TEORIA": 5, "NAO_VERIFICADO": 0}
KIND_PENALTY = {"opinion": -12, "guide": -6, "commerce": -15, "rumor": -4, "theory": -6, "news": 0, "evergreen": -8}


def _hours_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def used_titles(db: DB, days: int = 45) -> list[str]:
    rows = db.q("SELECT t.title, v.metadata FROM videos v JOIN topics t ON t.id=v.topic_id "
                "WHERE v.created_at >= datetime('now', ?) AND v.state NOT IN ('ABANDONED')", f"-{days} days")
    out = []
    for r in rows:
        out.append(r["title"])
        if r["metadata"]:
            try:
                out.append(json.loads(r["metadata"]).get("title", ""))
            except json.JSONDecodeError:
                pass
    return [t for t in out if t]


def score_topic(t: dict[str, Any], used: list[str]) -> tuple[float, dict[str, float]]:
    d = json.loads(t["score_detail"] or "{}")
    age = _hours_since(d.get("newest"))
    if t["kind"] == "evergreen":
        novelty = 8.0
    elif age is None:
        novelty = 10.0
    else:
        novelty = max(0.0, 25.0 * (1 - age / 96))   # cai a zero em 4 dias
    interest = 2.0 * float(d.get("interest", 5)) + min(4.0, float(d.get("sources", 1)) - 1)
    interest = min(20.0, interest)
    rel = CONF_POINTS.get(t["confirmation"], 0)
    retention = float(d.get("retention", 5))
    visual = float(d.get("visual", 5))
    ents = json.loads(t["entities"] or "[]")
    if ents:
        visual = min(10.0, visual + 2)
    sim = max((fuzz.token_set_ratio(norm(t["title"]), norm(u)) for u in used), default=0)
    diff = 15.0 * (1 - sim / 100)
    total = novelty + interest + rel + retention + visual + diff + KIND_PENALTY.get(d.get("kind", "news"), 0)
    parts = {"novidade": round(novelty, 1), "interesse": round(interest, 1), "confiabilidade": rel,
             "retencao": retention, "visual": visual, "diferenca": round(diff, 1), "similaridade_max": sim}
    return round(total, 1), parts


def rank_topics(db: DB) -> list[dict[str, Any]]:
    used = used_titles(db)
    block = float(settings().get("editorial.similarity_block", 82))
    out = []
    for r in db.q("SELECT * FROM topics WHERE state='DISCOVERED' AND confirmation != 'NAO_VERIFICADO'"):
        t = dict(r)
        d = json.loads(t["score_detail"] or "{}")
        if d.get("kind") == "offtopic":
            continue
        score, parts = score_topic(t, used)
        if parts["similaridade_max"] >= block:
            db.x("UPDATE topics SET state='DUPLICATE', note=? WHERE id=?", f"similar a pauta já usada ({parts['similaridade_max']})", t["id"])
            continue
        if t["kind"] == "news" and (_hours_since(d.get("newest")) or 0) > 24 * 7:
            db.x("UPDATE topics SET state='STALE' WHERE id=?", t["id"])
            continue
        db.x("UPDATE topics SET score=?, score_detail=? WHERE id=?", score, json.dumps({**d, "parts": parts}), t["id"])
        t["score"], t["parts"] = score, parts
        out.append(t)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def pick(db: DB, n: int, exclude_ids: set[int] | None = None) -> list[dict[str, Any]]:
    """Escolhe n pautas diversas. Evita duas pautas parecidas no mesmo lote e limita TEORIA a 1."""
    exclude_ids = exclude_ids or set()
    min_score = float(settings().get("editorial.min_topic_score", 45))
    ranked = [t for t in rank_topics(db) if t["id"] not in exclude_ids]
    chosen: list[dict[str, Any]] = []
    theories = 0
    for t in ranked:
        if len(chosen) >= n:
            break
        if t["kind"] == "news" and t["score"] < min_score:
            continue
        if t["confirmation"] == "TEORIA":
            if theories >= 1:
                continue
            theories += 1
        if any(fuzz.token_set_ratio(norm(t["title"]), norm(c["title"])) >= 70 for c in chosen):
            continue
        chosen.append(t)
    if len(chosen) < n:
        # reserva: atemporais oficiais (já estão no ranking com kind=evergreen); garante preenchimento
        for t in ranked:
            if len(chosen) >= n:
                break
            if t["kind"] == "evergreen" and t not in chosen:
                chosen.append(t)
    log.info("Pautas escolhidas: %s", [(c["id"], c["title"][:50], c["score"]) for c in chosen])
    return chosen
