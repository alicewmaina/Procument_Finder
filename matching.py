"""Product name/model normalisation, relevance ranking and same-product matching.

Order of evidence (spec 13.8): model numbers, then normalised names, then
fuzzy similarity. Items with different model numbers are never merged.
AI match checks for borderline cases come in a later phase.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from prices import clean_text

FUZZY_THRESHOLD = 85

_LEN = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:m|mtr|mtrs|metre|metres|meter|meters)\b", re.I)
_CAT = re.compile(r"\bcat\.?[\s-]?(5e|6a|6|7a|7|8)\b", re.I)
STOPWORDS = {"a", "an", "and", "the", "for", "with", "of", "in", "to", "on", "by", "or", "x", "pcs", "piece", "pieces"}

# Tokens that contain letters and digits but are specs or units, not model numbers.
_NOT_MODEL = re.compile(
    r"^(?:\d+(?:\.\d+)?(?:m|mm|cm|mtr|mtrs|gb|tb|mb|kb|ghz|mhz|khz|w|v|a|ah|va|kva|u|g|dbi|db|mbps|gbps|"
    r"kg|g|in|inch|port|ports|p|x|k|mp|fps|ft|hz)"
    r"|\d+-?(?:port|ports|way|core|pair|u)|cat[.\s-]?\d+[a-z]?|rj\d+|wifi\d+|wi-fi\d+|[2-6]g|ipv\d|ip\d\d|"
    r"usb\d(?:\.\d)?|802\.\d+[a-z]*|ddr\d|pcie\d?|hdmi\d?|om\d|os\d|\d+x\d+|\d+x)$", re.I)
_MODEL_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-/.+][A-Za-z0-9]+)*\+*")


def normalise(name: str) -> str:
    """Lowercase, plain text, standard units ('3 Mtr' -> '3m', 'Cat 6A' -> 'cat6a'), no punctuation."""
    s = clean_text(name).lower()
    s = s.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")   # non-breaking hyphen, en and em dash
    s = _LEN.sub(lambda m: f"{float(m.group(1)):g}m", s)
    s = _CAT.sub(lambda m: f"cat{m.group(1).lower()}", s)
    s = re.sub(r"[^a-z0-9+.\s]", " ", s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(text: str) -> list[str]:
    return [t for t in normalise(text).split() if t not in STOPWORDS]


def lengths(name: str) -> set[str]:
    return {f"{float(m.group(1)):g}m" for m in _LEN.finditer(clean_text(name))}


def _norm_model(m: str) -> str:
    return re.sub(r"[-/.\s]", "", m).upper()


def extract_models(name: str, sku: str | None = None) -> set[str]:
    """Model numbers in a name (e.g. NS-5ACL, GXP1625, CRS326-24G-2S+), normalised without hyphens."""
    found = set()
    for raw in _MODEL_TOKEN.findall(clean_text(name)) + ([sku] if sku else []):
        tok = raw.strip("./-")
        if not tok or _NOT_MODEL.match(tok):
            continue
        letters, digits = re.search(r"[A-Za-z]", tok), re.search(r"\d", tok)
        if letters and digits and len(_norm_model(tok)) >= 4:
            found.add(_norm_model(tok))
    return found


def model_from_name(name: str) -> str | None:
    """Best single model-like token from a name, as written (for display)."""
    for raw in _MODEL_TOKEN.findall(clean_text(name)):
        tok = raw.strip("./-")
        if tok and not _NOT_MODEL.match(tok) and re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok) \
                and len(_norm_model(tok)) >= 4:
            return tok
    return None


def _squashed(name: str) -> str:
    """Name with spaces and punctuation removed, uppercase ("Loco M5" -> "LOCOM5")."""
    return re.sub(r"[^A-Z0-9+]", "", clean_text(name).upper())


def _model_written_apart(models: set[str], other_name: str) -> str | None:
    """A model number the other name contains with spaces ("locoM5" vs "Loco M5"). Long models only."""
    squashed = _squashed(other_name)
    return next((m for m in sorted(models) if len(m) >= 5 and m in squashed), None)


def compare(a_name: str, b_name: str, a_model: str | None = None, b_model: str | None = None) -> tuple[str, str]:
    """Return (verdict, reason): same | similar | different."""
    la, lb = lengths(a_name), lengths(b_name)
    if la and lb and not (la & lb):
        return "different", "Different lengths"
    ma, mb = extract_models(a_name, a_model), extract_models(b_name, b_model)
    if ma and mb:
        shared = ma & mb
        if shared:
            return "same", f"Same model number ({sorted(shared)[0]})"
        return "different", "Different model numbers"
    if ma or mb:
        m = _model_written_apart(ma, b_name) if ma else _model_written_apart(mb, a_name)
        if m:
            return "same", f"Model number {m} appears in both names"
    score = fuzz.token_set_ratio(normalise(a_name), normalise(b_name))
    if score >= FUZZY_THRESHOLD:
        return "similar", f"Names are {score:.0f}% alike; no model number to confirm. Check specs"
    return "different", "Names don't match"


def relevance(query: str, name: str, model: str | None = None) -> float:
    """Share of the query's words found in the product name or model (0..1)."""
    q = tokens(query)
    if not q:
        return 0.0
    name_norm = normalise(name)
    name_tokens = set(name_norm.split())
    joined = name_norm.replace(" ", "")
    model_norm = _norm_model(model or "").lower()

    def hit(t: str) -> bool:
        return t in name_tokens or (len(t) >= 2 and (t in joined or bool(model_norm and t in model_norm)))

    # Words with digits ("cat6a", "m5", "24") pin down the product: if the query has
    # any (lengths aside), at least one must match or it's a different product.
    key = [t for t in q if re.search(r"\d", t) and not re.fullmatch(r"\d+(?:\.\d+)?m", t)]
    if key and not any(hit(t) for t in key):
        return 0.0
    return sum(hit(t) for t in q) / len(q)


def rank(query: str, items: list, name=lambda o: o.name, model=lambda o: o.model, minimum: float = 0.6) -> list:
    """Keep items matching most of the query's words (60%), best first."""
    scored = []
    for it in items:
        r = relevance(query, name(it), model(it))
        if r >= minimum:
            scored.append((r, fuzz.token_set_ratio(normalise(query), normalise(name(it))), it))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [it for _, _, it in scored]


def simplify_query(query: str) -> str:
    """Drop lengths and quantities ('3m', '305m', 'x10') that stores often write differently."""
    q = _LEN.sub(" ", query)
    q = re.sub(r"\bx\s*\d+\b|\b\d+\s*(?:pcs|pieces|units)\b", " ", q, flags=re.I)
    return re.sub(r"\s+", " ", q).strip()


def group(offers: list[dict]) -> list[int | None]:
    """Group offers that are the same product (shared model number). Returns a group number per offer,
    or None when the offer matches nothing else."""
    n = len(offers)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if compare(offers[i]["name"], offers[j]["name"], offers[i].get("model"), offers[j].get("model"))[0] == "same":
                parent[find(i)] = find(j)
    roots: dict[int, list[int]] = {}
    for i in range(n):
        roots.setdefault(find(i), []).append(i)
    out: list[int | None] = [None] * n
    gid = 0
    for members in roots.values():
        if len(members) > 1:
            gid += 1
            for i in members:
                out[i] = gid
    return out
