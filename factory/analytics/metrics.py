"""Coleta de métricas reais (Data API + Analytics API) e relatórios comparativos.

Nenhuma métrica é estimada: se a API não devolve um valor, ele fica vazio no relatório.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from factory.database.db import DB, now_iso
from factory.log import get_logger
from factory.youtube import client as yt

log = get_logger("analytics")


def collect(db: DB) -> int:
    rows = db.q("SELECT id, youtube_id FROM videos WHERE state='PUBLISHED' AND youtube_id IS NOT NULL "
                "AND updated_at >= datetime('now','-30 days')")
    if not rows or not yt.configured():
        return 0
    ids = {r["youtube_id"]: r["id"] for r in rows}
    stats = yt.statistics(list(ids))
    try:
        ana = yt.analytics(list(ids), (date.today() - timedelta(days=30)).isoformat(), date.today().isoformat())
    except Exception as e:  # Analytics API pode não estar habilitada no projeto
        log.warning("Analytics API indisponível: %s", e)
        ana = {}
    ts = now_iso()
    for ytid, s in stats.items():
        a = ana.get(ytid, {})
        db.x("INSERT OR REPLACE INTO metrics(video_id,fetched_at,views,likes,comments,avg_view_duration,avg_view_pct,source)"
             " VALUES (?,?,?,?,?,?,?,?)", ids[ytid], ts, s["views"], s["likes"], s["comments"],
             a.get("avg_view_duration"), a.get("avg_view_pct"), "data+analytics" if a else "data")
    log.info("Métricas atualizadas para %d vídeos", len(stats))
    return len(stats)


def latest(db: DB) -> list[dict[str, Any]]:
    rows = db.q("""
        SELECT v.id, v.youtube_url, v.hook_style, v.duration, v.slot_time, v.metadata, t.kind, t.confirmation,
               m.views, m.likes, m.comments, m.avg_view_duration, m.avg_view_pct
        FROM videos v JOIN topics t ON t.id=v.topic_id
        JOIN metrics m ON m.video_id=v.id
        WHERE m.fetched_at = (SELECT MAX(fetched_at) FROM metrics WHERE video_id=v.id)""")
    return [dict(r) for r in rows]


def comparisons(db: DB) -> dict[str, list[dict[str, Any]]]:
    data = latest(db)

    def group(key) -> list[dict[str, Any]]:
        g: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for d in data:
            g[str(key(d))].append(d)
        out = []
        for k, items in g.items():
            views = [i["views"] or 0 for i in items]
            pct = [i["avg_view_pct"] for i in items if i["avg_view_pct"] is not None]
            eng = [((i["likes"] or 0) + (i["comments"] or 0)) / max(1, i["views"] or 0) for i in items]
            out.append({"grupo": k, "videos": len(items), "views_media": round(sum(views) / len(views), 1),
                        "retencao_media_pct": round(sum(pct) / len(pct), 1) if pct else None,
                        "engajamento_pct": round(100 * sum(eng) / len(eng), 2)})
        return sorted(out, key=lambda x: x["views_media"], reverse=True)

    def dur_bucket(d: dict[str, Any]) -> str:
        x = d["duration"] or 0
        return "<25s" if x < 25 else "25-35s" if x < 35 else "35-45s" if x < 45 else "45s+"

    return {"gancho": group(lambda d: d["hook_style"]), "tipo_pauta": group(lambda d: d["kind"]),
            "confirmacao": group(lambda d: d["confirmation"]), "duracao": group(dur_bucket),
            "horario": group(lambda d: d["slot_time"])}


def hook_weights(db: DB) -> dict[str, float]:
    """Pesos por estilo de gancho a partir de dados reais (usado só com >= 3 vídeos por estilo)."""
    w = {}
    for row in comparisons(db).get("gancho", []):
        if row["videos"] >= 3:
            w[row["grupo"]] = row["views_media"] * (1 + (row["retencao_media_pct"] or 0) / 100)
    return w


def report_markdown(db: DB) -> str:
    comps = comparisons(db)
    lines = ["# Relatório de desempenho", ""]
    if not any(comps.values()):
        return "\n".join(lines + ["Ainda não há métricas publicadas pela API."])
    for name, rows in comps.items():
        lines += [f"## Por {name}", "", "| grupo | vídeos | views médias | retenção média % | engajamento % |",
                  "|---|---|---|---|---|"]
        lines += [f"| {r['grupo']} | {r['videos']} | {r['views_media']} | {r['retencao_media_pct'] if r['retencao_media_pct'] is not None else 'n/d'} | {r['engajamento_pct']} |" for r in rows]
        lines.append("")
    return "\n".join(lines)


def dump_json(db: DB) -> str:
    return json.dumps(comparisons(db), ensure_ascii=False, indent=1)
