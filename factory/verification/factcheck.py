"""Checagem factual do roteiro antes da produção.

Camada 1 (programática, determinística):
  - cada claim aponta para evidência existente;
  - a citação ("quote") existe de fato no texto da evidência (fuzzy >= 90);
  - números citados na frase aparecem na citação/evidência;
  - status do claim não excede o tier da evidência;
  - frases REPORTADO têm atribuição; TEORIA tem marcação de teoria;
  - toda frase com número/nome próprio relevante está coberta por um claim;
  - nomes próprios de GTA usados no roteiro existem nas evidências ou no registro oficial.
Camada 2 (LLM juiz, temperatura 0): cada claim é sustentado pela citação? A narração
exagera a certeza? O título promete algo que o roteiro não entrega?
"""
from __future__ import annotations

import json
import re
from typing import Any

from rapidfuzz import fuzz

from factory.entities import known_names
from factory.llm import LLM, LLMError
from factory.log import get_logger
from factory.textutil import norm, numbers_in

log = get_logger("factcheck")

TIER_MAX = {"official": "CONFIRMADO", "press": "REPORTADO", "aggregator": "REPORTADO", "community": "TEORIA"}
ORDER = {"TEORIA": 1, "REPORTADO": 2, "CONFIRMADO": 3}
ATTRIBUTION = re.compile(r"\b(segundo|de acordo com|conforme|report|publicou|afirmou|diz(em)?|disse|informou|"
                         r"revelou|apontou|teria|teriam|rumor|suposta|vazamento|noticiou|contou)", re.I)
THEORY = re.compile(r"\b(teoria|teorias|especula|f[ãa]s acreditam|comunidade|hip[óo]tese|talvez|pode ser)", re.I)
# números "inofensivos" que não precisam de evidência
SAFE_NUMBERS = {"6", "1", "2", "3", "4", "5", "0"}
NUM_WORDS = {"um": "1", "uma": "1", "dois": "2", "duas": "2", "três": "3", "tres": "3", "quatro": "4", "cinco": "5",
             "seis": "6", "sete": "7", "oito": "8", "nove": "9", "dez": "10", "trinta": "30", "sessenta": "60",
             "cem": "100"}   # "mil" fica de fora: aparece em anos por extenso ("dois mil e vinte e seis")

IMAGINATION = re.compile(r"\b(imagina|imagine|e se|já pensou|ja pensou|na minha cabeça|vamos supor|supondo|"
                         r"na imaginação|fingindo|de mentirinha|tipo assim)", re.I)

JUDGE_SYSTEM = """Você é um checador de fatos rigoroso de um canal de HUMOR sobre GTA 6.
O roteiro mistura segmentos "fato" (notícia real) e "humor" (ficção/piada assumida). Humor é permitido.
Para cada AFIRMAÇÃO (só dos segmentos fato), diga se é integralmente sustentada pela CITAÇÃO e pelo TEXTO DA
EVIDÊNCIA correspondente (tradução para o português é permitida).
- "supported": tudo o que a afirmação diz está na evidência.
- "partial": parte está na evidência, mas há acréscimo, exagero, atribuição errada ou certeza maior que a fonte.
- "unsupported": não está na evidência ou a contradiz.
Também avalie o roteiro inteiro e liste em script_issues SOMENTE problemas reais:
- fato narrado sem evidência fora dos trechos de humor; rumor/teoria dito como confirmado;
- trecho de humor que um espectador poderia confundir com notícia verdadeira (falta de "imagina/e se" ou de absurdo evidente);
- algo inventado sobre PESSOA REAL dito como verdade, falas inventadas de pessoa real, ou piada ofensiva/difamatória;
- título/descrição que prometem o que o vídeo não entrega.
Exagero cômico assumido e cenários absurdos com personagens fictícios do jogo NÃO são problemas.
Responda JSON: {"claims":[{"i":0,"verdict":"supported","why":"..."}],
"script_issues":["..."], "title_ok":true, "overall":"pass|fail"}"""


# nomes de conhecimento geral que não exigem evidência (empresas, plataformas, lugares)
COMMON_NAMES = {norm(x) for x in (
    "GTA", "Rockstar", "Rockstar Games", "Take-Two", "Sony", "Microsoft", "Nintendo", "PlayStation", "Xbox", "Steam",
    "Epic", "PC", "YouTube", "TikTok", "Instagram", "Twitch", "Discord", "Brasil", "Estados Unidos", "Miami", "Flórida",
    "Florida", "Vice City", "Leonida", "Liberty City", "Los Santos", "San Andreas", "Grand Theft Auto", "Google",
    "Deus", "Meu Deus", "Nossa Senhora", "Jesus", "Greenwich", "GMT", "Japão", "Europa", "São Paulo", "Rio de Janeiro")}


def _norm_quote(s: str) -> str:
    return re.sub(r"\s+", " ", norm(s.replace("’", "'"))).strip()


def quote_in_evidence(quote: str, ev_text: str) -> float:
    q, t = _norm_quote(quote), _norm_quote(ev_text)
    if not q:
        return 0.0
    if q in t:
        return 100.0
    return float(fuzz.partial_ratio(q, t))


def _numbers_pt(text: str) -> set[str]:
    nums = numbers_in(text)
    for w in norm(text).split():
        if w in NUM_WORDS:
            nums.add(NUM_WORDS[w])
    return nums


def programmatic_check(script: dict[str, Any], evidence: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    ev_by_id = {e["id"]: e for e in evidence}
    all_ev_text = " ".join(e["text"] for e in evidence)
    ev_numbers = numbers_in(all_ev_text)
    ev_norm = norm(all_ev_text)
    names = known_names() | COMMON_NAMES
    for e in evidence:   # nomes dos veículos/fontes (ex.: "PlayStation Blog") podem ser citados na narração
        names |= {norm(e["source"])} | {w for w in norm(e["source"]).split() if len(w) > 2}
    # evidências cuja fonte é citada em QUALQUER ponto do roteiro (a atribuição vale para todo o texto)
    attributed: set[str] = set()
    for seg in script.get("segments", []):
        t = seg.get("text", "")
        for c in seg.get("claims", []) or []:
            e = ev_by_id.get(str(c.get("evidence", "")).strip())
            if e and (ATTRIBUTION.search(t) or (norm(e["source"]) and norm(e["source"]).split()[0] in norm(t))):
                attributed.add(e["id"])

    for si, seg in enumerate(script.get("segments", [])):
        text = seg.get("text", "")
        claims = seg.get("claims", []) or []
        if seg.get("tipo") == "humor" and not claims:
            continue   # ficção assumida: o juiz avalia se está claramente marcada como imaginação
        claim_numbers: set[str] = set()
        for ci, c in enumerate(claims):
            tag = f"seg{si}.claim{ci}"
            e = ev_by_id.get(str(c.get("evidence", "")).strip())
            if e is None:
                issues.append(f"{tag}: evidência inexistente '{c.get('evidence')}'")
                continue
            q = c.get("quote", "")
            score = quote_in_evidence(q, e["text"])
            if score < 90:
                issues.append(f"{tag}: citação não encontrada literalmente em {e['id']} (similaridade {score:.0f}): '{q[:80]}'")
            st = str(c.get("status", "")).upper()
            if st not in ORDER:
                issues.append(f"{tag}: status inválido '{st}'")
            elif ORDER[st] > ORDER[TIER_MAX[e["tier"]]]:
                issues.append(f"{tag}: status {st} acima do permitido pela fonte ({e['tier']} -> {TIER_MAX[e['tier']]})")
            if st == "REPORTADO":
                cites_source = norm(e["source"]) and norm(e["source"]).split()[0] in norm(text)
                if ATTRIBUTION.search(text) or cites_source or text.strip().endswith("?"):
                    attributed.add(e["id"])
                elif e["id"] not in attributed:
                    issues.append(f"seg{si}: fato REPORTADO sem atribuição à fonte na narração: '{text[:80]}'")
            if st == "TEORIA" and not THEORY.search(text):
                issues.append(f"seg{si}: TEORIA narrada sem deixar claro que é teoria: '{text[:80]}'")
            claim_numbers |= numbers_in(q) | numbers_in(e["text"])
        # números na narração precisam estar sustentados
        for n in _numbers_pt(text) - SAFE_NUMBERS:
            if n not in claim_numbers and n not in ev_numbers:
                issues.append(f"seg{si}: número '{n}' não aparece nas evidências: '{text[:80]}'")
            elif not claims and n not in SAFE_NUMBERS:
                issues.append(f"seg{si}: frase com número '{n}' sem claim/evidência associada")
        # nomes próprios: palavras capitalizadas fora do início de frase
        for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zà-ú]+(?:\s+[A-Z][a-zà-ú']+)*)", text):
            cand = norm(m.group(1))
            if not cand or len(cand) < 3:
                continue
            if cand in names or cand in ev_norm:
                continue
            if all(w in ev_norm or w in names for w in cand.split()):
                continue
            issues.append(f"seg{si}: nome '{m.group(1)}' não consta nas evidências")
    if script.get("confirmation") == "TEORIA":
        hook = script["segments"][0]["text"] if script.get("segments") else ""
        if not THEORY.search(hook + " " + script.get("title", "")):
            issues.append("pauta TEORIA: gancho/título precisam deixar claro que é teoria")
    return issues


def llm_judge(llm: LLM, script: dict[str, Any], evidence: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    claims = []
    for seg in script.get("segments", []):
        for c in seg.get("claims", []) or []:
            claims.append({"i": len(claims), "frase_narrada": seg.get("text", ""), "afirmacao": c.get("text", ""),
                           "evidencia": c.get("evidence"), "citacao": c.get("quote", ""), "status": c.get("status")})
    ev_txt = "\n\n".join(f"[{e['id']}] tier={e['tier']} fonte={e['source']}\n{e['text'][:3500]}" for e in evidence)
    narr = "\n".join(f"[{s.get('tipo', 'fato')}] {s.get('text', '')}" for s in script.get("segments", []))
    user = (f"TÍTULO: {script.get('title')}\nDESCRIÇÃO: {script.get('description')}\nROTEIRO: {narr}\n\n"
            f"AFIRMAÇÕES:\n{json.dumps(claims, ensure_ascii=False)}\n\nEVIDÊNCIAS:\n{ev_txt}")
    out = llm.json(JUDGE_SYSTEM, user, purpose="factcheck", temperature=0.0, max_tokens=2500)
    issues = []
    for c in out.get("claims", []) if isinstance(out, dict) else []:
        if c.get("verdict") in ("unsupported", "partial"):
            idx = c.get("i")
            txt = claims[idx]["afirmacao"] if isinstance(idx, int) and 0 <= idx < len(claims) else "?"
            issues.append(f"juiz: afirmação '{txt[:90]}' -> {c.get('verdict')}: {c.get('why', '')[:160]}")
    for s in (out.get("script_issues") or []) if isinstance(out, dict) else []:
        if s and str(s).strip().lower() not in ("nenhum", "none", ""):
            issues.append(f"juiz(roteiro): {str(s)[:200]}")
    if isinstance(out, dict) and out.get("title_ok") is False:
        issues.append("juiz: título promete algo que o roteiro não entrega")
    if isinstance(out, dict) and out.get("overall") == "fail" and not issues:
        issues.append("juiz: reprovado sem detalhes")
    return issues, out if isinstance(out, dict) else {}


def check(llm: LLM, script: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    prog = programmatic_check(script, evidence)
    judge_issues: list[str] = []
    judge_raw: dict[str, Any] = {}
    if not prog:
        try:
            judge_issues, judge_raw = llm_judge(llm, script, evidence)
        except LLMError as e:
            judge_issues = [f"juiz indisponível: {e}"]
    n_claims = sum(len(s.get("claims", []) or []) for s in script.get("segments", []))
    if n_claims == 0:
        prog.append("roteiro sem nenhuma afirmação vinculada a evidência")
    passed = not prog and not judge_issues
    result = {"passed": passed, "programmatic": prog, "judge": judge_issues, "claims": n_claims,
              "judge_raw": judge_raw}
    log.info("Checagem factual: %s (%d claims, %d problemas)", "APROVADO" if passed else "REPROVADO",
             n_claims, len(prog) + len(judge_issues))
    return result
