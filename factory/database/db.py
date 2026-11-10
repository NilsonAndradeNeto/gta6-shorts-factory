"""Persistência em SQLite + máquina de estados de produção.

O arquivo .db é versionado num branch dedicado (`state`) pelo GitHub Actions, então
nada se perde entre execuções efêmeras.
"""
from __future__ import annotations

import json
import sqlite3
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=DELETE;
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    summary TEXT,
    body TEXT,
    source_id TEXT NOT NULL,
    source_name TEXT,
    tier TEXT NOT NULL,              -- official|press|aggregator|community
    domain TEXT,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    confirmation TEXT,               -- CONFIRMADO|REPORTADO|TEORIA|NAO_VERIFICADO
    reliability REAL,
    image_url TEXT,
    cluster_key TEXT
);
CREATE INDEX IF NOT EXISTS ix_news_cluster ON news_items(cluster_key);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- news|evergreen
    cluster_key TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    confirmation TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    score_detail TEXT,
    evidence TEXT NOT NULL,          -- JSON: lista de evidências {id,url,source,tier,text}
    entities TEXT,                   -- JSON: entidades GTA6 citadas
    state TEXT NOT NULL DEFAULT 'DISCOVERED',
    created_at TEXT NOT NULL,
    used_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    idem_key TEXT UNIQUE NOT NULL,   -- data+slot: impede dois vídeos no mesmo horário
    topic_id INTEGER REFERENCES topics(id),
    slot_date TEXT NOT NULL,
    slot_time TEXT NOT NULL,
    publish_at_utc TEXT,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    script TEXT, storyboard TEXT, metadata TEXT, qa TEXT,
    audio_path TEXT, video_path TEXT, file_sha256 TEXT,
    youtube_id TEXT UNIQUE, youtube_url TEXT,
    angle TEXT, hook_style TEXT, duration REAL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_videos_state ON videos(state);

CREATE TABLE IF NOT EXISTS assets_used (
    video_id TEXT NOT NULL, asset_id TEXT NOT NULL, start REAL, end REAL,
    PRIMARY KEY (video_id, asset_id, start)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, video_id TEXT,
    from_state TEXT, to_state TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, video_id TEXT,
    stage TEXT, error TEXT, trace TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT, status TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    video_id TEXT NOT NULL, fetched_at TEXT NOT NULL, views INTEGER, likes INTEGER,
    comments INTEGER, avg_view_duration REAL, avg_view_pct REAL, source TEXT,
    PRIMARY KEY (video_id, fetched_at)
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, provider TEXT, model TEXT, purpose TEXT,
    ok INTEGER, chars_in INTEGER, chars_out INTEGER, error TEXT
);
"""


class State(str, Enum):
    DISCOVERED = "DISCOVERED"
    VERIFIED = "VERIFIED"
    SCRIPTED = "SCRIPTED"
    AUDIO_READY = "AUDIO_READY"
    ASSETS_READY = "ASSETS_READY"
    RENDERED = "RENDERED"
    VALIDATED = "VALIDATED"
    UPLOADED = "UPLOADED"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    # estados de erro / bloqueio
    FAILED = "FAILED"          # falha transitória: será retomado
    BLOCKED = "BLOCKED"        # reprovado no QA / verificação: NÃO publica
    ABANDONED = "ABANDONED"    # excedeu tentativas


PIPELINE_ORDER = [State.VERIFIED, State.SCRIPTED, State.AUDIO_READY, State.ASSETS_READY,
                  State.RENDERED, State.VALIDATED, State.UPLOADED, State.SCHEDULED, State.PUBLISHED]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(videos)")}
        if "tiktok_sent_at" not in cols:
            self.conn.execute("ALTER TABLE videos ADD COLUMN tiktok_sent_at TEXT")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, *args: Any) -> int:
        cur = self.conn.execute(sql, args)
        return cur.lastrowid or cur.rowcount

    # ---- vídeos -------------------------------------------------------------------
    def create_video(self, topic_id: int, slot_date: str, slot_time: str, publish_at_utc: str) -> str | None:
        """Cria vídeo para um slot. Retorna None se o slot já tem vídeo (idempotência)."""
        idem = f"{slot_date}T{slot_time}"
        vid = uuid.uuid4().hex[:12]
        ts = now_iso()
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO videos(id,idem_key,topic_id,slot_date,slot_time,publish_at_utc,state,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (vid, idem, topic_id, slot_date, slot_time, publish_at_utc, State.VERIFIED.value, ts, ts))
                c.execute("UPDATE topics SET state='USED', used_at=? WHERE id=?", (ts, topic_id))
                c.execute("INSERT INTO events(ts,video_id,from_state,to_state,message) VALUES (?,?,?,?,?)",
                          (ts, vid, None, State.VERIFIED.value, f"slot {idem}"))
        except sqlite3.IntegrityError:
            return None
        return vid

    def video(self, vid: str) -> dict[str, Any]:
        row = self.one("SELECT * FROM videos WHERE id=?", vid)
        if not row:
            raise KeyError(vid)
        d = dict(row)
        for k in ("script", "storyboard", "metadata", "qa"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    def advance(self, vid: str, to: State, message: str = "", **fields: Any) -> None:
        cur = self.one("SELECT state FROM videos WHERE id=?", vid)
        frm = cur["state"] if cur else None
        sets = ["state=?", "updated_at=?"]
        vals: list[Any] = [to.value, now_iso()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        vals.append(vid)
        with self.tx() as c:
            c.execute(f"UPDATE videos SET {', '.join(sets)} WHERE id=?", vals)
            c.execute("INSERT INTO events(ts,video_id,from_state,to_state,message) VALUES (?,?,?,?,?)",
                      (now_iso(), vid, frm, to.value, message[:1000]))

    def set_fields(self, vid: str, **fields: Any) -> None:
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        vals.append(vid)
        self.x(f"UPDATE videos SET {', '.join(sets)}, updated_at='{now_iso()}' WHERE id=?", *vals)

    def record_error(self, vid: str | None, stage: str, err: BaseException) -> None:
        self.x("INSERT INTO errors(ts,video_id,stage,error,trace) VALUES (?,?,?,?,?)",
               now_iso(), vid, stage, f"{type(err).__name__}: {err}"[:2000],
               "".join(traceback.format_exception(err))[-6000:])

    def event(self, message: str, vid: str | None = None) -> None:
        self.x("INSERT INTO events(ts,video_id,message) VALUES (?,?,?)", now_iso(), vid, message[:1000])


def connect(path: Path | None = None) -> DB:
    if path is None:
        from factory.config import settings
        path = settings().path("data_dir") / "factory.db"
    return DB(path)
