# src.core.core.py
from __future__ import annotations
import re
import time
import threading
from src.core import *

# -------------------------------
# Frame assembler (gom readline -> 1 frame)
# -------------------------------
class FrameAssembler:
    def __init__(self, rules: List[BreakRule]):
        self.rules = rules
        self.buf = ""
        self.t0: Optional[float] = None

    def push(self, chunk: str) -> Optional[str]:
        if not chunk:
            return None
        if self.t0 is None:
            self.t0 = time.time()
        self.buf += chunk
        if should_break(self.buf, self.rules):
            out = self.buf.strip()
            self.buf = ""
            self.t0 = None
            return out
        return None

    def reset(self) -> None:
        self.buf = ""
        self.t0 = None


# -------------------------------
# Bridge
# -------------------------------
def infer_status(text: str) -> Optional[str]:
    up = text.upper()
    # ưu tiên FAIL trước để tránh “FAIL03PASS” bị hiểu nhầm PASS
    if "PASSED=0" in up or "FAIL" in up or "ERRO" in up:
        return "FAIL"
    if "PASSED=1" in up or " PASS" in up or up.endswith("PASS"):
        return "PASS"
    return None

class LaserSfcBridge:
    """
    Refactor goals:
    1) Listen COM_LASER only (trigger)
    2) Use send_text_and_wait() to talk with SFC (open SFC on-demand)
    3) Hold point: if SFC response contains PASSED=[01] => DO NOT write back to Laser, end testing-chain
    4) While True needs a stop trigger on config reload => break_on_reload option
    5) Provide get_* for UI polling later
    6) Testing-chain: starts at first Laser trigger, continues until hold-point from SFC
    7) During chain => status "Testing", otherwise "Listening"
    9) Designed to run in a background thread OR be polled by UI (step())
    """

    HOLD_RX = re.compile(r"PASSED=[01]", re.IGNORECASE)

    def __init__(
        self,
        cfg: "ConfigManager",
        *,
        sfc_timeout: float = 5.0,
        idle_sleep: float = 0.01,  # dùng khi run_forever
        break_on_reload: bool = False,
        log: Callable[[str], None] = print,
        on_result: Optional[Callable[[str, str, str], None]] = None,  # (status, laser_req, sfc_resp)
    ):
        self.cfg = cfg
        self.baud_laser = self.cfg.baudrate.BAUDRATE_LASER
        self.baud_sfc = self.cfg.baudrate.BAUDRATE_SFC
        self.sfc_timeout = sfc_timeout
        self.idle_sleep = idle_sleep
        self.break_on_reload = break_on_reload
        self.log = log
        self.on_result = on_result

        # Serial (LASER only)
        self.ser_laser = None
        self._cur_laser_port: str = ""

        # Assembler for Laser frames
        self.laser_asm = FrameAssembler(self.cfg.rules)

        # Runtime state for UI
        self._running: bool = False
        self._mode: str = "Idle"          # Idle / Listening / Testing / Error / Stopped
        self._last_error: str = ""
        self._last_status: str = ""       # PASS/FAIL/UNKNOWN/TIMEOUT/ERROR...
        self._last_laser_req: str = ""
        self._last_sfc_resp: str = ""
        self._chain_active: bool = False

        # Stop control
        self._stop_event = threading.Event()

    # -----------------
    # UI getters (yêu cầu 5,8)
    # -----------------
    def get_status_triplet(self) -> Tuple[bool, str, str]:
        """Return (is_ok, com_laser, status_text)."""
        com = self._cur_laser_port or getattr(self.cfg.com, "COM_LASER", "")
        if not self._running:
            return False, com, "Stopped"
        if self._mode == "Error":
            return False, com, f"Error: {self._last_error}"
        # requirement: Listening/Testing should return True
        if self._mode in ("Listening", "Testing"):
            return True, com, self._mode
        return True, com, self._mode

    def get_mode(self) -> str:
        return self._mode

    def is_testing(self) -> bool:
        return self._mode == "Testing"

    def get_last_result(self) -> Tuple[str, str, str]:
        """(last_status, last_laser_req, last_sfc_resp)"""
        return self._last_status, self._last_laser_req, self._last_sfc_resp

    def get_last_error(self) -> str:
        return self._last_error

    # -----------------
    # Lifecycle
    # -----------------
    def request_stop(self) -> None:
        self._stop_event.set()

    def close(self) -> None:
        try:
            if self.ser_laser:
                self.ser_laser.close()
        except Exception:
            pass
        self.ser_laser = None
        self._cur_laser_port = ""

    # -----------------
    # Internal helpers
    # -----------------
    def _ensure_laser_open(self) -> bool:
        """
        Open/reopen LASER port if needed.
        Also refresh rules on config reload.
        """
        changed = self.cfg.reload_if_changed()

        # Optional: trigger stop/break when config changes (requirement 4)
        if changed and self.break_on_reload:
            self._mode = "Listening" if self.ser_laser else "Idle"
            self._chain_active = False
            self.laser_asm.reset()
            self.log("[INFO] Config changed -> break loop (break_on_reload=True)")
            return False  # signal caller to break

        com = self.cfg.com
        laser_port = com.COM_LASER  # dùng đúng field name từ ComConfig
        self._cur_laser_port = laser_port

        # Update rules if changed (even if ports not changed)
        self.laser_asm.rules = self.cfg.rules

        if self.ser_laser and getattr(self.ser_laser, "port", None) == laser_port:
            return True

        # Reopen
        self.close()
        try:
            self.log(f"[INFO] Opening LASER={laser_port}")
            # timeout=0 để step() không block UI; run_forever sẽ sleep idle_sleep
            self.ser_laser = serial.Serial(laser_port, self.baud_laser, timeout=0, write_timeout=1.0)
            self.ser_laser.reset_input_buffer()
            self.ser_laser.reset_output_buffer()
            self.laser_asm.reset()
            return True
        except Exception as e:
            self._mode = "Error"
            self._last_error = f"Open LASER failed: {e}"
            self.log(f"[ERROR] {self._last_error}")
            self.close()
            return True  # vẫn return True để loop không crash; UI thấy Error qua getter

    def _read_laser_frame_nonblock(self) -> Optional[str]:
        """
        Non-blocking poll: read 1 readline chunk, feed assembler.
        Return full frame if assembler breaks, else None.
        """
        if not self.ser_laser:
            return None

        b = self.ser_laser.readline()
        if not b:
            return None

        try:
            s = b.decode("utf-8")
        except UnicodeDecodeError:
            s = b.decode("latin-1", errors="ignore")

        return self.laser_asm.push(s)

    def _write_back_to_laser(self, text: str) -> None:
        if not self.ser_laser:
            return
        payload = (text.rstrip("\r\n") + "\r\n").encode("utf-8", errors="replace")
        self.ser_laser.write(payload)
        self.ser_laser.flush()

    def _is_hold_point(self, sfc_resp: str) -> bool:
        return bool(self.HOLD_RX.search(sfc_resp or ""))

    # -----------------
    # Core loop API (UI-friendly)
    # -----------------
    def step(self) -> str:
        """
        One small tick (UI can call via after()).
        Returns an event code:
          - "IDLE" (no data)
          - "RELOAD_BREAK" (config changed + break_on_reload)
          - "LASER_FRAME" (got trigger)
          - "SFC_OK" / "SFC_TIMEOUT" / "SFC_ERROR"
          - "HOLD" (hold-point reached, chain ended)
          - "ERROR"
        """
        try:
            self._running = True
            if self._mode in ("Idle", "Stopped"):
                self._mode = "Listening"

            ok = self._ensure_laser_open()
            if not ok and self.break_on_reload:
                # explicit trigger to stop outer while True
                return "RELOAD_BREAK"

            if self._mode == "Error":
                return "ERROR"

            frame = self._read_laser_frame_nonblock()
            if not frame:
                self._mode = "Testing" if self._chain_active else "Listening"
                return "IDLE"

            # Trigger from Laser
            self._last_laser_req = frame
            self._last_sfc_resp = ""
            self._last_status = ""
            self.log(f"[RX][LASER] {frame!r}")

            # Start/continue testing chain
            self._chain_active = True
            self._mode = "Testing"

            # Send to SFC using send_text_and_wait (requirement 2)
            com = self.cfg.com
            ok_sfc, sfc_resp = send_text_and_wait(
                frame,
                port=com.COM_SFC,
                baudrate=self.baud_sfc,
                write_append_crlf=True,
                read_timeout=self.sfc_timeout,
                log_callback=self.log,
            )

            if not ok_sfc:
                self._last_status = "TIMEOUT" if "timeout" in (sfc_resp or "").lower() else "SFC_ERROR"
                self._last_sfc_resp = sfc_resp or ""
                self.log(f"[WARN] SFC failed: {sfc_resp}")
                if self.on_result:
                    self.on_result(self._last_status, frame, self._last_sfc_resp)

                # Fail-safe: end chain to avoid being stuck in Testing forever
                self._chain_active = False
                self._mode = "Listening"
                return "SFC_TIMEOUT" if self._last_status == "TIMEOUT" else "SFC_ERROR"

            # Got SFC response
            self._last_sfc_resp = sfc_resp
            self.log(f"[RX][SFC]   {sfc_resp!r}")

            # Update status each response
            self._last_status = infer_status(sfc_resp) or "UNKNOWN"
            if self.on_result:
                self.on_result(self._last_status, frame, sfc_resp)

            # Hold point (requirement 3,6): end chain and DO NOT write back to Laser
            if self._is_hold_point(sfc_resp):
                self.log("[STATE] HOLD: PASSED=[01] seen -> end chain, back to Listening (no write-back to LASER)")
                self._chain_active = False
                self._mode = "Listening"
                return "HOLD"

            # Normal: write back to Laser
            self._write_back_to_laser(sfc_resp)
            self.log(f"[TX][LASER] {sfc_resp!r}")
            return "SFC_OK"

        except Exception as e:
            self._mode = "Error"
            self._last_error = str(e)
            self.log(f"[ERROR] Bridge step exception: {e}")
            return "ERROR"

    def run_forever(self) -> None:
        """
        Background-thread friendly loop:
        - never blocks UI thread (run it in a thread)
        - has stop_event
        - can break on config reload if break_on_reload=True
        """
        self._stop_event.clear()
        self._running = True
        self._mode = "Listening"
        try:
            while not self._stop_event.is_set():
                ev = self.step()
                if ev == "RELOAD_BREAK":
                    break

                # ngủ nhẹ không chỉ khi IDLE, mà cả khi ERROR để tránh spin CPU
                if ev in ("IDLE", "ERROR", "SFC_ERROR", "SFC_TIMEOUT"):
                    time.sleep(max(self.idle_sleep, 0.05))
        finally:
            self._running = False
            self._mode = "Stopped"
            self.close()

# -------------------------------
# Example main
# -------------------------------
# if __name__ == "__main__":
#     # giả sử config.ini nằm cùng app_dir
#     loggeria, bufferia = build_log_buffer("core") 
#     cfg = CFG
#     cfg.set_logger(log_callback=loggeria)

#     def on_result(status: str, laser_req: str, sfc_resp: str) -> None:
#         print(f"[RESULT] {status} | req={laser_req!r} | resp={sfc_resp!r}")

#     bridge = LaserSfcBridge(cfg, baud_laser=9600, baud_sfc=9600, on_result=on_result)
#     bridge.run_forever()