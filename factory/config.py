"""Carregamento de configuração (YAML + variáveis de ambiente)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Carrega .env local (sem sobrescrever variáveis já definidas). Nunca usado no CI."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    sources: dict[str, Any]

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, key: str) -> Path:
        p = Path(self.get(f"paths.{key}"))
        p = p if p.is_absolute() else ROOT / p
        override = os.environ.get(f"FACTORY_{key.upper()}")
        if override:
            p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def dry_run(self) -> bool:
        return os.environ.get("FACTORY_DRY_RUN", "0").lower() in ("1", "true", "yes")


@lru_cache(maxsize=1)
def settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    raw = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    src = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    return Settings(raw=raw, sources=src)
