"""Reconciliation: line the ledger up with what the bank actually imported.

Everything that decides something is a pure function over plain data
(`plan_actions` and the helpers above it). `Reconciler` only fetches, applies
and reports. Nothing here calls the model.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from receiptbot.categorize import Categorizer, match_category, normalize_key, shorten
from receiptbot.ledger import Ledger, Question
from receiptbot.models import Category
from receiptbot.settings import Settings
from receiptbot.validate import allocate, fmt_money, parse_money, to_milliunits, truncate_words
from receiptbot.ynab import MEMO_MAX, Txn, YnabClient, YnabError

log = logging.getLogger(__name__)

MERCHANT_ALIASES: dict[str, tuple[str, ...]] = {"amazon": ("amazon", "amzn")}
MAX_SUBSET_CANDIDATES = 12
MAX_SHIPMENTS = 5
MAX_ITEMS_FOR_SHIPMENT_SPLIT = 8
MAX_QUESTIONS_PER_RUN = 3
CENT = 10  # milliunits
CACHE_DAYS = 120
_YES = {"yes", "y", "ok", "okay", "sure", "yep", "yup", "please"}


# -- plain data ------------------------------------------------------------


@dataclass(frozen=True)
class OrderLine:
    description: str
    key: str
    category_id: str
    amount: int  # positive milliunits, allocated share of the order total
    sub_id: str | None = None


@dataclass(frozen=True)
class OpenOrder:
    id: int
    merchant: str
    order_date: date
    total: int  # positive milliunits
    transaction_id: str
    order_number: str | None
    lines: tuple[OrderLine, ...]


@dataclass(frozen=True)
class RecurringItem:
    description: str
    price: Decimal
    category_id: str
    merchant: str | None = None


@dataclass(frozen=True)
class Explanation:
    order: OpenOrder
    imports: tuple[Txn, ...]


@dataclass(frozen=True)
class Action:
    """One thing reconciliation wants to do. `kind` is one of: mark_matched,
    mark_reverted, learn, apply_imports, annotate, ask."""

    kind: str
    summary: str
    order_id: int | None = None
    transaction_id: str | None = None
    updates: tuple[tuple[str, dict[str, Any]], ...] = ()
    delete_transaction_id: str | None = None
    rules: tuple[tuple[str, str], ...] = ()
    message: str | None = None


# -- pure helpers ----------------------------------------------------------


def _letters(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def payee_matches(txn: Txn, merchant: str) -> bool:
    """Does the bank's payee text look like this merchant?"""
    wanted = _letters(merchant)
    if not wanted:
        return False
    aliases = MERCHANT_ALIASES.get(wanted, (wanted,))
    names = (txn.payee_name, txn.import_payee_name, txn.import_payee_name_original)
    haystack = " ".join(_letters(name) for name in names if name)
    return any(alias in haystack for alias in aliases)


def may_modify(txn: Txn, own_ids: set[str] | frozenset[str]) -> bool:
    """Rule 7: never touch a reconciled transaction, and never touch one the bot
    did not create unless it is an unapproved import with an empty memo."""
    if txn.deleted or txn.cleared == "reconciled":
        return False
    if txn.id in own_ids:
        return True
    return txn.is_import and not txn.approved and not (txn.memo or "").strip()


def is_open_import(txn: Txn, own_ids: set[str] | frozenset[str]) -> bool:
    return (
        txn.is_import
        and not txn.is_matched
        and txn.amount < 0
        and txn.id not in own_ids
        and may_modify(txn, own_ids)
    )


def _in_window(txn: Txn, order: OpenOrder, window_days: int) -> bool:
    start = order.order_date - timedelta(days=1)
    return start <= txn.date <= order.order_date + timedelta(days=window_days)


def explain_orders(
    orders: list[OpenOrder], imports: list[Txn], window_days: int
) -> list[Explanation]:
    """Explain open imports with open orders.

    An order is explained by exactly one import of its total, or by exactly one
    set of imports inside the date window that sums to its total. Whenever more
    than one reading is possible the order is left alone for a person.
    """
    used: set[str] = set()
    found: list[Explanation] = []
    ordered = sorted(orders, key=lambda o: (o.order_date, o.id))

    def candidates(order: OpenOrder) -> list[Txn]:
        return [
            t
            for t in imports
            if t.id not in used
            and payee_matches(t, order.merchant)
            and _in_window(t, order, window_days)
        ]

    # Pass 1: one import equal to the order total, unambiguous in both directions.
    exact: dict[int, list[Txn]] = {
        order.id: [t for t in candidates(order) if -t.amount == order.total] for order in ordered
    }
    claims: dict[str, int] = {}
    for matches in exact.values():
        for txn in matches:
            claims[txn.id] = claims.get(txn.id, 0) + 1
    unexplained = []
    for order in ordered:
        matches = exact[order.id]
        if len(matches) == 1 and claims[matches[0].id] == 1:
            used.add(matches[0].id)
            found.append(Explanation(order, (matches[0],)))
        elif not matches:
            unexplained.append(order)
        # several exact matches, or a contested one: ambiguous, leave it

    # Pass 2: a unique set of imports that sums to the order total.
    for order in unexplained:
        pool = [t for t in candidates(order) if -t.amount < order.total]
        if not 2 <= len(pool) <= MAX_SUBSET_CANDIDATES:
            continue
        pool.sort(key=lambda t: (t.date, t.id))
        subsets = [
            combo
            for size in range(2, min(MAX_SHIPMENTS, len(pool)) + 1)
            for combo in itertools.combinations(pool, size)
            if -sum(t.amount for t in combo) == order.total
        ]
        if len(subsets) == 1:
            used.update(t.id for t in subsets[0])
            found.append(Explanation(order, subsets[0]))
    return found


def allocate_to_shipments(
    lines: tuple[OrderLine, ...], shipments: list[int]
) -> list[list[int]] | None:
    """Assign each order line to one shipment (by positive milliunit amount).

    Returns line indexes per shipment only when exactly one assignment makes
    every shipment's lines add up to its charge, within a cent per line (tax is
    rounded per shipment). Otherwise None: the caller uses the category mix.
    """
    if len(lines) > MAX_ITEMS_FOR_SHIPMENT_SPLIT or len(shipments) > MAX_SHIPMENTS:
        return None
    if len(lines) < len(shipments):
        return None
    signatures: dict[tuple, list[list[int]]] = {}
    for assignment in itertools.product(range(len(shipments)), repeat=len(lines)):
        groups: list[list[int]] = [[] for _ in shipments]
        for line_index, shipment_index in enumerate(assignment):
            groups[shipment_index].append(line_index)
        if any(not group for group in groups):
            continue
        if any(
            abs(sum(lines[i].amount for i in group) - shipments[n]) > CENT * len(group)
            for n, group in enumerate(groups)
        ):
            continue
        # Swapping identical lines between shipments is the same answer.
        signature = tuple(
            tuple(sorted((lines[i].category_id, lines[i].amount, lines[i].description) for i in g))
            for g in groups
        )
        signatures.setdefault(signature, groups)
        if len(signatures) > 1:
            return None
    return next(iter(signatures.values())) if signatures else None


def _memo(descriptions: list[str], order_number: str | None) -> str:
    suffix = f" [{order_number}]" if order_number else ""
    return truncate_words(", ".join(descriptions), MEMO_MAX - len(suffix)) + suffix


def _split_changes(amount: int, lines: list[OrderLine], order_number: str | None) -> dict[str, Any]:
    """Category and memo changes for one import (negative milliunits) that
    covers `lines`. Several categories become subtransactions that sum exactly."""
    by_category: dict[str, list[OrderLine]] = {}
    for line in lines:
        by_category.setdefault(line.category_id, []).append(line)
    memo = _memo([line.description for line in lines], order_number)
    if len(by_category) == 1:
        return {"category_id": next(iter(by_category)), "memo": memo}
    weights = [Decimal(sum(line.amount for line in group)) for group in by_category.values()]
    shares = allocate(weights, Decimal(-amount) / 1000)
    return {
        "memo": memo,
        "subtransactions": [
            {
                "amount": -to_milliunits(share),
                "category_id": category_id,
                "memo": _memo([line.description for line in group], order_number),
            }
            for (category_id, group), share in zip(by_category.items(), shares, strict=True)
        ],
    }


def import_updates(explanation: Explanation) -> list[tuple[str, dict[str, Any]]]:
    """What to write onto each import that explains an order."""
    order, imports = explanation.order, list(explanation.imports)
    lines = list(order.lines)
    if len(imports) == 1:
        return [(imports[0].id, _split_changes(imports[0].amount, lines, order.order_number))]
    groups = allocate_to_shipments(order.lines, [-t.amount for t in imports])
    if groups is None:
        # Items cannot be pinned to shipments: every charge gets the order's
        # category mix and the full memo.
        return [(t.id, _split_changes(t.amount, lines, order.order_number)) for t in imports]
    return [
        (t.id, _split_changes(t.amount, [lines[i] for i in group], order.order_number))
        for t, group in zip(imports, groups, strict=True)
    ]


def match_recurring(
    txn: Txn, recurring: list[RecurringItem], tolerance: float
) -> RecurringItem | None:
    amount = Decimal(-txn.amount) / 1000
    hits = [
        item
        for item in recurring
        if (item.merchant is None or payee_matches(txn, item.merchant))
        and abs(amount - item.price) <= item.price * Decimal(str(tolerance))
    ]
    return hits[0] if len(hits) == 1 else None


def _describe(txn: Txn) -> str:
    payee = txn.payee_name or txn.import_payee_name or "A charge"
    return f"{payee} {fmt_money(Decimal(-txn.amount) / 1000)} on {txn.date:%b %-d}"


@dataclass
class PlanInput:
    today: date
    transactions: list[Txn]  # cached view of the account(s), deleted ones included
    logged_orders: list[OpenOrder]  # ledger state "logged", posted for real
    approved_candidates: list[OpenOrder]  # logged or matched, not yet learned from
    merchants: list[str]
    recurring: list[RecurringItem]
    asked: set[str]
    not_before: date  # imports older than the bot's first run are left alone
    window_days: int = 21
    grace_days: int = 2
    recurring_tolerance: float = 0.05
    category_names: dict[str, str] = field(default_factory=dict)


def plan_actions(data: PlanInput) -> list[Action]:
    """Decide everything reconciliation should do. Pure: no I/O, no clock."""
    actions: list[Action] = []
    by_id = {t.id: t for t in data.transactions}
    own_ids = frozenset(o.transaction_id for o in [*data.logged_orders, *data.approved_candidates])

    # Steps 2 and 3: placeholders that YNAB matched, and what approval teaches.
    still_open: list[OpenOrder] = []
    for order in data.logged_orders:
        txn = by_id.get(order.transaction_id)
        if txn is None:
            continue  # not seen yet; the next delta will include it
        if txn.deleted:
            actions.append(
                Action("mark_reverted", f"order {order.id}: deleted in YNAB", order_id=order.id)
            )
        elif txn.is_matched or txn.is_import:
            actions.append(
                Action("mark_matched", f"order {order.id}: matched to import", order_id=order.id)
            )
        else:
            still_open.append(order)

    for order in data.approved_candidates:
        txn = by_id.get(order.transaction_id)
        if txn is None or txn.deleted or not txn.approved:
            continue
        subs = {s.id: s.category_id for s in txn.subtransactions if not s.deleted}
        rules = []
        for line in order.lines:
            final = subs.get(line.sub_id or "") if subs else txn.category_id
            if final and line.key:
                rules.append((line.key, final))
        actions.append(
            Action(
                "learn",
                f"order {order.id}: learn {len(rules)} rule(s) from approved transaction",
                order_id=order.id,
                rules=tuple(dict(rules).items()),
            )
        )

    # Steps 4 and 5: imports that explain an order YNAB did not match itself.
    open_imports = [
        t
        for t in data.transactions
        if is_open_import(t, own_ids) and any(payee_matches(t, m) for m in data.merchants)
    ]
    explained: set[str] = set()
    for explanation in explain_orders(still_open, open_imports, data.window_days):
        order = explanation.order
        placeholder = by_id[order.transaction_id]
        if not may_modify(placeholder, own_ids):
            continue
        explained.update(t.id for t in explanation.imports)
        charges = ", ".join(_describe(t) for t in explanation.imports)
        label = order.order_number or order.merchant
        actions.append(
            Action(
                "apply_imports",
                f"order {order.id} ({label}) explained by: {charges}",
                order_id=order.id,
                updates=tuple(import_updates(explanation)),
                delete_transaction_id=order.transaction_id,
            )
        )

    # Step 6: imports with no receipt, once the grace period has passed.
    explained_orders = {a.order_id for a in actions if a.kind == "apply_imports"}
    waiting = [o for o in still_open if o.id not in explained_orders]
    questions = 0
    for txn in sorted(open_imports, key=lambda t: (t.date, t.id)):
        if txn.id in explained or txn.id in data.asked:
            continue
        if txn.date < data.not_before or (data.today - txn.date).days < data.grace_days:
            continue
        if any(
            payee_matches(txn, o.merchant)
            and _in_window(txn, o, data.window_days)
            and -txn.amount <= o.total
            for o in waiting
        ):
            continue  # may still be a shipment of an open order
        item = match_recurring(txn, data.recurring, data.recurring_tolerance)
        if item is not None:
            name = data.category_names.get(item.category_id, "its category")
            actions.append(
                Action(
                    "annotate",
                    f"{_describe(txn)}: recurring item {item.description!r}",
                    transaction_id=txn.id,
                    updates=(
                        (txn.id, {"memo": item.description, "category_id": item.category_id}),
                    ),
                    message=f"{_describe(txn)} looks like {item.description} ({name}). "
                    "I filled it in and left it unapproved.",
                )
            )
        elif questions < MAX_QUESTIONS_PER_RUN:
            questions += 1
            actions.append(
                Action(
                    "ask",
                    f"{_describe(txn)}: no receipt, ask",
                    transaction_id=txn.id,
                    message=f"{_describe(txn)} has no receipt. What was it? Reply to this message.",
                )
            )
    return actions


# -- recurring items -------------------------------------------------------


def load_recurring(path: Path, categories: list[Category]) -> list[RecurringItem]:
    """Read recurring.yaml: a list of {description, price, category, merchant?}."""
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text()) or []
    if not isinstance(raw, list):
        raise ValueError(f"{path} must be a list of recurring items")
    items = []
    for entry in raw:
        category = match_category(str(entry.get("category", "")), categories)
        try:
            price = parse_money(str(entry.get("price", "")))
        except ValueError:
            price = None
        if category is None or price is None or price <= 0 or not entry.get("description"):
            log.warning("Ignoring recurring entry %r", entry.get("description"))
            continue
        items.append(
            RecurringItem(
                description=str(entry["description"]),
                price=price,
                category_id=category.id,
                merchant=str(entry["merchant"]) if entry.get("merchant") else None,
            )
        )
    return items


# -- the part that touches the world ----------------------------------------


@dataclass
class Reply:
    text: str
    question: dict[str, Any] | None = None


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        ynab: YnabClient,
        categorizer: Categorizer,
        recurring: list[RecurringItem] | None = None,
    ):
        self.settings = settings
        self.ledger = ledger
        self.ynab = ynab
        self.categorizer = categorizer
        self._recurring = recurring or []

    # -- gathering ---------------------------------------------------------

    def recurring(self) -> list[RecurringItem]:
        learned = [
            RecurringItem(
                description=r["description"],
                price=Decimal(r["price"]),
                category_id=r["category_id"],
                merchant=r.get("merchant"),
            )
            for r in self.ledger.get("recurring_learned", [])
        ]
        return [*self._recurring, *learned]

    def sync(self, today: date) -> list[Txn]:
        """Delta-fetch each configured account into the local cache and return it."""
        txns: list[Txn] = []
        for account_id in self.settings.ynab.account_ids:
            key = f"knowledge:{account_id}"
            knowledge = self.ledger.get(key)
            since = None if knowledge is not None else today - timedelta(days=CACHE_DAYS)
            changed, new_knowledge = self.ynab.account_transactions(
                account_id, last_knowledge=knowledge, since_date=since
            )
            for txn in changed:
                self.ledger.upsert_bank_transaction(
                    txn.id, account_id, txn.date, txn.model_dump_json()
                )
            self.ledger.set(key, new_knowledge)
            txns.extend(
                Txn.model_validate_json(raw) for raw in self.ledger.bank_transactions(account_id)
            )
        self.ledger.prune_bank_transactions(today - timedelta(days=CACHE_DAYS))
        return txns

    def _open_order(self, order) -> OpenOrder | None:
        items = self.ledger.items(order.id)
        if not order.ynab_transaction_id or any(
            item.amount is None or not item.category_id for item in items
        ):
            return None
        return OpenOrder(
            id=order.id,
            merchant=order.merchant,
            order_date=order.order_date,
            total=to_milliunits(order.grand_total),
            transaction_id=order.ynab_transaction_id,
            order_number=order.order_number,
            lines=tuple(
                OrderLine(
                    description=item.description,
                    key=item.normalized_key,
                    category_id=item.category_id,
                    amount=to_milliunits(item.amount),
                    sub_id=item.ynab_subtransaction_id,
                )
                for item in items
            ),
        )

    def gather(self, today: date, transactions: list[Txn]) -> PlanInput:
        not_before = self.ledger.get("reconcile_start")
        if not_before is None:
            not_before = today.isoformat()
            self.ledger.set("reconcile_start", not_before)
        logged = [o for o in self.ledger.orders_in_state("logged") if not o.dry_run]
        matched = [o for o in self.ledger.orders_in_state("matched") if not o.dry_run]
        learned = set(self.ledger.get("learned_orders", []))
        recent = today - timedelta(days=CACHE_DAYS)
        candidates = [
            o for o in [*logged, *matched] if o.id not in learned and o.order_date >= recent
        ]
        recurring = self.recurring()
        merchants = {o.merchant for o in self.ledger.orders_with_transactions()}
        merchants.update(item.merchant for item in recurring if item.merchant)
        return PlanInput(
            today=today,
            transactions=transactions,
            logged_orders=[o for o in map(self._open_order, logged) if o],
            approved_candidates=[o for o in map(self._open_order, candidates) if o],
            merchants=sorted(merchants),
            recurring=recurring,
            asked=self.ledger.asked_bank_transaction_ids(),
            not_before=date.fromisoformat(not_before),
            window_days=self.settings.reconcile.window_days,
            grace_days=self.settings.reconcile.grace_days,
            recurring_tolerance=self.settings.reconcile.recurring_tolerance,
            category_names={c.id: c.name for c in self.categorizer.categories},
        )

    # -- applying ----------------------------------------------------------

    def run(
        self,
        send: Callable[[str], int] | None = None,
        *,
        today: date | None = None,
        dry_run: bool | None = None,
    ) -> list[str]:
        """One reconciliation pass. Returns a line per action. In dry-run mode
        nothing is written to YNAB, the ledger's order states, or the group."""
        today = today or date.today()
        dry_run = self.settings.dry_run if dry_run is None else dry_run
        transactions = self.sync(today)
        data = self.gather(today, transactions)
        actions = plan_actions(data)
        by_id = {t.id: t for t in transactions}
        own_ids = {o.transaction_id for o in [*data.logged_orders, *data.approved_candidates]}
        report = []
        for action in actions:
            report.append(f"{'would ' if dry_run else ''}{action.kind}: {action.summary}")
            if dry_run:
                continue
            try:
                self._apply(action, by_id, own_ids, send)
            except YnabError as exc:
                log.error("Reconcile action %s failed: %s", action.kind, exc)
                report.append(f"  failed: {exc}")
        for line in report:
            log.info("reconcile %s", line)
        return report

    def _apply(
        self,
        action: Action,
        by_id: dict[str, Txn],
        own_ids: set[str],
        send: Callable[[str], int] | None,
    ) -> None:
        if action.kind == "mark_matched":
            self.ledger.update_order(action.order_id, state="matched")
        elif action.kind == "mark_reverted":
            self.ledger.update_order(action.order_id, state="reverted")
        elif action.kind == "learn":
            for key, category_id in action.rules:
                self.categorizer.learn(key, category_id, "approved")
            learned = self.ledger.get("learned_orders", [])
            self.ledger.set("learned_orders", [*learned, action.order_id][-2000:])
        elif action.kind == "apply_imports":
            # Guard again at the point of writing: only unapproved, memo-less imports.
            if not all(may_modify(by_id[txn_id], own_ids) for txn_id, _ in action.updates):
                return
            for txn_id, changes in action.updates:
                self.ynab.update_transaction(txn_id, changes)
            self.ynab.delete_transaction(action.delete_transaction_id)
            self.ledger.update_order(action.order_id, state="matched")
            learned = self.ledger.get("learned_orders", [])
            self.ledger.set("learned_orders", [*learned, action.order_id][-2000:])
        elif action.kind == "annotate":
            if not may_modify(by_id[action.transaction_id], own_ids):
                return
            for txn_id, changes in action.updates:
                self.ynab.update_transaction(txn_id, changes)
            timestamp = send(action.message) if send else 0
            self.ledger.answer_question(
                self.ledger.add_question(
                    timestamp, "import_notice", bank_transaction_id=action.transaction_id
                )
            )
        elif action.kind == "ask" and send is not None:
            timestamp = send(action.message)
            self.ledger.add_question(
                timestamp, "import_memo", bank_transaction_id=action.transaction_id
            )

    # -- answers to "what was it?" -----------------------------------------

    def answer(self, question: Question, text: str) -> Reply:
        handler = {
            "import_memo": self._answer_memo,
            "import_category": self._answer_category,
            "recurring_offer": self._answer_offer,
        }.get(question.question_type)
        if handler is None:
            self.ledger.answer_question(question.id)
            return Reply("That one is already taken care of.")
        return handler(question, text.strip())

    def _again(self, question: Question, text: str) -> Reply:
        return Reply(
            text,
            {
                "type": question.question_type,
                "bank_transaction_id": question.bank_transaction_id,
                "payload": question.payload,
            },
        )

    def _fetch(self, question: Question, expected_memo: str) -> Txn | None:
        """The import, if the bot may still write to it."""
        try:
            txn = self.ynab.get_transaction(question.bank_transaction_id)
        except YnabError:
            return None
        untouched = (txn.memo or "").strip() == expected_memo
        if txn.deleted or txn.approved or txn.cleared == "reconciled" or not untouched:
            return None
        return txn

    def _answer_memo(self, question: Question, text: str) -> Reply:
        if not text:
            return self._again(question, "What was it? A few words is enough.")
        txn = self._fetch(question, "")
        self.ledger.answer_question(question.id)
        if txn is None:
            return Reply("Someone already handled that one in YNAB, so I left it alone.")
        memo = truncate_words(text, MEMO_MAX)
        key = normalize_key(text)
        category_id = self.categorizer.from_rules(text)
        if self.settings.dry_run:
            return Reply(f'DRY RUN: would note {_describe(txn)} as "{memo}".')
        changes: dict[str, Any] = {"memo": memo}
        if category_id:
            changes["category_id"] = category_id
        self.ynab.update_transaction(txn.id, changes)
        if not category_id:
            return Reply(
                f'Noted. Which category is "{shorten(text)}"? Reply to this message.',
                {
                    "type": "import_category",
                    "bank_transaction_id": txn.id,
                    "payload": {"key": key, "memo": memo},
                },
            )
        name = self.categorizer.name_of(category_id)
        reply = Reply(f"Noted: {_describe(txn)} · {name}: {shorten(text)}")
        return self._maybe_offer(reply, txn, text, key, category_id)

    def _answer_category(self, question: Question, text: str) -> Reply:
        category = match_category(text, self.categorizer.categories)
        if category is None:
            return self._again(
                question, f'I don\'t have one category that matches "{text[:40]}". Which is it?'
            )
        txn = self._fetch(question, question.payload.get("memo", ""))
        self.ledger.answer_question(question.id)
        if txn is None:
            return Reply("Someone already handled that one in YNAB, so I left it alone.")
        if self.settings.dry_run:
            return Reply(f"DRY RUN: would set {_describe(txn)} to {category.name}.")
        self.ynab.update_transaction(txn.id, {"category_id": category.id})
        key = question.payload.get("key", "")
        self.categorizer.learn(key, category.id, "human")
        reply = Reply(f"Noted: {_describe(txn)} · {category.name}")
        return self._maybe_offer(reply, txn, question.payload.get("memo", ""), key, category.id)

    def _maybe_offer(self, reply: Reply, txn: Txn, text: str, key: str, category_id: str) -> Reply:
        """After the same answer twice for similar amounts, offer to make it recurring."""
        amount = Decimal(-txn.amount) / 1000
        tolerance = Decimal(str(self.settings.reconcile.recurring_tolerance))
        history = self.ledger.get("import_answers", [])
        seen_before = any(
            past["key"] == key and abs(Decimal(past["amount"]) - amount) <= amount * tolerance
            for past in history
        )
        self.ledger.set("import_answers", [*history, {"key": key, "amount": str(amount)}][-500:])
        if not seen_before or not key:
            return reply
        merchant = next(
            (m for m in sorted({o.merchant for o in self.ledger.orders_with_transactions()})
             if payee_matches(txn, m)),
            None,
        )  # fmt: skip
        reply.text += (
            f'\nThat is the second "{shorten(text)}" around {fmt_money(amount)}. '
            "Reply yes to this message and I will fill it in automatically from now on."
        )
        reply.question = {
            "type": "recurring_offer",
            "bank_transaction_id": txn.id,
            "payload": {
                "description": truncate_words(text, 80),
                "price": str(amount),
                "category_id": category_id,
                "merchant": merchant,
            },
        }
        return reply

    def _answer_offer(self, question: Question, text: str) -> Reply:
        self.ledger.answer_question(question.id)
        if text.lower().rstrip(".!") not in _YES:
            return Reply("Okay, I won't.")
        learned = self.ledger.get("recurring_learned", [])
        self.ledger.set("recurring_learned", [*learned, question.payload])
        return Reply(f'Done. I will fill in "{question.payload["description"]}" automatically.')
