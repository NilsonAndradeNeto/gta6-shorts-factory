"""Roda no PC do dono: baixa as lives/reacts escolhidas e manda para a nuvem.

O YouTube bloqueia downloads vindos dos servidores do GitHub ("confirme que você não é um robô"), então só esta
parte roda no PC (conexão residencial normal, sem burlar nada). Corte, legenda, checagem e postagem continuam na nuvem.

Uso:
  python scripts/baixar_fontes.py            # rotina diária (tarefa agendada do Windows)
  python scripts/baixar_fontes.py --agora 4  # baixa e pede 4 clipes publicados assim que ficarem prontos
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

RELEASE = "fontes"
WORK = ROOT / ".cache" / "fontes_pc"
SPAM = re.compile(r"giveaway|redeem|code|c[óo]digo|sorteio|uid|free fire diamonds|subscribe to win", re.I)


def sh(*cmd: str, check: bool = True) -> str:
    return subprocess.run(cmd, check=check, capture_output=True, text=True, encoding="utf-8").stdout


def cloud_db() -> Path:
    """Cópia do banco da nuvem (branch state), só para leitura."""
    sh("git", "fetch", "-q", "origin", "state")
    tmp = Path(tempfile.mkdtemp()) / "factory.db"
    with open(tmp, "wb") as f:
        f.write(subprocess.run(["git", "show", "origin/state:data/factory.db"], check=True, capture_output=True).stdout)
    return tmp


def ensure_release() -> set[str]:
    if subprocess.run(["gh", "release", "view", RELEASE], capture_output=True).returncode != 0:
        sh("gh", "release", "create", RELEASE, "--title", "Fontes baixadas no PC",
           "--notes", "Lives/reacts (Creative Commons ou autorizados) baixados no PC para a clipagem na nuvem. "
                      "Apagados após 3 dias.")
    data = json.loads(sh("gh", "release", "view", RELEASE, "--json", "assets"))
    names = set()
    limit = datetime.now(timezone.utc) - timedelta(days=3)
    for a in data["assets"]:
        if datetime.fromisoformat(a["createdAt"].replace("Z", "+00:00")) < limit:
            sh("gh", "release", "delete-asset", RELEASE, a["name"], "-y", check=False)
        else:
            names.add(a["name"])
    return names


def candidates(db_path: Path, want: int, have: set[str]) -> list[dict]:
    os.environ["FACTORY_DATA_DIR"] = str(db_path.parent)
    from factory.clips import engine
    from factory.config import settings
    from factory.database.db import DB
    db = DB(db_path)
    engine.initialize(db)
    settings()   # carrega o .env local (credenciais do YouTube para a busca)
    if os.environ.get("YT_REFRESH_TOKEN"):
        db.x("DELETE FROM clip_discover_runs")   # busca nova agora (cópia local; não altera a nuvem)
        try:
            engine.discover(db)
        except Exception as e:
            print("busca de fontes falhou:", e)
    max_h = float(settings().get("clips.max_age_days", 7)) * 24
    min_v = float(settings().get("clips.min_views_per_hour", 50))
    rows = [dict(r) for r in db.q("SELECT * FROM clip_sources WHERE status='NEW' OR "
                                  "(status='FAILED' AND (note LIKE '%bot%' OR note LIKE '%yt_dlp%'))")]
    rows = [r for r in rows if engine._hours_since(r.get("published_at")) <= max_h
            and engine.velocity(r) >= min_v and not SPAM.search(r.get("title") or "")
            and f"{r['video_id']}.mp4" not in have]
    rows.sort(key=engine.velocity, reverse=True)
    return rows[:want]


def download(src: dict, minutes: int) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"{src['video_id']}.mp4"
    if not out.exists():
        subprocess.run([sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-progress",
                        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
                        "--download-sections", f"*0-{minutes * 60}", "--merge-output-format", "mp4",
                        "-o", str(out), src["url"]], check=True)
    meta = {k: src.get(k) for k in ("video_id", "title", "channel", "channel_id", "views", "duration_s", "license",
                                    "basis", "url", "published_at")}
    (WORK / f"{src['video_id']}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agora", type=int, default=0, help="clipes extras publicados assim que prontos")
    ap.add_argument("--fontes", type=int, default=3, help="quantas lives/reacts baixar")
    ap.add_argument("--minutos", type=int, default=45, help="limite de minutos baixados por fonte")
    ap.add_argument("--sem-disparo", action="store_true", help="não iniciar a fábrica na nuvem")
    a = ap.parse_args()

    have = ensure_release()
    srcs = candidates(cloud_db(), a.fontes, have)
    print(f"{len(srcs)} fonte(s) escolhida(s)")
    sent = 0
    for s in srcs:
        try:
            mp4 = download(s, a.minutos)
            sh("gh", "release", "upload", RELEASE, str(mp4), str(mp4.with_suffix(".json")), "--clobber")
            sent += 1
            print("enviada:", s["video_id"], "-", s["title"][:70])
        except Exception as e:
            print("falhou:", s["video_id"], e)
    shutil.rmtree(WORK, ignore_errors=True)
    if not a.sem_disparo:
        args = ["gh", "workflow", "run", "factory.yml"] + (["-f", f"agora={a.agora}"] if a.agora else [])
        sh(*args)
        print("fábrica na nuvem iniciada", f"(agora={a.agora})" if a.agora else "")
    return 0 if (sent or have) else 1


if __name__ == "__main__":
    raise SystemExit(main())
