from decimal import Decimal as D

import pytest

from receiptbot.models import Adjustment, Item, ParsedReceipt
from receiptbot.validate import (
    allocate,
    check_totals,
    fmt_money,
    normalize_signs,
    parse_money,
    to_milliunits,
    truncate_words,
)


def receipt(items, adjustments, total):
    return ParsedReceipt(
        merchant="Shop",
        grand_total=D(total),
        items=[Item(description=f"item {i}", line_total=D(v)) for i, v in enumerate(items)],
        adjustments=[Adjustment(label=k, amount=D(v)) for k, v in adjustments],
        source="parser:test",
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$43.18", "43.18"),
        ("$1,234.56", "1234.56"),
        ("-$10.00", "-10.00"),
        ("$-10.00", "-10.00"),
        ("−$3.50", "-3.50"),
        ("($5.00)", "-5.00"),
        ("12", "12"),
        ("USD 7.25", "7.25"),
    ],
)
def test_parse_money(text, expected):
    assert parse_money(text) == D(expected)


@pytest.mark.parametrize("text", ["", "free", "$", "1.2.3", "$5 or $6"])
def test_parse_money_rejects_non_amounts(text):
    with pytest.raises(ValueError):
        parse_money(text)


def test_to_milliunits():
    assert to_milliunits(D("43.18")) == 43180
    assert to_milliunits(D("-0.01")) == -10
    assert to_milliunits(D("1234.5")) == 1234500
    assert to_milliunits(D("0")) == 0
    assert isinstance(to_milliunits(D("1.00")), int)


def test_to_milliunits_refuses_sub_milliunit_precision():
    with pytest.raises(ValueError):
        to_milliunits(D("0.0001"))


def test_fmt_money():
    assert fmt_money(D("1234.5")) == "$1,234.50"
    assert fmt_money(D("-3")) == "-$3.00"
    assert fmt_money(D("3"), "SEK") == "3.00 SEK"


def test_totals_ok_within_a_cent():
    assert check_totals(receipt(["31.99", "11.19"], [("Tax", "2.59")], "45.77")).ok
    assert check_totals(receipt(["31.99"], [("Tax", "2.59")], "34.59")).ok


def test_totals_mismatch_reports_both_numbers():
    check = check_totals(receipt(["31.99", "11.19"], [], "50.00"))
    assert not check.ok
    assert (check.computed, check.stated) == (D("43.18"), D("50.00"))


def test_totals_fail_without_items():
    assert not check_totals(receipt([], [], "0.00")).ok


def test_normalize_signs_flips_unsigned_discount():
    fixed = normalize_signs(receipt(["20.00"], [("Coupon discount", "5.00")], "15.00"))
    assert fixed.adjustments[0].amount == D("-5.00")
    assert check_totals(fixed).ok


def test_normalize_signs_leaves_unfixable_receipt_alone():
    original = receipt(["20.00"], [("Coupon discount", "5.00")], "99.00")
    assert normalize_signs(original) == original


def test_allocate_spreads_tax_and_sums_exactly():
    parts = allocate([D("31.99"), D("11.19")], D("45.77"))
    assert sum(parts) == D("45.77")
    assert parts == [D("33.91"), D("11.86")]


def test_allocate_puts_remainder_on_largest_line():
    parts = allocate([D("1.00"), D("1.00"), D("1.00")], D("1.00"))
    assert sum(parts) == D("1.00")
    assert parts == [D("0.34"), D("0.33"), D("0.33")]
    parts = allocate([D("1.00"), D("5.00"), D("1.00")], D("7.01"))
    assert parts == [D("1.00"), D("5.01"), D("1.00")]


@pytest.mark.parametrize(
    ("lines", "total"),
    [
        (["0.01", "0.01", "0.01"], "0.02"),
        (["19.99", "5.49", "102.00", "0.99"], "117.31"),
        (["10.00", "0.00"], "8.50"),
        (["33.33", "33.33", "33.34"], "0.01"),
    ],
)
def test_allocate_always_sums_to_total(lines, total):
    parts = allocate([D(x) for x in lines], D(total))
    assert sum(parts) == D(total)
    assert all(p == p.quantize(D("0.01")) for p in parts)


def test_allocate_zero_base():
    assert allocate([D("0")], D("0")) == [D("0.00")]
    with pytest.raises(ValueError):
        allocate([D("0")], D("5.00"))


def test_truncate_words():
    assert truncate_words("short", 10) == "short"
    cut = truncate_words("dog food, trash bags, sponges", 20)
    assert len(cut) <= 20
    assert cut == "dog food, trash…"
