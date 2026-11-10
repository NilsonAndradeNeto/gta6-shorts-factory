"""Entidades oficiais de GTA VI: nomes, apelidos, pronúncia pt-BR e pastas de assets.

Somente entidades apresentadas oficialmente pela Rockstar (site /VI e kit de mídia).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from factory.textutil import norm


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    kind: str                      # character|place|game|org|vehicle
    aliases: tuple[str, ...] = ()
    asset_keys: tuple[str, ...] = ()   # prefixos de pasta/arquivo no kit oficial
    say: str | None = None         # grafia fonética para o TTS
    related: tuple[str, ...] = field(default=())


ENTITIES: tuple[Entity, ...] = (
    Entity("jason", "Jason Duval", "character", ("jason", "jason duval"), ("Jason_Duval", "Jason_and_Lucia", "Jason_Lucia"), "Djêisson Duvál", ("lucia",)),
    Entity("lucia", "Lucia Caminos", "character", ("lucia", "lucía", "lucia caminos"), ("Lucia_Caminos", "Jason_and_Lucia", "Jason_Lucia"), "Lúcia Camínos", ("jason",)),
    Entity("jason_lucia", "Jason e Lucia", "character", ("jason e lucia", "jason and lucia", "jason & lucia"), ("Jason_and_Lucia", "Jason_Lucia"), "Djêisson e Lúcia"),
    Entity("cal", "Cal Hampton", "character", ("cal hampton", "cal"), ("Cal_Hampton",), "Kéu Rémpton"),
    Entity("boobie", "Boobie Ike", "character", ("boobie", "boobie ike"), ("Boobie_Ike",), "Búbi Áique", ("drequan", "only_raw")),
    Entity("drequan", "Dre'Quan Priest", "character", ("dre quan", "drequan", "dre'quan", "dre quan priest"), ("DreQuan_Priest",), "Drê-Kuân Príst", ("boobie", "real_dimez")),
    Entity("real_dimez", "Real Dimez", "character", ("real dimez", "bae luxe", "roxy"), ("Real_Dimez",), "Ríal Dáimes", ("drequan",)),
    Entity("raul", "Raul Bautista", "character", ("raul", "raul bautista"), ("Raul_Bautista",), "Raúl Bautísta"),
    Entity("brian", "Brian Heder", "character", ("brian", "brian heder", "lori"), ("Brian_Heder",), "Bráian Réder"),
    Entity("vice_city", "Vice City", "place", ("vice city",), ("Vice_City",), "Váice Síti"),
    Entity("leonida", "Leonida", "place", ("leonida", "estado de leonida"), ("Vice_City", "Leonida_Keys", "Grassrivers", "Port_Gellhorn", "Ambrosia", "Mount_Kalaga", "Official_Cover_Art"), "Leônida"),
    Entity("leonida_keys", "Leonida Keys", "place", ("leonida keys", "the keys"), ("Leonida_Keys",), "Leônida Kíz"),
    Entity("grassrivers", "Grassrivers", "place", ("grassrivers",), ("Grassrivers",), "Grás Rívers"),
    Entity("port_gellhorn", "Port Gellhorn", "place", ("port gellhorn",), ("Port_Gellhorn",), "Port Guélhorn"),
    Entity("ambrosia", "Ambrosia", "place", ("ambrosia",), ("Ambrosia",), "Ambrósia"),
    Entity("mount_kalaga", "Mount Kalaga", "place", ("mount kalaga", "kalaga"), ("Mount_Kalaga",), "Máunt Kalága"),
    Entity("only_raw", "Only Raw Records", "org", ("only raw", "only raw records"), ("Boobie_Ike", "DreQuan_Priest", "Real_Dimez"), "Ônli Ró Récords"),
    Entity("gta6", "GTA 6", "game", ("gta 6", "gta vi", "grand theft auto vi", "gta6"), ("Official_Cover_Art", "Jason_and_Lucia"), "gê tê á seis"),
    Entity("ultimate_edition", "Ultimate Edition", "game", ("ultimate edition", "edicao ultimate"), ("ULTIMATE_EDITION",), "Últimeit Edíchon"),
    Entity("vintage_pack", "Vintage Vice City Pack", "game", ("vintage vice city",), ("VINTAGE_VICE_CITY",), "Víntage Váice Síti Pack"),
)

BY_ID = {e.id: e for e in ENTITIES}

# Pronúncia de termos frequentes (aplicada só ao texto falado; legenda mantém a grafia real).
LEXICON: dict[str, str] = {
    "GTA VI": "gê tê á seis", "GTA 6": "gê tê á seis", "GTA V": "gê tê á cinco", "GTA 5": "gê tê á cinco",
    "GTA IV": "gê tê á quatro", "GTA": "gê tê á", "Grand Theft Auto VI": "Grând Théft Óto seis",
    "Grand Theft Auto": "Grând Théft Óto",
    "Rockstar Games": "Róckstar Guêimes", "Rockstar": "Róckstar", "Take-Two": "Têik Tchú", "Newswire": "Niúsuáier",
    "Liberty City": "Líberti Síti", "trailer": "trêiler", "trailers": "trêilers", "gameplay": "guêimplei",
    "PS5 Pro": "Plêistêichon cinco Pro", "PS5": "Plêistêichon cinco", "PlayStation 5": "Plêistêichon cinco",
    "PlayStation": "Plêistêichon", "Xbox Series X|S": "Équis bóx Séries", "Xbox Series": "Équis bóx Séries",
    "Xbox One": "Équis bóx Uãn", "Xbox": "Équis bóx", "FPS": "éfe pê ésse", "fps": "éfe pê ésse", "PC": "pê cê",
    "DualSense": "Dual Sense", "Strauss Zelnick": "Stráuss Zélnick", "Mudslide": "Mâd-sláide", "DWNPLY": "Daun Plei",
    "pre-load": "pré-carregamento", "preload": "pré-carregamento", "online": "ônlain", "Online": "Ônlain",
    "easter egg": "íster égue", "easter eggs": "íster égues", "Ultimate Edition": "Últimeit Edíchon",
    "Standard Edition": "Stândard Edíchon", "Vice City": "Váice Síti", "Leonida": "Leônida",
    "GMT": "gê ême tê", "GB": "gigas", "Justice": "Jâstiss", "Grammy": "Grémi", "Gaspard Augé": "Gaspár Ojê",
    "Xavier de Rosnay": "Gzaviê de Rosnê", "Flower in a Hurricane": "Fláuer in â Rárriquein",
    "Tame Impala": "Têim Impála", "SZA": "Ésse Zí Êi", "King Gizzard and the Lizard Wizard": "Kin Guízard én dâ Lízard Uízard",
    "The Festival": "dâ Féstival", "Mana Wynwood": "Mâna Uínwud", "Miami": "Maiâmi", "Keys": "Kíz", "Lori": "Lóri",
}
for _e in ENTITIES:
    if _e.say:
        LEXICON.setdefault(_e.name, _e.say)


def find_entities(text: str) -> list[str]:
    """IDs das entidades mencionadas num texto (busca por apelidos normalizados, palavra inteira)."""
    t = f" {norm(text)} "
    found: list[str] = []
    for e in ENTITIES:
        for a in e.aliases:
            if f" {norm(a)} " in t:
                found.append(e.id)
                break
    if "jason" in found and "lucia" in found and "jason_lucia" not in found:
        found.append("jason_lucia")
    return found


def known_names() -> set[str]:
    names = set()
    for e in ENTITIES:
        names.add(norm(e.name))
        names.update(norm(a) for a in e.aliases)
    return names
