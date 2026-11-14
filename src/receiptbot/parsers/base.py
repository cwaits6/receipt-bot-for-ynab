"""The small interface every merchant parser implements.

To add a merchant: write a module with a class that has `name`, `matches` and
`parse`, then add an instance to PARSERS in `parsers/__init__.py`. Build it
against synthetic fixtures under tests/fixtures/.
"""

from __future__ import annotations

from typing import Protocol

from receiptbot.models import ParsedReceipt


class Parser(Protocol):
    name: str

    def matches(self, text: str) -> bool:
        """Cheap check: does this text look like this merchant's layout?"""
        ...

    def parse(self, text: str) -> ParsedReceipt | None:
        """Parse with anchored patterns. Return None when the layout is not understood.

        Never guess: a result is only used if it then passes validation.
        """
        ...
