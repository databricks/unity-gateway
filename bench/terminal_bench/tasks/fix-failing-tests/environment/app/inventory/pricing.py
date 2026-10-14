"""Order pricing for the inventory service."""

from decimal import ROUND_HALF_UP, Decimal

# (minimum quantity, discount fraction), checked from the largest tier down.
BULK_TIERS = [(100, Decimal("0.15")), (50, Decimal("0.10")), (10, Decimal("0.05"))]

TAX_RATES = {"CA": Decimal("0.0725"), "NY": Decimal("0.04"), "OR": Decimal("0")}


def bulk_discount(quantity: int) -> Decimal:
    for minimum, discount in BULK_TIERS:
        if quantity > minimum:
            return discount
    return Decimal("0")


def round_cents(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"))


def line_total(unit_price: str, quantity: int) -> Decimal:
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    subtotal = Decimal(unit_price) * quantity
    return round_cents(subtotal - subtotal * bulk_discount(quantity))


def order_total(lines: list[tuple[str, int]], state: str, coupon: str | None = None) -> Decimal:
    subtotal = sum((line_total(price, qty) for price, qty in lines), Decimal("0"))
    if coupon == "SAVE10":
        subtotal -= Decimal("10")
    tax = subtotal * TAX_RATES[state]
    return round_cents(subtotal + tax)
