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


def _engine(kind: str) -> Any:
    cfg = settings()
    speed = float(cfg.get("voice.speed", 1.08))
    if kind == "piper":
        return PiperEngine(os.environ.get("PIPER_VOICE", cfg.get("voice.piper_voice")), speed)
    if kind == "edge":
        return EdgeEngine(os.environ.get("EDGE_VOICE", cfg.get("voice.edge_voice")), speed)
    raise TTSError(f"motor desconhecido {kind}")


def synthesize_script(script: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Gera narração completa. Retorna wav + timeline de frases (início/fim em segundos)."""
    cfg = settings()
    order = [os.environ.get("TTS_PRIMARY", cfg.get("voice.primary")), cfg.get("voice.fallback")]
    last_err: Exception | None = None
    for kind in [k for k in order if k]:
        try:
            eng = _engine(kind)
            return _render_with(eng, script, workdir)
        except Exception as e:  # tenta o próximo motor
            last_err = e
            log.warning("Motor TTS %s falhou: %s", kind, e)
    raise TTSError(f"Nenhum motor TTS funcionou: {last_err}")


def _render_with(eng: Any, script: dict[str, Any], workdir: Path) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    pause = int(settings().get("voice.sentence_pause_ms", 140)) / 1000
    lead, tail = 0.12, 0.45
    pieces: list[np.ndarray] = [np.zeros(int(lead * SR), np.float32)]
    t = lead
    sentences = []
    for si, seg in enumerate(script["segments"]):
        for k, sent in enumerate(split_sentences(seg["text"])):
            spoken = normalize_for_tts(sent)
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


def project_root() -> Path:
    return ROOT
