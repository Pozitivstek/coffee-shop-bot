"""Сборка диспетчера и запуск бота."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from . import admin, customer, payments
from .config import Settings, Shop, load_settings, load_shop
from .db import Database

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="menu", description="Меню кофейни"),
    BotCommand(command="cart", description="Корзина"),
    BotCommand(command="orders", description="Мои заказы"),
]


def build_dispatcher(db: Database, shop: Shop, settings: Settings) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), db=db, shop=shop, settings=settings)
    # платежи и админка раньше клиентских обработчиков: у них более узкие фильтры
    dp.include_routers(payments.router, admin.router, customer.router)

    @dp.error()
    async def on_error(event: ErrorEvent) -> bool:
        log.error("необработанная ошибка в обработчике", exc_info=event.exception)
        call = event.update.callback_query
        if call is not None:
            try:
                await call.answer("Что-то пошло не так. Попробуйте ещё раз.", show_alert=True)
            except Exception:
                log.debug("не удалось ответить на callback после ошибки", exc_info=True)
        return True

    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if not settings.bot_token:
        raise SystemExit("Не задан BOT_TOKEN: скопируйте .env.example в .env и впишите токен от @BotFather")
    shop = load_shop(settings.menu_file)
    db = await Database.open(settings.db_path)
    await db.sync_menu(shop)

    if not settings.admin_ids:
        log.warning("ADMIN_IDS пуст: админка недоступна, уведомления о заказах не придут. "
                    "Узнайте свой id командой /id у бота.")
    log.info("оплата: %s", "Telegram Payments (токен провайдера задан)" if settings.real_payments
             else "тестовая заглушка (PAYMENT_PROVIDER_TOKEN не задан)")

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(db, shop, settings)
    try:
        await bot.set_my_commands(COMMANDS)
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


def run() -> None:
    asyncio.run(main())
