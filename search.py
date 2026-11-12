"""Runs a search across all suppliers and merges the results (spec 13.10, plus Tier 2/3 from Phase 2).

1. Understand the request (AI for long sentences when live; otherwise the words as typed).
2. Run every Tier 1 reader, and the readable shops of nearby Tier 2/3 suppliers, at the same time,
   each with its own timeout. Results are streamed to the page as each supplier answers.
3. Reuse cached results for the same supplier and query (CACHE_MINUTES).
4. Normalise prices (VAT, units), apply the price range, group the same products, flag prices far
   below the others, and show the change since the price was last seen.
5. List nearby suppliers without online prices (with rating and contacts) for RFQs.
6. Save the search to history.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass

import httpx

import config
import db
import discover
import fixture_source
import llm
import ratings
import sitecheck
from adapters import Blocked, Fetcher, SupplierError, adapter_for
from adapters.generic import adapter_for_row, supplier_for
from matching import group, relevance
from prices import detect_unit, kes, per_unit

log = logging.getLogger("procurefind.search")

REQUEST_INTERVAL = 1.0      # seconds between requests to the same supplier site (live)
MAX_PER_SUPPLIER = 10
MAX_NEARBY_SHOWN = 30


@dataclass
class SearchRequest:
    item: str
    location: str = ""
    min_price: float | None = None
    max_price: float | None = None
    quantity: int = 1
    brand: str = ""
    sort: str = "best"          # best | price

    @property
    def query(self) -> str:
        q = " ".join(self.item.split())
        if self.brand and self.brand.lower() not in q.lower():
            q = f"{self.brand.strip()} {q}"
        return q[:150]

    @property
    def place(self) -> str:
        loc = (self.location or "").strip()
        return config.DEFAULT_LOCATION if not loc or loc.lower() in ("anywhere", "anywhere in kenya", "kenya") else loc

    def filters(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "item" and v not in (None, "")}


def http_client() -> httpx.AsyncClient:
    if config.SUPPLIER_SOURCE == "fixtures":
        return httpx.AsyncClient(transport=fixture_source.make_transport(), timeout=config.FETCH_TIMEOUT)
    return httpx.AsyncClient(timeout=config.FETCH_TIMEOUT)


def _fetcher(client: httpx.AsyncClient) -> Fetcher:
    live = config.SUPPLIER_SOURCE == "live"
    return Fetcher(client, config.USER_AGENT, min_interval=REQUEST_INTERVAL if live else 0)


def _log_fetch(key: str, ok: bool) -> None:
    db.execute("INSERT INTO fetch_log (supplier_key, ok, at) VALUES (?, ?, ?)", (key, int(ok), db.now()))


def _record_prices(dicts: list[dict]) -> None:
    for o in dicts:
        if o.get("price") is not None:
            db.execute("INSERT INTO price_history (url, supplier_key, name, price, total, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (o["url"], o["supplier_key"], o["name"], o["price"], o.get("total_incl_vat"), o["fetched_at"]))


async def _search_supplier(supplier, query: str, fetcher: Fetcher, adapter=None) -> tuple[list[dict], dict | None]:
    """Offers from one supplier, or a note saying why there are none."""
    cache_key = f"{config.SUPPLIER_SOURCE}:{query.lower()}"
    cached = db.cached_offers(supplier.key, cache_key, config.CACHE_MINUTES)
    if cached is not None:
        return cached[0], None
    adapter = adapter or adapter_for(supplier)
    try:
        offers = await asyncio.wait_for(adapter.search(query, fetcher), timeout=config.FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        _log_fetch(supplier.key, False)
        return [], _note(supplier, f"{supplier.name} didn't respond in time. Try again, or request a quote.")
    except Blocked as exc:
        _log_fetch(supplier.key, False)
        return [], _note(supplier, f"{supplier.name} didn't respond: the site {exc}. Try again later, or request a quote.")
    except SupplierError as exc:
        _log_fetch(supplier.key, False)
        return [], _note(supplier, f"{supplier.name} couldn't be read: the site {exc}. Try again, or request a quote.")
    except Exception as exc:          # a reader bug must not break the other suppliers
        log.exception("Reader for %s failed", supplier.key)
        _log_fetch(supplier.key, False)
        return [], _note(supplier, f"{supplier.name} couldn't be read ({exc.__class__.__name__}). Request a quote instead.")
    _log_fetch(supplier.key, True)
    dicts = [o.to_dict() for o in offers[:MAX_PER_SUPPLIER]]
    db.store_offers(supplier.key, cache_key, dicts)
    _record_prices(dicts)
    return dicts, None


def _note(supplier, text: str, kind: str = "error") -> dict:
    return {"supplier": supplier.name, "supplier_key": supplier.key, "text": text, "kind": kind}


def previous_price(o: dict) -> dict | None:
    """The last different-time price for the same product page, to show the change."""
    row = db.one("SELECT price, total, fetched_at FROM price_history WHERE url = ? AND fetched_at < ? "
                 "ORDER BY fetched_at DESC LIMIT 1", (o["url"], o.get("fetched_at") or db.now()))
    if not row or row["price"] is None or o.get("price") is None or abs(row["price"] - o["price"]) < 0.5:
        return None
    return {"price": row["price"], "total": row["total"], "fetched_at": row["fetched_at"],
            "pct": round((o["price"] / row["price"] - 1) * 100, 1)}


def enrich(o: dict, req: SearchRequest) -> dict:
    unit = detect_unit(o["name"])
    total = o.get("total_incl_vat")
    o.update(
        unit_kind=unit.kind, unit_size=unit.size, unit_label=unit.label,
        per_unit_incl_vat=per_unit(total, unit) if unit.size != 1 else None,
        line_total=total * req.quantity if total is not None else None,
        vat_unknown=o.get("vat_basis") == "unknown" and o.get("price") is not None,
        relevance=round(relevance(req.query, o["name"], o.get("model")), 2),
        price_change=previous_price(o),
    )
    o.setdefault("flags", [])
    return o


def in_range(o: dict, req: SearchRequest) -> bool:
    """Price range applies to the total incl. VAT, or the listed price when VAT is unknown."""
    if o.get("no_public_price"):
        return True
    value = o.get("total_incl_vat")
    if value is None:
        value = o.get("price")
    if value is None:
        return True
    if req.min_price is not None and value < req.min_price:
        return False
    if req.max_price is not None and value > req.max_price:
        return False
    return True


async def _prepare(req: SearchRequest) -> dict:
    """Filters to record; for long sentences with the AI connected, let it pick the search words."""
    filters = req.filters()
    if not config.PRACTICE_AI and len(req.item.split()) >= 5:
        parsed = await llm.parse_request(req.item)
        if parsed:
            filters["ai_understood"] = parsed["keywords"]
            req.item = parsed["keywords"]
            req.brand = req.brand or parsed["brand"]
            req.location = req.location or parsed["location"]
            if req.max_price is None and parsed["max_price"]:
                req.max_price = parsed["max_price"]
            if req.quantity == 1 and parsed["quantity"]:
                req.quantity = int(parsed["quantity"])
            filters.update({k: v for k, v in req.filters().items() if k != "sort"})
    return filters


def rfq_only_notes() -> list[dict]:
    out = []
    for s in config.SUPPLIERS:
        if not s.active:
            why = s.notes.split(":")[0] if s.notes else "no searchable website"
            out.append(_note(s, f"{s.name}: prices can't be read online ({why.lower()}). Request a quote instead.", "rfq"))
    return out


# ---- Tier 2/3: nearby suppliers ---------------------------------------------------------------------
def _stale(iso: str) -> bool:
    return discover._age_days(iso) > config.SITE_CHECK_DAYS


def _rating_brief(r: dict) -> dict:
    top = sorted(r["reasons"], key=lambda x: (not x.get("assessed", True), -x["points"]))
    return {"score": r["score"], "label": r["label"], "tier": r["tier"], "flags": r["flags"], "parts": r["parts"],
            "evidence": r.get("evidence", 100),
            "reasons": [(f"+{x['points']}/{x['max']} " if x.get("assessed", True) and x["max"] else "· ") + x["text"]
                        for x in top][:14]}


def _place_center(place: str) -> dict | None:
    row = db.one("SELECT data FROM places WHERE query = ?", (discover.location_key(place),))
    return row["data"] if row else None


def _directory_plan(place: str) -> tuple[dict, list[dict], list[dict]]:
    """(directory state, rows whose shop to search now, rows whose website to check first)."""
    state = discover.ensure(place)
    rows = [r for r in discover.suppliers_near(place) if r["status"] != "blocked"]
    readable = [r for r in rows if r.get("website") and not _stale(r.get("probed_at")) and sitecheck.readable(r.get("probe"))]
    unchecked = [r for r in rows if r.get("website") and _stale(r.get("probed_at"))]
    # added-by-hand first, then IT companies and computer shops before general electronics
    order = {"added": 0}
    unchecked.sort(key=lambda r: (order.get(r["source"], 1), r["kind"] not in ("IT company", "Computer shop", "Telecoms"), r["name"]))
    return state, readable, unchecked[:config.MAX_NEW_SITE_CHECKS]


async def _check_site(row: dict, fetcher: Fetcher) -> dict:
    try:
        probe = await asyncio.wait_for(sitecheck.check(row["website"], fetcher), timeout=config.FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        probe = {"url": row["website"], "reachable": False, "error": "timed out"}
    except Exception as exc:          # a website check must never break the search
        log.warning("Website check for %s failed: %s", row["website"], exc)
        probe = {"url": row["website"], "reachable": False, "error": exc.__class__.__name__}
    db.execute("UPDATE directory SET probe = ?, probed_at = ? WHERE id = ?", (probe, db.now(), row["id"]))
    row = {**row, "probe": probe, "probed_at": db.now()}
    row["rating"] = ratings.update_directory_rating(row)
    return row


def nearby_view(place: str, with_offers: set[str]) -> list[dict]:
    """Directory suppliers near the place, best first, for the 'nearby suppliers' list."""
    center = _place_center(place) or {}
    stats = ratings.supplier_stats()
    out = []
    for r in discover.suppliers_near(place):
        rating = r.get("rating") if isinstance(r.get("rating"), dict) and r["rating"].get("reasons") else None
        rating = rating or ratings.update_directory_rating(r, stats)
        km = discover.distance_km(center.get("lat"), center.get("lon"), r.get("lat"), r.get("lon"))
        out.append({
            "id": r["id"], "name": r["name"], "kind": r["kind"], "tier": rating["tier"], "rating": _rating_brief(rating),
            "phone": r["phone"], "email": r["email"], "website": r["website"], "address": r["address"],
            "distance_km": round(km, 1) if km is not None else None, "osm_url": r["osm_url"], "source": r["source"],
            "site": sitecheck.summary(r.get("probe") or {}) if r.get("website") else "No website listed",
            "has_offers": r["id"] in with_offers, "blocked": rating["blocked"],
        })
    out.sort(key=lambda x: (x["blocked"], x["tier"] or 9, bool(x["rating"]["flags"]), -x["rating"]["score"],
                            x["distance_km"] if x["distance_km"] is not None else 1e9))
    return out


# ---- The search ------------------------------------------------------------------------------------------
async def stream_search(req: SearchRequest, user: str = ""):
    """Yield events as each supplier answers, so results show as soon as the fastest site replies:
    {"type": "start"}, one {"type": "supplier"} per supplier, then {"type": "done", "view": ...}."""
    filters = await _prepare(req)
    query = req.query
    place = req.place
    active = [s for s in config.SUPPLIERS if s.active]
    dir_state, readable_rows, to_check = _directory_plan(place)
    tier_of = {r["id"]: ((r.get("rating") or {}).get("tier") or r.get("tier") or 3) for r in readable_rows}
    yield {"type": "start", "query": query, "quantity": req.quantity, "place": place,
           "suppliers": [{"key": s.key, "name": s.name, "tier": 1} for s in active] +
                        [{"key": r["id"], "name": r["name"], "tier": tier_of[r["id"]]} for r in readable_rows],
           "checking": len(to_check), "directory": dir_state}

    offers: list[dict] = []
    notes: list[dict] = []
    filtered_out = 0
    async with http_client() as client:
        fetcher = _fetcher(client)

        async def tier1(s):
            found, note = await _search_supplier(s, query, fetcher)
            return s.key, s.name, found, note

        async def directory_shop(row):
            tier = (row.get("rating") or {}).get("tier") or 3
            s = supplier_for(row, tier)
            try:
                found, note = await _search_supplier(s, query, fetcher, adapter_for_row(row, tier))
            except Exception as exc:  # never let a Tier 2/3 shop break the search
                log.warning("Shop search for %s failed: %s", row["id"], exc)
                found, note = [], None
            return s.key, s.name, found, None if not note else {**note, "kind": "info"}

        async def check_then_search(row):
            row = await _check_site(row, fetcher)
            if sitecheck.readable(row["probe"]) and not row["rating"]["blocked"]:
                return await directory_shop(row)
            return row["id"], row["name"], [], None

        tasks = [tier1(s) for s in active] + [directory_shop(r) for r in readable_rows] + [check_then_search(r) for r in to_check]
        for fut in asyncio.as_completed(tasks):
            key, name, found, note = await fut
            enriched = [enrich(dict(o), req) for o in found]
            kept = [o for o in enriched if in_range(o, req)]
            filtered_out += len(enriched) - len(kept)
            offers.extend(kept)
            if note:
                notes.append(note)
            yield {"type": "supplier", "key": key, "name": name, "offers": kept, "note": note,
                   "hidden_by_price": len(enriched) - len(kept)}

    notes.extend(rfq_only_notes())
    if dir_state["status"] == "running" and not dir_state["count"]:
        notes.append({"supplier": "", "supplier_key": "", "kind": "info",
                      "text": f"Finding suppliers in {place} on OpenStreetMap. The first lookup can take a few minutes; "
                              "they'll be included in your next searches."})
    elif dir_state["status"] == "error" and not dir_state["count"]:
        notes.append({"supplier": "", "supplier_key": "", "kind": "info",
                      "text": f"Couldn't load suppliers near {place} from OpenStreetMap ({dir_state['detail']}). Tier 1 results only."})
    if (req.min_price or req.max_price) and any(o["vat_unknown"] for o in offers):
        notes.append({"supplier": "", "supplier_key": "", "kind": "info",
                      "text": "Where the VAT basis is unknown, the price range was applied to the listed price."})
    if req.location and req.location.lower() not in ("anywhere", "anywhere in kenya", "kenya"):
        away = sorted({s.name for s in active if s.location and s.location.lower() != req.location.lower()})
        if away:
            notes.append({"supplier": "", "supplier_key": "", "kind": "info",
                          "text": f"{', '.join(away)} {'is' if len(away) == 1 else 'are'} based in Nairobi. "
                                  f"Confirm delivery to {req.location}."})

    # Ratings on every offer
    stats = ratings.supplier_stats()
    tier1_ratings = {s.key: _rating_brief(ratings.rate_tier1(s, stats)) for s in config.SUPPLIERS}
    dir_ratings = {}
    for key in {o["supplier_key"] for o in offers if o["supplier_key"] not in tier1_ratings}:
        row = db.one("SELECT * FROM directory WHERE id = ?", (key,))
        if row:
            dir_ratings[key] = _rating_brief(row["rating"] if isinstance(row.get("rating"), dict) and row["rating"].get("reasons")
                                             else ratings.update_directory_rating(row, stats))
    for o in offers:
        o["rating"] = tier1_ratings.get(o["supplier_key"]) or dir_ratings.get(o["supplier_key"])
        if o["rating"] and o["rating"]["tier"] and o["tier"] != 1:
            o["tier"] = o["rating"]["tier"]

    def sort_key(o):
        price = o["total_incl_vat"] if o["total_incl_vat"] is not None else (o["price"] or 0)
        no_price = o["no_public_price"] or o["total_incl_vat"] is None
        if req.sort == "price":
            return (o["tier"], no_price, price)
        return (o["tier"], -o["relevance"], no_price, price)

    offers.sort(key=sort_key)
    for i, gid in enumerate(group(offers)):
        offers[i]["group"] = gid
    ratings.flag_low_prices(offers)
    for i, o in enumerate(offers):
        o["idx"] = i

    nearby = nearby_view(place, {o["supplier_key"] for o in offers})
    extra = {"place": place, "directory": discover.run_state(place), "nearby": nearby[:MAX_NEARBY_SHOWN],
             "nearby_total": len(nearby), "tier1_ratings": tier1_ratings}
    search_id = db.execute("INSERT INTO searches (user, query, filters, offers, notes, extra, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (user, query, filters, offers, notes, extra, db.now()))
    ratings.clear_cache()
    view = build_view(search_id, query, filters, offers, notes, filtered_out, extra)
    view.update(user=user, created_at=db.now())
    yield {"type": "done", "view": view}


async def run_search(req: SearchRequest, user: str = "") -> dict:
    """The whole search at once (used by tests and non-streaming callers)."""
    view = None
    async for event in stream_search(req, user):
        if event["type"] == "done":
            view = event["view"]
    return view


def build_view(search_id: int, query: str, filters: dict, offers: list[dict], notes: list[dict], filtered_out: int = 0,
               extra: dict | None = None) -> dict:
    extra = extra or {}
    tiers: dict[int, list[dict]] = {}
    for o in offers:
        tiers.setdefault(o["tier"], []).append(o)
    groups = {}
    for o in offers:
        if o.get("group"):
            groups.setdefault(o["group"], []).append(o)
    priced = [o for o in offers if o.get("total_incl_vat") is not None]
    per_supplier: dict[str, int] = {}
    for o in offers:
        per_supplier[o["supplier"]] = per_supplier.get(o["supplier"], 0) + 1
    return {
        "id": search_id, "query": query, "filters": filters, "offers": offers, "tiers": dict(sorted(tiers.items())),
        "notes": notes, "filtered_out": filtered_out, "same_product_groups": len(groups),
        "suppliers_with_offers": len({o["supplier"] for o in offers}),
        "cheapest": kes(min(o["total_incl_vat"] for o in priced)) if priced else None,
        "cheapest_supplier": min(priced, key=lambda o: o["total_incl_vat"])["supplier"] if priced else None,
        "per_supplier": per_supplier, "quantity": int(filters.get("quantity", 1) or 1),
        "place": extra.get("place", ""), "directory": extra.get("directory", {}), "nearby": extra.get("nearby", []),
        "nearby_total": extra.get("nearby_total", 0),
    }


def load_search(search_id: int) -> dict | None:
    row = db.one("SELECT * FROM searches WHERE id = ?", (search_id,))
    if not row:
        return None
    view = build_view(row["id"], row["query"], row["filters"] or {}, row["offers"] or [], row["notes"] or [],
                      extra=row.get("extra") or {})
    view.update(user=row["user"], created_at=row["created_at"])
    return view
