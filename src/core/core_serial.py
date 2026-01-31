import time
import re
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Pattern, Tuple

import serial


@dataclass
class RxLine:
    seq: int
    t: float
    text: str


class SFCComReader:
    """
    - 1 thread đọc liên tục (duy nhất)
    - send() chỉ write, KHÔNG đọc
    - send_and_collect(): đặt mốc seq -> send -> chờ idle/regex -> lấy lines sau mốc
    """

    def __init__(
        self,
        port: str,
        baudrate: int,
        *,
        keep_lines: int = 2000,
        read_sleep: float = 0.005,
        decode: str = "latin-1",   # 1:1, dễ debug byte lạ
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.keep_lines = keep_lines
        self.read_sleep = read_sleep
        self.decode = decode
        self.log = log

        self._ser: Optional[serial.Serial] = None
        self._stop = threading.Event()
        self._ready = threading.Event()

        self._write_lock = threading.Lock()

        self._rx_buf = bytearray()
        self._lines: Deque[RxLine] = deque(maxlen=keep_lines)
        self._seq = 0

        self._last_rx_time = 0.0
        self._data_evt = threading.Event()  # set mỗi khi có line mới

        self._th = threading.Thread(target=self._reader_loop, daemon=True)

    # ---------- lifecycle ----------
    def start(self) -> None:
        self._th.start()
        # chờ ready để sender không viết khi chưa open
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("SFCComReader: open port timeout")

    def stop(self) -> None:
        self._stop.set()
        self._th.join(timeout=1.0)
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass

    def is_ready(self) -> bool:
        return self._ready.is_set()

    # ---------- public API ----------
    def snapshot_seq(self) -> int:
        return self._seq

    def get_lines_since(self, seq0: int) -> List[str]:
        # copy ra list để thread-safe (deque iteration ok vì GIL, nhưng ta copy nhanh)
        return [x.text for x in list(self._lines) if x.seq > seq0]

    def clear_input_buffer(self) -> None:
        """Xoá rác còn tồn trước khi bắt đầu 1 transaction (tuỳ use-case)."""
        if not self._ready.wait(timeout=2.0):
            return
        assert self._ser is not None
        with self._write_lock:
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass

    def send(self, cmd: str, *, append_crlf: bool = True) -> None:
        """Chỉ write. Không read."""
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("SFCComReader: port not ready")
        assert self._ser is not None

        payload = cmd.rstrip("\r\n")
        if append_crlf:
            payload += "\r\n"
        b = payload.encode("utf-8", errors="replace")

        with self._write_lock:
            self._ser.write(b)
            # flush chỉ đảm bảo bytes đẩy ra driver, không đảm bảo thiết bị phản hồi
            self._ser.flush()

        if self.log:
            self.log(f"[TX] {cmd!r}")

    def send_and_collect(
        self,
        cmd: str,
        *,
        timeout: float = 5.0,
        idle_after_last_rx: float = 0.6,
        expect: Optional[Pattern[str]] = None,
        append_crlf: bool = True,
        clear_before_send: bool = True,
    ) -> Tuple[bool, str, List[str]]:
        """
        Flow:
        - (optional) clear input buffer
        - đặt mốc seq0
        - send()
        - chờ:
            + nếu expect != None: đợi match (trong các line sau mốc)
            + sau đó (hoặc nếu không expect): đợi "im lặng" idle_after_last_rx
          hoặc timeout
        - trả (ok, best_line, all_lines)
        """
        if clear_before_send:
            self.clear_input_buffer()

        seq0 = self.snapshot_seq()
        self._data_evt.clear()

        t0 = time.perf_counter()
        matched = False

        self.send(cmd, append_crlf=append_crlf)

        while True:
            now = time.perf_counter()
            if now - t0 >= timeout:
                break

            lines = self.get_lines_since(seq0)

            if expect is not None and (not matched):
                for ln in lines:
                    if expect.search(ln):
                        matched = True
                        break

            # Quy tắc kết thúc: đã match (hoặc không cần match) + im lặng đủ lâu
            if (expect is None) or matched:
                # _last_rx_time cập nhật trong reader
                if self._last_rx_time and (now - self._last_rx_time) >= idle_after_last_rx:
                    return True, _pick_best_line(lines), lines

            # chờ event có data mới hoặc tick nhỏ
            self._data_evt.wait(timeout=0.05)
            self._data_evt.clear()

        # timeout
        lines = self.get_lines_since(seq0)
        return False, _pick_best_line(lines), lines

    # ---------- internal ----------
    def _emit_line(self, s: str) -> None:
        self._seq += 1
        self._last_rx_time = time.perf_counter()
        self._lines.append(RxLine(seq=self._seq, t=self._last_rx_time, text=s))
        self._data_evt.set()
        if self.log:
            self.log(f"[RX] {s}")

    def _reader_loop(self) -> None:
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0,        # non-blocking
                write_timeout=1.0
            )
            self._ready.set()
        except Exception as e:
            if self.log:
                self.log(f"[ERR] open {self.port}@{self.baudrate}: {e}")
            return

        assert self._ser is not None

        while not self._stop.is_set():
            try:
                n = self._ser.in_waiting
            except Exception:
                n = 0

            chunk = b""
            try:
                chunk = self._ser.read(n or 1)
            except Exception:
                chunk = b""

            if chunk:
                self._rx_buf += chunk

                # cắt theo newline (SFC thường có CRLF). Nếu SFC không newline,
                # thì bạn nên chuyển sang “framing” khác hoặc chỉ dùng idle-window để kết thúc.
                while b"\n" in self._rx_buf:
                    line, _, rest = self._rx_buf.partition(b"\n")
                    self._rx_buf = bytearray(rest)

                    s = line.decode(self.decode, errors="replace").replace("\r", "").strip()
                    if s:
                        self._emit_line(s)
            else:
                time.sleep(self.read_sleep)


def _pick_best_line(lines: List[str]) -> str:
    """Chọn line đáng tin nhất (ưu tiên PASS/FAIL/ERRO...)."""
    if not lines:
        return ""
    def score(ln: str) -> int:
        up = ln.upper()
        sc = 0
        if "PASS" in up or "FAIL" in up:
            sc += 100
        if "PASSED=1" in up or "PASSED=0" in up:
            sc += 30
        if "ERRO" in up or "TIMEOUT" in up:
            sc += 50
        if up.startswith("$"):
            sc += 10
        sc += min(len(ln), 120) // 20
        return sc
    return max(lines, key=score)
