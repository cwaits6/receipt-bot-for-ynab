Closes #

<!-- Title: feat(<scope>): <issue title, first letter lowercased> -->

## What someone can do now

The new behavior, from the person's side. Who (household member in the Signal group / operator running the bot) and where (group chat, YNAB, a script).

## Change

How it's built, and why it is shaped this way.

## Left untouched

<!-- Optional: adjacent things deliberately not changed. Delete if none. -->

## Tests

What was added or run. If this changes what lands in YNAB, say whether `scripts/live_check.py --plan Test` was run.

## Example exchange

<!-- Required if the bot says something new in the group. Synthetic names and amounts only. Delete otherwise. -->

```text
member: <attachment: invoice.pdf>
bot:    ...
```

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check .` and `uv run pytest` pass locally
- [ ] Only `llm.py` calls the Claude API and only `ynab.py` calls YNAB; the model does no arithmetic, flow control or tool use
- [ ] Money stays `Decimal` until `validate.to_milliunits`
- [ ] New merchant → plain module in `src/receiptbot/parsers/`, registered in `parsers/__init__.py`, with a synthetic fixture
- [ ] New setting → added to `config.example.yaml` and the README
- [ ] No real receipts, addresses, order numbers, tokens or phone numbers in code, fixtures, logs or this PR

<!-- Ledger change — delete this block if the PR does not touch the SQLite schema. -->
## Ledger change

- [ ] New numbered migration in `ledger.py`; earlier migrations left as they are
- [ ] An existing `/data/receiptbot.sqlite3` opens and upgrades without losing rows
- [ ] Test covers upgrading from the previous `user_version`
