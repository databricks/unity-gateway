"""Write invoice.pdf with a Flate-compressed text stream (standard library only)."""

import sys
import zlib
from pathlib import Path

LINES = [
    "Northwind Supplies GmbH",
    "Invoice INV-2026-0419",
    "Consulting hours: 38",
    "Subtotal: 6,169.91 EUR",
    "VAT (19%): 1,172.28 EUR",
    "Total due: 7,342.19 EUR",
]


def main(app: Path) -> None:
    text = "BT /F1 14 Tf 72 720 Td 20 TL " + " ".join(f"({line}) Tj T*" for line in LINES) + " ET"
    stream = zlib.compress(text.encode())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    app.mkdir(parents=True, exist_ok=True)
    (app / "invoice.pdf").write_bytes(bytes(pdf))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
