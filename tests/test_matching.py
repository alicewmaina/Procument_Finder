import pytest

from matching import compare, extract_models, group, normalise, rank, relevance, simplify_query


@pytest.mark.parametrize("name, expected", [
    ("Ubiquiti airMAX NanoStation AC Loco NS-5ACL", {"NS5ACL"}),
    ("Ubiquiti LiteBeam M5 LBE-M5-23", {"LBEM523"}),
    ("Grandstream GXP1625 IP Phone", {"GXP1625"}),
    ("MikroTik CRS326-24G-2S+RM 24 Gigabit Port Switch", {"CRS32624G2S+RM"}),
    ("Ubiquiti NanoBeam NBE-5AC-GEN2", {"NBE5ACGEN2"}),
])
def test_model_extraction(name, expected):
    assert expected <= extract_models(name)


@pytest.mark.parametrize("name", ["Cat6a Patch Cord 3m", "24-Port Gigabit Switch 1U", "RJ45 Cat6 connectors", "WiFi6 5GHz 16dBi"])
def test_specs_are_not_models(name):
    assert extract_models(name) == set()


def test_normalise_units_and_categories():
    assert normalise("PowerMAX+ 5 Mtr Cat.6A Patch Cord \u2013 PVC") == "powermax+ 5m cat6a patch cord pvc"
    assert normalise("Cat 6 cable 305 metres") == "cat6 cable 305m"


def test_same_product_by_model_number():
    v, _ = compare("Ubiquiti NanoStation locoM5", "Ubiquiti NanoStation LocoM5 MIMO Wireless Bridge")
    assert v == "same"


def test_different_model_numbers_never_merged():
    v, _ = compare("Ubiquiti NanoStation M5 (NSM5)", "Ubiquiti Nanostation locoM5")
    assert v == "different"
    v, _ = compare("Grandstream GWN7803 Pro 24-Port", "Grandstream GWN7803PH Pro 24-Port PoE++")
    assert v == "different"


def test_similar_without_model():
    v, _ = compare("Ubiquiti airMAX NanoStation Loco M5", "Ubiquiti NanoStation Loco M5 airMAX")
    assert v == "similar"


def test_different_lengths():
    v, _ = compare("Optace Patch Cord CAT6a UTP 3m", "Optace Patch Cord CAT6a UTP 5m")
    assert v == "different"


def test_relevance_requires_key_tokens():
    q = "Ubiquiti NanoStation Loco M5"
    assert relevance(q, "Ubiquiti Nanostation locoM5") == 1
    assert relevance(q, "Ubiquiti airMAX NanoStation locoM2") == 0      # wrong model family
    assert relevance("Cat6a patch cord 3m", "Extralink Fiber Patch Cord LC-LC 3M") == 0


def test_rank_orders_best_first():
    names = ["Optace Patch Cord CAT6a UTP 10m", "Optace Patch Cord CAT6a UTP 3m", "Fibre Patch Cord 3m"]
    ranked = rank("Cat6a patch cord 3m", names, name=lambda n: n, model=lambda n: None)
    assert ranked[0].endswith("3m") and "Fibre Patch Cord 3m" not in ranked


def test_simplify_query_drops_lengths():
    assert simplify_query("Cat6a patch cord 3m") == "Cat6a patch cord"
    assert simplify_query("Cat6 cable 305 metres x10") == "Cat6 cable"


def test_group_same_model_across_suppliers():
    offers = [{"name": "Ubiquiti NanoStation locoM5", "model": None},
              {"name": "Ubiquiti NanoStation LocoM5 MIMO", "model": "LOCOM5"},
              {"name": "Ubiquiti NanoStation M5 (NSM5)", "model": "NSM5"}]
    g = group(offers)
    assert g[0] == g[1] and g[0] is not None and g[2] is None


def test_model_written_with_a_space_is_same_product():
    v, reason = compare("Ubiquiti airMAX NanoStation Loco M5", "Ubiquiti NanoStation LocoM5 MIMO", None, "locoM5")
    assert v == "same" and "LOCOM5" in reason
    g = group([{"name": "Ubiquiti airMAX NanoStation Loco M5", "model": None},
               {"name": "Ubiquiti NanoStation LocoM5 MIMO", "model": "locoM5"}])
    assert g[0] == g[1] == 1


def test_cable_category_is_not_a_model_and_lengths_split_groups():
    assert extract_models("PowerMAX+ 5mtr Cat.6A S/FTP Patch Cord") == set()
    offers = [{"name": "Optace Ethernet Patch Cord, CAT6a UTP, 3m, Grey", "model": None},
              {"name": "Optace Ethernet Patch Cord, CAT6a UTP, 1m, Grey", "model": None},
              {"name": "PowerMAX+ 5mtr Cat.6A S/FTP Patch Cord - PVC", "model": "1201-06035"}]
    assert group(offers) == [None, None, None]
    assert compare("Cable 3m ABC-1234", "Cable 5m ABC-1234")[0] == "different"
