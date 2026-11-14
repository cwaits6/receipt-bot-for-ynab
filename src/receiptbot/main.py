"""Entry point: wire everything together and run the bot."""

from __future__ import annotations

import logging
import signal as os_signal
import threading

from receiptbot.categorize import Categorizer, load_seed_rules
from receiptbot.ledger import Ledger
from receiptbot.pipeline import Pipeline
from receiptbot.settings import ConfigError, Settings, load_settings, require_env
from receiptbot.ynab import PlanMismatch, YnabClient

log = logging.getLogger("receiptbot")


def build_pipeline(settings: Settings) -> Pipeline:
    """Build the pipeline from config and environment, after the plan-name check."""
    ynab = YnabClient(require_env("YNAB_ACCESS_TOKEN"), settings.ynab.plan_id)
    ynab.check_plan(settings.ynab.plan_name)

    llm = None
    if settings.llm.enabled:
        from receiptbot.llm import LlmClient

        require_env("ANTHROPIC_API_KEY")
        llm = LlmClient(settings.llm.model)

    ledger = Ledger(settings.db_path)
    categorizer = Categorizer(
        ledger,
        ynab.categories,
        seed_rules=load_seed_rules(settings.rules_file, ynab.categories()),
        llm=llm,
        min_confidence=settings.llm.min_confidence,
    )
    return Pipeline(settings, ledger, ynab, categorizer, llm=llm)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs are noise
    try:
        settings = load_settings()
        if settings.signal is None:
            raise ConfigError("config.yaml needs a signal section to run the bot.")
        account = require_env("SIGNAL_ACCOUNT")
        pipeline = build_pipeline(settings)
    except (ConfigError, PlanMismatch) as exc:
        raise SystemExit(f"Refusing to start: {exc}") from exc

    from receiptbot.handlers import Bot
    from receiptbot.reconcile import Reconciler, load_recurring
    from receiptbot.signal import SignalClient

    transport = SignalClient(settings.signal.url, account)
    recurring = load_recurring(settings.recurring_file, pipeline.categorizer.categories)
    reconciler = Reconciler(
        settings, pipeline.ledger, pipeline.ynab, pipeline.categorizer, recurring
    )
    bot = Bot(settings, pipeline, transport, reconciler)

    stop = threading.Event()
    os_signal.signal(os_signal.SIGTERM, lambda *_: stop.set())
    os_signal.signal(os_signal.SIGINT, lambda *_: stop.set())
    log.info(
        "Starting: plan %r, dry_run=%s, llm=%s",
        settings.ynab.plan_name,
        settings.dry_run,
        settings.llm.model if settings.llm.enabled else "off",
    )
    bot.run(stop)


if __name__ == "__main__":
    main()
