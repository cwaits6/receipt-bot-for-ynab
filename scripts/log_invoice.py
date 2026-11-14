"""Process one receipt file end to end from the command line.

Usage: uv run --env-file .env scripts/log_invoice.py <file> [--dry-run] [--sender NAME]

Accepts a PDF, an image, or a text file. Questions the bot would ask in the
group are asked on the terminal. With --dry-run nothing is written to YNAB.
"""

from __future__ import annotations

import argparse
import logging
import mimetypes
from datetime import date
from pathlib import Path

from receiptbot.main import build_pipeline
from receiptbot.models import Document
from receiptbot.settings import load_settings


def load_document(path: Path) -> Document:
    media_type = mimetypes.guess_type(path.name)[0] or ""
    data = path.read_bytes()
    if media_type == "application/pdf" or data.startswith(b"%PDF"):
        return Document(kind="pdf", data=data, media_type="application/pdf")
    if media_type.startswith("image/"):
        return Document(kind="image", data=data, media_type=media_type)
    return Document(kind="text", text=data.decode("utf-8", errors="replace"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="do not write to YNAB")
    parser.add_argument("--sender", default="CLI")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = load_settings(args.config)
    if args.dry_run:
        settings.dry_run = True
    pipeline = build_pipeline(settings)

    outcome = pipeline.process(load_document(args.file), sender=args.sender, today=date.today())
    fake_timestamp = 0
    while True:
        print(outcome.reply)
        if outcome.question is None:
            break
        fake_timestamp += 1
        pipeline.sent(outcome, fake_timestamp)
        answer = input("> ").strip()
        question = pipeline.ledger.open_question(fake_timestamp)
        if answer.lower() in ("cancel", "undo", ""):
            outcome = pipeline.cancel(question)
        else:
            outcome = pipeline.answer(question, answer)


if __name__ == "__main__":
    main()
