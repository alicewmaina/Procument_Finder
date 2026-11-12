"""Each Tier 1 reader against its saved pages, plus the politeness rules."""

import asyncio
import time

import httpx
import pytest
import respx

import config
from adapters import Blocked, Fetcher, SupplierError, adapter_for
from adapters.optace import parse_catalogue
from fixture_source import make_transport

from conftest import FIXTURES

SUPPLIERS = {s.key: s for s in config.SUPPLIERS}


def run(coro):
    return asyncio.run(coro)


async def search(key, query):
    async with httpx.AsyncClient(transport=make_transport()) as c:
        return await adapter_for(SUPPLIERS[key]).search(query, Fetcher(c, config.USER_AGENT, 0))


def by_name(offers, text):
    return next(o for o in offers if text.lower() in o.name.lower())


def test_supplier_config():
    assert SUPPLIERS["optace"].vat_basis == "excl"
    assert not SUPPLIERS["nile_computers"].active and adapter_for(SUPPLIERS["nile_computers"]) is None
    assert not SUPPLIERS["om_it"].active


def test_network_options_prices_in_cents_plus_vat():
    offers = run(search("network_options", "nanostation"))
    o = by_name(offers, "NS-5ACL")
    assert o.price == 6000 and o.price_text == "6,000.00 KSh +VAT"
    assert o.vat_basis == "excl" and o.total_incl_vat == pytest.approx(6960)
    assert o.stock == "out of stock" and o.model == "NS-5ACL"
    assert o.url.startswith("https://network-options.co.ke/product/") and o.fetched_at


def test_network_options_no_public_price():
    offers = run(search("network_options", "grandstream gwn7803"))
    assert offers and all(o.no_public_price and o.price is None and o.total_incl_vat is None for o in offers)


def test_ctc_whole_shillings_excl_tax():
    offers = run(search("ctc_solutions", "Ubiquiti NanoStation Loco M5"))
    o = by_name(offers, "Loco M5")
    assert o.price == 9000 and o.price_text == "KSh 9,000 Excl. Tax"
    assert o.total_incl_vat == pytest.approx(10440) and o.stock == "in stock"


def test_ctc_variable_product_flagged():
    offers = run(search("ctc_solutions", "patch cord"))
    assert any("option" in o.note for o in offers)


def test_no_results_retries_simpler_query():
    async def go():
        async with httpx.AsyncClient(transport=make_transport()) as c:
            f = Fetcher(c, config.USER_AGENT, 0)
            await adapter_for(SUPPLIERS["network_options"]).search("Cat6a patch cord 3m", f)
            return f.requests_made
    made = run(go())
    assert any("search=Cat6a+patch+cord+3m" in u for u in made)
    assert any(u.endswith("search=Cat6a+patch+cord&per_page=20") for u in made)


def test_optace_catalogue_and_call_for_quote():
    items = parse_catalogue((FIXTURES / "optace" / "catalogue.html").read_text())
    assert len(items) > 500
    offers = run(search("optace", "Ubiquiti NanoStation Loco M5"))
    o = offers[0]
    assert o.name == "Ubiquiti Nanostation locoM5" and o.price == 11000 and o.price_text == "11,000 KES"
    assert o.total_incl_vat == pytest.approx(12760) and o.stock is None
    assert o.url == "https://optacenetworks.com/products/" + next(i["slug"] for i in items if i["name"] == o.name)
    quote = run(search("optace", "10G SFP+ Direct-Attached Copper Twinax"))
    assert quote and quote[0].no_public_price and quote[0].price_text == "Call for Quote"


def test_optace_layout_change_is_reported():
    async def go():
        with respx.mock:
            respx.get("https://optacenetworks.com/robots.txt").mock(return_value=httpx.Response(404))
            respx.get("https://optacenetworks.com/").mock(return_value=httpx.Response(200, text="<html>new site</html>"))
            async with httpx.AsyncClient() as c:
                await adapter_for(SUPPLIERS["optace"]).search("switch", Fetcher(c, config.USER_AGENT, 0))
    with pytest.raises(SupplierError, match="layout changed"):
        run(go())


def test_403_is_blocked_not_bypassed():
    async def go():
        with respx.mock:
            respx.get("https://ctcsolutions.co.ke/robots.txt").mock(return_value=httpx.Response(200, text=""))
            route = respx.get(url__startswith="https://ctcsolutions.co.ke/wp-json/").mock(return_value=httpx.Response(403))
            async with httpx.AsyncClient() as c:
                try:
                    await adapter_for(SUPPLIERS["ctc_solutions"]).search("switch", Fetcher(c, config.USER_AGENT, 0))
                finally:
                    assert route.call_count == 1          # no retries with other tricks
    with pytest.raises(Blocked, match="403"):
        run(go())


def test_robots_disallow_respected():
    async def go():
        with respx.mock:
            respx.get("https://network-options.co.ke/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nDisallow: /wp-json/\n"))
            route = respx.get(url__startswith="https://network-options.co.ke/wp-json/").mock(return_value=httpx.Response(200, json=[]))
            async with httpx.AsyncClient() as c:
                try:
                    await adapter_for(SUPPLIERS["network_options"]).search("switch", Fetcher(c, config.USER_AGENT, 0))
                finally:
                    assert route.call_count == 0
    with pytest.raises(Blocked, match="robots.txt"):
        run(go())


def test_user_agent_and_rate_limit():
    seen = []

    async def go():
        with respx.mock:
            def record(request):
                seen.append((time.monotonic(), request.headers["user-agent"]))
                return httpx.Response(404 if request.url.path == "/robots.txt" else 200, json=[])
            respx.get(url__startswith="https://network-options.co.ke/").mock(side_effect=record)
            async with httpx.AsyncClient() as c:
                f = Fetcher(c, config.USER_AGENT, min_interval=0.3)
                await adapter_for(SUPPLIERS["network_options"]).search("switch", f)
    run(go())
    assert all(ua.startswith("ProcureFind/0.1") for _, ua in seen)
    gaps = [b[0] - a[0] for a, b in zip(seen, seen[1:])]
    assert gaps and min(gaps) >= 0.28
