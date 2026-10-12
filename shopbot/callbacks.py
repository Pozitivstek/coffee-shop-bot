"""Разбор callback_data. Данные приходят от клиента и могут быть подделаны — проверяем всё."""

MAX_INT_DIGITS = 11   # влезает в SQLite INTEGER с запасом


def part(data: str | None, index: int) -> str:
    """Часть callback_data после разбиения по «:»; пустая строка, если её нет."""
    pieces = (data or "").split(":")
    return pieces[index] if len(pieces) > index else ""


def to_int(raw: str) -> int | None:
    """Целое неотрицательное число или None."""
    return int(raw) if raw.isdigit() and len(raw) <= MAX_INT_DIGITS else None
