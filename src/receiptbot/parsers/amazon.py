"""Amazon order invoice ("Final Details for Order #...") layout.

Works on the text layer of the printable invoice saved as a PDF, or on the same
page pasted as text. Items appear as "<qty> of: <title>" blocks followed by a
price, and the order summary is a run of "Label: $amount" lines ending in
"Grand Total:". Anything else is left to the fallback path.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from receiptbot.models import Adjustment, Item, ParsedReceipt
from receiptbot.validate import parse_money

_ORDER_NUMBER = re.compile(r"\b([0-9D]\d{2}-\d{7}-\d{7})\b")
_ORDER_DATE = re.compile(r"Order Placed:?\s+([A-Z][a-z]+ \d{1,2}, \d{4})", re.IGNORECASE)
_ITEM_START = re.compile(r"^(\d+) of: (.+)$")
_PRICE = re.compile(r"-?\$[\d,]+\.\d{2}")
_PRICE_AT_END = re.compile(r"^(.*?)\s*(-?\$[\d,]+\.\d{2})$")
_SUMMARY_LINE = re.compile(r"^([A-Za-z][A-Za-z &()/'.\-]*?):\s*(-?\$-?[\d,]+\.\d{2})$")
_LAST4 = re.compile(r"(?:Last digits:|ending in)\s*(\d{4})", re.IGNORECASE)
_BLOCK_END = re.compile(
    r"^(Shipping Address:|Shipping Speed:|Payment information|Shipped on|Items Ordered|"
    r"Item\(s\) Subtotal|Not Yet Shipped|Delivered|Billing address)",
    re.IGNORECASE,
)
_TITLE_END = re.compile(r"^(Sold by:|Supplied by:|Condition:|Business Price)", re.IGNORECASE)
_NOT_ADJUSTMENTS = ("item(s) subtotal", "total before tax", "grand total", "order total", "refund")


class AmazonParser:
    name = "amazon"

    def matches(self, text: str) -> bool:
        return bool(
            "amazon" in text.lower() and "Grand Total:" in text and _ORDER_NUMBER.search(text)
        )

    def parse(self, text: str) -> ParsedReceipt | None:
        lines = [" ".join(line.split()) for line in text.splitlines()]
        lines = [line for line in lines if line]

        summary = _summary(lines)
        grand_total = summary.get("grand total")
        blocks = _item_blocks(lines)
        if grand_total is None or not blocks:
            return None
        items = _items(blocks, summary.get("item(s) subtotal"))
        if items is None:
            return None

        adjustments = [
            Adjustment(label=label, amount=amount)
            for label, amount in _adjustments(lines)
            if amount != 0
        ]
        order_number = _ORDER_NUMBER.search(text)
        last4 = _LAST4.search(text)
        return ParsedReceipt(
            merchant="Amazon",
            order_number=order_number.group(1) if order_number else None,
            order_date=_order_date(text),
            currency="USD",
            grand_total=grand_total,
            payment_last4=last4.group(1) if last4 else None,
            items=items,
            adjustments=adjustments,
            source=f"parser:{self.name}",
        )


def _order_date(text: str) -> date | None:
    match = _ORDER_DATE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%B %d, %Y").date()
    except ValueError:
        return None


def _summary_lines(lines: list[str]) -> list[tuple[str, Decimal]]:
    found = []
    for line in lines:
        match = _SUMMARY_LINE.match(line)
        if match:
            found.append((match.group(1).strip(), parse_money(match.group(2))))
    return found


def _summary(lines: list[str]) -> dict[str, Decimal]:
    # The last occurrence wins: multi-shipment invoices print the summary at the end.
    return {label.lower(): amount for label, amount in _summary_lines(lines)}


def _adjustments(lines: list[str]) -> list[tuple[str, Decimal]]:
    seen: dict[str, Decimal] = {}
    for label, amount in _summary_lines(lines):
        if not label.lower().startswith(_NOT_ADJUSTMENTS):
            seen[label] = amount
    return list(seen.items())


def _item_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Group lines into (quantity, block lines) starting at each "<n> of:" line."""
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        start = _ITEM_START.match(line)
        if start:
            current = [start.group(2)]
            blocks.append((int(start.group(1)), current))
        elif current is not None:
            if _BLOCK_END.match(line) or _SUMMARY_LINE.match(line):
                current = None
            else:
                current.append(line)
    return blocks


def _split_block(block: list[str]) -> tuple[str, Decimal] | None:
    """Return (title, printed price) for one item block, or None if there is no price."""
    title_parts: list[str] = []
    price: Decimal | None = None
    in_title = True
    for line in block:
        if _TITLE_END.match(line):
            in_title = False
        ends_with_price = _PRICE_AT_END.match(line)
        if ends_with_price and price is None:
            price = parse_money(ends_with_price.group(2))
            line = ends_with_price.group(1)
            if in_title and line:
                title_parts.append(line)
            in_title = False
        elif in_title:
            title_parts.append(line)
    title = " ".join(title_parts).strip()
    if price is None or not title:
        return None
    return title, price


def _items(blocks: list[tuple[int, list[str]]], subtotal: Decimal | None) -> list[Item] | None:
    parsed = []
    for quantity, block in blocks:
        split = _split_block(block)
        if split is None or quantity < 1:
            return None
        parsed.append((quantity, *split))

    # The printed price is per unit. If that does not reproduce the printed
    # subtotal but reading it as a line total does, use the line total.
    per_unit = [Item(description=t, quantity=q, line_total=p * q) for q, t, p in parsed]
    if subtotal is None or sum(i.line_total for i in per_unit) == subtotal:
        return per_unit
    as_line = [Item(description=t, quantity=q, line_total=p) for q, t, p in parsed]
    if sum(i.line_total for i in as_line) == subtotal:
        return as_line
    return per_unit
