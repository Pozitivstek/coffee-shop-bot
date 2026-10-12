"""Форматирование цен, корзины и заказов."""
import unittest
from datetime import datetime, timezone

from shopbot.db import CartLine, Order, OrderItem
from shopbot.texts import cart_text, money, order_text, pickup_label

NBSP = " "


def line(name="Латте", label="300 мл", price=290, qty=1, available=True) -> CartLine:
    return CartLine(1, "latte", name, label, price, qty, available)


def order(**kw) -> Order:
    base = dict(
        id=12, user_id=5, user_name="Аня <script>", status="new", payment="cash", paid=False,
        total=580, pickup="Через 15 минут", charge_id="",
        created_at=datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc),
        items=(OrderItem("Латте", "300 мл", 290, 2),))
    base.update(kw)
    return Order(**base)


class MoneyTest(unittest.TestCase):
    def test_small_amount(self):
        self.assertEqual(money(290), f"290{NBSP}₽")

    def test_thousands_are_grouped(self):
        self.assertEqual(money(1240), f"1{NBSP}240{NBSP}₽")


class CartTextTest(unittest.TestCase):
    def test_empty_cart(self):
        self.assertIn("пуста", cart_text([]))

    def test_total_is_sum_of_subtotals(self):
        text = cart_text([line(qty=2), line("Круассан", "1 шт", 190, 1)])
        self.assertIn(money(770), text)

    def test_unavailable_lines_are_marked_and_not_counted(self):
        text = cart_text([line(qty=1), line("Круассан", "1 шт", 190, 1, available=False)])
        self.assertIn("нет в наличии", text)
        self.assertIn(money(290), text)
        self.assertNotIn(money(480), text)

    def test_names_are_html_escaped(self):
        text = cart_text([line("<b>Хак</b>")])
        self.assertNotIn("<b>Хак</b>", text)
        self.assertIn("&lt;b&gt;", text)


class OrderTextTest(unittest.TestCase):
    def test_contains_number_items_total_and_status(self):
        text = order_text(order())
        self.assertIn("№12", text)
        self.assertIn("Латте", text)
        self.assertIn("× 2", text)
        self.assertIn(money(580), text)
        self.assertIn("новый", text)

    def test_user_name_is_escaped_for_admin(self):
        text = order_text(order(), admin=True)
        self.assertNotIn("<script>", text)
        self.assertIn("tg://user?id=5", text)

    def test_payment_method_is_shown(self):
        self.assertIn("при получении", order_text(order(payment="cash")))
        self.assertIn("оплачен", order_text(order(payment="card", paid=True)))
        self.assertIn("ждёт оплаты", order_text(order(payment="card", status="awaiting_payment")))


class PickupTest(unittest.TestCase):
    def test_known_codes(self):
        self.assertEqual(pickup_label("asap"), "Как можно скорее")
        self.assertEqual(pickup_label("30"), "Через 30 минут")

    def test_unknown_code_returns_none(self):
        self.assertIsNone(pickup_label("999"))


if __name__ == "__main__":
    unittest.main()
