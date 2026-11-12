"""Find Tier 2/3 suppliers near a location from OpenStreetMap (spec Phase 2).

Location lookup: Nominatim. Supplier search: Overpass API (with a backup server).
Supplier names, addresses and contacts come only from OpenStreetMap, never from the AI.
Data © OpenStreetMap contributors, available under the ODbL.

Overpass can take minutes, so the directory is built in the background and cached
for DISCOVERY_DAYS. Searches use whatever is cached and never wait for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

import config
import db

log = logging.getLogger("procurefind.discover")

# What counts as a possible IT / networking supplier on OpenStreetMap.
FILTERS = ["[shop=computer]", "[shop=electronics]", "[office=it]", "[office=telecommunication]", "[shop=telecommunication]"]
KIND_LABELS = {"computer": "Computer shop", "electronics": "Electronics shop", "it": "IT company",
               "telecommunication": "Telecoms"}
MAX_ELEMENTS = 1500


class DiscoverError(Exception):
    pass


def location_key(place: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (place or config.DEFAULT_LOCATION).lower()).strip("-") or "nairobi"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(iso: str) -> float:
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 1e9


def http_client(timeout: float) -> httpx.AsyncClient:
    if config.SUPPLIER_SOURCE == "fixtures":
        import fixture_source
        return httpx.AsyncClient(transport=fixture_source.make_transport(), timeout=timeout)
    return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": config.USER_AGENT})


# ---- Location ------------------------------------------------------------------------
_nominatim_lock = asyncio.Lock()


async def locate(place: str) -> dict:
    """'Nairobi' -> {label, key, area_id or (lat, lon, radius), lat, lon}. Cached in the database."""
    place = (place or config.DEFAULT_LOCATION).strip()
    key = location_key(place)
    cached = db.one("SELECT data FROM places WHERE query = ?", (key,))
    if cached:
        return cached["data"]
    async with _nominatim_lock:                      # Nominatim: at most one request per second
        async with http_client(30) as client:
            try:
                r = await client.get(config.NOMINATIM_URL, params={"q": f"{place}, Kenya", "format": "json", "limit": 5,
                                                                   "countrycodes": "ke"},
                                     headers={"User-Agent": config.USER_AGENT})
                r.raise_for_status()
                hits = r.json()
            except Exception as exc:
                raise DiscoverError(f"Couldn't look up '{place}' on OpenStreetMap ({exc.__class__.__name__}).") from exc
        if config.SUPPLIER_SOURCE == "live":
            await asyncio.sleep(1.1)
    if not hits:
        raise DiscoverError(f"'{place}' wasn't found in Kenya on OpenStreetMap. Try a town or county name.")
    rel = next((h for h in hits if h.get("osm_type") == "relation"), None)
    h = rel or hits[0]
    data = {"label": h.get("display_name", place).split(",")[0], "key": key,
            "lat": float(h["lat"]), "lon": float(h["lon"]),
            "area_id": 3600000000 + int(h["osm_id"]) if rel else None, "radius_m": None if rel else 10000}
    db.execute("INSERT OR REPLACE INTO places (query, data, fetched_at) VALUES (?, ?, ?)", (key, data, db.now()))
    return data


def build_query(loc: dict) -> str:
    if loc.get("area_id"):
        head, where = f"area(id:{loc['area_id']})->.a;", "(area.a)"
    else:
        head, where = "", f"(around:{loc.get('radius_m') or 10000},{loc['lat']:.5f},{loc['lon']:.5f})"
    parts = "\n".join(f"  nwr{f}{where};" for f in FILTERS)
    return f"[out:json][timeout:{int(config.OVERPASS_TIMEOUT)}];\n{head}\n(\n{parts}\n);\nout center tags {MAX_ELEMENTS};"


async def overpass(query: str) -> list[dict]:
    """Try each Overpass server in turn; they're often busy."""
    last = "no server configured"
    async with http_client(config.OVERPASS_TIMEOUT + 15) as client:
        for url in config.OVERPASS_URLS:
            try:
                r = await client.post(url, data={"data": query}, headers={"User-Agent": config.USER_AGENT})
            except Exception as exc:
                last = f"{urlsplit(url).netloc} didn't respond ({exc.__class__.__name__})"
                continue
            if r.status_code != 200:
                last = f"{urlsplit(url).netloc} returned HTTP {r.status_code}"
                continue
            data = r.json()
            if data.get("remark") and not data.get("elements"):
                last = f"{urlsplit(url).netloc}: {data['remark'][:120]}"
                continue
            return data.get("elements", [])
    raise DiscoverError(f"OpenStreetMap search failed: {last}")


# ---- Turning map data into directory rows ---------------------------------------------
def clean_website(url: str) -> str:
    url = (url or "").strip().split(";")[0].strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if not host or "." not in host or " " in host:
        return ""
    return f"{parts.scheme.lower()}://{host}{parts.path if parts.path not in ('', '/') else '/'}"


def domain_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.") if url else ""


def _address(tags: dict) -> str:
    bits = [tags.get("addr:housename"), " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])),
            tags.get("addr:place"), tags.get("addr:suburb"), tags.get("addr:city")]
    return ", ".join(b for b in bits if b)


def row_from_element(el: dict, loc_key: str) -> dict | None:
    tags = el.get("tags") or {}
    name = (tags.get("name") or tags.get("brand") or "").strip()
    if len(name) < 2:
        return None
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    website = clean_website(tags.get("website") or tags.get("contact:website") or tags.get("url") or "")
    kind = tags.get("shop") or tags.get("office") or ""
    return {
        "id": f"osm:{el.get('type', 'node')}/{el.get('id')}", "name": name, "website": website, "domain": domain_of(website),
        "phone": (tags.get("phone") or tags.get("contact:phone") or tags.get("contact:mobile") or "").strip(),
        "email": (tags.get("email") or tags.get("contact:email") or "").strip(),
        "address": _address(tags), "town": tags.get("addr:city") or tags.get("addr:suburb") or "",
        "lat": lat, "lon": lon, "kind": KIND_LABELS.get(kind, kind.replace("_", " ").capitalize()),
        "opening_hours": tags.get("opening_hours", ""), "source": "osm",
        "osm_url": f"https://www.openstreetmap.org/{el.get('type', 'node')}/{el.get('id')}",
        "location_key": loc_key, "tags": {k: v for k, v in tags.items() if not k.startswith("name:")},
    }


def tier1_domains() -> set[str]:
    return {domain_of(s.website) for s in config.SUPPLIERS}


def save_rows(rows: list[dict]) -> int:
    t1 = tier1_domains()
    saved = 0
    for r in rows:
        if r["domain"] and r["domain"] in t1:
            continue                                  # already a Tier 1 supplier
        existing = db.one("SELECT id FROM directory WHERE id = ?", (r["id"],))
        if existing:
            db.execute("""UPDATE directory SET name=?, website=?, domain=?, phone=?, email=?, address=?, town=?, lat=?, lon=?,
                          kind=?, opening_hours=?, osm_url=?, location_key=?, tags=?, updated_at=? WHERE id=?""",
                       (r["name"], r["website"], r["domain"], r["phone"], r["email"], r["address"], r["town"], r["lat"],
                        r["lon"], r["kind"], r["opening_hours"], r["osm_url"], r["location_key"], r["tags"], db.now(), r["id"]))
        else:
            db.execute("""INSERT INTO directory (id, name, website, domain, phone, email, address, town, lat, lon, kind,
                          opening_hours, source, osm_url, location_key, tags, created_at, updated_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (r["id"], r["name"], r["website"], r["domain"], r["phone"], r["email"], r["address"], r["town"],
                        r["lat"], r["lon"], r["kind"], r["opening_hours"], r["source"], r["osm_url"], r["location_key"],
                        r["tags"], db.now(), db.now()))
        saved += 1
    return saved


# ---- Background refresh -------------------------------------------------------------------
_tasks: dict[str, asyncio.Task] = {}


async def refresh(place: str) -> dict:
    """Look up the place and (re)load its suppliers from OpenStreetMap. Slow: run in the background."""
    key = location_key(place)
    db.execute("INSERT OR REPLACE INTO directory_runs (location_key, label, status, detail, count, ran_at) VALUES (?, ?, ?, ?, ?, ?)",
               (key, place, "running", "", 0, db.now()))
    try:
        loc = await locate(place)
        elements = await overpass(build_query(loc))
        rows = [r for r in (row_from_element(e, key) for e in elements) if r]
        count = save_rows(rows)
        db.execute("UPDATE directory_runs SET label=?, status='ok', detail='', count=?, ran_at=? WHERE location_key=?",
                   (loc["label"], count, db.now(), key))
        log.info("Directory for %s: %d suppliers", loc["label"], count)
        return {"status": "ok", "count": count}
    except Exception as exc:
        detail = str(exc) if isinstance(exc, DiscoverError) else f"{exc.__class__.__name__}: {exc}"
        db.execute("UPDATE directory_runs SET status='error', detail=?, ran_at=? WHERE location_key=?", (detail[:300], db.now(), key))
        log.warning("Directory refresh for %s failed: %s", place, detail)
        return {"status": "error", "detail": detail}


def start_refresh(place: str) -> bool:
    """Start a background refresh unless one is already running for this place."""
    key = location_key(place)
    task = _tasks.get(key)
    if task and not task.done():
        return False
    _tasks[key] = asyncio.get_running_loop().create_task(refresh(place))
    return True


def run_state(place: str) -> dict:
    key = location_key(place)
    run = db.one("SELECT * FROM directory_runs WHERE location_key = ?", (key,)) or {}
    running = bool(_tasks.get(key) and not _tasks[key].done())
    count = (db.one("SELECT COUNT(*) AS n FROM directory WHERE location_key = ?", (key,)) or {}).get("n", 0)
    return {"key": key, "label": run.get("label") or place, "status": "running" if running else run.get("status", "never"),
            "detail": run.get("detail", ""), "ran_at": run.get("ran_at", ""), "count": count,
            "stale": _age_days(run.get("ran_at", "")) > config.DISCOVERY_DAYS}


def ensure(place: str) -> dict:
    """State of the directory for a place; starts a background refresh when it's missing or stale."""
    state = run_state(place)
    if state["status"] != "running" and (state["status"] in ("never", "error") or state["stale"]):
        if state["status"] != "error" or _age_days(state["ran_at"]) > 1 / 24:   # retry failures after an hour
            if start_refresh(place):
                state["status"] = "running"
    return state


def suppliers_near(place: str) -> list[dict]:
    """Directory suppliers for a place (plus any added by hand), excluding blocked ones last."""
    key = location_key(place)
    return db.query("SELECT * FROM directory WHERE location_key = ? OR source = 'added' ORDER BY status = 'blocked', name",
                    (key,))


def distance_km(lat1, lon1, lat2, lon2) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def add_supplier(name: str, website: str, location: str, phone: str = "", email: str = "", user: str = "") -> dict:
    """A supplier procurement knows about but OpenStreetMap doesn't. Checked and rated like any other."""
    website = clean_website(website)
    domain = domain_of(website)
    if not name.strip() or not website:
        raise DiscoverError("Give the supplier's name and website.")
    if domain in tier1_domains():
        raise DiscoverError("That's already a Tier 1 supplier.")
    sid = f"web:{domain}"
    if db.one("SELECT id FROM directory WHERE id = ?", (sid,)):
        raise DiscoverError("That supplier is already in the directory.")
    db.execute("""INSERT INTO directory (id, name, website, domain, phone, email, town, source, location_key, added_by,
                  created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'added', ?, ?, ?, ?)""",
               (sid, name.strip()[:120], website, domain, phone.strip()[:40], email.strip()[:120], location.strip()[:60],
                location_key(location), user, db.now(), db.now()))
    return db.one("SELECT * FROM directory WHERE id = ?", (sid,))


def as_json(value) -> str:
    return json.dumps(value)
