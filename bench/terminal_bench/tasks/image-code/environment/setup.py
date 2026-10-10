"""Draw a numeric code into code.png with a 5x7 bitmap font (standard library only)."""

import struct
import sys
import zlib
from pathlib import Path

CODE = "47193825"
FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}
SCALE, GAP, MARGIN = 12, 2, 3


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def main(app: Path) -> None:
    cols = MARGIN * 2 + len(CODE) * (5 + GAP) - GAP
    rows = MARGIN * 2 + 7
    grid = [[255] * cols for _ in range(rows)]
    for i, digit in enumerate(CODE):
        for y, row in enumerate(FONT[digit]):
            for x, bit in enumerate(row):
                if bit == "1":
                    grid[MARGIN + y][MARGIN + i * (5 + GAP) + x] = 0
    raw = b"".join(
        b"\x00" + bytes(v for v in row for _ in range(SCALE)) for row in grid for _ in range(SCALE)
    )
    header = struct.pack(">IIBBBBB", cols * SCALE, rows * SCALE, 8, 0, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    app.mkdir(parents=True, exist_ok=True)
    (app / "code.png").write_bytes(png)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
