"""Supplier rating out of 100, with every point explained (spec section 7).

- Legitimacy (40): working secure website, mapped location, address, contacts, Kenyan domain, opening hours.
- Price transparency and competitiveness (35): publishes prices, states VAT, shows stock, and how its
  prices compare with other suppliers for the same items (from ProcureFind's own searches).
- Reputation (25): brand partnerships shown on its website, how often Angani chose it, how reliably
  its website answers. Public review sites aren't used: none offers terms that allow it yet.
- Red flags: PPRA debarment (blocked), broken / parked / redirected website, prices far below market.

Tier 1 stays Tier 1 whatever the score. A discovered supplier becomes Tier 2 ("verified") when its
legitimacy checks pass and it has no red flags; otherwise it's Tier 3 ("unverified").
"""

from __future__ import annotations

import time
from statistics import median
from urllib.parse import urlsplit

import db
import debarred
import sitecheck

LOW_PRICE_RATIO = 0.6          # below 60% of the median for the same product = suspicious
_stats_cache: dict = {"at": 0.0, "value": None}


def clear_cache() -> None:
    _stats_cache.update(at=0.0, value=None)


def flag_low_prices(offers: list[dict]) -> None:
    """Mark offers priced far below the other suppliers of the same product (needs 3+ priced offers)."""
    groups: dict[int, list[dict]] = {}
    for o in offers:
        if o.get("group") and o.get("total_incl_vat") is not None:
            groups.setdefault(o["group"], []).append(o)
    for members in groups.values():
        if len({m["supplier_key"] for m in members}) < 3:
            continue
        mid = median(m["total_incl_vat"] for m in members)
        for m in members:
            if m["total_incl_vat"] < LOW_PRICE_RATIO * mid:
                m.setdefault("flags", []).append(
                    f"Unusually low: {round(100 - 100 * m['total_incl_vat'] / mid)}% below the median for this product. "
                    "Check it's the same item and genuine.")


def supplier_stats() -> dict[str, dict]:
    """What ProcureFind has learnt about each supplier from searches, fetches and decisions (cached 60 s)."""
    if _stats_cache["value"] is not None and time.time() - _stats_cache["at"] < 60:
        return _stats_cache["value"]
    stats: dict[str, dict] = {}

    def s(key):
        return stats.setdefault(key, {"offers": 0, "priced": 0, "vat_stated": 0, "stock_shown": 0, "groups": 0,
                                      "competitive": 0, "low_flags": 0, "fetch_ok": 0, "fetch_all": 0, "chosen": 0})

    for row in db.query("SELECT offers FROM searches ORDER BY id DESC LIMIT 500"):
        offers = row["offers"] or []
        for o in offers:
            st = s(o.get("supplier_key", ""))
            st["offers"] += 1
            st["priced"] += o.get("price") is not None
            st["vat_stated"] += o.get("vat_basis") in ("excl", "incl")
            st["stock_shown"] += o.get("stock") is not None
            st["low_flags"] += any("Unusually low" in f for f in o.get("flags", []))
        by_group: dict[int, list[dict]] = {}
        for o in offers:
            if o.get("group") and o.get("total_incl_vat") is not None:
                by_group.setdefault(o["group"], []).append(o)
        for members in by_group.values():
            if len({m["supplier_key"] for m in members}) < 2:
                continue
            cheapest = min(m["total_incl_vat"] for m in members)
            best_by_supplier: dict[str, float] = {}
            for m in members:
                k = m["supplier_key"]
                best_by_supplier[k] = min(best_by_supplier.get(k, 1e18), m["total_incl_vat"])
            for k, v in best_by_supplier.items():
                s(k)["groups"] += 1
                s(k)["competitive"] += v <= cheapest * 1.05
    for row in db.query("SELECT supplier_key, SUM(ok) AS ok, COUNT(*) AS n FROM fetch_log GROUP BY supplier_key"):
        s(row["supplier_key"]).update(fetch_ok=row["ok"] or 0, fetch_all=row["n"] or 0)
    for row in db.query("SELECT chosen_offer FROM comparisons WHERE chosen_offer IS NOT NULL"):
        ch = row["chosen_offer"] or {}
        if isinstance(ch, dict) and ch.get("supplier_key"):
            s(ch["supplier_key"])["chosen"] += 1
    _stats_cache.update(at=time.time(), value=stats)
    return stats


def rate(e: dict, stats: dict | None = None) -> dict:
    """Rate a supplier. `e` has: key, name, website, tier_fixed (1 for Tier 1), and when known: lat/lon, address,
    phone, email, opening_hours, location, vat_basis, probe (website check), rfq_only."""
    st = (stats if stats is not None else supplier_stats()).get(e["key"], {})
    reasons: list[dict] = []
    flags: list[str] = []

    def add(part, points, maximum, text, assessed=True):
        """assessed=False: no evidence either way yet, so it doesn't count for or against the supplier."""
        reasons.append({"part": part, "points": points, "max": maximum, "text": text, "assessed": assessed})

    probe = e.get("probe") or {}
    website = e.get("website") or ""
    host = urlsplit(website).netloc.lower()

    # ---- Legitimacy (40)
    if e.get("tier_fixed") == 1 and not e.get("rfq_only"):
        ok_https = website.startswith("https://")
        add("Legitimacy", 12 if ok_https else 6, 12, "Working website" + (" with a secure (HTTPS) connection" if ok_https else ""))
    elif e.get("rfq_only"):
        add("Legitimacy", 0, 12, "Website currently offline")
        flags.append("Website offline")
    elif not website:
        add("Legitimacy", 0, 12, "No website listed")
    elif not probe:
        add("Legitimacy", 0, 12, "Website not checked yet", assessed=False)
    elif not probe.get("reachable"):
        add("Legitimacy", 0, 12, "Website didn't load")
        flags.append("Website doesn't load")
    elif not probe.get("same_site"):
        add("Legitimacy", 0, 12, f"Website redirects to a different site ({urlsplit(probe.get('final_url', '')).netloc})")
        flags.append("Website goes to a different business")
    elif probe.get("parked"):
        add("Legitimacy", 0, 12, "Website is a parked or placeholder page")
        flags.append("Parked or placeholder website")
    else:
        add("Legitimacy", 12 if probe.get("https") else 6, 12,
            "Working website" + (" with a secure (HTTPS) connection" if probe.get("https") else " (not secure: no HTTPS)"))
    if e.get("lat") is not None:
        add("Legitimacy", 8, 8, "Physical location mapped on OpenStreetMap")
    elif e.get("location"):
        add("Legitimacy", 6, 8, f"Location stated: {e['location']} (from the supplier list)")
    else:
        add("Legitimacy", 0, 8, "No mapped location")
    tier1 = e.get("tier_fixed") == 1
    if tier1:
        add("Legitimacy", 0, 13, "Address and contacts not recorded for Tier 1 suppliers (known to Angani)", assessed=False)
    else:
        add("Legitimacy", 5 if e.get("address") else 0, 5, "Street address listed" if e.get("address") else "No street address listed")
        add("Legitimacy", 5 if e.get("phone") else 0, 5, "Phone number listed" if e.get("phone") else "No phone number listed")
        add("Legitimacy", 3 if e.get("email") else 0, 3, "Email listed" if e.get("email") else "No email listed")
    ke = host.endswith(".ke")
    add("Legitimacy", 4 if ke else 0, 4, "Kenyan (.ke) web address" if ke else "Not a Kenyan (.ke) web address")
    if not tier1:
        add("Legitimacy", 3 if e.get("opening_hours") else 0, 3,
            "Opening hours published" if e.get("opening_hours") else "No opening hours published")

    # ---- Price transparency and competitiveness (35)
    sells_online = st.get("priced", 0) > 0 or (e.get("tier_fixed") == 1 and not e.get("rfq_only")) or sitecheck.readable(probe)
    looked = bool(e.get("tier_fixed") == 1 or probe.get("reachable") or st.get("offers"))
    add("Prices", 12 if sells_online else 0, 12,
        "Publishes prices online" if sells_online else
        ("No prices published online (quotes only)" if looked else "Online prices not checked yet"), assessed=looked)
    vat_known = e.get("vat_basis") in ("excl", "incl") or st.get("vat_stated", 0) > 0
    add("Prices", 5 if vat_known else 0, 5, "States whether prices include VAT" if vat_known else "VAT basis not stated",
        assessed=looked)
    if st.get("offers"):
        stock_share = st.get("stock_shown", 0) / st["offers"]
        add("Prices", 5 if stock_share >= 0.5 else 0, 5, "Shows stock levels" if stock_share >= 0.5 else "Stock levels not shown")
    else:
        add("Prices", 0, 5, "No offers seen yet to check stock information", assessed=bool(probe) and not sells_online)
    if st.get("groups", 0) >= 3:
        share = st["competitive"] / st["groups"]
        pts = round(13 * share)
        add("Prices", pts, 13, f"Cheapest or within 5% in {st['competitive']} of {st['groups']} same-product comparisons")
    else:
        add("Prices", 0, 13, "Not enough same-product comparisons yet to judge price competitiveness", assessed=False)

    # ---- Reputation (25)
    partners = probe.get("partner_brands") or []
    if partners:
        add("Reputation", 8, 8, "Website shows brand partnerships: " + ", ".join(partners[:4]))
    elif probe.get("reachable"):
        add("Reputation", 0, 8, "No brand partnerships found on the website")
    else:
        add("Reputation", 0, 8, "Brand partnerships not checked (website not read)", assessed=False)
    chosen = st.get("chosen", 0)
    add("Reputation", min(10, 5 * chosen), 10,
        f"Chosen in {chosen} Angani decision{'s' if chosen != 1 else ''}" if chosen else "Not chosen in an Angani decision yet",
        assessed=bool(chosen))
    if st.get("fetch_all", 0) >= 3:
        rate_ok = st["fetch_ok"] / st["fetch_all"]
        add("Reputation", 7 if rate_ok >= 0.9 else 3 if rate_ok >= 0.6 else 0, 7,
            f"Website answered {st['fetch_ok']} of {st['fetch_all']} ProcureFind searches")
    else:
        add("Reputation", 0, 7, "Too few searches yet to judge website reliability", assessed=False)
    add("Reputation", 0, 0, "Public reviews not checked: no review source with terms that allow it yet", assessed=False)

    # ---- Red flags
    if st.get("low_flags"):
        flags.append(f"{st['low_flags']} price{'s' if st['low_flags'] != 1 else ''} far below other suppliers")
    deb = debarred.match(e.get("name", ""))
    parts = {p: sum(r["points"] for r in reasons if r["part"] == p) for p in ("Legitimacy", "Prices", "Reputation")}
    # Score out of the checks that could actually be made, so missing history doesn't count against anyone.
    evidence = sum(r["max"] for r in reasons if r["assessed"])
    earned = sum(r["points"] for r in reasons if r["assessed"])
    score = round(100 * earned / evidence) if evidence else 0
    score -= 10 * len([f for f in flags if "Website" in f or "Parked" in f]) + (5 if st.get("low_flags") else 0)
    score = max(0, min(100, score))
    if deb:
        flags.insert(0, f"On the PPRA debarred list ({deb['debarred_on']}, {deb['period']})")
        score = 0
    if e.get("tier_fixed") == 1:
        tier = 1
    elif deb:
        tier = None
    elif parts["Legitimacy"] >= 24 and not flags and probe.get("reachable") and probe.get("same_site") and not probe.get("parked"):
        tier = 2                      # verified: real working website, mapped business details, no red flags
    else:
        tier = 3
    return {"score": score, "evidence": evidence, "parts": parts, "reasons": reasons, "flags": flags, "tier": tier, "blocked": bool(deb),
            "label": "Blocked" if deb else {1: "Trusted", 2: "Verified", 3: "Unverified"}[tier]}


def rate_tier1(s, stats: dict | None = None) -> dict:
    return rate({"key": s.key, "name": s.name, "website": s.website, "tier_fixed": 1, "location": s.location,
                 "vat_basis": s.vat_basis, "rfq_only": not s.active}, stats)


def rate_directory_row(r: dict, stats: dict | None = None) -> dict:
    return rate({"key": r["id"], "name": r["name"], "website": r.get("website"), "lat": r.get("lat"), "lon": r.get("lon"),
                 "address": r.get("address"), "phone": r.get("phone"), "email": r.get("email"),
                 "opening_hours": r.get("opening_hours"), "probe": r.get("probe") or {},
                 "location": r.get("town")}, stats)


def update_directory_rating(row: dict, stats: dict | None = None) -> dict:
    rating = rate_directory_row(row, stats)
    db.execute("UPDATE directory SET rating = ?, tier = ?, status = ? WHERE id = ?",
               (rating, rating["tier"] or 3, "blocked" if rating["blocked"] else "active", row["id"]))
    return rating

