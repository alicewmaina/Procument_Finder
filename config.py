"""Settings from the environment (.env) and the Tier 1 supplier list (suppliers.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BASE = Path(__file__).parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments. Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE / ".env")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "mock").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
LLM_TIMEOUT = _float("LLM_TIMEOUT", 300)
PRACTICE_AI = LLM_BASE_URL == "mock"

# Where supplier pages come from: "live" (the real websites) or "fixtures"
# (saved pages in tests/fixtures, no internet needed). Defaults to fixtures
# in practice mode.
SUPPLIER_SOURCE = os.environ.get("SUPPLIER_SOURCE", "fixtures" if PRACTICE_AI else "live").lower()
if SUPPLIER_SOURCE not in ("live", "fixtures"):
    SUPPLIER_SOURCE = "live"

VAT_RATE = _float("VAT_RATE", 0.16)
CACHE_MINUTES = _float("CACHE_MINUTES", 60)
FETCH_TIMEOUT = _float("FETCH_TIMEOUT", 15)
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DB_PATH = os.environ.get("PROCUREFIND_DB", str(BASE / "procurefind.db"))
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", BASE / "tests" / "fixtures"))
MAX_COMPARE = 5

# Tier 2/3 discovery (OpenStreetMap) and supplier checks
DEFAULT_LOCATION = os.environ.get("DEFAULT_LOCATION", "Nairobi").strip() or "Nairobi"
DISCOVERY_DAYS = _float("DISCOVERY_DAYS", 7)          # refresh the supplier directory this often
SITE_CHECK_DAYS = _float("SITE_CHECK_DAYS", 7)        # re-check a discovered supplier's website this often
MAX_NEW_SITE_CHECKS = int(_float("MAX_NEW_SITE_CHECKS", 4))   # unchecked websites to check per search
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OVERPASS_URLS = [u.strip() for u in os.environ.get(
    "OVERPASS_URLS", "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter"
).split(",") if u.strip()]
OVERPASS_TIMEOUT = _float("OVERPASS_TIMEOUT", 240)
PPRA_DEBARRED_URL = os.environ.get("PPRA_DEBARRED_URL", "https://ppra.go.ke/debarred-firms/")

USER_AGENT = (f"ProcureFind/0.1 (Angani; {CONTACT_EMAIL})" if CONTACT_EMAIL
              else "ProcureFind/0.1 (Angani; +https://angani.co)")


# Demo build: no sign-in page. Searches, choices and RFQs are recorded under this name.
DEMO_USER = os.environ.get("DEMO_USER", "").strip() or "Demo"


@dataclass(frozen=True)
class Supplier:
    key: str
    name: str
    tier: int
    website: str
    vat_basis: str                 # excl | incl | unknown
    adapter: str | None            # module in adapters/, None when rfq_only
    status: str                    # active | rfq_only | blocked
    search_url: str | None = None
    location: str = ""
    categories: tuple[str, ...] = ()
    vat_evidence: str = ""
    notes: str = ""
    checked: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active" and bool(self.adapter)


VALID_VAT = {"excl", "incl", "unknown"}
VALID_STATUS = {"active", "rfq_only", "blocked"}


def load_suppliers(path: Path | None = None) -> list[Supplier]:
    data = yaml.safe_load((path or BASE / "suppliers.yaml").read_text()) or []
    out = []
    for row in data:
        vat = str(row.get("vat_basis", "unknown")).lower()
        status = str(row.get("status", "active")).lower()
        if vat not in VALID_VAT:
            raise ValueError(f"{row.get('name')}: vat_basis must be one of {sorted(VALID_VAT)}")
        if status not in VALID_STATUS:
            raise ValueError(f"{row.get('name')}: status must be one of {sorted(VALID_STATUS)}")
        out.append(Supplier(
            key=row["key"], name=row["name"], tier=int(row.get("tier", 1)), website=row["website"],
            vat_basis=vat, adapter=row.get("adapter"), status=status, search_url=row.get("search_url"),
            location=row.get("location", ""), categories=tuple(row.get("categories") or ()),
            vat_evidence=(row.get("vat_evidence") or "").strip(), notes=(row.get("notes") or "").strip(),
            checked=str(row.get("checked", "")),
        ))
    return out


SUPPLIERS: list[Supplier] = load_suppliers()
