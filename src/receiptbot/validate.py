"""Money math and receipt checks. Code only: the model never does arithmetic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from receiptbot.models import ParsedReceipt

CENT = Decimal("0.01")
_MONEY_RE = re.compile(r"^\(?\s*([-+−]?)\s*[^\d\-+.,(]*\s*([-+−]?)\s*([\d,]*\.?\d+)\s*\)?$")


def parse_money(text: str) -> Decimal:
    """Parse a printed amount such as "$1,234.56", "-$5.00" or "($5.00)".

    Raises ValueError for anything that is not plainly one amount.
    """
    cleaned = text.strip()
    match = _MONEY_RE.match(cleaned)
    if not match:
        raise ValueError(f"not an amount: {text!r}")
    sign_a, sign_b, digits = match.groups()
    try:
        value = Decimal(digits.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    negative = bool(sign_a.strip("+")) or bool(sign_b.strip("+")) or cleaned.startswith("(")
    return -value if negative else value


def to_milliunits(amount: Decimal) -> int:
    """Convert a currency amount to YNAB integer milliunits (1000 = one unit)."""
    scaled = amount * 1000
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount} has more precision than a milliunit")
    return int(scaled)


def fmt_money(amount: Decimal, currency: str = "USD") -> str:
    symbol = {"USD": "$", "CAD": "$", "AUD": "$", "EUR": "€", "GBP": "£"}.get(currency)
    body = f"{abs(amount):,.2f}"
    text = f"{symbol}{body}" if symbol else f"{body} {currency}"
    return f"-{text}" if amount < 0 else text


@dataclass(frozen=True)
class TotalsCheck:
    ok: bool
    computed: Decimal  # items plus adjustments
    stated: Decimal  # grand total printed on the receipt


def check_totals(receipt: ParsedReceipt) -> TotalsCheck:
    computed = receipt.items_total + receipt.adjustments_total
    ok = bool(receipt.items) and abs(computed - receipt.grand_total) <= CENT
    return TotalsCheck(ok=ok, computed=computed, stated=receipt.grand_total)


def normalize_signs(receipt: ParsedReceipt) -> ParsedReceipt:
    """Make discounts and gift card payments negative when that makes the totals add up.

    Some receipts print "Discount $5.00" without a minus sign. The flip is applied
    only if it is exactly what reconciles the receipt; otherwise nothing changes.
    """
    if check_totals(receipt).ok:
        return receipt
    flipped = [
        adj.model_copy(update={"amount": -adj.amount})
        if adj.kind in ("discount", "payment") and adj.amount > 0
        else adj
        for adj in receipt.adjustments
    ]
    candidate = receipt.model_copy(update={"adjustments": flipped})
    return candidate if check_totals(candidate).ok else receipt


def allocate(line_totals: list[Decimal], total: Decimal) -> list[Decimal]:
    """Split `total` across lines in proportion to their line totals.

    This spreads tax, shipping, discounts and gift card payments over the items.
    The parts are whole cents and sum exactly to `total`; the rounding remainder
    goes on the largest line.
    """
    if not line_totals:
        raise ValueError("nothing to allocate to")
    base = sum(line_totals, Decimal("0"))
    if base == 0:
        if total != 0:
            raise ValueError("cannot spread a total over items that sum to zero")
        return [Decimal("0.00") for _ in line_totals]
    parts = [(total * line / base).quantize(CENT, rounding=ROUND_HALF_UP) for line in line_totals]
    remainder = total - sum(parts, Decimal("0"))
    largest = max(range(len(line_totals)), key=lambda i: (abs(line_totals[i]), -i))
    parts[largest] += remainder
    return parts


def truncate_words(text: str, limit: int) -> str:
    """Cut text to at most `limit` characters at a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;") + "…"
