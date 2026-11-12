"""Supplier readers. Each Tier 1 supplier in suppliers.yaml names its adapter here."""

from __future__ import annotations

from adapters.base import Adapter, Blocked, Fetcher, Offer, SupplierError
from adapters.ctc_solutions import CTCSolutionsAdapter
from adapters.network_options import NetworkOptionsAdapter
from adapters.optace import OptaceAdapter
from config import Supplier

REGISTRY: dict[str, type[Adapter]] = {
    "network_options": NetworkOptionsAdapter,
    "ctc_solutions": CTCSolutionsAdapter,
    "optace": OptaceAdapter,
}


def adapter_for(supplier: Supplier) -> Adapter | None:
    cls = REGISTRY.get(supplier.adapter or "")
    return cls(supplier) if cls and supplier.active else None


__all__ = ["Adapter", "Blocked", "Fetcher", "Offer", "SupplierError", "REGISTRY", "adapter_for"]
