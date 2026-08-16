import os
from pathlib import Path

from pydantic import BaseModel, ValidationError
import yaml


class LoggingConfig(BaseModel):
    file: Path
    # отдельный файл воркера: на Windows ротация ломается, если два процесса держат один лог
    worker_file: Path
    console_level: str
    file_level: str
    console_format: str
    file_format: str
    rotation: str
    retention: int
    compression: str
    backtrace: bool
    diagnose: bool
    enqueue: bool


class ServicesConfig(BaseModel):
    # goods_id площадки → человекочитаемое название товара
    goods_human: dict[str, str]
    # площадка → {goods_id с фиксированным сроком → service_name поставщика}
    fixed_product_to_service: dict[str, dict[str, str]]
    # площадка → набор goods_id с выбором срока (диапазон 1–180 дней)
    range_products: dict[str, set[str]]
    # площадка → {goods_id → тип покупки Fragment: stars | premium}
    fragment_products: dict[str, dict[str, str]]
    # количество дней → service_name поставщика
    days_to_service: dict[int, str]
    # площадка → footer на странице статуса
    footer_by_platform: dict[str, str]


class Config(BaseModel):
    logging: LoggingConfig
    services: ServicesConfig


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Конфигурационный файл {path} не найден")
    with path.open("r", encoding="utf-8") as f:
        return yaml.full_load(f) or {}


def load_config(yaml_path: Path | None = None, services_path: Path | None = None) -> Config:
    if yaml_path is None:
        config_path_env = os.getenv("CONFIG_PATH")
        yaml_path = Path(config_path_env) if config_path_env else Path("config/config.yaml")

    if services_path is None:
        services_path_env = os.getenv("SERVICES_PATH")
        services_path = Path(services_path_env) if services_path_env else Path("config/services.yaml")

    config_dict = _load_yaml(yaml_path)
    config_dict["services"] = _load_yaml(services_path)

    return Config.model_validate(config_dict)


try:
    config = load_config()
except (FileNotFoundError, ValidationError) as e:
    import sys

    print(f"ОШИБКА конфигурации: {e}", file=sys.stderr)
    print(
        "Убедитесь, что файлы config/config.yaml и config/services.yaml существуют и заполнены ",
        file=sys.stderr,
    )
    sys.exit(1)
