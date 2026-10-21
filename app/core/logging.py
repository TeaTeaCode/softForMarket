import logging
from pathlib import Path
import sys

from loguru import logger

from app.core.config.config import config


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = str(record.levelno)

        frame = logging.currentframe()
        depth = 2

        if frame is not None:
            while frame.f_code.co_filename == logging.__file__:
                if frame.f_back is None:
                    break
                frame = frame.f_back
                depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(file_override: Path | None = None) -> None:
    log_config = config.logging
    file_path = file_override if file_override is not None else log_config.file

    logging.root.handlers = [InterceptHandler()]
    logging.root.setLevel(log_config.console_level)

    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    logging.getLogger("httpcore").setLevel(logging.ERROR)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logging.getLogger("logging").setLevel(logging.ERROR)

    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True

    logger.configure(
        handlers=[
            {
                "sink": sys.stdout,
                "level": log_config.console_level,
                "format": log_config.console_format,
                "backtrace": log_config.backtrace,
                "diagnose": log_config.diagnose,
                "enqueue": log_config.enqueue,
            },
            {
                "sink": Path(file_path),
                "level": log_config.file_level,
                "rotation": log_config.rotation,
                "retention": f"{log_config.retention} days",
                "compression": log_config.compression,
                "format": log_config.file_format,
                "backtrace": log_config.backtrace,
                "diagnose": log_config.diagnose,
                "enqueue": log_config.enqueue,
            },
        ]
    )
