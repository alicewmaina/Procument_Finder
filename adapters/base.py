"""Adapter interface, the Offer record, and polite fetching shared by all supplier readers.

Politeness rules (spec 6 / 13.10):
- fetch only when someone searches (callers use a cache),
- at most one request per second per site,
- a clear ProcureFind User-Agent,
- respect robots.txt, and never work around a refusal (403, CAPTCHA, login).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from config import Supplier


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Offer:
    supplier: str
    tier: int
    name: str                   # as shown on the site
    model: str | None           # model number / SKU if found
    url: str                    # product page link
    price_text: str             # exact text as shown, e.g. "6,000.00 KSh +VAT"
    price: float | None         # parsed number, None if no price
    currency: str               # "KES"
    vat_basis: str              # excl | incl | unknown
    total_incl_vat: float | None
    stock: str | None           # "in stock", "out of stock", None if not shown
    no_public_price: bool       # True for "Call for Quote" or missing price
    fetched_at: str             # ISO time
    supplier_key: str = ""
    image: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SupplierError(Exception):
    """The supplier site couldn't be read (down, refused, changed layout)."""


class Blocked(SupplierError):
    """robots.txt disallows the page, or the site refused the request. Never retried around."""


# ---- Polite fetching --------------------------------------------------------
_host_locks: dict[str, asyncio.Lock] = {}
_host_last: dict[str, float] = {}
_robots: dict[str, RobotFileParser] = {}


def reset_politeness_state() -> None:
    """Forget robots.txt and rate-limit state (tests, or after changing fetch mode)."""
    _host_locks.clear()
    _host_last.clear()
    _robots.clear()


@dataclass
class Fetcher:
    client: httpx.AsyncClient
    user_agent: str
    min_interval: float = 1.0           # seconds between requests to the same site
    check_robots: bool = True
    requests_made: list[str] = field(default_factory=list)

    async def _wait_turn(self, host: str) -> None:
        lock = _host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = _host_last.get(host, 0) + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _host_last[host] = time.monotonic()

    async def _raw_get(self, url: str, **kw) -> httpx.Response:
        host = urlsplit(url).netloc
        await self._wait_turn(host)
        self.requests_made.append(url)
        try:
            return await self.client.get(url, headers={"User-Agent": self.user_agent}, follow_redirects=True, **kw)
        except httpx.TimeoutException as exc:
            raise SupplierError("timed out") from exc
        except httpx.HTTPError as exc:
            raise SupplierError(f"couldn't connect ({exc.__class__.__name__})") from exc

    async def allowed(self, url: str) -> bool:
        if not self.check_robots:
            return True
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        rp = _robots.get(root)
        if rp is None:
            r = await self._raw_get(f"{root}/robots.txt")
            rp = RobotFileParser()
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            elif r.status_code in (404, 410):
                rp.parse([])                     # no robots.txt: no stated restrictions
            else:
                # 401/403/5xx: the site isn't letting us in right now. Don't cache, don't proceed.
                raise Blocked(f"refused the request (HTTP {r.status_code})")
            _robots[root] = rp
        return rp.can_fetch(self.user_agent, url)

    async def get(self, url: str, **kw) -> httpx.Response:
        if not await self.allowed(url):
            raise Blocked("robots.txt asks automated tools not to read this page")
        r = await self._raw_get(url, **kw)
        if r.status_code in (401, 403, 429):
            raise Blocked(f"refused the request (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise SupplierError(f"returned an error (HTTP {r.status_code})")
        return r


class Adapter:
    """One reader per supplier. Returns what the site says; prices.py does the maths."""

    def __init__(self, supplier: Supplier):
        self.supplier = supplier

    async def search(self, query: str, fetcher: Fetcher) -> list[Offer]:
        raise NotImplementedError
