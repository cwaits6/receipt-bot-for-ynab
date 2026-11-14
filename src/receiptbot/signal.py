"""Signal transport over a signal-cli daemon in HTTP mode.

Per signal-cli-jsonrpc(5): `signal-cli -a ACCOUNT daemon --http HOST:PORT` exposes
POST /api/v1/rpc (JSON-RPC), GET /api/v1/events (server-sent events for incoming
messages) and GET /api/v1/check. Method names are the CLI commands and parameters
are the CLI flags in camelCase.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

RECONNECT_SECONDS = 5


class SignalError(Exception):
    pass


@dataclass(frozen=True)
class Attachment:
    id: str
    content_type: str
    filename: str | None
    size: int


@dataclass(frozen=True)
class Incoming:
    sender_number: str | None
    sender_uuid: str | None
    timestamp: int
    group_id: str | None
    text: str
    attachments: tuple[Attachment, ...] = ()
    quote_timestamp: int | None = None

    @property
    def sender(self) -> str:
        return self.sender_number or self.sender_uuid or ""


def parse_event(payload: dict[str, Any]) -> Incoming | None:
    """Turn one received event into an Incoming, or None for anything that is
    not a normal message (receipts, typing, sync messages, reactions)."""
    payload = payload.get("params", payload)
    payload = payload.get("result", payload)
    envelope = payload.get("envelope") or {}
    message = envelope.get("dataMessage")
    if not isinstance(message, dict) or message.get("reaction") or message.get("remoteDelete"):
        return None
    attachments = tuple(
        Attachment(
            id=str(a["id"]),
            content_type=str(a.get("contentType") or ""),
            filename=a.get("filename"),
            size=int(a.get("size") or 0),
        )
        for a in message.get("attachments") or []
        if a.get("id")
    )
    quote = message.get("quote") or {}
    return Incoming(
        sender_number=envelope.get("sourceNumber"),
        sender_uuid=envelope.get("sourceUuid"),
        timestamp=int(message.get("timestamp") or envelope.get("timestamp") or 0),
        group_id=(message.get("groupInfo") or {}).get("groupId"),
        text=message.get("message") or "",
        attachments=attachments,
        quote_timestamp=int(quote["id"]) if quote.get("id") else None,
    )


class SignalClient:
    def __init__(self, url: str, account: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.account = account
        self._http = http or httpx.Client(timeout=60)
        self._ids = itertools.count(1)

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        body = {"jsonrpc": "2.0", "method": method, "params": params, "id": next(self._ids)}
        try:
            response = self._http.post(f"{self.url}/api/v1/rpc", json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SignalError(f"signal-cli {method} failed: {type(exc).__name__}") from exc
        if data.get("error"):
            raise SignalError(f"signal-cli {method} error: {data['error'].get('message')}")
        return data.get("result")

    def check(self) -> bool:
        try:
            return self._http.get(f"{self.url}/api/v1/check").status_code == 200
        except httpx.HTTPError:
            return False

    def send(
        self,
        group_id: str,
        text: str,
        *,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
    ) -> int:
        """Send a group message, optionally quoting one. Returns its timestamp,
        which is how replies to it are recognized later."""
        params: dict[str, Any] = {"groupId": group_id, "message": text}
        if quote_timestamp and quote_author:
            params["quoteTimestamp"] = quote_timestamp
            params["quoteAuthor"] = quote_author
        result = self._rpc("send", params)
        return int(result["timestamp"])

    def react(self, group_id: str, emoji: str, *, author: str, timestamp: int) -> None:
        self._rpc(
            "sendReaction",
            {
                "groupId": group_id,
                "emoji": emoji,
                "targetAuthor": author,
                "targetTimestamp": timestamp,
            },
        )

    def fetch_attachment(self, attachment_id: str, group_id: str) -> bytes:
        result = self._rpc("getAttachment", {"id": attachment_id, "groupId": group_id})
        data = result.get("data") if isinstance(result, dict) else result
        if not isinstance(data, str):
            raise SignalError("signal-cli returned no attachment data")
        return base64.b64decode(data)

    def events(self, stop: threading.Event, last_event_id: str | None = None) -> Iterator[Incoming]:
        """Yield incoming messages forever, reconnecting with Last-Event-ID so
        messages that arrived while disconnected are not missed."""
        while not stop.is_set():
            headers = {"Accept": "text/event-stream"}
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id
            try:
                with self._http.stream(
                    "GET", f"{self.url}/api/v1/events", headers=headers, timeout=None
                ) as response:
                    response.raise_for_status()
                    log.info("Listening for Signal messages")
                    data_lines: list[str] = []
                    event_id = None
                    for line in response.iter_lines():
                        if stop.is_set():
                            return
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                        elif line.startswith("id:"):
                            event_id = line[3:].strip()
                        elif line == "" and data_lines:
                            raw, data_lines = "\n".join(data_lines), []
                            last_event_id = event_id or last_event_id
                            try:
                                incoming = parse_event(json.loads(raw))
                            except (ValueError, TypeError, KeyError, AttributeError):
                                log.warning("Skipping an event that could not be parsed")
                                continue
                            if incoming is not None:
                                yield incoming
            except httpx.HTTPError as exc:
                log.warning("Signal event stream dropped (%s); reconnecting", type(exc).__name__)
            stop.wait(RECONNECT_SECONDS)
