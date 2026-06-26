import logging
import sys


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("zoe")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger


logger = configure_logging()
