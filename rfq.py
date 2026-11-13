"""Request-for-quotation drafts (template). llm.py may reword it; this is the fallback."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RFQRequest:
    item: str
    specs: str = ""
    quantity: int = 1
    supplier: str = ""
    delivery_location: str = ""
    required_by: str = ""
    product_url: str = ""
    requester: str = ""


def template_draft(r: RFQRequest) -> dict:
    qty = max(int(r.quantity or 1), 1)
    lines = [
        f"Dear {r.supplier} team," if r.supplier else "Dear Sir/Madam,",
        "",
        "Angani would like a quotation for the following:",
        "",
        f"- Item: {r.item}",
    ]
    if r.specs:
        lines.append(f"- Specification: {r.specs}")
    lines.append(f"- Quantity: {qty}")
    if r.product_url:
        lines.append(f"- Product page: {r.product_url}")
    if r.delivery_location:
        lines.append(f"- Delivery to: {r.delivery_location}")
    if r.required_by:
        lines.append(f"- Required by: {r.required_by}")
    lines += [
        "",
        "Please include:",
        "- unit price and total price",
        "- whether the prices include VAT (16%)",
        "- availability / lead time and delivery cost",
        "- warranty and how long the quotation is valid",
        "",
        "Thank you.",
        "",
        "Kind regards,",
        r.requester or "[Your name]",
        "Angani",
    ]
    subject = f"Request for quotation: {r.item}" + (f" (qty {qty})" if qty > 1 else "")
    return {"subject": subject, "body": "\n".join(lines)}
