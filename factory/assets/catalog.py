"""Catálogo de assets visuais com rastreabilidade de fonte e licença.

Fonte principal: kit oficial de mídia de GTA VI (rockstargames.com/VI/media), oferecido
pela Rockstar com o texto "Download and share official videos, screenshots, and more".
Os arquivos vêm organizados por personagem/local, o que dá tags semânticas confiáveis.
Uso sujeito à política da Rockstar (ver docs/LEGAL.md): crédito sempre exibido.
"""
from __future__ import annotations

import json
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from factory.config import settings
from factory.entities import ENTITIES
from factory.log import get_logger

log = get_logger("assets")

KIT_BASE = "https://media-rockstargames-com.akamaized.net/VI/downloads"
KIT_ZIPS = {
    "screenshots": f"{KIT_BASE}/screenshots/GTAVI_Screenshots.zip",
    "artwork": f"{KIT_BASE}/artwork_wallpapers/GTAVI_Artwork_Wallpapers.zip",
    "videos": f"{KIT_BASE}/videos/GTAVI_Videos.zip",
    "ultimate": f"{KIT_BASE}/screenshots/GTAVI_Ultimate_Edition_Benefits.zip",
    "vintage": f"{KIT_BASE}/screenshots/GTAVI_Vintage_Vice_City_Pack.zip",
}
LICENSE = ("Rockstar Games — kit oficial de mídia de GTA VI (rockstargames.com/VI/media, "
           "'Download and share'). Uso editorial com crédito; sujeito à política de conteúdo da Rockstar.")
CREDIT = "Imagens: Rockstar Games"
ALLOWED_LICENSE_PREFIXES = ("Rockstar Games —", "Gerado pelo sistema", "Divulgação oficial —")


def _cache() -> Path:
    d = settings().path("cache_dir") / "official"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_kit() -> dict[str, Path]:
    out = {}
    for key, url in KIT_ZIPS.items():
        dest = _cache() / Path(url).name
        if not dest.exists() or dest.stat().st_size == 0:
            log.info("Baixando kit oficial: %s", url)
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(".part")
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
                tmp.replace(dest)
        out[key] = dest
    return out


def _entities_for(path_str: str) -> list[str]:
    s = path_str.replace(" ", "_")
    ents = []
    for e in ENTITIES:
        if any(k.lower() in s.lower() for k in e.asset_keys):
            ents.append(e.id)
    # pastas específicas não devem herdar entidades "guarda-chuva" demais
    if "Jason_Duval" in s:
        ents = [e for e in ents if e not in ("lucia", "jason_lucia")]
    if "Lucia_Caminos" in s:
        ents = [e for e in ents if e not in ("jason", "jason_lucia")]
    if "VINTAGE" in s.upper():
        ents = [e for e in ents if e not in ("vice_city", "leonida")]
    return ents


def _shots(video: Path) -> list[list[float]]:
    """Detecta cortes de cena para nunca usar um trecho que atravessa um corte."""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                               capture_output=True, text=True, check=True).stdout.strip() or 0)
    res = subprocess.run(["ffmpeg", "-v", "info", "-i", str(video), "-vf", "select='gt(scene,0.32)',showinfo", "-an",
                          "-f", "null", "-"], capture_output=True, text=True)
    cuts = sorted(float(m) for m in re.findall(r"pts_time:([\d.]+)", res.stderr))
    bounds = [0.0] + [c for c in cuts if 0.3 < c < dur - 0.3] + [dur]
    shots = [[round(a, 3), round(b, 3)] for a, b in zip(bounds, bounds[1:]) if b - a >= 0.9]
    return [s for s in shots if _shot_ok(video, s)]


def _shot_ok(video: Path, shot: list[float]) -> bool:
    """Descarta trechos que são cards de texto (ex.: classificação ESRB) ou tela escura/logo."""
    import numpy as np
    for frac in (0.2, 0.5, 0.8):
        t = shot[0] + (shot[1] - shot[0]) * frac
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                              "-vf", "scale=160:90", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True).stdout
        if len(raw) < 160 * 90 * 3:
            return False
        px = np.frombuffer(raw[:160 * 90 * 3], np.uint8).reshape(-1, 3)
        if (px.min(axis=1) > 225).mean() > 0.25 or px.mean() < 18 or float(px.std()) < 20:
            return False
    return True


# o repositório opencv_zoo guarda modelos em Git LFS: a URL "raw" devolveria só o ponteiro
YUNET_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
_detector = None


def _face_detector():
    """Detector facial YuNet (OpenCV Zoo, ~230 KB, roda no CPU). None se indisponível."""
    global _detector
    if _detector is None:
        import shutil
        import tempfile
        f = _cache() / "face_detection_yunet_2023mar.onnx"
        try:
            if not f.exists() or f.stat().st_size < 100_000:
                r = requests.get(YUNET_URL, timeout=60)
                r.raise_for_status()
                if len(r.content) < 100_000:
                    raise ValueError("download do modelo YuNet inválido")
                f.write_bytes(r.content)
            ascii_copy = Path(tempfile.gettempdir()) / f.name   # OpenCV não lê caminhos com acento (Windows)
            if not ascii_copy.exists():
                shutil.copy(f, ascii_copy)
            import cv2
            _detector = cv2.FaceDetectorYN.create(str(ascii_copy), "", (320, 320), 0.75, 0.3, 50)
        except Exception as e:
            log.warning("Detector facial indisponível: %s", e)
            _detector = False
    return _detector or None


def detect_focus(path: Path) -> dict[str, Any]:
    """Ponto focal (centro dos rostos principais) em coordenadas 0..1."""
    import cv2
    import numpy as np
    det = _face_detector()
    if det is None:
        return {}
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return {}
    h, w = img.shape[:2]
    s = 960 / max(w, h)
    small = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    det.setInputSize((small.shape[1], small.shape[0]))
    _, faces = det.detect(small)
    if faces is None or len(faces) == 0:
        return {"faces": 0}
    faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
    big = faces[0][2] * faces[0][3]
    main = [f for f in faces if f[2] * f[3] >= 0.25 * big][:3]   # ignora rostos minúsculos ao fundo
    x0 = min(f[0] for f in main)
    x1 = max(f[0] + f[2] for f in main)
    y0 = min(f[1] for f in main)
    y1 = max(f[1] + f[3] for f in main)
    W, H = small.shape[1], small.shape[0]
    fx = (x0 + x1) / 2 / W
    fy = min(0.85, ((y0 + y1) / 2 + (y1 - y0) * 0.6) / H)   # um pouco abaixo do rosto (rosto + tronco)
    return {"faces": len(main), "focus": [round(float(fx), 3), round(float(fy), 3)],
            "face_span": round(float((x1 - x0) / W), 3)}


def _proxy(src: Path, dst: Path) -> None:
    """Proxy 1080p H.264 sem áudio (os clipes oficiais vêm em 4K)."""
    if dst.exists() and dst.stat().st_size > 0:
        return
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-an", "-vf", "scale=-2:1080:flags=lanczos",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(dst)], check=True)


def build_catalog(force: bool = False) -> list[dict[str, Any]]:
    cat_path = _cache() / "catalog.json"
    if cat_path.exists() and not force:
        return json.loads(cat_path.read_text(encoding="utf-8"))
    zips = download_kit()
    ex = _cache() / "extracted"
    items: list[dict[str, Any]] = []
    for key, zp in zips.items():
        target = ex / key
        with zipfile.ZipFile(zp) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            for n in names:
                if not (target / n).exists():
                    z.extract(n, target)
        for n in names:
            p = target / n
            low = n.lower()
            if low.endswith(".mp4"):
                proxy = target / (Path(n).stem + "_1080.mp4")
                _proxy(p, proxy)
                shots = _shots(proxy)
                items.append({"id": f"{key}/{Path(n).stem}", "path": str(proxy.relative_to(_cache())), "kind": "video",
                              "src": str(p.relative_to(_cache())),
                              "entities": _entities_for(n), "w": 1920, "h": 1080, "orientation": "landscape",
                              "shots": shots, "source_url": KIT_ZIPS[key], "license": LICENSE, "credit": CREDIT,
                              "title": Path(n).stem.replace("_", " "), "logo": "Cover_Art" in n})
                continue
            if not low.endswith((".jpg", ".jpeg", ".png")):
                continue
            if key == "artwork":
                # um formato por arte: preferimos 'portrait' (9:16), depois 'phone'
                stem = Path(n).stem
                if not re.search(r"_(portrait|phone(_A)?)$", stem):
                    continue
                folder = str(Path(n).parent)
                if stem.endswith("_phone") or stem.endswith("_phone_A"):
                    if any(Path(x).parent == Path(folder) and Path(x).stem.endswith("_portrait") for x in names):
                        continue
            with Image.open(p) as im:
                w, h = im.size
            items.append({"id": f"{key}/{Path(n).with_suffix('').as_posix()}", "path": str(p.relative_to(_cache())),
                          "kind": "artwork" if key == "artwork" else "screenshot", "entities": _entities_for(n),
                          "w": w, "h": h, "orientation": "portrait" if h > w else "landscape",
                          "source_url": KIT_ZIPS[key], "license": LICENSE, "credit": CREDIT,
                          "title": Path(n).stem.replace("_", " "), "logo": "With_Logo" in n or "Cover_Art" in n,
                          "group": key, **detect_focus(p)})
    cat_path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Catálogo: %d assets (%d vídeos)", len(items), sum(1 for i in items if i["kind"] == "video"))
    return items


def abs_path(item: dict[str, Any]) -> Path:
    p = Path(item["path"])
    return p if p.is_absolute() else _cache() / p


def load_tags() -> dict[str, Any]:
    """Descrições visuais por asset (geradas por modelo de visão; ver assets/tagger.py)."""
    f = settings().path("data_dir") / "asset_tags.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {}


def fetch_news_image(url: str, dest_dir: Path) -> dict[str, Any] | None:
    """Imagem oficial associada a um post da Newswire (somente domínio oficial da Rockstar)."""
    if "media-rockstargames-com.akamaized.net" not in url and "rockstargames.com" not in url:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.]+", "_", url.split("/")[-1].split("?")[0])[-80:]
    dest = dest_dir / name
    if not dest.exists():
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
        except requests.RequestException as e:
            log.info("Falha ao baixar imagem oficial %s: %s", url, e)
            return None
    try:
        with Image.open(dest) as im:
            w, h = im.size
            thumb = im.convert("RGB").resize((160, 90))
    except Exception:
        return None
    if min(w, h) < 600:
        return None
    # descarta cards de texto (ex.: selo de classificação ESRB): muito fundo branco/liso
    import numpy as np
    px = np.asarray(thumb).reshape(-1, 3)
    if (px.min(axis=1) > 225).mean() > 0.25 or float(px.std()) < 25:
        log.info("Imagem oficial descartada (parece card de texto/classificação): %s", url)
        return None
    return {"id": f"newswire/{name}", "path": str(dest), "kind": "screenshot", "entities": [], "w": w, "h": h,
            "orientation": "portrait" if h > w else "landscape", "source_url": url,
            "license": "Rockstar Games — imagem oficial publicada na Rockstar Newswire. Uso editorial com crédito.",
            "credit": CREDIT, "title": "Imagem oficial da notícia", "logo": False, "group": "newswire"}
