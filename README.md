# ☕ Coffee Shop Bot

**RU** — Telegram-бот кофейни «Ягода»: меню, корзина, оформление с выбором времени, оплата картой (Telegram Payments, тестовый режим) или при получении, админка прямо в Telegram.
**EN** — A Telegram bot for a coffee shop: menu, cart, pickup-time checkout, card payments (Telegram Payments, test mode) or pay-on-pickup, and an admin panel inside Telegram.

Python 3.11+ · aiogram 3 · SQLite (aiosqlite) · 66 tests, 82% coverage

## Возможности / Features

**Клиент / Customer**
- Каталог по категориям, варианты объёма и цены, корзина с «+ / −», лимиты количества
- Оформление: время выдачи → оплата картой онлайн или при получении
- «Мои заказы»: статусы, повторная оплата, отмена до начала приготовления
- Уведомления: заказ принят → готов → выдан / отменён

**Администратор / Admin** (`/admin`)
- Новые и текущие заказы, кнопки статусов (принять → готов → выдан, отмена)
- Стоп-лист: убрать позицию из меню одним нажатием
- Правка цен прямо в чате
- Выручка, число заказов и топ позиций за сегодня

**Платежи / Payments**
- Есть `PAYMENT_PROVIDER_TOKEN` → настоящий инвойс Telegram Payments (проверка суммы и владельца в `pre_checkout_query`, идемпотентная обработка `successful_payment`)
- Нет токена → тестовая заглушка «Оплатить (тест)», деньги не списываются

## Запуск / Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # впишите BOT_TOKEN и ADMIN_IDS
.venv/bin/python -m shopbot
```

1. Создайте бота у [@BotFather](https://t.me/BotFather), токен — в `BOT_TOKEN`.
2. Напишите боту `/id`, свой ID — в `ADMIN_IDS` (несколько через запятую).
3. Для настоящих (тестовых) платежей: @BotFather → ваш бот → **Payments** → провайдер → **Test**, токен — в `PAYMENT_PROVIDER_TOKEN`.

## Тесты / Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

Сквозные тесты (`tests/test_bot_flow.py`) прогоняют настоящие обработчики через диспетчер aiogram с подменённым Telegram API.

## Устройство / Structure

```
menu.toml            меню и данные кофейни (переносится в БД при старте)
shopbot/
  config.py          .env и валидация menu.toml
  db.py              каталог, корзины, заказы, статусы, статистика (SQLite)
  customer.py        клиентский поток
  payments.py        инвойсы, pre_checkout, тестовая оплата
  admin.py           админка и диалог правки цены
  keyboards.py       inline-клавиатуры
  texts.py           тексты (HTML, экранирование)
  callbacks.py       безопасный разбор callback_data
  notify.py          уведомления админам/клиентам
  bot.py             сборка диспетчера и запуск
tests/
```

Меню в `menu.toml` — только начальные данные: новые позиции подхватываются по `id`, а правки цен и стоп-лист из админки при перезапуске не затираются.
