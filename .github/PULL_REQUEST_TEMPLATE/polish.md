Closes #

<!-- Title: fix(<scope>): <issue title, first letter lowercased> -->

## What changes for the person using it

The difference someone in the group will notice — wording, question flow, memos and payees in YNAB, log readability.

## Change

What changed in code. Name any shared message or helper introduced or reused.

## Left untouched

<!-- Optional: issues noticed in this area but left for another issue. Delete if none. -->

## Before and after

<!-- Required. The bot's message or YNAB memo before and after. Synthetic names and amounts only. -->

```text
before: ...
after:  ...
```

## Checklist

- [ ] Messages are short, plain and say what the bot needs; a question says how to answer it
- [ ] "Failed", "nothing to do" and "waiting on you" read differently from each other
- [ ] Memos fit YNAB's 500-character limit with the order number kept
- [ ] No addresses, document text or secrets in messages or logs
- [ ] `uv run ruff check . && uv run ruff format --check .` and `uv run pytest` pass locally
