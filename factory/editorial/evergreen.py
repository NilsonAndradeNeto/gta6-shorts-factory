"""Pautas atemporais (reserva) construídas SOMENTE a partir de textos oficiais da Rockstar.

São usadas quando não há notícias suficientes que passem nos critérios. Cada pauta aponta
para a evidência oficial capturada pelo coletor (páginas /VI e /VI/only-in-leonida).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from factory.config import settings
from factory.database.db import DB, now_iso
from factory.log import get_logger

log = get_logger("evergreen")

# (chave, título, foco/ângulo, entidades, páginas oficiais usadas como evidência)
EVERGREEN = [
    ("eg-jason-profile", "Quem é Jason Duval, segundo a Rockstar", "Passado de Jason e por que Lucia pode mudar a vida dele", ["jason", "lucia"], ["only_in_leonida"]),
    ("eg-lucia-profile", "Quem é Lucia Caminos, segundo a Rockstar", "Da Penitenciária de Leonida ao plano de Lucia; a ligação com Liberty City", ["lucia", "jason"], ["only_in_leonida"]),
    ("eg-cal-profile", "Cal Hampton, o paranoico de GTA 6", "O amigo de Jason que espiona a Guarda Costeira", ["cal", "jason"], ["only_in_leonida"]),
    ("eg-boobie-profile", "Boobie Ike, a lenda de Vice City", "Como Boobie transformou o passado nas ruas num império", ["boobie", "only_raw"], ["only_in_leonida"]),
    ("eg-drequan-profile", "Dre'Quan Priest e o sonho da música", "De traficante a magnata da música em Vice City", ["drequan", "only_raw"], ["only_in_leonida"]),
    ("eg-realdimez-profile", "Real Dimez: as rappers de GTA 6", "Amizade de colégio, hit viral e a gravadora Only Raw", ["real_dimez", "only_raw"], ["only_in_leonida"]),
    ("eg-raul-profile", "Raul Bautista, o assaltante de bancos", "Experiência, charme e imprudência em cada golpe", ["raul"], ["only_in_leonida"]),
    ("eg-brian-profile", "Brian Heder, o contrabandista dos Keys", "A era de ouro do contrabando e o acordo com Jason", ["brian", "jason", "leonida_keys"], ["only_in_leonida"]),
    ("eg-only-raw", "A gravadora que conecta três personagens", "Boobie, Dre'Quan e Real Dimez ligados pela Only Raw Records", ["only_raw", "boobie", "drequan", "real_dimez"], ["only_in_leonida"]),
    ("eg-premise", "A premissa oficial de GTA 6", "Jason e Lucia, um golpe que dá errado e uma conspiração em Leonida", ["jason_lucia", "leonida", "vice_city"], ["vi_home"]),
    ("eg-liberty-city-link", "A ligação de Lucia com Liberty City", "O sonho da mãe de Lucia desde os tempos de Liberty City", ["lucia"], ["only_in_leonida"]),
    ("eg-jason-brian-deal", "O acordo entre Jason e Brian", "Moradia de graça em troca de 'serviços' nos Keys", ["jason", "brian"], ["only_in_leonida"]),
    ("eg-cast-quotes", "As frases mais marcantes do site oficial", "Frases publicadas no site oficial, sem atribuí-las a quem não foi indicado", ["jason_lucia", "brian", "raul"], ["only_in_leonida"]),
]


def ensure_evergreen_topics(db: DB) -> int:
    """Cria/atualiza pautas atemporais com a evidência oficial mais recente."""
    cooldown = timedelta(days=int(settings().get("editorial.evergreen_cooldown_days", 30)))
    n = 0
    for key, title, angle, ents, pages in EVERGREEN:
        ev = []
        for pid in pages:
            row = db.one("SELECT * FROM news_items WHERE source_id=? AND tier='official'", pid)
            if row and row["body"]:
                ev.append({"id": f"E{len(ev) + 1}", "news_id": row["id"], "url": row["url"],
                           "source": "Rockstar Games (site oficial)", "tier": "official",
                           "published_at": None, "image_url": "", "text": row["body"][:9000]})
        if not ev:
            continue
        detail = {"kind": "evergreen", "angle": angle, "interest": 6, "retention": 6, "visual": 9, "sources": 1,
                  "newest": ""}
        row = db.one("SELECT id, state, used_at FROM topics WHERE cluster_key=?", key)
        if row is None:
            db.x("INSERT INTO topics(kind,cluster_key,title,confirmation,score,score_detail,evidence,entities,state,created_at)"
                 " VALUES ('evergreen',?,?,'CONFIRMADO',0,?,?,?,'DISCOVERED',?)",
                 key, title, json.dumps(detail, ensure_ascii=False), json.dumps(ev, ensure_ascii=False),
                 json.dumps(ents), now_iso())
            n += 1
        else:
            reopen = row["state"] == "USED" and row["used_at"] and \
                datetime.fromisoformat(row["used_at"]) < datetime.now(timezone.utc) - cooldown
            db.x("UPDATE topics SET evidence=?, score_detail=?, state=? WHERE id=?",
                 json.dumps(ev, ensure_ascii=False), json.dumps(detail, ensure_ascii=False),
                 "DISCOVERED" if reopen else row["state"], row["id"])
    return n
