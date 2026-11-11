"""Vídeo longo diário (16:9, ~8-11 min): "Games Hoje" — as notícias mais fortes do dia, uma por capítulo.

Cada capítulo é escrito e checado contra as evidências da SUA notícia (mesma checagem dos Shorts).
Abertura/encerramento só citam o que já está nos capítulos verificados (o juiz confere isso).
Produção: roteiro -> narração por bloco -> storyboard por bloco (imagens do jogo de cada notícia)
-> vinhetas entre capítulos -> mixagem -> render 1920x1080 -> QA -> thumbnail -> upload.
"""
from __future__ import annotations

import json
import subprocess
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from rapidfuzz import fuzz

from factory.config import settings
from factory.database.db import DB, State, now_iso
from factory.entities import ENTITIES
from factory.llm import LLM, LLMError
from factory.log import get_logger
from factory.textutil import norm

log = get_logger("longform")
BRT = ZoneInfo("America/Sao_Paulo")
GAP = 1.6   # segundos de vinheta entre capítulos

ENTITY_IDS = ", ".join(f"{e.id} ({e.name})" for e in ENTITIES)

CHAPTER_SYSTEM = f"""Você escreve UM capítulo de um vídeo longo brasileiro do YouTube chamado "Games Hoje", que resume as
notícias de games do dia com humor. Tom: criador humano brasileiro conversando com o público (gírias leves, ironia,
reações), nunca texto de jornal ou de IA. Explique a notícia com mais contexto que num Short, com piadas no meio.

FORMATO DO CAPÍTULO (150 a 220 palavras faladas):
- abertura: 1 frase-gancho que faz o espectador querer ouvir a notícia;
- contexto e desenvolvimento: o que aconteceu, por que importa, detalhes das EVIDÊNCIAS;
- 1 ou 2 momentos de humor ("tipo": "humor"), com o que for imaginário marcado ("imagina...", "e se...");
- fechamento curto com uma opinião bem-humorada.
Não cumprimente, não diga "no vídeo de hoje", não peça inscrição (isso fica no encerramento do vídeo).

REGRAS DE VERACIDADE (segmentos "fato"):
1. SOMENTE fatos das EVIDÊNCIAS. Cada frase "fato" com informação precisa de "claims" com "evidence" (ex.: "E1")
   e "quote" COPIADA LITERALMENTE da evidência (idioma original, 5 a 30 palavras).
2. "status": CONFIRMADO (só tier=official), REPORTADO (imprensa, com atribuição: "segundo a IGN"), TEORIA (comunidade).
3. Pessoas reais só em cenários claramente imaginários e inofensivos; nunca falas inventadas, crimes ou polêmicas.
4. Datas por extenso; preços "80 dólares".

VISUAL por segmento: "entities" com "game:<nome oficial do jogo>" ou IDs de GTA 6 ({ENTITY_IDS}); "intent"; "on_screen_text" (0-4 palavras).

Responda SOMENTE JSON:
{{"title_short":"até 45 caracteres","segments":[{{"role":"hook","tipo":"fato","text":"...","claims":[{{"text":"...","evidence":"E1","quote":"...","status":"CONFIRMADO"}}],"visual":{{"entities":[],"intent":"...","on_screen_text":""}}}}]}}"""

FRAME_SYSTEM = """Você cria a abertura, o encerramento e os metadados do episódio diário "Games Hoje" (YouTube, vídeo longo
brasileiro de notícias de games com humor). Use SOMENTE os capítulos fornecidos (já verificados): não acrescente fatos novos.
- "intro": 2 a 4 segmentos (60 a 90 palavras no total): gancho forte citando de forma provocativa 2 ou 3 notícias do episódio,
  apresentação rápida do "Games Hoje" e promessa do que vem. Tom humano, animado, brasileiro.
- "outro": 2 segmentos (40 a 70 palavras): pergunta para os comentários sobre as notícias + convite natural para se inscrever
  e voltar amanhã.
- "title": até 90 caracteres, chamativo e fiel (cite a notícia mais forte); sem hashtags.
- "thumbnail_text": 2 a 4 palavras em MAIÚSCULAS para a thumbnail (fiel ao conteúdo).
- "description": 2 a 3 frases resumindo o episódio.
- "hashtags": 3 a 5.
Cada segmento: {"role":"hook|context|cta","tipo":"humor","text":"...","claims":[],"visual":{"entities":[],"intent":"...","on_screen_text":""}}
Responda SOMENTE JSON: {"title":"...","thumbnail_text":"...","description":"...","hashtags":["#games"],"intro":[...],"outro":[...]}"""


# ----------------------------------------------------------------------------- pautas
SYNONYMS = {"preload": "preload", "carregamento": "preload", "download": "preload", "predownload": "preload",
            "antecipado": "preload", "vendas": "venda", "vende": "venda", "vendeu": "venda", "recorde": "recorde",
            "record": "recorde", "consoles": "console"}


GENERIC_WORDS = {"ps5", "ps4", "xbox", "series", "console", "playstation", "switch", "nintendo", "steam", "pc", "jogo",
                 "jogos", "games", "game", "novo", "nova", "chega", "ganha", "pre", "gta", "vi"}


def _canon(words: set[str]) -> set[str]:
    return {SYNONYMS.get(w, w) for w in words} - {"pre"}


def _has_visuals(t: dict[str, Any]) -> bool:
    from factory.assets.steam import find_app
    from factory.entities import BY_ID
    ents = json.loads(t.get("entities") or "[]")
    if any(e in BY_ID for e in ents) and "gta" in norm(t["title"]):
        return True
    return any(find_app(e[5:]) for e in ents if str(e).startswith("game:"))


def pick_stories(db: DB, n: int) -> list[dict[str, Any]]:
    """Notícias mais fortes das últimas 36h (inclui as já usadas nos Shorts), sem repetir assunto."""
    since = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
    rows = [dict(r) for r in db.q(
        "SELECT * FROM topics WHERE kind='news' AND confirmation IN ('CONFIRMADO','REPORTADO') "
        "AND state NOT IN ('REJECTED','DUPLICATE','STALE') AND created_at>=? ORDER BY score DESC LIMIT 80", since)]
    from factory.textutil import keywords
    chosen: list[dict[str, Any]] = []
    for t in rows:
        kw = _canon(keywords(t["title"]))
        dup = False
        for c in chosen:
            ckw = _canon(keywords(c["title"]))
            jacc = len(kw & ckw) / max(1, len(kw | ckw))
            # mesma notícia com outra manchete ("pré-carregamento" x "pré-download"): similaridade OU palavras em comum
            same_game = set(json.loads(t.get("entities") or "[]")) & set(json.loads(c.get("entities") or "[]"))
            shared_content = (kw & ckw) - GENERIC_WORDS
            if fuzz.token_set_ratio(norm(t["title"]), norm(c["title"])) >= 60 or jacc >= 0.3 or                     (same_game and shared_content):
                dup = True
                break
        if dup:
            continue
        # capítulo precisa de imagens oficiais do assunto (kit da Rockstar ou página do jogo na Steam);
        # no máximo 1 capítulo pode depender só de cards tipográficos
        if not _has_visuals(t):
            if sum(1 for c in chosen if not _has_visuals(c)) >= 1:
                continue
        chosen.append(t)
        if len(chosen) >= n:
            break
    return chosen


# ----------------------------------------------------------------------------- roteiro
def _validate_chapter(ch: dict[str, Any]) -> list[str]:
    probs = []
    segs = ch.get("segments")
    if not isinstance(segs, list) or len(segs) < 3:
        return ["capítulo precisa de pelo menos 3 segmentos"]
    for s in segs:
        if not isinstance(s, dict) or not s.get("text"):
            return ["segmento sem texto"]
        s.setdefault("claims", [])
        s.setdefault("visual", {"entities": [], "intent": "", "on_screen_text": ""})
        s["tipo"] = "humor" if str(s.get("tipo", "fato")).lower().startswith("humor") else "fato"
    fatos = [s for s in segs if s["tipo"] == "fato"]
    if len(fatos) < 2 or sum(len(s["claims"]) for s in fatos) < 2:
        probs.append("a notícia precisa de pelo menos 2 segmentos 'fato' com claims")
    wc = sum(len(s["text"].split()) for s in segs)
    if wc < 120:
        probs.append(f"capítulo curto demais ({wc} palavras); escreva 150 a 220")
    if wc > 260:
        probs.append(f"capítulo longo demais ({wc} palavras); máximo 220")
    if not ch.get("title_short"):
        probs.append("faltou title_short")
    return probs


def write_chapter(db: DB, llm: LLM, topic: dict[str, Any]) -> dict[str, Any] | None:
    from factory.scriptwriter.writer import build_user_prompt
    from factory.verification.factcheck import check
    evidence = json.loads(topic["evidence"])
    feedback = ""
    for attempt in range(3):
        user = build_user_prompt(topic, "exagero", [], feedback).replace("ESTILO DE GANCHO OBRIGATÓRIO", "ESTILO DE GANCHO SUGERIDO")
        try:
            ch = llm.json(CHAPTER_SYSTEM, user, purpose="long_chapter", max_tokens=4000)
        except LLMError as e:
            log.warning("Capítulo falhou no LLM: %s", e)
            continue
        if not isinstance(ch, dict):
            continue
        probs = _validate_chapter(ch)
        if probs:
            feedback = "\n".join(f"- {p}" for p in probs)
            continue
        ch.update(title=ch["title_short"], confirmation=topic["confirmation"], topic_id=topic["id"])
        fact = check(llm, ch, evidence)
        if fact["passed"]:
            ch["factcheck"] = {k: fact[k] for k in ("passed", "claims", "programmatic", "judge")}
            return ch
        feedback = "\n".join(f"- {p}" for p in (fact["programmatic"] + fact["judge"])[:10])
        log.info("Capítulo '%s' reprovado (tentativa %d): %s", topic["title"][:50], attempt + 1, feedback[:300])
    return None


def write_episode(db: DB, llm: LLM) -> dict[str, Any]:
    cfg = settings()
    want = int(cfg.get("longform.chapters", 7))
    chapters: list[dict[str, Any]] = []
    for t in pick_stories(db, want + 5):
        if len(chapters) >= want:
            break
        ch = write_chapter(db, llm, t)
        if ch:
            chapters.append(ch)
            log.info("Capítulo %d aprovado: %s", len(chapters), ch["title_short"])
    if len(chapters) < int(cfg.get("longform.min_chapters", 5)):
        raise RuntimeError(f"só {len(chapters)} capítulos passaram na checagem; episódio não será produzido")
    summary = "\n\n".join(f"CAPÍTULO {i + 1} — {c['title_short']}:\n" + " ".join(s["text"] for s in c["segments"])
                          for i, c in enumerate(chapters))
    frame: dict[str, Any] = {}
    from factory.verification.factcheck import llm_judge
    feedback = ""
    for attempt in range(3):
        frame = llm.json(FRAME_SYSTEM, f"CAPÍTULOS DO EPISÓDIO:\n{summary}" + (f"\n\nCORRIJA:\n{feedback}" if feedback else ""),
                         purpose="long_frame", max_tokens=3000)
        if not (isinstance(frame, dict) and frame.get("intro") and frame.get("outro") and frame.get("title")):
            feedback = "- faltaram intro/outro/title"
            continue
        for part in ("intro", "outro"):
            if isinstance(frame[part], str):
                frame[part] = [frame[part]]
            # o modelo às vezes devolve os trechos como texto simples: normaliza para o formato de segmento
            frame[part] = [x if isinstance(x, dict) else {"role": "hook" if part == "intro" else "cta", "text": str(x)}
                           for x in frame[part] if (x.get("text") if isinstance(x, dict) else str(x).strip())]
            for s in frame[part]:
                s.setdefault("claims", [])
                s.setdefault("visual", {"entities": [], "intent": "", "on_screen_text": ""})
                s["tipo"] = "humor"
        # o juiz confere se abertura/encerramento só usam o que está nos capítulos verificados
        ev = [{"id": f"E{i + 1}", "tier": "official", "source": f"capítulo {i + 1}", "text": " ".join(s["text"] for s in c["segments"])}
              for i, c in enumerate(chapters)]
        probe = {"title": frame["title"], "description": frame.get("description", ""),
                 "segments": frame["intro"] + frame["outro"]}
        try:
            issues, _ = llm_judge(llm, probe, ev)
        except LLMError as e:
            issues = [f"juiz indisponível: {e}"]
        if not issues:
            break
        feedback = "\n".join(f"- {i}" for i in issues[:8])
        log.info("Abertura/encerramento reprovados: %s", feedback[:300])
    else:
        raise RuntimeError("abertura/encerramento não passaram na checagem")
    return {"format": "long", "title": frame["title"][:95], "description": frame.get("description", ""),
            "hashtags": frame.get("hashtags") or ["#games"], "thumbnail_text": frame.get("thumbnail_text", "")[:40],
            "intro": {"segments": frame["intro"]}, "outro": {"segments": frame["outro"]}, "chapters": chapters,
            "hook_style": "games_hoje", "angle": "resumo diário"}


def blocks_of(script: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"kind": "intro", **script["intro"]}] + \
        [{"kind": "chapter", "n": i + 1, **c} for i, c in enumerate(script["chapters"])] + \
        [{"kind": "outro", **script["outro"]}]


# ----------------------------------------------------------------------------- áudio
def _wav_info(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() == 2:
            a = a.reshape(-1, 2).mean(axis=1).astype(np.int16)
    return a, sr


def synthesize_episode(script: dict[str, Any], workdir: Path) -> dict[str, Any]:
    from factory.voice.tts import synthesize_script
    pieces, sentences, block_meta = [], [], []
    sr_out, t = 48000, 0.0
    for bi, block in enumerate(blocks_of(script)):
        if block["kind"] == "chapter":
            pieces.append(np.zeros(int(GAP * sr_out), np.int16))     # espaço da vinheta "#n"
            block_meta.append({"bumper_start": round(t, 3), "n": block["n"], "title": block["title_short"]})
            t += GAP
        v = synthesize_script({"segments": block["segments"]}, workdir / f"voice_{bi:02d}")
        a, sr = _wav_info(v["wav"])
        if sr != sr_out:
            raise RuntimeError(f"taxa inesperada {sr}")
        for s in v["sentences"]:
            sentences.append({**s, "block": bi, "start": round(s["start"] + t, 3), "end": round(s["end"] + t, 3)})
        block_meta.append({"block": bi, "start": round(t, 3), "duration": v["duration"], "engine": v["engine"]})
        pieces.append(a)
        t += len(a) / sr_out
    audio = np.concatenate(pieces)
    out = workdir / "narration.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr_out)
        w.writeframes(audio.tobytes())
    return {"wav": str(out), "duration": round(len(audio) / sr_out, 3), "sentences": sentences, "blocks": block_meta,
            "engine": block_meta[1].get("engine") if len(block_meta) > 1 else ""}


# ----------------------------------------------------------------------------- storyboard
def plan_episode(db: DB, video_id: str, script: dict[str, Any], voice: dict[str, Any], workdir: Path) -> dict[str, Any]:
    from factory.storyboard.planner import Matcher, generated_background, plan
    from factory.subtitles.align import align, chunk_words
    scenes, chunks, ratios = [], [], []
    blocks = blocks_of(script)
    topics = {c["topic_id"]: dict(db.one("SELECT * FROM topics WHERE id=?", c["topic_id"])) for c in script["chapters"]}
    all_ents = sorted({e for t in topics.values() for e in json.loads(t.get("entities") or "[]")})
    frame_topic = {"id": 0, "title": script["title"], "evidence": "[]", "entities": json.dumps(all_ents),
                   "confirmation": "CONFIRMADO"}
    for meta in voice["blocks"]:
        if "bumper_start" in meta:
            bg = generated_background(meta["n"])
            scenes.append({"start": meta["bumper_start"], "end": meta["bumper_start"] + GAP, "narration": "",
                           "type": "title_card", "layout": "fullbleed", "asset_id": bg["id"], "asset_path": bg["path"],
                           "source_url": "", "license": bg["license"], "credit": None, "effect": "zoom_in",
                           "focus": [0.5, 0.5], "focus_known": False, "clip": None,
                           "text": f"#{meta['n']} {meta['title']}".upper()[:40], "transition_in": "flash",
                           "match_confidence": 0.7, "match_reason": "vinheta de capítulo", "entities": [], "role": "bumper",
                           "seg": -1, "description": "vinheta", "main_entity": None})
            continue
        bi = meta["block"]
        block = blocks[bi]
        topic = topics.get(block.get("topic_id"), frame_topic)
        bdir = workdir / f"voice_{bi:02d}"
        local = [{**s, "start": s["start"] - meta["start"], "end": s["end"] - meta["start"]}
                 for s in voice["sentences"] if s["block"] == bi]
        al = align(local, str(bdir / "narration.wav"))
        ratios.append(al["match_ratio"])
        bvoice = {"sentences": local, "duration": meta["duration"]}
        m = Matcher(db, topic, workdir, seed=f"{video_id}-{bi}")
        sb = plan(db, video_id, {"segments": block["segments"]}, bvoice, al["words"], topic, workdir,
                  max_scene=6.0, target_scene=4.2, matcher=m, record_usage=False, prefer_images=True)
        for s in sb["scenes"]:
            s["start"] = round(s["start"] + meta["start"], 3)
            s["end"] = round(s["end"] + meta["start"], 3)
            scenes.append(s)
        for c in chunk_words(al["words"], 5):
            c = dict(c, start=c["start"] + meta["start"], end=c["end"] + meta["start"],
                     words=[dict(w, start=w["start"] + meta["start"], end=w["end"] + meta["start"]) for w in c["words"]])
            chunks.append(c)
    scenes.sort(key=lambda s: s["start"])
    for i, s in enumerate(scenes):
        s["i"] = i
    for a, b in zip(scenes, scenes[1:]):
        a["end"] = b["start"]
    if scenes:
        scenes[-1]["end"] = voice["duration"]
    for s in scenes:
        if s["asset_id"]:
            db.x("INSERT OR IGNORE INTO assets_used(video_id,asset_id,start,end) VALUES (?,?,?,?)",
                 video_id, s["asset_id"], s["start"], s["end"])
    conf = [s["match_confidence"] for s in scenes]
    valid = [r for r in ratios if r is not None]
    return {"video_id": video_id, "duration": voice["duration"], "scenes": scenes, "chunks": chunks,
            "avg_confidence": round(sum(conf) / max(1, len(conf)), 3), "status_badge": "",
            "align_ratio": round(sum(valid) / len(valid), 3) if valid else None}


# ----------------------------------------------------------------------------- metadados e thumbnail
def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}" if sec < 3600 else f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def episode_metadata(script: dict[str, Any], voice: dict[str, Any], plan_: dict[str, Any], db: DB, video_id: str) -> dict[str, Any]:
    chapters_ts = ["00:00 Abertura"]
    for meta in voice["blocks"]:
        if "bumper_start" in meta:
            chapters_ts.append(f"{_ts(meta['bumper_start'])} {meta['title']}")
    outro_start = voice["blocks"][-1]["start"]
    chapters_ts.append(f"{_ts(outro_start)} Encerramento")
    sources: list[str] = []
    for i, c in enumerate(script["chapters"]):
        t = db.one("SELECT evidence FROM topics WHERE id=?", c["topic_id"])
        ev = {e["id"]: e for e in json.loads(t["evidence"])} if t else {}
        used = {cl.get("evidence") for s in c["segments"] for cl in (s.get("claims") or [])}
        for eid in used:
            if eid in ev:
                sources.append(f"- Cap. {i + 1}: {ev[eid]['source']} — {ev[eid]['url']}")
    owners = sorted({s["credit"].replace("Imagens: ", "") for s in plan_["scenes"] if s.get("credit")})
    tags_h = list(dict.fromkeys(["#" + norm(h).replace(" ", "") for h in script.get("hashtags", []) if h] + ["#games"]))[:5]
    desc = "\n\n".join([
        script.get("description", ""),
        "Capítulos:\n" + "\n".join(chapters_ts),
        "Fontes:\n" + "\n".join(sources[:30]),
        "Contém trechos de humor e ficção: as cenas imaginárias são piada, não informação oficial.",
        (f"Imagens oficiais de divulgação: {', '.join(owners)}. " if owners else "")
        + "Canal de fãs, sem vínculo oficial com as produtoras citadas. Narração com voz sintética; roteiro e edição originais.",
        " ".join(tags_h), f"ref:{video_id}"])
    tags = ["games", "notícias de games", "games hoje", "jogos"] + [c["title_short"][:30] for c in script["chapters"]][:8]
    return {"title": script["title"], "description": desc[:4900], "hashtags": tags_h, "tags": tags, "ref": video_id,
            "chapters": chapters_ts}


def make_thumbnail(script: dict[str, Any], plan_: dict[str, Any], out: Path) -> Path | None:
    """Thumbnail 1280x720: imagem oficial mais forte do 1º capítulo + texto grande."""
    import cv2
    from factory.renderer import text as T
    from factory.renderer.compositor import imread
    cand = [s for s in plan_["scenes"] if s["type"] == "image" and s.get("asset_path") and s.get("role") != "bumper"]
    if not cand:
        return None
    best = max(cand[:12], key=lambda s: s["match_confidence"])
    img = imread(best["asset_path"])
    h, w = img.shape[:2]
    s = max(1280 / w, 720 / h)
    img = cv2.resize(img, (int(w * s) + 1, int(h * s) + 1), interpolation=cv2.INTER_AREA)
    fx, fy = best.get("focus") or [0.5, 0.5]
    x0 = int(min(max(fx * img.shape[1] - 640, 0), img.shape[1] - 1280))
    y0 = int(min(max(fy * img.shape[0] - 360, 0), img.shape[0] - 720))
    img = img[y0:y0 + 720, x0:x0 + 1280].copy()
    grad = np.linspace(0.25, 1.0, 720)[:, None, None]
    img = (img.astype(np.float32) * grad).astype(np.uint8)
    txt = (script.get("thumbnail_text") or script["chapters"][0]["title_short"]).upper()
    t_img = T.text_rgba(txt, "anton", 150, fill=T.YELLOW, stroke=10, shadow=12)
    if t_img.shape[1] > 1180:
        t_img = T.scale_rgba(t_img, 1180 / t_img.shape[1])
    T.blend(img, t_img, 640 - t_img.shape[1] // 2, 700 - t_img.shape[0])
    chip = T.boxed_text("GAMES HOJE", "anton", 56)
    T.blend(img, chip, 30, 24)
    cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(str(out))
    return out


# ----------------------------------------------------------------------------- slot
def ensure_slot(db: DB) -> str | None:
    """Cria o vídeo longo do dia (horário longform.publish_time) se ainda não existir e estiver na janela."""
    cfg = settings()
    if not cfg.get("longform.enabled", False):
        return None
    hh, mm = map(int, str(cfg.get("longform.publish_time", "19:00")).split(":"))
    now = datetime.now(timezone.utc)
    window = timedelta(hours=float(cfg.get("automation.produce_window_hours", 23)))
    import uuid
    for d in (0, 1):
        day = (datetime.now(BRT) + timedelta(days=d)).date()
        pa = datetime(day.year, day.month, day.day, hh, mm, tzinfo=BRT).astimezone(timezone.utc)
        if not (now + timedelta(minutes=90) <= pa <= now + window):
            continue
        idem = f"{day.isoformat()}TLONG"
        if db.one("SELECT 1 FROM videos WHERE idem_key=?", idem):
            continue
        vid = uuid.uuid4().hex[:12]
        db.x("INSERT INTO videos(id,idem_key,topic_id,slot_date,slot_time,publish_at_utc,state,format,created_at,updated_at)"
             " VALUES (?,?,NULL,?,?,?,?,'long',?,?)", vid, idem, day.isoformat(), f"{hh:02d}:{mm:02d}", pa.isoformat(),
             State.VERIFIED.value, now_iso(), now_iso())
        db.event("vídeo longo do dia criado", vid)
        return vid
    return None
