"""Reference fix for fix-failing-tests."""

import os
from pathlib import Path

path = Path(os.environ.get("TASK_APP_DIR", "/app")) / "inventory" / "pricing.py"
source = path.read_text()
for old, new in [
    ("if quantity > minimum:", "if quantity >= minimum:"),
    (
        'amount.quantize(Decimal("0.01"))',
        'amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)',
    ),
    (
        '        subtotal -= Decimal("10")\n',
        '        subtotal = max(subtotal - Decimal("10"), Decimal("0"))\n'
        "    if state not in TAX_RATES:\n"
        '        raise ValueError(f"unknown state: {state}")\n',
    ),
]:
    assert old in source, old
    source = source.replace(old, new)
path.write_text(source)
