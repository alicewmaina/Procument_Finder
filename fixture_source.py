"""Practice-mode supplier source: answers supplier requests from saved pages in tests/fixtures.

Used when SUPPLIER_SOURCE=fixtures (the default in practice mode) and by the
tests, so ProcureFind runs with no internet. WooCommerce searches are emulated
by filtering every saved product for the shop the way the live Store API
behaves (the search text must appear as one phrase).
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

import config


def _woo_products(folder: Path) -> list[dict]:
    seen: dict = {}
    for f in sorted(folder.glob("*.json")):
        try:
            for item in json.loads(f.read_text()):
                seen.setdefault(item.get("id"), item)
        except (ValueError, AttributeError):
            continue
    return list(seen.values())


def _plain(text: str) -> str:
    return " ".join(html.unescape(text or "").lower().split())


def _woo_search(products: list[dict], query: str) -> list[dict]:
    """Like the live shops: the search text must appear as one phrase in the name or SKU."""
    phrase = _plain(query)
    return [p for p in products if phrase in _plain(p.get("name", "")) or phrase in _plain(p.get("sku", ""))][:20]


def _overpass_files(root: Path) -> dict[str, Path]:
    """Map OpenStreetMap area ids to saved Overpass results, via the saved Nominatim lookups."""
    out = {}
    for f in (root / "osm").glob("nominatim-*.json"):
        key = f.stem.removeprefix("nominatim-")
        try:
            hit = next((h for h in json.loads(f.read_text()) if h.get("osm_type") == "relation"), None)
        except ValueError:
            continue
        if hit and (root / "osm" / f"overpass-{key}.json").exists():
            out[str(3600000000 + int(hit["osm_id"]))] = root / "osm" / f"overpass-{key}.json"
    return out


def make_transport(fixtures_dir: Path | None = None, suppliers=None) -> httpx.MockTransport:
    root = Path(fixtures_dir or config.FIXTURES_DIR)
    hosts = {urlsplit(s.website).netloc: s for s in (suppliers or config.SUPPLIERS)}
    overpass_hosts = {urlsplit(u).netloc for u in config.OVERPASS_URLS}

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        # OpenStreetMap: location lookup and supplier search
        if host == urlsplit(config.NOMINATIM_URL).netloc:
            place = request.url.params.get("q", "").split(",")[0].strip().lower().replace(" ", "-")
            f = root / "osm" / f"nominatim-{place}.json"
            return httpx.Response(200, text=f.read_text() if f.exists() else "[]", headers={"content-type": "application/json"})
        if host in overpass_hosts:
            body = request.content.decode("utf-8", "ignore")
            for area, f in _overpass_files(root).items():
                if area in body:
                    return httpx.Response(200, text=f.read_text(), headers={"content-type": "application/json"})
            return httpx.Response(200, json={"elements": []})
        # PPRA debarred firms
        if host == urlsplit(config.PPRA_DEBARRED_URL).netloc:
            return httpx.Response(200, text=(root / "ppra" / "debarred-firms.html").read_text(), headers={"content-type": "text/html"})
        # Tier 1 suppliers
        s = hosts.get(host)
        if s is not None:
            folder = root / s.key
            if not folder.is_dir():
                return httpx.Response(404, text="Not in saved pages")
            if path == "/robots.txt":
                return httpx.Response(404)
            if "/wp-json/wc/store/" in path:
                return httpx.Response(200, json=_woo_search(_woo_products(folder), request.url.params.get("search", "")))
            if path in ("", "/") and (folder / "catalogue.html").exists():
                return httpx.Response(200, text=(folder / "catalogue.html").read_text(), headers={"content-type": "text/html"})
            return httpx.Response(404, text="Not in saved pages")
        # Suppliers found on OpenStreetMap: saved home pages, with the redirect / status seen at the time
        site = root / "sites" / host
        if not site.is_dir():
            raise httpx.ConnectError(f"{host} didn't respond when the pages were saved", request=request)
        meta = json.loads((site / "meta.json").read_text()) if (site / "meta.json").exists() else {}
        if path == "/robots.txt":
            return httpx.Response(404)
        if meta.get("redirect"):
            return httpx.Response(301, headers={"location": meta["redirect"]})
        if path in ("", "/") and (site / "home.html").exists():
            return httpx.Response(int(meta.get("status", 200)), text=(site / "home.html").read_text(errors="ignore"),
                                  headers={"content-type": "text/html"})
        return httpx.Response(404, text="Not in saved pages")

    return httpx.MockTransport(handler)
