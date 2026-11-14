Closes #

<!-- Title: fix(<scope>): <issue title, first letter lowercased> -->

## What was wrong

What the person saw in the group or in YNAB, and how to reproduce it.

## Root cause

Why it happened — the actual defect, not the symptom.

## Fix

What changed, and why this is the right place to fix it.

## Left untouched

<!-- Optional: related problems noticed but not fixed here, with an issue if one was opened. Delete if none. -->

## Tests

- [ ] A test that fails before this change and passes after it: `<path>`
- [ ] Other suites run:

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check .` and `uv run pytest` pass locally
- [ ] Money stays `Decimal` until `validate.to_milliunits`
- [ ] When the bot is unsure it still asks in the group instead of guessing
- [ ] Fixtures are synthetic; no real receipts, addresses, order numbers, tokens or phone numbers

<!-- Ledger change — delete this block if the PR does not touch the SQLite schema. -->
## Ledger change

- [ ] New numbered migration in `ledger.py`; earlier migrations left as they are
- [ ] An existing `/data/receiptbot.sqlite3` opens and upgrades without losing rows
- [ ] Test covers upgrading from the previous `user_version`
