"""Controle de qualidade automático. Nenhum vídeo reprovado é enviado.

Limitações declaradas (não automatizáveis com confiança total):
  - "Movimentos suaves" e "ritmo adequado" são verificados por proxies (freezedetect, duração
    das cenas), não por percepção humana.
  - A correspondência imagem/áudio é verificada pelas marcações oficiais dos assets e pela
    confiança do storyboard; não há "visão" conferindo cada quadro.
  - A checagem factual depende das evidências coletadas; fatos corretos mas ausentes das
    evidências são reprovados (falso negativo aceito por segurança).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from rapidfuzz import fuzz

from factory.assets.catalog import ALLOWED_LICENSE_PREFIXES
from factory.config import settings
from factory.database.db import DB
from factory.entities import find_entities
from factory.log import get_logger
from factory.textutil import norm

log = get_logger("qa")


def _ffprobe(path: Path) -> dict[str, Any]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _ff_filter(path: Path, args: list[str]) -> str:
    return subprocess.run(["ffmpeg", "-v", "info", "-nostats", "-i", str(path), *args, "-f", "null", "-"],
                          capture_output=True, text=True).stderr


def technical(path: Path, voice: dict[str, Any], chunks: list[dict[str, Any]], align_ratio: float | None) -> dict[str, Any]:
    cfg = settings()
    fails, warns, info = [], [], {}
    if not path.exists() or path.stat().st_size < 100_000:
        return {"passed": False, "fails": ["arquivo inexistente ou vazio"], "warns": [], "info": {}}
    p = _ffprobe(path)
    v = next((s for s in p["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in p["streams"] if s["codec_type"] == "audio"), None)
    dur = float(p["format"]["duration"])
    info.update(duration=round(dur, 2), size_mb=round(int(p["format"]["size"]) / 1e6, 2))
    if not v:
        fails.append("sem trilha de vídeo")
    else:
        if (int(v["width"]), int(v["height"])) != (int(cfg.get("video.width")), int(cfg.get("video.height"))):
            fails.append(f"resolução {v['width']}x{v['height']} (esperado 1080x1920)")
        if v["codec_name"] != "h264" or v.get("pix_fmt") != "yuv420p":
            fails.append(f"codec/pix_fmt incompatível: {v['codec_name']}/{v.get('pix_fmt')}")
        num, den = v["r_frame_rate"].split("/")
        if abs(float(num) / float(den) - int(cfg.get("video.fps"))) > 0.1:
            fails.append(f"fps {v['r_frame_rate']}")
    if not a:
        fails.append("sem áudio")
    elif a["codec_name"] != "aac":
        fails.append(f"áudio {a['codec_name']} (esperado aac)")
    if not (float(cfg.get("video.min_seconds")) <= dur <= float(cfg.get("video.max_seconds"))):
        fails.append(f"duração {dur:.1f}s fora do permitido")
    elif not (float(cfg.get("video.target_min_seconds")) <= dur <= float(cfg.get("video.target_max_seconds")) + 5):
        warns.append(f"duração {dur:.1f}s fora da faixa preferida")
    # reprodutível do início ao fim
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    if dec.returncode != 0 or dec.stderr.strip():
        fails.append("erro de decodificação: " + dec.stderr.strip()[:200])
    # quadros pretos, congelados, silêncio e loudness
    log_txt = _ff_filter(path, ["-vf", "blackdetect=d=0.25:pix_th=0.06",
                                "-af", "silencedetect=noise=-42dB:d=0.9,ebur128=framelog=quiet"])
    if "black_start" in log_txt:
        fails.append("quadros pretos detectados")
    for st, en in zip(re.findall(r"silence_start: ([\d.]+)", log_txt), re.findall(r"silence_end: ([\d.]+)", log_txt)):
        if float(st) > 0.3 and float(en) < dur - 0.7:
            fails.append(f"silêncio inesperado em {float(st):.1f}s-{float(en):.1f}s")
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", log_txt)
    if m:
        lufs = float(m[-1])
        info["lufs"] = lufs
        if not (-18.5 <= lufs <= -10.5):
            fails.append(f"loudness {lufs} LUFS fora de [-18.5, -10.5]")
    # narração não cortada
    if dur < float(voice.get("duration", dur)) - 0.3:
        fails.append(f"vídeo ({dur:.1f}s) mais curto que a narração ({voice['duration']:.1f}s): áudio cortado")
    if voice["sentences"]:
        if voice["sentences"][0]["start"] < 0.05:
            fails.append("narração começa cortada")
        if voice["sentences"][-1]["end"] > dur - 0.2:
            fails.append("narração termina cortada")
    # legendas
    speech = sum(s["end"] - s["start"] for s in voice["sentences"])
    covered = sum(c["end"] - c["start"] for c in chunks)
    info["caption_coverage"] = round(covered / max(speech, 0.1), 2)
    if not chunks:
        fails.append("sem legendas")
    elif covered / max(speech, 0.1) < 0.85:
        fails.append(f"legendas cobrem só {covered / speech:.0%} da fala")
    if any(len(c["words"]) > int(cfg.get("subtitles.max_words_per_chunk", 3)) for c in chunks):
        fails.append("legenda com palavras demais por bloco")
    info["align_ratio"] = align_ratio
    if align_ratio is None:
        warns.append("verificação legenda x áudio indisponível (whisper não rodou)")
    elif align_ratio < float(cfg.get("subtitles.min_alignment_ratio", 0.8)):
        fails.append(f"legenda não corresponde ao áudio (só {align_ratio:.0%} das palavras reconhecidas)")
    return {"passed": not fails, "fails": fails, "warns": warns, "info": info}


def visual(path: Path, storyboard: dict[str, Any]) -> dict[str, Any]:
    fails, warns = [], []
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, int(fps // 2))
    prev = None
    static_run = 0.0
    idx = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if idx % step == 0:
            g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            if idx % int(fps) == int(fps // 2):
                if g.std() < 12:
                    fails.append(f"quadro quase uniforme em {idx / fps:.1f}s")
                if g.mean() < 14:
                    fails.append(f"quadro escuro demais em {idx / fps:.1f}s")
            # movimento: diferença entre quadros desfocados (ignora o grão)
            small = cv2.GaussianBlur(cv2.resize(g, (270, 480)), (0, 0), 2).astype(np.float32)
            if prev is not None:
                static_run = static_run + step / fps if float(np.abs(small - prev).mean()) < 0.35 else 0.0
                if static_run >= 2.0:
                    fails.append(f"imagem parada (sem movimento) perto de {idx / fps:.1f}s")
                    static_run = -999
            prev = small
        idx += 1
    cap.release()
    scenes = storyboard["scenes"]
    durs = [s["end"] - s["start"] for s in scenes]
    if durs and max(durs) > 4.6:
        fails.append(f"cena longa demais ({max(durs):.1f}s): ritmo lento")
    if len(scenes) < storyboard["duration"] / 4.0:
        fails.append("poucos cortes para a duração")
    return {"passed": not fails, "fails": sorted(set(fails))[:10], "warns": warns}


def editorial(db: DB, video_id: str, script: dict[str, Any], fact: dict[str, Any], storyboard: dict[str, Any],
              metadata: dict[str, Any], topic: dict[str, Any]) -> dict[str, Any]:
    fails, warns = [], []
    if not fact.get("passed"):
        fails.append("checagem factual reprovada")
    narration = " ".join(s["text"] for s in script["segments"])
    text_all = f"{topic['title']} {narration} {metadata['title']}"
    if not (find_entities(text_all) or re.search(r"\bgta\s?(6|vi)\b|grand theft auto vi", norm(text_all))):
        fails.append("conteúdo não está claramente relacionado a GTA 6")
    for r in db.q("SELECT id, script, metadata FROM videos WHERE id != ? AND script IS NOT NULL "
                  "ORDER BY created_at DESC LIMIT 80", video_id):
        try:
            other = " ".join(s["text"] for s in json.loads(r["script"])["segments"])
            om = json.loads(r["metadata"] or "{}")
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if fuzz.token_set_ratio(norm(narration), norm(other)) >= 75:
            fails.append(f"roteiro muito parecido com o vídeo {r['id']}")
            break
        if om.get("title") and fuzz.ratio(norm(om["title"]), norm(metadata["title"])) >= 85:
            fails.append(f"título muito parecido com o vídeo {r['id']}")
            break
    for s in storyboard["scenes"]:
        if s["type"] != "title_card" and not s.get("asset_id"):
            fails.append(f"cena {s['i']} sem material visual")
        if s.get("license") and not s["license"].startswith(ALLOWED_LICENSE_PREFIXES):
            fails.append(f"cena {s['i']} com licença não permitida")
        if s["match_confidence"] < 0.5:
            fails.append(f"cena {s['i']} com correspondência visual fraca ({s['match_confidence']})")
    if storyboard["avg_confidence"] < 0.55:
        fails.append(f"correspondência visual média baixa ({storyboard['avg_confidence']})")
    uses: dict[str, int] = {}
    for s in storyboard["scenes"]:
        if s.get("asset_id") and s["type"] != "title_card":
            uses[s["asset_id"]] = uses.get(s["asset_id"], 0) + 1
    if uses and max(uses.values()) > 2:
        fails.append("mesmo asset repetido mais de 2 vezes")
    if len(metadata["title"]) > 100 or "#" in metadata["title"]:
        fails.append("título inválido para o YouTube")
    if len(metadata["description"]) > 4900:
        fails.append("descrição longa demais")
    return {"passed": not fails, "fails": fails, "warns": warns}
