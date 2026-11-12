"""Test setup: temporary database, practice-mode AI, saved supplier pages, demo user."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="procurefind-test-")
os.environ.update({
    "PROCUREFIND_DB": os.path.join(_tmp, "test.db"),
    "LLM_BASE_URL": "mock",
    "SUPPLIER_SOURCE": "fixtures",
    "DEMO_USER": "Tester",
    "SECRET_KEY": "test-secret",
    "CACHE_MINUTES": "60",
    "FETCH_TIMEOUT": "5",
})

import pytest  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
from adapters import base, optace  # noqa: E402
import ratings  # noqa: E402
import discover  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Fresh caches for every test; practice mode with saved pages unless a test changes it."""
    monkeypatch.setattr(config, "LLM_BASE_URL", "mock")
    monkeypatch.setattr(config, "PRACTICE_AI", True)
    monkeypatch.setattr(config, "SUPPLIER_SOURCE", "fixtures")
    db.init()
    db.clear_cache()
    for table in ("directory", "directory_runs", "places", "debarred", "price_history", "fetch_log", "searches", "comparisons"):
        db.execute(f"DELETE FROM {table}")
    discover._tasks.clear()
    ratings.clear_cache()
    optace.clear_cache()
    base.reset_politeness_state()
    llm.reset_health_cache()
    yield
