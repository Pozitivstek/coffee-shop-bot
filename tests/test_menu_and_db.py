"""Меню из toml и слой БД: корзина, заказы, статусы, оплата, статистика."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shopbot.config import ROOT, MenuError, load_shop
from shopbot.db import (
    CART_LINE_LIMIT,
    CART_QTY_LIMIT,
    Database,
    EmptyCart,
)

SHOP = load_shop(ROOT / "menu.toml")

SHOP_HEADER = """
[shop]
name = "X"
address = "a"
phone = "p"
hours = "h"
timezone = "Europe/Moscow"
about = "t"
"""


class MenuLoadTest(unittest.TestCase):
    def test_real_menu_is_valid(self):
        self.assertEqual(SHOP.name, "Кофейня Ягода")
        ids = [p.id for c in SHOP.categories for p in c.products]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 15)
        for category in SHOP.categories:
            for product in category.products:
                self.assertTrue(product.options, product.id)
                for option in product.options:
                    self.assertGreater(option.price, 0)

    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "menu.toml"
            path.write_text(text, encoding="utf-8")
            return load_shop(path)

    def test_duplicate_product_ids_are_rejected(self):
        text = SHOP_HEADER + """
[[category]]
id = "c"
name = "C"
  [[category.product]]
  id = "dup"
  name = "A"
  description = ""
    [[category.product.option]]
    label = "1"
    price = 10
  [[category.product]]
  id = "dup"
  name = "B"
  description = ""
    [[category.product.option]]
    label = "1"
    price = 10
"""
        with self.assertRaises(MenuError):
            self._load(text)

    def test_non_positive_price_is_rejected(self):
        text = SHOP_HEADER + """
[[category]]
id = "c"
name = "C"
  [[category.product]]
  id = "p"
  name = "A"
  description = ""
    [[category.product.option]]
    label = "1"
    price = 0
"""
        with self.assertRaises(MenuError):
            self._load(text)

    def test_product_without_options_is_rejected(self):
        text = SHOP_HEADER + """
[[category]]
id = "c"
name = "C"
  [[category.product]]
  id = "p"
  name = "A"
  description = ""
"""
        with self.assertRaises(MenuError):
            self._load(text)


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await Database.open(":memory:")
        await self.db.sync_menu(SHOP)

    async def asyncTearDown(self):
        await self.db.close()

    async def option_of(self, product_id: str, index: int = 0) -> int:
        product = await self.db.get_product(product_id)
        return product.options[index].id


class CatalogTest(DbCase):
    async def test_sync_is_idempotent(self):
        before = await self.db.list_categories()
        await self.db.sync_menu(SHOP)
        after = await self.db.list_categories()
        self.assertEqual(before, after)
        self.assertEqual(len(before), len(SHOP.categories))

    async def test_sync_keeps_admin_edits(self):
        product = await self.db.get_product("latte")
        await self.db.set_option_price(product.options[0].id, 999)
        await self.db.set_available("latte", False)
        await self.db.sync_menu(SHOP)
        again = await self.db.get_product("latte")
        self.assertEqual(again.options[0].price, 999)
        self.assertFalse(again.available)

    async def test_stop_list_hides_product_from_listing(self):
        await self.db.set_available("latte", False)
        names = [p.id for p in await self.db.list_products("espresso")]
        self.assertNotIn("latte", names)
        all_names = [p.id for p in await self.db.list_products("espresso", only_available=False)]
        self.assertIn("latte", all_names)

    async def test_unknown_product_returns_none(self):
        self.assertIsNone(await self.db.get_product("nope"))


class CartTest(DbCase):
    async def test_add_increments_and_totals(self):
        opt = await self.option_of("latte")
        await self.db.cart_add(1, opt)
        await self.db.cart_add(1, opt)
        lines = await self.db.cart_lines(1)
        self.assertEqual([(l.product_name, l.qty) for l in lines], [("Латте", 2)])
        self.assertEqual(lines[0].price, 290)

    async def test_carts_are_per_user(self):
        opt = await self.option_of("latte")
        await self.db.cart_add(1, opt)
        self.assertEqual(await self.db.cart_lines(2), [])

    async def test_decrement_to_zero_removes_line(self):
        opt = await self.option_of("latte")
        await self.db.cart_add(1, opt)
        await self.db.cart_add(1, opt, -1)
        self.assertEqual(await self.db.cart_lines(1), [])

    async def test_quantity_is_capped(self):
        opt = await self.option_of("latte")
        for _ in range(CART_QTY_LIMIT + 5):
            await self.db.cart_add(1, opt)
        lines = await self.db.cart_lines(1)
        self.assertEqual(lines[0].qty, CART_QTY_LIMIT)

    async def test_line_count_is_capped(self):
        options = [o.id for c in await self.all_products() for o in c.options]
        accepted = 0
        for opt in options:
            if await self.db.cart_add(1, opt):
                accepted += 1
        self.assertEqual(accepted, CART_LINE_LIMIT)

    async def all_products(self):
        products = []
        for category in await self.db.list_categories():
            products.extend(await self.db.list_products(category.id))
        return products

    async def test_unknown_option_is_ignored(self):
        self.assertFalse(await self.db.cart_add(1, 999_999))
        self.assertEqual(await self.db.cart_lines(1), [])

    async def test_unavailable_product_cannot_be_added(self):
        opt = await self.option_of("latte")
        await self.db.set_available("latte", False)
        self.assertFalse(await self.db.cart_add(1, opt))

    async def test_clear(self):
        await self.db.cart_add(1, await self.option_of("latte"))
        await self.db.cart_clear(1)
        self.assertEqual(await self.db.cart_lines(1), [])


class OrderTest(DbCase):
    async def fill(self, user_id=1):
        await self.db.cart_add(user_id, await self.option_of("latte"))
        await self.db.cart_add(user_id, await self.option_of("latte"))
        await self.db.cart_add(user_id, await self.option_of("croissant"))

    async def test_empty_cart_cannot_be_ordered(self):
        with self.assertRaises(EmptyCart):
            await self.db.create_order(1, "Аня", payment="cash", pickup="как можно скорее")

    async def test_cash_order_is_new_and_clears_cart(self):
        await self.fill()
        order = await self.db.create_order(1, "Аня", payment="cash", pickup="через 15 мин")
        self.assertEqual(order.status, "new")
        self.assertEqual(order.total, 290 * 2 + 190)
        self.assertEqual(sum(i.qty for i in order.items), 3)
        self.assertEqual(await self.db.cart_lines(1), [])

    async def test_card_order_waits_for_payment(self):
        await self.fill()
        order = await self.db.create_order(1, "Аня", payment="card", pickup="как можно скорее")
        self.assertEqual(order.status, "awaiting_payment")
        self.assertFalse(order.paid)

    async def test_order_keeps_price_snapshot(self):
        await self.fill()
        order = await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        latte = await self.db.get_product("latte")
        await self.db.set_option_price(latte.options[0].id, 1)
        again = await self.db.get_order(order.id)
        self.assertEqual(again.total, 290 * 2 + 190)

    async def test_unavailable_items_are_dropped_from_order(self):
        await self.fill()
        await self.db.set_available("croissant", False)
        order = await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        self.assertEqual([i.product_name for i in order.items], ["Латте"])
        self.assertEqual(order.total, 580)

    async def test_order_of_only_unavailable_items_is_empty_cart(self):
        await self.db.cart_add(1, await self.option_of("croissant"))
        await self.db.set_available("croissant", False)
        with self.assertRaises(EmptyCart):
            await self.db.create_order(1, "Аня", payment="cash", pickup="x")

    async def test_mark_paid_is_idempotent(self):
        await self.fill()
        order = await self.db.create_order(1, "Аня", payment="card", pickup="x")
        self.assertTrue(await self.db.mark_paid(order.id, "charge-1"))
        self.assertFalse(await self.db.mark_paid(order.id, "charge-1"))
        paid = await self.db.get_order(order.id)
        self.assertEqual(paid.status, "new")
        self.assertTrue(paid.paid)
        self.assertEqual(paid.charge_id, "charge-1")

    async def test_status_flow(self):
        await self.fill()
        order = await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        for status in ("accepted", "ready", "done"):
            updated = await self.db.set_status(order.id, status)
            self.assertEqual(updated.status, status)

    async def test_invalid_transition_is_rejected(self):
        await self.fill()
        order = await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        self.assertIsNone(await self.db.set_status(order.id, "done"))
        self.assertEqual((await self.db.get_order(order.id)).status, "new")

    async def test_finished_order_cannot_be_cancelled(self):
        await self.fill()
        order = await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        for status in ("accepted", "ready", "done"):
            await self.db.set_status(order.id, status)
        self.assertIsNone(await self.db.set_status(order.id, "cancelled"))

    async def test_user_orders_newest_first_and_scoped(self):
        await self.fill(1)
        first = await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        await self.fill(1)
        second = await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        await self.fill(2)
        await self.db.create_order(2, "Боря", payment="cash", pickup="x")
        ids = [o.id for o in await self.db.user_orders(1)]
        self.assertEqual(ids, [second.id, first.id])

    async def test_orders_by_status(self):
        await self.fill()
        a = await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        await self.fill()
        b = await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        await self.db.set_status(b.id, "accepted")
        new = [o.id for o in await self.db.orders_by_status(("new",))]
        self.assertEqual(new, [a.id])


class StatsTest(DbCase):
    async def test_today_stats_exclude_cancelled_and_unpaid(self):
        opt = await self.option_of("latte")
        for _ in range(3):
            await self.db.cart_add(1, opt)
        paid_cash = await self.db.create_order(1, "Аня", payment="cash", pickup="x")   # 870
        await self.db.cart_add(1, opt)
        cancelled = await self.db.create_order(1, "Аня", payment="cash", pickup="x")   # 290
        await self.db.set_status(cancelled.id, "cancelled")
        await self.db.cart_add(1, opt)
        await self.db.create_order(1, "Аня", payment="card", pickup="x")   # не оплачен

        now = datetime.now(timezone.utc)
        stats = await self.db.stats(now - timedelta(hours=1), now + timedelta(hours=1))
        self.assertEqual(stats.orders, 1)
        self.assertEqual(stats.revenue, 870)
        self.assertEqual(stats.top_items[0], ("Латте", 3))
        self.assertEqual(paid_cash.total, 870)

    async def test_stats_outside_window_are_empty(self):
        await self.db.cart_add(1, await self.option_of("latte"))
        await self.db.create_order(1, "Аня", payment="cash", pickup="x")
        past = datetime.now(timezone.utc) - timedelta(days=2)
        stats = await self.db.stats(past - timedelta(days=1), past)
        self.assertEqual((stats.orders, stats.revenue, stats.top_items), (0, 0, ()))


if __name__ == "__main__":
    unittest.main()
