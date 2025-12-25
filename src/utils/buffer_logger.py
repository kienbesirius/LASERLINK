# src/utils/buffer_logger.py
from __future__ import annotations
import sys
import logging
import threading
from typing import List, Tuple

_fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_datefmt = "%Y-%m-%d %H:%M:%S"

class ListLogHandler(logging.Handler):
    def __init__(self, buffer: List[str], max_buffer: int = 500, lock: threading.RLock | None = None):
        super().__init__()
        self._buffer = buffer
        self._max_buffer = max_buffer
        self._lock = lock or threading.RLock()

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            with self._lock:
                self._buffer.append(msg)
                if self._max_buffer and len(self._buffer) > self._max_buffer:
                    extra = len(self._buffer) - self._max_buffer
                    del self._buffer[:extra]
        except Exception:
            self.handleError(record)

def build_log_buffer(name: str = "LASERLINK", level=logging.DEBUG, *, max_buffer: int = 500) -> Tuple[logging.Logger, List[str]]:
    logger = logging.getLogger(name=name)

    if getattr(logger, "_laserlink_inited", False):
        return logger, getattr(logger, "_laserlink_buffer")

    logger.setLevel(level)
    logger.propagate = False

    log_buffer: List[str] = []
    lock = threading.RLock()

    fmt = logging.Formatter(fmt=_fmt, datefmt=_datefmt)

    list_handler = ListLogHandler(log_buffer, max_buffer=max_buffer, lock=lock)
    list_handler.setFormatter(fmt)
    list_handler.setLevel(level)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    stdout_handler.setLevel(level)

    logger.addHandler(list_handler)
    logger.addHandler(stdout_handler)

    logger._laserlink_inited = True
    logger._laserlink_buffer = log_buffer
    logger._laserlink_lock = lock   # <-- GUI sẽ dùng lock này

    return logger, log_buffer


# # src.utils.buffer_logger.py
# import sys
# import logging
# from typing import List, Tuple

# _fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
# _datefmt = "%Y-%m-%d %H:%M:%S"

# class ListLogHandler(logging.Handler):
#     def __init__(self, buffer: List[str], max_buffer: int = 500):
#         super().__init__()
#         self._buffer = buffer
#         self._max_buffer = max_buffer

#     def emit(self, record: logging.LogRecord):
#         try:
#             msg = self.format(record)
#             self._buffer.append(msg)
#             # trim to avoid unlimited growth
#             if self._max_buffer and len(self._buffer) > self._max_buffer:
#                 extra = len(self._buffer) - self._max_buffer
#                 del self._buffer[:extra]
#         except Exception:
#             self.handleError(record)

# def build_log_buffer(
#     name: str = "LASERLINK",
#     level=logging.DEBUG,
#     *,
#     max_buffer: int = 500,
# ) -> Tuple[logging.Logger, List[str]]:
#     logger = logging.getLogger(name=name)

#     # ---- prevent duplicate handlers if called multiple times ----
#     if getattr(logger, "_laserlink_inited", False):
#         # upgrade max_buffer if needed
#         try:
#             for h in logger.handlers:
#                 if isinstance(h, ListLogHandler):
#                     h._max_buffer = max(h._max_buffer, max_buffer)
#         except Exception:
#             pass
#         return logger, getattr(logger, "_laserlink_buffer")

#     logger.setLevel(level)
#     logger.propagate = False  # avoid double print via root logger

#     log_buffer: List[str] = []

#     fmt = logging.Formatter(fmt=_fmt, datefmt=_datefmt)

#     list_handler = ListLogHandler(log_buffer, max_buffer=max_buffer)
#     list_handler.setFormatter(fmt)
#     list_handler.setLevel(level)

#     stdout_handler = logging.StreamHandler(sys.stdout)
#     stdout_handler.setFormatter(fmt)
#     stdout_handler.setLevel(level)

#     logger.addHandler(list_handler)
#     logger.addHandler(stdout_handler)

#     # stash to logger object
#     logger._laserlink_inited = True
#     logger._laserlink_buffer = log_buffer

#     return logger, log_buffer