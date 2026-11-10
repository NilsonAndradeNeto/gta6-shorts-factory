"""Agrupa notícias em "histórias" e define o status de confirmação de cada uma.

Regras de confirmação (determinísticas — o LLM só ajuda a agrupar/rotular):
  CONFIRMADO      : a história contém item de fonte oficial (Rockstar/Take-Two) com texto.
  REPORTADO       : ao menos 1 veículo jornalístico identificado (press) com texto, ou
                    2+ veículos distintos via agregador. Rumores/vazamentos nunca passam daqui.
  TEORIA          : só comunidade (Reddit etc.) ou natureza especulativa declarada.
  NAO_VERIFICADO  : evidência insuficiente (ex.: 1 manchete de agregador sem texto).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from rapidfuzz import fuzz

from factory.database.db import DB, now_iso
from factory.entities import find_entities
from factory.llm import LLM, LLMError
from factory.log import get_logger
from factory.textutil import norm

log = get_logger("verify")

CONF_ORDER = {"NAO_VERIFICADO": 0, "TEORIA": 1, "REPORTADO": 2, "CONFIRMADO": 3}
SPECULATIVE_KINDS = {"rumor", "leak", "theory", "speculation"}

CLUSTER_SYSTEM = """Você é editor-chefe de um canal sobre GTA 6 (Grand Theft Auto VI).
Agrupe as manchetes recebidas em HISTÓRIAS (o mesmo fato noticiado por veículos diferentes, em qualquer idioma, deve ficar junto).
Regras:
- Use somente as informações das manchetes/trechos. Não invente nada.
- Se a manchete combina com uma história existente (lista EXISTENTES), use a mesma "key".
- "key": slug curto em inglês, estável e descritivo (ex.: "gta6-preorder-record-psstore").
- "kind": news (fato), rumor (não confirmado/vazamento), theory (teoria de fã), opinion (opinião/coluna), guide (tutorial/como fazer), commerce (promoção/loja/brinde), offtopic (não é sobre GTA 6).
- "interest": 0-10, potencial de interesse para o público brasileiro de Shorts.
- "retention": 0-10, potencial de prender a atenção por 30-45 segundos (curiosidade, revelação, número marcante).
- "visual": 0-10, quanto o tema pode ser ilustrado com material oficial (personagens, locais, trailers, capa).
Responda JSON: {"stories":[{"key":"...","title_pt":"manchete curta em português","item_ids":[1,2],"kind":"news","interest":7,"retention":6,"visual":5}]}"""


def _fallback_clusters(items: list[dict[str, Any]], existing: dict[str, str]) -> list[dict[str, Any]]:
    """Agrupamento determinístico por similaridade de título (usado se não houver LLM)."""
    stories: list[dict[str, Any]] = []
    for it in items:
        best, best_s = None, 0
        for st in stories:
            s = fuzz.token_set_ratio(it["title_norm"], st["_norm"])
            if s > best_s:
                best, best_s = st, s
        if best and best_s >= 75:
            best["item_ids"].append(it["id"])
            continue
        key = None
        for k, t in existing.items():
            if fuzz.token_set_ratio(it["title_norm"], norm(t)) >= 80:
                key = k
                break
        speculative = bool(re.search(r"\b(rumor|rumour|leak|vazad|teoria|theory|reportedly|supostamente)", it["title_norm"]))
        stories.append({"key": key or "auto-" + "-".join(it["title_norm"].split()[:7]), "title_pt": it["title"],
                        "item_ids": [it["id"]], "kind": "rumor" if speculative else "news",
                        "interest": 5, "retention": 5, "visual": 5, "_norm": it["title_norm"]})
    return stories


def confirmation_for(members: list[dict[str, Any]], kind: str) -> str:
    official = [m for m in members if m["tier"] == "official" and len(m.get("body") or m.get("summary") or "") >= 60]
    press = [m for m in members if m["tier"] == "press" and len(m.get("body") or "") >= 400]
    press_any = {m["source_name"] for m in members if m["tier"] in ("press", "aggregator")}
    if kind == "theory" or all(m["tier"] == "community" for m in members):
        status = "TEORIA"
    elif official:
        status = "CONFIRMADO"
    elif press or len(press_any) >= 2:
        status = "REPORTADO"
    else:
        status = "NAO_VERIFICADO"
    # Rumores só viram CONFIRMADO quando há fonte oficial no grupo (regra acima); caso
    # contrário ficam no máximo REPORTADO e o roteiro é obrigado a tratá-los como rumor.
    return status


def build_evidence(members: list[dict[str, Any]], max_chars: int = 9000) -> list[dict[str, Any]]:
    """Evidências ordenadas por confiabilidade: oficial > imprensa > agregador > comunidade."""
    rank = {"official": 0, "press": 1, "aggregator": 2, "community": 3}
    ms = sorted(members, key=lambda m: (rank[m["tier"]], -len(m.get("body") or "")))
    ev, used = [], 0
    seen_sources: set[str] = set()
    for i, m in enumerate(ms):
        text = (m.get("body") or "").strip() or (m.get("summary") or "").strip()
        if not text:
            text = m["title"]
        if m["source_name"] in seen_sources and m["tier"] != "official":
            continue
        seen_sources.add(m["source_name"])
        budget = max(400, min(4000, max_chars - used))
        if used >= max_chars:
            break
        snippet = f"{m['title']}\n{text}"[:budget]
        used += len(snippet)
        ev.append({"id": f"E{len(ev) + 1}", "news_id": m["id"], "url": m["url"], "source": m["source_name"],
                   "tier": m["tier"], "published_at": m.get("published_at"), "image_url": m.get("image_url") or "",
                   "text": snippet})
    return ev


def cluster_and_classify(db: DB, llm: LLM, days: int = 10) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = [dict(r) for r in db.q(
        "SELECT * FROM news_items WHERE cluster_key IS NULL AND (published_at IS NULL OR published_at>=?) "
        "ORDER BY published_at DESC LIMIT 160", since)]
    if not rows:
        return 0
    existing = {r["cluster_key"]: r["title"] for r in db.q(
        "SELECT cluster_key, title FROM topics WHERE kind='news' AND created_at>=? ORDER BY created_at DESC LIMIT 60", since)}
    stories: list[dict[str, Any]] = []
    for start in range(0, len(rows), 45):
        batch = rows[start:start + 45]
        if llm.available:
            listing = "\n".join(f"[{r['id']}] ({r['tier']}, {r['source_name']}) {r['title']} — {(r['summary'] or '')[:160]}"
                                for r in batch)
            ex = "\n".join(f"- {k}: {t}" for k, t in list(existing.items())[:40]) or "(nenhuma)"
            try:
                out = llm.json(CLUSTER_SYSTEM, f"EXISTENTES:\n{ex}\n\nMANCHETES:\n{listing}", purpose="cluster",
                               temperature=0.1)
                got = out.get("stories", []) if isinstance(out, dict) else []
                valid_ids = {r["id"] for r in batch}
                for s in got:
                    s["item_ids"] = [int(i) for i in s.get("item_ids", []) if str(i).isdigit() and int(i) in valid_ids]
                assigned = {i for s in got for i in s["item_ids"]}
                missing = [r for r in batch if r["id"] not in assigned]
                stories.extend(s for s in got if s["item_ids"])
                stories.extend(_fallback_clusters(missing, existing))
                continue
            except LLMError as e:
                log.warning("Clusterização via LLM falhou (%s); usando fallback", e)
        stories.extend(_fallback_clusters(batch, existing))

    by_id = {r["id"]: r for r in rows}
    n_topics = 0
    for s in stories:
        key = re.sub(r"[^a-z0-9\-]+", "-", str(s.get("key", "")).lower()).strip("-")[:80] or "story"
        ids = s["item_ids"]
        for i in ids:
            db.x("UPDATE news_items SET cluster_key=? WHERE id=?", key, i)
        kind = s.get("kind", "news")
        members = [dict(r) for r in db.q("SELECT * FROM news_items WHERE cluster_key=?", key)] or [by_id[i] for i in ids]
        if kind == "offtopic":
            continue
        conf = confirmation_for(members, kind)
        for m in members:
            db.x("UPDATE news_items SET confirmation=? WHERE id=?", conf, m["id"])
        evidence = build_evidence(members)
        ents = sorted({e for m in members for e in find_entities(m["title"] + " " + (m.get("body") or "")[:1500])})
        detail = {"kind": kind, "interest": s.get("interest", 5), "retention": s.get("retention", 5),
                  "visual": s.get("visual", 5), "sources": len({m["source_name"] for m in members}),
                  "newest": max((m.get("published_at") or "") for m in members)}
        row = db.one("SELECT id, state FROM topics WHERE cluster_key=?", key)
        if row:
            db.x("UPDATE topics SET confirmation=?, evidence=?, entities=?, score_detail=?, title=? WHERE id=?",
                 conf, json.dumps(evidence, ensure_ascii=False), json.dumps(ents), json.dumps(detail),
                 s.get("title_pt") or members[0]["title"], row["id"])
        else:
            db.x("INSERT INTO topics(kind,cluster_key,title,confirmation,score,score_detail,evidence,entities,state,created_at)"
                 " VALUES ('news',?,?,?,0,?,?,?,'DISCOVERED',?)",
                 key, s.get("title_pt") or members[0]["title"], conf, json.dumps(detail),
                 json.dumps(evidence, ensure_ascii=False), json.dumps(ents), now_iso())
            n_topics += 1
    log.info("Histórias novas: %d (itens processados: %d)", n_topics, len(rows))
    return n_topics
