"""Síntese de narração pt-BR.

Motor principal: Piper (offline, sem custo, roda no CPU do runner).
Reserva: edge-tts (gratuito, mas endpoint não oficial — sem garantia de disponibilidade).
A narração é sintetizada frase a frase; assim o tempo exato de cada frase é conhecido
e serve de base para cenas e legendas.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np
import requests

from factory.config import ROOT, settings
from factory.log import get_logger
from factory.voice.normalize import normalize_for_tts, split_sentences

log = get_logger("voice")

SR = 24000
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR"


class TTSError(RuntimeError):
    pass


def _voice_dir() -> Path:
    d = settings().path("cache_dir") / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_piper_voice(name: str) -> Path:
    """Baixa o modelo Piper (uma vez; cacheado no Actions)."""
    _, speaker, quality = name.split("-")
    d = _voice_dir()
    onnx = d / f"{name}.onnx"
    for suffix in (".onnx", ".onnx.json"):
        f = d / f"{name}{suffix}"
        if f.exists() and f.stat().st_size > 0:
            continue
        url = f"{PIPER_BASE}/{speaker}/{quality}/{name}{suffix}"
        log.info("Baixando voz %s", url)
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp = f.with_suffix(f.suffix + ".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
            tmp.replace(f)
    return onnx


def _espeak_dir() -> Path:
    """O espeak embutido no Piper não aceita caminhos com acentos (Windows); copia para caminho ASCII."""
    import piper
    src = Path(piper.__file__).parent / "espeak-ng-data"
    try:
        str(src).encode("ascii")
        return src
    except UnicodeEncodeError:
        dst = Path(os.environ.get("PROGRAMDATA", tempfile.gettempdir())) / "gta6factory" / "espeak-ng-data"
        if not (dst / "phontab").exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        return dst


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() == 2:
            data = data.reshape(-1, 2).mean(axis=1)
    if sr != SR:
        x_old = np.linspace(0, 1, len(data), endpoint=False)
        x_new = np.linspace(0, 1, int(len(data) * SR / sr), endpoint=False)
        data = np.interp(x_new, x_old, data).astype(np.float32)
    return data


def _trim(a: np.ndarray, thresh: float = 0.012, pad: int = int(0.03 * SR)) -> np.ndarray:
    idx = np.where(np.abs(a) > thresh)[0]
    if len(idx) == 0:
        return a
    return a[max(0, idx[0] - pad): min(len(a), idx[-1] + pad)]


class PiperEngine:
    name = "piper"

    def __init__(self, voice: str, speed: float):
        from piper import PiperVoice
        self.voice_name = voice
        self.voice = PiperVoice.load(str(ensure_piper_voice(voice)), espeak_data_dir=_espeak_dir())
        self.speed = speed

    def synth(self, text: str, out: Path) -> Path:
        from piper import SynthesisConfig
        cfg = SynthesisConfig(length_scale=1.0 / self.speed, noise_scale=0.72, noise_w_scale=0.85)
        with wave.open(str(out), "wb") as w:
            self.voice.synthesize_wav(text, w, syn_config=cfg)
        return out


class EdgeEngine:
    name = "edge"

    def __init__(self, voice: str, speed: float):
        import edge_tts  # noqa: F401
        self.voice_name = voice
        self.rate = f"+{int(round((speed - 1) * 100))}%" if speed >= 1 else f"-{int(round((1 - speed) * 100))}%"

    def synth(self, text: str, out: Path) -> Path:
        import edge_tts
        mp3 = out.with_suffix(".mp3")

        async def run() -> None:
            await edge_tts.Communicate(text, self.voice_name, rate=self.rate).save(str(mp3))

        asyncio.run(run())
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3), "-ac", "1", "-ar", str(SR), str(out)], check=True)
        mp3.unlink(missing_ok=True)
        return out


def _engine(spec: dict[str, Any]) -> Any:
    cfg = settings()
    speed = float(cfg.get("voice.speed", 1.08))
    kind = spec["engine"]
    if kind == "gemini":
        return GeminiEngine(spec["model"], spec.get("voice", "Fenrir"), spec.get("style", ""))
    if kind == "piper":
        return PiperEngine(os.environ.get("PIPER_VOICE", spec.get("voice", cfg.get("voice.piper_voice"))), speed)
    if kind == "edge":
        return EdgeEngine(os.environ.get("EDGE_VOICE", spec.get("voice", cfg.get("voice.edge_voice"))), speed)
    raise TTSError(f"motor desconhecido {kind}")


def synthesize_script(script: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Gera narração completa tentando os motores na ordem de `voice.engines`."""
    cfg = settings()
    specs = cfg.get("voice.engines") or [{"engine": cfg.get("voice.primary")}, {"engine": cfg.get("voice.fallback")}]
    last_err: Exception | None = None
    for spec in specs:
        try:
            eng = _engine(spec)
            return _render_with(eng, script, workdir)
        except Exception as e:  # tenta o próximo motor
            last_err = e
            log.warning("Motor TTS %s falhou: %s", spec.get("engine"), e)
    raise TTSError(f"Nenhum motor TTS funcionou: {last_err}")


def _render_whole(eng: Any, script: dict[str, Any], workdir: Path) -> tuple[list[np.ndarray], list[dict[str, Any]], float]:
    """Motores "naturais" (Gemini): o roteiro inteiro numa chamada só, para a entonação fluir.
    Os tempos de cada frase vêm do alinhamento palavra a palavra (faster-whisper)."""
    from factory.subtitles.align import align, tokenize
    sents = [{"seg": si, "role": seg.get("role", ""), "text": s}
             for si, seg in enumerate(script["segments"]) for s in split_sentences(seg["text"])]
    full = " ".join(s["text"] for s in sents)
    f = workdir / "whole.wav"
    eng.synth(full, f)
    a = _trim(_read_wav(f))
    expected = len(full.split()) / 3.6          # ~3,6 palavras/s é rápido; menos que isso = áudio truncado
    if len(a) / SR < expected * 0.6 or float(np.abs(a).max()) < 0.02:
        raise TTSError(f"áudio do {eng.name} curto/inaudível ({len(a) / SR:.1f}s para {len(full.split())} palavras)")
    lead = 0.12
    tmp = workdir / "whole_trim.wav"
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.concatenate([np.zeros(int(lead * SR), np.float32), a]) * 32767).clip(-32768, 32767)
                      .astype(np.int16).tobytes())
    end = lead + len(a) / SR
    al = align([{"seg": 0, "text": full, "start": lead, "end": end}], str(tmp))
    words = al["words"]
    if al["match_ratio"] is not None and al["match_ratio"] < 0.75:
        raise TTSError(f"{eng.name} não leu o roteiro fielmente (só {al['match_ratio']:.0%} das palavras reconhecidas)")
    i = 0
    for s in sents:
        n = len(tokenize(s["text"]))
        ws = words[i:i + n] or words[-1:]
        s["start"], s["end"], s["spoken"] = round(ws[0]["start"], 3), round(ws[-1]["end"], 3), s["text"]
        i += n
    sents[0]["start"] = max(lead, sents[0]["start"])
    return [np.zeros(int(lead * SR), np.float32), a], sents, end


def _render_with(eng: Any, script: dict[str, Any], workdir: Path) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    pause = int(settings().get("voice.sentence_pause_ms", 140)) / 1000
    lead, tail = 0.12, 0.45
    if getattr(eng, "whole", False):
        pieces, sentences, t = _render_whole(eng, script, workdir)
        pieces.append(np.zeros(int(tail * SR), np.float32))
        return _finish(eng, pieces, sentences, t, workdir)
    pieces: list[np.ndarray] = [np.zeros(int(lead * SR), np.float32)]
    t = lead
    sentences = []
    for si, seg in enumerate(script["segments"]):
        for k, sent in enumerate(split_sentences(seg["text"])):
            # o léxico fonético só ajuda o Piper; vozes neurais leem melhor o texto original
            spoken = normalize_for_tts(sent) if eng.name == "piper" else sent
            f = workdir / f"s{si:02d}_{k:02d}.wav"
            eng.synth(spoken, f)
            a = _trim(_read_wav(f))
            if len(a) < int(0.15 * SR) or float(np.abs(a).max()) < 0.02:
                raise TTSError(f"áudio vazio/inaudível para a frase: {sent!r}")
            dur = len(a) / SR
            sentences.append({"seg": si, "role": seg.get("role", ""), "text": sent, "spoken": spoken,
                              "start": round(t, 3), "end": round(t + dur, 3)})
            pieces.append(a)
            gap = pause * (1.8 if k == len(split_sentences(seg["text"])) - 1 else 1.0)
            pieces.append(np.zeros(int(gap * SR), np.float32))
            t += dur + gap
    pieces.append(np.zeros(int(tail * SR), np.float32))
    return _finish(eng, pieces, sentences, t, workdir)


def _finish(eng: Any, pieces: list[np.ndarray], sentences: list[dict[str, Any]], t: float, workdir: Path) -> dict[str, Any]:
    audio = np.concatenate(pieces)
    peak = float(np.abs(audio).max()) or 1.0
    audio = audio / peak * 0.89
    raw = workdir / "narration_raw.wav"
    with wave.open(str(raw), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())
    out = workdir / "narration.wav"
    # leve compressão + EQ de presença para voz mais "de YouTube"; loudness final é feito na mixagem
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-af",
                    "highpass=f=70,equalizer=f=3200:t=q:w=1.2:g=2.5,acompressor=threshold=-20dB:ratio=3:attack=5:release=80,"
                    "alimiter=limit=0.95", "-ar", "48000", "-ac", "1", str(out)], check=True)
    duration = len(audio) / SR
    engine_id = f"{eng.name}:{eng.voice_name}"
    log.info("Narração %.1fs com %s (%d frases)", duration, engine_id, len(sentences))
    return {"wav": str(out), "duration": round(duration, 3), "sentences": sentences, "engine": engine_id,
            "speech_end": round(t, 3)}


class GeminiEngine:
    """Voz natural do Google (Gemini TTS, camada gratuita do AI Studio). Roteiro inteiro numa chamada."""
    name = "gemini"
    whole = True

    def __init__(self, model: str, voice: str, style: str):
        self.key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not self.key:
            raise TTSError("GEMINI_API_KEY ausente")
        self.model, self.voice_name, self.style = model, voice, style

    def synth(self, text: str, out: Path) -> Path:
        import base64
        body = {"contents": [{"parts": [{"text": (self.style + " " if self.style else "") + text}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                                     "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice_name}}}}}
        for attempt in range(3):
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                              headers={"x-goog-api-key": self.key}, json=body, timeout=240)
            if r.status_code == 429 or r.status_code >= 500:
                if r.status_code == 429 and "per day" in r.text.lower():
                    raise TTSError(f"cota diária do {self.model} esgotada")
                import time
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise TTSError(f"{self.model} HTTP {r.status_code}: {r.text[:200]}")
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            data = base64.b64decode(part["data"])
            if part.get("mimeType", "").startswith("audio/wav"):
                out.write_bytes(data)
            else:   # PCM 16-bit mono 24 kHz
                with wave.open(str(out), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(24000)
                    w.writeframes(data)
            return out
        raise TTSError(f"{self.model}: falhou após retentativas")
