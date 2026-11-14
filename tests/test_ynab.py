"""YNAB client tests against response shapes from the OpenAPI spec. No network."""

import json
from datetime import date

import httpx
import pytest

from receiptbot.ynab import NewTxn, PlanMismatch, RateLimited, Split, YnabClient, YnabError

PLAN = "11111111-1111-4111-8111-111111111111"
ACCOUNT = "22222222-2222-4222-8222-222222222222"


def txn_json(**over):
    base = {
        "id": "t-1",
        "date": "2026-11-03",
        "amount": -43180,
        "memo": "dog food [111-0000000-0000000]",
        "cleared": "uncleared",
        "approved": False,
        "flag_color": None,
        "account_id": ACCOUNT,
        "account_name": "Card",
        "payee_id": "p-1",
        "payee_name": "Amazon",
        "category_id": "c-pets",
        "category_name": "Pets",
        "transfer_account_id": None,
        "transfer_transaction_id": None,
        "matched_transaction_id": None,
        "import_id": None,
        "import_payee_name": None,
        "import_payee_name_original": None,
        "debt_transaction_type": None,
        "deleted": False,
        "subtransactions": [],
    }
    return base | over


def make_client(handler, sleeps=None):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    record = sleeps if sleeps is not None else []
    return YnabClient("test-token", PLAN, http=http, sleep=record.append)


def test_check_plan_accepts_matching_name_and_sends_bearer_token():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": {"plans": [{"id": PLAN, "name": "Test"}]}})

    make_client(handler).check_plan("Test")
    assert seen == {"auth": "Bearer test-token", "path": "/v1/plans"}


@pytest.mark.parametrize(
    "plans", [[{"id": PLAN, "name": "Real Money"}], [{"id": "other", "name": "Test"}]]
)
def test_check_plan_refuses_wrong_plan(plans):
    client = make_client(lambda r: httpx.Response(200, json={"data": {"plans": plans}}))
    with pytest.raises(PlanMismatch):
        client.check_plan("Test")


def test_create_single_category_transaction_has_no_import_id():
    bodies = []

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == f"/v1/plans/{PLAN}/transactions"
        bodies.append(json.loads(request.content))
        data = {"transaction_ids": ["t-1"], "transaction": txn_json(), "server_knowledge": 5}
        return httpx.Response(201, json={"data": data})

    new = NewTxn(
        account_id=ACCOUNT,
        date=date(2026, 11, 3),
        amount=-43180,
        payee_name="Amazon",
        category_id="c-pets",
        memo="dog food [111-0000000-0000000]",
    )
    created = make_client(handler).create_transaction(new)
    assert created.id == "t-1"
    sent = bodies[0]["transaction"]
    assert "import_id" not in sent
    assert "subtransactions" not in sent
    assert sent["approved"] is False
    assert sent["cleared"] == "uncleared"
    assert sent["amount"] == -43180
    assert sent["date"] == "2026-11-03"


def test_create_split_transaction_sends_subtransactions_and_null_category():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        subs = [
            {"id": "s-1", "transaction_id": "t-1", "amount": -31990, "category_id": "c-pets",
             "memo": "dog food", "deleted": False},
            {"id": "s-2", "transaction_id": "t-1", "amount": -11190, "category_id": "c-house",
             "memo": "trash bags", "deleted": False},
        ]  # fmt: skip
        data = {
            "transaction_ids": ["t-1"],
            "transaction": txn_json(category_id=None, subtransactions=subs),
            "server_knowledge": 6,
        }
        return httpx.Response(201, json={"data": data})

    new = NewTxn(
        account_id=ACCOUNT,
        date=date(2026, 11, 3),
        amount=-43180,
        payee_name="Amazon",
        subtransactions=[
            Split(amount=-31990, category_id="c-pets", memo="dog food"),
            Split(amount=-11190, category_id="c-house", memo="trash bags"),
        ],
    )
    created = make_client(handler).create_transaction(new)
    sent = bodies[0]["transaction"]
    assert sent["category_id"] is None
    assert [s["amount"] for s in sent["subtransactions"]] == [-31990, -11190]
    assert sum(s["amount"] for s in sent["subtransactions"]) == sent["amount"]
    assert [s.id for s in created.subtransactions] == ["s-1", "s-2"]


def test_delete_transaction():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        data = {"transaction": txn_json(deleted=True), "server_knowledge": 7}
        return httpx.Response(200, json={"data": data})

    make_client(handler).delete_transaction("t-1")
    assert calls == [("DELETE", f"/v1/plans/{PLAN}/transactions/t-1")]


def test_429_backs_off_then_succeeds():
    responses = [
        httpx.Response(429, json={"error": {"id": "429", "name": "too_many_requests"}}),
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"id": "429"}}),
        httpx.Response(200, json={"data": {"plans": []}}),
    ]
    sleeps = []
    client = make_client(lambda r: responses.pop(0), sleeps)
    assert client.plans() == []
    assert sleeps == [30, 7.0]


def test_429_gives_up_after_bounded_retries():
    sleeps = []
    client = make_client(lambda r: httpx.Response(429, json={"error": {"id": "429"}}), sleeps)
    with pytest.raises(RateLimited):
        client.plans()
    assert len(sleeps) == 3


def test_error_response_is_raised_with_detail():
    error = {"error": {"id": "404.2", "name": "resource_not_found", "detail": "nope"}}
    client = make_client(lambda r: httpx.Response(404, json=error))
    with pytest.raises(YnabError) as excinfo:
        client.get_transaction("missing")
    assert excinfo.value.status == 404
    assert "nope" in str(excinfo.value)


def test_categories_are_cached_and_skip_hidden_deleted_and_card_payments():
    hits = []
    groups = [
        {"id": "g1", "name": "Everyday", "hidden": False, "deleted": False, "categories": [
            {"id": "c-pets", "name": "Pets", "hidden": False, "deleted": False},
            {"id": "c-old", "name": "Old", "hidden": True, "deleted": False},
            {"id": "c-gone", "name": "Gone", "hidden": False, "deleted": True},
        ]},
        {"id": "g2", "name": "Credit Card Payments", "hidden": False, "deleted": False,
         "categories": [{"id": "c-cc", "name": "Card", "hidden": False, "deleted": False}]},
        {"id": "g3", "name": "Internal Master Category", "hidden": False, "deleted": False,
         "internal": True,
         "categories": [{"id": "c-rta", "name": "Inflow: Ready to Assign", "hidden": False,
                         "deleted": False, "internal": True}]},
    ]  # fmt: skip

    def handler(request):
        hits.append(request.url.path)
        data = {"category_groups": groups, "server_knowledge": 1}
        return httpx.Response(200, json={"data": data})

    client = make_client(handler)
    first = client.categories()
    assert [(c.id, c.name, c.group) for c in first] == [("c-pets", "Pets", "Everyday")]
    assert client.categories() is first
    assert len(hits) == 1


def test_account_transactions_delta_request():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        txns = [txn_json(id="t-9", import_id="YNAB:-43180:2026-11-05:1", deleted=False)]
        return httpx.Response(200, json={"data": {"transactions": txns, "server_knowledge": 42}})

    txns, knowledge = make_client(handler).account_transactions(ACCOUNT, last_knowledge=40)
    assert seen["path"] == f"/v1/plans/{PLAN}/accounts/{ACCOUNT}/transactions"
    assert seen["params"] == {"last_knowledge_of_server": "40"}
    assert knowledge == 42
    assert txns[0].is_import and not txns[0].is_matched
