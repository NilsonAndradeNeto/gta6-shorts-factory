import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_LOG_DIR", str(tmp_path / "logs"))
    from factory.database.db import DB
    d = DB(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture()
def evidence():
    return [
        {"id": "E1", "news_id": 1, "url": "https://www.rockstargames.com/newswire/article/x", "source": "Rockstar Games",
         "tier": "official", "published_at": None, "image_url": "",
         "text": "Grand Theft Auto VI Pre-Load Begins on November 10\nPre-loading for Grand Theft Auto VI begins on "
                 "November 10 for PlayStation 5 and Xbox Series X|S. The game launches November 19, 2026."},
        {"id": "E2", "news_id": 2, "url": "https://www.ign.com/a", "source": "IGN", "tier": "press",
         "published_at": None, "image_url": "",
         "text": "GTA 6 will run at 30 frames per second on all consoles, according to a report from IGN sources."},
    ]
