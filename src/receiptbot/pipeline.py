"""One document in, one outcome out: read, categorize, validate, dedupe, post, record.

Plain code decides everything here. The model is consulted through `llm` only
where the brief allows it, and its output is validated like any other input.
Used by both the Signal handlers and scripts/log_invoice.py.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

from receiptbot.categorize import Categorizer, match_category
from receiptbot.ledger import Ledger, Order, OrderItem, Question
from receiptbot.llm import LlmError, ReadResult
from receiptbot.models import Document, ParsedReceipt, canonical_merchant
from receiptbot.parsers import run_parsers
from receiptbot.pdftext import PdfError, extract_text
from receiptbot.reconcile import payee_matches
from receiptbot.settings import Settings
from receiptbot.validate import (
    allocate,
    check_totals,
    fmt_money,
    normalize_signs,
    parse_money,
    to_milliunits,
    truncate_words,
)
from receiptbot.ynab import MEMO_MAX, PAYEE_MAX, NewTxn, Split, Txn, YnabClient, YnabError

log = logging.getLogger(__name__)

RESEND_HINT = "Please send the PDF invoice or paste the order text instead."
_YES = {"yes", "y", "ok", "okay", "correct", "right", "confirm", "confirmed", "yep", "yup"}


class DocumentReader(Protocol):
    def read_document(self, document: Document) -> ReadResult: ...


@dataclass
class Outcome:
    """What happened, and what to say. `question` is set when the reply asks
    something: the caller registers it against the sent message's timestamp."""

    kind: str  # logged | question | duplicate | not_receipt | unreadable | error | info
    reply: str
    order_id: int | None = None
    question: dict[str, Any] | None = None
    model_used: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _existing_changes(new: NewTxn, target: Txn, overwrite: bool) -> dict[str, Any]:
    """What to write onto a charge that is already in YNAB. The memo is always
    written. Categories are written when the person asked to replace the details,
    or when the charge has none yet; an existing split is never restructured."""
    changes: dict[str, Any] = {"memo": new.memo}
    if any(not sub.deleted for sub in target.subtransactions):
        return changes
    if overwrite or target.category_id is None:
        if new.subtransactions:
            changes["subtransactions"] = [s.model_dump(mode="json") for s in new.subtransactions]
        else:
            changes["category_id"] = new.category_id
    return changes


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        ynab: YnabClient,
        categorizer: Categorizer,
        llm: DocumentReader | None = None,
    ):
        self.settings = settings
        self.ledger = ledger
        self.ynab = ynab
        self.categorizer = categorizer
        self.llm = llm if settings.llm.enabled else None

    # -- reading -----------------------------------------------------------

    def read(self, document: Document) -> tuple[ParsedReceipt | None, Outcome | None]:
        """Path A (parsers) first; path B (model) only when A cannot do it."""
        limits = self.settings.limits
        if len(document.data) > limits.max_file_bytes:
            return None, Outcome("error", "That file is too large for me to read.")

        text = document.text
        if document.kind == "pdf":
            try:
                text = extract_text(document.data, max_pages=limits.max_pdf_pages)
            except PdfError as exc:
                return None, Outcome("error", f"I couldn't open that PDF: {exc}.")
        if text:
            outcome = run_parsers(text)
            if outcome.receipt is not None:
                log.info("Read by %s", outcome.receipt.source)
                return outcome.receipt, None
            log.info("No parser result (layout matched: %s)", outcome.matched)

        if self.llm is None:
            return None, Outcome("unreadable", f"I couldn't read that document. {RESEND_HINT}")
        try:
            result = self.llm.read_document(document)
        except LlmError as exc:
            log.warning("Model fallback failed: %s", exc)
            return None, Outcome(
                "unreadable", f"I couldn't read that document ({exc}). {RESEND_HINT}"
            )
        if not result.is_receipt:
            return None, Outcome(
                "not_receipt",
                "That doesn't look like a receipt, so I didn't log anything.",
                model_used=True,
            )
        if result.receipt is None:
            return None, Outcome(
                "unreadable",
                f"I couldn't read the amounts on that one. {RESEND_HINT}",
                model_used=True,
            )
        return normalize_signs(result.receipt), None

    # -- main flow ---------------------------------------------------------

    def process(
        self,
        document: Document,
        *,
        sender: str,
        today: date,
        message_timestamp: int | None = None,
    ) -> Outcome:
        receipt, failure = self.read(document)
        if failure is not None:
            return failure
        assert receipt is not None
        model_used = receipt.source == "llm"

        source_hash = hashlib.sha256(document.data or document.text.encode()).hexdigest()
        duplicate = self.ledger.find_duplicate(
            receipt.order_number, source_hash, include_dry_run=self.settings.dry_run
        )
        if duplicate is not None and duplicate.state == "needs_attention":
            # Still waiting on an answer (or stranded by a failure): a resend starts
            # over. Nothing was posted for it, and its questions go with it.
            self.ledger.delete_order(duplicate.id)
        elif duplicate is not None:
            return self._duplicate(duplicate, model_used)

        if receipt.grand_total <= 0:
            paid = receipt.paid_elsewhere
            detail = f" ({fmt_money(paid, receipt.currency)} was paid by gift card or points)"
            return Outcome(
                "info",
                "Nothing was charged to the card for this order"
                f"{detail if paid > 0 else ''}, so I didn't log anything.",
                model_used=model_used,
            )

        order_date = receipt.order_date or today
        order_date = min(order_date, today)  # YNAB does not accept future dates here
        note = ""
        if receipt.paid_elsewhere > 0:
            paid = fmt_money(receipt.paid_elsewhere, receipt.currency)
            note = f"{paid} paid by gift card or points"

        items = self.categorizer.assign(receipt.items) if receipt.items else []
        order_id = self.ledger.create_order(
            source_hash=source_hash,
            order_number=receipt.order_number,
            merchant=canonical_merchant(receipt.merchant),
            order_date=order_date,
            currency=receipt.currency,
            grand_total=receipt.grand_total,
            account_id=self.settings.ynab.account_for(receipt.payment_last4),
            sender=sender,
            source=receipt.source,
            state="needs_attention",
            note=note,
            dry_run=self.settings.dry_run,
            message_timestamp=message_timestamp,
            items=[
                {
                    "description": item.description,
                    "normalized_key": item.key,
                    "line_total": item.line_total,
                    "category_id": item.category_id,
                    "category_source": item.category_source,
                }
                for item in items
            ],
        )
        log.info(
            "Order %s (%s) total %s read by %s",
            order_id,
            receipt.order_number or "no order number",
            receipt.grand_total,
            receipt.source,
        )

        totals = check_totals(receipt)
        if not totals.ok:
            computed = fmt_money(totals.computed, receipt.currency)
            stated = fmt_money(totals.stated, receipt.currency)
            return Outcome(
                "question",
                f"The lines on this receipt add up to {computed} but the total says {stated}. "
                "Reply to this message with the correct total, or resend the receipt.",
                order_id=order_id,
                question={"type": "total", "order_id": order_id, "payload": {}},
                model_used=model_used,
            )
        guesses = {item.key: item.guesses for item in items}
        return self._continue(order_id, guesses=guesses)

    def _duplicate(self, duplicate: Order, model_used: bool) -> Outcome:
        label = duplicate.order_number or "this document"
        total = fmt_money(duplicate.grand_total, duplicate.currency)
        reply = f"Already logged: {label}, {total} on {duplicate.order_date:%b %-d}."
        return Outcome("duplicate", reply, order_id=duplicate.id, model_used=model_used)

    def _continue(self, order_id: int, guesses: dict[str, list[str]] | None = None) -> Outcome:
        """Ask about the next uncategorized item, or post when none are left."""
        order = self.ledger.get_order(order_id)
        assert order is not None
        items = self.ledger.items(order_id)
        model_used = order.source == "llm"
        missing = next((item for item in items if item.category_id is None), None)
        if missing is None:
            return self._post(order, items)

        ideas = [
            self.categorizer.name_of(cid) for cid in (guesses or {}).get(missing.normalized_key, [])
        ]
        hint = f" Maybe {' or '.join(ideas)}?" if ideas else ""
        amount = fmt_money(missing.line_total, order.currency)
        return Outcome(
            "question",
            f'Which category is "{missing.description}" ({amount}) from {order.merchant}?{hint} '
            "Reply to this message with the category name.",
            order_id=order_id,
            question={"type": "category", "order_id": order_id, "payload": {"item_id": missing.id}},
            model_used=model_used,
        )

    # -- posting -----------------------------------------------------------

    def _post(
        self,
        order: Order,
        items: list[OrderItem],
        decision: tuple[str, str] | None = None,
    ) -> Outcome:
        try:
            amounts = allocate([item.line_total for item in items], order.grand_total)
            to_milliunits(order.grand_total)
        except ValueError:
            self.ledger.delete_order(order.id)
            return Outcome(
                "error",
                "I couldn't split that total across the items, so I didn't log anything. "
                "Please enter this one in YNAB by hand.",
            )

        groups: dict[str, list[tuple[OrderItem, Decimal]]] = {}
        for item, amount in zip(items, amounts, strict=True):
            groups.setdefault(item.category_id or "", []).append((item, amount))

        suffix = f" [{order.order_number}]" if order.order_number else ""

        def memo(parts: list[tuple[OrderItem, Decimal]]) -> str:
            text = ", ".join(item.description for item, _ in parts)
            return truncate_words(text, MEMO_MAX - len(suffix)) + suffix

        new = NewTxn(
            account_id=order.account_id,
            date=order.order_date,
            amount=-to_milliunits(order.grand_total),
            payee_name=order.merchant[:PAYEE_MAX],
            memo=memo(list(zip(items, amounts, strict=True))),
            cleared="uncleared",
            approved=self.settings.ynab.approved,
        )
        if len(groups) == 1:
            new.category_id = next(iter(groups))
        else:
            new.subtransactions = [
                Split(
                    amount=-to_milliunits(sum((a for _, a in parts), Decimal("0"))),
                    category_id=category_id,
                    memo=memo(parts),
                )
                for category_id, parts in groups.items()
            ]

        dry_run = self.settings.dry_run
        sub_ids: dict[str, str | None] = {}
        transaction_id = None
        target: Txn | None = None  # an existing charge to fill in instead of adding one
        overwrite = False
        try:
            if decision is not None:
                action, existing_id = decision
                target = self.ynab.get_transaction(existing_id)
                if action == "skip":
                    return self._link(order, items, amounts, target, "and I left it as it is")
                overwrite = True
            else:
                matches = self._existing_charges(order)
                if len(matches) > 1:
                    self.ledger.delete_order(order.id)
                    total = fmt_money(order.grand_total, order.currency)
                    return Outcome(
                        "info",
                        f"There are already {len(matches)} {order.merchant} charges of {total} "
                        "in YNAB and I can't tell which one this is, so I didn't add or change "
                        "anything. Please fill it in by hand.",
                    )
                if matches:
                    target = matches[0]
                    if target.cleared == "reconciled":
                        return self._link(
                            order, items, amounts, target, "and reconciled, so I left it alone"
                        )
                    old_memo = (target.memo or "").strip()
                    if old_memo:
                        total = fmt_money(order.grand_total, order.currency)
                        return Outcome(
                            "question",
                            f"There is already a {total} {order.merchant} charge on "
                            f'{target.date:%b %-d} in YNAB with the memo "'
                            f'{truncate_words(old_memo, 80)}". Reply yes to this message to '
                            "replace it with this receipt's details, or no to leave it.",
                            order_id=order.id,
                            question={
                                "type": "existing",
                                "order_id": order.id,
                                "payload": {"transaction_id": target.id},
                            },
                        )
            if target is not None:
                changes = _existing_changes(new, target, overwrite)
                transaction_id = target.id
                result = target if dry_run else self.ynab.update_transaction(target.id, changes)
                wrote_splits = "subtransactions" in changes
            elif not dry_run:
                result = self.ynab.create_transaction(new)
                transaction_id = result.id
                wrote_splits = bool(new.subtransactions)
            if not dry_run and wrote_splits:
                live = [s for s in result.subtransactions if not s.deleted]
                if len(live) == len(new.subtransactions):
                    sub_ids = {
                        split.category_id or "": sub.id
                        for split, sub in zip(new.subtransactions, live, strict=True)
                    }
        except YnabError as exc:
            log.error("YNAB rejected order %s: %s", order.id, exc)
            self.ledger.delete_order(order.id)
            return Outcome(
                "error",
                f"YNAB didn't accept that one ({exc.detail}). Nothing was logged; "
                "please try again later.",
            )

        for item, amount in zip(items, amounts, strict=True):
            self.ledger.set_item_posting(item.id, amount, sub_ids.get(item.category_id or ""))
        # An order that filled in an existing charge is "matched": that transaction
        # is the real one, so undo must never delete it.
        self.ledger.update_order(
            order.id,
            state="matched" if target is not None and not dry_run else "logged",
            ynab_transaction_id=None if dry_run else transaction_id,
            dry_run=dry_run,
        )
        log.info(
            "Order %s %s %s (dry_run=%s)",
            order.id,
            "updated existing" if target is not None else "logged as",
            transaction_id,
            dry_run,
        )

        parts_text = []
        for category_id, parts in groups.items():
            lines = ", ".join(
                f"{item.description} {fmt_money(amount, order.currency)}" for item, amount in parts
            )
            parts_text.append(f"{self.categorizer.name_of(category_id)}: {lines}")
        total = fmt_money(order.grand_total, order.currency)
        if target is None:
            reply = f"Logged {total} for {order.sender}"
        else:
            reply = (
                f"Updated the existing {total} charge from {target.date:%b %-d} for {order.sender}"
            )
            if "category_id" not in changes and "subtransactions" not in changes:
                parts_text = [f"memo: {new.memo}", "kept its category"]
        reply = " \u00b7 ".join([reply, *parts_text])
        if order.note:
            reply += f" \u00b7 {order.note}"
        if order.source == "llm":
            reply += " \u00b7 read by AI"
        if any(item.category_source == "llm" for item in items):
            reply += " \u00b7 category by AI"
        if dry_run:
            reply += " \u00b7 DRY RUN, nothing written to YNAB"
        return Outcome("logged", reply, order_id=order.id, model_used=order.source == "llm")

    def _existing_charges(self, order: Order) -> list[Txn]:
        """Transactions already in YNAB that look like this order's charge: same
        account, same merchant, exact amount, dated around the order. The bot's
        own entries do not count."""
        own = {o.ynab_transaction_id for o in self.ledger.orders_with_transactions()}
        since = order.order_date - timedelta(days=1)
        until = order.order_date + timedelta(days=self.settings.reconcile.window_days)
        amount = -to_milliunits(order.grand_total)
        return [
            txn
            for txn in self.ynab.recent_transactions(order.account_id, since)
            if not txn.deleted
            and txn.id not in own
            and txn.amount == amount
            and since <= txn.date <= until
            and payee_matches(txn, order.merchant)
        ]

    def _link(
        self, order: Order, items: list[OrderItem], amounts: list[Decimal], txn: Txn, why: str
    ) -> Outcome:
        """Record that this order is an existing YNAB transaction, changing nothing there."""
        for item, amount in zip(items, amounts, strict=True):
            self.ledger.set_item_posting(item.id, amount, None)
        dry_run = self.settings.dry_run
        self.ledger.update_order(
            order.id,
            state="logged" if dry_run else "matched",
            ynab_transaction_id=None if dry_run else txn.id,
            dry_run=dry_run,
        )
        self.ledger.set(
            "learned_orders", [*self.ledger.get("learned_orders", []), order.id][-2000:]
        )
        total = fmt_money(order.grand_total, order.currency)
        return Outcome(
            "info",
            f"The {total} {order.merchant} charge from {txn.date:%b %-d} is already in YNAB "
            f"{why}. Nothing was added or changed.",
            order_id=order.id,
        )

    # -- follow-ups --------------------------------------------------------

    def sent(self, outcome: Outcome, bot_timestamp: int) -> None:
        """Record the timestamp of the message that carried `outcome.reply`."""
        if outcome.question is not None:
            self.ledger.add_question(
                bot_timestamp,
                outcome.question["type"],
                order_id=outcome.question.get("order_id"),
                bank_transaction_id=outcome.question.get("bank_transaction_id"),
                payload=outcome.question.get("payload"),
            )
        elif outcome.kind == "logged" and outcome.order_id is not None:
            self.ledger.update_order(outcome.order_id, reply_timestamp=bot_timestamp)

    def answer(self, question: Question, text: str) -> Outcome:
        """Handle a reply to one of the bot's order questions."""
        order = self.ledger.get_order(question.order_id) if question.order_id else None
        if order is None or order.state != "needs_attention":
            self.ledger.answer_question(question.id)
            return Outcome("info", "That one is already taken care of.")
        if question.question_type == "total":
            return self._answer_total(question, order, text)
        if question.question_type == "existing":
            return self._answer_existing(question, order, text)
        return self._answer_category(question, order, text)

    def _again(self, question: Question, reply: str) -> Outcome:
        again = {
            "type": question.question_type,
            "order_id": question.order_id,
            "payload": question.payload,
        }
        return Outcome("question", reply, order_id=question.order_id, question=again)

    def _answer_existing(self, question: Question, order: Order, text: str) -> Outcome:
        answer = text.strip().lower().rstrip(".!")
        if answer in _YES or answer in ("update", "replace"):
            action = "update"
        elif answer in ("no", "n", "nope", "leave", "leave it", "keep", "keep it"):
            action = "skip"
        else:
            return self._again(question, "Reply yes to replace that charge's details, or no.")
        self.ledger.answer_question(question.id)
        decision = (action, question.payload["transaction_id"])
        return self._post(order, self.ledger.items(order.id), decision)

    def _answer_total(self, question: Question, order: Order, text: str) -> Outcome:
        answer = text.strip().lower().rstrip(".!")
        if answer in _YES:
            total = order.grand_total
        else:
            try:
                total = parse_money(text)
            except ValueError:
                return self._again(
                    question,
                    "I need the total as a number, for example 43.18. "
                    "Reply to this message with it, or resend the receipt.",
                )
        if total <= 0:
            return self._again(question, "The total has to be more than zero. What is it?")
        self.ledger.update_order(order.id, grand_total=total)
        self.ledger.answer_question(question.id)
        return self._continue(order.id)

    def _answer_category(self, question: Question, order: Order, text: str) -> Outcome:
        items = self.ledger.items(order.id)
        item = next((i for i in items if i.id == question.payload.get("item_id")), None)
        if item is None:
            self.ledger.answer_question(question.id)
            return self._continue(order.id)
        category = match_category(text, self.categorizer.categories)
        if category is None:
            return self._again(
                question,
                f'I don\'t have one category that matches "{text.strip()[:40]}". '
                f'Reply with the category name for "{item.description}".',
            )
        for other in items:
            if other.category_id is None and other.normalized_key == item.normalized_key:
                self.ledger.set_item_category(other.id, category.id, "human")
        self.categorizer.learn(item.normalized_key, category.id, "human")
        self.ledger.answer_question(question.id)
        return self._continue(order.id)

    def cancel(self, question: Question) -> Outcome:
        """Drop an order that is still waiting on an answer."""
        self.ledger.answer_question(question.id)
        if question.order_id is not None:
            self.ledger.delete_order(question.order_id)
        return Outcome("info", "Okay, dropped that one. Nothing was logged.")

    def undo(self, order: Order) -> Outcome:
        if order.state == "reverted":
            return Outcome("info", "That one was already undone.")
        if order.state == "matched":
            return Outcome(
                "info",
                "That one is already matched to the bank charge, so I left it alone. "
                "Change it in YNAB if it is wrong.",
            )
        if order.ynab_transaction_id and not order.dry_run:
            try:
                self.ynab.delete_transaction(order.ynab_transaction_id)
            except YnabError as exc:
                if exc.status != 404:
                    log.error("Undo of order %s failed: %s", order.id, exc)
                    return Outcome("error", f"YNAB wouldn't delete it ({exc.detail}).")
        self.ledger.update_order(order.id, state="reverted")
        log.info("Order %s reverted", order.id)
        total = fmt_money(order.grand_total, order.currency)
        return Outcome("info", f"Undone: removed {total} at {order.merchant} from YNAB.")
