"""Utilidades de texto compartilhadas."""
from __future__ import annotations

import hashlib
import re
import unicodedata

STOP = set("""a o as os um uma uns umas de da do das dos em no na nos nas por para pra com sem que e é ou se
the a an of to in on for and or is are was be with at by from this that it its as how why what new gta gta6 vi 6
grand theft auto rockstar games game""".split())


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s: str) -> str:
    s = strip_accents(s.lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def keywords(s: str) -> set[str]:
    return {w for w in norm(s).split() if len(w) > 2 and w not in STOP}


def sha(s: str, n: int = 16) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


def domain_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    d = (m.group(1) if m else "").lower()
    return d[4:] if d.startswith("www.") else d


def numbers_in(s: str) -> set[str]:
    """Números/dados relevantes num texto (anos, valores, datas), normalizados."""
    out: set[str] = set()
    for tok in re.findall(r"\d[\d.,]*\d|\d", s):
        # "1.500" (pt, milhar) e "5.2" (en, decimal) / "5,2" (pt, decimal) e "1,500" (en, milhar):
        # guarda todas as leituras para comparar textos em idiomas diferentes
        for v in (tok.replace(".", "").replace(",", "."), tok.replace(",", ""), tok.replace(",", ".")):
            try:
                f = float(v) if v.count(".") <= 1 else None
            except ValueError:
                f = None
            if f is not None:
                out.add(str(int(f)) if f.is_integer() else str(f))
    return out
