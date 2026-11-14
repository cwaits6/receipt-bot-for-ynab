import json
import threading

import httpx

from receiptbot.signal import SignalClient, parse_event

GROUP = "GROUP="


def envelope(**message):
    data = {"timestamp": 1700000000000, "message": "hi", "groupInfo": {"groupId": GROUP}}
    return {
        "envelope": {
            "source": "+15550100001",
            "sourceNumber": "+15550100001",
            "sourceUuid": "11111111-1111-4111-8111-111111111111",
            "timestamp": 1700000000000,
            "dataMessage": data | message,
        },
        "account": "+15550100000",
    }


def test_parse_plain_group_message():
    msg = parse_event(envelope())
    assert (msg.sender, msg.group_id, msg.text, msg.timestamp) == (
        "+15550100001",
        GROUP,
        "hi",
        1700000000000,
    )
    assert msg.attachments == () and msg.quote_timestamp is None


def test_parse_attachment_and_quote():
    msg = parse_event(
        envelope(
            message=None,
            attachments=[
                {"contentType": "application/pdf", "filename": "inv.pdf", "id": "abc", "size": 99}
            ],
            quote={"id": 1699999999999, "author": "+15550100000", "text": "Logged"},
        )
    )
    assert msg.text == ""
    assert msg.attachments[0].content_type == "application/pdf"
    assert (msg.attachments[0].id, msg.attachments[0].size) == ("abc", 99)
    assert msg.quote_timestamp == 1699999999999


def test_parse_accepts_jsonrpc_notification_wrapper():
    wrapped = {"jsonrpc": "2.0", "method": "receive", "params": envelope()}
    assert parse_event(wrapped).text == "hi"


def test_non_messages_are_ignored():
    assert parse_event({"envelope": {"receiptMessage": {"isDelivery": True}}}) is None
    assert parse_event({"envelope": {"typingMessage": {"action": "STARTED"}}}) is None
    assert parse_event(envelope(reaction={"emoji": "x"})) is None
    assert parse_event({}) is None


def make_client(handler):
    return SignalClient("http://signal:8080/", "+15550100000", http=httpx.Client(
        transport=httpx.MockTransport(handler)))  # fmt: skip


def test_send_with_quote_uses_documented_rpc_shape():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"timestamp": 999}, "id": 1})

    client = make_client(handler)
    stamp = client.send(
        GROUP, "Logged", quote_timestamp=5, quote_author="+15550100001", quote_text="here"
    )
    assert stamp == 999
    path, body = seen[0]
    assert path == "/api/v1/rpc"
    assert body["method"] == "send"
    assert body["params"] == {
        "groupId": GROUP,
        "message": "Logged",
        "quoteTimestamp": 5,
        "quoteAuthor": "+15550100001",
        "quoteMessage": "here",
    }
    client.send(GROUP, "x", quote_timestamp=5, quote_author="+1", quote_attachments=("image/png",))
    assert seen[1][1]["params"]["quoteAttachments"] == ["image/png"]
    client.send(GROUP, "x", quote_timestamp=5, quote_author="+1")  # nothing to quote with
    assert "quoteTimestamp" not in seen[2][1]["params"]


def test_react_and_fetch_attachment():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        result = {"data": "aGVsbG8="} if body["method"] == "getAttachment" else {"timestamp": 1}
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": body["id"]})

    client = make_client(handler)
    client.react(GROUP, "\U0001f440", author="+15550100001", timestamp=5)
    assert client.fetch_attachment("abc", GROUP) == b"hello"
    assert bodies[0]["method"] == "sendReaction"
    assert bodies[0]["params"]["targetTimestamp"] == 5
    assert bodies[1]["params"] == {"id": "abc", "groupId": GROUP}


def test_event_stream_yields_messages_and_skips_noise():
    stop = threading.Event()
    events = [
        ": keep-alive\n\n",
        f"id: 1\nevent: receive\ndata: {json.dumps(envelope(message='one'))}\n\n",
        'id: 2\nevent: receive\ndata: {"envelope": {"typingMessage": {}}}\n\n',
        "id: 3\nevent: receive\ndata: not json\n\n",
        f"id: 4\nevent: receive\ndata: {json.dumps(envelope(message='two'))}\n\n",
    ]
    headers_seen = []

    def handler(request):
        headers_seen.append(request.headers.get("Last-Event-ID"))
        assert request.url.path == "/api/v1/events"
        return httpx.Response(200, text="".join(events))

    client = make_client(handler)
    got = []
    for msg in client.events(stop, last_event_id="0"):
        got.append(msg.text)
        if len(got) == 2:
            stop.set()
    assert got == ["one", "two"]
    assert headers_seen == ["0"]


def test_own_message_from_linked_account_arrives_as_sync_and_is_parsed():
    """Linked-device mode: the account owner's phone messages are sync copies."""
    sent = {
        "destination": None,
        "timestamp": 1700000005000,
        "message": "",
        "groupInfo": {"groupId": GROUP, "type": "DELIVER"},
        "attachments": [{"contentType": "image/png", "id": "shot", "size": 10}],
    }
    event = {
        "envelope": {
            "source": "+15550100001",
            "sourceNumber": "+15550100001",
            "sourceUuid": "11111111-1111-4111-8111-111111111111",
            "timestamp": 1700000005000,
            "syncMessage": {"sentMessage": sent},
        }
    }
    msg = parse_event(event)
    assert (msg.sender, msg.group_id, msg.timestamp) == ("+15550100001", GROUP, 1700000005000)
    assert msg.attachments[0].id == "shot"


def test_other_sync_messages_are_ignored():
    read_sync = {"envelope": {"syncMessage": {"readMessages": [{"timestamp": 1}]}}}
    assert parse_event(read_sync) is None
    reaction = {"envelope": {"syncMessage": {"sentMessage": {"reaction": {"emoji": "x"}}}}}
    assert parse_event(reaction) is None
