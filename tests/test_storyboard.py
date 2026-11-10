from factory.storyboard.planner import number_label


def test_number_label_units_and_dates():
    assert number_label("o jogo pode ocupar até 212 GB, dependendo") == "212 GB"
    assert number_label("começou à meia-noite do dia 10 de novembro") == "10 DE NOVEMBRO"
    assert number_label("um vinil triplo com 30 faixas") == "30 FAIXAS"


def test_number_label_ignores_names():
    assert number_label("No PlayStation 5, o pré-download") == ""
    assert number_label("A trilha de GTA 6 é do Justice") == ""
    assert number_label("No Xbox Series X|S a liberação") == ""
