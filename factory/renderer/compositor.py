"""Compositor quadro a quadro (numpy/OpenCV) com envio por pipe ao FFmpeg.

Camadas por quadro:
  1. cena base: imagem com movimento de câmera (zoom/pan com precisão subpixel, ponto focal),
     card com fundo desfocado, clipe oficial em vídeo, ou card tipográfico;
  2. transição de entrada (whip com motion blur, zoom punch, flash, corte seco);
  3. título de impacto do segmento (pop-in), selo de confirmação, crédito, barra de progresso;
  4. legendas palavra a palavra com destaque da palavra falada.
Tudo fica dentro da área segura dos Shorts (evita a coluna de botões e o rodapé).
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any, Iterator

import cv2
import numpy as np

from factory.assets.catalog import _cache
from factory.config import settings
from factory.log import get_logger
from factory.renderer import text as T

log = get_logger("render")

BADGE_COLORS = {"OFICIAL": (46, 204, 113), "REPORTADO": (255, 176, 32), "TEORIA": (186, 104, 255)}
# Área segura (px) para 1080x1920
# Perfis de layout. "short" = 9:16 (área segura dos Shorts/TikTok); "long" = 16:9 para vídeos longos do YouTube.
PROFILES = {
    "short": {"W": 1080, "H": 1920, "caption_cy": 1240, "caption_cx": 500, "caption_maxw": 880, "caption_size": 76,
              "headline_y": 330, "top_y": 150, "card_box": (1010, 1300), "card_cy": 820, "title_cy": 800,
              "title_maxw": 960, "headline_maxw": 880, "title_size": 170, "headline_size": 88, "vertical": True},
    "long": {"W": 1920, "H": 1080, "caption_cy": 965, "caption_cx": 960, "caption_maxw": 1500, "caption_size": 56,
             "headline_y": 70, "top_y": 40, "card_box": (1500, 900), "card_cy": 520, "title_cy": 500,
             "title_maxw": 1600, "headline_maxw": 1300, "title_size": 150, "headline_size": 70, "vertical": False},
}
SAFE: dict[str, Any] = dict(PROFILES["short"])


def use_profile(name: str) -> dict[str, Any]:
    SAFE.clear()
    SAFE.update(PROFILES[name])
    return SAFE


def imread(path: str) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)   # suporta caminhos com acento no Windows
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"imagem ilegível: {path}")
    return img


def ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 0.5 - 0.5 * math.cos(math.pi * p)


def ease_out_back(p: float) -> float:
    p = min(1.0, max(0.0, p))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


class ImageScene:
    def __init__(self, scene: dict[str, Any], W: int, H: int):
        self.W, self.H = W, H
        self.dur = max(0.05, scene["end"] - scene["start"])
        self.effect = scene.get("effect", "zoom_in")
        fx, fy = (scene.get("focus") or [0.5, 0.45])[:2]
        img = imread(scene["asset_path"])
        h, w = img.shape[:2]
        landscape = w > h
        layout = scene.get("layout", "auto")
        if layout == "auto":
            layout = "card" if self.effect in ("zoom_in", "zoom_out") else "fullbleed"
        if SAFE["vertical"]:
            self.card = landscape and layout == "card" and scene.get("type") != "title_card"
        else:   # 16:9: imagens largas em tela cheia; verticais/quadradas viram card sobre fundo desfocado
            self.card = (w / h) < 1.5 and scene.get("type") != "title_card"
        self.focus_known = bool(scene.get("focus_known"))
        if scene.get("type") == "title_card":
            self.effect = "zoom_in"
        self.zmax = 1.14
        if self.card:
            # fundo: imagem cobrindo a tela, desfocada e escurecida
            s = max(W / w, H / h)
            small = cv2.resize(img, (int(w * s / 6), int(h * s / 6)), interpolation=cv2.INTER_AREA)
            small = cv2.GaussianBlur(small, (0, 0), 6)
            bg = cv2.resize(small, (int(w * s), int(h * s)), interpolation=cv2.INTER_LINEAR)
            y0, x0 = (bg.shape[0] - H) // 2, (bg.shape[1] - W) // 2
            self.bg = (bg[y0:y0 + H, x0:x0 + W].astype(np.float32) * 0.42).astype(np.uint8)
            bw, bh = SAFE["card_box"]
            fit = min(bw / w, bh / h)
            self.cw, self.ch = int(w * fit) // 2 * 2, int(h * fit) // 2 * 2
            self.cx0 = (W - self.cw) // 2
            self.cy = SAFE["card_cy"] - self.ch // 2
            self.src = cv2.resize(img, (int(self.cw * self.zmax), int(self.ch * self.zmax)), interpolation=cv2.INTER_AREA)
            mask = np.zeros((self.ch, self.cw), np.uint8)
            cv2.rectangle(mask, (28, 0), (self.cw - 29, self.ch - 1), 255, -1)
            cv2.rectangle(mask, (0, 28), (self.cw - 1, self.ch - 29), 255, -1)
            for cx, cy in ((28, 28), (self.cw - 29, 28), (28, self.ch - 29), (self.cw - 29, self.ch - 29)):
                cv2.circle(mask, (cx, cy), 28, 255, -1, lineType=cv2.LINE_AA)
            self.mask = (mask.astype(np.float32) / 255.0)[:, :, None]
            # sombra do card no fundo
            sh = np.zeros((H, W), np.float32)
            sh[self.cy + 18:self.cy + self.ch + 18, self.cx0 + 10:self.cx0 + self.cw + 10] = 1
            sh = cv2.GaussianBlur(sh, (0, 0), 18)[:, :, None]
            self.bg = (self.bg.astype(np.float32) * (1 - 0.6 * sh)).astype(np.uint8)
            self.fx, self.fy = fx, fy
        else:
            s_cover = max(W / w, H / h)
            k = s_cover * self.zmax
            self.src = cv2.resize(img, (max(W, int(w * k)), max(H, int(h * k))), interpolation=cv2.INTER_AREA)
            self.k_full = self.src.shape[1] / w  # escala do src redimensionado
            self.s_cover = s_cover
            self.iw, self.ih = w, h
            self.fx, self.fy = fx, fy
        if scene.get("type") == "title_card":
            self.dim = 0.38
            blurred = cv2.GaussianBlur(cv2.resize(self.src, (self.src.shape[1] // 4, self.src.shape[0] // 4)), (0, 0), 5)
            self.src = cv2.resize(blurred, (self.src.shape[1], self.src.shape[0]))
        else:
            self.dim = 1.0

    def _zoom_pan(self, p: float) -> tuple[float, float, float]:
        e = self.effect
        if e == "zoom_in":
            return 1.0 + 0.12 * p, 0.0, 0.0
        if e == "zoom_out":
            return 1.12 - 0.12 * p, 0.0, 0.0
        # pans combinam leve zoom para garantir folga e movimento contínuo
        if e == "pan_right":
            return 1.04 + 0.04 * p, -0.5 + p, 0.0
        if e == "pan_left":
            return 1.04 + 0.04 * p, 0.5 - p, 0.0
        if e == "pan_up":
            return 1.08 + 0.04 * p, 0.0, 0.5 - p
        if e == "pan_down":
            return 1.08 + 0.04 * p, 0.0, -0.5 + p
        return 1.0, 0.0, 0.0

    def frame(self, t: float) -> np.ndarray:
        p = ease(t / self.dur)
        z, px, py = self._zoom_pan(p)
        if self.card:
            out = self.bg.copy()
            sw, sh = self.src.shape[1], self.src.shape[0]
            cw, ch = self.cw * self.zmax / z, self.ch * self.zmax / z   # janela visível em px do src
            cx = min(max(self.fx * sw, cw / 2), sw - cw / 2)
            cy = min(max(self.fy * sh, ch / 2), sh - ch / 2)
            k = self.cw / cw
            M = np.float32([[k, 0, -k * (cx - cw / 2)], [0, k, -k * (cy - ch / 2)]])
            card = cv2.warpAffine(self.src, M, (self.cw, self.ch), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            x0 = self.cx0
            roi = out[self.cy:self.cy + self.ch, x0:x0 + self.cw].astype(np.float32)
            out[self.cy:self.cy + self.ch, x0:x0 + self.cw] = (card * self.mask + roi * (1 - self.mask)).astype(np.uint8)
            return out
        W, H = self.W, self.H
        sw, sh = self.src.shape[1], self.src.shape[0]
        k = (self.s_cover * z) / self.k_full             # px de saída por px do src redimensionado
        vw, vh = W / k, H / k                              # janela visível em px do src
        slack_x, slack_y = max(0.0, sw - vw), max(0.0, sh - vh)
        # com ponto focal conhecido, o pan percorre no máximo 1/4 da janela para manter o assunto em cena
        amp_x = min(slack_x * 0.9, 0.25 * vw) if self.focus_known else slack_x * 0.9
        amp_y = min(slack_y * 0.9, 0.25 * vh) if self.focus_known else slack_y * 0.9
        cx = self.fx * sw + px * amp_x
        cy = self.fy * sh + py * amp_y
        cx = min(max(cx, vw / 2), sw - vw / 2)
        cy = min(max(cy, vh / 2), sh - vh / 2)
        M = np.float32([[k, 0, -k * (cx - vw / 2)], [0, k, -k * (cy - vh / 2)]])
        out = cv2.warpAffine(self.src, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        if self.dim < 1.0:
            out = (out.astype(np.float32) * self.dim).astype(np.uint8)
        return out


class VideoScene:
    """Clipe oficial: recorte 9:16 em torno do centro, leve zoom; loop vai-e-volta se o clipe for curto."""

    def __init__(self, scene: dict[str, Any], W: int, H: int, fps: int):
        self.W, self.H, self.fps = W, H, fps
        self.dur = max(0.05, scene["end"] - scene["start"])
        src = scene.get("video_src") or scene["asset_path"]
        clip = scene.get("clip") or {"in": 0.0, "out": self.dur}
        self.ow, self.oh = int(W * 1.06) // 2 * 2, int(H * 1.06) // 2 * 2
        length = max(0.2, clip["out"] - clip["in"])
        need = min(length, self.dur)
        if SAFE["vertical"]:
            vf = f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale={self.ow}:{self.oh}:flags=lanczos,fps={fps}"
        else:
            vf = (f"scale={self.ow}:{self.oh}:force_original_aspect_ratio=increase:flags=lanczos,"
                  f"crop={self.ow}:{self.oh},fps={fps}")
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{clip['in']:.3f}", "-t", f"{need + 0.05:.3f}", "-i", src,
               "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
        fsize = self.ow * self.oh * 3
        n = len(raw) // fsize
        if n == 0:
            raise ValueError(f"clipe sem quadros: {src}")
        self.frames = [np.frombuffer(raw, np.uint8, count=fsize, offset=i * fsize).reshape(self.oh, self.ow, 3)
                       for i in range(n)]

    def frame(self, t: float) -> np.ndarray:
        n = len(self.frames)
        i = int(t * self.fps)
        if n > 1:
            period = 2 * (n - 1)
            j = i % period
            i = j if j < n else period - j     # vai-e-volta
        else:
            i = 0
        f = self.frames[min(i, n - 1)]
        # o quadro decodificado tem 6% de sobra; k=1 mostra o centro 1:1, k>1 aproxima
        k = 1.0 + 0.04 * ease(t / self.dur)
        vw, vh = self.W / k, self.H / k
        x0, y0 = (self.ow - vw) / 2, (self.oh - vh) / 2
        M = np.float32([[k, 0, -k * x0], [0, k, -k * y0]])
        return cv2.warpAffine(f, M, (self.W, self.H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


class CaptionLayer:
    def __init__(self, chunks: list[dict[str, Any]]):
        self.chunks = chunks
        self.cache: dict[tuple[int, int], np.ndarray] = {}

    def _compose(self, ci: int, active: int) -> np.ndarray:
        key = (ci, active)
        if key in self.cache:
            return self.cache[key]
        words = [w["text"].upper() for w in self.chunks[ci]["words"]]
        out = T.caption_rgba(words, active, SAFE["caption_size"], SAFE["caption_maxw"])
        self.cache[key] = out
        return out

    def draw(self, frame: np.ndarray, t: float) -> None:
        for ci, c in enumerate(self.chunks):
            if c["start"] - 0.02 <= t < c["end"]:
                active = 0
                for wi, w in enumerate(c["words"]):
                    if t >= w["start"] - 0.03:
                        active = wi
                img = self._compose(ci, active)
                pop = 0.86 + 0.14 * ease_out_back((t - c["start"]) / 0.13)
                img = T.scale_rgba(img, pop)
                x = SAFE["caption_cx"] - img.shape[1] // 2
                y = SAFE["caption_cy"] - img.shape[0] // 2
                T.blend(frame, img, x, y)
                return


def _apply_transition(kind: str, cur: np.ndarray, prev: np.ndarray | None, p: float, W: int) -> np.ndarray:
    if kind in ("none", "cut") or prev is None or p >= 1:
        if kind == "flash" and p < 1:
            a = (1 - ease(p)) * 0.85
            return cv2.addWeighted(cur, 1 - a, np.full_like(cur, 255), a, 0)
        return cur
    e = ease(p)
    if kind == "whip":
        shift = int(W * 0.4 * (1 - e))
        k = max(1, int(60 * (1 - e)))
        a = cv2.warpAffine(prev, np.float32([[1, 0, -W * 0.4 * e], [0, 1, 0]]), (W, cur.shape[0]), borderMode=cv2.BORDER_REFLECT)
        b = cv2.warpAffine(cur, np.float32([[1, 0, shift], [0, 1, 0]]), (W, cur.shape[0]), borderMode=cv2.BORDER_REFLECT)
        mixf = cv2.addWeighted(a, 1 - e, b, e, 0)
        return cv2.blur(mixf, (k, 1)) if k > 2 else mixf
    if kind == "zoom":
        s = 1.0 + 0.16 * (1 - e)
        H = cur.shape[0]
        M = cv2.getRotationMatrix2D((W / 2, H / 2), 0, s)
        z = cv2.warpAffine(cur, M, (W, H), borderMode=cv2.BORDER_REFLECT)
        return cv2.addWeighted(prev, 1 - e, z, e, 0)
    if kind == "flash":
        a = (1 - e) * 0.85
        return cv2.addWeighted(cur, 1 - a, np.full_like(cur, 255), a, 0)
    return cur


def render(storyboard: dict[str, Any], chunks: list[dict[str, Any]], audio_path: str, out_path: Path,
           profile: str = "short") -> Path:
    cfg = settings()
    prof = use_profile(profile)
    W, H, FPS = prof["W"], prof["H"], int(cfg.get("video.fps"))
    duration = float(storyboard["duration"])
    nframes = int(math.ceil(duration * FPS))
    scenes = storyboard["scenes"]
    for s in scenes:
        if s["type"] == "video" and s.get("asset_id"):
            from factory.assets.catalog import build_catalog
            src = next((a.get("src") for a in build_catalog() if a["id"] == s["asset_id"]), None)
            if src:
                s["video_src"] = str(_cache() / src)
    captions = CaptionLayer(chunks)
    badge_txt = storyboard.get("status_badge") or ""
    badge = T.chip(badge_txt, BADGE_COLORS[badge_txt]) \
        if badge_txt in BADGE_COLORS and cfg.get("video.show_badge", False) else None
    credits = sorted({s.get("credit") for s in scenes if s.get("credit")})
    credit = T.text_rgba(" · ".join(credits), "semibold", 26, fill=(235, 235, 235), stroke=2, pad=4) \
        if credits and cfg.get("video.show_credit", False) else None
    headline_cache: dict[int, np.ndarray] = {}
    TR = 0.24
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-i", audio_path, "-map", "0:v", "-map", "1:a",
           "-vf", "vignette=angle=PI/5,noise=alls=3:allf=t", "-c:v", "libx264", "-preset", str(cfg.get("video.preset")),
           "-crf", str(cfg.get("video.crf")), "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", "-shortest", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    cur_idx, cur_obj = -1, None
    prev_last: np.ndarray | None = None
    last_frame: np.ndarray | None = None
    try:
        for fi in range(nframes):
            t = fi / FPS
            si = next((i for i, s in enumerate(scenes) if s["start"] <= t < s["end"]), len(scenes) - 1)
            sc = scenes[si]
            if si != cur_idx:
                prev_last = last_frame
                cur_obj = VideoScene(sc, W, H, FPS) if sc["type"] == "video" else ImageScene(sc, W, H)
                cur_idx = si
            lt = t - sc["start"]
            fr = cur_obj.frame(lt)
            fr = np.ascontiguousarray(fr)
            last_frame = fr
            if lt < TR:
                fr = _apply_transition(sc.get("transition_in", "cut"), fr, prev_last, lt / TR, W)
            else:
                fr = fr.copy()
            # título de impacto / card tipográfico
            if sc.get("text"):
                if si not in headline_cache:
                    if sc["type"] == "title_card":
                        headline_cache[si] = T.text_rgba(sc["text"], "anton", SAFE["title_size"], fill=T.YELLOW, stroke=6, shadow=10)
                    else:
                        headline_cache[si] = T.boxed_text(sc["text"], "anton", SAFE["headline_size"], rotate=-2.0)
                    maxw = SAFE["title_maxw"] if sc["type"] == "title_card" else SAFE["headline_maxw"]   # área segura
                    if headline_cache[si].shape[1] > maxw:
                        headline_cache[si] = T.scale_rgba(headline_cache[si], maxw / headline_cache[si].shape[1])
                img = headline_cache[si]
                pop = ease_out_back(lt / 0.28)
                fade = min(1.0, (sc["end"] - t) / 0.15)
                im2 = T.scale_rgba(img, 0.6 + 0.4 * pop)
                cy = SAFE["title_cy"] if sc["type"] == "title_card" else SAFE["headline_y"] + img.shape[0] // 2
                T.blend(fr, im2, W // 2 - im2.shape[1] // 2, cy - im2.shape[0] // 2, opacity=fade)
            if badge is not None:
                T.blend(fr, badge, 48, SAFE["top_y"])
            if credit is not None:
                T.blend(fr, credit, 52, SAFE["top_y"] + (badge.shape[0] + 8 if badge is not None else 0))
            # barra de progresso
            pw = int(W * (t / duration))
            fr[0:8, :pw] = (10, 214, 255)
            captions.draw(fr, t)
            proc.stdin.write(fr.tobytes())
        proc.stdin.close()
        rc = proc.wait()
    except BrokenPipeError:
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg retornou {rc}")
    log.info("Renderizado %s (%d quadros)", out_path.name, nframes)
    return out_path
