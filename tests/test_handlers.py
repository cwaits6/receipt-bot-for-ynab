"""Routing tests with a fake transport: allowlist, commands, questions, documents."""

from conftest import fixture_text, make_pdf
from fakes import TODAY, make_pipeline, make_settings

from receiptbot.categorize import normalize_key
from receiptbot.handlers import Bot
from receiptbot.signal import Attachment, Incoming

GROUP = "GROUP="
ALEX = "+15550100001"
SAM_UUID = "22222222-2222-4222-8222-222222222222"
DOG_FOOD = "Good Boy Kibble Adult Dry Dog Food, Chicken & Rice Formula, 30 lb. Bag"


class FakeTransport:
    def __init__(self):
        self.sent = []  # (text, quote_timestamp)
        self.reactions = []
        self.files = {}
        self.fetched = []
        self._clock = 9000

    def send(self, group_id, text, *, quote_timestamp=None, quote_author=None):
        assert group_id == GROUP
        self._clock += 1
        self.sent.append((text, quote_timestamp))
        return self._clock

    def react(self, group_id, emoji, *, author, timestamp):
        self.reactions.append((author, timestamp))

    def fetch_attachment(self, attachment_id, group_id):
        self.fetched.append(attachment_id)
        return self.files[attachment_id]

    @property
    def last(self):
        return self.sent[-1][0]

    @property
    def last_timestamp(self):
        return self._clock


def make_bot(**kwargs):
    settings = kwargs.pop("settings", None) or make_settings()
    pipeline, ynab, llm, ledger = make_pipeline(settings=settings, **kwargs)
    transport = FakeTransport()
    bot = Bot(settings, pipeline, transport, today=lambda: TODAY)
    return bot, transport, ynab, ledger


_stamp = iter(range(1_700_000_000_000, 1_800_000_000_000, 1000))


def message(text="", *, sender=ALEX, uuid=None, group=GROUP, attachments=(), quote=None):
    return Incoming(
        sender_number=sender,
        sender_uuid=uuid,
        timestamp=next(_stamp),
        group_id=group,
        text=text,
        attachments=tuple(attachments),
        quote_timestamp=quote,
    )


def pdf_attachment(transport, fixture="amazon_single.txt", content_type="application/pdf"):
    data = make_pdf(fixture_text(fixture).splitlines())
    transport.files["att-1"] = data
    return Attachment(id="att-1", content_type=content_type, filename="x.pdf", size=len(data))


def test_other_groups_and_senders_are_dropped_silently():
    bot, transport, ynab, _ = make_bot()
    bot.handle(message("help", group="OTHER="))
    bot.handle(message("help", sender="+15559999999"))
    bot.handle(message("help", sender=None, uuid="99999999-9999-4999-8999-999999999999"))
    bot.handle(message("help", group=None))
    assert transport.sent == [] and transport.reactions == [] and transport.fetched == []


def test_help_and_uuid_sender():
    bot, transport, _, _ = make_bot()
    bot.handle(message("Help!", sender=None, uuid=SAM_UUID))
    assert "Share an order invoice" in transport.last


def test_ordinary_chatter_is_ignored():
    bot, transport, _, _ = make_bot()
    bot.handle(message("did you order the dog food?"))
    assert transport.sent == []


def test_pdf_is_acknowledged_logged_and_reply_quotes_the_message():
    bot, transport, ynab, ledger = make_bot()
    ledger.upsert_rule(normalize_key(DOG_FOOD), "c-pets", "human")
    msg = message(attachments=[pdf_attachment(transport)], sender=None, uuid=SAM_UUID)
    bot.handle(msg)
    assert transport.reactions == [(SAM_UUID, msg.timestamp)]
    assert transport.sent == [
        ("Logged $31.99 for Sam · Pets: adult dry dog food $31.99", msg.timestamp)
    ]
    assert len(ynab.created) == 1
    order = ledger.order_by_reply(transport.last_timestamp)
    assert (order.sender, order.message_timestamp) == ("Sam", msg.timestamp)


def test_replayed_event_is_handled_once():
    bot, transport, ynab, ledger = make_bot()
    ledger.upsert_rule(normalize_key(DOG_FOOD), "c-pets", "human")
    msg = message(attachments=[pdf_attachment(transport)])
    bot.handle(msg)
    bot.handle(msg)
    assert len(transport.sent) == 1 and len(ynab.created) == 1


def test_undo_as_reply_to_confirmation():
    bot, transport, ynab, ledger = make_bot()
    ledger.upsert_rule(normalize_key(DOG_FOOD), "c-pets", "human")
    bot.handle(message(attachments=[pdf_attachment(transport)]))
    confirmation = transport.last_timestamp
    bot.handle(message("Undo", quote=confirmation))
    assert transport.last == "Undone: removed $31.99 at Amazon from YNAB."
    assert ynab.deleted == ["t-1"]


def test_undo_without_a_reply_or_on_someone_elses_message_explains():
    bot, transport, ynab, _ = make_bot()
    bot.handle(message("undo"))
    assert "Reply" in transport.last
    bot.handle(message("undo", quote=12345))
    assert transport.last == 'I can only undo my own "Logged" messages.'
    assert ynab.deleted == []


def test_question_answer_round_trip_in_group():
    bot, transport, ynab, ledger = make_bot()
    bot.handle(message(attachments=[pdf_attachment(transport)]))
    assert transport.last.startswith('Which category is "adult dry dog food" ($31.99)')
    question = transport.last_timestamp

    bot.handle(message("pets", quote=question, sender=None, uuid=SAM_UUID))
    assert transport.last == "Logged $31.99 for Alex · Pets: adult dry dog food $31.99"
    assert len(ynab.created) == 1
    assert ledger.get_rule(normalize_key(DOG_FOOD)).category_id == "c-pets"


def test_cancel_a_pending_question():
    bot, transport, ynab, ledger = make_bot()
    bot.handle(message(attachments=[pdf_attachment(transport)]))
    bot.handle(message("cancel", quote=transport.last_timestamp))
    assert transport.last == "Okay, dropped that one. Nothing was logged."
    assert ledger.orders_in_state("needs_attention") == [] and ynab.created == []


def test_long_pasted_text_is_processed_and_short_text_is_not():
    bot, transport, ynab, ledger = make_bot()
    ledger.upsert_rule(normalize_key(DOG_FOOD), "c-pets", "human")
    bot.handle(message(fixture_text("amazon_single.txt")))
    assert transport.last.startswith("Logged $31.99")
    assert transport.fetched == []


def test_unsupported_and_oversized_attachments():
    settings = make_settings(limits={"max_file_bytes": 50})
    bot, transport, _, _ = make_bot(settings=settings)
    zip_file = Attachment(id="z", content_type="application/zip", filename="a.zip", size=10)
    bot.handle(message(attachments=[zip_file]))
    assert "PDFs and images" in transport.last
    big = Attachment(id="big", content_type="application/pdf", filename="a.pdf", size=51)
    bot.handle(message(attachments=[big]))
    assert transport.last == "That file is too large for me to read."
    assert transport.fetched == []  # refused before download


def test_image_with_llm_disabled_asks_for_pdf_or_text():
    settings = make_settings(llm={"enabled": False})
    bot, transport, _, _ = make_bot(settings=settings)
    transport.files["img"] = b"\x89PNG fake"
    image = Attachment(id="img", content_type="image/png", filename=None, size=9)
    bot.handle(message(attachments=[image]))
    assert "PDF invoice or paste the order text" in transport.last


def test_unexpected_error_is_reported_not_swallowed():
    bot, transport, _, _ = make_bot()
    missing = Attachment(id="nope", content_type="application/pdf", filename="a.pdf", size=5)
    bot.handle(message(attachments=[missing]))  # fetch raises KeyError in the fake
    assert transport.last == "Something went wrong on my side, so nothing was logged for that."


def test_keep_attachments_off_by_default_and_on_when_set(tmp_path):
    bot, transport, _, _ = make_bot(settings=make_settings(tmp_path))
    bot.handle(message(attachments=[pdf_attachment(transport)]))
    assert not (tmp_path / "attachments").exists()

    kept = make_settings(tmp_path, keep_attachments=True)
    bot, transport, _, _ = make_bot(settings=kept)
    bot.handle(message(attachments=[pdf_attachment(transport)]))
    assert len(list((tmp_path / "attachments").glob("*.pdf"))) == 1


def test_bot_never_reacts_to_its_own_messages_on_a_linked_account():
    """Linked to a person's account, the bot's replies are sent as that person."""
    from dataclasses import replace

    bot, transport, ynab, ledger = make_bot()
    bot.handle(message("help"))
    echo = replace(message(fixture_text("amazon_single.txt")), timestamp=transport.last_timestamp)
    bot.handle(echo)
    assert len(transport.sent) == 1 and transport.reactions == []
