from datetime import date
from decimal import Decimal as D

import pytest
from conftest import fixture_text, make_pdf

from receiptbot.parsers import run_parsers
from receiptbot.parsers.amazon import AmazonParser
from receiptbot.pdftext import PdfError, extract_text
from receiptbot.validate import allocate, check_totals


def parse(name):
    outcome = run_parsers(fixture_text(name))
    assert outcome.receipt is not None, f"{name} did not parse"
    return outcome.receipt


def test_single_item():
    r = parse("amazon_single.txt")
    assert r.source == "parser:amazon"
    assert r.merchant == "Amazon"
    assert r.order_number == "111-0000000-0000001"
    assert r.order_date == date(2026, 11, 3)
    assert r.grand_total == D("31.99")
    assert r.payment_last4 == "0000"
    assert [(i.quantity, i.line_total) for i in r.items] == [(1, D("31.99"))]
    assert r.items[0].description.startswith("Good Boy Kibble Adult Dry Dog Food")
    assert r.adjustments == []


def test_multi_item_with_quantity_wrapped_title_and_tax():
    r = parse("amazon_multi.txt")
    assert r.grand_total == D("57.89")
    assert [(i.quantity, i.line_total) for i in r.items] == [(1, D("31.99")), (2, D("22.38"))]
    assert r.items[1].description.endswith("(Pack of 1), Unscented")
    assert [(a.label, a.amount, a.kind) for a in r.adjustments] == [
        ("Estimated tax to be collected", D("3.52"), "tax")
    ]
    parts = allocate([i.line_total for i in r.items], r.grand_total)
    assert sum(parts) == r.grand_total
    assert parts == [D("34.06"), D("23.83")]


def test_discount_free_shipping_and_inline_price():
    r = parse("amazon_discount.txt")
    assert r.order_date == date(2026, 10, 28)
    assert [i.line_total for i in r.items] == [D("24.99"), D("14.50")]
    assert r.items[0].description == "Brightleaf Laundry Detergent Pacs, Fresh Scent, 81 Count"
    assert {a.label: a.amount for a in r.adjustments} == {
        "Shipping & Handling": D("5.99"),
        "Free Shipping": D("-5.99"),
        "Promotion Applied": D("-2.00"),
        "Estimated tax to be collected": D("2.87"),
    }
    assert r.grand_total == D("40.36")
    assert check_totals(r).ok


def test_gift_card_partial_payment_logs_card_charged_amount():
    r = parse("amazon_giftcard.txt")
    assert r.grand_total == D("18.42")  # what the card was charged
    assert r.paid_elsewhere == D("10.00")
    assert r.items_total == D("26.49")
    parts = allocate([i.line_total for i in r.items], r.grand_total)
    assert sum(parts) == D("18.42")


def test_other_merchant_does_not_match():
    outcome = run_parsers(fixture_text("other_store.txt"))
    assert outcome.receipt is None
    assert outcome.matched is None


def test_matching_layout_that_fails_validation_is_a_miss():
    text = fixture_text("amazon_single.txt").replace("Grand Total: $31.99", "Grand Total: $99.99")
    outcome = run_parsers(text)
    assert outcome.receipt is None
    assert outcome.matched == "amazon"


def test_matching_layout_without_item_lines_is_a_miss():
    text = fixture_text("amazon_single.txt").replace("1 of: ", "")
    outcome = run_parsers(text)
    assert outcome.receipt is None
    assert outcome.matched == "amazon"


def test_line_total_interpretation_when_unit_price_does_not_reproduce_subtotal():
    text = fixture_text("amazon_multi.txt")
    text = text.replace("Item(s) Subtotal: $54.37", "Item(s) Subtotal: $43.18")
    text = text.replace("Total before tax: $54.37", "Total before tax: $43.18")
    text = text.replace("Grand Total: $57.89", "Grand Total: $46.70")
    r = AmazonParser().parse(text)
    assert [i.line_total for i in r.items] == [D("31.99"), D("11.19")]
    assert check_totals(r).ok


def test_same_input_same_output():
    text = fixture_text("amazon_multi.txt")
    assert run_parsers(text) == run_parsers(text)


def test_pdf_text_layer_round_trip():
    pdf = make_pdf(fixture_text("amazon_multi.txt").splitlines())
    text = extract_text(pdf, max_pages=5)
    receipt = run_parsers(text).receipt
    assert receipt is not None
    assert receipt.order_number == "111-0000000-0000002"
    assert receipt.grand_total == D("57.89")
    assert len(receipt.items) == 2


def test_pdf_without_text_layer_returns_empty():
    assert extract_text(make_pdf([]), max_pages=5) == ""


def test_pdf_page_limit():
    with pytest.raises(PdfError, match="pages"):
        extract_text(make_pdf(["hello"], pages=4), max_pages=3)


def test_garbage_is_not_a_pdf():
    with pytest.raises(PdfError):
        extract_text(b"this is not a pdf at all", max_pages=3)
