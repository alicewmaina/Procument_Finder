"""Qwen3 (via Ollama on gpu-01) for summaries, RFQ wording and request parsing.

Reused from Lead Finder: JSON parsing, practice mode and the cached health check.
LLM_BASE_URL=mock is practice mode: plain, rule-based text and no AI server.

Guardrail: the AI never supplies prices, suppliers or products. Every number
in an AI reply must already be in the facts we gave it, otherwise the reply
is discarded and the plain version is shown.
"""

from __future__ import annotations

import json
import re
import time

import httpx

import config
from prices import kes
from rfq import RFQRequest, template_draft

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ---- Ollama client ------------------------------------------------------------
def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model reply contained no JSON")
    return json.loads(text[start:end + 1])


async def chat_json(system: str, user: str, temperature: float = 0.2) -> dict:
    payload = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": "json", "think": False, "stream": False,
        "options": {"temperature": temperature, "num_ctx": 8192},
    }
    last: Exception | None = None
    async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT) as client:
        for _ in range(2):
            r = await client.post(f"{config.LLM_BASE_URL}/api/chat", json=payload)
            r.raise_for_status()
            try:
                return parse_json(r.json()["message"]["content"])
            except (ValueError, KeyError) as exc:
                last = exc
    raise ValueError(f"The AI returned an unreadable answer: {last}")


_health_cache: dict = {"at": 0.0, "value": None}


async def health() -> dict:
    """AI server status, cached for 30 seconds so pages stay fast when it's offline."""
    if _health_cache["value"] and time.time() - _health_cache["at"] < 30:
        return _health_cache["value"]
    _health_cache.update(value=await _health(), at=time.time())
    return _health_cache["value"]


async def _health() -> dict:
    if config.PRACTICE_AI:
        return {"ok": True, "mode": "practice", "model": "none", "detail": "Practice mode: no AI server used"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{config.LLM_BASE_URL}/api/tags")
        names = [m["name"] for m in r.json().get("models", [])]
        ok = config.LLM_MODEL in names
        return {"ok": ok, "mode": "live", "model": config.LLM_MODEL,
                "detail": "Ready" if ok else f"{config.LLM_MODEL} not found on the AI server"}
    except Exception as exc:
        return {"ok": False, "mode": "live", "model": config.LLM_MODEL,
                "detail": f"AI server unreachable ({exc.__class__.__name__})"}


def reset_health_cache() -> None:
    _health_cache.update(at=0.0, value=None)


# ---- Fact checking --------------------------------------------------------------
def _numbers(text: str) -> set[float]:
    out = set()
    for n in _NUMBER.findall(text or ""):
        try:
            out.add(float(n.replace(",", "")))
        except ValueError:
            pass
    return out


def numbers_supported(reply_text: str, facts_text: str) -> bool:
    """Every number in the reply must appear in the facts (small counts like 'two' or '3' are fine)."""
    allowed = _numbers(facts_text)
    for n in _numbers(reply_text):
        if n <= 10 or n == 16:          # counts, and the VAT rate
            continue
        if not any(abs(n - a) < 0.51 for a in allowed):
            return False
    return True


# ---- Comparison summary -----------------------------------------------------------
def comparison_facts(offers: list[dict], quantity: int) -> str:
    """The only facts the AI may use, one line per offer, with precomputed differences."""
    priced = [o for o in offers if o.get("total_incl_vat") is not None]
    cheapest = min((o["total_incl_vat"] for o in priced), default=None)
    lines = [f"Quantity requested: {quantity}"]
    for i, o in enumerate(offers, 1):
        parts = [f"Offer {i}: {o['supplier']} (Tier {o['tier']})", f"product: {o['name']}"]
        if o.get("no_public_price"):
            parts.append("no public price (needs a quotation)")
        elif o.get("total_incl_vat") is None:
            parts.append(f"listed price {kes(o.get('price'))}, VAT basis unknown so total not known")
        else:
            parts.append(f"total incl. VAT {kes(o['total_incl_vat'])} each, {kes(o['total_incl_vat'] * quantity)} for {quantity}")
            if cheapest and o["total_incl_vat"] > cheapest:
                pct = round((o["total_incl_vat"] / cheapest - 1) * 100)
                parts.append(f"{pct}% more than the cheapest")
        if o.get("unit_label") and o["unit_label"] != "each":
            parts.append(f"sold per {o['unit_label']}")
        parts.append(f"stock: {o.get('stock') or 'not shown on the website'}")
        if o.get("match"):
            parts.append(o["match"])
        lines.append("; ".join(parts))
    return "\n".join(lines)


def plain_summary(offers: list[dict], quantity: int, units_differ: bool = False) -> dict:
    """Rule-based summary used in practice mode and whenever the AI is unavailable or fails a check."""
    notes = []
    priced = sorted((o for o in offers if o.get("total_incl_vat") is not None), key=lambda o: o["total_incl_vat"])
    if units_differ:
        notes.append("Units differ (e.g. per metre vs per box), so prices can't be compared directly. Check each page.")
    if priced:
        best = priced[0]
        summary = (f"{best['supplier']} is cheapest at {kes(best['total_incl_vat'])} incl. VAT each"
                   + (f" ({kes(best['total_incl_vat'] * quantity)} for {quantity})" if quantity > 1 else "") + ".")
        if best.get("stock") == "out of stock":
            summary += " It is shown as out of stock."
            in_stock = [o for o in priced if o.get("stock") == "in stock"]
            if in_stock:
                alt = in_stock[0]
                pct = round((alt["total_incl_vat"] / best["total_incl_vat"] - 1) * 100)
                summary += f" {alt['supplier']} has it in stock for {kes(alt['total_incl_vat'])} ({pct}% more)."
        if len(priced) > 1:
            gap = priced[-1]["total_incl_vat"] - best["total_incl_vat"]
            notes.append(f"The difference between the cheapest and the most expensive is {kes(gap)} per item incl. VAT.")
        cheapest = best["supplier"]
    else:
        summary, cheapest = "None of these offers has a total price incl. VAT yet.", ""
    for o in offers:
        if o.get("no_public_price"):
            notes.append(f"{o['supplier']}: no public price, request a quotation.")
        elif o.get("total_incl_vat") is None:
            notes.append(f"{o['supplier']}: VAT basis unknown, so the total isn't shown. Confirm with the supplier.")
        if o.get("stock") is None and not o.get("no_public_price"):
            notes.append(f"{o['supplier']}: stock isn't shown on the website.")
    seen, uniq = set(), []
    for n in notes:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return {"summary": summary, "cheapest": cheapest, "notes": uniq, "source": "plain"}


SUMMARY_SYSTEM = """You help an IT procurement team in Kenya choose between supplier offers.
Write a short, neutral comparison using ONLY the facts given. Never invent prices, suppliers,
products, stock levels, delivery times or percentages; use numbers exactly as written in the facts.
Mention trade-offs (price vs stock, tier, unknown VAT, no public price). No marketing language.
Reply with ONLY a JSON object: {"summary": "2-3 sentences", "cheapest": "supplier name or empty",
"notes": ["up to 4 short points"]}"""


async def compare_summary(offers: list[dict], quantity: int = 1, units_differ: bool = False) -> dict:
    plain = plain_summary(offers, quantity, units_differ)
    if config.PRACTICE_AI:
        return plain
    facts = comparison_facts(offers, quantity)
    try:
        data = await chat_json(SUMMARY_SYSTEM, f"Facts:\n{facts}\n\nReturn the JSON object.")
    except Exception as exc:
        return {**plain, "warning": f"AI unavailable ({exc.__class__.__name__}); showing the plain summary."}
    summary = str(data.get("summary", ""))[:1200]
    notes = [str(n)[:300] for n in data.get("notes", []) if isinstance(n, (str, int, float))][:4] \
        if isinstance(data.get("notes"), list) else []
    cheapest = str(data.get("cheapest", ""))[:120]
    names = {o["supplier"] for o in offers}
    reply_text = " ".join([summary, *notes])
    if not summary or not numbers_supported(reply_text, facts) or (cheapest and cheapest not in names):
        return {**plain, "warning": "The AI summary didn't match the facts, so the plain summary is shown."}
    if units_differ:
        notes.insert(0, plain["notes"][0])
    return {"summary": summary, "cheapest": cheapest, "notes": notes, "source": "ai"}


# ---- RFQ draft ------------------------------------------------------------------------
RFQ_SYSTEM = """You write short, polite requests for quotation from Angani (a Kenyan cloud company)
to IT suppliers. Use ONLY the details given. Do not add prices, numbers, dates or specifications
that aren't in the details. Always ask the supplier to state whether prices include VAT (16%).
Plain business English, under 160 words. Reply with ONLY JSON: {"subject": "...", "body": "..."}"""


async def rfq_draft(r: RFQRequest) -> dict:
    fallback = {**template_draft(r), "source": "template"}
    if config.PRACTICE_AI:
        return fallback
    details = "\n".join(f"{k}: {v}" for k, v in [
        ("Item", r.item), ("Specification", r.specs), ("Quantity", r.quantity), ("Supplier", r.supplier),
        ("Delivery location", r.delivery_location), ("Required by", r.required_by),
        ("Product page", r.product_url), ("Sender name", r.requester)] if v)
    try:
        data = await chat_json(RFQ_SYSTEM, f"Details:\n{details}\n\nReturn the JSON object.", temperature=0.3)
    except Exception:
        return {**fallback, "warning": "AI unavailable; showing the standard template."}
    subject, body = str(data.get("subject", ""))[:200], str(data.get("body", ""))[:3000]
    if not subject or not body or "vat" not in body.lower() or r.item.lower()[:20] not in (subject + body).lower() \
            or not numbers_supported(subject + " " + body, details):
        return {**fallback, "warning": "The AI draft didn't pass the checks, so the standard template is shown."}
    return {"subject": subject, "body": body, "source": "ai"}


# ---- Understanding a free-text request ------------------------------------------------
REQUEST_SYSTEM = """Extract a product search from a procurement request. Use only words from the request.
Reply with ONLY JSON: {"keywords": "short product search, e.g. brand + product + model",
"brand": "", "quantity": null, "max_price": null, "location": ""}"""


async def parse_request(text: str) -> dict | None:
    """Turn a sentence ("laptop for an engineer, 16GB, under 120k") into search fields.
    Returns None in practice mode, on error, or if the AI used words not in the request."""
    if config.PRACTICE_AI:
        return None
    try:
        data = await chat_json(REQUEST_SYSTEM, text, temperature=0.1)
    except Exception:
        return None
    kw = str(data.get("keywords", "")).strip()
    lower = text.lower()
    if not kw or any(w not in lower for w in re.findall(r"[a-z0-9]+", kw.lower())):
        return None
    brand = str(data.get("brand") or "").strip()
    location = str(data.get("location") or "").strip()
    # Numbers must come from the request itself ("120k" counts as 120,000).
    stated = _numbers(text) | {n * 1000 for n in _numbers(" ".join(re.findall(r"\d[\d,.]*\s*k\b", lower)))}
    out = {"keywords": kw[:120],
           "brand": brand[:40] if brand.lower() in lower else "",
           "location": location[:60] if location.lower() in lower else ""}
    for key in ("quantity", "max_price"):
        v = data.get(key)
        out[key] = float(v) if isinstance(v, (int, float)) and v > 0 and float(v) in stated else None
    return out
