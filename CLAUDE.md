# receipt-bot-for-ynab

`PROJECT_BRIEF.md` is the source of truth for scope and design. Read it first.

## Commands

- `uv sync` installs everything.
- `uv run pytest` runs the tests. They never touch the network.
- `uv run ruff check . && uv run ruff format --check .` lints. Run before every commit.
- `uv run --env-file .env scripts/log_invoice.py <file> --dry-run` processes one document.
- `uv run --env-file .env scripts/reconcile.py --dry-run` prints what reconciliation would change.
- `uv run --env-file .env scripts/live_check.py --plan Test` runs the opt-in live YNAB check.

## Conventions

- Deterministic code first. `llm.py` is the only module that calls the Claude API and
  `ynab.py` is the only module that calls YNAB. The model never does arithmetic, controls
  flow, or gets tools.
- Money is `Decimal` everywhere until `validate.to_milliunits`. No floats.
- Never commit real receipts, addresses, order numbers, tokens or phone numbers.
  Fixtures under `tests/fixtures/` are synthetic. Real samples live in git-ignored `samples/`.
- Never log secrets, document text or addresses.
- When unsure, the bot asks in the group. It does not guess.
- New merchants are plain modules in `src/receiptbot/parsers/` registered in `parsers/__init__.py`.
- No web framework, queue, ORM, agent framework or database server.
- Commits and PR titles use Conventional Commits (`feat:`, `fix:`, `chore:` ...). release-please
  reads them to open the Release PR and write `CHANGELOG.md`. Do not edit the version in
  `pyproject.toml` or `uv.lock` by hand.
- CI calls the shared workflows in `krypsis-io/.github` (trivy, semgrep, dependency review,
  release-please, Buildah container build). Change behaviour there, not by copying them here.
