The `inventory` package in the current directory has a failing test suite.

Run it with `python -m unittest discover -s tests` and fix the code in `inventory/` until every test passes.

Rules:
- Do not modify anything under `tests/`.
- Keep the public functions in `inventory/pricing.py` and their signatures.
- Money values are `Decimal`s rounded to cents with half-up rounding.
- `order_total` must raise `ValueError` for a state with no tax rate, and a coupon can never make the total negative.
