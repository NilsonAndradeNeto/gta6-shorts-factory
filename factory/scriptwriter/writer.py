"""Geração de roteiro + plano visual + metadados num único JSON estruturado.

O LLM recebe SOMENTE as evidências coletadas. Cada afirmação factual precisa apontar
para uma evidência (E1, E2...) e trazer uma citação literal dessa evidência; isso permite
a checagem programática em factory.verification.factcheck.
"""
from __future__ import annotations

import json
import random
from typing import Any

from rapidfuzz import fuzz

from factory.database.db import DB
from factory.entities import BY_ID, ENTITIES
from factory.llm import LLM
from factory.log import get_logger
from factory.textutil import norm

log = get_logger("script")

HOOK_STYLES = {
    "e_se": "abre com um 'E se...' absurdo e engraçado que puxa a notícia real",
    "absurdo": "abre com uma situação ridícula e visual (ex.: alguém levando fechada de um flamingo em Vice City)",
    "pergunta_maluca": "abre com uma pergunta inesperada e engraçada ao espectador",
    "exagero": "abre exagerando de propósito a reação à notícia (drama cômico)",
    "personagem": "abre colocando um personagem oficial (Jason, Lucia, Brian...) numa situação cômica",
    "contraste": "abre com expectativa x realidade, em tom de piada",
    "confissao": "abre como se o narrador confessasse algo constrangedor ligado à notícia",
    "urgencia": "abre com um 'para tudo!' bem-humorado sobre o que aconteceu hoje",
}

ENTITY_IDS = ", ".join(f"{e.id} ({e.name})" for e in ENTITIES)

SYSTEM = f"""Você é roteirista de YouTube Shorts brasileiros de HUMOR sobre GTA 6: o canal pega uma notícia REAL
e transforma numa historinha engraçada, do primeiro ao último segundo. Linguagem de brasileiro jovem, gírias leves,
frases curtas, ritmo rápido, timing de piada, exagero cômico, escalada do absurdo, callback e punchline.
Proibido: "Olá pessoal", "No vídeo de hoje", "Fala galera", pedir like/inscrição, emojis na narração.

FORMATO "NOTÍCIA + ZOEIRA" (obrigatório):
- Cada segmento tem "tipo": "fato" (informação real) ou "humor" (história inventada, piada, cenário imaginário).
- A notícia real precisa aparecer com clareza (pelo menos 2 segmentos "fato"), costurada dentro da história.
- O humor pode inventar cenas, diálogos de personagens FICTÍCIOS do jogo (Jason, Lucia, Brian...) e situações absurdas.
- A primeira parte inventada precisa deixar claro que é imaginação: "imagina...", "e se...", "na minha cabeça...",
  "já pensou...", "vamos supor...". O espectador nunca pode confundir a piada com notícia.
- Pessoas reais (jogadores, artistas, executivos): só em situações claramente imaginárias, absurdas e inofensivas
  ("e se o Ronaldo...") — NUNCA inventar falas, crimes, polêmicas, vida pessoal, saúde ou política de gente real,
  e nunca afirmar como verdade algo sobre uma pessoa real que não esteja nas EVIDÊNCIAS.
- Nada de humor ofensivo (raça, religião, gênero, orientação, deficiência), nem violência gráfica ou sexual.
- Termine com uma punchline/virada que amarre a piada com o fato real, + uma pergunta engraçada para os comentários.

REGRAS DE VERACIDADE (valem para os segmentos "fato"):
1. Use SOMENTE fatos presentes nas EVIDÊNCIAS. Nada de conhecimento externo.
2. Cada frase "fato" com informação (nome, número, data, preço, plataforma, recurso, declaração) precisa de "claims"
   com "evidence" (ex.: "E1") e "quote": trecho COPIADO LITERALMENTE da evidência (no idioma original, 5 a 30 palavras).
3. "status" do claim: CONFIRMADO (apenas tier=official), REPORTADO (imprensa), TEORIA (comunidade).
4. REPORTADO precisa de atribuição na frase ("segundo a IGN", "de acordo com o Eurogamer").
   Rumor ou teoria nunca é dito como fato.
5. Segmentos "humor" não podem conter fatos sobre o jogo que não estejam nas evidências disfarçados de verdade
   (ex.: inventar recurso do jogo dentro da piada sem marcar como imaginação). Datas por extenso; preços "80 dólares".

ESTRUTURA (30 a 45 segundos falados = 80 a 120 palavras):
- hook (0-2s, tipo humor ou fato): a frase mais forte; tem que prender na hora.
- context: situa a notícia real.
- body (2 a 4 segmentos): alterna fato e zoeira, cada frase sobe o absurdo; nada de enrolação.
- reveal: virada/punchline amarrando com o fato.
- cta: pergunta engraçada e genuína para os comentários.

PLANO VISUAL por segmento ("visual"):
- "entities": IDs oficiais que a frase menciona ou sobre os quais fala. IDs válidos: {ENTITY_IDS}. [] se nenhum.
- "intent": o que deveria aparecer (ex.: "Jason com cara de surpreso", "Vice City à noite").
- "on_screen_text": 1 a 4 palavras de impacto (MAIÚSCULAS), pode ser engraçado. Pode ser "".

METADADOS:
- "title": até 70 caracteres, engraçado e fiel (se for cenário imaginário, use "E se..." ou deixe claro). Sem hashtags.
- "description": 2 a 3 frases: resuma a notícia real e diga que o vídeo traz uma história de humor/ficção.
- "hashtags": 3 a 5, incluindo #GTA6.

Responda SOMENTE JSON:
{{"angle":"...","hook_style":"...","title":"...","description":"...","hashtags":["#GTA6"],
 "segments":[{{"role":"hook","tipo":"humor","text":"...","claims":[],
   "visual":{{"entities":["jason"],"intent":"...","on_screen_text":"..."}}}},
  {{"role":"context","tipo":"fato","text":"...","claims":[{{"text":"...","evidence":"E1","quote":"...","status":"CONFIRMADO"}}],
   "visual":{{"entities":[],"intent":"...","on_screen_text":""}}}}]}}"""


def recent_hooks(db: DB, n: int = 16) -> list[tuple[str, str]]:
    out = []
    for r in db.q("SELECT hook_style, script FROM videos WHERE script IS NOT NULL ORDER BY created_at DESC LIMIT ?", n):
        try:
            seg = json.loads(r["script"])["segments"][0]["text"]
        except (KeyError, IndexError, json.JSONDecodeError, TypeError):
            continue
        out.append((r["hook_style"] or "", seg))
    return out


def choose_hook_style(db: DB, topic: dict[str, Any]) -> str:
    recent = [h for h, _ in recent_hooks(db, 8)]
    options = [h for h in HOOK_STYLES if h not in recent[:4]]
    ents = json.loads(topic.get("entities") or "[]")
    if not any(BY_ID.get(e) and BY_ID[e].kind == "character" for e in ents):
        options = [o for o in options if o != "personagem"] or options
    rnd = random.Random(f"{topic['id']}-{len(recent)}")
    options = options or list(HOOK_STYLES)
    try:
        from factory.analytics.metrics import hook_weights
        weights = {k: v for k, v in hook_weights(db).items() if k in options}
    except Exception:
        weights = {}
    if weights and rnd.random() < 0.7:            # explora o que funciona; 30% testa outros estilos
        return max(weights, key=weights.get)
    return rnd.choice(options)


def word_count(script: dict[str, Any]) -> int:
    return sum(len(s.get("text", "").split()) for s in script.get("segments", []))


def build_user_prompt(topic: dict[str, Any], hook_style: str, prev_hooks: list[str], feedback: str = "") -> str:
    ev = json.loads(topic["evidence"])
    detail = json.loads(topic.get("score_detail") or "{}")
    ev_txt = "\n\n".join(
        f"[{e['id']}] tier={e['tier']} fonte={e['source']} data={e.get('published_at') or 'n/d'}\n{e['text']}" for e in ev)
    angle = detail.get("angle", "")
    lines = [
        f"PAUTA: {topic['title']}",
        f"STATUS DA PAUTA: {topic['confirmation']} (tipo: {detail.get('kind', topic['kind'])})",
    ]
    if angle:
        lines.append(f"ÂNGULO SUGERIDO: {angle}")
    lines += [
        f"ESTILO DE GANCHO OBRIGATÓRIO: {hook_style} — {HOOK_STYLES[hook_style]}",
        "GANCHOS JÁ USADOS (não repita estrutura nem palavras):\n" + ("\n".join(f"- {h}" for h in prev_hooks[:10]) or "- nenhum"),
    ]
    if topic["confirmation"] == "REPORTADO":
        lines.append("ATENÇÃO: a pauta não tem confirmação oficial. Atribua cada fato ao veículo que o publicou.")
    if topic["confirmation"] == "TEORIA":
        lines.append("ATENÇÃO: trata-se de teoria/discussão da comunidade. Deixe isso explícito no gancho e no título.")
    if feedback:
        lines.append(f"CORREÇÕES OBRIGATÓRIAS DA VERSÃO ANTERIOR:\n{feedback}")
    lines.append(f"EVIDÊNCIAS:\n{ev_txt}")
    return "\n\n".join(lines)


def validate_shape(script: dict[str, Any], prev_hooks: list[str]) -> list[str]:
    problems = []
    segs = script.get("segments")
    if not isinstance(segs, list) or len(segs) < 4:
        return ["roteiro precisa de pelo menos 4 segmentos (hook, context, body..., reveal/cta)"]
    for s in segs:
        if not isinstance(s, dict) or not s.get("text"):
            problems.append("segmento sem texto")
            continue
        s.setdefault("claims", [])
        s.setdefault("visual", {"entities": [], "intent": "", "on_screen_text": ""})
        s["tipo"] = "humor" if str(s.get("tipo", "fato")).lower().startswith("humor") else "fato"
    fatos = [s for s in segs if isinstance(s, dict) and s.get("tipo") == "fato"]
    if len(fatos) < 2 or sum(len(s["claims"]) for s in fatos) < 2:
        problems.append("a notícia real precisa aparecer em pelo menos 2 segmentos 'fato' com claims/evidências")
    humor = [s for s in segs if isinstance(s, dict) and s.get("tipo") == "humor"]
    if not humor:
        problems.append("faltou o humor: inclua uma história/cenário engraçado (segmentos 'humor')")
    else:
        from factory.verification.factcheck import IMAGINATION
        if not IMAGINATION.search(humor[0].get("text", "")) and not IMAGINATION.search(script.get("title", "")):
            problems.append("o primeiro trecho inventado precisa deixar claro que é imaginação ('imagina', 'e se', 'já pensou')")
    wc = word_count(script)
    if wc < 60:
        problems.append(f"roteiro curto demais ({wc} palavras); escreva entre 80 e 120")
    if wc > 140:
        problems.append(f"roteiro longo demais ({wc} palavras); máximo 120")
    first = norm(segs[0].get("text", ""))
    for h in prev_hooks:
        if fuzz.token_set_ratio(first, norm(h)) >= 75:
            problems.append(f"gancho parecido demais com um anterior: '{h}'")
            break
    banned = ("ola pessoal", "no video de hoje", "fala galera", "e ai galera", "se inscreva", "deixa o like")
    full = norm(" ".join(s.get("text", "") for s in segs))
    for b in banned:
        if b in full:
            problems.append(f"expressão proibida: '{b}'")
    for k in ("title", "description"):
        if not script.get(k):
            problems.append(f"faltou '{k}'")
    if len(script.get("title", "")) > 95:
        problems.append("título longo demais")
    return problems


def write_script(db: DB, llm: LLM, topic: dict[str, Any], feedback: str = "", hook_style: str | None = None) -> dict[str, Any]:
    hook_style = hook_style or choose_hook_style(db, topic)
    prev = [h for _, h in recent_hooks(db)]
    user = build_user_prompt(topic, hook_style, prev, feedback)
    script: dict[str, Any] = {}
    for attempt in range(3):
        script = llm.json(SYSTEM, user, purpose="script", max_tokens=3500)
        if not isinstance(script, dict):
            script = {}
        problems = validate_shape(script, prev)
        if not problems:
            break
        log.info("Roteiro com problemas de forma (tentativa %d): %s", attempt + 1, problems)
        user = build_user_prompt(topic, hook_style, prev, (feedback + "\n" if feedback else "") + "\n".join(f"- {p}" for p in problems))
    else:
        raise ValueError("Roteiro não atendeu aos requisitos de forma: " + "; ".join(validate_shape(script, prev)))
    script["hook_style"] = script.get("hook_style") or hook_style
    script["topic_id"] = topic["id"]
    script["confirmation"] = topic["confirmation"]
    return script
