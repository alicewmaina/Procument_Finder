"""End-to-end (demo build, no sign-in): search, compare, choose, RFQ, history/CSV, practice mode, failures."""

import asyncio
import json
import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import config
import llm
import search as search_mod
from adapters.optace import OptaceAdapter
from app import app
from fixture_source import make_transport


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def search(client, **params) -> tuple[list[dict], dict]:
    """Run a search through the streaming API; return (all events, final view)."""
    r = client.get("/api/search", params=params)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert events[0]["type"] == "start" and events[-1]["type"] == "done"
    return events, events[-1]["view"]


def totals(view) -> set[int]:
    return {round(o["total_incl_vat"]) for o in view["offers"] if o["total_incl_vat"] is not None}


def by_supplier(view, key):
    return [o for o in view["offers"] if o["supplier_key"] == key]


# ---- Dashboard (demo: no sign-in) ------------------------------------------------------
def test_dashboard_opens_without_sign_in(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "What do you need to buy?" in r.text
    assert "Sign out" not in r.text and "Tester" in r.text
    assert 'href="/compare"' in r.text                 # Compare tab in the top bar
    assert client.get("/login").status_code == 404


def test_dashboard_shows_recent_searches(client):
    _, view = search(client, q="Ubiquiti NanoStation Loco M5")
    html = client.get("/").text
    assert "Recent searches" in html and f'href="/?id={view["id"]}"' in html


def test_saved_search_opens_instantly_from_history(client):
    _, view = search(client, q="nanostation")
    html = client.get(f"/?id={view['id']}").text
    data = json.loads(re.search(r'<script id="pf-data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert data["saved"]["id"] == view["id"] and len(data["saved"]["offers"]) == len(view["offers"])
    assert client.get("/?id=99999", follow_redirects=False).status_code == 303


def test_query_in_url_starts_search_in_browser(client):
    html = client.get("/?q=Cat6a+patch+cord+3m&quantity=2").text
    data = json.loads(re.search(r'<script id="pf-data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert data["pending"]["q"] == "Cat6a patch cord 3m" and data["pending"]["quantity"] == "2"


def test_old_results_links_redirect(client):
    assert client.get("/results?id=5", follow_redirects=False).headers["location"] == "/?id=5"
    assert client.get("/results?q=switch", follow_redirects=False).headers["location"] == "/?q=switch"


# ---- Search -------------------------------------------------------------------------------
def test_results_stream_one_event_per_supplier(client):
    events, view = search(client, q="Ubiquiti NanoStation Loco M5")
    supplier_events = [e for e in events if e["type"] == "supplier"]
    assert {"network_options", "ctc_solutions", "optace"} <= {e["key"] for e in supplier_events}
    assert sum(len(e["offers"]) for e in supplier_events) == len(view["offers"])


def test_search_all_tier1_suppliers(client):
    _, view = search(client, q="Ubiquiti NanoStation Loco M5")
    assert {11368, 10440, 12760} <= totals(view)   # +VAT, Excl. Tax and Optace excl, all with 16% added
    assert view["cheapest"] == "KES 10,440" and view["cheapest_supplier"].startswith("City Telecommunication")
    optace = by_supplier(view, "optace")[0]
    assert optace["url"].startswith("https://optacenetworks.com/products/") and optace["fetched_at"]
    assert any(n["supplier_key"] == "nile_computers" and n["kind"] == "rfq" for n in view["notes"])
    assert view["same_product_groups"] >= 1         # locoM5 at several suppliers


def test_price_range_applies_to_total_incl_vat(client):
    _, view = search(client, q="Ubiquiti NanoStation Loco M5", max_price="11,000")
    assert 10440 in totals(view) and 11368 not in totals(view) and 12760 not in totals(view)
    assert view["filtered_out"] > 0


def test_quantity_recorded(client):
    _, view = search(client, q="Ubiquiti NanoStation Loco M5", quantity="3")
    assert view["quantity"] == 3
    assert all(o["line_total"] == pytest.approx(o["total_incl_vat"] * 3) for o in view["offers"] if o["total_incl_vat"])


def test_no_public_price_offers(client):
    _, view = search(client, q="Grandstream GWN7803")
    assert view["offers"] and all(o["no_public_price"] for o in view["offers"])


def test_no_results(client):
    _, view = search(client, q="quantum flux capacitor")
    assert view["offers"] == []


def test_empty_query_rejected(client):
    assert client.get("/api/search", params={"q": " "}).status_code == 400


def test_cache_reused(client, monkeypatch):
    search(client, q="nanostation")
    calls = []
    orig = OptaceAdapter.search

    async def counting(self, q, f):
        calls.append(q)
        return await orig(self, q, f)
    monkeypatch.setattr(OptaceAdapter, "search", counting)
    search(client, q="nanostation")
    assert calls == []


def test_supplier_timeout_does_not_block_others(client, monkeypatch):
    async def slow(self, q, f):
        await asyncio.sleep(2)
    monkeypatch.setattr(OptaceAdapter, "search", slow)
    monkeypatch.setattr(config, "FETCH_TIMEOUT", 0.3)
    events, view = search(client, q="Ubiquiti NanoStation Loco M5")
    order = [e["key"] for e in events if e["type"] == "supplier"]
    assert order[-1] == "optace"                    # the fast suppliers arrive first
    assert any("Optace Networks didn't respond in time" in n["text"] for n in view["notes"])
    assert {11368, 10440} <= totals(view)


def test_live_mode_with_respx_serving_saved_pages(client, monkeypatch):
    monkeypatch.setattr(config, "SUPPLIER_SOURCE", "live")
    monkeypatch.setattr(search_mod, "REQUEST_INTERVAL", 0)
    handler = make_transport().handler
    with respx.mock(assert_all_called=False) as m:
        for host in ["network-options.co.ke", "ctcsolutions.co.ke", "optacenetworks.com"]:
            m.route(host=host).mock(side_effect=handler)
        _, view = search(client, q="Ubiquiti NanoStation Loco M5")
    assert {11368, 10440, 12760} <= totals(view)


def test_live_mode_site_refuses(client, monkeypatch):
    monkeypatch.setattr(config, "SUPPLIER_SOURCE", "live")
    monkeypatch.setattr(search_mod, "REQUEST_INTERVAL", 0)
    handler = make_transport().handler
    with respx.mock(assert_all_called=False) as m:
        m.route(host="ctcsolutions.co.ke").mock(return_value=httpx.Response(403))
        for host in ["network-options.co.ke", "optacenetworks.com"]:
            m.route(host=host).mock(side_effect=handler)
        _, view = search(client, q="Ubiquiti NanoStation Loco M5")
    assert any("City Telecommunication Centre (CTC) didn't respond" in n["text"] for n in view["notes"])
    assert 11368 in totals(view)


# ---- Compare tab, compare, choose, history ---------------------------------------------------
def test_compare_tab(client):
    html = client.get("/compare").text
    assert "Ready to compare" in html and "Past comparisons" in html


def test_compare_choose_history_csv(client):
    _, view = search(client, q="Ubiquiti NanoStation Loco M5")
    refs = [f"{view['id']}:{o['idx']}" for o in view["offers"][:3]]
    r = client.post("/compare", data={"ref": refs}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert "Total incl. VAT" in page and "Plain summary" in page and "cheapest" in page
    cid = int(r.headers["location"].rsplit("/", 1)[1])

    summary = client.post(f"/api/compare/{cid}/summary").json()
    assert summary["source"] == "plain" and "cheapest" in summary["summary"]

    r = client.post(f"/compare/{cid}/choose", data={"offer": 0, "reason": "In stock, Tier 1, best total"}, follow_redirects=False)
    assert r.status_code == 303
    hist = client.get("/history").text
    assert "In stock, Tier 1, best total" in hist and "Tester" in hist
    assert "In stock, Tier 1, best total" in client.get("/compare").text or "Chose" in client.get("/compare").text
    csv_text = client.get("/history.csv").text
    assert "In stock, Tier 1, best total" in csv_text and "total_incl_vat" in csv_text


def test_compare_offers_from_different_searches(client):
    _, a = search(client, q="Ubiquiti NanoStation Loco M5")
    _, b = search(client, q="Cat6a patch cord 3m")
    r = client.post("/compare", data={"ref": [f"{a['id']}:0", f"{b['id']}:0"]}, follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert a["offers"][0]["name"] in page and b["offers"][0]["name"] in page


def test_compare_needs_two_to_five(client):
    _, view = search(client, q="nanostation")
    refs = [f"{view['id']}:{o['idx']}" for o in view["offers"]]
    r = client.post("/compare", data={"ref": refs[:1]}, follow_redirects=False)
    assert r.headers["location"] == "/compare"
    assert "Pick between 2 and 5" in client.get("/compare").text
    r = client.post("/compare", data={"ref": refs[:6]}, follow_redirects=False)
    assert r.headers["location"] == "/compare"
    r = client.post("/compare", data={"ref": [f"{view['id']}:0", "99999:0"]}, follow_redirects=False)
    assert r.headers["location"] == "/compare"


def test_compare_old_form_still_works(client):
    _, view = search(client, q="nanostation")
    r = client.post("/compare", data={"search_id": view["id"], "offer": [0, 1]}, follow_redirects=False)
    assert r.headers["location"].startswith("/compare/")


def test_choose_needs_reason(client):
    _, view = search(client, q="nanostation")
    r = client.post("/compare", data={"ref": [f"{view['id']}:0", f"{view['id']}:1"]}, follow_redirects=False)
    cid = r.headers["location"].rsplit("/", 1)[1]
    r = client.post(f"/compare/{cid}/choose", data={"offer": 0, "reason": "  "}, follow_redirects=False)
    assert r.headers["location"] == f"/compare/{cid}"


# ---- RFQ -------------------------------------------------------------------------------
def test_rfq_draft_prefilled_from_offer(client):
    _, view = search(client, q="Grandstream GWN7803")
    form = client.get(f"/rfq?search_id={view['id']}&offer=0").text
    assert "Grandstream GWN7803" in form and "Network Options" in form
    r = client.post("/rfq", data={"item": "Grandstream GWN7803 Pro", "quantity": "2", "supplier": "Network Options & Solutions",
                                  "delivery_location": "Nairobi"})
    assert "Request for quotation: Grandstream GWN7803 Pro (qty 2)" in r.text
    assert "whether the prices include VAT" in r.text
    assert "Grandstream GWN7803 Pro" in client.get("/history").text


# ---- AI ---------------------------------------------------------------------------------
OFFERS = [
    {"supplier": "CTC", "tier": 1, "name": "NanoStation Loco M5", "price": 9000, "total_incl_vat": 10440.0,
     "stock": "in stock", "no_public_price": False, "unit_label": "each"},
    {"supplier": "Optace", "tier": 1, "name": "Nanostation locoM5", "price": 11000, "total_incl_vat": 12760.0,
     "stock": None, "no_public_price": False, "unit_label": "each"},
]


def _ai_on(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://ai.test")
    monkeypatch.setattr(config, "PRACTICE_AI", False)


def test_ai_unreachable_falls_back(monkeypatch):
    _ai_on(monkeypatch)
    with respx.mock:
        respx.post("http://ai.test/api/chat").mock(side_effect=httpx.ConnectError("down"))
        respx.get("http://ai.test/api/tags").mock(side_effect=httpx.ConnectError("down"))
        out = asyncio.run(llm.compare_summary(OFFERS, 1))
        health = asyncio.run(llm.health())
    assert out["source"] == "plain" and "AI unavailable" in out["warning"]
    assert health["ok"] is False and "unreachable" in health["detail"]


def _ai_reply(content: dict) -> httpx.Response:
    import json
    return httpx.Response(200, json={"message": {"content": json.dumps(content)}})


def test_ai_summary_with_invented_number_rejected(monkeypatch):
    _ai_on(monkeypatch)
    with respx.mock:
        respx.post("http://ai.test/api/chat").mock(return_value=_ai_reply(
            {"summary": "CTC is cheapest at KES 9,999 incl. VAT.", "cheapest": "CTC", "notes": []}))
        out = asyncio.run(llm.compare_summary(OFFERS, 1))
    assert out["source"] == "plain" and "didn't match the facts" in out["warning"]


def test_ai_summary_with_supported_numbers_accepted(monkeypatch):
    _ai_on(monkeypatch)
    with respx.mock:
        respx.post("http://ai.test/api/chat").mock(return_value=_ai_reply(
            {"summary": "CTC is cheapest at KES 10,440 incl. VAT and in stock; Optace is 22% more.",
             "cheapest": "CTC", "notes": ["Optace doesn't show stock."]}))
        out = asyncio.run(llm.compare_summary(OFFERS, 1))
    assert out["source"] == "ai" and "10,440" in out["summary"]


def test_ai_rfq_without_vat_question_rejected(monkeypatch):
    from rfq import RFQRequest
    _ai_on(monkeypatch)
    with respx.mock:
        respx.post("http://ai.test/api/chat").mock(return_value=_ai_reply(
            {"subject": "Quote for Cat6 cable", "body": "Please quote Cat6 cable."}))
        out = asyncio.run(llm.rfq_draft(RFQRequest(item="Cat6 cable", quantity=2)))
    assert out["source"] == "template" and "include VAT" in out["body"]


def test_parse_request_only_uses_words_from_request(monkeypatch):
    _ai_on(monkeypatch)
    with respx.mock:
        respx.post("http://ai.test/api/chat").mock(return_value=_ai_reply(
            {"keywords": "laptop 16GB", "brand": "Dell", "quantity": 2, "max_price": 120000, "location": "Nairobi"}))
        out = asyncio.run(llm.parse_request("2 laptops for engineers, 16GB RAM, under 120k, Nairobi"))
    assert out == {"keywords": "laptop 16GB", "brand": "", "location": "Nairobi", "quantity": 2.0, "max_price": 120000.0}


# ---- Pages and practice mode ---------------------------------------------------------------
def test_practice_mode_pages(client):
    assert "Practice mode" in client.get("/").text
    sup = client.get("/suppliers").text
    assert "RFQ only" in sup and "Prices exclude VAT" in sup
    h = client.get("/health?format=json").json()
    assert h["ai"]["mode"] == "practice" and h["supplier_source"] == "fixtures" and len(h["suppliers"]) == 5
    assert "System status" in client.get("/health").text
