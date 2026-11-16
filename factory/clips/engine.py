"""Modo CLIPAGEM: melhores momentos de lives/reacts de games, em vertical, com legenda em português.

Fontes permitidas (config `clips`):
  - busca automática SOMENTE de vídeos com licença Creative Commons no YouTube (categoria Games);
  - canais que o dono marcou como autorizados (permissão do criador ou programa oficial de clipes).
Nunca baixa conteúdo fora dessas fontes. O crédito do criador fica sempre visível e na descrição.
"""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from factory.config import settings
from factory.database.db import DB, State, now_iso
from factory.llm import LLM, LLMError
from factory.log import get_logger
from factory.textutil import norm

log = get_logger("clips")

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_sources (
  video_id TEXT PRIMARY KEY, title TEXT, channel TEXT, channel_id TEXT, views INTEGER, duration_s INTEGER,
  license TEXT, basis TEXT, url TEXT, status TEXT NOT NULL DEFAULT 'NEW', clips_made INTEGER DEFAULT 0,
  discovered_at TEXT, processed_at TEXT, note TEXT);
"""

PICK_SYSTEM = """Você é editor de clipes virais (estilo OpusClip) para um canal brasileiro de GAMES no YouTube Shorts/TikTok.
Recebe a transcrição de uma live ou react, com tempos em segundos. Escolha os MELHORES MOMENTOS para virar clipes:
- cada clipe com 20 a 58 segundos, começando no início de uma fala e terminando no fim de uma fala;
- precisa fazer sentido sozinho para quem não viu a live (tem contexto, começo e fim);
- priorize: reação forte, momento engraçado, revelação, surpresa, opinião polêmica, frase marcante, clímax de gameplay;
- evite: trechos sem fala, conversa de bastidor, propaganda, pedido de inscrição, discurso de ódio, conteúdo sexual.
- "title_pt": título curto e chamativo em português, fiel ao que acontece no trecho (sem clickbait falso).
- "score": 1 a 10 de potencial viral. "why": por que funciona.
Responda SOMENTE JSON: {"clips":[{"start":123.4,"end":160.2,"title_pt":"...","score":8,"why":"..."}]}"""

TRANSLATE_SYSTEM = """Traduza para português do Brasil falas de uma live/react de games, em tom natural e jovem (gírias leves),
mantendo o sentido, os palavrões suavizados e os nomes próprios. Uma tradução por item, mesma ordem e mesmos ids.
Responda SOMENTE JSON: {"items":[{"id":0,"pt":"..."}]}"""


def initialize(db: DB) -> None:
    db.conn.executescript(SCHEMA)


# ----------------------------------------------------------------------------- descoberta
def _iso_dur(d: str) -> int:
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    return sum(int(x or 0) * n for x, n in zip(m.groups(), (3600, 60, 1))) if m else 0


def discover(db: DB) -> int:
    """Busca fontes permitidas (Creative Commons + canais autorizados). No máximo 1x por dia (cota)."""
    from factory.youtube import client as yt
    initialize(db)
    cfg = settings()
    day = datetime.now(timezone.utc).date().isoformat()
    if db.one("SELECT 1 FROM clip_sources WHERE discovered_at LIKE ?", f"{day}%"):
        return 0
    api = yt.service()
    found: dict[str, dict[str, Any]] = {}
    for q in cfg.get("clips.search_queries", [])[: int(cfg.get("clips.max_searches_per_day", 2))]:
        res = api.search().list(part="snippet", q=q, type="video", videoLicense="creativeCommon", videoCategoryId="20",
                                order="viewCount", videoDuration="long", maxResults=15,
                                publishedAfter=(datetime.now(timezone.utc) - timedelta(days=int(cfg.get("clips.max_age_days", 60))))
                                .isoformat().replace("+00:00", "Z")).execute()
        for it in res.get("items", []):
            found[it["id"]["videoId"]] = {"basis": "Creative Commons (filtro oficial da busca do YouTube)"}
    for ch in cfg.get("clips.authorized_channels", []) or []:
        if not ch.get("authorized"):
            continue   # só canais que o dono marcou como autorizados
        handle = ch["url"].rstrip("/").split("/")[-1]
        items = api.channels().list(part="contentDetails", forHandle=handle).execute().get("items", [])
        if not items:
            continue
        pl = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        for it in api.playlistItems().list(part="contentDetails", playlistId=pl, maxResults=10).execute().get("items", []):
            found[it["contentDetails"]["videoId"]] = {"basis": f"canal autorizado pelo criador ({ch.get('basis', 'permissão')})"}
    if not found:
        return 0
    n = 0
    ids = list(found)
    for off in range(0, len(ids), 50):
        for v in api.videos().list(part="snippet,statistics,status,contentDetails", id=",".join(ids[off:off + 50])).execute()["items"]:
            lic = v["status"].get("license")
            basis = found[v["id"]]["basis"]
            if lic != "creativeCommon" and not basis.startswith("canal autorizado"):
                continue   # dupla checagem da licença
            dur = _iso_dur(v["contentDetails"]["duration"])
            if not (8 * 60 <= dur <= 4 * 3600):
                continue
            db.x("INSERT OR IGNORE INTO clip_sources(video_id,title,channel,channel_id,views,duration_s,license,basis,url,status,discovered_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?, 'NEW', ?)", v["id"], v["snippet"]["title"], v["snippet"]["channelTitle"],
                 v["snippet"]["channelId"], int(v["statistics"].get("viewCount", 0)), dur, lic, basis,
                 f"https://www.youtube.com/watch?v={v['id']}", now_iso())
            n += 1
    log.info("Clipagem: %d fontes novas permitidas", n)
    return n


# ----------------------------------------------------------------------------- mídia
def _yt_dlp(args: list[str], timeout: int = 1800) -> None:
    import sys
    subprocess.run([sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-progress", *args], check=True, timeout=timeout)


def download_audio(url: str, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    _yt_dlp(["-f", "bestaudio[ext=m4a]/bestaudio", "-o", str(work / "audio.%(ext)s"), url])
    return next(work.glob("audio.*"))


def download_section(url: str, start: float, end: float, out: Path) -> Path:
    _yt_dlp(["-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
             "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
             "--merge-output-format", "mp4", "-o", str(out), url])
    return out


def transcribe(audio: Path) -> dict[str, Any]:
    from factory.subtitles.align import _load_16k, _whisper
    segs, info = _whisper().transcribe(_load_16k(str(audio)), word_timestamps=True, beam_size=1, vad_filter=True,
                                       condition_on_previous_text=False)
    out = []
    for s in segs:
        out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
                    "words": [{"w": w.word.strip(), "start": float(w.start), "end": float(w.end)} for w in (s.words or [])]})
    return {"language": info.language, "segments": out}


def loudness_peaks(audio: Path) -> list[float]:
    """Segundos com pico de energia (gritos/risadas/reações): ajudam a IA a achar momentos fortes."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32)
    sec = a[: len(a) // 8000 * 8000].reshape(-1, 8000)
    rms = np.sqrt((sec ** 2).mean(axis=1)) + 1e-6
    db_ = 20 * np.log10(rms)
    thr = np.percentile(db_, 97)
    return [float(i) for i in np.where(db_ >= thr)[0]]


# ----------------------------------------------------------------------------- seleção
def pick_moments(llm: LLM, tr: dict[str, Any], peaks: list[float], max_clips: int) -> list[dict[str, Any]]:
    segs = tr["segments"]
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs]
    peak_txt = ", ".join(f"{p:.0f}" for p in peaks[:60])
    cands: list[dict[str, Any]] = []
    window, buf = [], 0
    chunks = []
    for ln in lines:
        window.append(ln)
        buf += len(ln)
        if buf > 14000:
            chunks.append(window)
            window, buf = [], 0
    if window:
        chunks.append(window)
    for ch in chunks:
        try:
            out = llm.json(PICK_SYSTEM, f"PICOS DE ENERGIA (segundos com grito/risada): {peak_txt}\n\nTRANSCRIÇÃO:\n" + "\n".join(ch),
                           purpose="clip_pick", temperature=0.3, max_tokens=2000)
        except LLMError as e:
            log.warning("Seleção de momentos falhou: %s", e)
            continue
        for c in (out.get("clips") or []) if isinstance(out, dict) else []:
            try:
                st, en = float(c["start"]), float(c["end"])
            except (KeyError, TypeError, ValueError):
                continue
            # encaixa nos limites reais das falas
            st = max((s["start"] for s in segs if s["start"] <= st + 0.5), default=st)
            en = min((s["end"] for s in segs if s["end"] >= en - 0.5), default=en)
            if 18 <= en - st <= 60:
                cands.append({**c, "start": st, "end": en})
    cands.sort(key=lambda c: c.get("score", 0), reverse=True)
    chosen: list[dict[str, Any]] = []
    for c in cands:
        if any(not (c["end"] <= o["start"] or c["start"] >= o["end"]) for o in chosen):
            continue   # sem sobreposição
        chosen.append(c)
        if len(chosen) >= max_clips:
            break
    return chosen


PROFANITY = [
    (r"\bputa\s+que\s+pariu\b", "meu Deus"), (r"\bputa\s+merda\b", "caramba"), (r"\bfilh[oa]\s+da\s+puta\b", "safado"),
    (r"\bcaralh(o|ada)\b", "caramba"), (r"\bporra\b", "poxa"), (r"\bmerda\b", "droga"), (r"\bfod[ae]r?\b", "doido"),
    (r"\bfodido\b", "lascado"), (r"\bcacete\b", "caramba"), (r"\bbosta\b", "droga"), (r"\bcu\b", "bumbum"),
    (r"\bputa\b", "baita"), (r"\bvai\s+se\s+foder\b", "vai passear"),
]


def soften(text: str) -> str:
    """Palavrões trocados por versões leves (monetização/público jovem), independente da IA."""
    for pat, rep in PROFANITY:
        text = re.sub(pat, rep, text, flags=re.I)
    return text


def captions_for(llm: LLM, tr: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    """Legendas em português (curtas, 3-5 palavras) com tempos relativos ao início do clipe."""
    segs = [s for s in tr["segments"] if s["end"] > start and s["start"] < end]
    pt: dict[int, str] = {}
    if tr["language"] != "pt":
        # lotes pequenos + nova tentativa só do que faltou (modelos leves às vezes pulam itens)
        todo = list(range(len(segs)))
        for _attempt in range(3):
            for b in range(0, len(todo), 12):
                batch = [{"id": i, "text": segs[i]["text"]} for i in todo[b:b + 12]]
                try:
                    out = llm.json(TRANSLATE_SYSTEM, json.dumps(batch, ensure_ascii=False), purpose="clip_translate",
                                   temperature=0.2)
                except LLMError:
                    continue
                for x in (out.get("items") or []) if isinstance(out, dict) else []:
                    try:
                        if isinstance(x, dict) and str(x.get("pt", "")).strip():
                            pt[int(x["id"])] = str(x["pt"]).strip()
                    except (KeyError, TypeError, ValueError):
                        pass
            todo = [i for i in range(len(segs)) if i not in pt]
            if not todo:
                break
        if todo:
            raise RuntimeError(f"tradução incompleta das legendas ({len(todo)} de {len(segs)} falas sem tradução)")
    caps = []
    for i, s in enumerate(segs):
        s0, s1 = max(start, s["start"]), min(end, s["end"])
        if s1 - s0 < 0.2:
            continue
        if tr["language"] == "pt" and s["words"]:
            ws = [w for w in s["words"] if start <= w["start"] < end]
            for k in range(0, len(ws), 4):
                grp = ws[k:k + 4]
                caps.append({"start": grp[0]["start"] - start, "end": grp[-1]["end"] - start,
                             "text": " ".join(w["w"] for w in grp)})
            continue
        words = (pt.get(i) or s["text"]).split()
        n = max(1, round(len(words) / 4))
        per = (s1 - s0) / n
        for k in range(n):
            part = words[k * len(words) // n:(k + 1) * len(words) // n]
            if part:
                caps.append({"start": s0 - start + k * per, "end": s0 - start + (k + 1) * per, "text": " ".join(part)})
    for c in caps:
        c["text"] = soften(c["text"])
    return caps


# ----------------------------------------------------------------------------- layout e render
def choose_layout(clip: Path) -> dict[str, Any]:
    """Decide o enquadramento vertical olhando rostos em quadros amostrados."""
    import cv2
    from factory.assets.catalog import _face_detector
    det = _face_detector()
    cap = cv2.VideoCapture(str(clip))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    faces = []
    for idx in range(0, max(1, n), int(fps * 2)):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, fr = cap.read()
        if not ok or det is None:
            continue
        s = 640 / max(W, H)
        small = cv2.resize(fr, (int(W * s), int(H * s)))
        det.setInputSize((small.shape[1], small.shape[0]))
        _, f = det.detect(small)
        if f is not None and len(f):
            b = max(f, key=lambda x: x[2] * x[3])
            faces.append(((b[0] + b[2] / 2) / s, (b[1] + b[3] / 2) / s, b[2] * b[3] / (s * s) / (W * H), b[2] / s, b[3] / s))
    cap.release()
    if len(faces) < 3:
        return {"kind": "center", "W": W, "H": H}
    fx = float(np.median([f[0] for f in faces]))
    fy = float(np.median([f[1] for f in faces]))
    area = float(np.median([f[2] for f in faces]))
    fw = float(np.median([f[3] for f in faces]))
    if area >= 0.02:
        return {"kind": "face", "W": W, "H": H, "fx": fx}
    return {"kind": "split", "W": W, "H": H, "fx": fx, "fy": fy, "fw": fw}


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def write_ass(caps: list[dict[str, Any]], credit: str, path: Path, cap_y: int) -> None:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Anton,84,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,8,70,70,{cap_y},1
Style: Credit,Anton,58,&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,1,0,3,10,0,8,40,40,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 1,{_ass_time(0)},{_ass_time(99999)},Credit,,0,0,0,,{credit}"]
    for c in caps:
        txt = c["text"].upper().replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},Cap,,0,0,0,,{{\\fad(60,0)}}{txt}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def render_clip(src: Path, layout: dict[str, Any], ass: Path, out: Path) -> Path:
    from factory.config import ROOT
    W, H = layout["W"], layout["H"]
    fontsdir = str(ROOT / "assets" / "fonts").replace("\\", "/").replace(":", "\\:")
    subs = f"subtitles='{str(ass).replace(chr(92), '/').replace(':', chr(92) + ':')}':fontsdir='{fontsdir}'"
    if layout["kind"] == "face":
        cw = int(H * 9 / 16) // 2 * 2
        x0 = int(min(max(layout["fx"] - cw / 2, 0), W - cw))
        fc = f"[0:v]crop={cw}:{H}:{x0}:0,scale=1080:1920:flags=lanczos,setsar=1,{subs}[v]"
    elif layout["kind"] == "split":
        # jogo em cima (centro do quadro), facecam embaixo (em volta do rosto)
        fw = max(layout["fw"] * 3.2, W * 0.18)
        fh = fw * 820 / 1080
        x0 = int(min(max(layout["fx"] - fw / 2, 0), W - fw))
        y0 = int(min(max(layout["fy"] - fh / 2, 0), H - fh))
        gw = int(H * 1080 / 1100) // 2 * 2
        gx = (W - gw) // 2
        fc = (f"[0:v]split=2[a][b];[a]crop={gw}:{H}:{gx}:0,scale=1080:1100:flags=lanczos[top];"
              f"[b]crop={int(fw)}:{int(fh)}:{x0}:{y0},scale=1080:820:flags=lanczos[bot];"
              f"[top][bot]vstack=inputs=2,setsar=1,{subs}[v]")
    else:
        fc = (f"[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              f"boxblur=24:2,eq=brightness=-0.15[bg];[b]scale=1080:-2:flags=lanczos[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,{subs}[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
                    "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                    "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                    "-movflags", "+faststart", str(out)], check=True)
    return out


# ----------------------------------------------------------------------------- orquestração
def clip_metadata(src: dict[str, Any], clip: dict[str, Any], vid: str) -> dict[str, Any]:
    title = soften(re.sub(r"#\w+", "", clip.get("title_pt", "")).strip())[:85]
    lic = ("Licença: Creative Commons Atribuição (CC BY) — reutilização permitida pelo autor."
           if src["license"] == "creativeCommon" else f"Uso autorizado: {src['basis']}.")
    desc = "\n\n".join([
        f"Trecho de \"{src['title']}\" — por {src['channel']}.",
        f"Vídeo original: {src['url']}", lic,
        "Legendas traduzidas/adaptadas para português. Todo o crédito ao criador original.",
        "#games #shorts #react", f"ref:{vid}"])
    return {"title": f"{title} | {src['channel']}"[:98], "description": desc, "hashtags": ["#games", "#shorts", "#react"],
            "tags": ["games", "react", "live", src["channel"]], "ref": vid}


def produce_clips(db: DB, llm: LLM, slots: list[tuple[str, str, str]], workroot: Path) -> list[str]:
    """Preenche os slots informados (dia, horário, publish_at) com clipes prontos e validados."""
    from factory.qa.checks import technical
    initialize(db)
    made: list[str] = []
    pending = list(slots)
    for src in [dict(r) for r in db.q("SELECT * FROM clip_sources WHERE status='NEW' ORDER BY views DESC LIMIT 6")]:
        if not pending:
            break
        work = workroot / f"src_{src['video_id']}"
        try:
            audio = download_audio(src["url"], work)
            tr = transcribe(audio)
            moments = pick_moments(llm, tr, loudness_peaks(audio), max_clips=min(len(pending), 3))
            if not moments:
                db.x("UPDATE clip_sources SET status='NO_MOMENTS', processed_at=? WHERE video_id=?", now_iso(), src["video_id"])
                continue
            for m in moments:
                if not pending:
                    break
                day, slot, pa = pending[0]
                vid = uuid.uuid4().hex[:12]
                try:
                    made_vid = _one_clip(db, llm, src, tr, m, work, workroot, day, slot, pa, vid)
                except Exception as e:   # um momento ruim não derruba a live inteira
                    db.record_error(None, "clip_moment", e)
                    log.warning("Momento %.0f-%.0fs descartado: %s", m["start"], m["end"], e)
                    continue
                if made_vid:
                    made.append(made_vid)
                    pending.pop(0)
            db.x("UPDATE clip_sources SET status='DONE', clips_made=clips_made+?, processed_at=? WHERE video_id=?",
                 len(moments), now_iso(), src["video_id"])
        except Exception as e:
            db.record_error(None, "clips", e)
            db.x("UPDATE clip_sources SET status='FAILED', note=?, processed_at=? WHERE video_id=?", str(e)[:300], now_iso(),
                 src["video_id"])
            log.warning("Fonte %s falhou: %s", src["video_id"], e)
    return made


def _one_clip(db: DB, llm: LLM, src: dict[str, Any], tr: dict[str, Any], m: dict[str, Any], work: Path, workroot: Path,
              day: str, slot: str, pa: str, vid: str) -> str | None:
    from factory.qa.checks import technical
    raw = download_section(src["url"], m["start"], m["end"], work / f"{vid}_raw.mp4")
    caps = captions_for(llm, tr, m["start"], m["end"])
    layout = choose_layout(raw)
    credit = f"{src['channel']} • YouTube"
    ass = work / f"{vid}.ass"
    write_ass(caps, credit, ass, cap_y=1080 if layout["kind"] == "split" else 1250)
    out = workroot / f"{day}_{slot.replace(':', '')}_CLIP_{vid}.mp4"
    render_clip(raw, layout, ass, out)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True).stdout or 0)
    voice = {"sentences": [{"start": 0.3, "end": max(0.4, dur - 0.5)}], "duration": dur}
    chunks = [{"start": c["start"], "end": c["end"], "words": c["text"].split()[:3]} for c in caps]
    tech = technical(out, voice, chunks, None, fmt="short", max_silence=3.0)
    fails = [f for f in tech["fails"] if "narração" not in f and "legendas cobrem" not in f]
    if fails or not caps:
        log.warning("Clipe reprovado no QA: %s", fails or "sem legendas")
        return None
    meta = clip_metadata(src, m, vid)
    db.x("INSERT INTO videos(id,idem_key,topic_id,slot_date,slot_time,publish_at_utc,state,format,video_path,"
         "metadata,qa,script,duration,hook_style,created_at,updated_at) VALUES (?,?,NULL,?,?,?,?,'clip',?,?,?,?,?,?,?,?)",
         vid, f"{day}T{slot}", day, slot, pa, State.VALIDATED.value, str(out), json.dumps(meta, ensure_ascii=False),
         json.dumps({"technical": tech, "layout": layout["kind"]}, ensure_ascii=False),
         json.dumps({"source": src, "moment": m, "captions": caps}, ensure_ascii=False), dur, "clip", now_iso(), now_iso())
    db.event(f"clipe pronto de {src['channel']} ({m['start']:.0f}-{m['end']:.0f}s, layout {layout['kind']})", vid)
    return vid


def mode(data_dir: Path) -> str:
    f = data_dir / "mode.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("mode", "producao")
    except (OSError, ValueError):
        return "producao"
