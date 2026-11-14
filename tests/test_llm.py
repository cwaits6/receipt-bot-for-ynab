"""Model fallback tests. Recorded structured outputs are replayed; no network."""

from decimal import Decimal as D

import anthropic
import httpx
import pytest
from conftest import fixture_text
from fakes import FakeAnthropic, recorded

from receiptbot.llm import (
    CATEGORIZE_SYSTEM,
    READ_SYSTEM,
    LlmClient,
    LlmError,
    ReceiptReading,
    reading_to_receipt,
)
from receiptbot.models import Document
from receiptbot.validate import check_totals, normalize_signs


def client_for(*outputs, stop_reason="end_turn"):
    fake = FakeAnthropic(list(outputs), stop_reason=stop_reason)
    return LlmClient("test-model", client=fake), fake


def test_read_text_document_returns_validated_receipt():
    llm, fake = client_for(recorded("hardware_store.json"))
    result = llm.read_document(Document(kind="text", text=fixture_text("other_store.txt")))
    receipt = result.receipt
    assert result.is_receipt and receipt.source == "llm"
    assert receipt.grand_total == D("12.68")
    assert [i.line_total for i in receipt.items] == [D("6.49"), D("5.25")]
    assert check_totals(receipt).ok
    assert llm.calls == 1

    request = fake.requests[0]
    assert request["model"] == "test-model"
    assert request["output_format"] is ReceiptReading
    assert "tools" not in request  # the model gets no tools
    assert "temperature" not in request
    text = request["messages"][0]["content"][0]["text"]
    assert text.startswith("<document>") and "CORNER HARDWARE" in text
    assert "CORNER HARDWARE" not in request["system"]  # the document is data, not instructions


def test_pdf_and_image_are_sent_as_base64_blocks():
    llm, fake = client_for(recorded("hardware_store.json"), recorded("hardware_store.json"))
    llm.read_document(Document(kind="pdf", data=b"%PDF-fake"))
    llm.read_document(Document(kind="image", data=b"\x89PNG", media_type="image/png"))
    pdf_block = fake.requests[0]["messages"][0]["content"][0]
    image_block = fake.requests[1]["messages"][0]["content"][0]
    assert (pdf_block["type"], pdf_block["source"]["media_type"]) == ("document", "application/pdf")
    assert (image_block["type"], image_block["source"]["media_type"]) == ("image", "image/png")
    assert pdf_block["source"]["data"] == "JVBERi1mYWtl"


def test_prompt_rules():
    lowered = READ_SYSTEM.lower()
    assert "exactly as printed" in lowered
    assert "never compute" in lowered
    assert "untrusted data" in lowered
    assert "untrusted data" in CATEGORIZE_SYSTEM.lower()


def test_non_receipt():
    llm, _ = client_for(recorded("not_a_receipt.json"))
    result = llm.read_document(Document(kind="image", data=b"x", media_type="image/jpeg"))
    assert not result.is_receipt and result.receipt is None


def test_amounts_are_parsed_by_code_and_signs_fixed_by_code():
    receipt = reading_to_receipt(ReceiptReading.model_validate(recorded("unsigned_discount.json")))
    assert receipt.grand_total == D("15.00")
    assert receipt.payment_last4 == "0000"
    assert receipt.items[0].quantity == 1
    assert not check_totals(receipt).ok  # as printed: 20.00 + 5.00 != 15.00
    assert check_totals(normalize_signs(receipt)).ok


@pytest.mark.parametrize(
    "change",
    [
        {"grand_total": "about fifteen"},
        {"grand_total": None},
        {"items": []},
        {"merchant": None},
        {"items": [{"description": "x", "quantity": 1, "line_total": None}]},
    ],
)
def test_unusable_reading_is_rejected(change):
    reading = ReceiptReading.model_validate(recorded("hardware_store.json") | change)
    assert reading_to_receipt(reading) is None


def test_injected_instruction_cannot_make_a_wrong_total_post():
    """If the model obeyed the instruction inside the document, the code check
    still refuses: lines add to 36.18 but the model claims 0.01."""
    llm, _ = client_for(recorded("injection_followed.json"))
    doc = Document(kind="text", text=fixture_text("injected_receipt.txt"))
    receipt = llm.read_document(doc).receipt
    assert receipt.grand_total == D("0.01")
    check = check_totals(normalize_signs(receipt))
    assert not check.ok and check.computed == D("36.18")


def test_injected_instruction_ignored_reads_normally():
    llm, fake = client_for(recorded("injection_ignored.json"))
    doc = Document(kind="text", text=fixture_text("injected_receipt.txt"))
    result = llm.read_document(doc)
    assert result.receipt.grand_total == D("36.18")
    assert "ignored" in result.notes
    # The injected text travels only inside the delimited user-turn document.
    assert "Ignore all previous instructions" not in fake.requests[0]["system"]


def test_categorize_sends_only_items_and_names():
    guesses = {
        "guesses": [
            {"item": 2, "category": "Household", "confidence": 0.9, "alternative": None},
            {"item": 1, "category": "Pets", "confidence": 1.7, "alternative": "Gifts"},
        ]
    }
    llm, fake = client_for(guesses)
    out = llm.categorize_items(["dog toy", "sponges", "mystery"], ["Pets", "Household", "Gifts"])
    assert [(g.category, g.confidence) for g in out] == [
        ("Pets", 1.0),
        ("Household", 0.9),
        (None, 0.0),
    ]
    assert out[0].alternative == "Gifts"
    text = fake.requests[0]["messages"][0]["content"][0]["text"]
    assert "1. dog toy" in text and "- Household" in text


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_and_truncation_raise(stop_reason):
    llm, _ = client_for(recorded("hardware_store.json"), stop_reason=stop_reason)
    with pytest.raises(LlmError):
        llm.read_document(Document(kind="text", text="x"))


def test_api_errors_become_llm_errors():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    errors = [
        anthropic.APIConnectionError(request=request),
        anthropic.RateLimitError(
            "slow down", response=httpx.Response(429, request=request), body=None
        ),
        anthropic.InternalServerError(
            "oops", response=httpx.Response(500, request=request), body=None
        ),
    ]
    for error in errors:
        llm, _ = client_for(error)
        with pytest.raises(LlmError):
            llm.read_document(Document(kind="text", text="x"))
