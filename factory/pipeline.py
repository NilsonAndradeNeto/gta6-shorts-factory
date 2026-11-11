"""Orquestração da fábrica: pesquisa -> pautas -> produção -> QA -> upload -> confirmação.

Estados duráveis (ficam no SQLite versionado): DISCOVERED, VERIFIED, SCRIPTED, UPLOADED,
SCHEDULED, PUBLISHED, BLOCKED, ABANDONED. Estados intermediários (AUDIO_READY ... VALIDATED)
dependem de arquivos locais; se a execução cair, a próxima regenera a partir de SCRIPTED
(o processo é determinístico), sem gastar nova chamada de LLM e sem risco de duplicar upload.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from factory.config import settings
from factory.database.db import DB, State, now_iso
from factory.llm import LLM, LLMError
from factory.log import get_logger

log = get_logger("pipeline")
BRT = ZoneInfo("America/Sao_Paulo")
ACTIVE = (State.VERIFIED, State.SCRIPTED, State.AUDIO_READY, State.ASSETS_READY, State.RENDERED,
          State.VALIDATED, State.FAILED)
LOCAL_STATES = (State.AUDIO_READY, State.ASSETS_READY, State.RENDERED, State.VALIDATED)


class RunLock:
    """Evita duas execuções simultâneas no mesmo banco (o Actions também usa concurrency group)."""

    def __init__(self, data_dir: Path):
        self.f = data_dir / "run.lock"

    def __enter__(self) -> "RunLock":
        if self.f.exists():
            age = time.time() - self.f.stat().st_mtime
            if age < 2 * 3600:
                raise RuntimeError(f"Outra execução em andamento (lock com {age / 60:.0f} min)")
            log.warning("Lock antigo removido (%.0f min)", age / 60)
        self.f.write_text(f"{os.getpid()} {now_iso()}", encoding="utf-8")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.f.unlink(missing_ok=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slot_datetimes(days_ahead: int = 2) -> list[tuple[str, str, datetime]]:
    slots = settings().get("channel.publish_slots")
    today = datetime.now(BRT).date()
    out = []
    for d in range(days_ahead):
        day = today + timedelta(days=d)
        for s in slots:
            h, m = map(int, s.split(":"))
            dt = datetime(day.year, day.month, day.day, h, m, tzinfo=BRT)
            out.append((day.isoformat(), s, dt))
    return out


class Factory:
    def __init__(self, db: DB, llm: LLM | None = None):
        self.db = db
        self.llm = llm or LLM(db)
        self.cfg = settings()
        self.t0 = time.time()
        self.out_root = self.cfg.path("output_dir")
        self.summary: dict[str, Any] = {"produced": [], "uploaded": [], "blocked": [], "errors": []}

    # ------------------------------------------------------------------ pesquisa
    def research(self) -> dict[str, Any]:
        from factory.editorial.evergreen import ensure_evergreen_topics
        from factory.research.collector import collect, collect_official_pages
        from factory.verification.classify import cluster_and_classify
        stats: dict[str, Any] = {}
        try:
            stats["official_pages"] = collect_official_pages(self.db)
            stats["feeds"] = collect(self.db)
        except Exception as e:  # fonte fora do ar não derruba a fábrica
            self.db.record_error(None, "research", e)
            log.exception("Falha parcial na coleta")
        try:
            stats["new_topics"] = cluster_and_classify(self.db, self.llm)
        except Exception as e:
            self.db.record_error(None, "classify", e)
            log.exception("Falha na classificação")
        stats["evergreen"] = ensure_evergreen_topics(self.db)
        return stats

    # ------------------------------------------------------------------ slots
    def plan_slots(self) -> list[str]:
        from factory.editorial.selector import pick
        now = datetime.now(timezone.utc)
        lead = timedelta(minutes=int(self.cfg.get("automation.min_lead_minutes", 40)))
        window = timedelta(hours=float(self.cfg.get("automation.produce_window_hours", 10)))
        need = []
        for day, slot, dt in slot_datetimes():
            if now + lead <= dt.astimezone(timezone.utc) <= now + window:
                if not self.db.one("SELECT 1 FROM videos WHERE idem_key=?", f"{day}T{slot}"):
                    need.append((day, slot, dt))
        if not need:
            return []
        topics = pick(self.db, len(need))
        created = []
        for (day, slot, dt), topic in zip(need, topics):
            vid = self.db.create_video(topic["id"], day, slot, dt.astimezone(timezone.utc).isoformat())
            if vid:
                created.append(vid)
                log.info("Slot %s %s -> vídeo %s (pauta %s: %s)", day, slot, vid, topic["id"], topic["title"][:60])
        if len(topics) < len(need):
            self.db.event(f"Sem pautas aprovadas suficientes: {len(topics)}/{len(need)} slots preenchidos")
        return created

    # ------------------------------------------------------------------ produção
    def _work(self, vid: str) -> Path:
        d = self.out_root / vid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _topic(self, topic_id: int) -> dict[str, Any]:
        return dict(self.db.one("SELECT * FROM topics WHERE id=?", topic_id))

    def _replace_topic(self, v: dict[str, Any], reason: str) -> bool:
        from factory.editorial.selector import pick
        self.db.x("UPDATE topics SET state='REJECTED', note=? WHERE id=?", reason[:500], v["topic_id"])
        used = {r["topic_id"] for r in self.db.q("SELECT topic_id FROM videos")}
        alt = pick(self.db, 1, exclude_ids=used)
        if not alt:
            return False
        self.db.x("UPDATE topics SET state='USED', used_at=? WHERE id=?", now_iso(), alt[0]["id"])
        self.db.advance(v["id"], State.VERIFIED, f"pauta trocada: {reason[:200]}", topic_id=alt[0]["id"],
                        script=None, storyboard=None, metadata=None, qa=None)
        return True

    def step_script(self, v: dict[str, Any]) -> None:
        from factory.scriptwriter.writer import write_script
        from factory.verification.factcheck import check
        for _topic_try in range(3):
            topic = self._topic(v["topic_id"])
            evidence = json.loads(topic["evidence"])
            feedback = ""
            for attempt in range(3):
                script = write_script(self.db, self.llm, topic, feedback=feedback)
                fact = check(self.llm, script, evidence)
                if fact["passed"]:
                    script["factcheck"] = {k: fact[k] for k in ("passed", "claims", "programmatic", "judge")}
                    self.db.advance(v["id"], State.SCRIPTED, f"roteiro aprovado (tentativa {attempt + 1})",
                                    script=script, hook_style=script.get("hook_style"), angle=script.get("angle"))
                    return
                problems = fact["programmatic"] + fact["judge"]
                feedback = "\n".join(f"- {p}" for p in problems[:12])
                log.info("Roteiro reprovado na checagem (tentativa %d): %s", attempt + 1, problems[:4])
            if not self._replace_topic(v, "roteiro não passou na checagem factual: " + "; ".join(problems[:3])):
                break
            v = self.db.video(v["id"])
        self.db.advance(v["id"], State.BLOCKED, "nenhuma pauta produziu roteiro verificável")
        raise RuntimeError("bloqueado: checagem factual")

    def step_audio(self, v: dict[str, Any]) -> None:
        from factory.voice.tts import synthesize_script
        voice = synthesize_script(v["script"], self._work(v["id"]) / "voice")
        self.db.advance(v["id"], State.AUDIO_READY, voice["engine"], audio_path=voice["wav"],
                        storyboard={"voice": voice})

    def step_assets(self, v: dict[str, Any]) -> None:
        from factory.storyboard.planner import plan
        from factory.subtitles.align import align, chunk_words
        work = v["storyboard"]
        voice = work["voice"]
        al = align(voice["sentences"], voice["wav"])
        chunks = chunk_words(al["words"], int(self.cfg.get("subtitles.max_words_per_chunk", 3)))
        topic = self._topic(v["topic_id"])
        self.db.x("DELETE FROM assets_used WHERE video_id=?", v["id"])
        sb = plan(self.db, v["id"], v["script"], voice, al["words"], topic, self._work(v["id"]))
        work.update(align_ratio=al["match_ratio"], chunks=chunks, plan=sb)
        self.db.advance(v["id"], State.ASSETS_READY, f"{len(sb['scenes'])} cenas", storyboard=work)
        (self._work(v["id"]) / "storyboard.json").write_text(json.dumps(sb, ensure_ascii=False, indent=1), encoding="utf-8")

    def step_render(self, v: dict[str, Any]) -> None:
        from factory.renderer.audio import mix
        from factory.renderer.compositor import render
        from factory.youtube.metadata import build
        work = v["storyboard"]
        topic = self._topic(v["topic_id"])
        meta = build(v["script"], json.loads(topic["evidence"]), topic["confirmation"], v["id"],
                     credits=[sc.get("credit") for sc in work["plan"]["scenes"] if sc.get("credit")])
        mixed = mix(work["voice"]["wav"], work["plan"], work["voice"]["duration"], self._work(v["id"]), v["id"])
        out = self._work(v["id"]) / f"{v['slot_date']}_{v['slot_time'].replace(':', '')}_{v['id']}.mp4"
        render(work["plan"], work["chunks"], str(mixed), out)
        self.db.advance(v["id"], State.RENDERED, out.name, video_path=str(out), file_sha256=sha256_file(out),
                        metadata=meta, duration=work["voice"]["duration"])

    def step_qa(self, v: dict[str, Any]) -> None:
        from factory.qa.checks import editorial, technical, visual
        work = v["storyboard"]
        path = Path(v["video_path"])
        tech = technical(path, work["voice"], work["chunks"], work.get("align_ratio"))
        vis = visual(path, work["plan"])
        topic = self._topic(v["topic_id"])
        edi = editorial(self.db, v["id"], v["script"], v["script"].get("factcheck", {}), work["plan"], v["metadata"], topic)
        qa = {"technical": tech, "visual": vis, "editorial": edi,
              "limitations": ["movimento/ritmo verificados por métricas indiretas",
                              "correspondência visual verificada pelas marcações oficiais dos assets"]}
        if tech["passed"] and vis["passed"] and edi["passed"]:
            self.db.advance(v["id"], State.VALIDATED, "QA aprovado", qa=qa)
            return
        fails = tech["fails"] + vis["fails"] + edi["fails"]
        self.db.set_fields(v["id"], qa=qa)
        if not edi["passed"]:
            if self._replace_topic(v, "reprovado no QA editorial: " + "; ".join(edi["fails"][:3])):
                raise RuntimeError("QA editorial reprovou; nova pauta atribuída")
            self.db.advance(v["id"], State.BLOCKED, "QA editorial: " + "; ".join(fails[:4]))
            raise RuntimeError("bloqueado no QA editorial")
        raise RuntimeError("QA técnico/visual reprovado: " + "; ".join(fails[:4]))

    def produce(self, vid: str) -> str:
        """Leva um vídeo até VALIDATED. Retorna o estado final."""
        v = self.db.video(vid)
        if v["state"] in ("RENDERED", "VALIDATED") and v.get("video_path") and not Path(v["video_path"]).exists():
            from factory.delivery.telegram import _find_file
            kept = _find_file(v["video_path"])   # cópia guardada no release entre execuções
            if kept is not None and kept.exists():
                self.db.set_fields(vid, video_path=str(kept))
                v = self.db.video(vid)
        if v["state"] in [s.value for s in LOCAL_STATES]:
            needed = v.get("video_path") if v["state"] in ("RENDERED", "VALIDATED") else v.get("audio_path")
            if not needed or not Path(needed).exists():
                log.info("Vídeo %s: arquivos locais ausentes, retomando a partir do roteiro", vid)
                self.db.advance(vid, State.SCRIPTED, "retomada após execução interrompida")
        if v["state"] == State.FAILED.value:
            back = State.SCRIPTED if v.get("script") else State.VERIFIED
            self.db.advance(vid, back, "nova tentativa após falha")
        if v.get("format") == "long":
            steps = {State.VERIFIED.value: self.long_script, State.SCRIPTED.value: self.long_audio,
                     State.AUDIO_READY.value: self.long_assets, State.ASSETS_READY.value: self.long_render,
                     State.RENDERED.value: self.long_qa}
        else:
            steps = {State.VERIFIED.value: self.step_script, State.SCRIPTED.value: self.step_audio,
                     State.AUDIO_READY.value: self.step_assets, State.ASSETS_READY.value: self.step_render,
                     State.RENDERED.value: self.step_qa}
        for _ in range(12):
            v = self.db.video(vid)
            fn = steps.get(v["state"])
            if fn is None:
                return v["state"]
            fn(v)
        return self.db.video(vid)["state"]

    # ------------------------------------------------------------------ vídeo longo (16:9)
    def long_script(self, v: dict[str, Any]) -> None:
        from factory.longform import write_episode
        script = write_episode(self.db, self.llm)
        self.db.advance(v["id"], State.SCRIPTED, f"episódio com {len(script['chapters'])} capítulos", script=script,
                        hook_style=script["hook_style"], angle=script["angle"])

    def long_audio(self, v: dict[str, Any]) -> None:
        from factory.longform import synthesize_episode
        voice = synthesize_episode(v["script"], self._work(v["id"]) / "voice")
        self.db.advance(v["id"], State.AUDIO_READY, f"narração {voice['duration']:.0f}s", audio_path=voice["wav"],
                        storyboard={"voice": voice})

    def long_assets(self, v: dict[str, Any]) -> None:
        from factory.longform import plan_episode
        work = v["storyboard"]
        self.db.x("DELETE FROM assets_used WHERE video_id=?", v["id"])
        plan_ = plan_episode(self.db, v["id"], v["script"], work["voice"], self._work(v["id"]) / "voice")
        work.update(plan={k: plan_[k] for k in plan_ if k != "chunks"}, chunks=plan_["chunks"],
                    align_ratio=plan_["align_ratio"])
        self.db.advance(v["id"], State.ASSETS_READY, f"{len(plan_['scenes'])} cenas", storyboard=work)

    def long_render(self, v: dict[str, Any]) -> None:
        from factory.longform import episode_metadata, make_thumbnail
        from factory.renderer.audio import mix
        from factory.renderer.compositor import render
        work, wd = v["storyboard"], self._work(v["id"])
        meta = episode_metadata(v["script"], work["voice"], work["plan"], self.db, v["id"])
        mixed = mix(work["voice"]["wav"], work["plan"], work["voice"]["duration"], wd, v["id"])
        out = wd / f"{v['slot_date']}_LONG_{v['id']}.mp4"
        render(work["plan"], work["chunks"], str(mixed), out, profile="long")
        thumb = make_thumbnail(v["script"], work["plan"], wd / "thumbnail.jpg")
        meta["thumbnail"] = str(thumb) if thumb else None
        self.db.advance(v["id"], State.RENDERED, out.name, video_path=str(out), file_sha256=sha256_file(out),
                        metadata=meta, duration=work["voice"]["duration"])

    def long_qa(self, v: dict[str, Any]) -> None:
        from factory.assets.catalog import ALLOWED_LICENSE_PREFIXES
        from factory.qa.checks import technical, visual
        work, path = v["storyboard"], Path(v["video_path"])
        tech = technical(path, work["voice"], work["chunks"], work.get("align_ratio"), fmt="long")
        vis = visual(path, work["plan"], fmt="long")
        edi_fails = []
        if not all(c.get("factcheck", {}).get("passed") for c in v["script"]["chapters"]):
            edi_fails.append("capítulo sem checagem factual aprovada")
        for s in work["plan"]["scenes"]:
            if s.get("license") and not s["license"].startswith(ALLOWED_LICENSE_PREFIXES):
                edi_fails.append(f"cena {s['i']} com licença não permitida")
        if work["plan"]["avg_confidence"] < 0.55:
            edi_fails.append(f"correspondência visual média baixa ({work['plan']['avg_confidence']})")
        qa = {"technical": tech, "visual": vis, "editorial": {"passed": not edi_fails, "fails": edi_fails, "warns": []}}
        if tech["passed"] and vis["passed"] and not edi_fails:
            self.db.advance(v["id"], State.VALIDATED, "QA aprovado (vídeo longo)", qa=qa)
            return
        self.db.set_fields(v["id"], qa=qa)
        raise RuntimeError("QA do vídeo longo reprovado: " + "; ".join((tech["fails"] + vis["fails"] + edi_fails)[:4]))

    # ------------------------------------------------------------------ YouTube
    def publish(self, vid: str) -> str:
        from factory.youtube import client as yt
        v = self.db.video(vid)
        if v["state"] != State.VALIDATED.value:
            return v["state"]
        if self.cfg.dry_run or not yt.configured():
            self.db.event("upload pulado (dry-run ou credenciais ausentes)", vid)
            return v["state"]
        if v.get("youtube_id"):
            self.db.advance(vid, State.UPLOADED, "já possuía youtube_id")
            return State.UPLOADED.value
        existing = yt.find_by_ref(vid)
        if existing:
            self.db.advance(vid, State.UPLOADED, "upload anterior encontrado pelo ref (evitou duplicata)",
                            youtube_id=existing, youtube_url=f"https://youtube.com/shorts/{existing}")
        else:
            ytid = yt.upload(Path(v["video_path"]), v["metadata"], v["publish_at_utc"])
            is_long = v.get("format") == "long"
            self.db.advance(vid, State.UPLOADED, "upload concluído", youtube_id=ytid,
                            youtube_url=(f"https://youtu.be/{ytid}" if is_long else f"https://youtube.com/shorts/{ytid}"))
            thumb = (v.get("metadata") or {}).get("thumbnail")
            if is_long and thumb and Path(thumb).exists():
                yt.set_thumbnail(ytid, Path(thumb))
        self.summary["uploaded"].append(vid)
        return self.confirm(vid)

    def confirm(self, vid: str) -> str:
        """Só marca SCHEDULED/PUBLISHED após a API confirmar o estado."""
        from factory.youtube import client as yt
        v = self.db.video(vid)
        st = yt.get_status(v["youtube_id"])
        if st is None:
            self.db.event("vídeo não encontrado na API (processando ou removido)", vid)
            return v["state"]
        if st["uploadStatus"] in ("rejected", "failed", "deleted"):
            self.db.advance(vid, State.BLOCKED, f"YouTube: {st['uploadStatus']} {st.get('rejectionReason') or st.get('failureReason') or ''}")
            return State.BLOCKED.value
        if st["privacyStatus"] == "public":
            self.db.advance(vid, State.PUBLISHED, "público confirmado pela API")
            return State.PUBLISHED.value
        if st["privacyStatus"] == "private" and st.get("publishAt"):
            if v["state"] != State.SCHEDULED.value:
                self.db.advance(vid, State.SCHEDULED, f"agendado para {st['publishAt']} (processamento: {st['processing']})")
            return State.SCHEDULED.value
        if st["privacyStatus"] == "private" and not st.get("publishAt"):
            self.db.event("ATENÇÃO: vídeo ficou privado sem agendamento (projeto da API ainda sem auditoria?)", vid)
        return v["state"]

    def next_free_time(self, gap_minutes: int = 75) -> datetime:
        """Próximo horário livre para publicar (>= agora+20 min e afastado dos vídeos já agendados)."""
        t = datetime.now(timezone.utc) + timedelta(minutes=20)
        taken = sorted(datetime.fromisoformat(r["publish_at_utc"]) for r in self.db.q(
            "SELECT publish_at_utc FROM videos WHERE state NOT IN ('ABANDONED','BLOCKED') AND publish_at_utc IS NOT NULL "
            "AND COALESCE(format,'short')='short'"))
        for x in taken:
            if abs((x - t).total_seconds()) < gap_minutes * 60:
                t = x + timedelta(minutes=gap_minutes)
        return t.replace(microsecond=0)

    def check_published(self) -> None:
        from factory.youtube import client as yt
        if not yt.configured():
            return
        for r in self.db.q("SELECT id FROM videos WHERE state IN ('UPLOADED','SCHEDULED')"):
            try:
                self.confirm(r["id"])
            except Exception as e:
                self.db.record_error(r["id"], "confirm", e)

    # ------------------------------------------------------------------ ciclo
    def budget_left(self) -> bool:
        return (time.time() - self.t0) / 60 < float(self.cfg.get("automation.run_time_budget_minutes", 70))

    def cycle(self, research: bool = True) -> dict[str, Any]:
        run_id = uuid.uuid4().hex[:10]
        self.db.x("INSERT INTO runs(id,kind,started_at,status) VALUES (?,?,?,?)", run_id, "cycle", now_iso(), "running")
        status = "ok"
        try:
            if research:
                self.summary["research"] = self.research()
            if self.llm.available:
                try:
                    from factory.assets.tagger import tag_pending
                    tag_pending(self.llm, int(self.cfg.get("automation.tag_assets_per_run", 12)))
                except Exception as e:
                    self.db.record_error(None, "tagger", e)
            self.summary["new_slots"] = self.plan_slots()
            try:
                from factory.longform import ensure_slot
                long_vid = ensure_slot(self.db)
                if long_vid:
                    self.summary["new_slots"].append(long_vid)
            except Exception as e:
                self.db.record_error(None, "longform_slot", e)
            self.check_published()
            max_att = int(self.cfg.get("automation.max_attempts", 4))
            now = datetime.now(timezone.utc)
            rows = self.db.q(f"SELECT id, publish_at_utc, attempts, state FROM videos WHERE state IN "
                             f"({','.join('?' * len(ACTIVE))}) AND slot_time NOT LIKE 'test-%' "
                             f"ORDER BY publish_at_utc", *[s.value for s in ACTIVE])
            from factory.youtube import client as yt
            can_publish = yt.configured() and not self.cfg.dry_run
            for r in rows:
                if r["state"] == State.VALIDATED.value and not can_publish:
                    continue   # já aprovado; aguarda credenciais do YouTube (não re-renderiza à toa)
                if not self.budget_left():
                    log.warning("Orçamento de tempo da execução esgotado; restante fica para a próxima")
                    break
                pa = datetime.fromisoformat(r["publish_at_utc"])
                slot_day = pa.astimezone(BRT).date()
                today = datetime.now(BRT).date()
                # vídeo do dia (ou aprovado ontem) que perdeu o horário: reagenda em vez de descartar
                if pa < now + timedelta(minutes=15) and (slot_day >= today or
                                                         (r["state"] == State.VALIDATED.value and (today - slot_day).days <= 1)):
                    new_pa = datetime.now(timezone.utc)   # pronto e atrasado: publica na hora
                    self.db.set_fields(r["id"], publish_at_utc=new_pa.isoformat())
                    self.db.event(f"horário perdido; publicação imediata em {new_pa.astimezone(BRT):%d/%m %H:%M}", r["id"])
                elif slot_day < today:
                    self.db.advance(r["id"], State.ABANDONED, "dia do slot passou sem vídeo aprovado")
                    continue
                if r["attempts"] >= max_att:
                    self.db.advance(r["id"], State.ABANDONED, f"excedeu {max_att} tentativas")
                    continue
                try:
                    final = self.produce(r["id"])
                    if final == State.VALIDATED.value:
                        self.summary["produced"].append(r["id"])
                        self.publish(r["id"])
                    elif final == State.BLOCKED.value:
                        self.summary["blocked"].append(r["id"])
                except Exception as e:
                    from factory.youtube.client import QuotaExceeded
                    self.db.record_error(r["id"], "produce", e)
                    cur = self.db.video(r["id"])
                    if cur["state"] not in (State.BLOCKED.value, State.UPLOADED.value, State.SCHEDULED.value):
                        self.db.advance(r["id"], State.FAILED, str(e)[:500], attempts=cur["attempts"] + 1,
                                        last_error=str(e)[:500])
                    self.summary["errors"].append(f"{r['id']}: {e}")
                    log.exception("Falha no vídeo %s", r["id"])
                    if isinstance(e, QuotaExceeded):
                        log.error("Cota do YouTube esgotada; interrompendo uploads nesta execução")
                        break
                    if isinstance(e, LLMError) and "Todos os provedores" in str(e):
                        log.error("LLM indisponível; interrompendo produção nesta execução")
                        break
            try:
                from factory.delivery.telegram import deliver_due, release_manifest
                self.summary["telegram_sent"] = deliver_due(self.db)
                release_manifest(self.db, self.out_root)
            except Exception as e:
                self.db.record_error(None, "telegram", e)
            self._maybe_metrics()
        except Exception as e:
            status = "error"
            self.db.record_error(None, "cycle", e)
            raise
        finally:
            self.db.x("UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?", now_iso(), status,
                      json.dumps(self.summary, ensure_ascii=False, default=str)[:20000], run_id)
            try:
                self.housekeeping()
            except Exception as e:
                log.warning("housekeeping falhou: %s", e)
            self.cleanup()
        return self.summary

    def _maybe_metrics(self) -> None:
        from factory.analytics.metrics import collect as collect_metrics
        last = self.db.one("SELECT MAX(fetched_at) m FROM metrics")
        if last and last["m"] and datetime.fromisoformat(last["m"]) > datetime.now(timezone.utc) - timedelta(hours=20):
            return
        try:
            collect_metrics(self.db)
        except Exception as e:
            self.db.record_error(None, "metrics", e)

    def housekeeping(self) -> None:
        """Mantém o banco pequeno: textos de notícias antigas viram só metadados; logs antigos saem."""
        self.db.x("UPDATE news_items SET body=NULL WHERE collected_at < datetime('now','-30 days') AND body IS NOT NULL")
        self.db.x("DELETE FROM events WHERE ts < datetime('now','-120 days')")
        self.db.x("DELETE FROM errors WHERE ts < datetime('now','-60 days')")
        self.db.x("DELETE FROM llm_usage WHERE ts < datetime('now','-30 days')")
        self.db.x("UPDATE videos SET storyboard=json_remove(storyboard,'$.voice.sentences') "
                  "WHERE state IN ('PUBLISHED','ABANDONED') AND updated_at < datetime('now','-14 days')")
        self.db.conn.execute("VACUUM")

    def cleanup(self, keep_days: int = 2) -> None:
        """Remove arquivos temporários de vídeos finalizados (no Actions o runner é descartado ao fim)."""
        if os.environ.get("GITHUB_ACTIONS"):
            return  # o workflow ainda publica os MP4 como artefato de revisão antes de o runner sumir
        for d in self.out_root.glob("*"):
            if not d.is_dir():
                continue
            row = self.db.one("SELECT state, updated_at FROM videos WHERE id=?", d.name)
            if row and row["state"] in ("UPLOADED", "SCHEDULED", "PUBLISHED", "BLOCKED", "ABANDONED"):
                if datetime.fromisoformat(row["updated_at"]) < datetime.now(timezone.utc) - timedelta(days=keep_days):
                    shutil.rmtree(d, ignore_errors=True)
