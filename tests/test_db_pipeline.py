import json

from factory.database.db import State, now_iso
from factory.subtitles.align import chunk_words
from factory.youtube.metadata import build


def _topic(db, key="k1", title="Pauta teste"):
    return db.x("INSERT INTO topics(kind,cluster_key,title,confirmation,score,evidence,state,created_at) "
                "VALUES ('news',?,?,'CONFIRMADO',50,'[]','DISCOVERED',?)", key, title, now_iso())


def test_slot_idempotency(db):
    tid = _topic(db)
    a = db.create_video(tid, "2026-11-11", "09:00", "2026-11-11T12:00:00+00:00")
    b = db.create_video(tid, "2026-11-11", "09:00", "2026-11-11T12:00:00+00:00")
    assert a and b is None
    assert db.one("SELECT state FROM topics WHERE id=?", tid)["state"] == "USED"


def test_state_transitions_and_events(db):
    tid = _topic(db)
    vid = db.create_video(tid, "2026-11-11", "12:30", "2026-11-11T15:30:00+00:00")
    db.advance(vid, State.SCRIPTED, "ok", script={"segments": []})
    v = db.video(vid)
    assert v["state"] == "SCRIPTED" and v["script"] == {"segments": []}
    ev = db.q("SELECT to_state FROM events WHERE video_id=? ORDER BY id", vid)
    assert [e["to_state"] for e in ev] == ["VERIFIED", "SCRIPTED"]


def test_unique_youtube_id(db):
    tid = _topic(db)
    a = db.create_video(tid, "2026-11-11", "17:30", "x")
    tid2 = _topic(db, "k2")
    b = db.create_video(tid2, "2026-11-11", "21:00", "y")
    db.set_fields(a, youtube_id="abc")
    import sqlite3
    try:
        db.set_fields(b, youtube_id="abc")
        raise AssertionError("deveria impedir youtube_id duplicado")
    except sqlite3.IntegrityError:
        pass


def test_chunk_words_short_lines():
    words = [{"text": w, "start": i * 0.3, "end": i * 0.3 + 0.25, "seg": 0}
             for i, w in enumerate("Lucia saiu da prisão por pura sorte, e agora tem um plano.".split())]
    chunks = chunk_words(words, 3)
    assert all(len(c["words"]) <= 3 for c in chunks)
    assert " ".join(c["text"] for c in chunks) == " ".join(w["text"] for w in words)


def test_metadata(evidence):
    script = {"title": "Pré-download de GTA 6 começou #GTA6", "description": "Resumo.", "hashtags": ["#GTA6", "#PS5"],
              "segments": [{"text": "Lucia em Vice City.", "claims": [{"evidence": "E1"}]}]}
    m = build(script, evidence, "CONFIRMADO", "vid123")
    assert "#" not in m["title"]
    assert "ref:vid123" in m["description"]
    assert "rockstargames.com" in m["description"]
    assert "ign.com" not in m["description"]  # só fontes efetivamente citadas
    assert "Lucia Caminos" in m["tags"]
    assert json.dumps(m)
