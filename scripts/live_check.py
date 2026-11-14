"""Opt-in live check against a real YNAB plan named "Test". Never run in CI.

Usage: uv run --env-file .env scripts/live_check.py --plan Test [--keep]

Creates a $0.01 unapproved, uncleared, user-entered transaction, reads it back,
then deletes it. With --keep the transaction is left in place so you can check
two things in the YNAB app that the API docs do not state:

  1. the unapproved transaction counts toward its category's balance, and
  2. a later bank import of the same amount (within 10 days) matches it.

Refuses any plan whose name is not exactly "Test".
"""

from __future__ import annotations

import argparse
from datetime import date

from receiptbot.settings import load_settings, require_env
from receiptbot.ynab import NewTxn, YnabClient

REQUIRED_PLAN = "Test"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", required=True, help='must be "Test"')
    parser.add_argument("--keep", action="store_true", help="leave the transaction in place")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    if args.plan != REQUIRED_PLAN:
        raise SystemExit(f'Live checks only run against a plan named "{REQUIRED_PLAN}".')

    settings = load_settings(args.config)
    if settings.ynab.plan_name != REQUIRED_PLAN:
        raise SystemExit(f'config.yaml ynab.plan_name must be "{REQUIRED_PLAN}" for live checks.')
    client = YnabClient(require_env("YNAB_ACCESS_TOKEN"), settings.ynab.plan_id)
    client.check_plan(REQUIRED_PLAN)
    print(f'Plan check passed: {settings.ynab.plan_id} is named "{REQUIRED_PLAN}".')

    category = client.categories()[0]
    created = client.create_transaction(
        NewTxn(
            account_id=settings.ynab.default_account_id,
            date=date.today(),
            amount=-10,
            payee_name="receiptbot live check",
            category_id=category.id,
            memo="receiptbot live check, safe to delete",
        )
    )
    print(f"Created {created.id}: amount={created.amount} approved={created.approved}")
    print(f"  cleared={created.cleared} import_id={created.import_id} category={category.name}")
    assert created.amount == -10 and not created.approved and created.import_id is None

    if args.keep:
        print("Left in place (--keep). Delete it in YNAB when you are done looking.")
        return
    client.delete_transaction(created.id)
    print("Deleted. Live check passed.")


if __name__ == "__main__":
    main()
