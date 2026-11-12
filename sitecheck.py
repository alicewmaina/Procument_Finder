"""Check a discovered supplier's website: does it load, over HTTPS, is it the same business,
is it a parked/default page, and does it have a shop ProcureFind can read (WooCommerce or Shopify)?

One polite home-page request (plus robots.txt, and one shop check when the page suggests a shop).
Results are cached on the directory row for SITE_CHECK_DAYS.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from adapters.base import Blocked, Fetcher, SupplierError
from prices import clean_text

BRANDS = ["Ubiquiti", "MikroTik", "Cisco", "Huawei", "TP-Link", "Grandstream", "Yealink", "Fortinet", "Sophos", "HP", "HPE",
          "Dell", "Lenovo", "Microsoft", "Hikvision", "Dahua", "APC", "Schneider", "Cambium", "Ruijie", "Aruba", "Juniper",
          "D-Link", "Netgear", "Synology", "Samsung", "Epson", "Canon", "Eaton", "Legrand", "Molex", "Commscope"]
PARTNER_WORDS = r"authori[sz]ed|official|certified|partner|distributor|reseller|dealer"
PARKED = re.compile(r"default page|domain (is )?for sale|buy this domain|parked|coming soon|under construction|"
                    r"account suspended|suspendedpage|index of /|it works!|welcome to nginx|this site can.t be reached",
                    re.I)


def _registrable(host: str) -> str:
    """'shop.example.co.ke' -> 'example.co.ke' (good enough for Kenyan and common domains)."""
    parts = host.lower().removeprefix("www.").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "or", "ac", "go", "ne", "com", "net", "org"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


async def check(website: str, fetcher: Fetcher) -> dict:
    out = {"url": website, "reachable": False, "https": False, "final_url": "", "same_site": True, "parked": False,
           "platform": "none", "title": "", "brands": [], "partner_brands": [], "currency": "", "error": ""}
    try:
        r = await fetcher.get(website)
    except Blocked as exc:
        out["error"] = f"refused automated access ({exc})"
        return out
    except SupplierError as exc:
        out["error"] = str(exc)
        return out
    final = str(r.url)
    html = r.text[:400_000]
    text = clean_text(re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I))
    out.update(reachable=True, final_url=final, https=final.startswith("https://"))
    out["same_site"] = _registrable(urlsplit(final).netloc) == _registrable(urlsplit(website).netloc)
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    out["title"] = clean_text(m.group(1))[:120] if m else ""
    out["parked"] = bool(PARKED.search(out["title"])) or len(text) < 200 and bool(PARKED.search(text[:500]))
    low = text.lower()
    out["brands"] = [b for b in BRANDS if re.search(rf"\b{re.escape(b.lower())}\b", low)][:12]
    out["partner_brands"] = [b for b in out["brands"]
                             if re.search(rf"({PARTNER_WORDS})[^.]{{0,40}}\b{re.escape(b.lower())}\b|"
                                          rf"\b{re.escape(b.lower())}\b[^.]{{0,40}}({PARTNER_WORDS})", low)]
    root = f"{urlsplit(final).scheme}://{urlsplit(final).netloc}"
    try:
        if "woocommerce" in html.lower() or "wc-block" in html.lower() or "/wp-content/" in html.lower():
            api = await fetcher.get(f"{root}/wp-json/wc/store/v1/products?per_page=1")
            data = api.json()
            if isinstance(data, list):
                out["platform"] = "woocommerce"
                if data:
                    out["currency"] = (data[0].get("prices") or {}).get("currency_code", "")
        elif "cdn.shopify.com" in html.lower() or "shopify" in html.lower():
            cart = await fetcher.get(f"{root}/cart.js")
            data = cart.json()
            if isinstance(data, dict) and "currency" in data:
                out["platform"] = "shopify"
                out["currency"] = data.get("currency", "")
    except (SupplierError, ValueError):
        pass                       # no readable shop; prices need an RFQ
    return out


def readable(probe: dict) -> bool:
    """Prices can be read when the shop is WooCommerce or Shopify, priced in KES, and the site is genuine."""
    return bool(probe and probe.get("reachable") and probe.get("same_site") and not probe.get("parked")
                and probe.get("platform") in ("woocommerce", "shopify") and probe.get("currency", "KES") in ("KES", ""))


def summary(probe: dict) -> str:
    """One line for the UI explaining what the website check found."""
    if not probe:
        return "Website not checked yet"
    if not probe.get("reachable"):
        err = probe.get("error") or ""
        why = "timed out" if "timed out" in err else "refused automated access" if "refused" in err else ""
        return "Website didn't load" + (f" ({why})" if why else "")
    if not probe.get("same_site"):
        return f"Website now goes to a different site ({urlsplit(probe.get('final_url', '')).netloc})"
    if probe.get("parked"):
        return "Website is a parked or placeholder page"
    if readable(probe):
        return f"Online shop ({probe['platform'].capitalize()}): prices read live"
    if probe.get("platform") in ("woocommerce", "shopify"):
        return f"Online shop priced in {probe.get('currency') or 'another currency'}: not compared"
    return "Website has no online price list ProcureFind can read: request a quote"
