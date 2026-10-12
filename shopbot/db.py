"""SQLite: каталог (с админскими правками), корзины, заказы, статистика."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .config import Shop

CART_QTY_LIMIT = 20    # штук одной позиции
CART_LINE_LIMIT = 15   # разных позиций в корзине

ACTIVE_STATUSES = ("new", "accepted", "ready")

# из какого статуса в какие можно перейти
TRANSITIONS: dict[str, frozenset[str]] = {
    "awaiting_payment": frozenset({"cancelled"}),   # в «new» переводит только mark_paid
    "new": frozenset({"accepted", "cancelled"}),
    "accepted": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"done", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id          TEXT PRIMARY KEY,
    category_id TEXT NOT NULL REFERENCES categories(id),
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    position    INTEGER NOT NULL,
    available   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS options (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL REFERENCES products(id),
    label      TEXT NOT NULL,
    price      INTEGER NOT NULL CHECK (price > 0),
    position   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_options_product ON options(product_id, position);
CREATE TABLE IF NOT EXISTS carts (
    user_id   INTEGER NOT NULL,
    option_id INTEGER NOT NULL REFERENCES options(id),
    qty       INTEGER NOT NULL CHECK (qty > 0),
    added_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, option_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    user_name  TEXT NOT NULL,
    status     TEXT NOT NULL,
    payment    TEXT NOT NULL,            -- card | cash
    paid       INTEGER NOT NULL DEFAULT 0,
    total      INTEGER NOT NULL,
    pickup     TEXT NOT NULL,
    charge_id  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL             -- UTC, ISO-8601
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, id);
CREATE TABLE IF NOT EXISTS order_items (
    order_id     INTEGER NOT NULL REFERENCES orders(id),
    product_name TEXT NOT NULL,
    label        TEXT NOT NULL,
    price        INTEGER NOT NULL,
    qty          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
"""


class EmptyCart(Exception):
    """В корзине нет доступных позиций."""


@dataclass(frozen=True)
class Category:
    id: str
    name: str


@dataclass(frozen=True)
class OptionRow:
    id: int
    label: str
    price: int


@dataclass(frozen=True)
class ProductRow:
    id: str
    category_id: str
    name: str
    description: str
    available: bool
    options: tuple[OptionRow, ...]

    @property
    def min_price(self) -> int:
        return min(o.price for o in self.options)


@dataclass(frozen=True)
class CartLine:
    option_id: int
    product_id: str
    product_name: str
    label: str
    price: int
    qty: int
    available: bool

    @property
    def subtotal(self) -> int:
        return self.price * self.qty


@dataclass(frozen=True)
class OrderItem:
    product_name: str
    label: str
    price: int
    qty: int


@dataclass(frozen=True)
class Order:
    id: int
    user_id: int
    user_name: str
    status: str
    payment: str
    paid: bool
    total: int
    pickup: str
    charge_id: str
    created_at: datetime
    items: tuple[OrderItem, ...]


@dataclass(frozen=True)
class Stats:
    orders: int
    revenue: int
    top_items: tuple[tuple[str, int], ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self._lock = asyncio.Lock()   # многошаговые записи не должны перемешиваться

    @classmethod
    async def open(cls, path: Path | str) -> "Database":
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # --- каталог ---

    async def sync_menu(self, shop: Shop) -> None:
        """Переносит menu.toml в базу. Существующие позиции не трогает — там могут быть правки админа."""
        async with self._lock:
            for c_pos, category in enumerate(shop.categories):
                await self.conn.execute(
                    "INSERT OR IGNORE INTO categories(id, name, position) VALUES (?, ?, ?)",
                    (category.id, category.name, c_pos))
                for p_pos, product in enumerate(category.products):
                    cur = await self.conn.execute(
                        "INSERT OR IGNORE INTO products(id, category_id, name, description, position) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (product.id, category.id, product.name, product.description, p_pos))
                    if cur.rowcount != 1:
                        continue
                    for o_pos, option in enumerate(product.options):
                        await self.conn.execute(
                            "INSERT INTO options(product_id, label, price, position) VALUES (?, ?, ?, ?)",
                            (product.id, option.label, option.price, o_pos))
            await self.conn.commit()

    async def list_categories(self) -> list[Category]:
        cur = await self.conn.execute("SELECT id, name FROM categories ORDER BY position")
        return [Category(r["id"], r["name"]) for r in await cur.fetchall()]

    async def _options_of(self, product_ids: list[str]) -> dict[str, tuple[OptionRow, ...]]:
        if not product_ids:
            return {}
        marks = ",".join("?" * len(product_ids))
        cur = await self.conn.execute(
            f"SELECT id, product_id, label, price FROM options WHERE product_id IN ({marks}) "
            "ORDER BY product_id, position", product_ids)
        grouped: dict[str, list[OptionRow]] = {}
        for r in await cur.fetchall():
            grouped.setdefault(r["product_id"], []).append(OptionRow(r["id"], r["label"], r["price"]))
        return {pid: tuple(rows) for pid, rows in grouped.items()}

    async def _products(self, where: str, params: tuple) -> list[ProductRow]:
        cur = await self.conn.execute(
            "SELECT id, category_id, name, description, available FROM products "
            f"WHERE {where} ORDER BY position", params)
        rows = await cur.fetchall()
        options = await self._options_of([r["id"] for r in rows])
        return [
            ProductRow(r["id"], r["category_id"], r["name"], r["description"],
                       bool(r["available"]), options.get(r["id"], ()))
            for r in rows
        ]

    async def list_products(self, category_id: str, *, only_available: bool = True) -> list[ProductRow]:
        where = "category_id = ?" + (" AND available = 1" if only_available else "")
        return await self._products(where, (category_id,))

    async def get_product(self, product_id: str) -> ProductRow | None:
        found = await self._products("id = ?", (product_id,))
        return found[0] if found else None

    async def product_of_option(self, option_id: int) -> ProductRow | None:
        cur = await self.conn.execute("SELECT product_id FROM options WHERE id = ?", (option_id,))
        row = await cur.fetchone()
        return await self.get_product(row["product_id"]) if row else None

    async def set_available(self, product_id: str, available: bool) -> bool:
        async with self._lock:
            cur = await self.conn.execute(
                "UPDATE products SET available = ? WHERE id = ?", (int(available), product_id))
            await self.conn.commit()
            return cur.rowcount == 1

    async def set_option_price(self, option_id: int, price: int) -> bool:
        if price <= 0:
            return False
        async with self._lock:
            cur = await self.conn.execute("UPDATE options SET price = ? WHERE id = ?", (price, option_id))
            await self.conn.commit()
            return cur.rowcount == 1

    # --- корзина ---

    async def cart_add(self, user_id: int, option_id: int, delta: int = 1) -> bool:
        """Меняет количество позиции. False — позицию нельзя добавить (нет такой, в стоп-листе, лимит)."""
        async with self._lock:
            cur = await self.conn.execute(
                "SELECT p.available FROM options o JOIN products p ON p.id = o.product_id WHERE o.id = ?",
                (option_id,))
            option = await cur.fetchone()
            if option is None or (delta > 0 and not option["available"]):
                return False

            cur = await self.conn.execute(
                "SELECT qty FROM carts WHERE user_id = ? AND option_id = ?", (user_id, option_id))
            existing = await cur.fetchone()
            current = existing["qty"] if existing else 0

            if current == 0 and delta > 0:
                cur = await self.conn.execute("SELECT COUNT(*) AS n FROM carts WHERE user_id = ?", (user_id,))
                if (await cur.fetchone())["n"] >= CART_LINE_LIMIT:
                    return False

            qty = max(0, min(CART_QTY_LIMIT, current + delta))
            if qty == 0:
                await self.conn.execute(
                    "DELETE FROM carts WHERE user_id = ? AND option_id = ?", (user_id, option_id))
            else:
                await self.conn.execute(
                    "INSERT INTO carts(user_id, option_id, qty, added_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id, option_id) DO UPDATE SET qty = excluded.qty",
                    (user_id, option_id, qty, _now()))
            await self.conn.commit()
            return True

    async def cart_lines(self, user_id: int) -> list[CartLine]:
        cur = await self.conn.execute(
            "SELECT c.option_id, p.id AS product_id, p.name AS product_name, o.label, o.price, "
            "       c.qty, p.available "
            "FROM carts c JOIN options o ON o.id = c.option_id JOIN products p ON p.id = o.product_id "
            "WHERE c.user_id = ? ORDER BY c.added_at, c.option_id", (user_id,))
        return [
            CartLine(r["option_id"], r["product_id"], r["product_name"], r["label"], r["price"],
                     r["qty"], bool(r["available"]))
            for r in await cur.fetchall()
        ]

    async def cart_clear(self, user_id: int) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM carts WHERE user_id = ?", (user_id,))
            await self.conn.commit()

    # --- заказы ---

    async def create_order(self, user_id: int, user_name: str, *, payment: str, pickup: str) -> Order:
        """Превращает корзину в заказ. Позиции из стоп-листа в заказ не попадают."""
        if payment not in ("card", "cash"):
            raise ValueError(f"неизвестный способ оплаты: {payment!r}")
        async with self._lock:
            lines = [line for line in await self.cart_lines(user_id) if line.available]
            if not lines:
                raise EmptyCart
            total = sum(line.subtotal for line in lines)
            status = "awaiting_payment" if payment == "card" else "new"
            cur = await self.conn.execute(
                "INSERT INTO orders(user_id, user_name, status, payment, paid, total, pickup, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (user_id, user_name, status, payment, total, pickup, _now()))
            order_id = cur.lastrowid
            await self.conn.executemany(
                "INSERT INTO order_items(order_id, product_name, label, price, qty) VALUES (?, ?, ?, ?, ?)",
                [(order_id, l.product_name, l.label, l.price, l.qty) for l in lines])
            await self.conn.execute("DELETE FROM carts WHERE user_id = ?", (user_id,))
            await self.conn.commit()
        return await self.get_order(order_id)

    async def _orders(self, where: str, params: tuple, tail: str = "") -> list[Order]:
        cur = await self.conn.execute(f"SELECT * FROM orders WHERE {where} {tail}", params)
        rows = await cur.fetchall()
        if not rows:
            return []
        marks = ",".join("?" * len(rows))
        cur = await self.conn.execute(
            f"SELECT * FROM order_items WHERE order_id IN ({marks}) ORDER BY rowid",
            [r["id"] for r in rows])
        items: dict[int, list[OrderItem]] = {}
        for r in await cur.fetchall():
            items.setdefault(r["order_id"], []).append(
                OrderItem(r["product_name"], r["label"], r["price"], r["qty"]))
        return [
            Order(r["id"], r["user_id"], r["user_name"], r["status"], r["payment"], bool(r["paid"]),
                  r["total"], r["pickup"], r["charge_id"], datetime.fromisoformat(r["created_at"]),
                  tuple(items.get(r["id"], ())))
            for r in rows
        ]

    async def get_order(self, order_id: int) -> Order | None:
        found = await self._orders("id = ?", (order_id,))
        return found[0] if found else None

    async def user_orders(self, user_id: int, limit: int = 10) -> list[Order]:
        return await self._orders("user_id = ?", (user_id,), f"ORDER BY id DESC LIMIT {int(limit)}")

    async def orders_by_status(self, statuses: tuple[str, ...]) -> list[Order]:
        marks = ",".join("?" * len(statuses))
        return await self._orders(f"status IN ({marks})", tuple(statuses), "ORDER BY id")

    async def mark_paid(self, order_id: int, charge_id: str) -> bool:
        """awaiting_payment → new. Повторный вызов (повторный webhook/платёж) ничего не меняет."""
        async with self._lock:
            cur = await self.conn.execute(
                "UPDATE orders SET status = 'new', paid = 1, charge_id = ? "
                "WHERE id = ? AND status = 'awaiting_payment' AND paid = 0", (charge_id, order_id))
            await self.conn.commit()
            return cur.rowcount == 1

    async def set_status(self, order_id: int, new_status: str) -> Order | None:
        """Меняет статус, только если переход разрешён. None — переход невозможен."""
        sources = [s for s, targets in TRANSITIONS.items() if new_status in targets]
        if not sources:
            return None
        marks = ",".join("?" * len(sources))
        async with self._lock:
            cur = await self.conn.execute(
                f"UPDATE orders SET status = ? WHERE id = ? AND status IN ({marks})",
                (new_status, order_id, *sources))
            await self.conn.commit()
            if cur.rowcount != 1:
                return None
        return await self.get_order(order_id)

    # --- статистика ---

    async def stats(self, start: datetime, end: datetime) -> Stats:
        """Заказы за [start, end): без отменённых и неоплаченных картой."""
        where = ("o.created_at >= ? AND o.created_at < ? "
                 "AND o.status NOT IN ('cancelled', 'awaiting_payment')")
        params = (_utc(start), _utc(end))
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(o.total), 0) AS revenue "
            f"FROM orders o WHERE {where}", params)
        row = await cur.fetchone()
        cur = await self.conn.execute(
            "SELECT i.product_name AS name, SUM(i.qty) AS qty FROM order_items i "
            f"JOIN orders o ON o.id = i.order_id WHERE {where} "
            "GROUP BY i.product_name ORDER BY qty DESC, name LIMIT 3", params)
        top = tuple((r["name"], r["qty"]) for r in await cur.fetchall())
        return Stats(orders=row["n"], revenue=row["revenue"], top_items=top)
