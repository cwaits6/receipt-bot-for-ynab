# receipt-bot-for-ynab

A small self-hosted bot for one household. Someone shares an order invoice (PDF, screenshot, or
pasted text) into a private Signal group. The bot reads it, creates the matching transaction in
YNAB with the right categories and an item-level memo, and replies with what it logged:

```
Logged $43.18 for Alex · Pets: dog food $31.99 · Household: trash bags $11.19
```

The budget is correct at the moment of purchase, days before the bank import arrives, and nobody
has to remember later what a charge was for. Amazon orders are the main use case, but nothing is
Amazon-only: any receipt or invoice works.

This is a personal-access-token tool. Each person runs their own copy with their own YNAB token
and their own Claude API key. There is no hosted version.

## How it decides

Plain code does everything it can. The same input always gives the same output.

1. **Read.** A PDF's text layer (or pasted text) goes to the merchant parsers. One ships today:
   the Amazon "Final Details for Order" invoice. A parser result is used only if its line items
   plus tax, shipping and discounts add up to the printed total within one cent.
2. **Categorize.** Each item is looked up in the learned rules table, then in the seed rules you
   wrote by hand.
3. **Validate.** All money math is `Decimal` arithmetic in code. Tax, shipping, discounts and gift
   card payments are spread across items in proportion, so split amounts sum exactly to the
   amount charged to the card.
4. **Dedupe, post, record.** An order number (or content hash) already in the ledger is not
   logged twice. Transactions are created unapproved and uncleared so you review them in YNAB.

### When the Claude API is used, and when it is not

The model is a fallback. It is called in exactly these cases:

| Case | What is sent |
| --- | --- |
| The input is an image, or a PDF with no text layer | That document |
| No parser recognizes the text, or a parser's result fails the totals check | That document |
| An item has no category rule | Only the unmatched item descriptions and your category names |

It is never used for arithmetic, flow control, shortening text, matching answers, or
reconciliation, and it has no tools. Its output is checked by the same code as everything else:
if the numbers do not add up, the bot asks instead of posting. Text inside a document that looks
like instructions is treated as data.

A reply is marked `· read by AI` when the model read the document and `· category by AI` when it
chose a category, so you know which entries to look at more closely. A model's category guess
only becomes a rule after you approve that transaction in YNAB.

Set `llm.enabled: false` and the bot never calls the model. It asks a person in the group
wherever it would have, and asks for a PDF or pasted text when it gets an image.

### Never silently wrong

If totals do not add up, a category is unknown, or anything fails, the bot replies with a short,
specific question or error. It does not guess.

## Data flow and privacy

- **Documents a parser can read never leave your machine** except as the YNAB transaction they
  become (payee, date, amount, categories, and a memo of short item names plus the order number).
- **Only documents that take the model path are sent to the Claude API**, along with item
  descriptions that have no rule. Nothing else is sent to Anthropic.
- The ledger is one SQLite file in the data volume. It stores order numbers, totals, short item
  descriptions, categories and message timestamps. It does not store raw documents or model
  responses. Shared files are kept on disk only if you set `keep_attachments: true`.
- Logs contain order numbers, totals, the reading path and outcomes. They never contain secrets,
  document text or addresses.
- The bot handles messages only from the configured group and the listed senders. Everything
  else is dropped without a reply.
- The bot does not log in to Amazon, scrape anything, or run a browser. Its only input is what a
  person shares.

## Setup

You need Docker with Compose on a machine that stays on, a YNAB account, a phone number the bot
can own on Signal (a dedicated number, not yours), and optionally a Claude API key.

### 1. Get the code and create your config files

```sh
git clone https://github.com/cwaits6/receipt-bot-for-ynab.git
cd receipt-bot-for-ynab
cp .env.example .env
cp config.example.yaml config.yaml
cp rules.example.yaml rules.yaml
cp recurring.example.yaml recurring.yaml
```

All four are git-ignored. Put your secrets in `.env`:

- `YNAB_ACCESS_TOKEN`: YNAB → Account Settings → Developer Settings → New Token.
- `ANTHROPIC_API_KEY`: from the Claude Console. Not needed if `llm.enabled: false`.
- `SIGNAL_ACCOUNT`: the bot's number in international format, for example `+15550100000`.
- `TZ`: your time zone, for example `America/New_York`, so dates match your calendar day.

### 2. Find your YNAB ids

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run --env-file .env scripts/ynab_ids.py              # lists plans
uv run --env-file .env scripts/ynab_ids.py --plan Test  # accounts and categories
```

Copy the plan id, plan name and account id into `config.yaml`. On startup the bot fetches the
plan and refuses to run if its name is not exactly `ynab.plan_name`. Start with a plan named
`Test` and `dry_run: true`.

### 3. Register the bot's Signal number (once)

```sh
docker compose run --rm signal-cli -a +15550100000 register
docker compose run --rm signal-cli -a +15550100000 verify 123456
docker compose run --rm signal-cli -a +15550100000 updateProfile --given-name "Receipt Bot"
```

If `register` reports that a captcha is required, follow the link it prints, solve it, and pass
the token with `--captcha`. See the
[signal-cli documentation](https://github.com/AsamK/signal-cli) for details.

Then put the bot in your group. In the Signal app, open the group, enable its invite link, and
copy it:

```sh
docker compose run --rm signal-cli -a +15550100000 joinGroup --uri "https://signal.group/#..."
docker compose run --rm signal-cli -a +15550100000 listGroups -d
```

Copy the group's base64 id into `signal.group_id`, and list each household member under
`signal.allowed_senders` with their phone number (or Signal account UUID) and a display name.
You can turn the invite link off again afterwards.

The official signal-cli image is x86_64 only. On arm64, switch the image in `docker-compose.yml`
to the community build noted in the comment at the top of that file.

### 4. Start it

```sh
docker compose up -d --build
docker compose logs -f receiptbot
```

Share an invoice in the group. In dry-run mode the reply ends with
`DRY RUN, nothing written to YNAB`. When you are happy, point `config.yaml` at your real plan and
set `dry_run: false`.

The bot container runs as a non-root user with a read-only root filesystem. Its only writable
path is the `bot-data` volume. The signal-cli daemon publishes no ports.

## Using it

- **Log an order:** share the invoice PDF, a screenshot, or paste the order text into the group.
  For Amazon, the printable invoice ("View invoice" on the order) saved as a PDF is read entirely
  by code.
- **`undo`:** reply `undo` to one of the bot's "Logged" messages to delete that transaction.
- **Questions:** when the bot asks something ("Which category is ...?"), reply to that message
  with the answer. Category names are matched with plain fuzzy matching. Reply `cancel` to drop
  the order instead.
- **`help`:** lists the above.

Anything else in the group is ignored.

### Categories get more automatic over time

A category becomes a rule whenever a person confirms it: by answering in the group, or by
approving the transaction in YNAB. If you change the category in YNAB before approving, your
choice replaces the rule. Manage rules by hand with:

```sh
uv run --env-file .env scripts/rules.py list
uv run --env-file .env scripts/rules.py add "dog food" "Pets"
uv run --env-file .env scripts/rules.py remove "dog food"
```

`rules.yaml` holds keyword rules you write yourself, for example `dog food: Pets`.

### Reconciliation

Every 30 minutes (configurable) the bot compares its ledger with the account in YNAB. It never
calls the model for this.

- A placeholder that YNAB matched to the bank import is marked done.
- Approved transactions teach category rules.
- Amazon often charges one order as several shipments, which YNAB cannot match on its own. When
  exactly one set of unapproved bank imports adds up to an open order, the bot copies the
  categories and memo onto those imports, leaves them unapproved, and deletes its placeholder.
  If more than one set could explain the order, it leaves everything alone.
- A bank import from a known merchant with no receipt after two days gets one question:
  `Amazon $14.07 on Nov 12 has no receipt. What was it?` Your reply becomes the memo.
- Charges listed in `recurring.yaml` are filled in automatically, with a one-line notice. If you
  give the same answer twice for similar amounts, the bot offers to make it recurring.

The bot never touches a transaction it did not create unless it is an unapproved import with an
empty memo, and never modifies reconciled transactions.

Preview a pass without changing anything:

```sh
uv run --env-file .env scripts/reconcile.py --dry-run
```

### Other scripts

```sh
uv run --env-file .env scripts/log_invoice.py invoice.pdf --dry-run   # one file, no Signal
uv run --env-file .env scripts/live_check.py --plan Test              # opt-in live YNAB check
```

## Costs

- **YNAB API:** free with your subscription. The limit is 200 requests per hour per token; the
  bot caches categories and accounts and uses delta requests, so a normal day is a few dozen.
- **Claude API:** only the fallback cases above cost anything. With the default small model, a
  screenshot or a category lookup is a fraction of a cent. Check current pricing at
  <https://www.anthropic.com/pricing>. With `llm.enabled: false` the cost is zero.
- **Signal:** free. You need a phone number that can receive one verification SMS or call.

## Limits

- One parser ships: the Amazon "Final Details for Order" invoice layout. Other Amazon page
  layouts and other merchants take the model path (or a question, with the model off) until
  someone adds a parser.
- One currency per plan. Refunds and orders with a zero or negative card total are not logged.
- One transaction per shared document. A document showing several orders is not split up.
- Email, logging in to merchants, and any UI beyond Signal messages are out of scope.
- Files over 10 MB or PDFs over 15 pages are refused (configurable).

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Tests never touch the network: YNAB is mocked from the OpenAPI spec's shapes and model outputs
are replayed from recorded fixtures. Fixtures are synthetic. Do not commit real receipts,
addresses, order numbers, tokens or phone numbers; keep real samples in the git-ignored
`samples/` directory.

To add a merchant, write a module in `src/receiptbot/parsers/` with `name`, `matches(text)` and
`parse(text)` (see `parsers/base.py`), register it in `parsers/__init__.py`, and add synthetic
fixtures that copy the layout with fake data.

`PROJECT_BRIEF.md` describes the design in full.

## Disclaimers

We are not affiliated, associated, or in any way officially connected with YNAB or any of its
subsidiaries or affiliates. The official YNAB website can be found at <https://www.ynab.com>.

The names YNAB and You Need A Budget, as well as related names, tradenames, marks, trademarks,
emblems, and images are registered trademarks of YNAB.

This project does not access Amazon in any way and is not affiliated with, endorsed by, or
connected to Amazon, Signal, or Anthropic. All product names and trademarks belong to their
respective owners.

This software creates and changes transactions in your budget. Review what it logs. It is
provided as is, without warranty of any kind; see the license.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
