"""Painel local de revisão: assista aos vídeos produzidos com roteiro, checagem, QA e storyboard.

Uso: python -m factory.preview  -> gera output/painel.html e abre no navegador.
"""
from __future__ import annotations

import html
import json
import os
import sys
import webbrowser
from pathlib import Path

from factory.config import settings
from factory.database import connect

STATE_COLORS = {"VALIDATED": "#2ecc71", "SCHEDULED": "#2ecc71", "PUBLISHED": "#2ecc71", "UPLOADED": "#3498db",
                "BLOCKED": "#e74c3c", "FAILED": "#e67e22"}
CONF_COLORS = {"CONFIRMADO": "#2ecc71", "REPORTADO": "#ffb020", "TEORIA": "#ba68ff"}


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _list(items: list[str], empty: str = "nenhum") -> str:
    if not items:
        return f"<span class='ok'>{_e(empty)}</span>"
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


def build(out: Path, video_ids: list[str] | None = None) -> Path:
    db = connect()
    q = "SELECT v.*, t.title topic_title, t.confirmation, t.evidence FROM videos v JOIN topics t ON t.id=v.topic_id"
    rows = [dict(r) for r in db.q(q + " ORDER BY v.created_at DESC")]
    if video_ids:
        rows = [r for r in rows if r["id"] in video_ids]
    rows = [r for r in rows if r.get("video_path") and Path(r["video_path"]).exists()]
    cards = []
    for r in rows:
        v = db.video(r["id"])
        script, meta, qa = v.get("script") or {}, v.get("metadata") or {}, v.get("qa") or {}
        plan = (v.get("storyboard") or {}).get("plan") or {}
        fact = script.get("factcheck", {})
        rel = os.path.relpath(r["video_path"], out.parent).replace("\\", "/")
        segs = "".join(
            f"<p><b class='role'>{_e(s.get('role'))}</b> {_e(s.get('text'))}"
            + "".join(f"<br><small class='claim'>✔ [{_e(c.get('evidence'))} · {_e(c.get('status'))}] “{_e(c.get('quote'))}”</small>"
                      for c in (s.get('claims') or []))
            + "</p>" for s in script.get("segments", []))
        scenes = "".join(
            f"<tr><td>{s['start']:.1f}–{s['end']:.1f}s</td><td>{_e(s['narration'])}</td><td>{_e(s['type'])}/{_e(s.get('layout'))}</td>"
            f"<td>{_e((s.get('asset_id') or '').split('/')[-1])}</td><td>{_e(s.get('effect'))} · {_e(s.get('transition_in'))}</td>"
            f"<td>{s['match_confidence']:.2f}</td><td><small>{_e(s.get('match_reason'))}</small></td></tr>"
            for s in plan.get("scenes", []))
        tech, vis, edi = qa.get("technical", {}), qa.get("visual", {}), qa.get("editorial", {})
        info = tech.get("info", {})
        conf = r["confirmation"]
        sources = sorted({(e["source"], e["url"]) for e in json.loads(r["evidence"] or "[]")
                          if e["id"] in {c.get("evidence") for s in script.get("segments", []) for c in (s.get("claims") or [])}})
        cards.append(f"""
<section class="card">
  <div class="vid"><video controls preload="metadata" src="{_e(rel)}"></video></div>
  <div class="meta">
    <div class="tags"><span class="pill" style="border-color:{STATE_COLORS.get(r['state'], '#888')}">{_e(r['state'])}</span>
      <span class="pill" style="border-color:{CONF_COLORS.get(conf, '#888')}">{_e(conf)}</span>
      <span class="pill">{_e(v.get('duration'))}s</span><span class="pill">gancho: {_e(r.get('hook_style'))}</span></div>
    <h2>{_e(meta.get('title') or r['topic_title'])}</h2>
    <p class="muted">Pauta: {_e(r['topic_title'])}</p>
    <details open><summary>Roteiro e evidências</summary>{segs}</details>
    <details><summary>Checagem factual</summary>
      <p>Programática: {_list(fact.get('programmatic', []), 'aprovada (citações, números, nomes e atribuições conferidos)')}</p>
      <p>Juiz LLM: {_list(fact.get('judge', []), 'aprovado')}</p></details>
    <details><summary>Controle de qualidade</summary>
      <p>Técnico: {'✅' if tech.get('passed') else '❌'} · LUFS {_e(info.get('lufs'))} · legenda×áudio {_e(info.get('align_ratio') and round(info['align_ratio'] * 100))}% · {_e(info.get('size_mb'))} MB</p>
      {_list(tech.get('fails', []) + tech.get('warns', []), 'sem falhas')}
      <p>Visual: {'✅' if vis.get('passed') else '❌'} {_list(vis.get('fails', []), 'sem falhas')}</p>
      <p>Editorial: {'✅' if edi.get('passed') else '❌'} {_list(edi.get('fails', []), 'sem falhas')}</p></details>
    <details><summary>Storyboard ({len(plan.get('scenes', []))} cenas · confiança média {_e(plan.get('avg_confidence'))})</summary>
      <div class="tw"><table><tr><th>tempo</th><th>narração</th><th>tipo</th><th>asset oficial</th><th>efeito</th><th>conf.</th><th>motivo</th></tr>{scenes}</table></div></details>
    <details><summary>Descrição do YouTube</summary><pre>{_e(meta.get('description'))}</pre></details>
    <p class="muted">Fontes: {' · '.join(f"<a href='{_e(u)}'>{_e(s)}</a>" for s, u in sources)}</p>
  </div>
</section>""")
    page = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel de revisão — Shorts GTA 6</title><style>
:root{{--bg:#0d0e12;--card:#171922;--fg:#ececf1;--muted:#9a9aab;--line:#2a2d3a;--acc:#ffd60a}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif;padding:16px}}
header{{max-width:1300px;margin:0 auto 16px}} h1{{margin:0 0 4px;font-size:24px}} .muted{{color:var(--muted)}}
.card{{max-width:1300px;margin:0 auto 20px;background:var(--card);border-radius:14px;padding:16px;display:grid;grid-template-columns:340px 1fr;gap:20px}}
@media (max-width:820px){{.card{{grid-template-columns:1fr}}}}
video{{width:100%;aspect-ratio:9/16;background:#000;border-radius:10px}}
h2{{margin:6px 0;font-size:19px}} .pill{{display:inline-block;border:2px solid #555;border-radius:99px;padding:1px 10px;margin:0 6px 6px 0;font-size:12px;font-weight:600}}
details{{margin:8px 0;border-top:1px solid var(--line);padding-top:6px}} summary{{cursor:pointer;font-weight:600;color:var(--acc)}}
.role{{text-transform:uppercase;font-size:11px;color:var(--acc);margin-right:6px}} .claim{{color:#8fd19e}} .ok{{color:#8fd19e}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;font-size:12px;width:100%}} td,th{{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}}
pre{{white-space:pre-wrap;background:#0f1016;padding:10px;border-radius:8px;font-size:12px}} a{{color:var(--acc)}}
</style></head><body><header><h1>Painel de revisão — Shorts GTA 6</h1>
<p class="muted">{len(rows)} vídeo(s) produzidos pelo pipeline. Clique para assistir; abra as seções para ver roteiro, evidências, checagem, QA e storyboard.</p></header>
{''.join(cards) or '<p>Nenhum vídeo renderizado encontrado.</p>'}</body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


if __name__ == "__main__":
    p = build(settings().path("output_dir") / "painel.html", sys.argv[1:] or None)
    print(p)
    webbrowser.open(p.as_uri())
