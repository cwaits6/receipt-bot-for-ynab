"""Print YNAB plans, accounts and categories with their ids.

Usage: uv run --env-file .env scripts/ynab_ids.py [--plan NAME_OR_ID]

Without --plan it lists your plans. With --plan it lists that plan's accounts
and categories, ready to paste into config.yaml.
"""

from __future__ import annotations

import argparse

from receiptbot.settings import require_env
from receiptbot.ynab import YnabClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", help="plan name or id to show accounts and categories for")
    args = parser.parse_args()

    token = require_env("YNAB_ACCESS_TOKEN")
    plans = YnabClient(token, "").plans()
    print("Plans:")
    for plan in plans:
        print(f"  {plan.id}  {plan.name}")
    if not args.plan:
        print("\nRe-run with --plan <name or id> to list accounts and categories.")
        return

    matches = [p for p in plans if args.plan in (p.id, p.name)]
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one plan matching {args.plan!r}, found {len(matches)}.")
    client = YnabClient(token, matches[0].id)
    print(f"\nAccounts in {matches[0].name}:")
    for account in client.accounts():
        print(f"  {account.id}  {account.name} ({account.type})")
    print(f"\nCategories in {matches[0].name}:")
    for category in client.categories():
        print(f"  {category.id}  {category.group} / {category.name}")


if __name__ == "__main__":
    main()
