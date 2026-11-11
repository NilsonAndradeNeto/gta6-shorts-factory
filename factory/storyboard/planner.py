"""Storyboard: cada trecho da narração recebe o material visual que corresponde a ele.

Regras de sincronia semântica:
  - Se a frase cita um personagem/local oficial, a cena PRECISA mostrar um asset oficial
    marcado com essa entidade (pastas do kit oficial são a fonte das marcações).
  - Se a frase não cita entidade: usa a imagem oficial da própria notícia (Newswire),
    depois assets das entidades da pauta e, por fim, um card tipográfico honesto
    (dados/números em destaque) sobre imagem oficial desfocada — nunca imagem aleatória.
  - Nenhuma imagem de outro jogo, nenhuma imagem gerada por IA apresentada como real.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

from factory.assets.catalog import ALLOWED_LICENSE_PREFIXES, abs_path, build_catalog, fetch_news_image, load_tags
from factory.database.db import DB
from factory.assets.steam import assets_for as steam_assets, game_key
from factory.entities import BY_ID, find_entities
from factory.textutil import norm
from factory.log import get_logger
from factory.textutil import keywords

log = get_logger("storyboard")

MAX_SCENE = 3.4      # segundos; acima disso a frase é dividida em mais cortes
MIN_SCENE = 1.1
# paisagem em tela vertical só tem folga horizontal; retrato só vertical
EFFECTS_LAND = ["pan_right", "zoom_in", "pan_left", "zoom_out"]
EFFECTS_PORT = ["zoom_in", "pan_down", "zoom_out", "pan_up"]


MONTHS = "janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro"
UNITS = r"%|GB|TB|FPS|fps|dólares|reais|milhões|bilhões|mil|faixas|horas|dias|anos|músicas|personagens"


def number_label(text: str) -> str:
    """Rótulo de dado com unidade ('212 GB', '10 DE NOVEMBRO'); ignora números de nomes (PlayStation 5, GTA 6)."""
    t = re.sub(r"\b(PlayStation|PS|GTA|Series|Xbox|Grand Theft Auto)\s?(\d+|VI|X\|S)\b", " ", text, flags=re.I)
    m = re.search(rf"(\d[\d.,]*\d|\d)(?:\s*({UNITS})\b)?(\s+de\s+({MONTHS}))?", t, flags=re.I)
    if not m:
        return ""
    label = m.group(1)
    if m.group(2):
        label += " " + m.group(2)
    if m.group(4):
        label += " de " + m.group(4)
    return label.upper()


def generated_background(i: int) -> dict[str, Any]:
    """Fundo em degradê gerado pelo sistema para cards tipográficos sem imagem do jogo."""
    import numpy as np
    from PIL import Image
    from factory.config import settings
    palettes = [((110, 40, 170), (30, 120, 210)), ((200, 50, 90), (70, 50, 170)), ((30, 140, 150), (140, 50, 200))]
    c1, c2 = palettes[i % len(palettes)]
    f = settings().path("cache_dir") / f"bg_gradient_{i % len(palettes)}.jpg"
    if not f.exists():
        h, w = 1920, 1920
        y = np.linspace(0, 1, h)[:, None, None]
        x = np.linspace(0, 1, w)[None, :, None]
        t = (0.65 * y + 0.35 * x)
        img = (np.array(c1) * (1 - t) + np.array(c2) * t + np.random.default_rng(i).normal(0, 6, (h, w, 3)))
        Image.fromarray(img.clip(0, 255).astype("uint8")).save(f, quality=92)
    return {"id": f"generated/gradient{i % len(palettes)}", "path": str(f), "kind": "screenshot", "entities": [],
            "w": 1920, "h": 1920, "orientation": "landscape", "source_url": "", "credit": None,
            "license": "Gerado pelo sistema (fundo em degradê)", "title": "fundo", "logo": False}


def _recent_asset_use(db: DB, n_videos: int = 10) -> dict[str, int]:
    rows = db.q("SELECT a.asset_id, COUNT(*) c FROM assets_used a WHERE a.video_id IN "
                "(SELECT id FROM videos ORDER BY created_at DESC LIMIT ?) GROUP BY a.asset_id", n_videos)
    return {r["asset_id"]: r["c"] for r in rows}


def _split_sentence(sent: dict[str, Any], words: list[dict[str, Any]], max_scene: float = MAX_SCENE,
                    target: float = 2.6) -> list[tuple[float, float, str]]:
    dur = sent["end"] - sent["start"]
    if dur <= max_scene or not words:
        return [(sent["start"], sent["end"], sent["text"])]
    n = max(2, round(dur / target))
    target = dur / n
    parts, cur, cur_start = [], [], sent["start"]
    for w in words:
        cur.append(w)
        if len(parts) < n - 1 and w["end"] - cur_start >= target:
            parts.append((cur_start, w["end"], " ".join(x["text"] for x in cur)))
            cur, cur_start = [], w["end"]
    if cur:
        parts.append((cur_start, sent["end"], " ".join(x["text"] for x in cur)))
    # funde pedaços curtos demais
    merged: list[tuple[float, float, str]] = []
    for p in parts:
        if merged and (p[1] - p[0] < MIN_SCENE):
            a = merged.pop()
            merged.append((a[0], p[1], a[2] + " " + p[2]))
        else:
            merged.append(p)
    return merged


class Matcher:
    def __init__(self, db: DB, topic: dict[str, Any], workdir: Path, seed: str):
        self.catalog = [a for a in build_catalog() if a["license"].startswith(ALLOWED_LICENSE_PREFIXES)]
        self.tags = load_tags()
        self.recent = _recent_asset_use(db)
        self.used: dict[str, int] = {}
        self.rnd = random.Random(seed)
        raw_ents = json.loads(topic.get("entities") or "[]")
        self.games: dict[str, str] = {}          # chave normalizada -> nome original (para buscar na Steam)
        for e in raw_ents:
            if str(e).startswith("game:"):
                self.add_game(str(e)[5:])
        self.topic_ents = [e if e in BY_ID else game_key(e) for e in raw_ents]
        # arte geral do GTA VI só serve de contexto para pautas sobre GTA
        self.gta_topic = any(e in BY_ID for e in raw_ents) or "gta" in norm(topic.get("title", ""))
        self.news_assets: list[dict[str, Any]] = []
        for ev in json.loads(topic["evidence"]):
            if ev.get("tier") == "official" and ev.get("image_url"):
                a = fetch_news_image(ev["image_url"], workdir / "news_images")
                if a:
                    a["entities"] = list(self.topic_ents)
                    self.news_assets.append(a)

    def _kw_score(self, asset: dict[str, Any], intent_kw: set[str]) -> float:
        t = self.tags.get(asset["id"])
        if not t or not intent_kw:
            return 0.0
        words = keywords(t.get("description", "") + " " + " ".join(t.get("tags", [])))
        return min(20.0, 5.0 * len(words & intent_kw))

    def add_game(self, name: str) -> str:
        """Carrega (uma vez) as imagens de divulgação do jogo na Steam e as adiciona ao acervo."""
        key = game_key(name)
        if key not in self.games:
            self.games[key] = name
            try:
                found = steam_assets(name)
            except Exception as e:   # Steam fora do ar não derruba o vídeo
                log.warning("Steam indisponível para %s: %s", name, e)
                found = []
            for a in found:
                if key not in a["entities"]:
                    a["entities"] = a["entities"] + [key]
            self.catalog.extend(found)
        return key

    def best(self, ents: list[str], intent: str, role: str, allow_video: bool = True) -> tuple[dict[str, Any] | None, float, str]:
        intent_kw = keywords(intent)
        primary = ents[0] if ents else None
        cands: list[tuple[float, dict[str, Any], float, str]] = []
        pool = self.catalog + self.news_assets
        for a in pool:
            if a["kind"] == "video" and (not allow_video or not a.get("shots")):
                continue
            aents = set(a["entities"])
            if primary:
                if primary not in aents:
                    continue
                allm = all(e in aents for e in ents)
                score = 50.0 if allm else 35.0
                conf = 0.95 if allm else 0.8
                reason = f"asset oficial marcado com {', '.join(ents if allm else [primary])}"
                score += 10.0 / max(1, len(aents))           # mais específico = melhor
                if primary == "gta6":                         # "GTA 6" é genérico: arte geral = contexto
                    conf, reason = 0.6, "contexto geral: arte oficial de GTA VI"
            else:
                if a in self.news_assets:
                    score, conf, reason = 45.0, 0.8, "imagem oficial publicada junto da notícia"
                elif self.topic_ents and set(self.topic_ents) & aents and \
                        (aents - {"leonida", "gta6"}) <= set(self.topic_ents):
                    # contexto só com assets cujo lugar/personagem específico pertence à pauta
                    # (evita mostrar, p.ex., o cartão-postal de Ambrosia quando a fala é sobre Miami)
                    score, conf, reason = 30.0, 0.6, f"contexto: entidades da pauta ({', '.join(set(self.topic_ents) & aents)})"
                elif self.gta_topic and ("gta6" in aents or a.get("logo")):
                    score, conf, reason = 20.0, 0.5, "contexto geral: arte oficial de GTA VI"
                else:
                    continue
            if a.get("logo") and role not in ("cta", "reveal", "hook"):
                score -= 15
            if primary and primary in BY_ID and BY_ID[primary].kind == "character":
                # personagem precisa aparecer: prioriza imagens com rosto detectado
                score += 8 if a.get("faces") else (-6 if a["kind"] != "video" else 0)
            score += self._kw_score(a, intent_kw)
            if a["kind"] == "video":
                score += 8 if role in ("hook", "reveal") else 3
            if a["orientation"] == "portrait":
                score += 5
            score -= 40 * self.used.get(a["id"], 0)
            score -= 6 * self.recent.get(a["id"], 0)
            score += self.rnd.random() * 6                   # variedade entre vídeos
            cands.append((score, a, conf, reason))
        if not cands:
            return None, 0.0, "nenhum asset oficial corresponde"
        cands.sort(key=lambda c: c[0], reverse=True)
        s, a, conf, reason = cands[0]
        if self.used.get(a["id"], 0) >= 2:
            # acervo pequeno: reaproveita a imagem CERTA menos usada (outro enquadramento/movimento)
            # em vez de cair num card vazio; o limite máximo evita repetição excessiva
            least = min(cands, key=lambda c: (self.used.get(c[1]["id"], 0), -c[0]))
            if self.used.get(least[1]["id"], 0) >= 4:
                return None, 0.0, "assets correspondentes esgotados (evitando repetição)"
            s, a, conf, reason = least
            reason += " (reaproveitada: acervo pequeno)"
        self.used[a["id"]] = self.used.get(a["id"], 0) + 1
        return a, conf, reason


def plan(db: DB, video_id: str, script: dict[str, Any], voice: dict[str, Any], words: list[dict[str, Any]],
         topic: dict[str, Any], workdir: Path, max_scene: float = MAX_SCENE, target_scene: float = 2.6,
         matcher: "Matcher | None" = None, record_usage: bool = True, prefer_images: bool = False) -> dict[str, Any]:
    m = matcher or Matcher(db, topic, workdir, seed=video_id)
    scenes: list[dict[str, Any]] = []
    eff_i = 0
    seg_first_scene: set[int] = set()
    for si_idx, sent in enumerate(voice["sentences"]):
        seg = script["segments"][sent["seg"]]
        vis = seg.get("visual") if isinstance(seg.get("visual"), dict) else {}
        sent_ents = find_entities(sent["text"])
        seg_ents = []
        for e in (vis.get("entities") or []) if isinstance(vis.get("entities"), list) else []:
            if isinstance(e, dict):   # o LLM às vezes devolve {"id": ...} / {"name": ...}
                e = e.get("id") or e.get("name") or ""
            if not isinstance(e, str) or not e:
                continue
            if e in BY_ID:
                seg_ents.append(e)
            elif str(e).startswith("game:") and len(str(e)) > 6:
                seg_ents.append(m.add_game(str(e)[5:]))
        # nomes de jogos da pauta citados na frase também valem como entidade
        sent_ents += [k for k, name in m.games.items() if norm(name) and f" {norm(name)} " in f" {norm(sent['text'])} "]
        # entidades citadas NA FRASE têm prioridade; senão, as declaradas para o segmento
        # "GTA 6" sozinho não é específico: herda as entidades do segmento
        specific = [e for e in sent_ents if e != "gta6"]
        ents = specific or [e for e in seg_ents if e != "gta6"] or sent_ents
        if "jason_lucia" in ents and ents[0] != "jason_lucia" and {"jason", "lucia"} <= set(ents):
            ents = ["jason_lucia"] + [e for e in ents if e not in ("jason", "lucia", "jason_lucia")]
        sw = [w for w in words if sent["start"] - 0.01 <= w["start"] < sent["end"] + 0.01]
        parts = _split_sentence(sent, sw, max_scene, target_scene)
        for pi, (st, en, ptxt) in enumerate(parts):
            if si_idx == 0 and pi == 0:
                st = 0.0
            role = seg.get("role", "body")
            num_label = number_label(ptxt)
            has_number = bool(num_label)
            asset, conf, reason = m.best(ents, vis.get("intent", "") + " " + ptxt, role, allow_video=True)
            stype = "image"
            # vídeo longo: card tipográfico só quando não há NENHUMA imagem do assunto (o dado vai por cima da imagem)
            if asset is None or (not prefer_images and not ents and has_number and conf < 0.8):
                # card tipográfico honesto: destaca o dado sobre imagem oficial desfocada
                bg, _, _ = m.best([], "capa oficial", "cta", allow_video=False)
                if bg:
                    m.used[bg["id"]] = m.used.get(bg["id"], 1) - 1  # fundo desfocado não conta como repetição
                else:
                    bg = generated_background(len(scenes))
                stype, asset, conf = "title_card", bg, 0.7 if has_number else 0.5
                reason = "dado/afirmação sem imagem específica: card tipográfico sobre arte oficial desfocada"
            elif asset["kind"] == "video":
                stype = "video"
            text_on = ""
            if sent["seg"] not in seg_first_scene:
                text_on = (vis.get("on_screen_text") or "").strip()[:28]
                seg_first_scene.add(sent["seg"])
            if stype == "title_card":
                text_on = num_label or text_on   # sem número, só repete o título no 1º card do segmento
            elif prefer_images and num_label and not text_on:
                text_on = num_label              # vídeo longo: o dado aparece em destaque sobre a imagem oficial
            orient = (asset or {}).get("orientation", "landscape")
            effects = EFFECTS_PORT if orient == "portrait" else EFFECTS_LAND
            effect = effects[eff_i % len(effects)]
            eff_i += 1
            tag0 = m.tags.get(asset["id"], {}) if asset else {}
            focus = tag0.get("focus") or (asset or {}).get("focus")
            is_char = any(BY_ID[e].kind == "character" for e in ents if e in BY_ID)
            if orient == "portrait" or stype != "image":
                layout = "fullbleed"
            elif focus is None and is_char:
                # sem ponto focal confiável o recorte 9:16 pode cortar o personagem: mostra a imagem inteira
                layout, effect = "card", ("zoom_in" if eff_i % 2 else "zoom_out")
            elif effect.startswith("pan"):
                layout = "fullbleed"
            else:
                layout = "card"
            if pi > 0:
                trans = "cut"
            elif role == "reveal":
                trans = "flash"
            elif si_idx == 0:
                trans = "none"
            else:
                trans = "whip" if eff_i % 2 else "zoom"
            clip = None
            if stype == "video" and asset:
                shots = asset["shots"]
                shot = shots[m.rnd.randrange(len(shots))]
                clip = {"in": shot[0], "out": shot[1]}
            scenes.append({
                "i": len(scenes), "start": round(st, 3), "end": round(en, 3), "narration": ptxt, "seg": sent["seg"],
                "role": role, "type": stype, "description": vis.get("intent", ""),
                "main_entity": ((BY_ID[ents[0]].name if ents[0] in BY_ID else m.games.get(ents[0], ents[0])) if ents else None),
                "entities": ents,
                "asset_id": asset["id"] if asset else None, "asset_path": str(abs_path(asset)) if asset else None,
                "source_url": asset["source_url"] if asset else None, "license": asset["license"] if asset else None,
                "credit": asset["credit"] if asset else None, "effect": effect, "layout": layout,
                "focus": focus or [0.5, 0.45], "focus_known": focus is not None, "clip": clip, "text": text_on,
                "transition_in": trans, "match_confidence": round(conf, 2), "match_reason": reason,
            })
    # a última cena vai até o fim do áudio
    if scenes:
        scenes[-1]["end"] = round(voice["duration"], 3)
        for a, b in zip(scenes, scenes[1:]):
            a["end"] = b["start"]
    for s in scenes if record_usage else []:
        if s["asset_id"]:
            db.x("INSERT OR IGNORE INTO assets_used(video_id,asset_id,start,end) VALUES (?,?,?,?)",
                 video_id, s["asset_id"], s["start"], s["end"])
    conf = [s["match_confidence"] for s in scenes]
    sb = {"video_id": video_id, "duration": voice["duration"], "scenes": scenes,
          "avg_confidence": round(sum(conf) / max(1, len(conf)), 3),
          "status_badge": {"CONFIRMADO": "OFICIAL", "REPORTADO": "REPORTADO", "TEORIA": "TEORIA"}.get(
              topic["confirmation"], "")}
    log.info("Storyboard: %d cenas, confiança média %.2f", len(scenes), sb["avg_confidence"])
    return sb
