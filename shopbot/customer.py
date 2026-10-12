"""Клиентский поток: меню → корзина → оформление → мои заказы.

callback_data: menu | home | cart | about | orders | checkout | clear
               cat:<id> | prod:<id> | add:<option> | inc:<option> | dec:<option> | del:<option>
               pick:<code> | pay:<code>:<card|cash> | order:<id> | cancel:<id>
"""
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from . import keyboards as kb
from .callbacks import to_int
from .config import Settings, Shop
from .db import CART_QTY_LIMIT, CART_LINE_LIMIT, Database, EmptyCart
from .notify import notify_admins, notify_new_order
from .payments import request_payment
from .texts import about_text, cart_text, order_text, pickup_label, welcome_text

log = logging.getLogger(__name__)
router = Router(name="customer")

Target = Message | CallbackQuery


def arg(data: str | None) -> str:
    return (data or "").partition(":")[2]


async def show(target: Target, text: str, markup: InlineKeyboardMarkup | None = None,
               toast: str | None = None) -> None:
    """Редактирует сообщение с кнопкой, а на обычную команду — отвечает новым."""
    if isinstance(target, CallbackQuery):
        message = target.message
        if isinstance(message, Message):
            try:
                await message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as exc:
                if "not modified" not in str(exc):
                    await message.answer(text, reply_markup=markup)
        else:
            await target.bot.send_message(target.from_user.id, text, reply_markup=markup)
        await target.answer(toast)
    else:
        await target.answer(text, reply_markup=markup)


async def cart_count(db: Database, user_id: int) -> int:
    return sum(line.qty for line in await db.cart_lines(user_id))


async def show_home(target: Target, db: Database, shop: Shop, settings: Settings) -> None:
    user_id = target.from_user.id
    markup = kb.main_menu(await cart_count(db, user_id), user_id in settings.admin_ids)
    await show(target, welcome_text(shop), markup)


async def show_categories(target: Target, db: Database) -> None:
    await show(target, "☕ <b>Меню</b>\nВыберите категорию:", kb.categories_kb(await db.list_categories()))


async def show_cart(target: Target, db: Database, toast: str | None = None) -> None:
    lines = await db.cart_lines(target.from_user.id)
    await show(target, cart_text(lines), kb.cart_kb(lines), toast)


async def show_orders(target: Target, db: Database) -> None:
    orders = await db.user_orders(target.from_user.id)
    if not orders:
        await show(target, "📦 Заказов пока нет. Самое время заглянуть в меню ☕", kb.home_button())
        return
    await show(target, "📦 <b>Ваши заказы</b>", kb.orders_kb(orders))


async def show_product(target: Target, db: Database, product_id: str, toast: str | None = None) -> None:
    product = await db.get_product(product_id)
    if product is None or not product.available:
        await show(target, "Этой позиции сейчас нет в наличии 🙈",
                   kb.home_button(), toast="Нет в наличии")
        return
    text = f"<b>{escape(product.name)}</b>"
    if product.description:
        text += f"\n{escape(product.description)}"
    text += "\n\nВыберите вариант:"
    await show(target, text, kb.product_kb(product, await cart_count(db, target.from_user.id)), toast)


# ---------------------------------------------------------------- команды

@router.message(CommandStart())
async def start(message: Message, db: Database, shop: Shop, settings: Settings) -> None:
    await show_home(message, db, shop, settings)


@router.message(Command("id"))
async def whoami(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("menu"))
async def cmd_menu(message: Message, db: Database) -> None:
    await show_categories(message, db)


@router.message(Command("cart"))
async def cmd_cart(message: Message, db: Database) -> None:
    await show_cart(message, db)


@router.message(Command("orders"))
async def cmd_orders(message: Message, db: Database) -> None:
    await show_orders(message, db)


# ---------------------------------------------------------------- каталог

@router.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery, db: Database, shop: Shop, settings: Settings) -> None:
    await show_home(call, db, shop, settings)


@router.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery, shop: Shop) -> None:
    await show(call, about_text(shop), kb.home_button())


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, db: Database) -> None:
    await show_categories(call, db)


@router.callback_query(F.data.startswith("cat:"))
async def cb_category(call: CallbackQuery, db: Database) -> None:
    category_id = arg(call.data)
    names = {c.id: c.name for c in await db.list_categories()}
    if category_id not in names:
        await call.answer("Такой категории нет", show_alert=True)
        return
    products = await db.list_products(category_id)
    if not products:
        await show(call, f"<b>{escape(names[category_id])}</b>\n\nСейчас всё разобрали 🙈",
                   kb.categories_kb(await db.list_categories()))
        return
    await show(call, f"<b>{escape(names[category_id])}</b>", kb.products_kb(products))


@router.callback_query(F.data.startswith("prod:"))
async def cb_product(call: CallbackQuery, db: Database) -> None:
    await show_product(call, db, arg(call.data))


@router.callback_query(F.data.startswith("add:"))
async def cb_add(call: CallbackQuery, db: Database) -> None:
    option_id = to_int(arg(call.data))
    product = await db.product_of_option(option_id) if option_id else None
    if option_id is None or product is None:
        await call.answer("Позиция не найдена", show_alert=True)
        return
    if not await db.cart_add(call.from_user.id, option_id):
        reason = ("Нет в наличии" if not product.available
                  else f"В корзине не больше {CART_LINE_LIMIT} позиций")
        await call.answer(reason, show_alert=True)
        return
    option = next(o for o in product.options if o.id == option_id)
    await show_product(call, db, product.id, toast=f"Добавлено: {product.name} · {option.label}")


# ---------------------------------------------------------------- корзина

@router.callback_query(F.data == "cart")
async def cb_cart(call: CallbackQuery, db: Database) -> None:
    await show_cart(call, db)


@router.callback_query(F.data.startswith(("inc:", "dec:", "del:")))
async def cb_change_qty(call: CallbackQuery, db: Database) -> None:
    kind, _, raw = (call.data or "").partition(":")
    option_id = to_int(raw)
    if option_id is None:
        await call.answer()
        return
    delta = {"inc": 1, "dec": -1, "del": -CART_QTY_LIMIT}[kind]
    ok = await db.cart_add(call.from_user.id, option_id, delta)
    await show_cart(call, db, toast=None if ok else "Не получилось изменить корзину")


@router.callback_query(F.data == "clear")
async def cb_clear(call: CallbackQuery, db: Database) -> None:
    await db.cart_clear(call.from_user.id)
    await show_cart(call, db, toast="Корзина очищена")


# ---------------------------------------------------------------- оформление

@router.callback_query(F.data == "checkout")
async def cb_checkout(call: CallbackQuery, db: Database) -> None:
    lines = [l for l in await db.cart_lines(call.from_user.id) if l.available]
    if not lines:
        await show_cart(call, db, toast="Нечего оформлять")
        return
    await show(call, "🕒 <b>Когда будете готовы забрать заказ?</b>", kb.pickup_kb())


@router.callback_query(F.data.startswith("pick:"))
async def cb_pickup(call: CallbackQuery) -> None:
    code = arg(call.data)
    if pickup_label(code) is None:
        await call.answer("Выберите время из списка", show_alert=True)
        return
    await show(call, "💳 <b>Как будете платить?</b>", kb.payment_kb(code))


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery, bot: Bot, db: Database, shop: Shop, settings: Settings) -> None:
    _, _, rest = (call.data or "").partition(":")
    code, _, method = rest.partition(":")
    pickup = pickup_label(code)
    if pickup is None or method not in ("card", "cash"):
        await call.answer("Что-то пошло не так, начните оформление заново", show_alert=True)
        return
    try:
        order = await db.create_order(call.from_user.id, call.from_user.full_name,
                                      payment=method, pickup=pickup)
    except EmptyCart:
        await show_cart(call, db, toast="Корзина пуста")
        return

    if method == "card":
        await request_payment(call, order, shop, settings)
        return
    await show(call, "✅ <b>Заказ принят!</b>\n\n" + order_text(order) +
               f"\n\nЖдём вас: {shop.address}", kb.home_button())
    await notify_new_order(bot, settings, order)


# ---------------------------------------------------------------- мои заказы

@router.callback_query(F.data == "orders")
async def cb_orders(call: CallbackQuery, db: Database) -> None:
    await show_orders(call, db)


@router.callback_query(F.data.startswith("order:"))
async def cb_order(call: CallbackQuery, db: Database) -> None:
    order_id = to_int(arg(call.data))
    order = await db.get_order(order_id) if order_id else None
    if order is None or order.user_id != call.from_user.id:
        await call.answer("Заказ не найден", show_alert=True)
        return
    await show(call, order_text(order), kb.order_kb(order))


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(call: CallbackQuery, bot: Bot, db: Database, shop: Shop, settings: Settings) -> None:
    order_id = to_int(arg(call.data))
    order = await db.get_order(order_id) if order_id else None
    if order is None or order.user_id != call.from_user.id:
        await call.answer("Заказ не найден", show_alert=True)
        return
    if order.paid:
        await call.answer(f"Заказ уже оплачен — позвоните нам: {shop.phone}", show_alert=True)
        return
    # клиент отменяет сам только до того, как заказ взяли в работу
    updated = (await db.set_status(order.id, "cancelled")
               if order.status in ("awaiting_payment", "new") else None)
    if updated is None:
        await call.answer("Этот заказ уже нельзя отменить", show_alert=True)
        return
    await show(call, order_text(updated), kb.order_kb(updated), toast="Заказ отменён")
    if order.status == "new":   # админ о нём уже знает
        await notify_admins(bot, settings, f"✖️ Клиент отменил заказ №{order.id}.")
