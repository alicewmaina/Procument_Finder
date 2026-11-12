"""Tier 2/3: OpenStreetMap discovery, website checks, PPRA debarment, ratings, price history."""

import asyncio
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import config
import db
import debarred
import discover
import ratings
import search as search_mod
import sitecheck
from adapters.base import Fetcher
from app import app
from fixture_source import make_transport
from search import SearchRequest, run_search

from conftest import FIXTURES


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def directory():
    """Nairobi directory and PPRA list loaded from the saved pages."""
    assert run(debarred.refresh())["status"] == "ok"
    result = run(discover.refresh("Nairobi"))
    assert result["status"] == "ok"
    return result


async def check(url):
    async with httpx.AsyncClient(transport=make_transport()) as c:
        return await sitecheck.check(url, Fetcher(c, config.USER_AGENT, 0))


# ---- Discovery ----------------------------------------------------------------------------
def test_directory_from_openstreetmap(directory):
    assert directory["count"] == 104                 # real Nairobi data saved on 12 Nov 2026
    state = discover.run_state("Nairobi")
    assert state["status"] == "ok" and state["label"] == "Nairobi" and not state["stale"]
    rows = discover.suppliers_near("Nairobi")
    dostum = next(r for r in rows if r["name"].startswith("Dostum"))
    assert dostum["kind"] == "IT company" and dostum["website"] == "https://www.dostum.co.ke/"
    assert dostum["osm_url"].startswith("https://www.openstreetmap.org/") and dostum["lat"]


def test_location_lookup_cached(directory):
    loc = run(discover.locate("Nairobi"))
    assert loc["area_id"] == 3609185096 and db.one("SELECT data FROM places WHERE query = 'nairobi'")


def test_unknown_place_reported():
    out = run(discover.refresh("Atlantis"))
    assert out["status"] == "error" and "wasn't found" in out["detail"]
    assert discover.run_state("Atlantis")["status"] == "error"


def test_overpass_falls_back_to_second_server(monkeypatch):
    monkeypatch.setattr(config, "SUPPLIER_SOURCE", "live")
    monkeypatch.setattr(config, "OVERPASS_URLS", ["https://op1.test/api", "https://op2.test/api"])
    with respx.mock:
        respx.post("https://op1.test/api").mock(return_value=httpx.Response(502))
        respx.post("https://op2.test/api").mock(return_value=httpx.Response(200, json={"elements": [{"type": "node", "id": 1}]}))
        assert run(discover.overpass("q")) == [{"type": "node", "id": 1}]


def test_tier1_suppliers_not_duplicated():
    rows = [discover.row_from_element({"type": "node", "id": 9, "lat": -1.28, "lon": 36.8,
                                       "tags": {"name": "Optace", "shop": "computer", "website": "https://optacenetworks.com"}}, "nairobi")]
    assert discover.save_rows(rows) == 0


def test_add_supplier_validation():
    with pytest.raises(discover.DiscoverError):
        discover.add_supplier("CTC", "https://ctcsolutions.co.ke", "Nairobi")
    discover.add_supplier("Some Shop", "shop.example.co.ke", "Nairobi")
    with pytest.raises(discover.DiscoverError, match="already"):
        discover.add_supplier("Some Shop", "https://shop.example.co.ke", "Nairobi")


# ---- Website checks ---------------------------------------------------------------------------
def test_parked_site_detected():
    probe = run(check("https://www.dostum.co.ke/"))
    assert probe["reachable"] and probe["parked"] and not sitecheck.readable(probe)


def test_redirect_to_other_business_detected():
    probe = run(check("https://www.bnt.co.ke/"))
    assert probe["reachable"] and not probe["same_site"]
    assert "different site" in sitecheck.summary(probe)


def test_unreachable_site():
    probe = run(check("http://www.dynavolt.co.ke/"))
    assert not probe["reachable"] and "didn't load" in sitecheck.summary(probe)


WOO_ITEM = {"id": 1, "name": "Ubiquiti NanoStation Loco M5", "sku": "LOCOM5", "permalink": "https://shop.example.co.ke/p/locom5",
            "prices": {"price": "950000", "currency_code": "KES", "currency_minor_unit": 2, "price_range": None},
            "price_html": "<span>KSh 9,500.00</span>", "is_in_stock": True, "stock_availability": {"text": ""}, "images": []}


def _woo_shop(m, host="shop.example.co.ke", items=(WOO_ITEM,)):
    m.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(404))
    m.get(f"https://{host}/").mock(return_value=httpx.Response(
        200, text="<html><title>Example Networks</title><link href='/wp-content/plugins/woocommerce/x.css'>"
                  "<p>Authorised Ubiquiti distributor in Kenya. MikroTik and Cisco too.</p>" + "x" * 300 + "</html>"))
    m.get(url__startswith=f"https://{host}/wp-json/wc/store/v1/products").mock(return_value=httpx.Response(200, json=list(items)))


def test_woocommerce_shop_detected(monkeypatch):
    monkeypatch.setattr(config, "SUPPLIER_SOURCE", "live")
    with respx.mock(assert_all_called=False) as m:
        _woo_shop(m)

        async def go():
            async with httpx.AsyncClient() as c:
                return await sitecheck.check("https://shop.example.co.ke/", Fetcher(c, config.USER_AGENT, 0))
        probe = run(go())
    assert probe["platform"] == "woocommerce" and probe["currency"] == "KES" and sitecheck.readable(probe)
    assert "Ubiquiti" in probe["partner_brands"] and probe["https"]


# ---- PPRA debarment -------------------------------------------------------------------------------
def test_debarred_list_parsed_and_matched():
    assert run(debarred.refresh()) == {"status": "ok", "count": 8}
    assert debarred.match("JOFRIM E.A. LTD")["name"] == "Jofrim E A Limited"
    assert debarred.match("Network Options & Solutions") is None


def test_debarred_supplier_blocked():
    run(debarred.refresh())
    r = ratings.rate({"key": "x", "name": "Jofrim E A Ltd", "website": "https://jofrim.co.ke"})
    assert r["blocked"] and r["tier"] is None and r["score"] == 0 and "debarred" in r["flags"][0]


# ---- Ratings ------------------------------------------------------------------------------------------
GOOD_PROBE = {"reachable": True, "https": True, "same_site": True, "parked": False, "platform": "none", "partner_brands": ["Cisco"]}


def test_tier2_needs_a_verified_website():
    base = {"key": "osm:node/1", "name": "Good IT Ltd", "website": "https://good.co.ke/", "lat": -1.2, "lon": 36.8,
            "address": "Moi Avenue", "phone": "+254 700 000000", "email": "info@good.co.ke", "opening_hours": "Mo-Fr 08:00-17:00"}
    assert ratings.rate(base)["tier"] == 3                                   # website not checked yet
    verified = ratings.rate({**base, "probe": GOOD_PROBE})
    assert verified["tier"] == 2 and verified["label"] == "Verified"
    parked = ratings.rate({**base, "probe": {**GOOD_PROBE, "parked": True}})
    assert parked["tier"] == 3 and "Parked or placeholder website" in parked["flags"]


def test_rating_points_are_explained_and_missing_evidence_is_neutral():
    r = ratings.rate({"key": "osm:node/2", "name": "No Web Shop", "lat": -1.2, "lon": 36.8, "phone": "0700"})
    assert all("text" in x and "points" in x for x in r["reasons"])
    na = [x for x in r["reasons"] if not x["assessed"]]
    assert any("comparisons" in x["text"] for x in na)                     # no history: not held against them
    assert r["evidence"] < 100 and 0 <= r["score"] <= 100


def test_tier1_rating_stays_tier1():
    s = next(s for s in config.SUPPLIERS if s.key == "optace")
    r = ratings.rate_tier1(s)
    assert r["tier"] == 1 and r["label"] == "Trusted" and r["score"] > 0


def test_low_price_flag():
    offers = [{"group": 1, "supplier_key": k, "total_incl_vat": t} for k, t in [("a", 10000), ("b", 10400), ("c", 5000)]]
    ratings.flag_low_prices(offers)
    assert "Unusually low" in offers[2]["flags"][0] and "flags" not in offers[0]


def test_competitiveness_learnt_from_searches(directory):
    for q in ["Ubiquiti NanoStation Loco M5", "nanostation", "Ubiquiti NanoStation M5"]:
        run(run_search(SearchRequest(item=q), "t"))
    ratings.clear_cache()
    st = ratings.supplier_stats()
    assert st["ctc_solutions"]["groups"] >= 1 and st["ctc_solutions"]["fetch_all"] >= 3


# ---- Search with Tier 2/3 ---------------------------------------------------------------------------------
def test_search_lists_nearby_suppliers_with_ratings_and_distance(directory):
    view = run(run_search(SearchRequest(item="Ubiquiti NanoStation Loco M5"), "t"))
    assert view["place"] == "Nairobi" and view["nearby_total"] == 104
    n = view["nearby"][0]
    assert n["rating"]["reasons"] and n["tier"] in (2, 3) and n["distance_km"] is not None
    assert all(o["rating"] for o in view["offers"])
    checked = db.one("SELECT COUNT(*) AS n FROM directory WHERE probed_at != ''")["n"]
    assert 0 < checked <= config.MAX_NEW_SITE_CHECKS                         # a few websites checked per search


def test_tier2_shop_prices_in_search(monkeypatch, directory):
    monkeypatch.setattr(config, "SUPPLIER_SOURCE", "live")
    monkeypatch.setattr(search_mod, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(config, "MAX_NEW_SITE_CHECKS", 1)
    discover.add_supplier("Example Networks", "https://shop.example.co.ke", "Nairobi", phone="+254 700 111222",
                          email="sales@example.co.ke")
    handler = make_transport().handler
    with respx.mock(assert_all_called=False) as m:
        for host in ["network-options.co.ke", "ctcsolutions.co.ke", "optacenetworks.com"]:
            m.route(host=host).mock(side_effect=handler)
        _woo_shop(m)
        view = run(run_search(SearchRequest(item="Ubiquiti NanoStation Loco M5"), "t"))
    ex = [o for o in view["offers"] if o["supplier_key"] == "web:shop.example.co.ke"]
    assert ex, "the added supplier's shop should be searched"
    o = ex[0]
    assert o["price"] == 9500 and o["vat_basis"] == "unknown" and o["total_incl_vat"] is None   # VAT never guessed
    assert o["tier"] in (2, 3) and o["rating"]["label"] in ("Verified", "Unverified")
    assert any(n["has_offers"] for n in view["nearby"] if n["id"] == "web:shop.example.co.ke")


def test_price_change_since_last_seen(directory):
    view = run(run_search(SearchRequest(item="Ubiquiti NanoStation Loco M5"), "t"))
    ctc = next(o for o in view["offers"] if o["supplier_key"] == "ctc_solutions")
    db.execute("INSERT INTO price_history (url, supplier_key, name, price, total, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
               (ctc["url"], "ctc_solutions", ctc["name"], 10000, 11600, "2026-11-01T10:00:00+00:00"))
    db.execute("DELETE FROM price_history WHERE fetched_at > '2026-11-02'")
    db.clear_cache()
    view = run(run_search(SearchRequest(item="Ubiquiti NanoStation Loco M5"), "t"))
    ctc = next(o for o in view["offers"] if o["supplier_key"] == "ctc_solutions")
    assert ctc["price_change"]["pct"] == -10.0 and ctc["price_change"]["price"] == 10000


# ---- Pages --------------------------------------------------------------------------------------------------
def test_suppliers_page_and_add(directory):
    with TestClient(app) as c:
        html = c.get("/suppliers").text
        assert "Tier 1 · Trusted" in html and "Nearby suppliers" in html and "OpenStreetMap contributors" in html
        assert "Why " in html and "PPRA debarred list: 8 firms" in html
        r = c.post("/suppliers/add", data={"name": "Jofrim E A Ltd", "website": "jofrim.example.co.ke", "location": "Nairobi"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert "debarred list, so it&#39;s blocked" in c.get(r.headers["location"]).text
        r = c.post("/suppliers/add", data={"name": "Again", "website": "https://ctcsolutions.co.ke", "location": "Nairobi"})
        assert "already a Tier 1 supplier" in r.text


def test_rfq_for_nearby_supplier(directory):
    row = discover.suppliers_near("Nairobi")[0]
    with TestClient(app) as c:
        html = c.get(f"/rfq?supplier={row['id']}").text
    assert row["name"].replace("&", "&amp;") in html


def test_dashboard_mentions_nearby_suppliers(directory):
    with TestClient(app) as c:
        html = c.get("/").text
    assert "104 nearby suppliers" in html
