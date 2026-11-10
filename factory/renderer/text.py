"""Renderização de texto (Pillow -> RGBA numpy) com contorno, sombra e caixas."""
from __future__ import annotations

from functools import lru_cache

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from factory.config import ROOT

FONT_DIR = ROOT / "assets" / "fonts"
YELLOW = (255, 214, 10)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


@lru_cache(maxsize=32)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    if name == "anton":
        return ImageFont.truetype(str(FONT_DIR / "Anton-Regular.ttf"), size)
    f = ImageFont.truetype(str(FONT_DIR / "Montserrat-Variable.ttf"), size)
    f.set_variation_by_name({"black": "Black", "bold": "Bold", "semibold": "SemiBold", "extrabold": "ExtraBold"}.get(name, "Black"))
    return f


def text_rgba(text: str, fname: str, size: int, fill=WHITE, stroke: int = 0, stroke_fill=BLACK,
              shadow: int = 0, pad: int = 12) -> np.ndarray:
    f = font(fname, size)
    l, t, r, b = f.getbbox(text, stroke_width=stroke)
    w, h = r - l + 2 * pad + shadow, b - t + 2 * pad + shadow
    img = Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
    if shadow:
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((pad - l + shadow, pad - t + shadow), text, font=f, fill=(0, 0, 0, 170),
                                stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(shadow * 0.6)))
    ImageDraw.Draw(img).text((pad - l, pad - t), text, font=f, fill=fill + (255,), stroke_width=stroke,
                             stroke_fill=stroke_fill + (255,))
    return np.array(img)


def caption_rgba(words: list[str], active: int, size: int = 76, maxw: int = 880, stroke: int = 8,
                 shadow: int = 5, line_gap: int = 6) -> np.ndarray:
    """Legenda com todas as palavras na MESMA linha de base (acentos/descendentes não desalinham)."""
    f = font("black", size)
    space = f.getlength(" ")
    ascent, descent = f.getmetrics()
    lines: list[list[int]] = [[]]
    width = 0.0
    for i, w in enumerate(words):
        wl = f.getlength(w)
        if lines[-1] and width + space + wl > maxw:
            lines.append([])
            width = 0.0
        width += wl + (space if lines[-1] else 0)
        lines[-1].append(i)
    line_h = ascent + descent + line_gap
    pad = stroke + shadow + 4
    widths = [sum(f.getlength(words[i]) for i in ln) + space * (len(ln) - 1) for ln in lines]
    W = int(max(widths) + 2 * pad)
    H = int(line_h * len(lines) + 2 * pad)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d, ds = ImageDraw.Draw(img), ImageDraw.Draw(sh)
    for li, ln in enumerate(lines):
        x = (W - widths[li]) / 2
        base = pad + ascent + li * line_h
        for i in ln:
            fill = YELLOW if i == active else WHITE
            ds.text((x + shadow, base + shadow), words[i], font=f, anchor="ls", fill=(0, 0, 0, 170),
                    stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
            d.text((x, base), words[i], font=f, anchor="ls", fill=fill + (255,), stroke_width=stroke,
                   stroke_fill=BLACK + (255,))
            x += f.getlength(words[i]) + space
    out = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(shadow * 0.6)), img)
    return np.array(out)


def boxed_text(text: str, fname: str, size: int, fg=BLACK, bg=YELLOW, padx: int = 30, pady: int = 16,
               radius: int = 18, rotate: float = 0.0) -> np.ndarray:
    f = font(fname, size)
    l, t, r, b = f.getbbox(text)
    w, h = r - l + 2 * padx, b - t + 2 * pady
    img = Image.new("RGBA", (w + 40, h + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((24, 26, 24 + w, 26 + h), radius, fill=(0, 0, 0, 120))  # sombra
    d.rounded_rectangle((18, 18, 18 + w, 18 + h), radius, fill=bg + (255,))
    d.text((18 + padx - l, 18 + pady - t), text, font=f, fill=fg + (255,))
    if rotate:
        img = img.rotate(rotate, resample=Image.BICUBIC, expand=True)
    return np.array(img)


def chip(text: str, color: tuple[int, int, int], size: int = 30) -> np.ndarray:
    f = font("bold", size)
    l, t, r, b = f.getbbox(text)
    w, h = r - l + 64, b - t + 24
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=(10, 10, 14, 190), outline=color + (255,), width=3)
    d.ellipse((18, h // 2 - 7, 32, h // 2 + 7), fill=color + (255,))
    d.text((42 - l, 12 - t), text, font=f, fill=(255, 255, 255, 255))
    return np.array(img)


def blend(dst: np.ndarray, rgba: np.ndarray, x: int, y: int, opacity: float = 1.0) -> None:
    """Alpha-blend de um RGBA (RGB) sobre frame BGR, in-place, recortando nas bordas."""
    if opacity <= 0.01:
        return
    H, W = dst.shape[:2]
    h, w = rgba.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x0 >= x1 or y0 >= y1:
        return
    src = rgba[y0 - y:y1 - y, x0 - x:x1 - x]
    a = (src[:, :, 3:4].astype(np.float32) / 255.0) * opacity
    roi = dst[y0:y1, x0:x1].astype(np.float32)
    rgb = src[:, :, 2::-1].astype(np.float32)  # RGB -> BGR
    dst[y0:y1, x0:x1] = (rgb * a + roi * (1 - a)).astype(np.uint8)


def scale_rgba(rgba: np.ndarray, s: float) -> np.ndarray:
    if abs(s - 1.0) < 0.01:
        return rgba
    h, w = rgba.shape[:2]
    return cv2.resize(rgba, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_LINEAR)
