import unittest
from decimal import Decimal

from inventory.pricing import bulk_discount, line_total, order_total


class BulkDiscountTest(unittest.TestCase):
    def test_tier_boundaries_are_inclusive(self):
        self.assertEqual(bulk_discount(9), Decimal("0"))
        self.assertEqual(bulk_discount(10), Decimal("0.05"))
        self.assertEqual(bulk_discount(50), Decimal("0.10"))
        self.assertEqual(bulk_discount(100), Decimal("0.15"))


class LineTotalTest(unittest.TestCase):
    def test_half_cents_round_up(self):
        # 3 x 0.125 = 0.375, which rounds to 0.38.
        self.assertEqual(line_total("0.125", 3), Decimal("0.38"))

    def test_bulk_discount_applies(self):
        self.assertEqual(line_total("2.00", 10), Decimal("19.00"))

    def test_negative_quantity_rejected(self):
        with self.assertRaises(ValueError):
            line_total("1.00", -1)


class OrderTotalTest(unittest.TestCase):
    def test_tax_is_added(self):
        self.assertEqual(order_total([("10.00", 1)], "CA"), Decimal("10.73"))

    def test_coupon_never_makes_total_negative(self):
        self.assertEqual(order_total([("4.00", 1)], "NY", coupon="SAVE10"), Decimal("0.00"))

    def test_unknown_state_is_a_value_error(self):
        with self.assertRaises(ValueError):
            order_total([("1.00", 1)], "ZZ")


if __name__ == "__main__":
    unittest.main()
