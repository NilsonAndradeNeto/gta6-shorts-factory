from types import SimpleNamespace

from factory.database.db import State, now_iso
from factory.pipeline import Factory


def _vid(db, slot, state):
    tid = db.x("INSERT INTO topics(kind,cluster_key,title,confirmation,score,evidence,state,created_at) "
               "VALUES ('news',?,?,'CONFIRMADO',50,'[]','DISCOVERED',?)", f"k{slot}", "t", now_iso())
    vid = db.create_video(tid, "2026-11-17", slot, "2026-11-17T12:00:00+00:00")
    db.advance(vid, state, "")
    return vid


def test_clipagem_frees_unsent_production_slots(db):
    failed = _vid(db, "09:00", State.FAILED)
    scheduled = _vid(db, "09:02", State.SCHEDULED)
    freed = Factory.release_production_slots(SimpleNamespace(db=db))
    assert freed == [failed]
    assert db.video(failed)["state"] == "ABANDONED"
    assert not db.one("SELECT 1 FROM videos WHERE idem_key=?", "2026-11-17T09:00")   # horário livre p/ clipe
    assert db.video(scheduled)["state"] == "SCHEDULED"                                # já no YouTube: intocado
