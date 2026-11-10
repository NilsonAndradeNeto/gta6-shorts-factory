"""Painel estático (HTML único) + resumo Markdown para o Job Summary do GitHub Actions."""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from factory.analytics.metrics import comparisons
from factory.config import settings
from factory.database.db import DB

BRT = ZoneInfo("America/Sao_Paulo")


def next_runs(n: int = 3) -> list[str]:
    """Próximas execuções previstas a partir do cron do workflow (UTC)."""
    hours = [int(h) for h in settings().get("automation.cron_hours_utc", [])]
    minute = int(settings().get("automation.cron_minute", 17))
    out, now = [], datetime.now(timezone.utc)
    for d in range(0, 2):
        for h in sorted(hours):
            t = (now + timedelta(days=d)).replace(hour=h, minute=minute, second=0, microsecond=0)
            if t > now:
                out.append(t.astimezone(BRT).strftime("%d/%m %H:%M BRT"))
    return out[:n]


def _fmt(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(BRT).strftime("%d/%m %H:%M")
    except ValueError:
        return iso


def collect(db: DB) -> dict:
    today = datetime.now(BRT).date().isoformat()
    vids = [dict(r) for r in db.q(
        "SELECT v.*, t.title topic_title, t.confirmation FROM videos v LEFT JOIN topics t ON t.id=v.topic_id "
        "ORDER BY v.slot_date DESC, v.slot_time DESC LIMIT 60")]
    for v in vids:
        try:
            v["meta_title"] = json.loads(v["metadata"] or "{}").get("title", "")
        except json.JSONDecodeError:
            v["meta_title"] = ""
    return {
        "generated": datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT"),
        "published_today": sum(1 for v in vids if v["slot_date"] == today and v["state"] in ("SCHEDULED", "PUBLISHED")),
        "videos": vids,
        "news": [dict(r) for r in db.q("SELECT title, source_name, tier, confirmation, published_at, url FROM news_items "
                                       "ORDER BY collected_at DESC LIMIT 25")],
        "topics": [dict(r) for r in db.q("SELECT title, kind, confirmation, score, state FROM topics "
                                         "WHERE state='DISCOVERED' ORDER BY score DESC LIMIT 15")],
        "errors": [dict(r) for r in db.q("SELECT ts, video_id, stage, error FROM errors ORDER BY id DESC LIMIT 15")],
        "metrics": comparisons(db),
        "next_runs": next_runs(),
    }


def _table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "<p class='muted'>Nada ainda.</p>"
    head = "".join(f"<th>{html.escape(c[1])}</th>" for c in cols)
    body = ""
    for r in rows:
        cells = ""
        for k, _ in cols:
            v = r.get(k, "")
            v = "" if v is None else str(v)
            if k == "youtube_url" and v:
                cells += f"<td><a href='{html.escape(v)}'>abrir</a></td>"
            elif k == "url" and v:
                cells += f"<td><a href='{html.escape(v)}'>link</a></td>"
            else:
                cells += f"<td>{html.escape(v)}</td>"
        body += f"<tr>{cells}</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def write_html(db: DB, path: Path) -> Path:
    d = collect(db)
    queue = [v for v in d["videos"] if v["state"] not in ("PUBLISHED",)]
    published = [v for v in d["videos"] if v["state"] == "PUBLISHED"]
    for v in d["videos"]:
        v["publish_brt"] = _fmt(v.get("publish_at_utc"))
    metric_html = ""
    for name, rows in d["metrics"].items():
        metric_html += f"<h3>Por {html.escape(name)}</h3>" + _table(rows, [("grupo", "grupo"), ("videos", "vídeos"),
                                                                           ("views_media", "views médias"),
                                                                           ("retencao_media_pct", "retenção %"),
                                                                           ("engajamento_pct", "engajamento %")])
    page = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Fábrica GTA 6 — Painel</title>
<style>
:root{{--bg:#0e0f13;--fg:#e9e9ee;--muted:#9a9aa8;--card:#171922;--line:#2a2d3a;--acc:#ffd60a}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,Segoe UI,sans-serif;margin:0;padding:16px;max-width:1200px;margin:auto}}
h1{{font-size:22px}} h2{{font-size:18px;margin-top:28px;border-bottom:1px solid var(--line);padding-bottom:6px}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap}} .kpi{{background:var(--card);padding:12px 16px;border-radius:10px}}
.kpi b{{font-size:24px;color:var(--acc);display:block}} .muted{{color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-size:13px;display:block;overflow-x:auto}} th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
a{{color:var(--acc)}}
</style></head><body>
<h1>Fábrica de Shorts GTA 6</h1><p class="muted">Atualizado em {d['generated']}</p>
<div class="kpis"><div class="kpi"><b>{d['published_today']}</b>agendados/publicados hoje</div>
<div class="kpi"><b>{len(queue)}</b>na fila / em produção</div>
<div class="kpi"><b>{len(published)}</b>publicados (últimos 60)</div>
<div class="kpi"><b>{html.escape(', '.join(d['next_runs']) or 'n/d')}</b>próximas execuções</div></div>
<h2>Fila e produção</h2>{_table(queue, [('slot_date', 'data'), ('slot_time', 'horário'), ('state', 'status'), ('topic_title', 'pauta'), ('meta_title', 'título'), ('confirmation', 'confirmação'), ('attempts', 'tentativas'), ('last_error', 'último erro'), ('youtube_url', 'YouTube')])}
<h2>Publicados</h2>{_table(published, [('slot_date', 'data'), ('slot_time', 'horário'), ('meta_title', 'título'), ('hook_style', 'gancho'), ('duration', 'duração'), ('youtube_url', 'YouTube')])}
<h2>Pautas candidatas</h2>{_table(d['topics'], [('title', 'pauta'), ('kind', 'tipo'), ('confirmation', 'confirmação'), ('score', 'score')])}
<h2>Notícias coletadas</h2>{_table(d['news'], [('published_at', 'data'), ('source_name', 'fonte'), ('tier', 'nível'), ('confirmation', 'status'), ('title', 'título'), ('url', 'link')])}
<h2>Métricas</h2>{metric_html}
<h2>Erros recentes</h2>{_table(d['errors'], [('ts', 'quando'), ('video_id', 'vídeo'), ('stage', 'etapa'), ('error', 'erro')])}
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path


def summary_markdown(db: DB) -> str:
    d = collect(db)
    lines = [f"## Fábrica GTA 6 — {d['generated']}", "",
             f"- Agendados/publicados hoje: **{d['published_today']}**",
             f"- Próximas execuções: {', '.join(d['next_runs']) or 'n/d'}", "",
             "| data | horário | status | título | YouTube |", "|---|---|---|---|---|"]
    for v in d["videos"][:12]:
        lines.append(f"| {v['slot_date']} | {v['slot_time']} | {v['state']} | {(v['meta_title'] or v['topic_title'] or '')[:60]} | {v['youtube_url'] or ''} |")
    if d["errors"]:
        lines += ["", "### Erros recentes"] + [f"- `{e['stage']}` {e['video_id'] or ''}: {e['error'][:160]}" for e in d["errors"][:5]]
    return "\n".join(lines)
