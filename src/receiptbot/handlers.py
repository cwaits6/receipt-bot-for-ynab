"""Message routing: allowlist, commands, replies to questions, and documents."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Protocol

from receiptbot.models import Document
from receiptbot.pipeline import Outcome, Pipeline
from receiptbot.reconcile import Reconciler
from receiptbot.settings import Settings
from receiptbot.signal import Attachment, Incoming, SignalError

log = logging.getLogger(__name__)

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ACK_EMOJI = "\U0001f440"
HELP = (
    "Share an order invoice here (PDF, screenshot or pasted text) and I will log it in YNAB.\n"
    '• Reply "undo" to one of my "Logged" messages to remove that transaction.\n'
    '• When I ask a question, reply to that message with the answer, or "cancel" to drop it.\n'
    '• Send "help" to see this again.'
)
_CANCEL = {"undo", "cancel", "skip", "never mind", "nevermind"}


class Transport(Protocol):
    def send(
        self,
        group_id: str,
        text: str,
        *,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
    ) -> int: ...

    def react(self, group_id: str, emoji: str, *, author: str, timestamp: int) -> None: ...

    def fetch_attachment(self, attachment_id: str, group_id: str) -> bytes: ...


class Bot:
    def __init__(
        self,
        settings: Settings,
        pipeline: Pipeline,
        transport: Transport,
        reconciler: Reconciler | None = None,
        today: Callable[[], date] = date.today,
    ):
        assert settings.signal is not None
        self.settings = settings
        self.signal = settings.signal
        self.pipeline = pipeline
        self.ledger = pipeline.ledger
        self.transport = transport
        self.reconciler = reconciler
        self.today = today
        self._lock = threading.Lock()

    # -- sending -----------------------------------------------------------

    def _sent(self, timestamp: int) -> int:
        """Remember what the bot sent. On a linked account its messages come
        from the same person it listens to, so it must never react to them."""
        self.ledger.set(
            "sent_timestamps", [*self.ledger.get("sent_timestamps", []), timestamp][-300:]
        )
        return timestamp

    def _reply(self, msg: Incoming, text: str) -> int:
        return self._sent(
            self.transport.send(
                self.signal.group_id, text, quote_timestamp=msg.timestamp, quote_author=msg.sender
            )
        )

    def _post(self, text: str) -> int:
        return self._sent(self.transport.send(self.signal.group_id, text))

    def _deliver(self, msg: Incoming, outcome: Outcome) -> None:
        timestamp = self._reply(msg, outcome.reply)
        self.pipeline.sent(outcome, timestamp)

    # -- routing -----------------------------------------------------------

    def handle(self, msg: Incoming) -> None:
        """Handle one incoming message. Everything outside the allowlisted group
        and senders is dropped without a reply."""
        if msg.group_id != self.signal.group_id:
            return
        name = self.signal.sender_name(msg.sender_number, msg.sender_uuid)
        if name is None:
            return
        if msg.timestamp in self.ledger.get("sent_timestamps", []) or self._seen(msg):
            return
        try:
            self._route(msg, name)
        except Exception:
            # Never fail silently, and never put document content in the log.
            log.exception("Unhandled error on message %s", msg.timestamp)
            try:
                self._reply(msg, "Something went wrong on my side, so nothing was logged for that.")
            except SignalError:
                log.error("Could not send the error reply either")

    def _seen(self, msg: Incoming) -> bool:
        """Events can be replayed after a reconnect; handle each message once."""
        key = f"{msg.sender}:{msg.timestamp}"
        seen = self.ledger.get("seen_messages", [])
        if key in seen:
            return True
        self.ledger.set("seen_messages", [*seen, key][-500:])
        return False

    def _route(self, msg: Incoming, name: str) -> None:
        text = msg.text.strip()
        command = re.sub(r"[.!]+$", "", text.lower())

        if msg.quote_timestamp is not None and self._handle_reply(msg, text, command):
            return
        if command == "help":
            self._reply(msg, HELP)
            return
        if command == "undo":
            self._reply(msg, 'Reply "undo" to the "Logged" message you want me to remove.')
            return

        supported = [a for a in msg.attachments if self._kind(a)]
        if supported:
            self._ack(msg)
            for attachment in supported:
                self._deliver(msg, self._process_attachment(msg, attachment, name))
        elif msg.attachments:
            self._reply(msg, "I can read PDFs and images. That file type isn't one I can use.")
        elif len(text) >= self.settings.limits.min_text_chars:
            self._ack(msg)
            outcome = self.pipeline.process(
                Document(kind="text", text=text),
                sender=name,
                today=self._message_date(msg),
                message_timestamp=msg.timestamp,
            )
            self._deliver(msg, outcome)
        # Anything else is ordinary chatter: ignore it.

    def _handle_reply(self, msg: Incoming, text: str, command: str) -> bool:
        """A reply to one of the bot's messages. Returns False if it was not one."""
        question = self.ledger.open_question(msg.quote_timestamp)
        if question is not None:
            if question.question_type in ("category", "total"):
                if command in _CANCEL:
                    outcome = self.pipeline.cancel(question)
                else:
                    outcome = self.pipeline.answer(question, text)
                self._deliver(msg, outcome)
            elif self.reconciler is not None:
                if command in _CANCEL:
                    self.ledger.answer_question(question.id)
                    self._reply(msg, "Okay, I'll leave that one for you to sort out in YNAB.")
                    return True
                reply = self.reconciler.answer(question, text)
                timestamp = self._reply(msg, reply.text)
                if reply.question is not None:
                    self.ledger.add_question(
                        timestamp,
                        reply.question["type"],
                        bank_transaction_id=reply.question.get("bank_transaction_id"),
                        payload=reply.question.get("payload"),
                    )
            return True
        if command == "undo":
            order = self.ledger.order_by_reply(msg.quote_timestamp)
            if order is None:
                self._reply(msg, 'I can only undo my own "Logged" messages.')
            else:
                self._reply(msg, self.pipeline.undo(order).reply)
            return True
        return False

    # -- documents ---------------------------------------------------------

    @staticmethod
    def _kind(attachment: Attachment) -> str | None:
        content_type = attachment.content_type.lower()
        if content_type == "application/pdf":
            return "pdf"
        if content_type in IMAGE_TYPES:
            return "image"
        return None

    def _ack(self, msg: Incoming) -> None:
        try:
            self.transport.react(
                self.signal.group_id, ACK_EMOJI, author=msg.sender, timestamp=msg.timestamp
            )
        except SignalError:
            log.warning("Could not send the acknowledgement reaction")

    def _message_date(self, msg: Incoming) -> date:
        if msg.timestamp:
            sent = datetime.fromtimestamp(msg.timestamp / 1000, tz=UTC).astimezone().date()
            return min(sent, self.today())
        return self.today()

    def _process_attachment(self, msg: Incoming, attachment: Attachment, name: str) -> Outcome:
        limit = self.settings.limits.max_file_bytes
        if attachment.size > limit:
            return Outcome("error", "That file is too large for me to read.")
        data = self.transport.fetch_attachment(attachment.id, self.signal.group_id)
        if len(data) > limit:
            return Outcome("error", "That file is too large for me to read.")
        kind = self._kind(attachment)
        if self.settings.keep_attachments:
            self._keep(msg, attachment, data)
        return self.pipeline.process(
            Document(kind=kind, data=data, media_type=attachment.content_type.lower()),
            sender=name,
            today=self._message_date(msg),
            message_timestamp=msg.timestamp,
        )

    def _keep(self, msg: Incoming, attachment: Attachment, data: bytes) -> None:
        folder = self.settings.data_dir / "attachments"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}.get(
            attachment.content_type.lower(), ".bin"
        )
        (folder / f"{msg.timestamp}-{attachment.id[:16]}".replace("/", "_")).with_suffix(
            suffix
        ).write_bytes(data)

    # -- running -----------------------------------------------------------

    def reconcile_once(self) -> None:
        if self.reconciler is None:
            return
        with self._lock:
            try:
                self.reconciler.run(self._post, today=self.today())
            except Exception:
                log.exception("Reconciliation run failed")

    def run(self, stop: threading.Event) -> None:
        """Listen for messages until `stop` is set; reconcile on a timer."""

        def reconcile_loop() -> None:
            interval = self.settings.reconcile.interval_minutes * 60
            while not stop.wait(interval):
                self.reconcile_once()

        threading.Thread(target=reconcile_loop, name="reconcile", daemon=True).start()
        for msg in self.transport.events(stop):
            with self._lock:
                self.handle(msg)
