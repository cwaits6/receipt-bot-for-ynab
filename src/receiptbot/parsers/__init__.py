"""Registry of deterministic merchant parsers, tried in order."""

from __future__ import annotations

from dataclasses import dataclass

from receiptbot.models import ParsedReceipt
from receiptbot.parsers.amazon import AmazonParser
from receiptbot.parsers.base import Parser
from receiptbot.validate import check_totals, normalize_signs

PARSERS: list[Parser] = [AmazonParser()]


@dataclass(frozen=True)
class ParserOutcome:
    receipt: ParsedReceipt | None  # set only when a parser's result passed validation
    matched: str | None  # name of the first parser whose layout check matched


def run_parsers(text: str, parsers: list[Parser] | None = None) -> ParserOutcome:
    """Try each parser. A parser that matches but fails validation counts as a miss."""
    matched = None
    for parser in PARSERS if parsers is None else parsers:
        if not parser.matches(text):
            continue
        matched = matched or parser.name
        receipt = parser.parse(text)
        if receipt is None:
            continue
        receipt = normalize_signs(receipt)
        if check_totals(receipt).ok:
            return ParserOutcome(receipt=receipt, matched=parser.name)
    return ParserOutcome(receipt=None, matched=matched)
