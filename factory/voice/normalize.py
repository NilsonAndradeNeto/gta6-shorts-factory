"""Normalização de texto para fala em pt-BR (só para o TTS; a legenda usa o texto original)."""
from __future__ import annotations

import re

from factory.entities import LEXICON

_UNITS = ["zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez", "onze", "doze",
          "treze", "quatorze", "quinze", "dezesseis", "dezessete", "dezoito", "dezenove"]
_TENS = ["", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta", "oitenta", "noventa"]
_HUNDREDS = ["", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos", "seiscentos", "setecentos",
             "oitocentos", "novecentos"]
_ORD = {1: "primeiro", 2: "segundo", 3: "terceiro", 4: "quarto", 5: "quinto", 6: "sexto", 7: "sétimo", 8: "oitavo",
        9: "nono", 10: "décimo"}


def _lt1000(n: int) -> str:
    if n < 20:
        return _UNITS[n]
    if n < 100:
        t, u = divmod(n, 10)
        return _TENS[t] + (f" e {_UNITS[u]}" if u else "")
    if n == 100:
        return "cem"
    h, r = divmod(n, 100)
    return _HUNDREDS[h] + (f" e {_lt1000(r)}" if r else "")


def number_to_words(n: int) -> str:
    if n < 0:
        return "menos " + number_to_words(-n)
    if n < 1000:
        return _lt1000(n)
    groups: list[str] = []
    for div, sing, plur in ((1_000_000_000, "bilhão", "bilhões"), (1_000_000, "milhão", "milhões"), (1000, "mil", "mil")):
        q, n = divmod(n, div)
        if q:
            if div == 1000 and q == 1:
                groups.append("mil")
            elif div == 1000:
                groups.append(f"{number_to_words(q)} mil")
            else:
                groups.append(f"{number_to_words(q)} {sing if q == 1 else plur}")
    if n:
        sep = " e " if (n < 100 or n % 100 == 0) else " "
        return " ".join(groups) + sep + _lt1000(n)
    if len(groups) > 1:
        return " ".join(groups[:-1]) + " e " + groups[-1]
    return groups[0]


def _num(m: re.Match) -> str:
    raw = m.group(0)
    if "," in raw:
        a, b = raw.split(",", 1)
        a = a.replace(".", "")
        return f"{number_to_words(int(a))} vírgula {' '.join(number_to_words(int(c)) for c in b) if b.startswith('0') else number_to_words(int(b))}"
    return number_to_words(int(raw.replace(".", "")))


def normalize_for_tts(text: str) -> str:
    t = text.replace("’", "'").replace("—", ", ").replace("–", ", ").replace("…", "...")
    t = re.sub(r"\bUS\$\s?(\d[\d.,]*\d|\d)(?:\s+(bilh[õo]es|milh[õo]es|mil)\b)?",
               lambda m: f"{m.group(1)} {m.group(2) + ' de ' if m.group(2) else ''}dólares", t)
    t = re.sub(r"\$\s?([\d.,]+)", r"\1 dólares", t)
    t = re.sub(r"\bR\$\s?([\d.,]+)", r"\1 reais", t)
    t = re.sub(r"(\d)\s?%", r"\1 por cento", t)
    t = re.sub(r"\b(\d{1,2})º", lambda m: _ORD.get(int(m.group(1)), m.group(1)), t)
    # léxico: chaves mais longas primeiro, com fronteira de palavra
    for k in sorted(LEXICON, key=len, reverse=True):
        t = re.sub(rf"(?<![\w]){re.escape(k)}(?![\w])", LEXICON[k], t)
    t = re.sub(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?", _num, t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # frases muito longas: quebra em vírgula para pausas naturais e cortes de cena
        if len(p.split()) > 22 and "," in p:
            chunks = [c.strip() for c in p.split(",") if c.strip()]
            buf = ""
            for c in chunks:
                buf = f"{buf}, {c}" if buf else c
                if len(buf.split()) >= 9:
                    out.append(buf)
                    buf = ""
            if buf:
                if out:
                    out[-1] = f"{out[-1]}, {buf}"
                else:
                    out.append(buf)
        else:
            out.append(p)
    return out
