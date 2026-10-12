"""Настройки из .env и меню кофейни из menu.toml."""
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")   # id попадает в callback_data (лимит Telegram — 64 байта)


class MenuError(ValueError):
    """Ошибка в menu.toml."""


@dataclass(frozen=True)
class Option:
    label: str
    price: int


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    description: str
    options: tuple[Option, ...]


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    products: tuple[Product, ...]


@dataclass(frozen=True)
class Shop:
    name: str
    address: str
    phone: str
    hours: str
    tz: ZoneInfo
    about: str
    categories: tuple[Category, ...]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    payment_token: str   # пусто → тестовая оплата-заглушка
    db_path: Path
    menu_file: Path

    @property
    def real_payments(self) -> bool:
        return bool(self.payment_token)


def _text(table: dict, key: str, where: str, *, allow_empty: bool = False) -> str:
    value = table.get(key)
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise MenuError(f"{where}: поле {key!r} должно быть непустой строкой")
    return value.strip()


def _ident(table: dict, where: str) -> str:
    value = _text(table, "id", where)
    if not ID_RE.match(value):
        raise MenuError(f"{where}: id {value!r} должен состоять из a-z, 0-9 и «-», до 32 символов")
    return value


def _option(raw: dict, where: str) -> Option:
    price = raw.get("price")
    if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
        raise MenuError(f"{where}: цена должна быть целым числом больше нуля, а не {price!r}")
    return Option(label=_text(raw, "label", where), price=price)


def _product(raw: dict, where: str, seen: set[str]) -> Product:
    product_id = _ident(raw, where)
    if product_id in seen:
        raise MenuError(f"{where}: id {product_id!r} встречается дважды")
    seen.add(product_id)
    raw_options = raw.get("option") or []
    if not raw_options:
        raise MenuError(f"{where} ({product_id}): нужна хотя бы одна [[category.product.option]]")
    return Product(
        id=product_id,
        name=_text(raw, "name", f"{where} ({product_id})"),
        description=_text(raw, "description", f"{where} ({product_id})", allow_empty=True),
        options=tuple(_option(o, f"{where} ({product_id}) вариант") for o in raw_options),
    )


def load_shop(path: Path | str) -> Shop:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MenuError(f"Не удалось прочитать меню {path}: {exc}") from exc

    info = data.get("shop") or {}
    try:
        tz = ZoneInfo(_text(info, "timezone", "[shop]"))
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError
        raise MenuError(f"[shop]: неизвестный часовой пояс: {exc}") from exc

    seen_products: set[str] = set()
    seen_categories: set[str] = set()
    categories = []
    for raw in data.get("category") or []:
        category_id = _ident(raw, "[[category]]")
        if category_id in seen_categories:
            raise MenuError(f"[[category]]: id {category_id!r} встречается дважды")
        seen_categories.add(category_id)
        products = tuple(
            _product(p, f"категория {category_id}", seen_products) for p in raw.get("product") or []
        )
        if not products:
            raise MenuError(f"категория {category_id}: нет ни одной позиции")
        categories.append(Category(category_id, _text(raw, "name", f"категория {category_id}"), products))
    if not categories:
        raise MenuError("В меню нет ни одной категории")

    return Shop(
        name=_text(info, "name", "[shop]"),
        address=_text(info, "address", "[shop]"),
        phone=_text(info, "phone", "[shop]"),
        hours=_text(info, "hours", "[shop]"),
        tz=tz,
        about=_text(info, "about", "[shop]"),
        categories=tuple(categories),
    )


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ValueError(f"ADMIN_IDS: {part!r} — не числовой id Telegram")
        ids.add(int(part))
    return frozenset(ids)


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        admin_ids=_parse_ids(os.getenv("ADMIN_IDS", "")),
        payment_token=os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip(),
        db_path=Path(os.getenv("DB_PATH") or ROOT / "data" / "shop.db"),
        menu_file=Path(os.getenv("MENU_FILE") or ROOT / "menu.toml"),
    )
