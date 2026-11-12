"""PPRA debarred firms (red-flag check). Read from the public PPRA page, refreshed weekly."""

from __future__ import annotations

import html
import logging
import re

from rapidfuzz import fuzz

import config
import db
import discover

log = logging.getLogger("procurefind.debarred")
_SUFFIX = re.compile(r"\b(limited|ltd|company|co|enterprises?|agencies|agency|kenya|k|ke|plc|llc|inc|group|the|and)\b")


def norm(name: str) -> str:
    s = html.unescape(name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse(page: str) -> list[dict]:
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        cells = [re.sub(r"\s+", " ", c) for c in cells]
        if len(cells) >= 1 and cells[0] and cells[0].upper() != "FIRM NAME" and len(norm(cells[0])) >= 3:
            out.append({"name": cells[0], "debarred_on": cells[1] if len(cells) > 1 else "",
                        "period": cells[2] if len(cells) > 2 else ""})
    return out


async def refresh() -> dict:
    try:
        async with discover.http_client(30) as client:
            r = await client.get(config.PPRA_DEBARRED_URL, headers={"User-Agent": config.USER_AGENT}, follow_redirects=True)
        r.raise_for_status()
        firms = parse(r.text)
    except Exception as exc:
        log.warning("PPRA debarred list couldn't be read: %s", exc)
        return {"status": "error", "detail": exc.__class__.__name__}
    if not firms:
        return {"status": "error", "detail": "no firms found on the page (layout may have changed)"}
    db.execute("DELETE FROM debarred")
    for f in firms:
        db.execute("INSERT OR REPLACE INTO debarred (name, norm, debarred_on, period, fetched_at) VALUES (?, ?, ?, ?, ?)",
                   (f["name"], norm(f["name"]), f["debarred_on"], f["period"], db.now()))
    return {"status": "ok", "count": len(firms)}


def state() -> dict:
    row = db.one("SELECT COUNT(*) AS n, MAX(fetched_at) AS at FROM debarred") or {}
    return {"count": row.get("n") or 0, "fetched_at": row.get("at") or ""}


def is_stale() -> bool:
    return discover._age_days(state()["fetched_at"]) > config.DISCOVERY_DAYS


def match(name: str) -> dict | None:
    """The debarred entry for this supplier name, if any (exact after normalising, or very close)."""
    n = norm(name)
    if len(n) < 3:
        return None
    for row in db.query("SELECT * FROM debarred"):
        if row["norm"] == n or (len(n) >= 6 and fuzz.ratio(row["norm"], n) >= 94):
            return row
    return None
