"""ProcureFind Phase 1: Tier 1 supplier search, price comparison incl. VAT, RFQ drafts, audit trail."""

from __future__ import annotations

import asyncio
import csv
import json
import io
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import config
import db
import debarred
import discover
import llm
import ratings
import sitecheck
from matching import compare as match_compare
from prices import kes, units_comparable, Unit
from rfq import RFQRequest
from search import SearchRequest, _fetcher, http_client, load_search, stream_search

BASE = Path(__file__).parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("procurefind")



@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    db.sync_suppliers(config.SUPPLIERS)
    # Background: PPRA debarred list and the nearby-supplier directory for the default location.
    if debarred.is_stale():
        asyncio.get_running_loop().create_task(debarred.refresh())
    discover.ensure(config.DEFAULT_LOCATION)
    yield


# Demo build: no sign-in. Every action is recorded under DEMO_USER.
# The session is only used for one-off messages ("Saved: ..."), so a temporary key is fine.
app = FastAPI(title="ProcureFind", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY or secrets.token_hex(32), same_site="lax")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["kes"] = kes
templates.env.filters["when"] = lambda iso: (iso or "").replace("T", " ")[:16]
templates.env.filters["host"] = lambda url: urlsplit(url or "").netloc.replace("www.", "")


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", config.DEMO_USER)
    ctx.setdefault("practice", config.PRACTICE_AI)
    ctx.setdefault("source", config.SUPPLIER_SOURCE)
    ctx.setdefault("vat_pct", round(config.VAT_RATE * 100))
    ctx.setdefault("flash", request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx, status_code=ctx.pop("status_code", 200))


def user_of(request: Request) -> str:
    return config.DEMO_USER


def _num(value: str | None) -> float | None:
    try:
        v = float(str(value).replace(",", "").strip())
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def _int(value, default: int = 1, lo: int = 1, hi: int = 100000) -> int:
    try:
        return min(max(int(float(str(value))), lo), hi)
    except (TypeError, ValueError):
        return default


# ---- Dashboard and search ----------------------------------------------------------------
def _search_request(q: str, location: str = "", min_price: str = "", max_price: str = "", quantity: str = "1",
                    brand: str = "", sort: str = "best") -> SearchRequest:
    return SearchRequest(item=q.strip()[:150], location=location.strip()[:60], min_price=_num(min_price),
                         max_price=_num(max_price), quantity=_int(quantity), brand=brand.strip()[:40],
                         sort="price" if sort == "price" else "best")


def recent_searches(limit: int = 8) -> list[dict]:
    return db.query("SELECT id, query, user, created_at, json_array_length(offers) AS n FROM searches "
                    "ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = "", location: str = "", min_price: str = "", max_price: str = "",
                    quantity: str = "1", brand: str = "", sort: str = "best", id: int | None = None):
    """The one main page: search box, recent searches, and results that stream in below."""
    saved = None
    if id is not None:
        saved = load_search(id)
        if not saved:
            request.session["flash"] = "That search isn't in the history."
            return RedirectResponse("/", status_code=303)
    pending = None
    if q.strip() and not saved:
        pending = {"q": q.strip()[:150], "location": location, "min_price": min_price, "max_price": max_price,
                   "quantity": str(_int(quantity)), "brand": brand, "sort": sort}
    form = pending or {"q": saved["query"] if saved else "", "location": "", "min_price": "", "max_price": "",
                       "quantity": str((saved or {}).get("quantity", 1)), "brand": "", "sort": "best"}
    if saved:
        f = saved["filters"]
        form.update({k: str(f.get(k, "")) for k in ("location", "min_price", "max_price", "brand") if f.get(k)})
    return render(request, "dashboard.html", recent=recent_searches(), saved=saved, pending=pending, form=form,
                  active=[s for s in config.SUPPLIERS if s.active], rfq_only=[s for s in config.SUPPLIERS if not s.active],
                  max_compare=config.MAX_COMPARE, directory=discover.run_state(config.DEFAULT_LOCATION),
                  active_list=[{"key": s.key, "name": s.name} for s in config.SUPPLIERS if s.active])


@app.get("/api/search")
async def api_search(request: Request, q: str = "", location: str = "", min_price: str = "", max_price: str = "",
                     quantity: str = "1", brand: str = "", sort: str = "best"):
    """Search results as newline-delimited JSON, one line per supplier as it answers, then a final 'done' line."""
    if not q.strip():
        return JSONResponse({"error": "Type what you need to buy"}, status_code=400)
    req = _search_request(q, location, min_price, max_price, quantity, brand, sort)

    async def lines():
        async for event in stream_search(req, user_of(request)):
            yield json.dumps(event) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/results")
async def results(request: Request, id: int | None = None):
    """Old links: everything now lives on the dashboard."""
    if id is not None:
        return RedirectResponse(f"/?id={id}", status_code=303)
    query = request.url.query
    return RedirectResponse("/" + (f"?{query}" if query else ""), status_code=303)


# ---- Compare and choose -----------------------------------------------------------------
def _comparison_view(comp: dict) -> dict:
    offers = comp["offers"]
    units = [Unit(o.get("unit_kind", "each"), o.get("unit_size", 1), o.get("unit_label", "each")) for o in offers]
    first = offers[0]
    for o in offers[1:]:
        verdict, reason = match_compare(first["name"], o["name"], first.get("model"), o.get("model"))
        o["match_verdict"], o["match_reason"] = verdict, reason
    priced = [o["total_incl_vat"] for o in offers if o.get("total_incl_vat") is not None]
    return {**comp, "units_differ": not units_comparable(units), "best_total": min(priced) if priced else None}


@app.get("/compare", response_class=HTMLResponse)
async def compare_tab(request: Request):
    """Compare tab: the offers collected for comparison (kept in the browser) and past comparisons."""
    past = db.query("SELECT c.id, c.offers, c.chosen_offer, c.reason, c.user, c.created_at, c.decided_at, s.query "
                    "FROM comparisons c LEFT JOIN searches s ON s.id = c.search_id ORDER BY c.id DESC LIMIT 50")
    return render(request, "compare_tab.html", past=past, max_compare=config.MAX_COMPARE)


@app.post("/compare")
async def compare(request: Request):
    """Compare 2-5 offers. Offers are referenced as "search_id:offer_index" (`ref`), so they can come
    from different searches; the older form (search_id + offer indexes) also works."""
    form = await request.form()
    refs: list[tuple[int, int]] = []
    for v in form.getlist("ref"):
        sid, _, idx = str(v).partition(":")
        pair = (_int(sid, -1, -1), _int(idx, -1, -1))
        if min(pair) >= 0 and pair not in refs:
            refs.append(pair)
    sid0 = _int(form.get("search_id"), -1, -1)
    for v in form.getlist("offer"):
        pair = (sid0, _int(v, -1, -1))
        if min(pair) >= 0 and pair not in refs:
            refs.append(pair)
    back = f"/?id={refs[0][0]}" if refs and form.get("search_id") else "/compare"
    if not 2 <= len(refs) <= config.MAX_COMPARE:
        request.session["flash"] = f"Pick between 2 and {config.MAX_COMPARE} offers to compare."
        return RedirectResponse(back, status_code=303)
    offers, searches = [], {}
    for sid, idx in refs:
        if sid not in searches:
            searches[sid] = load_search(sid)
        view = searches[sid]
        if not view or idx >= len(view["offers"]):
            request.session["flash"] = "One of those offers is no longer available. Search again."
            return RedirectResponse(back, status_code=303)
        offers.append({**view["offers"][idx], "search_id": sid})
    cid = db.execute("INSERT INTO comparisons (search_id, offers, user, created_at) VALUES (?, ?, ?, ?)",
                     (refs[0][0], offers, user_of(request), db.now()))
    return RedirectResponse(f"/compare/{cid}", status_code=303)


@app.get("/compare/{cid}", response_class=HTMLResponse)
async def compare_page(request: Request, cid: int):
    comp = db.one("SELECT * FROM comparisons WHERE id = ?", (cid,))
    if not comp:
        return render(request, "message.html", title="Comparison not found", text="", status_code=404)
    search = db.one("SELECT query, filters FROM searches WHERE id = ?", (comp["search_id"],)) or {}
    quantity = _int((search.get("filters") or {}).get("quantity", 1))
    c = _comparison_view(comp)
    return render(request, "compare.html", c=c, quantity=quantity, query=search.get("query", ""),
                  plain=llm.plain_summary(c["offers"], quantity, c["units_differ"]))


@app.post("/api/compare/{cid}/summary")
async def compare_summary(cid: int):
    comp = db.one("SELECT * FROM comparisons WHERE id = ?", (cid,))
    if not comp:
        return JSONResponse({"error": "not found"}, status_code=404)
    search = db.one("SELECT filters FROM searches WHERE id = ?", (comp["search_id"],)) or {}
    quantity = _int((search.get("filters") or {}).get("quantity", 1))
    c = _comparison_view(comp)
    return await llm.compare_summary(c["offers"], quantity, c["units_differ"])


@app.post("/compare/{cid}/choose")
async def choose(request: Request, cid: int, offer: int = Form(...), reason: str = Form("")):
    comp = db.one("SELECT * FROM comparisons WHERE id = ?", (cid,))
    if not comp or not 0 <= offer < len(comp["offers"]):
        return render(request, "message.html", title="Comparison not found", text="", status_code=404)
    if not reason.strip():
        request.session["flash"] = "Please give a short reason for the choice (it goes in the audit trail)."
        return RedirectResponse(f"/compare/{cid}", status_code=303)
    db.execute("UPDATE comparisons SET chosen_offer = ?, reason = ?, user = ?, decided_at = ? WHERE id = ?",
               (comp["offers"][offer], reason.strip()[:1000], user_of(request), db.now(), cid))
    request.session["flash"] = f"Saved: {comp['offers'][offer]['supplier']} chosen."
    return RedirectResponse("/history", status_code=303)


# ---- RFQ ----------------------------------------------------------------------------------
@app.get("/rfq", response_class=HTMLResponse)
async def rfq_form(request: Request, search_id: int | None = None, offer: int | None = None,
                   supplier: str = "", item: str = "", quantity: str = "1"):
    pre = {"item": item, "supplier": supplier, "quantity": _int(quantity), "product_url": "", "specs": "",
           "delivery_location": "", "required_by": ""}
    if search_id is not None:
        view = load_search(search_id)
        if view:
            pre.update(item=pre["item"] or view["query"], quantity=_int(view["filters"].get("quantity", 1)),
                       delivery_location=view["filters"].get("location", ""))
            if offer is not None and 0 <= offer < len(view["offers"]):
                o = view["offers"][offer]
                pre.update(item=o["name"], supplier=o["supplier"], product_url=o["url"],
                           specs=f"Model {o['model']}" if o.get("model") else "")
    if supplier and not pre["supplier"]:
        pre["supplier"] = supplier
    known = {s.key: s.name for s in config.SUPPLIERS}
    if pre["supplier"] in known:
        pre["supplier"] = known[pre["supplier"]]
    elif pre["supplier"].startswith(("osm:", "web:")):
        row = db.one("SELECT name, town FROM directory WHERE id = ?", (pre["supplier"],))
        pre["supplier"] = row["name"] if row else ""
    return render(request, "rfq.html", pre=pre, draft=None, suppliers=config.SUPPLIERS)


@app.post("/rfq", response_class=HTMLResponse)
async def rfq_make(request: Request, item: str = Form(...), specs: str = Form(""), quantity: str = Form("1"),
                   supplier: str = Form(""), delivery_location: str = Form(""), required_by: str = Form(""),
                   product_url: str = Form("")):
    r = RFQRequest(item=item.strip()[:200], specs=specs.strip()[:1000], quantity=_int(quantity),
                   supplier=supplier.strip()[:120], delivery_location=delivery_location.strip()[:120],
                   required_by=required_by.strip()[:40], product_url=product_url.strip()[:500],
                   requester=user_of(request))
    if not r.item:
        return RedirectResponse("/rfq", status_code=303)
    draft = await llm.rfq_draft(r)
    db.execute("INSERT INTO rfqs (user, item, specs, quantity, supplier, subject, body, source, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (r.requester, r.item, r.specs, r.quantity, r.supplier, draft["subject"], draft["body"],
                draft.get("source", "template"), db.now()))
    return render(request, "rfq.html", pre=vars(r), draft=draft, suppliers=config.SUPPLIERS)


# ---- Suppliers, history, health ------------------------------------------------------------
@app.get("/suppliers", response_class=HTMLResponse)
async def suppliers(request: Request, location: str = ""):
    """Tier 1 suppliers and the directory of nearby suppliers (Tier 2/3), each with a rating and its reasons."""
    place = location.strip() or config.DEFAULT_LOCATION
    stats = ratings.supplier_stats()
    tier1 = [{"s": s, "rating": ratings.rate_tier1(s, stats)} for s in config.SUPPLIERS]
    state = discover.ensure(place)
    center = (db.one("SELECT data FROM places WHERE query = ?", (discover.location_key(place),)) or {}).get("data") or {}
    rows = []
    for r in discover.suppliers_near(place):
        rating = r["rating"] if isinstance(r.get("rating"), dict) and r["rating"].get("reasons") else ratings.update_directory_rating(r, stats)
        km = discover.distance_km(center.get("lat"), center.get("lon"), r.get("lat"), r.get("lon"))
        rows.append({**r, "rating": rating, "distance_km": round(km, 1) if km is not None else None,
                     "site": sitecheck.summary(r.get("probe") or {}) if r.get("website") else "No website listed"})
    rows.sort(key=lambda r: (r["rating"]["blocked"], r["rating"]["tier"] or 9, bool(r["rating"]["flags"]), -r["rating"]["score"], r["name"]))
    counts = {"2": sum(1 for r in rows if r["rating"]["tier"] == 2), "3": sum(1 for r in rows if r["rating"]["tier"] == 3),
              "blocked": sum(1 for r in rows if r["rating"]["blocked"]), "web": sum(1 for r in rows if r["website"])}
    return render(request, "suppliers.html", tier1=tier1, rows=rows, state=state, place=place, counts=counts,
                  debarred=debarred.state(), default_location=config.DEFAULT_LOCATION)


@app.post("/suppliers/refresh")
async def suppliers_refresh(request: Request, location: str = Form("")):
    place = location.strip() or config.DEFAULT_LOCATION
    started = discover.start_refresh(place)
    asyncio.get_running_loop().create_task(debarred.refresh())
    request.session["flash"] = (f"Updating suppliers near {place} from OpenStreetMap. This can take a few minutes."
                                if started else f"Already updating suppliers near {place}.")
    return RedirectResponse(f"/suppliers?location={place}", status_code=303)


@app.post("/suppliers/add")
async def suppliers_add(request: Request, name: str = Form(""), website: str = Form(""), location: str = Form(""),
                        phone: str = Form(""), email: str = Form("")):
    """Add a supplier procurement knows about. Its website is checked and it's rated like any other."""
    place = location.strip() or config.DEFAULT_LOCATION
    try:
        row = discover.add_supplier(name, website, place, phone, email, user_of(request))
    except discover.DiscoverError as exc:
        request.session["flash"] = str(exc)
        return RedirectResponse(f"/suppliers?location={place}#add", status_code=303)
    async with http_client() as client:
        try:
            probe = await asyncio.wait_for(sitecheck.check(row["website"], _fetcher(client)), timeout=config.FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            probe = {"url": row["website"], "reachable": False, "error": "timed out"}
    db.execute("UPDATE directory SET probe = ?, probed_at = ? WHERE id = ?", (probe, db.now(), row["id"]))
    rating = ratings.update_directory_rating({**row, "probe": probe})
    request.session["flash"] = (f"Added {row['name']}: Tier {rating['tier']} ({rating['label']}), rating {rating['score']}. "
                                f"{sitecheck.summary(probe)}." if rating["tier"] else
                                f"Added {row['name']}, but it's on the PPRA debarred list, so it's blocked.")
    return RedirectResponse(f"/suppliers?location={place}", status_code=303)


def _history_rows() -> tuple[list[dict], list[dict], list[dict]]:
    searches = db.query("SELECT id, user, query, filters, created_at, json_array_length(offers) AS n "
                        "FROM searches ORDER BY id DESC LIMIT 500")
    decisions = db.query("SELECT c.*, s.query FROM comparisons c LEFT JOIN searches s ON s.id = c.search_id "
                         "ORDER BY c.id DESC LIMIT 500")
    rfqs = db.query("SELECT * FROM rfqs ORDER BY id DESC LIMIT 500")
    return searches, decisions, rfqs


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request):
    searches, decisions, rfqs = _history_rows()
    return render(request, "history.html", searches=searches, decisions=decisions, rfqs=rfqs)


@app.get("/history.csv")
async def history_csv():
    _, decisions, _ = _history_rows()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["comparison_id", "date", "user", "search", "offers_compared", "chosen_supplier", "chosen_product",
                "listed_price", "vat_basis", "total_incl_vat", "stock", "source_link", "price_fetched_at", "reason"])
    for d in decisions:
        ch = d.get("chosen_offer") or {}
        compared = " | ".join(f"{o['supplier']}: {o['name']} ({kes(o.get('total_incl_vat')) if o.get('total_incl_vat') is not None else 'no total'})"
                              for o in d["offers"])
        w.writerow([d["id"], d.get("decided_at") or d["created_at"], d["user"], d.get("query") or "", compared,
                    ch.get("supplier", ""), ch.get("name", ""), ch.get("price_text", ""), ch.get("vat_basis", ""),
                    round(ch["total_incl_vat"]) if ch.get("total_incl_vat") is not None else "",
                    ch.get("stock") or "", ch.get("url", ""), ch.get("fetched_at", ""), d.get("reason", "")])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="procurefind-decisions.csv"'})


_site_status: dict = {"at": 0.0, "value": None}


async def supplier_status() -> list[dict]:
    """One polite request per Tier 1 site (robots.txt), cached for 5 minutes."""
    if _site_status["value"] is not None and time.time() - _site_status["at"] < 300:
        return _site_status["value"]
    if config.SUPPLIER_SOURCE == "fixtures":
        return [{"name": s.name, "key": s.key, "status": s.status, "ok": s.active,
                 "detail": "Practice mode: saved pages, not checked live" if s.active else "RFQ only"}
                for s in config.SUPPLIERS]
    out = []
    async with http_client() as client:
        f = _fetcher(client)
        for s in config.SUPPLIERS:
            url = s.website.rstrip("/") + "/robots.txt"
            try:
                r = await f._raw_get(url)
                ok = r.status_code < 500 and r.status_code not in (401, 403)
                if r.history and "suspended" in str(r.url):
                    ok, detail = False, "Hosting account suspended"
                else:
                    detail = "Reachable" if ok else f"HTTP {r.status_code}"
            except Exception as exc:
                ok, detail = False, str(exc) or exc.__class__.__name__
            out.append({"name": s.name, "key": s.key, "status": s.status, "ok": ok, "detail": detail})
    _site_status.update(at=time.time(), value=out)
    return out


@app.get("/health")
async def health(request: Request, format: str = ""):
    ai = await llm.health()
    sites = await supplier_status()
    try:
        db.one("SELECT 1 AS ok")
        db_ok = True
    except Exception:
        db_ok = False
    data = {"app": "ok", "database": db_ok, "ai": ai, "supplier_source": config.SUPPLIER_SOURCE, "suppliers": sites,
            "directory": discover.run_state(config.DEFAULT_LOCATION), "debarred": debarred.state()}
    if format == "json" or "application/json" in request.headers.get("accept", ""):
        return JSONResponse(data)
    return render(request, "health.html", h=data)
