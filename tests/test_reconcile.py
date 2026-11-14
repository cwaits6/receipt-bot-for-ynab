"""Reconciliation: pure matching functions first, then the applying layer."""

from datetime import date
from decimal import Decimal as D

import pytest
from fakes import TODAY, FakeYnab, make_pipeline, make_settings

from receiptbot.reconcile import (
    OpenOrder,
    OrderLine,
    PlanInput,
    Reconciler,
    RecurringItem,
    allocate_to_shipments,
    explain_orders,
    import_updates,
    load_recurring,
    match_recurring,
    may_modify,
    payee_matches,
    plan_actions,
)
from receiptbot.ynab import SubTxn, Txn

ACCT = "acct"


def txn(id, amount, on, *, imported=True, payee="AMAZON MKTPL*AB12C", **over):
    return Txn(
        id=id,
        date=on if isinstance(on, date) else date.fromisoformat(on),
        amount=amount,
        account_id=ACCT,
        payee_name=over.pop("payee_name", "Amazon" if imported else "Amazon"),
        import_id=f"YNAB:{amount}:{on}:1" if imported else None,
        import_payee_name_original=payee if imported else None,
        **over,
    )


def line(desc, cat, amount, sub=None):
    return OrderLine(description=desc, key=desc, category_id=cat, amount=amount, sub_id=sub)


def order(id, total, on="2026-11-03", lines=None, merchant="Amazon"):
    lines = lines or (line("dog food", "c-pets", total),)
    return OpenOrder(
        id=id,
        merchant=merchant,
        order_date=date.fromisoformat(on),
        total=total,
        transaction_id=f"ph-{id}",
        order_number=f"111-0000000-000000{id}",
        lines=tuple(lines),
    )


def plan(**over):
    base = dict(
        today=TODAY,
        transactions=[],
        logged_orders=[],
        approved_candidates=[],
        merchants=["Amazon"],
        recurring=[],
        asked=set(),
        not_before=date(2026, 11, 1),
        category_names={"c-pets": "Pets", "c-house": "Household"},
    )
    return plan_actions(PlanInput(**(base | over)))


def kinds(actions):
    return [a.kind for a in actions]


# -- small predicates --------------------------------------------------------


def test_payee_matching_uses_aliases_and_every_payee_field():
    assert payee_matches(txn("a", -1000, "2026-11-04", payee="AMZN Mktp US*1A2B3"), "Amazon")
    assert payee_matches(txn("a", -1000, "2026-11-04", payee="Amazon.com*AB12"), "Amazon")
    assert payee_matches(txn("a", -1000, "2026-11-04", payee="CORNER HARDWARE 12", payee_name="x"),
                         "Corner Hardware")  # fmt: skip
    assert not payee_matches(
        txn("a", -1000, "2026-11-04", payee="GROCERY MART", payee_name="Grocery"), "Amazon"
    )
    assert not payee_matches(txn("a", -1000, "2026-11-04"), "")


def test_may_modify_rule_seven():
    own = {"mine"}
    assert may_modify(txn("imp", -1000, "2026-11-04"), own)
    assert not may_modify(txn("imp", -1000, "2026-11-04", approved=True), own)
    assert not may_modify(txn("imp", -1000, "2026-11-04", memo="birthday gift"), own)
    assert not may_modify(txn("manual", -1000, "2026-11-04", imported=False), own)
    assert not may_modify(txn("imp", -1000, "2026-11-04", cleared="reconciled"), own)
    assert not may_modify(txn("imp", -1000, "2026-11-04", deleted=True), own)
    assert may_modify(txn("mine", -1000, "2026-11-04", imported=False, memo="x"), own)
    assert not may_modify(
        txn("mine", -1000, "2026-11-04", imported=False, cleared="reconciled"), own
    )


# -- explaining orders with imports ------------------------------------------


def test_exact_single_match():
    o = order(1, 43180)
    [found] = explain_orders([o], [txn("i1", -43180, "2026-11-05")], 21)
    assert (found.order.id, [t.id for t in found.imports]) == (1, ["i1"])


def test_two_shipments_sum_to_order():
    o = order(1, 43180)
    imports = [
        txn("i1", -31990, "2026-11-05"),
        txn("i2", -11190, "2026-11-08"),
        txn("i3", -5000, "2026-11-06"),  # unrelated charge
    ]
    [found] = explain_orders([o], imports, 21)
    assert [t.id for t in found.imports] == ["i1", "i2"]


def test_three_shipments():
    o = order(1, 60000)
    imports = [txn(f"i{n}", -a, "2026-11-06") for n, a in enumerate([10000, 20000, 30000])]
    [found] = explain_orders([o], imports, 21)
    assert len(found.imports) == 3


def test_ambiguous_subsets_are_left_alone():
    o = order(1, 30000)
    imports = [
        txn("i1", -10000, "2026-11-05"),
        txn("i2", -20000, "2026-11-05"),
        txn("i3", -15000, "2026-11-06"),
        txn("i4", -15000, "2026-11-07"),
    ]
    assert explain_orders([o], imports, 21) == []


def test_two_equal_imports_for_one_order_are_ambiguous():
    o = order(1, 20000)
    imports = [txn("i1", -20000, "2026-11-05"), txn("i2", -20000, "2026-11-06")]
    assert explain_orders([o], imports, 21) == []


def test_two_orders_with_same_total_and_one_import_are_ambiguous():
    orders = [order(1, 20000), order(2, 20000, on="2026-11-04")]
    assert explain_orders(orders, [txn("i1", -20000, "2026-11-05")], 21) == []


def test_imports_outside_window_or_other_payee_do_not_count():
    o = order(1, 43180, on="2026-11-03")
    early = txn("i1", -43180, "2026-10-30")
    late = txn("i2", -43180, "2026-12-15")
    other = txn("i3", -43180, "2026-11-05", payee="GROCERY MART", payee_name="Grocery Mart")
    assert explain_orders([o], [early, late, other], 21) == []
    edge = txn("i4", -43180, "2026-11-24")  # order date + 21 days
    assert len(explain_orders([o], [edge], 21)) == 1


def test_each_import_explains_at_most_one_order():
    orders = [order(1, 30000, on="2026-11-03"), order(2, 50000, on="2026-11-04")]
    imports = [
        txn("i1", -30000, "2026-11-05"),
        txn("i2", -20000, "2026-11-06"),
        txn("i3", -30000 + 0, "2026-11-20", payee="GROCERY", payee_name="Grocery"),
        txn("i4", -30001, "2026-11-07"),
    ]
    found = {e.order.id: [t.id for t in e.imports] for e in explain_orders(orders, imports, 21)}
    assert found == {1: ["i1"]}  # i1 is taken, so order 2 cannot use it with i2


def test_partial_arrival_explains_nothing_yet():
    o = order(1, 43180)
    assert explain_orders([o], [txn("i1", -31990, "2026-11-05")], 21) == []


def test_explanations_are_deterministic_regardless_of_input_order():
    o = order(1, 43180)
    imports = [txn("i2", -11190, "2026-11-08"), txn("i1", -31990, "2026-11-05")]
    a = explain_orders([o], imports, 21)
    b = explain_orders([o], list(reversed(imports)), 21)
    assert a == b


# -- allocating items to shipments -------------------------------------------


def test_items_pinned_to_shipments_when_unambiguous():
    lines = (line("dog food", "c-pets", 31990), line("trash bags", "c-house", 11190))
    assert allocate_to_shipments(lines, [11190, 31990]) == [[1], [0]]


def test_shipment_tax_rounding_within_a_cent_per_line():
    lines = (line("a", "c-pets", 10010), line("b", "c-house", 20000), line("c", "c-house", 5000))
    assert allocate_to_shipments(lines, [10000, 25010]) == [[0], [1, 2]]


def test_ambiguous_item_split_returns_none():
    lines = (
        line("a", "c-pets", 10000),
        line("b", "c-house", 10000),
        line("c", "c-pets", 20000),
    )
    assert allocate_to_shipments(lines, [20000, 20000]) is None


def test_identical_lines_are_not_ambiguous():
    lines = (line("pens", "c-office", 5000), line("pens", "c-office", 5000))
    assert allocate_to_shipments(lines, [5000, 5000]) == [[0], [1]]


def test_no_valid_item_split_returns_none():
    lines = (line("a", "c-pets", 30000), line("b", "c-house", 13180))
    assert allocate_to_shipments(lines, [21590, 21590]) is None
    assert allocate_to_shipments((line("a", "c-pets", 43180),), [21590, 21590]) is None


def test_updates_for_pinned_shipments_copy_category_and_memo():
    lines = (line("dog food", "c-pets", 31990), line("trash bags", "c-house", 11190))
    o = order(1, 43180, lines=lines)
    [found] = explain_orders(
        [o], [txn("i1", -31990, "2026-11-05"), txn("i2", -11190, "2026-11-08")], 21
    )
    assert import_updates(found) == [
        ("i1", {"category_id": "c-pets", "memo": "dog food [111-0000000-0000001]"}),
        ("i2", {"category_id": "c-house", "memo": "trash bags [111-0000000-0000001]"}),
    ]


def test_updates_fall_back_to_category_mix_that_sums_exactly():
    lines = (line("dog food", "c-pets", 30000), line("trash bags", "c-house", 13180))
    o = order(1, 43180, lines=lines)
    [found] = explain_orders(
        [o], [txn("i1", -21590, "2026-11-05"), txn("i2", -21590, "2026-11-08")], 21
    )
    updates = import_updates(found)
    assert [u[0] for u in updates] == ["i1", "i2"]
    for _, changes in updates:
        assert changes["memo"] == "dog food, trash bags [111-0000000-0000001]"
        subs = changes["subtransactions"]
        assert [s["category_id"] for s in subs] == ["c-pets", "c-house"]
        assert sum(s["amount"] for s in subs) == -21590
        assert subs[0]["amount"] == -15000 and subs[1]["amount"] == -6590
        assert "approved" not in changes


def test_single_import_for_multi_category_order_becomes_a_split():
    lines = (line("dog food", "c-pets", 31990), line("trash bags", "c-house", 11190))
    o = order(1, 43180, lines=lines)
    [found] = explain_orders([o], [txn("i1", -43180, "2026-11-20")], 21)
    [(txn_id, changes)] = import_updates(found)
    assert txn_id == "i1"
    assert [s["amount"] for s in changes["subtransactions"]] == [-31990, -11190]


# -- recurring ---------------------------------------------------------------

LITTER = RecurringItem("cat litter subscription", D("23.74"), "c-pets", "Amazon")


@pytest.mark.parametrize(
    ("amount", "hit"), [(-23740, True), (-24900, True), (-22560, True), (-25000, False)]
)
def test_recurring_tolerance(amount, hit):
    found = match_recurring(txn("i", amount, "2026-11-10"), [LITTER], 0.05)
    assert (found is LITTER) == hit


def test_recurring_needs_merchant_and_a_single_candidate():
    other = txn("i", -23740, "2026-11-10", payee="GROCERY", payee_name="Grocery")
    assert match_recurring(other, [LITTER], 0.05) is None
    twin = RecurringItem("coffee pods", D("23.99"), "c-house", "Amazon")
    assert match_recurring(txn("i", -23740, "2026-11-10"), [LITTER, twin], 0.05) is None


def test_load_recurring(tmp_path):
    from fakes import CATS

    path = tmp_path / "recurring.yaml"
    path.write_text(
        '- {description: "cat litter", merchant: Amazon, price: "23.74", category: Pets}\n'
        '- {description: "bad price", price: "free", category: Pets}\n'
        '- {description: "bad category", price: "5", category: Nope}\n'
    )
    assert load_recurring(path, CATS) == [
        RecurringItem("cat litter", D("23.74"), "c-pets", "Amazon")
    ]
    assert load_recurring(tmp_path / "missing.yaml", CATS) == []


# -- planning ----------------------------------------------------------------


def test_placeholder_matched_by_ynab_is_marked():
    o = order(1, 43180)
    placeholder = txn("ph-1", -43180, "2026-11-03", imported=False, matched_transaction_id="i1")
    actions = plan(logged_orders=[o], transactions=[placeholder])
    assert kinds(actions) == ["mark_matched"]


def test_placeholder_deleted_in_ynab_is_marked_reverted():
    o = order(1, 43180)
    placeholder = txn("ph-1", -43180, "2026-11-03", imported=False, deleted=True)
    assert kinds(plan(logged_orders=[o], transactions=[placeholder])) == ["mark_reverted"]


def test_placeholder_not_yet_in_cache_does_nothing():
    assert plan(logged_orders=[order(1, 43180)], transactions=[]) == []


def test_learning_from_approved_single_category_uses_final_category():
    o = order(1, 43180, lines=(line("dog toy", "c-pets", 43180),))
    approved = txn(
        "ph-1", -43180, "2026-11-03", imported=False, approved=True, category_id="c-gifts"
    )
    [action] = plan(approved_candidates=[o], transactions=[approved])
    assert action.kind == "learn" and action.rules == (("dog toy", "c-gifts"),)


def test_learning_from_approved_split_maps_items_by_subtransaction():
    lines = (line("dog food", "c-pets", 31990, "s1"), line("trash bags", "c-house", 11190, "s2"))
    o = order(1, 43180, lines=lines)
    approved = txn(
        "ph-1", -43180, "2026-11-03", imported=False, approved=True,
        subtransactions=[
            SubTxn(id="s1", amount=-31990, category_id="c-pets"),
            SubTxn(id="s2", amount=-11190, category_id="c-gifts"),  # owner changed it
        ],
    )  # fmt: skip
    [action] = plan(approved_candidates=[o], transactions=[approved])
    assert dict(action.rules) == {"dog food": "c-pets", "trash bags": "c-gifts"}


def test_unapproved_transactions_teach_nothing():
    o = order(1, 43180)
    pending = txn("ph-1", -43180, "2026-11-03", imported=False, category_id="c-pets")
    assert plan(approved_candidates=[o], logged_orders=[o], transactions=[pending]) == []


def test_split_shipments_are_applied_and_placeholder_deleted():
    lines = (line("dog food", "c-pets", 31990), line("trash bags", "c-house", 11190))
    o = order(1, 43180, lines=lines)
    transactions = [
        txn("ph-1", -43180, "2026-11-03", imported=False),
        txn("i1", -31990, "2026-11-05"),
        txn("i2", -11190, "2026-11-08"),
    ]
    [action] = plan(logged_orders=[o], transactions=transactions)
    assert action.kind == "apply_imports"
    assert action.delete_transaction_id == "ph-1"
    assert [u[0] for u in action.updates] == ["i1", "i2"]


def test_approved_or_annotated_imports_are_never_used():
    o = order(1, 43180)
    transactions = [
        txn("ph-1", -43180, "2026-11-03", imported=False),
        txn("i1", -43180, "2026-11-20", approved=True),
        txn("i2", -43180, "2026-11-21", memo="already explained by a person"),
    ]
    assert plan(logged_orders=[o], transactions=transactions) == []


def test_unknown_import_is_asked_after_grace_period_only():
    recent = txn("i1", -14070, "2026-11-12")  # one day before TODAY
    assert plan(transactions=[recent]) == []
    old = txn("i2", -14070, "2026-11-10")
    [action] = plan(transactions=[old])
    assert action.kind == "ask"
    assert action.message == (
        "Amazon $14.07 on Nov 10 has no receipt. What was it? Reply to this message."
    )


def test_imports_are_not_asked_twice_or_from_before_install_or_other_payees():
    assert plan(transactions=[txn("i1", -14070, "2026-11-10")], asked={"i1"}) == []
    assert plan(transactions=[txn("i1", -14070, "2026-10-20")]) == []
    grocery = txn("i1", -14070, "2026-11-10", payee="GROCERY", payee_name="Grocery")
    assert plan(transactions=[grocery]) == []
    refund = txn("i1", 14070, "2026-11-10")
    assert plan(transactions=[refund]) == []


def test_possible_shipment_of_an_open_order_is_not_asked_about():
    o = order(1, 43180)
    transactions = [
        txn("ph-1", -43180, "2026-11-03", imported=False),
        txn("i1", -31990, "2026-11-05"),  # first shipment; second not charged yet
    ]
    assert plan(logged_orders=[o], transactions=transactions) == []


def test_recurring_import_is_annotated_with_notice():
    [action] = plan(transactions=[txn("i1", -23740, "2026-11-10")], recurring=[LITTER])
    assert action.kind == "annotate"
    assert action.updates == (("i1", {"memo": "cat litter subscription", "category_id": "c-pets"}),)
    assert action.message.startswith(
        "Amazon $23.74 on Nov 10 looks like cat litter subscription (Pets)."
    )


def test_questions_are_capped_per_run():
    imports = [txn(f"i{n}", -1000 - n, "2026-11-05") for n in range(6)]
    assert kinds(plan(transactions=imports)) == ["ask"] * 3


def test_plan_is_pure_and_repeatable():
    o = order(1, 43180)
    transactions = [
        txn("ph-1", -43180, "2026-11-03", imported=False),
        txn("i1", -43180, "2026-11-20"),
    ]
    assert plan(logged_orders=[o], transactions=transactions) == plan(
        logged_orders=[o], transactions=transactions
    )


# -- applying ----------------------------------------------------------------

DOG_FOOD = "Good Boy Kibble Adult Dry Dog Food, Chicken & Rice Formula, 30 lb. Bag"
TRASH_BAGS = "Sturdy Tall Kitchen Drawstring Trash Bags, 13 Gallon, 90 Count (Pack of 1), Unscented"


def logged_setup(dry_run=False, recurring=None):
    from conftest import fixture_text

    from receiptbot.categorize import normalize_key
    from receiptbot.models import Document

    settings = make_settings(dry_run=False)
    pipeline, ynab, _, ledger = make_pipeline(settings=settings, ynab=FakeYnab())
    ledger.upsert_rule(normalize_key(DOG_FOOD), "c-pets", "human")
    ledger.upsert_rule(normalize_key(TRASH_BAGS), "c-house", "human")
    outcome = pipeline.process(
        Document(kind="text", text=fixture_text("amazon_multi.txt")), sender="Alex", today=TODAY
    )
    assert outcome.kind == "logged"
    settings.dry_run = dry_run
    reconciler = Reconciler(settings, ledger, ynab, pipeline.categorizer, recurring)
    return reconciler, ynab, ledger, outcome.order_id


def test_run_uses_delta_requests_and_remembers_server_knowledge():
    reconciler, ynab, ledger, _ = logged_setup()
    reconciler.run(today=TODAY)
    reconciler.run(today=TODAY)
    assert ynab.delta_calls == [("acct", None), ("acct", 1)]
    assert ledger.get("knowledge:acct") == 2


def test_run_marks_matched_then_learns_after_approval():
    reconciler, ynab, ledger, order_id = logged_setup()
    ynab.transactions["t-1"].matched_transaction_id = "bank-1"
    reconciler.run(today=TODAY)
    assert ledger.get_order(order_id).state == "matched"

    ynab.transactions["t-1"].approved = True
    ynab.transactions["t-1"].subtransactions[1].category_id = "c-gifts"
    report = reconciler.run(today=TODAY)
    assert any(line.startswith("learn") for line in report)
    from receiptbot.categorize import normalize_key

    assert ledger.get_rule(normalize_key(TRASH_BAGS)).category_id == "c-gifts"
    assert ledger.get_rule(normalize_key(TRASH_BAGS)).origin == "approved"
    assert not any(line.startswith("learn") for line in reconciler.run(today=TODAY))


def test_run_applies_split_shipments_and_deletes_placeholder():
    reconciler, ynab, ledger, order_id = logged_setup()
    ynab.add(txn("i1", -34060, "2026-11-05"))
    ynab.add(txn("i2", -23830, "2026-11-09"))
    reconciler.run(today=TODAY)
    assert ynab.deleted == ["t-1"]
    assert ynab.transactions["i1"].category_id == "c-pets"
    assert ynab.transactions["i2"].category_id == "c-house"
    assert ynab.transactions["i1"].approved is False
    assert ynab.transactions["i2"].memo == "kitchen drawstring trash bags [111-0000000-0000002]"
    assert ledger.get_order(order_id).state == "matched"


def test_dry_run_prints_but_changes_nothing():
    reconciler, ynab, ledger, order_id = logged_setup(dry_run=True)
    ynab.add(txn("i1", -34060, "2026-11-05"))
    ynab.add(txn("i2", -23830, "2026-11-09"))
    sent = []
    report = reconciler.run(lambda text: sent.append(text) or 1, today=TODAY)
    assert len(report) == 1 and report[0].startswith("would apply_imports: order")
    assert ynab.updated == [] and ynab.deleted == [] and sent == []
    assert ledger.get_order(order_id).state == "logged"


def test_ask_then_answer_sets_memo_and_category_then_offers_recurring():
    reconciler, ynab, ledger, _ = logged_setup()
    ledger.set("reconcile_start", "2026-11-01")
    ledger.upsert_rule("cat litter", "c-pets", "human")
    ynab.transactions["t-1"].matched_transaction_id = "bank"  # order is settled
    ynab.add(txn("i1", -14070, "2026-11-08"))
    sent = []

    def send(text):
        sent.append(text)
        return 7000 + len(sent)

    reconciler.run(send, today=TODAY)
    assert sent == ["Amazon $14.07 on Nov 8 has no receipt. What was it? Reply to this message."]
    assert reconciler.run(send, today=TODAY) == []  # not asked again

    reply = reconciler.answer(ledger.open_question(7001), "cat litter")
    assert reply.text == "Noted: Amazon $14.07 on Nov 8 · Pets: cat litter"
    assert reply.question is None
    assert (ynab.transactions["i1"].memo, ynab.transactions["i1"].category_id) == (
        "cat litter",
        "c-pets",
    )

    ynab.add(txn("i2", -14250, "2026-11-10"))
    reconciler.run(send, today=TODAY)
    again = reconciler.answer(ledger.open_question(7002), "Cat litter")
    assert "Reply yes" in again.text
    assert again.question["type"] == "recurring_offer"
    qid = ledger.add_question(7003, "recurring_offer", bank_transaction_id="i2",
                              payload=again.question["payload"])  # fmt: skip
    done = reconciler.answer(ledger.open_question(7003), "yes")
    assert done.text.startswith("Done.") and qid
    [learned] = [r for r in reconciler.recurring() if r.description == "Cat litter"]
    assert (learned.price, learned.category_id, learned.merchant) == (
        D("14.25"),
        "c-pets",
        "Amazon",
    )


def test_answer_with_unknown_category_asks_second_question_and_learns():
    reconciler, ynab, ledger, _ = logged_setup()
    ynab.add(txn("i1", -14070, "2026-11-08"))
    qid = ledger.add_question(8000, "import_memo", bank_transaction_id="i1")
    reply = reconciler.answer(ledger.open_question(8000), "birthday candles")
    assert reply.text == 'Noted. Which category is "birthday candles"? Reply to this message.'
    assert ynab.transactions["i1"].memo == "birthday candles" and qid
    ledger.add_question(8001, "import_category", bank_transaction_id="i1",
                        payload=reply.question["payload"])  # fmt: skip
    done = reconciler.answer(ledger.open_question(8001), "gifts")
    assert done.text == "Noted: Amazon $14.07 on Nov 8 · Gifts"
    assert ynab.transactions["i1"].category_id == "c-gifts"
    assert ledger.get_rule("birthday candle").category_id == "c-gifts"


def test_answer_leaves_transactions_a_person_already_handled():
    reconciler, ynab, ledger, _ = logged_setup()
    ynab.add(txn("i1", -14070, "2026-11-08", memo="handled by hand", approved=True))
    ledger.add_question(8100, "import_memo", bank_transaction_id="i1")
    reply = reconciler.answer(ledger.open_question(8100), "cat litter")
    assert "left it alone" in reply.text
    assert ynab.updated == []
