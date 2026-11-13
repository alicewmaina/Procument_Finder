"""Readers for Tier 2/3 suppliers found by discovery: WooCommerce and Shopify shops.

Only shops priced in KES are read. Their VAT basis isn't known in advance, so it's taken from the
price wording ("+VAT", "Incl. VAT") when present and otherwise left unknown (flagged, no total).
"""

from __future__ import annotations

from urllib.parse import quote_plus, urlsplit

from adapters.base import Adapter, Fetcher, Offer, SupplierError, now_iso
from adapters.woocommerce import WooCommerceAdapter
from config import Supplier
from matching import model_from_name, rank, simplify_query
from prices import clean_text, kes, total_incl_vat
import config


def supplier_for(row: dict, tier: int) -> Supplier:
    probe = row.get("probe") or {}
    final = probe.get("final_url") or row["website"]
    root = f"{urlsplit(final).scheme}://{urlsplit(final).netloc}/"
    return Supplier(key=row["id"], name=row["name"], tier=tier, website=root, vat_basis="unknown",
                    adapter=probe.get("platform"), status="active", location=row.get("town") or "")


class DirectoryWooAdapter(WooCommerceAdapter):
    async def search(self, query: str, fetcher: Fetcher) -> list[Offer]:
        offers = await super().search(query, fetcher)
        return [o for o in offers if o.currency == "KES"]


class ShopifyAdapter(Adapter):
    """Shopify's public storefront search (the same results as the shop's own search box)."""

    def url(self, q: str) -> str:
        return (self.supplier.website.rstrip("/") +
                f"/search/suggest.json?q={quote_plus(q)}&resources[type]=product&resources[limit]=10")

    async def search(self, query: str, fetcher: Fetcher) -> list[Offer]:
        for q in dict.fromkeys([query, simplify_query(query) or query]):
            r = await fetcher.get(self.url(q))
            try:
                products = (((r.json() or {}).get("resources") or {}).get("results") or {}).get("products") or []
            except ValueError as exc:
                raise SupplierError("returned a page ProcureFind couldn't read") from exc
            offers = rank(query, [self.to_offer(p) for p in products if p.get("title")])
            if offers:
                return offers
        return []

    def to_offer(self, p: dict) -> Offer:
        s = self.supplier
        try:
            amount = float(str(p.get("price", "")).replace(",", ""))
        except ValueError:
            amount = 0.0
        price = amount if amount > 0 else None
        name = clean_text(p.get("title"))
        url = p.get("url") or ""
        if url.startswith("/"):
            url = s.website.rstrip("/") + url.split("?")[0]
        image = p.get("image") or (p.get("featured_image") or {}).get("url") or ""
        if image.startswith("//"):
            image = "https:" + image
        available = p.get("available")
        return Offer(
            supplier=s.name, tier=s.tier, name=name, model=model_from_name(name), url=url or s.website,
            price_text=f"{kes(price, 2)} (shop data)" if price else "No price shown", price=price, currency="KES",
            vat_basis="unknown", total_incl_vat=total_incl_vat(price, "unknown", config.VAT_RATE),
            stock=None if available is None else ("in stock" if available else "out of stock"),
            no_public_price=price is None, fetched_at=now_iso(), supplier_key=s.key, image=image,
        )


def adapter_for_row(row: dict, tier: int) -> Adapter | None:
    platform = (row.get("probe") or {}).get("platform")
    s = supplier_for(row, tier)
    if platform == "woocommerce":
        return DirectoryWooAdapter(s)
    if platform == "shopify":
        return ShopifyAdapter(s)
    return None
