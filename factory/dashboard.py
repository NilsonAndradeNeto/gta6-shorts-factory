"""Painel estático (HTML único) + resumo Markdown para o Job Summary do GitHub Actions."""
from __future__ import annotations

import html
import os
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


REPO = os.environ.get("GITHUB_REPOSITORY", "NilsonAndradeNeto/gta6-shorts-factory")


def _mode() -> dict:
    from factory.clips.engine import mode
    f = settings().path("data_dir") / "mode.json"
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    return {"mode": mode(settings().path("data_dir")), "changed_at": info.get("changed_at", ""), "by": info.get("by", "")}


def _insights(db: DB) -> list[dict]:
    try:
        row = db.one("SELECT ts, payload FROM strategy_memory ORDER BY ts DESC LIMIT 1")
    except Exception:
        return []
    if not row:
        return []
    out = []
    for f in json.loads(row["payload"]).get("findings", [])[:8]:
        out.append({"grupo": " / ".join(str(x) for x in f["cohort"][3:]), "amostra": f["sample"], "mediana": f["median_views"],
                    "melhor": f"{f['best']['views']} — {f['best']['title'][:60]}",
                    "pior": f"{f['weak']['views']} — {f['weak']['title'][:60]}"})
    return out


def _clip_sources(db: DB) -> list[dict]:
    try:
        return [dict(r) for r in db.q("SELECT channel, title, views, status, basis, url FROM clip_sources "
                                      "ORDER BY discovered_at DESC LIMIT 12")]
    except Exception:
        return []


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
        "mode": _mode(),
        "insights": _insights(db),
        "clip_sources": _clip_sources(db),
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


STATE_LABEL = {
    "PUBLISHED": ("No ar", "ok"), "SCHEDULED": ("Agendado", "info"), "UPLOADED": ("Enviado", "info"),
    "VALIDATED": ("Pronto", "info"), "RENDERED": ("Em revisão", "work"), "ASSETS_READY": ("Editando", "work"),
    "AUDIO_READY": ("Narrando", "work"), "SCRIPTED": ("Roteiro pronto", "work"), "VERIFIED": ("Na fila", "work"),
    "FAILED": ("Tentando de novo", "warn"), "BLOCKED": ("Barrado no controle", "bad"), "ABANDONED": ("Descartado", "bad"),
}
FORMAT_LABEL = {"short": "Short", "long": "Games Hoje · 10 min", "clip": "Clipe"}


def _yt_id(url: str | None) -> str:
    import re
    m = re.search(r"(?:shorts/|youtu\.be/|v=)([\w-]{11})", url or "")
    return m.group(1) if m else ""


def _views(db: DB) -> dict[str, dict]:
    """Últimas métricas públicas por vídeo do YouTube (coletadas pela inteligência)."""
    try:
        rows = db.q("SELECT o.video_id, o.views, o.likes, o.comments, o.title, o.published_at FROM observations o "
                    "WHERE o.platform='youtube' AND o.role='own' AND o.fetched_at=(SELECT MAX(p.fetched_at) FROM observations p "
                    "WHERE p.platform=o.platform AND p.video_id=o.video_id)")
        return {r["video_id"]: dict(r) for r in rows}
    except Exception:
        return {}


def _n(x) -> str:
    x = int(x or 0)
    return f"{x / 1_000_000:.1f} mi".replace(".", ",") if x >= 1_000_000 else \
        f"{x / 1000:.1f} mil".replace(".", ",") if x >= 1000 else str(x)


def write_html(db: DB, path: Path) -> Path:
    e = html.escape
    d = collect(db)
    views = _views(db)
    now = datetime.now(BRT)
    today = now.date().isoformat()
    vids = d["videos"]
    for v in vids:
        v["ytid"] = _yt_id(v.get("youtube_url"))
        v["views"] = (views.get(v["ytid"]) or {}).get("views")
        v["title_show"] = v.get("meta_title") or v.get("topic_title") or "Vídeo em produção"
    todays = sorted([v for v in vids if v["slot_date"] == today], key=lambda v: v["slot_time"])
    queue = [v for v in vids if v["slot_date"] > today or (v["slot_date"] == today and v["state"] not in
                                                           ("PUBLISHED", "SCHEDULED", "UPLOADED", "ABANDONED"))]
    week_ago = (now - timedelta(days=7)).date().isoformat()
    own = list(views.values())
    total_views = sum(int(o["views"] or 0) for o in own)
    week_views = sum(int(o["views"] or 0) for o in own if (o.get("published_at") or "")[:10] >= week_ago)
    top = sorted([o for o in own if o.get("views") is not None], key=lambda o: o["views"], reverse=True)[:6]
    top_max = max([o["views"] for o in top] or [1]) or 1
    errors_24h = [x for x in d["errors"] if x["ts"] >= (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
                  and x["stage"] not in ("telegram",)]   # Telegram foi desligado; erros antigos dele não contam
    mode = d["mode"]
    is_prod = mode["mode"] == "producao"

    def video_card(v: dict) -> str:
        label, cls = STATE_LABEL.get(v["state"], (v["state"], "info"))
        thumb = (f"<img loading='lazy' src='https://i.ytimg.com/vi/{e(v['ytid'])}/mqdefault.jpg' alt=''>" if v["ytid"]
                 else "<div class='ph'>🎬</div>")
        views_txt = f"<span class='vw'>👁 {_n(v['views'])}</span>" if v.get("views") is not None else ""
        link_open = f"<a class='vcard' href='{e(v['youtube_url'])}'>" if v.get("youtube_url") else "<div class='vcard'>"
        link_close = "</a>" if v.get("youtube_url") else "</div>"
        return (f"{link_open}<div class='th'>{thumb}</div><div class='vb'>"
                f"<div class='vt'>{e(v['title_show'][:90])}</div>"
                f"<div class='vm'><span class='chip {cls}'>{e(label)}</span>"
                f"<span class='muted'>{e(v['slot_time'])} · {e(FORMAT_LABEL.get(v.get('format') or 'short', 'Short'))}</span>{views_txt}</div>"
                f"</div>{link_close}")

    today_html = "".join(video_card(v) for v in todays) or "<p class='empty'>Nenhum vídeo para hoje ainda.</p>"
    queue_html = "".join(video_card(v) for v in queue[:8]) or "<p class='empty'>Fila vazia — tudo em dia ✅</p>"
    top_html = "".join(
        f"<a class='bar' href='https://youtu.be/{e(o['video_id'])}'><div class='bl'><span>{e((o['title'] or '')[:58])}</span>"
        f"<b>{_n(o['views'])}</b></div><div class='bt'><div style='width:{max(3, int(100 * o['views'] / top_max))}%'></div></div></a>"
        for o in top) or "<p class='empty'>As visualizações aparecem aqui depois das primeiras coletas.</p>"
    learn = []
    for f in d["insights"][:4]:
        age = f["grupo"].split(" / ")[-1]
        learn.append(f"<li><b>Vídeos com {e(age.replace('d', ' dias'))}:</b> mediana de {_n(f['mediana'])} views em {f['amostra']} vídeos. "
                     f"<br><span class='good'>▲ Melhor:</span> {e(f['melhor'])}"
                     f"<br><span class='badc'>▼ Pior:</span> {e(f['pior'])}</li>")
    learn_html = ("<ul class='learn'>" + "".join(learn) + "</ul><p class='hint'>São comparações, não certezas: a IA testa "
                  "essas pistas nos próximos roteiros.</p>") if learn else "<p class='empty'>Juntando dados — o aprendizado aparece após alguns dias de vídeos.</p>"

    def details(title: str, rows: list[dict], fmt) -> str:
        if not rows:
            return ""
        return f"<details><summary>{e(title)} <span class='cnt'>{len(rows)}</span></summary><div class='dl'>" + \
            "".join(fmt(r) for r in rows) + "</div></details>"

    news_d = details("Notícias encontradas", d["news"][:15], lambda r: f"<a class='li' href='{e(r['url'])}'><b>{e(r['source_name'] or '')}</b> "
                     f"{e((r['title'] or '')[:100])}</a>")
    topics_d = details("Próximas pautas", d["topics"][:10], lambda r: f"<div class='li'><b>{int(r['score'] or 0)}</b> {e(r['title'][:100])}</div>")
    clips_d = details("Fontes de clipes", d["clip_sources"], lambda r: f"<a class='li' href='{e(r['url'])}'><b>{e(r['channel'] or '')}</b> "
                      f"{e((r['title'] or '')[:80])} · {e(r['status'])}</a>")
    err_d = details("Erros recentes", d["errors"][:10], lambda r: f"<div class='li err'><b>{e(_fmt(r['ts']))}</b> {e(r['stage'])}: "
                    f"{e((r['error'] or '')[:160])}</div>")
    runs_html = "".join(f"<span class='run'>{e(r.replace(' BRT', ''))}</span>" for r in d["next_runs"])

    page = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0b10"><meta name="apple-mobile-web-app-capable" content="yes">
<title>Netão Verso — Painel</title>
<style>
:root{{--bg:#0b0b10;--card:#16161f;--card2:#1e1e2a;--fg:#f3f3f7;--muted:#9b9bb0;--line:#2a2a38;--acc:#ffd60a;
--ok:#2ecc71;--info:#4ea8ff;--work:#b58cff;--warn:#ffb020;--bad:#ff5c5c}}
@media (prefers-color-scheme: light){{:root{{--bg:#f4f4f8;--card:#ffffff;--card2:#f0f0f6;--fg:#15151d;--muted:#666677;--line:#e2e2ea}}}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
html,body{{overflow-x:hidden}} body{{margin:0;overflow-wrap:anywhere;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}}
.wrap{{max-width:560px;margin:0 auto;padding:16px 16px calc(28px + env(safe-area-inset-bottom))}}
header{{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 10px;margin:4px 0 14px}}
header h1{{font-size:21px;margin:0;letter-spacing:-.3px}} header .up{{font-size:12px;color:var(--muted)}}
a{{color:inherit;text-decoration:none}}
.mode{{background:linear-gradient(135deg,#2b2340,#16161f);border:1px solid var(--line);border-radius:18px;padding:16px;margin-bottom:14px}}
@media (prefers-color-scheme: light){{.mode{{background:linear-gradient(135deg,#efe9ff,#ffffff)}}}}
.mode .lbl{{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}}
.mode .val{{font-size:22px;font-weight:800;margin:4px 0 12px}}
.btn{{display:block;text-align:center;background:var(--acc);color:#111;font-weight:800;padding:14px;border-radius:14px;font-size:16px}}
.mode .sub{{font-size:12px;color:var(--muted);margin-top:8px;text-align:center}}
.kpis{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px;margin-bottom:18px}}
.kpi{{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}}
.kpi b{{display:block;font-size:26px;font-weight:800;letter-spacing:-.5px}} .kpi span{{font-size:12.5px;color:var(--muted)}}
.kpi.hl b{{color:var(--acc)}} .kpi.al b{{color:var(--bad)}}
h2{{font-size:17px;margin:22px 0 10px;display:flex;align-items:center;gap:8px}}
.vcard{{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:10px;margin-bottom:10px}}
.th{{flex:0 0 112px;height:63px;border-radius:10px;overflow:hidden;background:var(--card2)}}
.th img{{width:100%;height:100%;object-fit:cover;display:block}} .ph{{display:grid;place-items:center;height:100%;font-size:24px}}
.vb{{min-width:0;flex:1}} .vt{{font-weight:650;font-size:14.5px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.vm{{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;margin-top:6px;font-size:12.5px}}
.chip{{font-size:11.5px;font-weight:700;padding:3px 8px;border-radius:99px;border:1px solid currentColor}}
.chip.ok{{color:var(--ok)}} .chip.info{{color:var(--info)}} .chip.work{{color:var(--work)}} .chip.warn{{color:var(--warn)}} .chip.bad{{color:var(--bad)}}
.vw{{font-weight:700}} .muted{{color:var(--muted)}}
.bar{{display:block;min-width:0;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:11px 12px;margin-bottom:8px}}
.bl{{display:flex;justify-content:space-between;gap:10px;font-size:13.5px;margin-bottom:7px}} .bl span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bt{{height:8px;background:var(--card2);border-radius:99px;overflow:hidden}} .bt div{{height:100%;background:linear-gradient(90deg,var(--acc),#ff8a00);border-radius:99px}}
.learn{{list-style:none;padding:0;margin:0}} .learn li{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:8px;font-size:13.5px}}
.good{{color:var(--ok);font-weight:700}} .badc{{color:var(--bad);font-weight:700}}
.hint{{font-size:12px;color:var(--muted);margin:6px 2px}} .empty{{color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:14px;padding:14px;text-align:center;font-size:13.5px}}
.runs{{display:flex;flex-wrap:wrap;gap:8px}} .run{{background:var(--card);border:1px solid var(--line);border-radius:99px;padding:6px 12px;font-size:13px}}
details{{background:var(--card);border:1px solid var(--line);border-radius:14px;margin-bottom:10px;overflow:hidden}}
summary{{padding:14px;font-weight:700;cursor:pointer;list-style:none;display:flex;justify-content:space-between}} summary::-webkit-details-marker{{display:none}}
summary::after{{content:'▾';color:var(--muted)}} details[open] summary::after{{content:'▴'}}
.cnt{{margin-left:auto;margin-right:10px;color:var(--muted);font-weight:600}}
.dl{{border-top:1px solid var(--line)}} .li{{display:block;padding:10px 14px;border-bottom:1px solid var(--line);font-size:13px}} .li:last-child{{border-bottom:0}}
.li b{{color:var(--acc)}} .err b{{color:var(--bad)}}
footer{{text-align:center;color:var(--muted);font-size:12px;margin-top:22px}}
</style></head><body><div class="wrap">
<header><h1>📺 Netão Verso</h1><span class="up">atualizado {e(d['generated'].replace(' BRT', ''))}</span></header>

<section class="mode">
  <div class="lbl">Modo de produção</div>
  <div class="val">{'🎬 Produção própria' if is_prod else '✂️ Clipagem automática'}</div>
  <a class="btn" href="https://github.com/{REPO}/actions/workflows/modo.yml">Trocar modo</a>
  <div class="sub">Abre o GitHub → toque em “Run workflow” → escolha o modo.</div>
</section>

<section class="kpis">
  <div class="kpi hl"><b>{d['published_today']}</b><span>vídeos hoje no YouTube</span></div>
  <div class="kpi"><b>{_n(week_views)}</b><span>views (vídeos dos últimos 7 dias)</span></div>
  <div class="kpi"><b>{len(queue)}</b><span>na fila / em produção</span></div>
  <div class="kpi {'al' if errors_24h else ''}"><b>{len(errors_24h)}</b><span>{'problemas nas últimas 24h' if errors_24h else 'sem problemas em 24h ✅'}</span></div>
</section>

<h2>📅 Hoje</h2>{today_html}
<h2>🔥 Mais vistos</h2>{top_html}
<p class="hint">Total no canal (vídeos monitorados): {_n(total_views)} views.</p>
<h2>🧠 O que a IA aprendeu</h2>{learn_html}
<h2>⏳ Próximos na fila</h2>{queue_html}
<h2>🕒 Próximas execuções</h2><div class="runs">{runs_html or '<span class="muted">—</span>'}</div>
<p class="hint">TikTok: publicado automaticamente pelo Metricool (acompanhe no app do Metricool).</p>
<h2>📂 Detalhes</h2>{topics_d}{news_d}{clips_d}{err_d}
<footer>Fábrica automática · Netão Verso</footer>
</div></body></html>"""
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
