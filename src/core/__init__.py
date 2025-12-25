# src.core.__init__.py
from __future__ import annotations

import configparser
import os
import re
import time
import serial
from src import *
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

# =========================
# 3) Data structures
# =========================
@dataclass(frozen=True)
class ComConfig:
    COM_LASER: str
    COM_SFC: str
    COM_SCAN: str

@dataclass(frozen=True)
class BaudrateConfig:
    BAUDRATE_LASER: int
    BAUDRATE_SFC: int
    BAUDRATE_SCAN: int

@dataclass(frozen=True)
class BreakRule:
    mode: str   # "END" hoặc "IN"
    pattern: str  # đã upper()
    regex: Optional[re.Pattern] = None  # chỉ dùng khi mode == "REGEX"

# =========================
# 4) Helpers
# =========================
_LIST_SPLIT_RE = re.compile(r"[,\n]+")

def _split_list(s: str) -> List[str]:
    parts = _LIST_SPLIT_RE.split(s or "")
    out: List[str] = []
    for p in parts:
        t = p.strip()
        if not t or t.startswith(("#", ";")):
            continue
        out.append(t)
    return out

def _parse_rule(token: str, log_callback=print) -> BreakRule:
    raw = token.strip()
    up = raw.upper()

    # --- REGEX rule ---
    # MATCHREGEX:xxxx  (khuyến nghị)
    # REGEX:xxxx       (alias)
    if up.startswith("MATCHREGEX:") or up.startswith("REGEX:"):
        # cắt prefix theo đúng độ dài
        if up.startswith("MATCHREGEX:"):
            pat = raw[len("MATCHREGEX:"):].strip()
        else:
            pat = raw[len("REGEX:"):].strip()

        if not pat:
            return None

        try:
            rx = re.compile(pat, flags=re.IGNORECASE)
            return BreakRule(mode="REGEX", pattern=pat, regex=rx)
        except re.error as e:
            log_callback(f"[WARN] Invalid REGEX in config: {pat!r} | {e}")
            return None
    if up.startswith("END:"):
        return BreakRule("END", up[4:].strip())
    if up.startswith("IN:"):
        return BreakRule("IN", up[3:].strip())
    # default: IN
    return BreakRule("IN", up)

def _rule_id(r: BreakRule) -> Tuple[str, str]:
    """
    Để loại trùng giữa TOKENS và ALWAYS_LAST mà không bị nhầm.
    - IN/END: so theo pattern upper (đã upper sẵn)
    - REGEX: so theo raw pattern (giữ nguyên)
    """
    if r.mode == "REGEX":
        return ("REGEX", r.pattern)
    return (r.mode, r.pattern)

def normalize_windows_com_port(port: str) -> str:
    """
    Optional: port COM>=10 đôi khi cần dạng \\\\.\\COM10
    PySerial đa số tự xử lý, nhưng hàm này giúp chắc hơn.
    """
    p = (port or "").strip()
    if not p:
        return p
    if p.upper().startswith("COM"):
        # COM10+ an toàn hơn với prefix này
        m = re.match(r"^(COM)(\d+)$", p, re.IGNORECASE)
        if m and int(m.group(2)) >= 10 and not p.startswith("\\\\.\\"):
            return "\\\\.\\" + p
    return p


# =========================
# 5) ConfigManager (auto reload)
# =========================
class ConfigManager:
    """
    - Cache config + reload nếu file config.ini thay đổi (mtime)
    - Lấy COM ports + break rules theo thứ tự ưu tiên trong config
    """

    SEC_COM = "COM"
    SEC_BAUD = "BAUDRATE"
    SEC_BREAK = "SERIAL_READLINE_BREAK"

    def __init__(self, config_path: Path, log: Callable[[str], None] = print):
        self.config_path = Path(config_path)
        self.log = log

        self._mtime_ns: int = -1
        self._com: Optional[ComConfig] = None
        self._baud: Optional[BaudrateConfig] = None
        self._rules: List[BreakRule] = []

        # ensure file exists + patch missing keys
        try:
            ensure_config_ini(self.log)
        except Exception as e:
            self.log(f"[WARN] ensure_config_ini failed: {e}")

        self.reload(force=True)

    def set_logger(self, log_callback = print):
        self.log = log_callback
    
    def get_logger(self):
        return self.log
         
    @property
    def com(self) -> ComConfig:
        # luôn reload_if_changed trước khi trả
        self.reload_if_changed()
        assert self._com is not None
        return self._com

    @property
    def baudrate(self) -> BaudrateConfig:
        self.reload_if_changed()
        return self._baud
    
    @property
    def rules(self) -> List[BreakRule]:
        self.reload_if_changed()
        return self._rules

    def reload_if_changed(self) -> bool:
        """
        Return True nếu có reload (file đổi), False nếu không.
        """
        try:
            st = self.config_path.stat()
            m = st.st_mtime_ns
        except FileNotFoundError:
            # nếu bị xoá, tạo lại
            ensure_config_ini(self.log)
            return self.reload(force=True)
        except Exception as e:
            self.log(f"[WARN] stat config failed: {e}")
            return False

        if m != self._mtime_ns:
            return self.reload(force=True)
        return False

    def reload(self, force: bool = False) -> bool:
        """
        Reload từ disk. Return True nếu reload thành công.
        """
        if not force:
            return self.reload_if_changed()

        # patch missing keys trước khi read (không override)
        try:
            ensure_config_ini(self.log)
        except Exception as e:
            self.log(f"[WARN] ensure_config_ini failed: {e}")

        cp = configparser.ConfigParser(interpolation=None)
        cp.read(self.config_path, encoding="utf-8")

        # ---- load COM ----
        com_laser = cp.get(self.SEC_COM, "COM_LASER", fallback="COM1")
        com_sfc   = cp.get(self.SEC_COM, "COM_SFC",   fallback="COM2")
        com_scan  = cp.get(self.SEC_COM, "COM_SCAN",  fallback="COM3")

        self._com = ComConfig(
            COM_LASER=normalize_windows_com_port(com_laser),
            COM_SFC=normalize_windows_com_port(com_sfc),
            COM_SCAN=normalize_windows_com_port(com_scan),
        )

        # ----- BAUDRATE -----
        def get_int(section: str, key: str, default: int) -> int:
            raw = cp.get(section, key, fallback=str(default)).strip()
            try:
                return int(raw)
            except ValueError:
                self.log(f"[WARN] Invalid int in config: [{section}] {key}={raw!r}, fallback={default}")
                return default

        self._baud = BaudrateConfig(
            BAUDRATE_LASER=get_int(self.SEC_BAUD, "BAUDRATE_LASER", 9600),
            BAUDRATE_SFC=get_int(self.SEC_BAUD, "BAUDRATE_SFC", 9600),
            BAUDRATE_SCAN=get_int(self.SEC_BAUD, "BAUDRATE_SCAN", 9600),
        )

        # ---- load BREAK RULES ----
        rules = load_readline_break_rules(cfg_path=self.config_path, log=self.log)
        self._rules = rules
        # update mtime cache
        try:
            self._mtime_ns = self.config_path.stat().st_mtime_ns
        except Exception:
            self._mtime_ns = -1

        return True

    def update_sections(
        self,
        updates: dict[str, dict[str, str]],
        *,
        make_backup: bool = True,
        reload_after: bool = True,
    ) -> bool:
        """
        Update INI by patching only specified sections/keys (preserve the rest),
        then optionally reload CFG cache.

        Example:
            CFG.update_sections({
                "COM": {"COM_LASER":"COM5", "COM_SFC":"COM8"},
                "BAUDRATE": {"BAUDRATE_LASER":"9600"}
            })
        """
        import re, os, time

        # ensure config exists + has required baseline keys/sections
        try:
            ensure_config_ini(self.log)  # from src (public)
        except Exception:
            # still try to proceed if ensure isn't available
            pass

        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)

        if not path.exists():
            # ensure_config_ini should have created it; if not, create minimal file
            path.write_text("", encoding="utf-8")

        text = path.read_text(encoding="utf-8", errors="replace")
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines(True)  # keep line endings

        def is_section_header(line: str) -> bool:
            s = line.strip()
            if not s or s.startswith(("#", ";")):
                return False
            return s.startswith("[") and s.endswith("]") and len(s) >= 3

        def section_name(line: str) -> str:
            return line.strip()[1:-1].strip()

        def find_section_range(sec: str) -> tuple[int | None, int | None]:
            start = None
            for i, ln in enumerate(lines):
                if is_section_header(ln) and section_name(ln).lower() == sec.lower():
                    start = i
                    break
            if start is None:
                return None, None
            end = len(lines)
            for j in range(start + 1, len(lines)):
                if is_section_header(lines[j]):
                    end = j
                    break
            return start, end

        def ensure_section_exists(sec: str) -> tuple[int, int]:
            s, e = find_section_range(sec)
            if s is not None and e is not None:
                return s, e

            # append section
            if lines and lines[-1].strip() != "":
                lines.append(nl)
            lines.append(f"[{sec}]{nl}")
            return find_section_range(sec)  # type: ignore[return-value]

        def patch_key(sec: str, key: str, value: str) -> None:
            s, e = ensure_section_exists(sec)

            key_re = re.compile(rf"^\s*{re.escape(key)}\s*[:=]", re.IGNORECASE)

            # re-find each time because list length can change
            s, e = find_section_range(sec)
            assert s is not None and e is not None

            found = False
            for i in range(s + 1, e):
                raw = lines[i]
                stripped = raw.lstrip()
                if stripped.startswith(("#", ";")):
                    continue
                if key_re.match(raw):
                    indent = re.match(r"^\s*", raw).group(0)
                    lines[i] = f"{indent}{key}={value}{nl}"
                    found = True
                    break

            if not found:
                insert_at = e
                while insert_at > s + 1 and lines[insert_at - 1].strip() == "":
                    insert_at -= 1
                lines.insert(insert_at, f"{key}={value}{nl}")

        # apply updates
        for sec, kv in updates.items():
            for k, v in kv.items():
                patch_key(sec, k, str(v))

        new_text = "".join(lines)

        # backup + atomic-ish replace
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")

        if make_backup and path.exists():
            bk = path.with_suffix(path.suffix + f".bak_{time.strftime('%Y%m%d_%H%M%S')}")
            try:
                os.replace(path, bk)
            except Exception:
                pass

        os.replace(tmp, path)

        if reload_after:
            self.reload(force=True)

        return True


def load_readline_break_rules(cfg_path: str, *, log=print) -> List[BreakRule]:
    import configparser

    cp = configparser.ConfigParser(interpolation=None)
    cp.read(cfg_path, encoding="utf-8")

    sec = "SERIAL_READLINE_BREAK"
    tokens_raw = cp.get(sec, "TOKENS", fallback="")
    always_last_raw = cp.get(sec, "ALWAYS_LAST", fallback="END:PASS, END:FAIL, END:ERRO")

    def split_list(s: str) -> List[str]:
        parts = re.split(r"[,\n]+", s or "")
        return [p.strip() for p in parts if p.strip() and not p.strip().startswith(("#", ";"))]

    tokens = [r for r in (_parse_rule(x, log_callback=log) for x in split_list(tokens_raw)) if r is not None]
    always_last = [r for r in (_parse_rule(x, log_callback=log) for x in split_list(always_last_raw)) if r is not None]

    # ép ALWAYS_LAST xuống cuối (và loại trùng trong TOKENS)
    always_last_ids = {_rule_id(r) for r in always_last}

    rules: List[BreakRule] = []
    for r in tokens:
        if _rule_id(r) in always_last_ids:
            continue
        rules.append(r)

    rules.extend(always_last)
    return rules


def should_break(response: str, rules: List[BreakRule]) -> bool:
    """
    - IN/END: so trên response.upper()
    - REGEX: dùng re.search trên response gốc (IGNORECASE đã compile sẵn)
    """
    up = response.upper()
    up_stripped = up.rstrip()  # quan trọng cho END:
    for r in rules:
        if r.mode == "REGEX":
            if r.regex and r.regex.search(response):
                return True
        elif r.mode == "END":
            if up.endswith(r.pattern) or up_stripped.endswith(r.pattern):
                return True
        elif r.mode == "IN":
            if r.pattern in up:
                return True
        else:  # REGEX
            if r.regex and r.regex.search(response):
                return True

    return False

# =========================
# 7) Main Action
# =========================
CFG = ConfigManager(app_dir() / "config.ini")

def send_text_and_wait(
    text: str,
    port: str = "COM7",
    baudrate: int = 9600,
    write_append_crlf: bool = True,
    read_timeout: float = 5.0,
    log_callback: Callable[[str], None] = print,
) -> Tuple[bool, str]:
    """
    [TEXT / LINE-BASED SERIAL PROTOCOL]
    Gửi chuỗi text ra cổng COM rồi chờ response dạng TEXT (thường có ký tự xuống dòng).

    Khi nào dùng hàm này?
    ---------------------
    - Thiết bị giao tiếp bằng ASCII/UTF-8 (hoặc text tương đương)
    - Thiết bị trả về theo dòng (có '\\n') => đọc bằng ser.readline() là hợp lý
    - Bạn có “dấu hiệu kết thúc” trong response, ví dụ: PASS / FAIL / ERRO
      -> để dừng sớm, không cần chờ hết timeout.

    Tại sao COMScan (thiết bị scan SN chuyên dụng) KHÔNG dùng được hàm này?
    -----------------------------------------------------------------------
    - COMScan thường dùng giao thức binary: gửi các byte điều khiển (HEX) như 0x16 0x54 0x0D
    - Response của COMScan có thể là raw bytes và KHÔNG có newline '\\n'
      => ser.readline() có thể không trả gì cho đến khi timeout.
    - Nếu bạn encode text rồi gửi, thiết bị có thể không hiểu lệnh.

    Tại sao hàm này KHÔNG phù hợp cho mọi COM?
    ------------------------------------------
    - Vì nó giả định:
      (1) Gửi text (encode UTF-8)
      (2) Response có newline
      (3) Kết thúc bằng keyword PASS/FAIL/ERRO

    Return
    ------
        (True, response_str)  nếu nhận được dữ liệu hợp lệ
        (False, message)      nếu timeout hoặc response chứa FAIL/ERRO hoặc lỗi serial
    """
    try:
        CFG.reload_if_changed()
        rules = CFG.rules

        # CFG.com.COM_SFC
        # CFG.com.COM_SCAN
        # CFG.com.COM_LASER
        # timeout=0: non-blocking read. Ta tự quản timeout bằng vòng while + deadline
        # Lý do: đọc nhiều lần, gom response, dừng sớm khi gặp keyword
        with serial.Serial(port, baudrate, timeout=0) as ser:
            # ---- SEND ----
            # Nhiều thiết bị text-based yêu cầu CRLF để kết thúc frame/lệnh.
            send_str = text + ("\r\n" if write_append_crlf else "")
            send_bytes = send_str.encode("utf-8", errors="replace")

            # Reset buffer để tránh dính data cũ (stale) từ lần trước
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            ser.write(send_bytes)
            ser.flush()

            # ---- WAIT RESPONSE ----
            deadline = time.time() + read_timeout
            response = ""

            while time.time() < deadline:
                # readline() phù hợp khi thiết bị có '\n' kết thúc dòng
                line = ser.readline()
                if line:
                    # Decode text: ưu tiên utf-8, fallback latin-1 để không crash
                    try:
                        decoded = line.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded = line.decode("latin-1", errors="ignore")

                    response += decoded
                    log_callback(f"[debug][{port}] -> {decoded!r}")

                    # Dừng sớm nếu đã thấy keyword trạng thái
                    # (tuỳ protocol, bạn có thể đổi keyword)
                    # upper = response.upper()
                    # TODO: READ LAW FROM CONFIG TO CHECK THE BREAK CONDITIONS
                    if should_break(response, rules):
                        break
                else:
                    # Ngủ nhẹ để tránh while loop ăn CPU 100%
                    time.sleep(0.01)

            # upper = response.upper()
            # if "FAIL" in upper or "ERRO" in upper:
            #     return False, f"{port} FAIL/ERRO - {response.strip()}"

            if response.strip():
                return True, response.strip()

            return False, "No response (timeout)"

    except serial.SerialException as e:
        log_callback(f"[ERROR] Serial error on {port}: {e}")
        return False, f"Serial error: {e}"


def control_comscan(
    port: str = "COM5",
    baudrate: int = 9600,
    timeout_sec: float = 5.0,
    log_callback: Callable[[str], None] = print,
) -> Optional[bytes]:
    """
    [BINARY / COMMAND-BASED SERIAL PROTOCOL FOR COMSCAN]
    Điều khiển thiết bị COMScan (chuyên scan SN) bằng lệnh nhị phân (raw bytes).

    Khi nào dùng hàm này?
    ---------------------
    - Thiết bị không nhận “text command”, mà nhận “binary command frame”
      Ví dụ: cmd = 0x16 0x54 0x0D
    - Response trả về dạng bytes, có thể KHÔNG có '\\n'
      => Không dùng readline(), mà dùng read() theo in_waiting.

    Tại sao hàm này KHÔNG dùng cho COM7 kiểu Laser/SFC?
    ---------------------------------------------------
    - COM7 text-based thường chờ chuỗi ASCII + CRLF.
    - Nếu bạn gửi 0x16 0x54 0x0D vào thiết bị text-based:
      + thiết bị có thể hiểu sai, trả về garbage, hoặc “kẹt state machine”.
    - Vì protocol khác nhau nên phải tách riêng.

    Response bytes là gì?
    ---------------------
    - Bạn sẽ nhận bytes dạng ví dụ: b"GT542A0154530005" hoặc bytes raw khác
    - Việc “decode”/“parse” SN tuỳ vào spec của COMScan.

    Return
    ------
    - bytes nếu nhận được dữ liệu
    - None nếu timeout / lỗi
    """
    # Lệnh điều khiển dạng HEX (control code)
    # 0x16 (SYN), 0x54 ('T'), 0x0D (CR) - chỉ là ví dụ theo thiết bị của bạn
    cmd = bytes([0x16, 0x54, 0x0D])

    try:
        with serial.Serial(port, baudrate, timeout=0) as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            # ---- SEND BINARY CMD ----
            ser.write(cmd)
            ser.flush()

            # ---- READ RAW BYTES ----
            deadline = time.time() + timeout_sec
            recv = bytearray()

            while time.time() < deadline:
                n = ser.in_waiting
                if n:
                    recv.extend(ser.read(n))
                    # Nhiều scanner trả về “1 frame” rồi dừng,
                    # nên chỉ cần có data là break.
                    break

                time.sleep(0.01)

            if recv:
                return bytes(recv)
            return None

    except serial.SerialException as e:
        log_callback(f"[ERROR] Serial error on {port}: {e}")
        return None

