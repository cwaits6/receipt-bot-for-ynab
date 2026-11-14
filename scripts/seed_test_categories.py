"""Create a realistic set of household categories in a TEST plan.

Usage: uv run --env-file .env scripts/seed_test_categories.py --plan <plan name>

For trying out how the bot sorts items. The plan name must be typed out and
must match ynab.plan_name in config.yaml. Groups and categories that already
exist are left alone, so it is safe to run twice. Do not point this at a real
budget: the YNAB API cannot delete categories.
"""

from __future__ import annotations

import argparse

from receiptbot.settings import load_settings, require_env
from receiptbot.ynab import YnabClient

CATEGORIES: dict[str, list[str]] = {
    "Everyday": [
        "Groceries",
        "Household Supplies",
        "Pets",
        "Personal Care",
        "Baby & Kids",
        "Clothing",
    ],
    "Home": [
        "Home Maintenance",
        "Furniture & Decor",
        "Kitchen",
        "Garden & Outdoor",
        "Tools & Hardware",
    ],
    "Fun": ["Hobbies", "Books", "Games & Toys", "Electronics", "Gifts", "Entertainment"],
    "Health": ["Medical & Pharmacy", "Fitness"],
    "Work & School": ["Office Supplies", "Business Expenses"],
    "Auto": ["Car Maintenance"],
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", required=True, help="name of the test plan, typed out")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    settings = load_settings(args.config)
    if args.plan != settings.ynab.plan_name:
        raise SystemExit(f"--plan must match ynab.plan_name ({settings.ynab.plan_name!r}).")
    client = YnabClient(require_env("YNAB_ACCESS_TOKEN"), settings.ynab.plan_id)
    client.check_plan(args.plan)

    groups = client.category_groups()
    existing = {(c.group, c.name) for c in client.categories()}
    created = 0
    for group, names in CATEGORIES.items():
        group_id = groups.get(group) or client.create_category_group(group)
        for name in names:
            if (group, name) in existing:
                continue
            client.create_category(group_id, name)
            created += 1
            print(f"created {group} / {name}")
    print(f"{created} categories created in {args.plan}.")


if __name__ == "__main__":
    main()
