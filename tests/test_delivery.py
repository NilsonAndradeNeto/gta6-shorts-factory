from datetime import datetime
from zoneinfo import ZoneInfo

from factory.database.db import State, now_iso
from factory.delivery import telegram as tg

BRT = ZoneInfo("America/Sao_Paulo")


def _video(db, tmp_path, slot, state=State.VALIDATED):
    tid = db.x("INSERT INTO topics(kind,cluster_key,title,confirmation,score,evidence,state,created_at) "
               "VALUES ('news',?,?,'CONFIRMADO',50,'[]','DISCOVERED',?)", f"k{slot}", "t", now_iso())
    h, m = map(int, slot.split(":"))
    pa = datetime(2026, 11, 11, h, m, tzinfo=BRT).astimezone(ZoneInfo("UTC")).isoformat()
    vid = db.create_video(tid, "2026-11-11", slot, pa)
    f = tmp_path / f"{vid}.mp4"
    f.write_bytes(b"x")
    db.advance(vid, state, "", video_path=str(f), metadata={"title": "T", "hashtags": ["#GTA6"]})
    return vid


def test_deliver_all_four_at_nine(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    sent = []
    monkeypatch.setattr(tg, "send_video", lambda p, m, label, yt: sent.append(label))
    monkeypatch.setattr(tg, "_call", lambda *a, **k: {})
    ids = [_video(db, tmp_path, s) for s in ("09:00", "12:30", "17:30", "21:00")]
    _video(db, tmp_path, "10:00", State.BLOCKED)   # reprovado nunca é entregue
    early = datetime(2026, 11, 11, 7, 30, tzinfo=BRT)
    assert tg.deliver_due(db, early) == 0
    at_nine = datetime(2026, 11, 11, 9, 2, tzinfo=BRT)
    assert tg.deliver_due(db, at_nine) == 4
    assert sent == ["11/11 às 09:00", "11/11 às 12:30", "11/11 às 17:30", "11/11 às 21:00"]
    assert tg.deliver_due(db, at_nine) == 0           # não reenvia
    up, clean = tg.release_manifest(db, tmp_path / "out")
    assert {f"{i}.mp4" for i in ids} <= set(clean)    # entregues saem do depósito


def test_caption_has_hashtags():
    assert tg.tiktok_caption({"title": "Título", "hashtags": ["#GTA6", "#GTAVI"]}) == "Título\n\n#GTA6 #GTAVI"
