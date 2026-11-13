import pytest

from prices import (EACH, detect_unit, kes, parse_price_text, per_unit, total_incl_vat, units_comparable)


@pytest.mark.parametrize("text, price, vat", [
    ("19,500.00 KSh +VAT", 19500, "excl"),
    ("6,000.00&nbsp;<span>KSh</span> <small>+VAT</small>", 6000, "excl"),
    ("KSh6,800Excl. Tax", 6800, "excl"),
    ("KSh 8,000 Excl. Tax", 8000, "excl"),
    ("700 KES", 700, None),
    ("8,900 KES Excl VAT", 8900, "excl"),
    ("KES 12,500 incl. VAT", 12500, "incl"),
    ("Ksh 1,250.50", 1250.50, None),
])
def test_price_formats(text, price, vat):
    p = parse_price_text(text)
    assert p.price == pytest.approx(price)
    assert p.vat_basis == vat
    assert p.no_public_price is False


def test_sale_price_takes_current_lower_price():
    p = parse_price_text("<del>KSh 10,000</del> <ins>KSh 8,500</ins> Excl. Tax")
    assert p.price == 8500 and p.vat_basis == "excl"


@pytest.mark.parametrize("text", ["Call for Quote", "Request a quote", "Price on request", "", None, "Out of stock"])
def test_no_public_price(text):
    p = parse_price_text(text)
    assert p.price is None and p.no_public_price is True


def test_vat_percentage_is_not_read_as_price():
    assert parse_price_text("KSh 5,000 +VAT 16%").price == 5000


def test_total_incl_vat_rules():
    assert total_incl_vat(1000, "excl", 0.16) == pytest.approx(1160)
    assert total_incl_vat(1000, "incl", 0.16) == 1000
    assert total_incl_vat(1000, "unknown", 0.16) is None      # never guessed
    assert total_incl_vat(None, "excl", 0.16) is None


def test_kes_display_rounds_to_whole_shillings():
    assert kes(11368.0) == "KES 11,368"
    assert kes(985.9999999) == "KES 986"
    assert kes(None) == "-"


@pytest.mark.parametrize("name, kind, size", [
    ("Cat6 UTP Cable 305m box", "metre", 305),
    ("CAT6A Solid Cable 305M Grey", "metre", 305),
    ("Fibre drop cable, drum of 1000 m", "metre", 1000),
    ("Cat6 cable per metre", "metre", 1),
    ("RJ45 connectors pack of 100", "piece", 100),
    ("RJ45 Boots 50 pcs", "piece", 50),
    ("Cat6a Patch Cord 3m", "each", 1),              # a length, not a quantity
    ("PowerMAX+ 5mtr Cat.6A S/FTP Patch Cord", "each", 1),
    ("Ubiquiti NanoStation Loco M5", "each", 1),
])
def test_units(name, kind, size):
    u = detect_unit(name)
    assert (u.kind, u.size) == (kind, size)


def test_units_comparable_and_per_unit():
    box = detect_unit("Cat6 UTP Cable 305m box")
    assert per_unit(30500, box) == pytest.approx(100)
    assert units_comparable([EACH, EACH])
    assert not units_comparable([EACH, box])
