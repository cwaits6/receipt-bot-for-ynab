"""Run one reconciliation pass from the command line.

Usage: uv run --env-file .env scripts/reconcile.py [--dry-run]

With --dry-run it prints what it would change and writes nothing to YNAB.
Without it, changes are applied but questions are not sent (there is no group
here); the running bot asks them on its next pass.
"""

from __future__ import annotations

import argparse
import logging

from receiptbot.main import build_pipeline
from receiptbot.reconcile import Reconciler, load_recurring
from receiptbot.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    settings = load_settings(args.config)
    pipeline = build_pipeline(settings)
    recurring = load_recurring(settings.recurring_file, pipeline.categorizer.categories)
    reconciler = Reconciler(
        settings, pipeline.ledger, pipeline.ynab, pipeline.categorizer, recurring
    )
    report = reconciler.run(dry_run=args.dry_run or settings.dry_run)
    print("\n".join(report) if report else "Nothing to do.")


if __name__ == "__main__":
    main()
