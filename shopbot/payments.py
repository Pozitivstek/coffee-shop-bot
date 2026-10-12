"""Оплата картой через Telegram Payments (тестовый провайдер) или тестовая заглушка без токена.

Режим выбирается по PAYMENT_PROVIDER_TOKEN: пусто → кнопка «Оплатить (тест)», иначе — настоящий инвойс.
callback_data: mockpay:<order_id> | repay:<order_id>;  payload инвойса: order:<order_id>
"""
import logging
import uuid

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from . import keyboards as kb
from .callbacks import to_int
from .config import Settings, Shop
from .db import Database, Order
from .notify import notify_new_order
from .texts import order_text

log = logging.getLogger(__name__)
router = Router(name="payments")

CURRENCY = "RUB"
KOPECKS = 100
PAYLOAD_PREFIX = "order:"


def invoice_prices(order: Order) -> list[LabeledPrice]:
    return [
        LabeledPrice(label=f"{item.product_name} {item.label} × {item.qty}"[:100],
                     amount=item.price * item.qty * KOPECKS)
        for item in order.items
    ]


async def request_payment(call: CallbackQuery, order: Order, shop: Shop, settings: Settings) -> None:
    """Показывает клиенту способ оплатить уже созданный заказ."""
    if settings.real_payments:
        if isinstance(call.message, Message):
            await call.message.edit_text(
                f"Заказ №{order.id} создан. Счёт на оплату — в сообщении ниже 👇", reply_markup=None)
        await call.bot.send_invoice(
            chat_id=call.from_user.id,
            title=f"Заказ №{order.id} · {shop.name}"[:32],
            description=f"{len(order.items)} поз., {order.pickup.lower()}. Самовывоз: {shop.address}"[:255],
            payload=f"{PAYLOAD_PREFIX}{order.id}",
            provider_token=settings.payment_token,
            currency=CURRENCY,
            prices=invoice_prices(order),
        )
        await call.answer()
        return

    text = ("🧪 <b>Тестовая оплата</b>\n"
            "Платёжный провайдер не подключён, поэтому деньги не списываются: нажмите кнопку, "
            "чтобы пройти оплату в тестовом режиме.\n\n" + order_text(order))
    if isinstance(call.message, Message):
        await call.message.edit_text(text, reply_markup=kb.mock_pay_kb(order.id))
    else:
        await call.bot.send_message(call.from_user.id, text, reply_markup=kb.mock_pay_kb(order.id))
    await call.answer()


async def finalize_payment(bot: Bot, db: Database, settings: Settings,
                           order_id: int, charge_id: str) -> Order | None:
    """Помечает заказ оплаченным и зовёт админов. None — заказ уже был оплачен или не найден."""
    if not await db.mark_paid(order_id, charge_id):
        return None
    order = await db.get_order(order_id)
    await notify_new_order(bot, settings, order)
    return order


def order_id_from_payload(payload: str) -> int | None:
    if not payload.startswith(PAYLOAD_PREFIX):
        return None
    return to_int(payload.removeprefix(PAYLOAD_PREFIX))


# ---------------------------------------------------------------- настоящий инвойс

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, db: Database) -> None:
    order_id = order_id_from_payload(query.invoice_payload)
    order = await db.get_order(order_id) if order_id else None
    if order is None or order.user_id != query.from_user.id:
        await query.answer(ok=False, error_message="Заказ не найден. Оформите его заново.")
        return
    if order.status != "awaiting_payment":
        await query.answer(ok=False, error_message="Этот заказ уже оплачен или отменён.")
        return
    if query.currency != CURRENCY or query.total_amount != order.total * KOPECKS:
        await query.answer(ok=False, error_message="Сумма не совпадает с заказом. Оформите его заново.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot, db: Database, shop: Shop, settings: Settings) -> None:
    payment = message.successful_payment
    order_id = order_id_from_payload(payment.invoice_payload)
    if order_id is None:
        log.error("оплата с неизвестным payload %r (charge %s)", payment.invoice_payload,
                  payment.provider_payment_charge_id)
        return
    charge = payment.provider_payment_charge_id or payment.telegram_payment_charge_id
    order = await finalize_payment(bot, db, settings, order_id, charge)
    if order is None:
        log.warning("повторная оплата заказа %s (charge %s)", order_id, charge)
        return
    await message.answer("✅ <b>Оплата прошла!</b>\n\n" + order_text(order) +
                         f"\n\nЖдём вас: {shop.address}", reply_markup=kb.home_button())


# ---------------------------------------------------------------- тестовая заглушка

@router.callback_query(F.data.startswith(("mockpay:", "repay:")))
async def mock_pay(call: CallbackQuery, bot: Bot, db: Database, shop: Shop, settings: Settings) -> None:
    kind, _, raw = (call.data or "").partition(":")
    order_id = to_int(raw)
    order = await db.get_order(order_id) if order_id is not None else None
    if order is None or order.user_id != call.from_user.id:
        await call.answer("Заказ не найден", show_alert=True)
        return
    if order.status != "awaiting_payment":
        await call.answer("Этот заказ уже оплачен или отменён", show_alert=True)
        return

    if kind == "repay":   # вернуться к оплате из «Моих заказов»
        await request_payment(call, order, shop, settings)
        return
    if settings.real_payments:
        await call.answer("Тестовая оплата отключена", show_alert=True)
        return

    paid = await finalize_payment(bot, db, settings, order.id, f"mock-{uuid.uuid4().hex[:12]}")
    if paid is None:
        await call.answer("Оплата уже прошла", show_alert=True)
        return
    text = ("✅ <b>Тестовая оплата прошла!</b>\n\n" + order_text(paid) + f"\n\nЖдём вас: {shop.address}")
    if isinstance(call.message, Message):
        await call.message.edit_text(text, reply_markup=kb.home_button())
    await call.answer("Оплачено (тест)")
