"""Tempos por palavra para legendas.

1. A timeline por frase vem da síntese (exata).
2. faster-whisper transcreve a narração com timestamps por palavra.
3. As palavras do roteiro (grafia correta) são alinhadas às reconhecidas (difflib + fuzzy).
   Palavras não reconhecidas recebem tempo interpolado dentro da frase.
A taxa de palavras reconhecidas também é a verificação "a legenda corresponde ao áudio".
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from rapidfuzz import fuzz

from factory.config import settings
from factory.log import get_logger
from factory.textutil import norm

log = get_logger("align")

_model = None


def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        name = settings().get("subtitles.whisper_model", "small")
        _model = WhisperModel(name, device="cpu", compute_type="int8",
                              download_root=str(settings().path("cache_dir") / "whisper"))
    return _model


def tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[\wÀ-ú'’\-$%º]+[.,!?;:]*", text) if w.strip()]


def _key(w: str) -> str:
    return norm(w).replace(" ", "")


def _load_16k(wav: str):
    """Lê o wav via ffmpeg como float32 16 kHz (evita a dependência do PyAV no faster-whisper)."""
    import subprocess

    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", wav, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def transcribe_words(wav: str, prompt: str = "") -> list[dict[str, Any]]:
    # Sem initial_prompt: com o roteiro como prompt o modelo tende a "pular" trechos já presentes no prompt.
    segs, _ = _whisper().transcribe(_load_16k(wav), language="pt", word_timestamps=True, beam_size=5,
                                    vad_filter=False, condition_on_previous_text=False)
    words = []
    for s in segs:
        for w in s.words or []:
            words.append({"w": w.word.strip(), "start": float(w.start), "end": float(w.end)})
    return words


def _proportional(tokens: list[str], start: float, end: float) -> list[dict[str, Any]]:
    weights = [max(2, len(_key(t))) + 1 for t in tokens]
    total = sum(weights) or 1
    out, t = [], start
    for tok, wgt in zip(tokens, weights):
        d = (end - start) * wgt / total
        out.append({"text": tok, "start": round(t, 3), "end": round(t + d, 3), "matched": False})
        t += d
    return out


def align(sentences: list[dict[str, Any]], wav: str, use_whisper: bool = True) -> dict[str, Any]:
    recog: list[dict[str, Any]] = []
    if use_whisper:
        try:
            recog = transcribe_words(wav, " ".join(s["text"] for s in sentences))
        except Exception as e:  # whisper é melhoria; sem ele cai no proporcional
            log.warning("Whisper indisponível (%s); usando tempos proporcionais", e)
    words_out: list[dict[str, Any]] = []
    matched_total, total = 0, 0
    for s in sentences:
        toks = tokenize(s["text"])
        total += len(toks)
        base = _proportional(toks, s["start"], s["end"])
        window = [r for r in recog if r["end"] > s["start"] - 0.15 and r["start"] < s["end"] + 0.15]
        if window:
            a = [_key(t) for t in toks]
            b = [_key(r["w"]) for r in window]
            sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
            mapping: dict[int, int] = {}
            for blk in sm.get_matching_blocks():
                for k in range(blk.size):
                    mapping[blk.a + k] = blk.b + k
            # tenta casar o que sobrou por similaridade (nomes estrangeiros, números por extenso)
            for i, ak in enumerate(a):
                if i in mapping:
                    continue
                lo = max([mapping[j] for j in mapping if j < i], default=-1) + 1
                hi = min([mapping[j] for j in mapping if j > i], default=len(b))
                best, best_s = None, 0.0
                for j in range(lo, hi):
                    sc = fuzz.ratio(ak, b[j])
                    if sc > best_s:
                        best, best_s = j, sc
                if best is not None and best_s >= 60:
                    mapping[i] = best
            for i, j in mapping.items():
                base[i]["start"] = round(max(s["start"], window[j]["start"]), 3)
                base[i]["end"] = round(min(s["end"] + 0.1, max(window[j]["end"], window[j]["start"] + 0.08)), 3)
                base[i]["matched"] = True
            matched_total += len(mapping)
            # garante monotonicidade e interpola não casados entre vizinhos casados
            for i in range(len(base)):
                if not base[i]["matched"]:
                    prev_end = base[i - 1]["end"] if i > 0 else s["start"]
                    nxt = next((base[k]["start"] for k in range(i + 1, len(base)) if base[k]["matched"]), s["end"])
                    n_un = 1
                    k = i + 1
                    while k < len(base) and not base[k]["matched"]:
                        n_un += 1
                        k += 1
                    d = max(0.06, (nxt - prev_end) / n_un)
                    base[i]["start"] = round(prev_end, 3)
                    base[i]["end"] = round(prev_end + d, 3)
            for i in range(1, len(base)):
                if base[i]["start"] < base[i - 1]["start"]:
                    base[i]["start"] = base[i - 1]["end"]
                    base[i]["end"] = max(base[i]["end"], base[i]["start"] + 0.06)
        for w in base:
            w["seg"] = s["seg"]
        words_out.extend(base)
    ratio = (matched_total / total) if (recog and total) else None
    log.info("Alinhamento: %s palavras, reconhecidas %.0f%%", total, (ratio or 0) * 100)
    return {"words": words_out, "match_ratio": ratio, "recognized": " ".join(r["w"] for r in recog)}


def chunk_words(words: list[dict[str, Any]], max_words: int = 3) -> list[dict[str, Any]]:
    """Agrupa palavras em legendas curtas, quebrando em pontuação e mudança de frase."""
    chunks: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []

    def flush() -> None:
        if cur:
            chunks.append({"words": list(cur), "start": cur[0]["start"], "end": cur[-1]["end"],
                           "text": " ".join(w["text"] for w in cur)})
            cur.clear()

    for i, w in enumerate(words):
        if cur and (w["seg"] != cur[-1]["seg"] or w["start"] - cur[-1]["end"] > 0.35):
            flush()
        cur.append(w)
        chars = sum(len(x["text"]) for x in cur)
        if len(cur) >= max_words or re.search(r"[.!?,;:]$", w["text"]) or chars >= 16:
            flush()
    flush()
    # estende cada legenda até o início da próxima (sem buracos piscando), máx. 0.5s
    for a, b in zip(chunks, chunks[1:]):
        a["end"] = min(b["start"], a["end"] + 0.5) if b["start"] > a["end"] else a["end"]
    return chunks
