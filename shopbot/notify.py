"""Рассылка администраторам и клиентам: ошибки доставки не должны ронять обработчик."""
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from .config import Settings
from .db import Order
from .keyboards import admin_order_kb
from .texts import order_text

log = logging.getLogger(__name__)


async def safe_send(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
        return True
    except Exception:   # пользователь заблокировал бота, чат недоступен и т. п.
        log.warning("не удалось отправить сообщение в чат %s", chat_id, exc_info=True)
        return False


async def notify_admins(bot: Bot, settings: Settings, text: str,
                        markup: InlineKeyboardMarkup | None = None) -> None:
    if not settings.admin_ids:
        log.info("ADMIN_IDS не задан, уведомление пропущено: %s", text.splitlines()[0])
        return
    for admin_id in settings.admin_ids:
        await safe_send(bot, admin_id, text, markup)


async def notify_new_order(bot: Bot, settings: Settings, order: Order) -> None:
    await notify_admins(bot, settings, "🔔 <b>Новый заказ!</b>\n\n" + order_text(order, admin=True),
                        admin_order_kb(order))
