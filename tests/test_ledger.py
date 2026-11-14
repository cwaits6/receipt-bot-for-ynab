from datetime import date
from decimal import Decimal as D

from receiptbot.ledger import Ledger


def new_order(ledger, **over):
    fields = dict(
        source_hash="h1",
        order_number="111-0000000-0000001",
        merchant="Amazon",
        order_date=date(2026, 11, 3),
        currency="USD",
        grand_total=D("43.18"),
        account_id="acct",
        sender="Alex",
        source="parser:amazon",
        state="logged",
        message_timestamp=1000,
        items=[
            dict(description="dog food", normalized_key="dog food", line_total=D("31.99"),
                 category_id="c-pets", category_source="rule"),
            dict(description="trash bags", normalized_key="trash bag", line_total=D("11.19")),
        ],
    )  # fmt: skip
    return ledger.create_order(**(fields | over))


def test_creates_file_in_wal_mode_and_reopens(tmp_path):
    path = tmp_path / "data" / "ledger.sqlite3"
    ledger = Ledger(path)
    assert ledger.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert ledger.db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    order_id = new_order(ledger)
    ledger.close()
    again = Ledger(path)  # migrations are not re-run
    assert again.get_order(order_id).grand_total == D("43.18")
    assert again.db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_order_round_trip_keeps_decimals_and_items():
    ledger = Ledger(":memory:")
    order = ledger.get_order(new_order(ledger))
    assert (order.merchant, order.order_date, order.state) == (
        "Amazon",
        date(2026, 11, 3),
        "logged",
    )
    items = ledger.items(order.id)
    assert [i.line_total for i in items] == [D("31.99"), D("11.19")]
    assert items[1].category_id is None
    ledger.set_item_category(items[1].id, "c-house", "human")
    ledger.set_item_posting(items[1].id, D("11.19"), "s-2")
    updated = ledger.items(order.id)[1]
    assert (updated.category_id, updated.category_source) == ("c-house", "human")
    assert (updated.amount, updated.ynab_subtransaction_id) == (D("11.19"), "s-2")


def test_duplicate_by_order_number_or_hash_ignores_reverted():
    ledger = Ledger(":memory:")
    first = new_order(ledger)
    assert ledger.find_duplicate("111-0000000-0000001", "different").id == first
    assert ledger.find_duplicate(None, "h1").id == first
    assert ledger.find_duplicate("111-9999999-9999999", "h1") is None
    ledger.update_order(first, state="reverted")
    assert ledger.find_duplicate("111-0000000-0000001", "h1") is None


def test_questions():
    ledger = Ledger(":memory:")
    order_id = new_order(ledger, state="needs_attention")
    qid = ledger.add_question(555, "category", order_id=order_id, payload={"item_id": 2})
    question = ledger.open_question(555)
    assert (question.id, question.question_type, question.payload) == (
        qid,
        "category",
        {"item_id": 2},
    )
    ledger.answer_question(qid)
    assert ledger.open_question(555) is None
    ledger.add_question(556, "import_memo", bank_transaction_id="bank-1")
    assert ledger.asked_bank_transaction_ids() == {"bank-1"}


def test_reply_lookup_and_kv():
    ledger = Ledger(":memory:")
    order_id = new_order(ledger)
    ledger.update_order(order_id, reply_timestamp=777, ynab_transaction_id="t-1")
    assert ledger.order_by_reply(777).ynab_transaction_id == "t-1"
    assert ledger.order_by_reply(1) is None
    assert ledger.get("missing", 5) == 5
    ledger.set("knowledge:acct", 42)
    ledger.set("knowledge:acct", 43)
    assert ledger.get("knowledge:acct") == 43


def test_bank_transaction_cache():
    ledger = Ledger(":memory:")
    ledger.upsert_bank_transaction("b1", "acct", date(2026, 11, 1), '{"v":1}')
    ledger.upsert_bank_transaction("b1", "acct", date(2026, 11, 2), '{"v":2}')
    ledger.upsert_bank_transaction("b2", "acct", date(2026, 8, 1), '{"v":3}')
    ledger.prune_bank_transactions(date(2026, 9, 1))
    assert ledger.bank_transactions("acct") == ['{"v":2}']
    ledger.delete_bank_transaction("b1")
    assert ledger.bank_transactions("acct") == []
