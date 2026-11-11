from factory.verification.classify import confirmation_for
from factory.verification.factcheck import programmatic_check, quote_in_evidence


def _script(text, claims):
    return {"segments": [{"role": "hook", "text": text, "claims": claims}], "title": "t", "confirmation": "CONFIRMADO"}


def test_quote_must_exist(evidence):
    assert quote_in_evidence("Pre-loading for Grand Theft Auto VI begins on November 10", evidence[0]["text"]) == 100
    assert quote_in_evidence("Rockstar confirmed a PC version", evidence[0]["text"]) < 90


def test_valid_official_claim_passes(evidence):
    s = _script("O pré-download de GTA 6 começa em 10 de novembro.",
                [{"text": "pré-download começa 10/11", "evidence": "E1",
                  "quote": "Pre-loading for Grand Theft Auto VI begins on November 10", "status": "CONFIRMADO"}])
    assert programmatic_check(s, evidence) == []


def test_invented_quote_fails(evidence):
    s = _script("GTA 6 vai sair para PC em 2027.",
                [{"text": "PC 2027", "evidence": "E1", "quote": "GTA VI comes to PC in 2027", "status": "CONFIRMADO"}])
    issues = programmatic_check(s, evidence)
    assert any("citação não encontrada" in i for i in issues)
    assert any("2027" in i for i in issues)


def test_press_cannot_be_confirmed_and_needs_attribution(evidence):
    s = _script("GTA 6 vai rodar a 30 FPS nos consoles.",
                [{"text": "30 fps", "evidence": "E2", "quote": "GTA 6 will run at 30 frames per second on all consoles",
                  "status": "CONFIRMADO"}])
    issues = programmatic_check(s, evidence)
    assert any("acima do permitido" in i for i in issues)
    s2 = _script("GTA 6 vai rodar a 30 FPS nos consoles.",
                 [{"text": "30 fps", "evidence": "E2", "quote": "GTA 6 will run at 30 frames per second on all consoles",
                   "status": "REPORTADO"}])
    assert any("sem atribuição" in i for i in programmatic_check(s2, evidence))
    s3 = _script("Segundo a IGN, GTA 6 vai rodar a 30 FPS nos consoles.",
                 [{"text": "30 fps", "evidence": "E2", "quote": "GTA 6 will run at 30 frames per second on all consoles",
                   "status": "REPORTADO"}])
    assert programmatic_check(s3, evidence) == []


def test_unknown_evidence_and_no_claims(evidence):
    s = _script("Algo com número 42.", [{"text": "x", "evidence": "E9", "quote": "x", "status": "CONFIRMADO"}])
    issues = programmatic_check(s, evidence)
    assert any("evidência inexistente" in i for i in issues)


def test_confirmation_rules():
    off = {"tier": "official", "body": "x" * 100, "summary": "", "source_name": "Rockstar"}
    press = {"tier": "press", "body": "y" * 500, "summary": "", "source_name": "IGN"}
    agg = {"tier": "aggregator", "body": "", "summary": "", "source_name": "Estadão"}
    reddit = {"tier": "community", "body": "", "summary": "", "source_name": "r/GTA6"}
    assert confirmation_for([off, press], "news") == "CONFIRMADO"
    assert confirmation_for([press], "rumor") == "REPORTADO"
    assert confirmation_for([agg], "news") == "NAO_VERIFICADO"
    assert confirmation_for([reddit], "theory") == "TEORIA"


def test_attribution_carries_over_and_questions(evidence):
    q = "GTA 6 will run at 30 frames per second on all consoles"
    s = {"segments": [
        {"role": "hook", "tipo": "fato", "text": "Segundo a IGN, GTA 6 vai rodar a 30 FPS.",
         "claims": [{"text": "30", "evidence": "E2", "quote": q, "status": "REPORTADO"}]},
        {"role": "body", "tipo": "fato", "text": "E isso vale para todos os consoles da Sony e da Microsoft.",
         "claims": [{"text": "todos", "evidence": "E2", "quote": q, "status": "REPORTADO"}]},
        {"role": "body", "tipo": "humor", "text": "Imagina o Jason reclamando com o Cristiano Ronaldo do FPS.", "claims": []},
    ], "title": "t", "confirmation": "REPORTADO"}
    assert programmatic_check(s, evidence) == []


def test_humor_shape_rules():
    from factory.scriptwriter.writer import validate_shape
    base = {"title": "E se o Jason virasse influencer?", "description": "d",
            "segments": [{"tipo": "humor", "text": "Imagina o Jason " + "muito " * 20},
                         {"tipo": "fato", "text": "fato " * 15, "claims": [{"evidence": "E1"}]},
                         {"tipo": "fato", "text": "fato " * 15, "claims": [{"evidence": "E1"}]},
                         {"tipo": "humor", "text": "piada " * 20}]}
    assert validate_shape(base, []) == []
    no_fact = {**base, "segments": [dict(s, tipo="humor") for s in base["segments"]]}
    assert any("pelo menos 2 segmentos" in p for p in validate_shape(no_fact, []))


def test_numbers_across_languages_and_years():
    from factory.textutil import numbers_in
    assert "5.2" in numbers_in("vendeu 5,2 milhões") and "5.2" in numbers_in("sold 5.2 million")
    assert "1500" in numbers_in("1.500 pedidos") and "1500" in numbers_in("1,500 orders")
    from factory.verification.factcheck import _numbers_pt
    assert "1000" not in _numbers_pt("em novembro de dois mil e vinte e seis")


def test_attribution_anywhere_in_script(evidence):
    q = "GTA 6 will run at 30 frames per second on all consoles"
    s = {"segments": [
        {"tipo": "fato", "text": "Meu Deus, GTA 6 vai rodar a 30 FPS!", "claims": [{"evidence": "E2", "quote": q, "status": "REPORTADO"}]},
        {"tipo": "fato", "text": "Segundo a IGN, isso vale em todos os consoles.", "claims": [{"evidence": "E2", "quote": q, "status": "REPORTADO"}]},
    ], "title": "t", "confirmation": "REPORTADO"}
    assert programmatic_check(s, evidence) == []
