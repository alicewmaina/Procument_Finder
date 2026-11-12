"""Price text parsing, VAT normalisation and unit detection.

Adapters return what the page says; everything numeric is worked out here.
VAT is never guessed: an unknown basis leaves the total empty and flagged.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

NO_PRICE_PHRASES = re.compile(
    r"call\s+for\s+(a\s+)?quote|request\s+(a\s+)?quote|contact\s+(us\s+)?for\s+(a\s+)?price|price\s+on\s+request|"
    r"enquire\s+for\s+price|ask\s+for\s+price", re.I)
EXCL_VAT = re.compile(r"\+\s*VAT|(?<![a-z])excl(?:\.|uding|usive)?\s*(?:of\s+)?(?:VAT|tax)\b|\bex\.?\s*VAT\b|\bVAT\s+excl", re.I)
INCL_VAT = re.compile(r"(?<![a-z])incl(?:\.|uding|usive)?\s*(?:of\s+)?(?:VAT|tax)\b|\bVAT\s+incl", re.I)
_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_CUR = r"(?:KSh|KShs|Kshs|Ksh|KES|Kes)\.?"
CURRENCY_AMOUNT = re.compile(rf"{_CUR}\s*{_NUM}|{_NUM}\s*{_CUR}", re.I)
BARE_AMOUNT = re.compile(rf"(?<![\w.%]){_NUM}(?![\w%])")


@dataclass
class ParsedPrice:
    price: float | None
    vat_basis: str | None       # excl | incl | None when the text doesn't say
    no_public_price: bool


def clean_text(text: str | None) -> str:
    """HTML to single-spaced plain text (entities and &nbsp; decoded)."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def vat_wording(text: str) -> str | None:
    if EXCL_VAT.search(text):
        return "excl"
    if INCL_VAT.search(text):
        return "incl"
    return None


def parse_price_text(text: str | None) -> ParsedPrice:
    """Read the current price, VAT wording and 'no price' signals from price text.

    A sale shows the old price crossed out next to the new one: the lower
    amount is the current price.
    """
    text = clean_text(text)
    if not text or NO_PRICE_PHRASES.search(text):
        return ParsedPrice(None, vat_wording(text), True)
    vat = vat_wording(text)
    no_pct = re.sub(r"\d+(?:\.\d+)?\s*%", " ", text)
    amounts = [_num(a or b) for a, b in CURRENCY_AMOUNT.findall(no_pct)]
    if not amounts:
        bare = [_num(m) for m in BARE_AMOUNT.findall(no_pct)]
        amounts = bare if len(bare) in (1, 2) else []
    amounts = [a for a in amounts if a > 0]
    if not amounts:
        return ParsedPrice(None, vat, True)
    return ParsedPrice(min(amounts), vat, False)


def total_incl_vat(price: float | None, vat_basis: str, vat_rate: float) -> float | None:
    """excl -> add VAT; incl -> as shown; unknown -> None (flagged, never guessed)."""
    if price is None:
        return None
    if vat_basis == "excl":
        return price * (1 + vat_rate)
    if vat_basis == "incl":
        return price
    return None


def kes(amount: float | None, decimals: int = 0) -> str:
    """Display format: whole shillings by default."""
    if amount is None:
        return "-"
    return f"KES {amount:,.{decimals}f}"


# ---- Units -----------------------------------------------------------------
_METRE = r"(?:m|mtr|mtrs|metre|metres|meter|meters)"
_BULK = re.compile(rf"(\d+(?:\.\d+)?)\s*{_METRE}\b[^\d]{{0,20}}?\b(box|roll|reel|drum|spool|pull\s*box)\b|"
                   rf"\b(box|roll|reel|drum|spool)\b[^\d]{{0,20}}?(\d+(?:\.\d+)?)\s*{_METRE}\b", re.I)
_PER_METRE = re.compile(rf"\bper\s+{_METRE}\b|/\s*{_METRE}\b", re.I)
_PACK = re.compile(r"\b(?:pack|box|bag|set)\s+of\s+(\d+)\b|\b(\d+)\s*(?:pcs|pieces|pc|pack|-pack)\b", re.I)
_CABLE_BOX = re.compile(rf"\b(\d{{3}})\s*{_METRE}\b", re.I)   # e.g. "305M" cable boxes


@dataclass(frozen=True)
class Unit:
    kind: str       # "each" | "metre" | "piece"
    size: float     # how many of `kind` the price covers
    label: str      # human label, e.g. "box of 305 m"


EACH = Unit("each", 1, "each")


def detect_unit(name: str) -> Unit:
    """Detect per-metre, per-box and per-pack pricing from a product name.

    A length on its own ("Patch Cord 3m") is the product's length, not the
    quantity sold, so it stays "each". Only bulk words (box, roll, reel...),
    "per metre", or a three-digit cable length (e.g. "305M") count as metres.
    """
    name = clean_text(name)
    if _PER_METRE.search(name):
        return Unit("metre", 1, "per metre")
    m = _BULK.search(name)
    if m:
        size = float(m.group(1) or m.group(4))
        word = (m.group(2) or m.group(3)).lower()
        return Unit("metre", size, f"{word} of {size:g} m")
    if re.search(r"\bcable\b|twisted\s+pair|\bUTP\b|\bFTP\b", name, re.I) and not re.search(r"patch", name, re.I):
        m = _CABLE_BOX.search(name)
        if m and float(m.group(1)) >= 100:
            size = float(m.group(1))
            return Unit("metre", size, f"box of {size:g} m")
    m = _PACK.search(name)
    if m:
        size = float(m.group(1) or m.group(2))
        if size > 1:
            return Unit("piece", size, f"pack of {size:g}")
    return EACH


def per_unit(total: float | None, unit: Unit) -> float | None:
    if total is None or unit.size <= 0:
        return None
    return total / unit.size


def units_comparable(units: list[Unit]) -> bool:
    """Prices can be compared directly only if every offer is sold per the same kind of unit."""
    return len({u.kind for u in units}) <= 1
