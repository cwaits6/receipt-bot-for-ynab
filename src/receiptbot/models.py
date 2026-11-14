"""Shared data models. Every reading path produces a ParsedReceipt."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

AdjustmentKind = Literal["tax", "shipping", "discount", "payment", "other"]

# Word fragments that identify what an adjustment line is. "payment" means part of
# the order was paid with something other than the card (gift card, points).
_KIND_WORDS: tuple[tuple[AdjustmentKind, tuple[str, ...]], ...] = (
    ("payment", ("gift card", "giftcard", "points", "rewards", "store credit", "balance")),
    ("discount", ("discount", "promotion", "promo", "coupon", "savings", "subscribe")),
    ("tax", ("tax", "vat", "gst")),
    ("shipping", ("shipping", "handling", "delivery", "postage")),
)


def adjustment_kind(label: str) -> AdjustmentKind:
    lowered = label.lower()
    for kind, words in _KIND_WORDS:
        if any(word in lowered for word in words):
            return kind
    return "other"


class Item(BaseModel):
    description: str
    quantity: int = 1
    line_total: Decimal


class Adjustment(BaseModel):
    label: str
    amount: Decimal

    @property
    def kind(self) -> AdjustmentKind:
        return adjustment_kind(self.label)


class ParsedReceipt(BaseModel):
    merchant: str
    order_number: str | None = None
    order_date: date | None = None
    currency: str = "USD"
    grand_total: Decimal
    payment_last4: str | None = None
    items: list[Item] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    source: str  # "parser:<name>" or "llm"

    @property
    def items_total(self) -> Decimal:
        return sum((item.line_total for item in self.items), Decimal("0"))

    @property
    def adjustments_total(self) -> Decimal:
        return sum((adj.amount for adj in self.adjustments), Decimal("0"))

    @property
    def paid_elsewhere(self) -> Decimal:
        """Amount covered by gift card balance or points (a positive number)."""
        return -sum((a.amount for a in self.adjustments if a.kind == "payment"), Decimal("0"))


@dataclass(frozen=True)
class Document:
    """One thing a person shared: a file's bytes or pasted text."""

    kind: Literal["pdf", "image", "text"]
    data: bytes = b""
    text: str = ""
    media_type: str = ""


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    group: str = ""


@dataclass
class CategorizedItem:
    description: str  # shortened, for memos and replies
    key: str  # normalized rule key
    line_total: Decimal
    category_id: str | None = None
    category_source: Literal["rule", "llm", "human"] | None = None
    guesses: list[str] = field(default_factory=list)  # category ids offered when asking
