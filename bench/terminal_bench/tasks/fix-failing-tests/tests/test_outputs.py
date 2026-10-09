"""Verify the inventory fixes. Runs from Harbor (/app) or the native runner (TASK_APP_DIR)."""

import hashlib
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))
VISIBLE_TESTS_SHA256 = "d169ead570f87b1aa2eb351c1c226f42bea4b438e5e24720a98df3a5beb96002"


def main() -> None:
    tests = (APP / "tests" / "test_pricing.py").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(tests).hexdigest() == VISIBLE_TESTS_SHA256, "tests/ was modified"

    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=APP,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    sys.path.insert(0, str(APP))
    from inventory.pricing import bulk_discount, line_total, order_total

    assert bulk_discount(49) == Decimal("0.05")
    assert bulk_discount(99) == Decimal("0.10")
    assert line_total("0.005", 1) == Decimal("0.01")
    assert line_total("1.00", 100) == Decimal("85.00")
    assert order_total([("20.00", 1)], "NY", coupon="SAVE10") == Decimal("10.40")
    assert order_total([("3.00", 2)], "OR") == Decimal("6.00")
    print("ok")


if __name__ == "__main__":
    main()
