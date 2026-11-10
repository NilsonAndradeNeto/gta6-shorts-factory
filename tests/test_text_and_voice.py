from factory.entities import find_entities
from factory.textutil import domain_of, norm, numbers_in
from factory.voice.normalize import normalize_for_tts, number_to_words, split_sentences


def test_number_to_words():
    assert number_to_words(19) == "dezenove"
    assert number_to_words(100) == "cem"
    assert number_to_words(2026) == "dois mil e vinte e seis"
    assert number_to_words(1500) == "mil e quinhentos"
    assert number_to_words(1234) == "mil duzentos e trinta e quatro"
    assert number_to_words(750_000_000) == "setecentos e cinquenta milhões"
    assert number_to_words(2_000_000) == "dois milhões"


def test_normalize_currency_and_lexicon():
    t = normalize_for_tts("GTA 6 custa US$ 80 no PS5.")
    assert "oitenta dólares no" in t
    assert "gê tê á seis" in t
    assert "Plêistêichon cinco" in t
    assert "$" not in t and "80" not in t


def test_normalize_percent():
    assert "trinta por cento" in normalize_for_tts("30%")


def test_split_sentences():
    s = split_sentences("Primeira frase. Segunda frase! Terceira?")
    assert s == ["Primeira frase.", "Segunda frase!", "Terceira?"]


def test_entities():
    ents = find_entities("Lucia Caminos e Jason chegam a Vice City em GTA VI")
    assert {"lucia", "jason", "jason_lucia", "vice_city", "gta6"} <= set(ents)
    assert find_entities("nenhuma entidade aqui") == []


def test_textutil():
    assert norm("Ação — GTA VI!") == "acao gta vi"
    assert domain_of("https://www.ign.com/x") == "ign.com"
    assert {"19", "2026"} <= numbers_in("November 19, 2026")
