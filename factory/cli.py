"""Linha de comando da fábrica.

  python -m factory.cli run                 # ciclo completo (usado pelo GitHub Actions)
  python -m factory.cli research            # só coleta e classifica notícias
  python -m factory.cli topics              # lista pautas candidatas com score
  python -m factory.cli produce --topic ID  # produz 1 vídeo de teste (não agenda slot; upload opcional)
  python -m factory.cli auth --client-secrets client_secret.json
  python -m factory.cli youtube-check       # testa credenciais do YouTube
  python -m factory.cli status | dashboard | report | tag-assets | catalog
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factory.config import settings
from factory.database import State, connect
from factory.log import get_logger

log = get_logger("cli")


def _summary_out(text: str) -> None:
    f = os.environ.get("GITHUB_STEP_SUMMARY")
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)


def cmd_run(a: argparse.Namespace) -> int:
    from factory.dashboard import summary_markdown, write_html
    from factory.pipeline import Factory, RunLock
    db = connect()
    with RunLock(settings().path("data_dir")):
        summary = Factory(db).cycle(research=not a.no_research)
    write_html(db, settings().path("data_dir") / "dashboard.html")
    _summary_out(summary_markdown(db))
    print(json.dumps(summary, ensure_ascii=False, default=str, indent=1))
    return 0


def cmd_research(a: argparse.Namespace) -> int:
    from factory.pipeline import Factory
    db = connect()
    print(json.dumps(Factory(db).research(), ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_topics(a: argparse.Namespace) -> int:
    from factory.editorial.selector import rank_topics
    db = connect()
    for t in rank_topics(db)[: a.n]:
        print(f"[{t['id']:>4}] {t['score']:>5}  {t['confirmation']:<10} {t['kind']:<9} {t['title'][:90]}")
    return 0


def cmd_produce(a: argparse.Namespace) -> int:
    """Produz um vídeo avulso de teste num slot fictício 'test', sem consumir os slots reais."""
    from factory.pipeline import Factory
    db = connect()
    f = Factory(db)
    topic_id = a.topic
    if topic_id is None:
        from factory.editorial.selector import pick
        topic_id = pick(db, 1)[0]["id"]
    pa = (datetime.now(timezone.utc) + timedelta(hours=a.publish_in_hours)).replace(microsecond=0)
    slot = datetime.now().strftime("test-%H%M%S")
    vid = db.create_video(topic_id, datetime.now().date().isoformat(), slot, pa.isoformat())
    print("vídeo", vid)
    state = f.produce(vid)
    v = db.video(vid)
    print("estado:", state)
    if v.get("qa"):
        print(json.dumps(v["qa"], ensure_ascii=False, indent=1))
    if state == State.VALIDATED.value and a.upload:
        print("publicação:", f.publish(vid))
        print(db.video(vid).get("youtube_url"))
    print("arquivo:", v.get("video_path"))
    return 0 if state in (State.VALIDATED.value, State.UPLOADED.value, State.SCHEDULED.value) else 1


def cmd_auth(a: argparse.Namespace) -> int:
    from factory.youtube.auth import run
    vals = run(Path(a.client_secrets))
    print("\nSalve estes 3 valores como GitHub Secrets (Settings > Secrets and variables > Actions):\n")
    for k, v in vals.items():
        print(f"{k}={v}")
    print("\nNão compartilhe nem versione esses valores.")
    return 0


def cmd_youtube_check(a: argparse.Namespace) -> int:
    from factory.youtube import client as yt
    ch = yt.service().channels().list(part="snippet,status", mine=True).execute()
    for it in ch.get("items", []):
        print("Canal:", it["snippet"]["title"], "| id:", it["id"], "| longUploads:", it.get("status", {}).get("longUploadsStatus"))
    return 0 if ch.get("items") else 1


def cmd_status(a: argparse.Namespace) -> int:
    db = connect()
    for r in db.q("SELECT slot_date, slot_time, state, attempts, youtube_url, last_error FROM videos "
                  "ORDER BY slot_date DESC, slot_time DESC LIMIT ?", a.n):
        print(dict(r))
    return 0


def cmd_dashboard(a: argparse.Namespace) -> int:
    from factory.dashboard import write_html
    print(write_html(connect(), settings().path("data_dir") / "dashboard.html"))
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    from factory.analytics.metrics import collect, report_markdown
    db = connect()
    if a.fetch:
        collect(db)
    _summary_out(report_markdown(db))
    return 0


def cmd_tag(a: argparse.Namespace) -> int:
    from factory.assets.tagger import tag_pending
    from factory.llm import LLM
    print(tag_pending(LLM(connect()), a.n))
    return 0


def cmd_telegram_setup(a: argparse.Namespace) -> int:
    from factory.delivery.telegram import discover_chat_id
    chats = discover_chat_id()
    if not chats:
        print("Nenhuma mensagem encontrada. Mande 'oi' para o seu bot no Telegram e rode de novo.")
        return 1
    for c in chats:
        print(f"TELEGRAM_CHAT_ID={c['chat_id']}  ({c['name']}, {c['type']})")
    return 0


def cmd_telegram_test(a: argparse.Namespace) -> int:
    from factory.delivery.telegram import send_video
    db = connect()
    v = db.video(a.video)
    send_video(Path(v["video_path"]), v["metadata"], "(teste)", v.get("youtube_url"))
    print("enviado")
    return 0


def cmd_catalog(a: argparse.Namespace) -> int:
    from factory.assets.catalog import build_catalog
    c = build_catalog(force=a.force)
    print(len(c), "assets")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="factory")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--no-research", action="store_true")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("research").set_defaults(fn=cmd_research)
    t = sub.add_parser("topics")
    t.add_argument("-n", type=int, default=20)
    t.set_defaults(fn=cmd_topics)
    pr = sub.add_parser("produce")
    pr.add_argument("--topic", type=int)
    pr.add_argument("--upload", action="store_true", help="envia ao YouTube (privado + agendado)")
    pr.add_argument("--publish-in-hours", type=float, default=48.0)
    pr.set_defaults(fn=cmd_produce)
    au = sub.add_parser("auth")
    au.add_argument("--client-secrets", required=True)
    au.set_defaults(fn=cmd_auth)
    sub.add_parser("youtube-check").set_defaults(fn=cmd_youtube_check)
    s = sub.add_parser("status")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(fn=cmd_status)
    sub.add_parser("dashboard").set_defaults(fn=cmd_dashboard)
    rp = sub.add_parser("report")
    rp.add_argument("--fetch", action="store_true")
    rp.set_defaults(fn=cmd_report)
    tg = sub.add_parser("tag-assets")
    tg.add_argument("-n", type=int, default=20)
    tg.set_defaults(fn=cmd_tag)
    sub.add_parser("telegram-setup").set_defaults(fn=cmd_telegram_setup)
    tt = sub.add_parser("telegram-test")
    tt.add_argument("video")
    tt.set_defaults(fn=cmd_telegram_test)
    cg = sub.add_parser("catalog")
    cg.add_argument("--force", action="store_true")
    cg.set_defaults(fn=cmd_catalog)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
