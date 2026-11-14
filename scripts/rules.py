"""List, add and remove learned category rules.

Usage:
  uv run --env-file .env scripts/rules.py list [--offline]
  uv run --env-file .env scripts/rules.py add "dog food" "Pets"
  uv run --env-file .env scripts/rules.py remove "dog food"

`add` and `remove` normalize the item text the same way the bot does.
"""

from __future__ import annotations

import argparse

from receiptbot.categorize import match_category, normalize_key
from receiptbot.ledger import Ledger
from receiptbot.settings import load_settings, require_env
from receiptbot.ynab import YnabClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--offline", action="store_true", help="show ids, do not call YNAB")
    adding = sub.add_parser("add")
    adding.add_argument("item")
    adding.add_argument("category")
    removing = sub.add_parser("remove")
    removing.add_argument("item")
    args = parser.parse_args()

    settings = load_settings(args.config)
    ledger = Ledger(settings.db_path)

    def categories():
        client = YnabClient(require_env("YNAB_ACCESS_TOKEN"), settings.ynab.plan_id)
        client.check_plan(settings.ynab.plan_name)
        return client.categories()

    if args.command == "list":
        names = {} if args.offline else {c.id: c.name for c in categories()}
        rules = ledger.rules()
        for rule in rules:
            name = names.get(rule.category_id, rule.category_id)
            print(f"{rule.normalized_key:40}  {name:28}  {rule.origin:8}  hits={rule.hit_count}")
        print(f"{len(rules)} rule(s)")
    elif args.command == "add":
        category = match_category(args.category, categories())
        if category is None:
            raise SystemExit(f"No single category matches {args.category!r}.")
        key = normalize_key(args.item)
        ledger.upsert_rule(key, category.id, "manual")
        print(f"{key!r} -> {category.name}")
    else:
        key = normalize_key(args.item)
        removed = ledger.delete_rule(key) or ledger.delete_rule(args.item)
        print(f"Removed {key!r}." if removed else f"No rule for {key!r}.")


if __name__ == "__main__":
    main()
