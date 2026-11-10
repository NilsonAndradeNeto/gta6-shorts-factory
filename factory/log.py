"""Logging com rotação e mascaramento de segredos."""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_ENV = ("YT_CLIENT_SECRET", "YT_REFRESH_TOKEN", "GITHUB_MODELS_TOKEN", "GEMINI_API_KEY",
               "GROQ_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "TELEGRAM_BOT_TOKEN")
_TOKEN_RE = re.compile(r"(ya29\.[\w\-\.]+|1//[\w\-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|AIza[\w\-]{30,}|gsk_\w{20,})")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for name in _SECRET_ENV:
            val = os.environ.get(name)
            if val and len(val) > 6:
                msg = msg.replace(val, f"<{name}>")
        msg = _TOKEN_RE.sub("<redacted>", msg)
        record.msg, record.args = msg, ()
        return True


_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        root = logging.getLogger("factory")
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(RedactingFilter())
        root.addHandler(sh)
        log_dir = Path(os.environ.get("FACTORY_LOG_DIR", Path(__file__).resolve().parent.parent / "logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "factory.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(RedactingFilter())
        root.addHandler(fh)
        _configured = True
    return logging.getLogger(f"factory.{name}")
