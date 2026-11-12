"""Reusable reader for WooCommerce shops, via the public Store API (JSON).

GET {site}/wp-json/wc/store/v1/products?search=QUERY&per_page=20

This is the same data the shop's own search page shows, as JSON, so there's
no HTML scraping. Prices come in minor units (`currency_minor_unit`), which
differs per shop: Network Options uses cents (2), CTC whole shillings (0).
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

from adapters.base import Adapter, Fetcher, Offer, SupplierError, now_iso
from matching import model_from_name, rank, simplify_query
from prices import clean_text, kes, parse_price_text, total_incl_vat, NO_PRICE_PHRASES, vat_wording
import config

PER_PAGE = 20
MAX_REQUESTS = 4         # per supplier per search (polite; 1 request/second)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9+\-./]*")


def fallback_queries(query: str) -> list[str]:
    """Searches to try in order: as typed; without lengths; the model-like word; the longest word."""
    simpler = simplify_query(query) or query
    words = _WORD.findall(simpler)
    modelish = [w for w in words if re.search(r"\d", w) and re.search(r"[A-Za-z]", w) and len(w) >= 4]
    plain = [w for w in words if len(w) >= 4 and not re.search(r"\d", w)]
    candidates = [query, simpler]
    if modelish:
        candidates.append(max(modelish, key=len))
    if plain:
        candidates.append(max(plain, key=len))
    out: list[str] = []
    for c in candidates:
        if c and c.lower() not in (o.lower() for o in out):
            out.append(c)
    return out[:MAX_REQUESTS]


def _amount(raw, minor_unit: int) -> float | None:
    try:
        value = int(str(raw)) / (10 ** int(minor_unit or 0))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _price_text(price_html: str) -> str:
    """Price as the shop shows it, minus the crossed-out old price and screen-reader-only text."""
    h = re.sub(r"<del\b.*?</del>", " ", price_html or "", flags=re.S | re.I)
    h = re.sub(r'<span class="screen-reader-text">.*?</span>', " ", h, flags=re.S | re.I)
    return clean_text(h)


def _stock(item: dict) -> str | None:
    if item.get("is_on_backorder"):
        return "on backorder"
    if item.get("is_in_stock") is False:
        return "out of stock"
    if item.get("is_in_stock") is True:
        text = clean_text((item.get("stock_availability") or {}).get("text", "")).lower()
        return text if text and "out" not in text else "in stock"
    return None


class WooCommerceAdapter(Adapter):
    def search_url(self, query: str) -> str:
        template = self.supplier.search_url or (self.supplier.website.rstrip("/") +
                                                "/wp-json/wc/store/v1/products?search={query}&per_page=20")
        return template.replace("{query}", quote_plus(query))

    async def _fetch(self, query: str, fetcher: Fetcher) -> list[dict]:
        r = await fetcher.get(self.search_url(query))
        try:
            data = r.json()
        except ValueError as exc:
            raise SupplierError("returned a page ProcureFind couldn't read (not JSON)") from exc
        if not isinstance(data, list):
            raise SupplierError("returned an unexpected answer (layout may have changed)")
        return data

    async def search(self, query: str, fetcher: Fetcher) -> list[Offer]:
        """The shops match the search text as one phrase ("ubiquiti nanostation" misses
        "Ubiquiti airMAX NanoStation"), so try a few shorter searches, at most
        MAX_REQUESTS, and keep only results our own relevance check accepts."""
        simpler = simplify_query(query)
        for q in fallback_queries(query):
            items = await self._fetch(q, fetcher)
            offers = [self.to_offer(i) for i in items if isinstance(i, dict) and i.get("name")]
            ranked = rank(query, offers) or (rank(simpler, offers) if simpler != query else [])
            if ranked:
                return ranked
        return []

    def to_offer(self, item: dict) -> Offer:
        s = self.supplier
        p = item.get("prices") or {}
        mu = p.get("currency_minor_unit", 0)
        price = _amount(p.get("price"), mu)
        note = ""
        rng = p.get("price_range") or None
        if isinstance(rng, dict):
            lo, hi = _amount(rng.get("min_amount"), mu), _amount(rng.get("max_amount"), mu)
            if lo:
                price = lo
                if hi and hi != lo:
                    note = f"Price depends on the option chosen ({kes(lo)} to {kes(hi)} before VAT); lowest shown."
        text = _price_text(item.get("price_html", ""))
        if item.get("type") == "variable" and not note:
            note = "Has options (e.g. length or colour); price may differ by option."
        no_price = price is None or bool(NO_PRICE_PHRASES.search(text))
        if no_price:
            price = None
        elif not text:
            text = kes(price)
        # Use what the price text says about VAT; fall back to the supplier's known basis.
        basis = vat_wording(text) or s.vat_basis
        if text and not no_price and parse_price_text(text).price is None:
            note = (note + " " if note else "") + "Price read from the shop's data; check the page."
        sku = clean_text(item.get("sku") or "") or None
        name = clean_text(item.get("name"))
        images = item.get("images") or []
        return Offer(
            supplier=s.name, tier=s.tier, name=name, model=sku or model_from_name(name),
            url=item.get("permalink") or s.website, price_text=text or "No price shown", price=price,
            currency=p.get("currency_code") or "KES", vat_basis=basis,
            total_incl_vat=total_incl_vat(price, basis, config.VAT_RATE), stock=_stock(item),
            no_public_price=no_price, fetched_at=now_iso(), supplier_key=s.key,
            image=(images[0].get("thumbnail") or images[0].get("src") or "") if images else "", note=note,
        )
