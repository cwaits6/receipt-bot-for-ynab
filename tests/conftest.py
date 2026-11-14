from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def make_pdf(lines: list[str], pages: int = 1) -> bytes:
    """Build a small valid PDF with a real text layer (synthetic content only)."""

    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = ["BT", "/F1 9 Tf", "40 780 Td"]
    for line in lines:
        ops.append(f"({escape(line)}) Tj")
        ops.append("0 -11 Td")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")

    objects: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i in range(pages):
        content_ref = 5 + 2 * i
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_ref} 0 R >>"
            ).encode()
        )
        body = stream if i == 0 else b"BT ET"
        objects.append(b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


@pytest.fixture
def pdf_factory():
    return make_pdf
