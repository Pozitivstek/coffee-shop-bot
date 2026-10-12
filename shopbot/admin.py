"""Админка в Telegram: заказы, статусы, стоп-лист, цены, выручка за день.

Доступ — по user id из ADMIN_IDS. callback_data:
a:home | a:new | a:work | a:today | a:ord:<id> | a:st:<id>:<status>
a:stop → a:stopcat:<cat> → a:tog:<product> | a:price → a:pcat:<cat> → a:pprod:<product> → a:popt:<option>
"""
import logging
from datetime import datetime, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from . import keyboards as kb
from .callbacks import part, to_int
from .config import Settings, Shop
from .db import ACTIVE_STATUSES, Database
from .notify import safe_send
from .texts import money, order_text, status_message

log = logging.getLogger(__name__)

MAX_PRICE = 100_000


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        return event.from_user is not None and event.from_user.id in settings.admin_ids


class PriceEdit(StatesGroup):
    waiting = State()


router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def arg(data: str | None, index: int = 2) -> str:
    return part(data, index)


async def render(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None,
                 *, answer: bool = True) -> None:
    if isinstance(call.message, Message):
        try:
            await call.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                raise
    else:
        await call.bot.send_message(call.from_user.id, text, reply_markup=markup)
    if answer:
        await call.answer()


async def home_view(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    new = await db.orders_by_status(("new",))
    work = await db.orders_by_status(("accepted", "ready"))
    return "🛠 <b>Админка</b>", kb.admin_home_kb(len(new), len(work))


@router.message(Command("admin"))
async def cmd_admin(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    text, markup = await home_view(db)
    await message.answer(text, reply_markup=markup)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.callback_query(F.data == "a:home")
async def cb_home(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    text, markup = await home_view(db)
    await render(call, text, markup)


# ---------------------------------------------------------------- заказы

@router.callback_query(F.data.in_({"a:new", "a:work"}))
async def cb_orders(call: CallbackQuery, db: Database) -> None:
    statuses = ("new",) if call.data == "a:new" else ("accepted", "ready")
    orders = await db.orders_by_status(statuses)
    title = "🆕 <b>Новые заказы</b>" if call.data == "a:new" else "🔥 <b>В работе</b>"
    if not orders:
        title += "\n\nПусто."
    await render(call, title, kb.admin_orders_kb(orders))


@router.callback_query(F.data.startswith("a:ord:"))
async def cb_order(call: CallbackQuery, db: Database) -> None:
    order_id = to_int(arg(call.data))
    order = await db.get_order(order_id) if order_id is not None else None
    if order is None:
        await call.answer("Заказ не найден", show_alert=True)
        return
    await render(call, order_text(order, admin=True), kb.admin_order_kb(order))


@router.callback_query(F.data.startswith("a:st:"))
async def cb_status(call: CallbackQuery, bot: Bot, db: Database, shop: Shop) -> None:
    order_id, status = to_int(arg(call.data)), arg(call.data, 3)
    if order_id is None:
        await call.answer()
        return
    updated = await db.set_status(order_id, status)
    if updated is None:
        await call.answer("Так статус уже не поменять", show_alert=True)
        current = await db.get_order(order_id)
        if current is not None:   # обновим карточку: возможно, статус уже поменял другой админ
            await render(call, order_text(current, admin=True), kb.admin_order_kb(current), answer=False)
        return
    await render(call, order_text(updated, admin=True), kb.admin_order_kb(updated))
    await safe_send(bot, updated.user_id, status_message(updated, shop))


# ---------------------------------------------------------------- статистика

@router.callback_query(F.data == "a:today")
async def cb_today(call: CallbackQuery, db: Database, shop: Shop) -> None:
    start = datetime.now(shop.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = await db.stats(start, start + timedelta(days=1))
    rows = [f"📊 <b>Сегодня, {start:%d.%m}</b>\n",
            f"Заказов: <b>{stats.orders}</b>",
            f"Выручка: <b>{money(stats.revenue)}</b>"]
    if stats.orders:
        rows.append(f"Средний чек: {money(round(stats.revenue / stats.orders))}")
    if stats.top_items:
        rows.append("\nПопулярное:")
        rows += [f"• {escape(name)} — {qty} шт" for name, qty in stats.top_items]
    rows.append("\n<i>Без отменённых и неоплаченных картой заказов.</i>")
    await render(call, "\n".join(rows), kb.admin_orders_kb([]))


# ---------------------------------------------------------------- стоп-лист

async def stop_view(db: Database, category_id: str) -> tuple[str, InlineKeyboardMarkup]:
    products = await db.list_products(category_id, only_available=False)
    return ("🚫 <b>Стоп-лист</b>\nНажмите на позицию, чтобы убрать её из меню или вернуть:",
            kb.admin_stop_kb(products, category_id))


@router.callback_query(F.data == "a:stop")
async def cb_stop(call: CallbackQuery, db: Database) -> None:
    await render(call, "🚫 <b>Стоп-лист</b>\nВыберите категорию:",
                 kb.admin_categories_kb(await db.list_categories(), "a:stopcat"))


@router.callback_query(F.data.startswith("a:stopcat:"))
async def cb_stop_category(call: CallbackQuery, db: Database) -> None:
    await render(call, *await stop_view(db, arg(call.data)))


@router.callback_query(F.data.startswith("a:tog:"))
async def cb_toggle(call: CallbackQuery, db: Database) -> None:
    product = await db.get_product(arg(call.data))
    if product is None:
        await call.answer("Позиция не найдена", show_alert=True)
        return
    await db.set_available(product.id, not product.available)
    await render(call, *await stop_view(db, product.category_id))


# ---------------------------------------------------------------- цены

@router.callback_query(F.data == "a:price")
async def cb_price(call: CallbackQuery, db: Database) -> None:
    await render(call, "💵 <b>Цены</b>\nВыберите категорию:",
                 kb.admin_categories_kb(await db.list_categories(), "a:pcat"))


@router.callback_query(F.data.startswith("a:pcat:"))
async def cb_price_category(call: CallbackQuery, db: Database) -> None:
    products = await db.list_products(arg(call.data), only_available=False)
    await render(call, "💵 <b>Цены</b>\nВыберите позицию:", kb.admin_price_products_kb(products))


@router.callback_query(F.data.startswith("a:pprod:"))
async def cb_price_product(call: CallbackQuery, db: Database) -> None:
    product = await db.get_product(arg(call.data))
    if product is None:
        await call.answer("Позиция не найдена", show_alert=True)
        return
    await render(call, f"💵 <b>{escape(product.name)}</b>\nЧью цену меняем?",
                 kb.admin_price_options_kb(product))


@router.callback_query(F.data.startswith("a:popt:"))
async def cb_price_option(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    option_id = to_int(arg(call.data))
    product = await db.product_of_option(option_id) if option_id is not None else None
    option = next((o for o in product.options if o.id == option_id), None) if product else None
    if product is None or option is None:
        await call.answer("Вариант не найден", show_alert=True)
        return
    await state.set_state(PriceEdit.waiting)
    await state.update_data(option_id=option.id, title=f"{product.name} · {option.label}")
    await render(call, f"💵 <b>{escape(product.name)} · {escape(option.label)}</b>\n"
                       f"Сейчас: {money(option.price)}\n\nОтправьте новую цену числом в рублях.",
                 kb.cancel_edit_kb())


@router.message(PriceEdit.waiting, F.text)
async def new_price(message: Message, db: Database, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not 0 < int(text) <= MAX_PRICE:
        await message.answer(f"Нужно целое число от 1 до {MAX_PRICE}. Попробуйте ещё раз или /cancel.")
        return
    data = await state.get_data()
    if not await db.set_option_price(data["option_id"], int(text)):
        await message.answer("Не получилось сохранить: вариант не найден.")
    else:
        await message.answer(f"✅ {escape(data['title'])}: теперь {money(int(text))}.")
    await state.clear()
    home, markup = await home_view(db)
    await message.answer(home, reply_markup=markup)
