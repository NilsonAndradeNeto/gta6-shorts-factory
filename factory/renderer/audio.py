"""Trilha e efeitos sonoros gerados proceduralmente (autoria do próprio sistema, sem licença
de terceiros) + mixagem final com ducking e normalização de loudness."""
from __future__ import annotations

import random
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np

from factory.config import settings
from factory.log import get_logger

log = get_logger("audio")
SR = 44100


def _write(path: Path, x: np.ndarray) -> Path:
    x = np.clip(x, -1, 1)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    return path


def _lowpass(x: np.ndarray, cutoff: float, rolloff: float = 0.25) -> np.ndarray:
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    g = 1 / (1 + (f / cutoff) ** (2 / rolloff))
    return np.fft.irfft(X * g, n=len(x))


def _highpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    g = 1 - 1 / (1 + (f / cutoff) ** 4)
    return np.fft.irfft(X * g, n=len(x))


def _note(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _saw(freq: float, n: int, phase: float = 0.0) -> np.ndarray:
    t = np.arange(n) / SR
    return 2 * ((t * freq + phase) % 1.0) - 1


def music_bed(duration: float, seed: str) -> np.ndarray:
    """Synthwave discreto: pad + baixo + bateria leve + arpejo. Varia por semente (tom, bpm, progressão)."""
    rnd = random.Random(seed)
    bpm = rnd.choice([92, 96, 100, 104, 108])
    root = rnd.choice([45, 47, 48, 50, 52])            # A2..E3
    progs = [[0, 8, 3, 10], [0, 5, 8, 7], [0, 10, 8, 7], [0, 8, 10, 7]]
    prog = rnd.choice(progs)
    beat = 60 / bpm
    bar = 4 * beat
    n = int((duration + 1.0) * SR)
    out = np.zeros(n)
    pad = np.zeros(n)
    bass = np.zeros(n)
    drums = np.zeros(n)
    arp = np.zeros(n)
    nbars = int(np.ceil(n / SR / bar))
    import hashlib
    rng = np.random.default_rng(int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16))
    for b in range(nbars):
        deg = prog[b % len(prog)]
        chord = [root + 12 + deg, root + 12 + deg + (3 if deg in (0, 5) else 4), root + 12 + deg + 7]
        s0, s1 = int(b * bar * SR), min(n, int((b + 1) * bar * SR))
        L = s1 - s0
        if L <= 0:
            break
        env = np.minimum(1, np.arange(L) / (0.35 * SR)) * np.minimum(1, (L - np.arange(L)) / (0.3 * SR))
        for m in chord:
            for det in (-0.07, 0.07):
                pad[s0:s1] += 0.05 * _saw(_note(m + det), L, rnd.random()) * env
        # baixo em colcheias
        for k in range(8):
            a = s0 + int(k * beat / 2 * SR)
            ln = int(beat / 2 * SR * 0.9)
            if a + ln > n:
                break
            e = np.exp(-np.arange(ln) / (0.12 * SR))
            bass[a:a + ln] += 0.22 * np.sign(np.sin(2 * np.pi * _note(root + deg) * np.arange(ln) / SR)) * e
        # bateria
        for k in range(4):
            a = s0 + int(k * beat * SR)
            if k in (0, 2):
                ln = int(0.3 * SR)
                if a + ln <= n:
                    tt = np.arange(ln) / SR
                    f = 45 + 80 * np.exp(-tt * 25)
                    drums[a:a + ln] += 0.6 * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt * 9)
            else:
                ln = int(0.18 * SR)
                if a + ln <= n:
                    drums[a:a + ln] += 0.18 * rng.standard_normal(ln) * np.exp(-np.arange(ln) / (0.04 * SR))
            for h in (0, 0.5):
                ha = a + int(h * beat * SR)
                hl = int(0.04 * SR)
                if ha + hl <= n:
                    drums[ha:ha + hl] += 0.05 * rng.standard_normal(hl) * np.exp(-np.arange(hl) / (0.01 * SR))
        # arpejo esparso em semicolcheias
        for k in range(16):
            if rnd.random() < 0.45:
                continue
            a = s0 + int(k * beat / 4 * SR)
            ln = int(0.16 * SR)
            if a + ln > n:
                break
            m = chord[k % 3] + 12
            e = np.exp(-np.arange(ln) / (0.05 * SR))
            arp[a:a + ln] += 0.07 * np.sin(2 * np.pi * _note(m) * np.arange(ln) / SR) * e
    pad = _lowpass(pad, 1400)
    bass = _lowpass(bass, 380)
    drums = drums + _highpass(drums * 0.4, 6000) * 0.3
    out = pad + bass + drums + arp
    # eco simples (sensação de espaço)
    d = int(beat * 0.75 * SR)
    echo = np.zeros_like(out)
    echo[d:] = out[:-d] * 0.22
    out = out + _lowpass(echo, 2500)
    fade = int(1.2 * SR)
    out[:int(0.4 * SR)] *= np.linspace(0, 1, int(0.4 * SR))
    out[-fade:] *= np.linspace(1, 0, fade)
    out = out[: int(duration * SR)]
    return out / (np.abs(out).max() + 1e-9) * 0.7


def whoosh(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    ln = int(0.38 * SR)
    noise = rng.standard_normal(ln)
    env = np.sin(np.linspace(0, np.pi, ln)) ** 2
    x = _highpass(_lowpass(noise, 3500), 500) * env
    return x / (np.abs(x).max() + 1e-9) * 0.6


def impact() -> np.ndarray:
    ln = int(0.9 * SR)
    t = np.arange(ln) / SR
    f = 40 + 60 * np.exp(-t * 18)
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 4.5)
    x += 0.25 * _lowpass(np.random.default_rng(7).standard_normal(ln), 900) * np.exp(-t * 20)
    return x / (np.abs(x).max() + 1e-9) * 0.8


def build_sfx(storyboard: dict[str, Any], duration: float) -> np.ndarray:
    n = int(duration * SR) + SR
    out = np.zeros(n)
    for s in storyboard["scenes"]:
        trans = s.get("transition_in")
        a = int(max(0, s["start"] - 0.12) * SR)
        if trans in ("whip", "zoom"):
            w = whoosh(s["i"]) * 0.55
        elif trans == "flash":
            w = impact()
        else:
            continue
        b = min(n, a + len(w))
        out[a:b] += w[: b - a]
    return out[: int(duration * SR)]


def mix(narration_wav: str, storyboard: dict[str, Any], duration: float, workdir: Path, seed: str) -> Path:
    cfg = settings()
    bed_db = float(cfg.get("music.bed_db", -24))
    lufs = float(cfg.get("voice.target_lufs", -14))
    inputs = ["-i", narration_wav]
    filters = ["[0:a]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-2:LRA=9[narr]"]
    mix_in = ["[narr]"]
    idx = 1
    if cfg.get("music.enabled", True):
        mpath = _write(workdir / "music.wav", music_bed(duration, seed))
        inputs += ["-i", str(mpath)]
        filters.append(f"[{idx}:a]aresample=48000,volume={bed_db + 10}dB[mus0]")
        filters.append("[narr]asplit=2[narrA][narrK]")
        filters.append("[mus0][narrK]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[mus]")
        mix_in = ["[narrA]", "[mus]"]
        idx += 1
    sfx = build_sfx(storyboard, duration)
    if np.abs(sfx).max() > 0:
        spath = _write(workdir / "sfx.wav", sfx)
        inputs += ["-i", str(spath)]
        filters.append(f"[{idx}:a]aresample=48000,volume=-13dB[sfx]")
        mix_in.append("[sfx]")
        idx += 1
    filters.append(f"{''.join(mix_in)}amix=inputs={len(mix_in)}:normalize=0:duration=first,"
                   f"loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000[out]")
    out = workdir / "mix.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[out]",
                    "-ac", "2", "-ar", "48000", str(out)], check=True)
    return out
