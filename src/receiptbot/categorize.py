"""Category assignment: learned rules first, then seed rules, then (optionally)
the model for unknown items, then a question in the group.

Also holds the plain-code text helpers for item descriptions and rule keys.
"""

from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from receiptbot.ledger import Ledger
from receiptbot.models import CategorizedItem, Category, Item

log = logging.getLogger(__name__)

OVERLAP_THRESHOLD = 0.75  # conservative: most words must agree
GUESS_THRESHOLD = 0.4
KEEP_WORDS = 4

_UNITS = (
    r"oz|ounces?|fl|lbs?|pounds?|kg|g|grams?|ml|l|liters?|gal|gallons?|qt|quarts?|"
    r"ct|count|pk|packs?|pcs?|pieces?|rolls?|sheets?|bags?|box|boxes|"
    r"in|inch|inches|ft|feet|cm|mm|mah|w|watts?|v|volts?"
)
_SIZE = re.compile(rf"\b\d+(?:[.,]\d+)?\s*(?:x\s*\d+\s*)?(?:{_UNITS})\b\.?", re.IGNORECASE)
_PACK_OF = re.compile(r"\b(?:pack|set|box|case|bundle)\s+of\s+\d+\b", re.IGNORECASE)
_PARENS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_CLAUSE_END = re.compile(r",|;|\s[-–—|:]\s|\s(?:with|for|by|w/)\s", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "the",
        "of",
        "in",
        "on",
        "to",
        "for",
        "with",
        "by",
        "or",
        "new",
        "premium",
        "original",
        "classic",
        "best",
        "value",
        "size",
        "pack",
        "count",
        "each",
        "per",
        "assorted",
        "variety",
    ]
)
_BOILERPLATE = re.compile(
    r"\b(?:amazon(?:'s)? (?:basics|brand|essentials|choice)|by amazon|brand new|"
    r"packaging may vary|\d+\s*%\s*\w+)\b",
    re.IGNORECASE,
)


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _leading_clause(title: str) -> str:
    """The part of a listing title that names the product: size noise and brand
    boilerplate removed, cut at the first comma, dash or "with/for"."""
    text = _PARENS.sub(" ", title)
    text = _BOILERPLATE.sub(" ", text)
    text = _PACK_OF.sub(" ", text)
    text = _SIZE.sub(" ", text)
    text = " ".join(text.split()).strip(" ,;-")
    parts = [part for part in _CLAUSE_END.split(text) if _WORD.search(part.lower())]
    return parts[0].strip() if parts else text


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if not w.isdigit()]


def shorten(title: str) -> str:
    """A short human label for an item, e.g. for memos and replies.

    Listing titles put the brand first and the product noun last within the
    leading clause, so the last few words of that clause are kept.
    """
    words = _words(_leading_clause(title))
    words = words[-KEEP_WORDS:] if len(words) > KEEP_WORDS else words
    return " ".join(words) or " ".join(_words(title)[:KEEP_WORDS]) or title.strip()[:40]


def normalize_key(title: str) -> str:
    """The rule key for an item: lowercase significant words, no sizes or counts."""
    words = [_stem(w) for w in _words(_leading_clause(title)) if w not in _STOPWORDS]
    words = words[-KEEP_WORDS:] if len(words) > KEEP_WORDS else words
    return " ".join(words) or shorten(title)


def token_set(text: str) -> frozenset[str]:
    return frozenset(_stem(w) for w in _words(text) if w not in _STOPWORDS)


def overlap(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _plain(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def match_category(answer: str, categories: list[Category]) -> Category | None:
    """Match a person's typed answer to a category name. Deterministic: exact
    name, then a unique prefix, then one clear close match. Ambiguity is None."""
    wanted = _plain(answer)
    if not wanted:
        return None
    by_name: dict[str, Category] = {}
    for category in categories:
        by_name.setdefault(_plain(category.name), category)
    if wanted in by_name:
        return by_name[wanted]
    prefixed = [c for name, c in by_name.items() if name.startswith(wanted)]
    if len(prefixed) == 1:
        return prefixed[0]
    containing = [c for name, c in by_name.items() if wanted in name.split()]
    if len(containing) == 1:
        return containing[0]
    close = difflib.get_close_matches(wanted, list(by_name), n=2, cutoff=0.75)
    if len(close) == 1:
        return by_name[close[0]]
    return None


@dataclass(frozen=True)
class SeedRule:
    keyword: frozenset[str]
    category_id: str


def load_seed_rules(path: Path, categories: list[Category]) -> list[SeedRule]:
    """Read rules.yaml: `keyword: Category Name` entries written by the owner."""
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping of keyword to category name")
    rules = []
    for keyword, name in raw.items():
        category = match_category(str(name), categories)
        tokens = token_set(str(keyword))
        if category is None or not tokens:
            log.warning("Ignoring seed rule %r: no category named %r", keyword, name)
            continue
        rules.append(SeedRule(keyword=tokens, category_id=category.id))
    return rules


@dataclass(frozen=True)
class CategoryGuess:
    category: str | None
    confidence: float
    alternative: str | None = None


class ItemCategorizer(Protocol):
    def categorize_items(
        self, descriptions: list[str], category_names: list[str]
    ) -> list[CategoryGuess]: ...


class Categorizer:
    def __init__(
        self,
        ledger: Ledger,
        categories: Callable[[], list[Category]],
        *,
        seed_rules: list[SeedRule] | None = None,
        llm: ItemCategorizer | None = None,
        min_confidence: float = 0.8,
    ):
        self.ledger = ledger
        self._categories = categories
        self.seed_rules = seed_rules or []
        self.llm = llm
        self.min_confidence = min_confidence

    @property
    def categories(self) -> list[Category]:
        return self._categories()

    def name_of(self, category_id: str | None) -> str:
        for category in self.categories:
            if category.id == category_id:
                return category.name
        return "Uncategorized"

    def assign(self, items: list[Item]) -> list[CategorizedItem]:
        """Give each item a category where one can be determined. Items left
        with `category_id=None` need a person; `guesses` holds up to two ideas."""
        valid = {c.id for c in self.categories}
        out = []
        unknown: list[tuple[CategorizedItem, str]] = []
        for item in items:
            result = CategorizedItem(
                description=shorten(item.description),
                key=normalize_key(item.description),
                line_total=item.line_total,
            )
            category_id, guesses = self._from_rules(item.description, result.key, valid)
            if category_id:
                result.category_id = category_id
                result.category_source = "rule"
            else:
                result.guesses = guesses
                unknown.append((result, item.description))
            out.append(result)
        if unknown and self.llm is not None:
            self._from_model(unknown)
        return out

    def from_rules(self, text: str) -> str | None:
        """Category for a free-text description using rules only (no model)."""
        valid = {c.id for c in self.categories}
        return self._from_rules(text, normalize_key(text), valid)[0]

    def _from_rules(self, title: str, key: str, valid: set[str]) -> tuple[str | None, list[str]]:
        exact = self.ledger.get_rule(key)
        if exact and exact.category_id in valid:
            self.ledger.bump_rule(key)
            return exact.category_id, []

        key_tokens = token_set(key)
        scored = sorted(
            (
                (overlap(key_tokens, token_set(rule.normalized_key)), rule)
                for rule in self.ledger.rules()
                if rule.category_id in valid
            ),
            key=lambda pair: (-pair[0], pair[1].normalized_key),
        )
        if scored and scored[0][0] >= OVERLAP_THRESHOLD:
            best = [rule for score, rule in scored if score == scored[0][0]]
            if len({rule.category_id for rule in best}) == 1:
                self.ledger.bump_rule(best[0].normalized_key)
                return best[0].category_id, []

        title_tokens = token_set(title)
        seeded = [seed for seed in self.seed_rules if seed.keyword <= title_tokens]
        if seeded:
            longest = max(len(seed.keyword) for seed in seeded)
            picks = {seed.category_id for seed in seeded if len(seed.keyword) == longest}
            if len(picks) == 1 and picks <= valid:
                return picks.pop(), []

        guesses = list(
            dict.fromkeys(rule.category_id for score, rule in scored if score >= GUESS_THRESHOLD)
        )
        return None, guesses[:2]

    def _from_model(self, unknown: list[tuple[CategorizedItem, str]]) -> None:
        names = [c.name for c in self.categories]
        by_name = {c.name: c.id for c in reversed(self.categories)}
        try:
            guesses = self.llm.categorize_items([title for _, title in unknown], names)
        except Exception:
            log.exception("Model categorization failed; asking a person instead")
            return
        if len(guesses) != len(unknown):
            log.warning("Model returned %d guesses for %d items", len(guesses), len(unknown))
            return
        for (item, _), guess in zip(unknown, guesses, strict=True):
            # Only names that exist in the plan are accepted.
            offered = [by_name[n] for n in (guess.category, guess.alternative) if n in by_name]
            if guess.category in by_name and guess.confidence >= self.min_confidence:
                item.category_id = by_name[guess.category]
                item.category_source = "llm"
            else:
                item.guesses = list(dict.fromkeys(offered + item.guesses))[:2]

    def learn(self, key: str, category_id: str, origin: str) -> None:
        """Save a category a person confirmed. Model guesses are never saved here."""
        if key:
            self.ledger.upsert_rule(key, category_id, origin)
