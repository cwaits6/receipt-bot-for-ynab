Closes #

<!-- Title: <type>(<scope>): <issue title, first letter lowercased>
     Bug, feature, or polish? Use that template instead: add
     ?template=bug.md, ?template=feature.md or ?template=polish.md to the compare URL. -->

## Change

What changed and why it is shaped this way.

## Left untouched

<!-- Optional: adjacent things deliberately not changed. Delete if none. -->

## Tests

What was added or run.

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check .` and `uv run pytest` pass locally
- [ ] Only `llm.py` calls the Claude API and only `ynab.py` calls YNAB
- [ ] Money stays `Decimal` until `validate.to_milliunits`
- [ ] No real receipts, addresses, order numbers, tokens or phone numbers in code, fixtures, logs or this PR

<!-- Ledger change — delete this block if the PR does not touch the SQLite schema. -->
## Ledger change

- [ ] New numbered migration in `ledger.py`; earlier migrations left as they are
- [ ] An existing `/data/receiptbot.sqlite3` opens and upgrades without losing rows
- [ ] Test covers upgrading from the previous `user_version`
