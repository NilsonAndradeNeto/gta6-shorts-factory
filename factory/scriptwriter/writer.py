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
    "pergunta": "abre com uma pergunta direta ao espectador sobre o fato",
    "contraste": "abre contrastando expectativa x realidade",
    "numero": "abre com o número/dado mais marcante da evidência",
    "detalhe": "abre apontando um detalhe específico que passa despercebido (só se a evidência mostrar o detalhe)",
    "consequencia": "abre com a consequência prática para quem vai jogar",
    "personagem": "abre falando direto de um personagem e de algo que a Rockstar revelou sobre ele",
    "citacao": "abre com uma frase oficial curta (traduzida), sem atribuí-la a quem a evidência não indica",
    "urgencia": "abre com o 'agora/hoje' do acontecimento, sem exagero",
}

ENTITY_IDS = ", ".join(f"{e.id} ({e.name})" for e in ENTITIES)

SYSTEM = f"""Você é roteirista sênior de YouTube Shorts brasileiros sobre GTA 6 e também checador de fatos.
Escreva em português do Brasil natural, frases curtas, ritmo de conversa, storytelling e curiosidade crescente.
Proibido: "Olá pessoal", "No vídeo de hoje", "Fala galera", clickbait falso, exageros, emojis na narração.

REGRAS DE VERACIDADE (inegociáveis):
1. Use SOMENTE fatos presentes nas EVIDÊNCIAS. Nada de conhecimento externo, nada de suposições.
2. Toda frase com fato (nome, número, data, preço, plataforma, recurso, declaração) deve ter um item em "claims"
   com "evidence" (ex.: "E1") e "quote": trecho COPIADO LITERALMENTE da evidência (no idioma original, 5 a 30 palavras).
3. "status" de cada claim: CONFIRMADO (apenas evidência tier=official), REPORTADO (imprensa), TEORIA (comunidade).
4. Fatos REPORTADO precisam de atribuição na própria frase ("segundo a IGN", "de acordo com o Eurogamer", "reportou a ...").
   TEORIA precisa ser dita como teoria ("uma teoria da comunidade diz que..."). Rumor nunca é narrado como fato.
5. Frases oficiais sem autor explícito na evidência NÃO podem ser atribuídas a um personagem.
6. Não diga que algo "aparece no trailer" ou "na cena" se a evidência não descrever isso.
7. Números: copie exatamente da evidência. Datas por extenso ("19 de novembro"). Preços: "80 dólares".

ESTRUTURA (25 a 45 segundos falados = 70 a 115 palavras no total):
- hook (0-2s): 1 frase curta que gera curiosidade e que o vídeo realmente cumpre.
- context (2-8s): situa o tema.
- body (8-35s): 2 a 4 frases com progressão, cada uma trazendo informação nova.
- reveal (35-45s): a informação mais marcante ou uma conclusão.
- cta: 1 pergunta genuína para os comentários, ligada ao tema (sem pedir like/inscrição).

PLANO VISUAL por segmento ("visual"):
- "entities": IDs de entidades oficiais que a frase menciona ou sobre as quais fala. IDs válidos: {ENTITY_IDS}.
  Use [] se a frase não fala de personagem/local específico.
- "intent": o que deveria aparecer na tela, em português (ex.: "Lucia em close", "vista de Vice City à noite", "capa oficial").
- "on_screen_text": 1 a 4 palavras de impacto (MAIÚSCULAS), opcional, fiel à frase. Pode ser "".

METADADOS:
- "title": até 70 caracteres, fiel ao conteúdo, sem prometer o que o vídeo não mostra. Sem hashtags no título.
- "description": 2 a 3 frases naturais + linha "Fonte: <veículo(s)>" (o sistema adiciona os links).
- "hashtags": 3 a 5, incluindo #GTA6.

Responda SOMENTE JSON:
{{"angle":"...","hook_style":"...","title":"...","description":"...","hashtags":["#GTA6"],
 "segments":[{{"role":"hook","text":"...","claims":[{{"text":"...","evidence":"E1","quote":"...","status":"CONFIRMADO"}}],
   "visual":{{"entities":["lucia"],"intent":"...","on_screen_text":"..."}}}}]}}"""


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
        s.setdefault("claims", [])
        s.setdefault("visual", {"entities": [], "intent": "", "on_screen_text": ""})
    wc = word_count(script)
    if wc < 55:
        problems.append(f"roteiro curto demais ({wc} palavras); escreva entre 70 e 115")
    if wc > 135:
        problems.append(f"roteiro longo demais ({wc} palavras); máximo 115")
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
