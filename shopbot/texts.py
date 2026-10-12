"""Тексты сообщений бота (HTML parse mode — всё пользовательское экранируется)."""
from html import escape
from typing import Sequence

from .config import Shop
from .db import CartLine, Order

NBSP = " "

STATUS_LABELS = {
    "awaiting_payment": "ждёт оплаты",
    "new": "новый",
    "accepted": "готовится",
    "ready": "готов к выдаче",
    "done": "выдан",
    "cancelled": "отменён",
}

STATUS_ICONS = {
    "awaiting_payment": "💳",
    "new": "🆕",
    "accepted": "🔥",
    "ready": "✅",
    "done": "🏁",
    "cancelled": "✖️",
}

PICKUPS = {
    "asap": "Как можно скорее",
    "15": "Через 15 минут",
    "30": "Через 30 минут",
    "60": "Через час",
}


def pickup_label(code: str) -> str | None:
    return PICKUPS.get(code)


def money(value: int) -> str:
    return f"{value:,}".replace(",", NBSP) + f"{NBSP}₽"


def welcome_text(shop: Shop) -> str:
    return (f"☕ <b>{escape(shop.name)}</b>\n\n{escape(shop.about)}\n\n"
            "Выберите, что хотите сделать:")


def about_text(shop: Shop) -> str:
    return (f"☕ <b>{escape(shop.name)}</b>\n\n{escape(shop.about)}\n\n"
            f"📍 {escape(shop.address)}\n"
            f"🕗 {escape(shop.hours)}\n"
            f"📞 {escape(shop.phone)}")


def cart_text(lines: Sequence[CartLine]) -> str:
    if not lines:
        return "🧺 Корзина пуста. Загляните в меню ☕"
    rows = ["🧺 <b>Ваша корзина</b>\n"]
    total = 0
    for line in lines:
        mark = "" if line.available else " — <i>нет в наличии</i>"
        rows.append(f"• {escape(line.product_name)} · {escape(line.label)} "
                    f"× {line.qty} = {money(line.subtotal)}{mark}")
        if line.available:
            total += line.subtotal
    rows.append(f"\n<b>Итого: {money(total)}</b>")
    return "\n".join(rows)


def _payment_text(order: Order) -> str:
    if order.payment == "cash":
        return "оплата при получении"
    return "оплачен картой" if order.paid else "картой — ждёт оплаты"


def order_text(order: Order, *, admin: bool = False) -> str:
    icon = STATUS_ICONS.get(order.status, "")
    rows = [f"{icon} <b>Заказ №{order.id}</b> · {STATUS_LABELS.get(order.status, order.status)}"]
    if admin:
        name = escape(order.user_name)
        rows.append(f'👤 <a href="tg://user?id={order.user_id}">{name}</a>')
    rows.append("")
    for item in order.items:
        rows.append(f"• {escape(item.product_name)} · {escape(item.label)} "
                    f"× {item.qty} = {money(item.price * item.qty)}")
    rows.append(f"\n<b>Итого: {money(order.total)}</b> ({_payment_text(order)})")
    rows.append(f"🕒 {escape(order.pickup)}")
    return "\n".join(rows)


def status_message(order: Order, shop: Shop) -> str:
    """Что пишем клиенту, когда админ поменял статус."""
    n = order.id
    if order.status == "accepted":
        return f"🔥 Заказ №{n} принят, уже готовим."
    if order.status == "ready":
        return f"✅ Заказ №{n} готов! Забирайте: {escape(shop.address)}."
    if order.status == "done":
        return f"🏁 Заказ №{n} выдан. Спасибо, что выбрали «{escape(shop.name)}»!"
    if order.status == "cancelled":
        refund = (f"\nОплата картой будет возвращена вручную — позвоните: {escape(shop.phone)}."
                  if order.paid else "")
        return f"✖️ Заказ №{n} отменён.{refund}"
    return f"Заказ №{n}: {STATUS_LABELS.get(order.status, order.status)}."
