"""YNAB API client. This is the only module that talks to YNAB.

Shapes follow the OpenAPI spec at https://api.ynab.com/papi/open_api_spec.yaml
(v1.91): `/plans/...` paths, integer milliunit amounts, 200 requests per hour.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from receiptbot.models import Category

log = logging.getLogger(__name__)

BASE_URL = "https://api.ynab.com/v1"
MEMO_MAX = 500  # maxLength of `memo` on SaveTransaction and SaveSubTransaction
PAYEE_MAX = 200  # maxLength of `payee_name`
CACHE_SECONDS = 6 * 3600
_BACKOFF_SECONDS = (30, 120, 300)


class YnabError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"YNAB API error {status}: {detail}")
        self.status = status
        self.detail = detail


class RateLimited(YnabError):
    pass


class PlanMismatch(Exception):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SubTxn(_Model):
    id: str
    amount: int
    memo: str | None = None
    category_id: str | None = None
    deleted: bool = False


class Txn(_Model):
    """A transaction as returned by the API (TransactionDetail, trimmed)."""

    id: str
    date: date
    amount: int  # milliunits, outflows negative
    memo: str | None = None
    cleared: str = "uncleared"
    approved: bool = False
    account_id: str
    payee_name: str | None = None
    category_id: str | None = None
    matched_transaction_id: str | None = None
    import_id: str | None = None
    import_payee_name: str | None = None
    import_payee_name_original: str | None = None
    deleted: bool = False
    subtransactions: list[SubTxn] = Field(default_factory=list)

    @property
    def is_import(self) -> bool:
        return self.import_id is not None

    @property
    def is_matched(self) -> bool:
        return self.matched_transaction_id is not None


class Split(_Model):
    amount: int
    category_id: str | None = None
    memo: str | None = None


class NewTxn(_Model):
    """Body for creating a transaction. `import_id` is deliberately absent: a
    transaction created without one is "user-entered", which is what lets YNAB
    match it to the later bank import."""

    account_id: str
    date: date
    amount: int
    payee_name: str
    category_id: str | None = None
    memo: str | None = None
    cleared: str = "uncleared"
    approved: bool = False
    subtransactions: list[Split] = Field(default_factory=list)

    def body(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"subtransactions"})
        if self.subtransactions:
            data["category_id"] = None
            data["subtransactions"] = [s.model_dump(mode="json") for s in self.subtransactions]
        return data


class Account(_Model):
    id: str
    name: str
    type: str = ""
    on_budget: bool = True
    closed: bool = False
    deleted: bool = False


class Plan(_Model):
    id: str
    name: str


class YnabClient:
    def __init__(
        self,
        token: str,
        plan_id: str,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.plan_id = plan_id
        self._http = http or httpx.Client(timeout=40)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._sleep = sleep
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(len(_BACKOFF_SECONDS) + 1):
            try:
                response = self._http.request(
                    method, f"{BASE_URL}{path}", headers=self._headers, **kwargs
                )
            except httpx.HTTPError as exc:
                raise YnabError(0, f"could not reach YNAB ({type(exc).__name__})") from exc
            if response.status_code != 429:
                break
            if attempt == len(_BACKOFF_SECONDS):
                raise RateLimited(429, "rate limit still exceeded after backing off")
            retry_after = response.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else _BACKOFF_SECONDS[attempt]
            log.warning("YNAB rate limit hit, waiting %.0fs", wait)
            self._sleep(wait)
        if response.status_code >= 400:
            try:
                error = response.json()["error"]
                detail = f"{error.get('name')}: {error.get('detail')}"
            except (ValueError, KeyError, TypeError):
                detail = response.reason_phrase
            raise YnabError(response.status_code, detail)
        return response.json()["data"]

    def _plan(self, suffix: str) -> str:
        return f"/plans/{self.plan_id}{suffix}"

    def _cached(self, key: str, load: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = load()
        self._cache[key] = (self._clock(), value)
        return value

    # -- plans, accounts, categories ---------------------------------------

    def plans(self) -> list[Plan]:
        return [Plan.model_validate(p) for p in self._request("GET", "/plans")["plans"]]

    def check_plan(self, expected_name: str) -> None:
        """Refuse to run against anything but the configured plan."""
        plan = next((p for p in self.plans() if p.id == self.plan_id), None)
        if plan is None:
            raise PlanMismatch(f"Plan {self.plan_id} was not found for this token.")
        if plan.name != expected_name:
            raise PlanMismatch(
                f"Plan {self.plan_id} is named {plan.name!r}, but config says {expected_name!r}."
            )

    def accounts(self) -> list[Account]:
        def load() -> list[Account]:
            raw = self._request("GET", self._plan("/accounts"))["accounts"]
            accounts = [Account.model_validate(a) for a in raw]
            return [a for a in accounts if not a.deleted and not a.closed]

        return self._cached("accounts", load)

    def categories(self) -> list[Category]:
        """Categories a transaction can be assigned to, in plan order."""

        def load() -> list[Category]:
            groups = self._request("GET", self._plan("/categories"))["category_groups"]
            found = []
            for group in groups:
                if group.get("hidden") or group.get("deleted") or group.get("internal"):
                    continue
                if group.get("name") == "Credit Card Payments":
                    continue  # the API ignores these on transactions
                for cat in group.get("categories", []):
                    if cat.get("hidden") or cat.get("deleted") or cat.get("internal"):
                        continue
                    found.append(Category(id=cat["id"], name=cat["name"], group=group["name"]))
            return found

        return self._cached("categories", load)

    def create_category_group(self, name: str) -> str:
        """Create a category group and return its id."""
        data = self._request(
            "POST", self._plan("/category_groups"), json={"category_group": {"name": name[:50]}}
        )
        self._cache.pop("categories", None)
        return data["category_group"]["id"]

    def create_category(self, group_id: str, name: str) -> str:
        """Create a category in a group and return its id."""
        body = {"category": {"name": name, "category_group_id": group_id}}
        data = self._request("POST", self._plan("/categories"), json=body)
        self._cache.pop("categories", None)
        return data["category"]["id"]

    def category_groups(self) -> dict[str, str]:
        """Visible category group names mapped to ids."""
        groups = self._request("GET", self._plan("/categories"))["category_groups"]
        return {
            g["name"]: g["id"]
            for g in groups
            if not (g.get("hidden") or g.get("deleted") or g.get("internal"))
        }

    # -- transactions ------------------------------------------------------

    def create_transaction(self, txn: NewTxn) -> Txn:
        data = self._request("POST", self._plan("/transactions"), json={"transaction": txn.body()})
        return Txn.model_validate(data["transaction"])

    def get_transaction(self, transaction_id: str) -> Txn:
        data = self._request("GET", self._plan(f"/transactions/{transaction_id}"))
        return Txn.model_validate(data["transaction"])

    def update_transaction(self, transaction_id: str, changes: dict[str, Any]) -> Txn:
        data = self._request(
            "PUT", self._plan(f"/transactions/{transaction_id}"), json={"transaction": changes}
        )
        return Txn.model_validate(data["transaction"])

    def delete_transaction(self, transaction_id: str) -> None:
        self._request("DELETE", self._plan(f"/transactions/{transaction_id}"))

    def recent_transactions(self, account_id: str, since_date: date) -> list[Txn]:
        """Every transaction on the account dated on or after `since_date`."""
        return self.account_transactions(account_id, since_date=since_date)[0]

    def account_transactions(
        self,
        account_id: str,
        *,
        last_knowledge: int | None = None,
        since_date: date | None = None,
    ) -> tuple[list[Txn], int]:
        """Delta request: with `last_knowledge`, only transactions changed since
        then come back (including deleted ones). Returns the new server knowledge."""
        params: dict[str, Any] = {}
        if last_knowledge is not None:
            params["last_knowledge_of_server"] = last_knowledge
        if since_date is not None:
            params["since_date"] = since_date.isoformat()
        data = self._request(
            "GET", self._plan(f"/accounts/{account_id}/transactions"), params=params
        )
        txns = [Txn.model_validate(t) for t in data["transactions"]]
        return txns, int(data["server_knowledge"])
