"""Optace Networks (custom Next.js site).

Every Optace page embeds the full product list (about 515 items: id, name,
slug, image, regular_price) in its Next.js data (`self.__next_f.push(...)`).
The site's own search box filters that list in the browser, so ProcureFind
does the same: fetch the home page once per cache period and search locally.

Price 0 means "Call for Quote". Prices exclude VAT ("8,900 KES Excl VAT" on
product pages). Stock isn't shown on the site.
"""

from __future__ import annotations

import json
import re
import time

from adapters.base import Adapter, Fetcher, Offer, SupplierError, now_iso
from matching import model_from_name, rank, simplify_query
from prices import clean_text, total_incl_vat
import config

SITE = "https://optacenetworks.com"
IMAGE_BASE = "https://portal.optacenetworks.com"
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_catalogue: dict = {"at": 0.0, "items": None}


def clear_cache() -> None:
    _catalogue.update(at=0.0, items=None)


def parse_catalogue(html: str) -> list[dict]:
    """Product records from the page's embedded Next.js data."""
    payload = []
    for chunk in _PUSH.findall(html):
        try:
            payload.append(json.loads(f'"{chunk}"'))
        except ValueError:
            continue
    text = "".join(payload)
    dec = json.JSONDecoder()
    found: dict = {}
    for m in re.finditer(r'\{"id":', text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("name") and obj.get("slug") and "regular_price" in obj:
            found[obj["id"]] = obj
    return list(found.values())


class OptaceAdapter(Adapter):
    async def catalogue(self, fetcher: Fetcher) -> list[dict]:
        if _catalogue["items"] is not None and time.time() - _catalogue["at"] < config.CACHE_MINUTES * 60:
            return _catalogue["items"]
        r = await fetcher.get((self.supplier.search_url or SITE).rstrip("/") + "/")
        items = parse_catalogue(r.text)
        if not items:
            raise SupplierError("page layout changed: no product list found")
        _catalogue.update(at=time.time(), items=items)
        return items

    async def search(self, query: str, fetcher: Fetcher) -> list[Offer]:
        items = await self.catalogue(fetcher)
        hits = rank(query, items, name=lambda p: p["name"], model=lambda p: None)
        if not hits:
            hits = rank(simplify_query(query), items, name=lambda p: p["name"], model=lambda p: None)
        return [self.to_offer(p) for p in hits[:20]]

    def to_offer(self, p: dict) -> Offer:
        s = self.supplier
        try:
            amount = float(p.get("regular_price") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        price = amount if amount > 0 else None
        name = clean_text(p["name"])
        image = p.get("image") or ""
        return Offer(
            supplier=s.name, tier=s.tier, name=name, model=model_from_name(name),
            url=f"{SITE}/products/{p['slug']}",
            price_text=f"{amount:,.0f} KES" if price else "Call for Quote",
            price=price, currency="KES", vat_basis=s.vat_basis,
            total_incl_vat=total_incl_vat(price, s.vat_basis, config.VAT_RATE),
            stock=None, no_public_price=price is None, fetched_at=now_iso(), supplier_key=s.key,
            image=(IMAGE_BASE + image) if image.startswith("/") else image,
        )
