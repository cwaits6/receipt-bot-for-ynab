"""Local ledger: one SQLite file through the standard library.

Stores the minimum needed to dedupe, undo, learn categories and reconcile.
No raw documents and no model responses are kept here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    """
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY,
        source_hash TEXT NOT NULL,
        order_number TEXT,
        merchant TEXT NOT NULL,
        order_date TEXT NOT NULL,
        currency TEXT NOT NULL,
        grand_total TEXT NOT NULL,
        account_id TEXT NOT NULL,
        ynab_transaction_id TEXT,
        sender TEXT NOT NULL,
        source TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('logged','matched','reverted','needs_attention')),
        note TEXT NOT NULL DEFAULT '',
        dry_run INTEGER NOT NULL DEFAULT 0,
        message_timestamp INTEGER,
        reply_timestamp INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE INDEX orders_order_number ON orders (order_number);
    CREATE INDEX orders_source_hash ON orders (source_hash);
    CREATE INDEX orders_state ON orders (state);

    CREATE TABLE order_items (
        id INTEGER PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
        description TEXT NOT NULL,
        normalized_key TEXT NOT NULL,
        line_total TEXT NOT NULL,
        amount TEXT,
        category_id TEXT,
        category_source TEXT CHECK (category_source IN ('rule','llm','human')),
        ynab_subtransaction_id TEXT
    );
    CREATE INDEX order_items_order ON order_items (order_id);

    CREATE TABLE category_rules (
        normalized_key TEXT PRIMARY KEY,
        category_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        hit_count INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE pending_questions (
        id INTEGER PRIMARY KEY,
        bot_timestamp INTEGER NOT NULL,
        order_id INTEGER REFERENCES orders (id) ON DELETE CASCADE,
        bank_transaction_id TEXT,
        question_type TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        answered_at TEXT
    );
    CREATE INDEX pending_questions_ts ON pending_questions (bot_timestamp);

    CREATE TABLE bank_transactions (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        date TEXT NOT NULL,
        data TEXT NOT NULL
    );

    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class OrderItem:
    id: int
    order_id: int
    description: str
    normalized_key: str
    line_total: Decimal
    amount: Decimal | None
    category_id: str | None
    category_source: str | None
    ynab_subtransaction_id: str | None


@dataclass
class Order:
    id: int
    source_hash: str
    order_number: str | None
    merchant: str
    order_date: date
    currency: str
    grand_total: Decimal
    account_id: str
    ynab_transaction_id: str | None
    sender: str
    source: str
    state: str
    note: str
    dry_run: bool
    message_timestamp: int | None
    reply_timestamp: int | None
    created_at: str


@dataclass
class Rule:
    normalized_key: str
    category_id: str
    origin: str
    hit_count: int
    updated_at: str


@dataclass
class Question:
    id: int
    bot_timestamp: int
    order_id: int | None
    bank_transaction_id: str | None
    question_type: str
    payload: dict[str, Any]
    created_at: str
    answered_at: str | None


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


class Ledger:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One process is the only writer; callers serialize access with a lock.
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self.db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={number};\nCOMMIT;")

    def close(self) -> None:
        self.db.close()

    # -- orders ------------------------------------------------------------

    def create_order(
        self,
        *,
        source_hash: str,
        order_number: str | None,
        merchant: str,
        order_date: date,
        currency: str,
        grand_total: Decimal,
        account_id: str,
        sender: str,
        source: str,
        state: str,
        note: str = "",
        dry_run: bool = False,
        message_timestamp: int | None = None,
        items: list[dict[str, Any]],
    ) -> int:
        """Insert an order with its items. Each item dict has description,
        normalized_key, line_total, category_id and category_source."""
        with self.db:
            self.db.execute("BEGIN")
            cur = self.db.execute(
                """INSERT INTO orders (source_hash, order_number, merchant, order_date, currency,
                   grand_total, account_id, sender, source, state, note, dry_run,
                   message_timestamp, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_hash,
                    order_number,
                    merchant,
                    order_date.isoformat(),
                    currency,
                    str(grand_total),
                    account_id,
                    sender,
                    source,
                    state,
                    note,
                    int(dry_run),
                    message_timestamp,
                    _now(),
                ),
            )
            order_id = int(cur.lastrowid)
            for item in items:
                self.db.execute(
                    """INSERT INTO order_items (order_id, description, normalized_key,
                       line_total, category_id, category_source) VALUES (?,?,?,?,?,?)""",
                    (
                        order_id,
                        item["description"],
                        item["normalized_key"],
                        str(item["line_total"]),
                        item.get("category_id"),
                        item.get("category_source"),
                    ),
                )
        return order_id

    @staticmethod
    def _order(row: sqlite3.Row) -> Order:
        return Order(
            id=row["id"],
            source_hash=row["source_hash"],
            order_number=row["order_number"],
            merchant=row["merchant"],
            order_date=date.fromisoformat(row["order_date"]),
            currency=row["currency"],
            grand_total=Decimal(row["grand_total"]),
            account_id=row["account_id"],
            ynab_transaction_id=row["ynab_transaction_id"],
            sender=row["sender"],
            source=row["source"],
            state=row["state"],
            note=row["note"],
            dry_run=bool(row["dry_run"]),
            message_timestamp=row["message_timestamp"],
            reply_timestamp=row["reply_timestamp"],
            created_at=row["created_at"],
        )

    def get_order(self, order_id: int) -> Order | None:
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return self._order(row) if row else None

    def find_duplicate(self, order_number: str | None, source_hash: str) -> Order | None:
        """An earlier, not-reverted order with this order number, or with this
        content hash when there is no order number."""
        if order_number:
            row = self.db.execute(
                "SELECT * FROM orders WHERE order_number=? AND state!='reverted' ORDER BY id",
                (order_number,),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM orders WHERE source_hash=? AND state!='reverted' ORDER BY id",
                (source_hash,),
            ).fetchone()
        return self._order(row) if row else None

    def order_by_reply(self, reply_timestamp: int) -> Order | None:
        row = self.db.execute(
            "SELECT * FROM orders WHERE reply_timestamp=? ORDER BY id DESC", (reply_timestamp,)
        ).fetchone()
        return self._order(row) if row else None

    def orders_in_state(self, state: str) -> list[Order]:
        rows = self.db.execute("SELECT * FROM orders WHERE state=? ORDER BY id", (state,))
        return [self._order(row) for row in rows]

    def orders_with_transactions(self) -> list[Order]:
        rows = self.db.execute(
            "SELECT * FROM orders WHERE ynab_transaction_id IS NOT NULL ORDER BY id"
        )
        return [self._order(row) for row in rows]

    def update_order(self, order_id: int, **fields: Any) -> None:
        allowed = {
            "state",
            "ynab_transaction_id",
            "reply_timestamp",
            "grand_total",
            "note",
            "dry_run",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        if "grand_total" in fields:
            fields["grand_total"] = str(fields["grand_total"])
        if "dry_run" in fields:
            fields["dry_run"] = int(fields["dry_run"])
        assignments = ", ".join(f"{name}=?" for name in fields)
        self.db.execute(f"UPDATE orders SET {assignments} WHERE id=?", (*fields.values(), order_id))

    def delete_order(self, order_id: int) -> None:
        self.db.execute("DELETE FROM orders WHERE id=?", (order_id,))

    # -- items -------------------------------------------------------------

    def items(self, order_id: int) -> list[OrderItem]:
        rows = self.db.execute(
            "SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,)
        )
        return [
            OrderItem(
                id=row["id"],
                order_id=row["order_id"],
                description=row["description"],
                normalized_key=row["normalized_key"],
                line_total=Decimal(row["line_total"]),
                amount=_dec(row["amount"]),
                category_id=row["category_id"],
                category_source=row["category_source"],
                ynab_subtransaction_id=row["ynab_subtransaction_id"],
            )
            for row in rows
        ]

    def set_item_category(self, item_id: int, category_id: str, source: str) -> None:
        self.db.execute(
            "UPDATE order_items SET category_id=?, category_source=? WHERE id=?",
            (category_id, source, item_id),
        )

    def set_item_posting(self, item_id: int, amount: Decimal, sub_id: str | None) -> None:
        self.db.execute(
            "UPDATE order_items SET amount=?, ynab_subtransaction_id=? WHERE id=?",
            (str(amount), sub_id, item_id),
        )

    # -- category rules ----------------------------------------------------

    def rules(self) -> list[Rule]:
        rows = self.db.execute("SELECT * FROM category_rules ORDER BY normalized_key")
        return [Rule(**dict(row)) for row in rows]

    def get_rule(self, key: str) -> Rule | None:
        row = self.db.execute(
            "SELECT * FROM category_rules WHERE normalized_key=?", (key,)
        ).fetchone()
        return Rule(**dict(row)) if row else None

    def upsert_rule(self, key: str, category_id: str, origin: str) -> None:
        """Save a human-confirmed category. A changed category replaces the rule."""
        self.db.execute(
            """INSERT INTO category_rules (normalized_key, category_id, origin, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT (normalized_key) DO UPDATE SET
                 category_id=excluded.category_id, origin=excluded.origin,
                 updated_at=excluded.updated_at""",
            (key, category_id, origin, _now()),
        )

    def bump_rule(self, key: str) -> None:
        self.db.execute(
            "UPDATE category_rules SET hit_count=hit_count+1 WHERE normalized_key=?", (key,)
        )

    def delete_rule(self, key: str) -> bool:
        cur = self.db.execute("DELETE FROM category_rules WHERE normalized_key=?", (key,))
        return cur.rowcount > 0

    # -- pending questions -------------------------------------------------

    def add_question(
        self,
        bot_timestamp: int,
        question_type: str,
        *,
        order_id: int | None = None,
        bank_transaction_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO pending_questions (bot_timestamp, order_id, bank_transaction_id,
               question_type, payload, created_at) VALUES (?,?,?,?,?,?)""",
            (
                bot_timestamp,
                order_id,
                bank_transaction_id,
                question_type,
                json.dumps(payload or {}),
                _now(),
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _question(row: sqlite3.Row) -> Question:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return Question(**data)

    def open_question(self, bot_timestamp: int) -> Question | None:
        row = self.db.execute(
            """SELECT * FROM pending_questions WHERE bot_timestamp=? AND answered_at IS NULL
               ORDER BY id DESC""",
            (bot_timestamp,),
        ).fetchone()
        return self._question(row) if row else None

    def answer_question(self, question_id: int) -> None:
        self.db.execute(
            "UPDATE pending_questions SET answered_at=? WHERE id=?", (_now(), question_id)
        )

    def asked_bank_transaction_ids(self) -> set[str]:
        rows = self.db.execute(
            "SELECT DISTINCT bank_transaction_id FROM pending_questions "
            "WHERE bank_transaction_id IS NOT NULL"
        )
        return {row[0] for row in rows}

    # -- bank transaction cache (fed by delta requests) --------------------

    def upsert_bank_transaction(self, txn_id: str, account_id: str, on: date, data: str) -> None:
        self.db.execute(
            """INSERT INTO bank_transactions (id, account_id, date, data) VALUES (?,?,?,?)
               ON CONFLICT (id) DO UPDATE SET date=excluded.date, data=excluded.data""",
            (txn_id, account_id, on.isoformat(), data),
        )

    def delete_bank_transaction(self, txn_id: str) -> None:
        self.db.execute("DELETE FROM bank_transactions WHERE id=?", (txn_id,))

    def bank_transactions(self, account_id: str) -> list[str]:
        rows = self.db.execute(
            "SELECT data FROM bank_transactions WHERE account_id=? ORDER BY date, id",
            (account_id,),
        )
        return [row[0] for row in rows]

    def prune_bank_transactions(self, before: date) -> None:
        self.db.execute("DELETE FROM bank_transactions WHERE date<?", (before.isoformat(),))

    # -- kv ----------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?,?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
