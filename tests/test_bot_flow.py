"""Сквозные сценарии: настоящие обработчики + диспетчер aiogram, Telegram подменён."""
import unittest
from dataclasses import replace
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    EditMessageText,
    SendInvoice,
    SendMessage,
)

from shopbot.bot import build_dispatcher
from shopbot.config import ROOT, Settings, load_shop
from shopbot.db import Database

SHOP = load_shop(ROOT / "menu.toml")
TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
ADMIN = 900
ANNA = 100
BORIS = 200


class FakeSession(BaseSession):
    """Копит исходящие вызовы Bot API вместо похода в сеть."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        return None

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""

    def of(self, kind, **attrs):
        return [c for c in self.calls if isinstance(c, kind)
                and all(getattr(c, k, None) == v for k, v in attrs.items())]


_DISPATCHER = None


def shared_dispatcher(db: Database, settings: Settings):
    """Роутеры привязываются к диспетчеру один раз, поэтому на все тесты он один.

    Зависимости (db, settings) подменяются в workflow_data, FSM-хранилище чистится."""
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = build_dispatcher(db, SHOP, settings)
    _DISPATCHER.workflow_data.update(db=db, shop=SHOP, settings=settings)
    _DISPATCHER.storage.storage.clear()
    return _DISPATCHER


def user(uid: int, name: str = "Аня") -> dict:
    return {"id": uid, "is_bot": False, "first_name": name}


def message_update(n: int, uid: int, text: str | None = None, **extra) -> dict:
    msg = {"message_id": n, "date": 0, "chat": {"id": uid, "type": "private"}, "from": user(uid)}
    if text is not None:
        msg["text"] = text
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    msg.update(extra)
    return {"update_id": n, "message": msg}


def callback_update(n: int, uid: int, data: str) -> dict:
    return {"update_id": n, "callback_query": {
        "id": str(n), "from": user(uid), "chat_instance": "x", "data": data,
        "message": {"message_id": 1, "date": 0, "chat": {"id": uid, "type": "private"},
                    "from": {"id": 1, "is_bot": True, "first_name": "bot"}, "text": "старое"}}}


class FlowCase(unittest.IsolatedAsyncioTestCase):
    real_payments = False

    async def asyncSetUp(self):
        self.db = await Database.open(":memory:")
        await self.db.sync_menu(SHOP)
        self.settings = Settings(
            bot_token=TOKEN, admin_ids=frozenset({ADMIN}),
            payment_token="TEST:provider" if self.real_payments else "",
            db_path=Path(":memory:"), menu_file=ROOT / "menu.toml")
        self.session = FakeSession()
        self.bot = Bot(TOKEN, session=self.session,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = shared_dispatcher(self.db, self.settings)
        self.n = 0

    async def asyncTearDown(self):
        await self.db.close()

    async def send(self, uid: int, text: str):
        self.n += 1
        await self.dp.feed_raw_update(self.bot, message_update(self.n, uid, text))

    async def press(self, uid: int, data: str):
        self.n += 1
        await self.dp.feed_raw_update(self.bot, callback_update(self.n, uid, data))

    async def option_id(self, product_id: str, index: int = 0) -> int:
        return (await self.db.get_product(product_id)).options[index].id

    async def buy_latte(self, uid: int = ANNA, qty: int = 1) -> None:
        opt = await self.option_id("latte")
        for _ in range(qty):
            await self.press(uid, f"add:{opt}")

    def texts_to(self, chat_id: int) -> list[str]:
        return [c.text for c in self.session.of(SendMessage, chat_id=chat_id)]


class CustomerFlowTest(FlowCase):
    async def test_start_shows_shop_and_main_menu(self):
        await self.send(ANNA, "/start")
        sent = self.session.of(SendMessage, chat_id=ANNA)[0]
        self.assertIn("Кофейня Ягода", sent.text)
        callbacks = [b.callback_data for row in sent.reply_markup.inline_keyboard for b in row]
        self.assertEqual(callbacks[:3], ["menu", "cart", "orders"])
        self.assertNotIn("a:home", callbacks)   # обычному клиенту админка не видна

    async def test_admin_sees_admin_button(self):
        await self.send(ADMIN, "/start")
        sent = self.session.of(SendMessage, chat_id=ADMIN)[0]
        callbacks = [b.callback_data for row in sent.reply_markup.inline_keyboard for b in row]
        self.assertIn("a:home", callbacks)

    async def test_category_and_product_screens(self):
        await self.press(ANNA, "cat:espresso")
        category = self.session.of(EditMessageText)[-1]
        self.assertIn("Латте", [b.text.split(" · ")[0] for r in category.reply_markup.inline_keyboard for b in r])
        await self.press(ANNA, "prod:latte")
        card = self.session.of(EditMessageText)[-1]
        self.assertIn("мягкий, много молока", card.text)

    async def test_add_to_cart_and_totals(self):
        await self.buy_latte(qty=2)
        await self.press(ANNA, "cart")
        cart = self.session.of(EditMessageText)[-1]
        self.assertIn("× 2", cart.text)
        self.assertIn("580", cart.text)
        toasts = [c.text for c in self.session.of(AnswerCallbackQuery) if c.text]
        self.assertTrue(any("Латте" in t for t in toasts))

    async def test_cash_order_notifies_admin_with_buttons(self):
        await self.buy_latte()
        await self.press(ANNA, "checkout")
        await self.press(ANNA, "pick:15")
        await self.press(ANNA, "pay:15:cash")
        orders = await self.db.user_orders(ANNA)
        self.assertEqual([(o.status, o.pickup) for o in orders], [("new", "Через 15 минут")])
        admin_msg = self.session.of(SendMessage, chat_id=ADMIN)[-1]
        self.assertIn("Новый заказ", admin_msg.text)
        buttons = [b.callback_data for r in admin_msg.reply_markup.inline_keyboard for b in r]
        self.assertIn(f"a:st:{orders[0].id}:accepted", buttons)
        self.assertEqual(await self.db.cart_lines(ANNA), [])

    async def test_empty_cart_checkout_is_refused(self):
        await self.press(ANNA, "checkout")
        self.assertEqual(await self.db.user_orders(ANNA), [])

    async def test_forged_callback_data_does_not_crash_or_create_orders(self):
        await self.buy_latte()
        for data in ("pay:zzz:card", "pay:asap:crypto", "add:abc", "add:99999999", "cat:nope", "order:x"):
            await self.press(ANNA, data)
        self.assertEqual(await self.db.user_orders(ANNA), [])

    async def test_cannot_open_foreign_order(self):
        await self.buy_latte(ANNA)
        await self.press(ANNA, "pay:asap:cash")
        order = (await self.db.user_orders(ANNA))[0]
        before = len(self.session.of(EditMessageText))
        await self.press(BORIS, f"order:{order.id}")
        self.assertEqual(len(self.session.of(EditMessageText)), before)
        self.assertTrue(self.session.of(AnswerCallbackQuery)[-1].show_alert)

    async def test_customer_can_cancel_unpaid_order(self):
        await self.buy_latte()
        await self.press(ANNA, "pay:asap:cash")
        order = (await self.db.user_orders(ANNA))[0]
        await self.press(ANNA, f"cancel:{order.id}")
        self.assertEqual((await self.db.get_order(order.id)).status, "cancelled")
        self.assertTrue(any("отменил" in t for t in self.texts_to(ADMIN)))


class CancelRulesTest(FlowCase):
    async def test_customer_cannot_cancel_order_already_in_work(self):
        await self.buy_latte()
        await self.press(ANNA, "pay:asap:cash")
        order = (await self.db.user_orders(ANNA))[0]
        await self.press(ADMIN, f"a:st:{order.id}:accepted")
        await self.press(ANNA, f"cancel:{order.id}")   # поддельный callback: кнопки у клиента уже нет
        self.assertEqual((await self.db.get_order(order.id)).status, "accepted")

    async def test_huge_ids_in_callbacks_are_ignored(self):
        huge = "9" * 40
        for data in (f"order:{huge}", f"add:{huge}", f"a:ord:{huge}", f"a:st:{huge}:done",
                     f"a:popt:{huge}", f"mockpay:{huge}"):
            await self.press(ANNA, data)
            await self.press(ADMIN, data)
        self.assertEqual(await self.db.user_orders(ANNA), [])


class MockPaymentTest(FlowCase):
    async def test_card_order_waits_then_test_payment_marks_paid_once(self):
        await self.buy_latte()
        await self.press(ANNA, "pay:asap:card")
        order = (await self.db.user_orders(ANNA))[0]
        self.assertEqual(order.status, "awaiting_payment")
        self.assertIn("Тестовая оплата", self.session.of(EditMessageText)[-1].text)
        self.assertEqual(self.texts_to(ADMIN), [])   # пока не оплачен — админа не беспокоим

        await self.press(ANNA, f"mockpay:{order.id}")
        paid = await self.db.get_order(order.id)
        self.assertEqual((paid.status, paid.paid), ("new", True))
        self.assertTrue(paid.charge_id.startswith("mock-"))
        self.assertEqual(len(self.texts_to(ADMIN)), 1)

        await self.press(ANNA, f"mockpay:{order.id}")   # двойной клик
        self.assertEqual(len(self.texts_to(ADMIN)), 1)

    async def test_someone_else_cannot_pay_for_order(self):
        await self.buy_latte(ANNA)
        await self.press(ANNA, "pay:asap:card")
        order = (await self.db.user_orders(ANNA))[0]
        await self.press(BORIS, f"mockpay:{order.id}")
        self.assertEqual((await self.db.get_order(order.id)).status, "awaiting_payment")


class RealPaymentTest(FlowCase):
    real_payments = True

    async def pay_by_card(self):
        await self.buy_latte(qty=2)
        await self.press(ANNA, "pay:asap:card")
        return (await self.db.user_orders(ANNA))[0]

    async def test_invoice_matches_order_total(self):
        order = await self.pay_by_card()
        invoices = self.session.of(SendInvoice)
        self.assertEqual(len(invoices), 1)
        invoice = invoices[0]
        self.assertEqual(invoice.payload, f"order:{order.id}")
        self.assertEqual(sum(p.amount for p in invoice.prices), order.total * 100)
        self.assertEqual(invoice.currency, "RUB")

    async def test_mock_button_is_disabled_when_real_payments_on(self):
        order = await self.pay_by_card()
        await self.press(ANNA, f"mockpay:{order.id}")
        self.assertEqual((await self.db.get_order(order.id)).status, "awaiting_payment")

    def pre_checkout(self, n, uid, payload, amount):
        return {"update_id": n, "pre_checkout_query": {
            "id": f"pc{n}", "from": user(uid), "currency": "RUB",
            "total_amount": amount, "invoice_payload": payload}}

    async def test_pre_checkout_accepts_only_matching_amount_and_owner(self):
        order = await self.pay_by_card()
        good = order.total * 100
        cases = [
            (ANNA, f"order:{order.id}", good, True),
            (ANNA, f"order:{order.id}", good - 100, False),
            (BORIS, f"order:{order.id}", good, False),
            (ANNA, "order:9999", good, False),
            (ANNA, "garbage", good, False),
        ]
        for i, (uid, payload, amount, expected) in enumerate(cases, start=500):
            await self.dp.feed_raw_update(self.bot, self.pre_checkout(i, uid, payload, amount))
            answered = self.session.of(AnswerPreCheckoutQuery)[-1]
            self.assertEqual(answered.ok, expected, (payload, amount))

    async def test_successful_payment_marks_paid_and_notifies_once(self):
        order = await self.pay_by_card()
        payment = {"currency": "RUB", "total_amount": order.total * 100,
                   "invoice_payload": f"order:{order.id}",
                   "telegram_payment_charge_id": "tg-1", "provider_payment_charge_id": "prov-1"}
        for n in (600, 601):   # Telegram может повторить доставку
            await self.dp.feed_raw_update(self.bot, message_update(n, ANNA, successful_payment=payment))
        paid = await self.db.get_order(order.id)
        self.assertEqual((paid.status, paid.paid, paid.charge_id), ("new", True, "prov-1"))
        admin_texts = [t for t in self.texts_to(ADMIN) if "Новый заказ" in t]
        self.assertEqual(len(admin_texts), 1)
        self.assertTrue(any("Оплата прошла" in t for t in self.texts_to(ANNA)))


class AdminFlowTest(FlowCase):
    async def place_cash_order(self) -> int:
        await self.buy_latte()
        await self.press(ANNA, "pay:asap:cash")
        return (await self.db.user_orders(ANNA))[0].id

    async def test_admin_command_is_guarded(self):
        await self.send(ANNA, "/admin")
        self.assertEqual(self.session.of(SendMessage, chat_id=ANNA), [])
        await self.send(ADMIN, "/admin")
        self.assertIn("Админка", self.texts_to(ADMIN)[-1])

    async def test_non_admin_cannot_change_status(self):
        order_id = await self.place_cash_order()
        await self.press(ANNA, f"a:st:{order_id}:accepted")
        self.assertEqual((await self.db.get_order(order_id)).status, "new")

    async def test_admin_status_changes_notify_customer(self):
        order_id = await self.place_cash_order()
        await self.press(ADMIN, f"a:st:{order_id}:accepted")
        self.assertIn("принят", self.texts_to(ANNA)[-1])
        await self.press(ADMIN, f"a:st:{order_id}:ready")
        self.assertIn("готов", self.texts_to(ANNA)[-1])
        self.assertIn("Садовая", self.texts_to(ANNA)[-1])
        await self.press(ADMIN, f"a:st:{order_id}:done")
        self.assertEqual((await self.db.get_order(order_id)).status, "done")

    async def test_admin_cannot_skip_statuses(self):
        order_id = await self.place_cash_order()
        await self.press(ADMIN, f"a:st:{order_id}:done")
        self.assertEqual((await self.db.get_order(order_id)).status, "new")
        self.assertTrue(self.session.of(AnswerCallbackQuery)[-1].show_alert)

    async def test_stop_list_toggle_hides_item_from_customers(self):
        await self.press(ADMIN, "a:tog:latte")
        self.assertFalse((await self.db.get_product("latte")).available)
        await self.press(ANNA, "cat:espresso")
        shown = self.session.of(EditMessageText)[-1]
        labels = [b.text for r in shown.reply_markup.inline_keyboard for b in r]
        self.assertFalse(any(label.startswith("Латте") for label in labels))
        await self.press(ADMIN, "a:tog:latte")
        self.assertTrue((await self.db.get_product("latte")).available)

    async def test_price_edit_dialog(self):
        opt = await self.option_id("latte")
        await self.press(ADMIN, f"a:popt:{opt}")
        await self.send(ADMIN, "abc")
        self.assertEqual((await self.db.get_product("latte")).options[0].price, 290)
        await self.send(ADMIN, "310")
        self.assertEqual((await self.db.get_product("latte")).options[0].price, 310)

    async def test_price_edit_rejects_absurd_values(self):
        opt = await self.option_id("latte")
        await self.press(ADMIN, f"a:popt:{opt}")
        await self.send(ADMIN, "99999999")
        self.assertEqual((await self.db.get_product("latte")).options[0].price, 290)

    async def test_today_stats_screen(self):
        await self.place_cash_order()
        await self.press(ADMIN, "a:today")
        text = self.session.of(EditMessageText)[-1].text
        self.assertIn("Заказов: <b>1</b>", text)
        self.assertIn("Латте", text)


if __name__ == "__main__":
    unittest.main()
