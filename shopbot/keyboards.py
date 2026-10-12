"""Inline-клавиатуры. Формат callback_data описан рядом с обработчиками."""
from typing import Sequence

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .db import TRANSITIONS, CartLine, Category, Order, ProductRow
from .texts import PICKUPS, STATUS_ICONS, STATUS_LABELS, money

STATUS_ACTIONS = {          # статус → подпись кнопки админа
    "accepted": "🔥 Принять",
    "ready": "✅ Готов",
    "done": "🏁 Выдан",
    "cancelled": "✖️ Отменить",
}


def _markup(builder: InlineKeyboardBuilder, *sizes: int) -> InlineKeyboardMarkup:
    builder.adjust(*sizes)
    return builder.as_markup()


# ---------------------------------------------------------------- клиент

def main_menu(cart_count: int = 0, is_admin: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="☕ Меню", callback_data="menu")
    b.button(text=f"🧺 Корзина ({cart_count})" if cart_count else "🧺 Корзина", callback_data="cart")
    b.button(text="📦 Мои заказы", callback_data="orders")
    b.button(text="ℹ️ О кофейне", callback_data="about")
    if is_admin:
        b.button(text="🛠 Админка", callback_data="a:home")
    return _markup(b, 1, 2, 1, 1)


def home_button() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="← В начало", callback_data="home")
    return _markup(b, 1)


def categories_kb(categories: Sequence[Category]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for category in categories:
        b.button(text=category.name, callback_data=f"cat:{category.id}")
    b.button(text="🧺 Корзина", callback_data="cart")
    b.button(text="← В начало", callback_data="home")
    return _markup(b, 2, 2, 2, 2)


def products_kb(products: Sequence[ProductRow]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for product in products:
        prefix = "от " if len(product.options) > 1 else ""
        b.button(text=f"{product.name} · {prefix}{money(product.min_price)}",
                 callback_data=f"prod:{product.id}")
    b.button(text="← Категории", callback_data="menu")
    b.button(text="🧺 Корзина", callback_data="cart")
    return _markup(b, *([1] * len(products)), 2)


def product_kb(product: ProductRow, cart_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for option in product.options:
        b.button(text=f"➕ {option.label} · {money(option.price)}", callback_data=f"add:{option.id}")
    b.button(text=f"🧺 Корзина ({cart_count})" if cart_count else "🧺 Корзина", callback_data="cart")
    b.button(text="← Назад", callback_data=f"cat:{product.category_id}")
    return _markup(b, *([1] * len(product.options)), 2)


def cart_kb(lines: Sequence[CartLine]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    sizes: list[int] = []
    for line in lines:
        if line.available:
            b.button(text="➖", callback_data=f"dec:{line.option_id}")
            b.button(text=f"{line.product_name} × {line.qty}", callback_data=f"prod:{line.product_id}")
            b.button(text="➕", callback_data=f"inc:{line.option_id}")
        else:
            b.button(text=f"✖ убрать «{line.product_name}»", callback_data=f"del:{line.option_id}")
        sizes.append(3 if line.available else 1)
    if any(line.available for line in lines):
        b.button(text="🗑 Очистить", callback_data="clear")
        b.button(text="✅ Оформить заказ", callback_data="checkout")
        sizes.append(2)
    b.button(text="☕ В меню", callback_data="menu")
    sizes.append(1)
    return _markup(b, *sizes)


def pickup_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, label in PICKUPS.items():
        b.button(text=label, callback_data=f"pick:{code}")
    b.button(text="← К корзине", callback_data="cart")
    return _markup(b, 2, 2, 1)


def payment_kb(pickup_code: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Картой онлайн", callback_data=f"pay:{pickup_code}:card")
    b.button(text="💵 При получении", callback_data=f"pay:{pickup_code}:cash")
    b.button(text="← Назад", callback_data="checkout")
    return _markup(b, 1, 1, 1)


def mock_pay_kb(order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Оплатить (тест)", callback_data=f"mockpay:{order_id}")
    b.button(text="✖️ Отменить заказ", callback_data=f"cancel:{order_id}")
    return _markup(b, 1, 1)


def orders_kb(orders: Sequence[Order]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for order in orders:
        icon = STATUS_ICONS.get(order.status, "")
        b.button(text=f"{icon} №{order.id} · {money(order.total)} · {STATUS_LABELS[order.status]}",
                 callback_data=f"order:{order.id}")
    b.button(text="← В начало", callback_data="home")
    return _markup(b, *([1] * len(orders)), 1)


def order_kb(order: Order) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if order.status == "awaiting_payment":
        b.button(text="💳 Оплатить", callback_data=f"repay:{order.id}")
    if order.status in ("awaiting_payment", "new") and not order.paid:
        b.button(text="✖️ Отменить заказ", callback_data=f"cancel:{order.id}")
    b.button(text="← Мои заказы", callback_data="orders")
    return _markup(b, 1)


# ---------------------------------------------------------------- админ

def admin_home_kb(new_count: int, work_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"🆕 Новые ({new_count})", callback_data="a:new")
    b.button(text=f"🔥 В работе ({work_count})", callback_data="a:work")
    b.button(text="📊 Сегодня", callback_data="a:today")
    b.button(text="🚫 Стоп-лист", callback_data="a:stop")
    b.button(text="💵 Цены", callback_data="a:price")
    b.button(text="← В начало", callback_data="home")
    return _markup(b, 2, 1, 2, 1)


def admin_orders_kb(orders: Sequence[Order]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for order in orders:
        b.button(text=f"№{order.id} · {money(order.total)} · {order.user_name[:18]}",
                 callback_data=f"a:ord:{order.id}")
    b.button(text="← Админка", callback_data="a:home")
    return _markup(b, *([1] * len(orders)), 1)


def admin_order_kb(order: Order) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    targets = [s for s in ("accepted", "ready", "done", "cancelled") if s in TRANSITIONS[order.status]]
    for status in targets:
        b.button(text=STATUS_ACTIONS[status], callback_data=f"a:st:{order.id}:{status}")
    b.button(text="← Админка", callback_data="a:home")
    return _markup(b, *([len(targets)] if targets else []), 1)


def admin_categories_kb(categories: Sequence[Category], prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for category in categories:
        b.button(text=category.name, callback_data=f"{prefix}:{category.id}")
    b.button(text="← Админка", callback_data="a:home")
    return _markup(b, 2, 2, 2, 1)


def admin_stop_kb(products: Sequence[ProductRow], category_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for product in products:
        mark = "✅" if product.available else "🚫"
        b.button(text=f"{mark} {product.name}", callback_data=f"a:tog:{product.id}")
    b.button(text="← Категории", callback_data="a:stop")
    return _markup(b, *([1] * len(products)), 1)


def admin_price_products_kb(products: Sequence[ProductRow]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for product in products:
        b.button(text=product.name, callback_data=f"a:pprod:{product.id}")
    b.button(text="← Категории", callback_data="a:price")
    return _markup(b, *([1] * len(products)), 1)


def admin_price_options_kb(product: ProductRow) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for option in product.options:
        b.button(text=f"{option.label} · {money(option.price)}", callback_data=f"a:popt:{option.id}")
    b.button(text="← Назад", callback_data=f"a:pcat:{product.category_id}")
    return _markup(b, *([1] * len(product.options)), 1)


def cancel_edit_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Отмена", callback_data="a:home")
    return _markup(b, 1)
