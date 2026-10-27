from collections.abc import Awaitable, Callable
from typing import Any

from app.clients.suppliers.fragment import fragment
from app.clients.suppliers.smm_panel import smm_panel
from app.clients.suppliers.teateagram import teateagram
from app.core.config.config import config
from app.core.config.settings import settings

# Лямбды обязательны: они резолвят метод в момент вызова. Прямые ссылки на связанные
# методы захватились бы на импорте — и подмена клиента (в т.ч. в тестах) перестала бы работать.
# У Fragment метод называется иначе — get_task_status вместо get_supplier_status.
STATUS_FETCHERS: dict[str, Callable[[str], Awaitable[Any]]] = {
    "fragment": lambda oid: fragment.get_task_status(oid),
    "smm_panel": lambda oid: smm_panel.get_supplier_status(oid),
    "teateagram": lambda oid: teateagram.get_supplier_status(oid),
}

ORDER_CREATORS: dict[str, Callable[[int | str, str, int], Awaitable[dict[str, Any]]]] = {
    "smm_panel": lambda service, link, qty: smm_panel.create_supplier_order(str(service), link, qty),
    "teateagram": lambda service, link, qty: teateagram.create_supplier_order(int(service), link, qty),
}


async def fetch_status(supplier: str, order_id: str) -> Any:
    """Статус заказа у его поставщика."""
    fetcher = STATUS_FETCHERS.get(supplier)
    if fetcher is None:
        raise RuntimeError(f"Неизвестный поставщик: {supplier!r}, order_id={order_id}")
    return await fetcher(order_id)


def resolve_supplier_service(service_name: str | None) -> tuple[str, int | str | None]:
    """Поставщик новых заказов и услуга у него; None — услуга не заведена."""
    supplier = settings.ORDER_SUPPLIER
    if not service_name:
        return supplier, None
    if supplier == "teateagram":
        return supplier, config.services.teateagram_service_ids.get(service_name)
    return supplier, service_name


async def create_supplier_order(supplier: str, service: int | str, link: str, quantity: int) -> dict[str, Any]:
    """Размещает заказ у поставщика."""
    creator = ORDER_CREATORS.get(supplier)
    if creator is None:
        raise RuntimeError(f"Неизвестный поставщик: {supplier!r}")
    return await creator(service, link, quantity)
