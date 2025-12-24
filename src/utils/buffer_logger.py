# src.utils.buffer_logger.py
import sys
import logging
from typing import List, Tuple

_fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_datefmt = "%Y-%m-%d %H:%M:%S"

class ListLogHandler(logging.Handler):
    def __init__(self, buffer: List[str]):
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self._buffer.append(msg)
        except Exception as e:
            self.handleError(record)

def build_log_buffer(name: str = "LASERLINK", level = logging.DEBUG) -> Tuple[logging.Logger, List[str]]:
    logger = logging.getLogger(name=name)
    logger.setLevel(level)
    log_buffer: List[str] = []

    log_formatter = logging.Formatter(fmt=_fmt, datefmt=_datefmt)
    listLogHandler = ListLogHandler(log_buffer)
    listLogHandler.setFormatter(log_formatter)
    listLogHandler.setLevel(level)

    stdoutLogHandler = logging.StreamHandler(sys.stdout)
    stdoutLogHandler.setFormatter(log_formatter)
    stdoutLogHandler.setLevel(level)

    logger.addHandler(listLogHandler)
    logger.addHandler(stdoutLogHandler)

    return logger, log_buffer
