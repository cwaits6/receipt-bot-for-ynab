"""Extract the text layer of a PDF. Nothing in the file is rendered or executed."""

from __future__ import annotations

import io
import logging

from pypdf import PdfReader
from pypdf.errors import PyPdfError

log = logging.getLogger(__name__)

MIN_USABLE_CHARS = 40


class PdfError(Exception):
    """The file could not be read as a PDF, or breaks a limit."""


def extract_text(data: bytes, *, max_pages: int) -> str:
    """Return the PDF's text, or "" when it has no usable text layer (a scan)."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PdfError("the PDF is password protected")
        pages = len(reader.pages)
        if pages > max_pages:
            raise PdfError(f"the PDF has {pages} pages; the limit is {max_pages}")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except PdfError:
        raise
    except (PyPdfError, ValueError, KeyError, RecursionError, OSError) as exc:
        raise PdfError("the file could not be read as a PDF") from exc
    return text if has_usable_text(text) else ""


def has_usable_text(text: str) -> bool:
    return sum(1 for ch in text if not ch.isspace()) >= MIN_USABLE_CHARS
