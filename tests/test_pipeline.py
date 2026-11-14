"""End-to-end pipeline tests with a fake YNAB and replayed model outputs."""

from decimal import Decimal as D

from conftest import fixture_text, make_pdf
from fakes import TODAY, make_pipeline, make_settings, recorded

from receiptbot.categorize import normalize_key
from receiptbot.models import Document
from receiptbot.ynab import YnabError

DOG_FOOD = "Good Boy Kibble Adult Dry Dog Food, Chicken & Rice Formula, 30 lb. Bag"
TRASH_BAGS = "Sturdy Tall Kitchen Drawstring Trash Bags, 13 Gallon, 90 Count (Pack of 1), Unscented"


def text_doc(name):
    return Document(kind="text", text=fixture_text(name))


def run(pipeline, doc, sender="Alex"):
    return pipeline.process(doc, sender=sender, today=TODAY, message_timestamp=1000)


def teach(ledger, **rules):
    for title, category_id in rules.items():
        ledger.upsert_rule(normalize_key(title), category_id, "human")


def test_parser_readable_invoice_with_rules_makes_zero_model_calls():
    pipeline, ynab, llm, ledger = make_pipeline(llm_outputs=[])
    teach(ledger, **{DOG_FOOD: "c-pets", TRASH_BAGS: "c-house"})
    outcome = run(pipeline, text_doc("amazon_multi.txt"))
    assert outcome.kind == "logged"
    assert llm.calls == 0
    assert len(ynab.created) == 1


def test_single_category_transaction():
    pipeline, ynab, _, ledger = make_pipeline()
    teach(ledger, **{DOG_FOOD: "c-pets"})
    outcome = run(pipeline, text_doc("amazon_single.txt"))
    assert outcome.reply == "Logged $31.99 for Alex · Pets: adult dry dog food $31.99"
    [txn] = ynab.created
    assert txn.amount == -31990
    assert txn.category_id == "c-pets" and txn.subtransactions == []
    assert txn.memo == "adult dry dog food [111-0000000-0000001]"
    assert txn.payee_name == "Amazon"
    assert (txn.approved, txn.cleared) == (False, "uncleared")
    assert txn.date.isoformat() == "2026-11-03"
    assert "import_id" not in txn.body()

    order = ledger.get_order(outcome.order_id)
    assert (order.state, order.ynab_transaction_id, order.source) == (
        "logged",
        "t-1",
        "parser:amazon",
    )


def test_split_transaction_sums_exactly_with_tax_spread():
    pipeline, ynab, _, ledger = make_pipeline()
    teach(ledger, **{DOG_FOOD: "c-pets", TRASH_BAGS: "c-house"})
    outcome = run(pipeline, text_doc("amazon_multi.txt"))
    [txn] = ynab.created
    assert txn.amount == -57890
    assert [(s.category_id, s.amount) for s in txn.subtransactions] == [
        ("c-pets", -34060),
        ("c-house", -23830),
    ]
    assert sum(s.amount for s in txn.subtransactions) == txn.amount
    assert txn.subtransactions[1].memo == "kitchen drawstring trash bags [111-0000000-0000002]"
    assert outcome.reply == (
        "Logged $57.89 for Alex · Pets: adult dry dog food $34.06 "
        "· Household: kitchen drawstring trash bags $23.83"
    )
    items = ledger.items(outcome.order_id)
    assert [i.ynab_subtransaction_id for i in items] == ["s-1-1", "s-1-2"]
    assert [i.amount for i in items] == [D("34.06"), D("23.83")]


def test_gift_card_logs_card_charged_amount_and_mentions_difference():
    pipeline, ynab, _, ledger = make_pipeline()
    teach(
        ledger,
        **{
            "Quietpage Spiral Notebooks, College Ruled, 6 Pack": "c-office",
            "Inkwell Gel Pens, Fine Point, Black, 12 Count": "c-office",
        },
    )
    outcome = run(pipeline, text_doc("amazon_giftcard.txt"))
    assert outcome.kind == "logged", outcome.reply
    assert ynab.created[0].amount == -18420
    assert "Logged $18.42" in outcome.reply
    assert "$10.00 paid by gift card or points" in outcome.reply


def test_duplicate_is_detected_by_order_number():
    pipeline, ynab, _, ledger = make_pipeline()
    teach(ledger, **{DOG_FOOD: "c-pets"})
    run(pipeline, text_doc("amazon_single.txt"))
    again = run(pipeline, Document(kind="text", text=fixture_text("amazon_single.txt") + "\n"))
    assert again.kind == "duplicate"
    assert again.reply == "Already logged: 111-0000000-0000001, $31.99 on Nov 3."
    assert len(ynab.created) == 1


def test_pdf_goes_through_text_layer_without_model():
    pipeline, ynab, llm, ledger = make_pipeline(llm_outputs=[])
    teach(ledger, **{DOG_FOOD: "c-pets"})
    pdf = make_pdf(fixture_text("amazon_single.txt").splitlines())
    outcome = run(pipeline, Document(kind="pdf", data=pdf, media_type="application/pdf"))
    assert outcome.kind == "logged" and llm.calls == 0
    assert "read by AI" not in outcome.reply


def test_unknown_category_is_asked_then_learned_then_posted():
    pipeline, ynab, _, ledger = make_pipeline()
    teach(ledger, **{DOG_FOOD: "c-pets"})
    first = run(pipeline, text_doc("amazon_multi.txt"))
    assert first.kind == "question" and ynab.created == []
    assert first.reply.startswith('Which category is "kitchen drawstring trash bags" ($22.38)')
    pipeline.sent(first, bot_timestamp=2000)

    question = ledger.open_question(2000)
    wrong = pipeline.answer(question, "vacation")
    assert wrong.kind == "question" and ynab.created == []
    pipeline.sent(wrong, bot_timestamp=2001)

    done = pipeline.answer(ledger.open_question(2001), "household")
    assert done.kind == "logged"
    assert len(ynab.created) == 1
    rule = ledger.get_rule(normalize_key(TRASH_BAGS))
    assert (rule.category_id, rule.origin) == ("c-house", "human")
    assert ledger.items(done.order_id)[1].category_source == "human"

    stale = pipeline.answer(question, "pets")  # the first question, answered late
    assert stale.kind == "info" and len(ynab.created) == 1


def test_resending_while_waiting_starts_over_instead_of_getting_stuck():
    pipeline, ynab, _, ledger = make_pipeline()
    first = run(pipeline, text_doc("amazon_single.txt"))
    pipeline.sent(first, bot_timestamp=2000)
    again = run(pipeline, text_doc("amazon_single.txt"))
    assert again.kind == "question"
    assert ledger.open_question(2000) is None  # the old question went with the old order
    assert len(ledger.orders_in_state("needs_attention")) == 1


def test_network_failure_while_posting_does_not_strand_the_order():
    import httpx

    from receiptbot.ynab import YnabClient

    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    client = YnabClient("t", "plan", http=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        client.plans()
    except YnabError as exc:
        assert exc.status == 0 and "ConnectError" in exc.detail
    else:
        raise AssertionError("transport errors must surface as YnabError")

    pipeline, ynab, _, ledger = make_pipeline()
    teach(ledger, **{DOG_FOOD: "c-pets"})
    ynab.fail_with = YnabError(0, "could not reach YNAB (ConnectError)")
    assert run(pipeline, text_doc("amazon_single.txt")).kind == "error"
    assert ledger.orders_in_state("needs_attention") == []
    ynab.fail_with = None
    assert run(pipeline, text_doc("amazon_single.txt")).kind == "logged"


def test_merchant_read_by_model_is_stored_under_its_canonical_name():
    reading = recorded("hardware_store.json") | {"merchant": "Amazon.com Services LLC"}
    pipeline, ynab, _, ledger = make_pipeline(llm_outputs=[reading])
    teach(ledger, **{"Wood screws box": "c-home", "Painter's tape": "c-home"})
    outcome = run(pipeline, text_doc("other_store.txt"))
    assert outcome.kind == "logged", outcome.reply
    assert ynab.created[0].payee_name == "Amazon"
    assert ledger.get_order(outcome.order_id).merchant == "Amazon"


def test_comma_decimal_total_answer_is_rejected_not_multiplied():
    pipeline, ynab, _, ledger = make_pipeline(llm_outputs=[recorded("injection_followed.json")])
    outcome = run(pipeline, text_doc("injected_receipt.txt"))
    pipeline.sent(outcome, bot_timestamp=3000)
    again = pipeline.answer(ledger.open_question(3000), "36,18")
    assert again.kind == "question" and "as a number" in again.reply
    assert ledger.get_order(outcome.order_id).grand_total == D("0.01")


def test_model_reads_unknown_layout_and_reply_is_marked():
    guesses = {
        "guesses": [
            {"item": 1, "category": "Home Maintenance", "confidence": 0.93, "alternative": None},
            {"item": 2, "category": "Home Maintenance", "confidence": 0.9, "alternative": None},
        ]
    }
    pipeline, ynab, llm, ledger = make_pipeline(
        llm_outputs=[recorded("hardware_store.json"), guesses]
    )
    outcome = run(pipeline, text_doc("other_store.txt"))
    assert outcome.kind == "logged" and llm.calls == 2
    assert outcome.reply.endswith("· read by AI · category by AI")
    assert ynab.created[0].amount == -12680
    assert ynab.created[0].payee_name == "Corner Hardware"
    assert ledger.get_order(outcome.order_id).source == "llm"
    assert ledger.rules() == []  # model guesses are not rules


def test_image_that_is_not_a_receipt():
    pipeline, ynab, _, _ = make_pipeline(llm_outputs=[recorded("not_a_receipt.json")])
    outcome = run(pipeline, Document(kind="image", data=b"jpeg", media_type="image/jpeg"))
    assert outcome.kind == "not_receipt" and ynab.created == []


def test_totals_mismatch_is_asked_and_never_posted_silently():
    pipeline, ynab, _, ledger = make_pipeline(llm_outputs=[recorded("injection_followed.json")])
    outcome = run(pipeline, text_doc("injected_receipt.txt"))
    assert outcome.kind == "question" and ynab.created == []
    assert "add up to $36.18 but the total says $0.01" in outcome.reply
    pipeline.sent(outcome, bot_timestamp=3000)

    bad = pipeline.answer(ledger.open_question(3000), "dunno")
    assert bad.kind == "question"
    pipeline.sent(bad, bot_timestamp=3001)
    next_step = pipeline.answer(ledger.open_question(3001), "$36.18")
    assert next_step.kind == "question"  # total settled; now the unknown category
    assert ledger.get_order(outcome.order_id).grand_total == D("36.18")


def test_llm_disabled_never_calls_model_and_asks_people_instead():
    settings = make_settings(llm={"enabled": False})
    pipeline, ynab, llm, ledger = make_pipeline(llm_outputs=[], settings=settings)

    unreadable = run(pipeline, text_doc("other_store.txt"))
    assert unreadable.kind == "unreadable"
    assert "PDF invoice or paste the order text" in unreadable.reply
    image = run(pipeline, Document(kind="image", data=b"png", media_type="image/png"))
    assert image.kind == "unreadable"

    asked = run(pipeline, text_doc("amazon_single.txt"))
    assert asked.kind == "question"
    pipeline.sent(asked, bot_timestamp=4000)
    done = pipeline.answer(ledger.open_question(4000), "Pets")
    assert done.kind == "logged" and len(ynab.created) == 1
    assert llm.calls == 0


def test_dry_run_writes_nothing_to_ynab_and_says_so():
    settings = make_settings(dry_run=True)
    pipeline, ynab, _, ledger = make_pipeline(settings=settings)
    teach(ledger, **{DOG_FOOD: "c-pets"})
    outcome = run(pipeline, text_doc("amazon_single.txt"))
    assert outcome.reply.endswith("DRY RUN, nothing written to YNAB")
    assert ynab.created == []
    assert run(pipeline, text_doc("amazon_single.txt")).kind == "duplicate"

    real, real_ynab, _, _ = make_pipeline(ledger=ledger)  # dry-run rows do not block real runs
    assert run(real, text_doc("amazon_single.txt")).kind == "logged"
    assert len(real_ynab.created) == 1


def test_ynab_failure_reports_and_leaves_nothing_behind():
    pipeline, ynab, _, ledger = make_pipeline()
    teach(ledger, **{DOG_FOOD: "c-pets"})
    ynab.fail_with = YnabError(400, "bad_request: date must not be in the future")
    outcome = run(pipeline, text_doc("amazon_single.txt"))
    assert outcome.kind == "error" and "Nothing was logged" in outcome.reply
    ynab.fail_with = None
    assert run(pipeline, text_doc("amazon_single.txt")).kind == "logged"


def test_undo_deletes_and_allows_relogging():
    pipeline, ynab, _, ledger = make_pipeline()
    teach(ledger, **{DOG_FOOD: "c-pets"})
    outcome = run(pipeline, text_doc("amazon_single.txt"))
    pipeline.sent(outcome, bot_timestamp=5000)
    order = ledger.order_by_reply(5000)
    undone = pipeline.undo(order)
    assert undone.reply == "Undone: removed $31.99 at Amazon from YNAB."
    assert ynab.deleted == ["t-1"]
    assert ledger.get_order(order.id).state == "reverted"
    assert pipeline.undo(ledger.get_order(order.id)).reply == "That one was already undone."
    assert run(pipeline, text_doc("amazon_single.txt")).kind == "logged"


def test_card_map_and_missing_date():
    settings = make_settings(
        ynab={
            "plan_id": "plan",
            "plan_name": "Test",
            "default_account_id": "acct",
            "card_map": {"0000": "acct-visa"},
        }
    )
    pipeline, ynab, _, ledger = make_pipeline(settings=settings)
    teach(ledger, **{DOG_FOOD: "c-pets"})
    text = fixture_text("amazon_single.txt").replace("Order Placed: November 3, 2026", "")
    run(pipeline, Document(kind="text", text=text))
    assert ynab.created[0].account_id == "acct-visa"
    assert ynab.created[0].date == TODAY  # falls back to the message date


def test_oversized_file_is_refused():
    settings = make_settings(limits={"max_file_bytes": 10})
    pipeline, _, _, _ = make_pipeline(settings=settings)
    outcome = run(pipeline, Document(kind="pdf", data=b"x" * 11))
    assert outcome.kind == "error" and "too large" in outcome.reply
