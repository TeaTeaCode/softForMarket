from collections.abc import Awaitable, Callable
from typing import Any

from app.clients.suppliers.fragment import fragment
from app.clients.suppliers.smm_panel import smm_panel
from app.clients.suppliers.teateagram import teateagram

# Лямбды обязательны: они резолвят метод в момент вызова. Прямые ссылки на связанные
# методы захватились бы на импорте — и подмена клиента (в т.ч. в тестах) перестала бы работать.
# У Fragment метод называется иначе — get_task_status вместо get_supplier_status.
STATUS_FETCHERS: dict[str, Callable[[str], Awaitable[Any]]] = {
    "fragment": lambda oid: fragment.get_task_status(oid),
    "smm_panel": lambda oid: smm_panel.get_supplier_status(oid),
    "teateagram": lambda oid: teateagram.get_supplier_status(oid),
}


async def fetch_status(supplier: str, order_id: str) -> Any:
    """Статус заказа у его поставщика."""
    fetcher = STATUS_FETCHERS.get(supplier)
    if fetcher is None:
        raise RuntimeError(f"Неизвестный поставщик: {supplier!r}, order_id={order_id}")
    return await fetcher(order_id)
