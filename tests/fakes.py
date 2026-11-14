"""Test doubles. Nothing here touches the network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from receiptbot.categorize import Categorizer
from receiptbot.ledger import Ledger
from receiptbot.llm import LlmClient
from receiptbot.models import Category
from receiptbot.pipeline import Pipeline
from receiptbot.settings import Settings
from receiptbot.ynab import NewTxn, SubTxn, Txn, YnabError

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 13)

CATS = [
    Category("c-pets", "Pets", "Everyday"),
    Category("c-house", "Household", "Everyday"),
    Category("c-office", "Office", "Everyday"),
    Category("c-home", "Home Maintenance", "Everyday"),
    Category("c-gifts", "Gifts", "Fun"),
]


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / "llm" / name).read_text())


class FakeAnthropic:
    """Replays recorded structured outputs through the SDK's `messages.parse` shape."""

    def __init__(self, outputs: list, stop_reason: str = "end_turn"):
        self.outputs = list(outputs)
        self.stop_reason = stop_reason
        self.requests: list[dict] = []
        self.messages = self

    def parse(self, **kwargs):
        self.requests.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        parsed = None if output is None else kwargs["output_format"].model_validate(output)
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=parsed)


class FakeYnab:
    def __init__(self, categories=CATS):
        self._categories = list(categories)
        self.created: list[NewTxn] = []
        self.deleted: list[str] = []
        self.updated: list[tuple[str, dict]] = []
        self.transactions: dict[str, Txn] = {}
        self.fail_with: YnabError | None = None
        self.knowledge = 0
        self.delta_calls: list[tuple[str, int | None]] = []

    def categories(self):
        return self._categories

    def create_transaction(self, new: NewTxn) -> Txn:
        if self.fail_with:
            raise self.fail_with
        self.created.append(new)
        number = len(self.created)
        txn = Txn(
            id=f"t-{number}",
            date=new.date,
            amount=new.amount,
            memo=new.memo,
            approved=new.approved,
            cleared=new.cleared,
            account_id=new.account_id,
            payee_name=new.payee_name,
            category_id=None if new.subtransactions else new.category_id,
            subtransactions=[
                SubTxn(
                    id=f"s-{number}-{i}", amount=s.amount, memo=s.memo, category_id=s.category_id
                )
                for i, s in enumerate(new.subtransactions, start=1)
            ],
        )
        self.transactions[txn.id] = txn
        return txn

    def add(self, txn: Txn) -> Txn:
        self.transactions[txn.id] = txn
        return txn

    def get_transaction(self, transaction_id: str) -> Txn:
        if transaction_id not in self.transactions:
            raise YnabError(404, "resource_not_found: gone")
        return self.transactions[transaction_id]

    def delete_transaction(self, transaction_id: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.deleted.append(transaction_id)
        if transaction_id in self.transactions:
            self.transactions[transaction_id].deleted = True

    def update_transaction(self, transaction_id: str, changes: dict) -> Txn:
        self.updated.append((transaction_id, changes))
        txn = self.transactions[transaction_id]
        subs = changes.get("subtransactions")
        fields = {k: v for k, v in changes.items() if k != "subtransactions"}
        txn = txn.model_copy(update=fields)
        if subs:
            txn.category_id = None
            txn.subtransactions = [
                SubTxn(id=f"{transaction_id}-s{i}", **sub) for i, sub in enumerate(subs, start=1)
            ]
        self.transactions[transaction_id] = txn
        return txn

    def recent_transactions(self, account_id, since_date):
        if self.fail_with:
            raise self.fail_with
        return [
            t
            for t in self.transactions.values()
            if t.account_id == account_id and t.date >= since_date
        ]

    def account_transactions(self, account_id, *, last_knowledge=None, since_date=None):
        self.delta_calls.append((account_id, last_knowledge))
        self.knowledge += 1
        live = [t for t in self.transactions.values() if t.account_id == account_id]
        return live, self.knowledge


def make_settings(tmp_path=None, **over) -> Settings:
    base = {
        "ynab": {"plan_id": "plan", "plan_name": "Test", "default_account_id": "acct"},
        "signal": {
            "group_id": "GROUP=",
            "allowed_senders": [
                {"id": "+15550100001", "name": "Alex"},
                {"id": "22222222-2222-4222-8222-222222222222", "name": "Sam"},
            ],
        },
        "data_dir": str(tmp_path) if tmp_path else "data",
    }
    return Settings.model_validate(base | over)


def make_pipeline(*, llm_outputs=None, settings=None, ledger=None, ynab=None):
    """Returns (pipeline, ynab, llm, ledger). `llm_outputs=None` means no model."""
    settings = settings or make_settings()
    ledger = ledger or Ledger(":memory:")
    ynab = ynab or FakeYnab()
    llm = None
    if llm_outputs is not None:
        llm = LlmClient("test-model", client=FakeAnthropic(llm_outputs))
    categorizer = Categorizer(
        ledger,
        ynab.categories,
        llm=llm if settings.llm.enabled else None,
        min_confidence=settings.llm.min_confidence,
    )
    return Pipeline(settings, ledger, ynab, categorizer, llm=llm), ynab, llm, ledger
