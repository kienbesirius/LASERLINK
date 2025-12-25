from __future__ import annotations

import os
import sys
import time
import logging
import threading
import queue
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from pathlib import Path
from typing import Optional

from typer import style

# -----------------------------------------------------------------------------
# Path bootstrap: make sure we can import `src.*` when running this file directly.
# This file is expected at: <project_root>/src/gui/gui.py
# -----------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_src_dir = _THIS
while _src_dir.name != "src" and _src_dir.parent != _src_dir:
    _src_dir = _src_dir.parent

# Insert project root (parent of `src/`) so that `import src...` works.
_project_root = _src_dir.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Import shared config/export from src to keep config definitions in sync
# (CFG/DEFAULTS/ensure_config_file/... if your src/__init__.py exposes them)
try:
    from src import *  # type: ignore  # noqa: F401,F403
except Exception:
    pass

try:
    from src.utils.resource_path import app_dir  # type: ignore
except Exception:
    # Fallback: treat current working dir as app_dir
    def app_dir() -> Path:  # type: ignore
        return Path.cwd()

# Optional dependency
try:
    import serial.tools.list_ports  # type: ignore
    _HAS_SERIAL = True
except Exception:
    _HAS_SERIAL = False

try:
    from src.utils.buffer_logger import build_log_buffer
except Exception:
    # Fallback: dummy logger builder
    import logging
    from typing import List, Tuple

    def build_log_buffer(
        name: str = "LASERLINK",
        level=logging.DEBUG,
        *,
        max_buffer: int = 500,
    ) -> Tuple[logging.Logger, List[str]]:
        logger = logging.getLogger(name=name)
        logger.setLevel(level)
        return logger, []
    
try:
    from src.core.core import LaserSfcBridge  # type: ignore
except Exception as e:
    import traceback
    traceback.print_exc()   # <<< in ra đúng file + dòng lỗi
    LaserSfcBridge = None  # type: ignore

# -----------------------------
# Theme constants (Light, "uy tín")
# -----------------------------
BG = "#ECEEF2"          # outer gray
CARD_BG = "#FFFFFF"     # card
BORDER = "#D6DAE3"
TEXT = "#111827"
MUTED = "#6B7280"

OK_FG = "#0F5132"
ERR_FG = "#842029"
WARN_FG = "#7A4B00"
EDIT_KEY_ENV = "LASERLINK_EDIT_KEY"
DEFAULT_EDIT_KEY = "Laserlinkfii168!!"          # đổi tuỳ bạn
EDIT_UNLOCK_TTL_SEC = 3       # unlock tạm 3 giây sau khi nhập đúng

# -----------------------------
# Config (source of truth = src.core.CFG)
# -----------------------------
# We intentionally DO NOT maintain a duplicate schema/defaults in GUI.
# All COM/BAUDRATE/RULES should be read & written via the singleton CFG in src.core.
try:
    from src.core import CFG  # type: ignore
except Exception:
    CFG = None  # type: ignore


def list_ports() -> list[str]:
    if not _HAS_SERIAL:
        return []
    return [p.device for p in serial.tools.list_ports.comports()]




class DialogHost(ttk.Frame):
    """
    Nested modal overlay (inside the main window; no Toplevel).
    - Supports stacking dialogs.
    - Uses grab_set to make it truly modal.
    - Backdrop click can be enabled per-dialog (dismiss_on_backdrop).
    """

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.place_forget()

        self.stack: list[ttk.Frame] = []
        self._focus_stack: list[Optional[tk.Widget]] = []

        # Dim background (inside same window)
        self.dim = tk.Canvas(self, highlightthickness=0, bd=0, bg=BG)
        self.dim.place(x=0, y=0, relwidth=1, relheight=1)
        self._dim_rect = self.dim.create_rectangle(0, 0, 1, 1, fill=BG, outline="")

        self.bind("<Configure>", self._on_resize)
        self.bind_all("<Escape>", self._on_escape, add=True)

        # Eat clicks by default; optional close handled in _on_backdrop_click
        self.dim.bind("<Button-1>", self._on_backdrop_click)
        self.dim.bind("<ButtonRelease-1>", lambda e: "break")

    def _on_resize(self, _e=None):
        # Keep dim rectangle sized to the overlay size
        w = self.winfo_width()
        h = self.winfo_height()
        try:
            self.dim.coords(self._dim_rect, 0, 0, w, h)
        except tk.TclError:
            pass

    def show(self, dialog: ttk.Frame) -> None:
        # Make overlay visible and modal
        if not self.winfo_ismapped():
            self.place(x=0, y=0, relwidth=1, relheight=1)
            self.lift()
            try:
                self.grab_set()  # modal
            except tk.TclError:
                pass

        # Save focus to restore later
        try:
            self._focus_stack.append(self.winfo_toplevel().focus_get())
        except Exception:
            self._focus_stack.append(None)

        # Hide previous top (keep in stack)
        if self.stack:
            self.stack[-1].place_forget()

        self.stack.append(dialog)

        # Keep dim behind dialogs (but above the main UI because host is lifted)
        # Use tk-level 'lower' so we lower the canvas widget itself (Canvas.lower is for canvas items)
        try:
            self.tk.call("lower", self.dim._w)
        except tk.TclError:
            pass
        dialog.place(x=0, y=0, relwidth=1, relheight=1)
        dialog.lift()

        # Give focus to dialog (if possible)
        try:
            dialog.focus_set()
        except Exception:
            pass

    def close_top(self) -> None:
        if not self.stack:
            return

        top = self.stack.pop()
        try:
            top.destroy()
        except Exception:
            pass

        # Restore previous or hide overlay
        if self.stack:
            dlg = self.stack[-1]
            # restore as expanded centered dialog
            dlg.place(x=0, y=0, relwidth=1, relheight=1)
            dlg.lift()
            try:
                dlg.focus_set()
            except Exception:
                pass
        else:
            self.place_forget()
            try:
                self.grab_release()
            except tk.TclError:
                pass

        # Restore previous focus if available
        prev = self._focus_stack.pop() if self._focus_stack else None
        if prev and prev.winfo_exists():
            try:
                prev.focus_set()
            except Exception:
                pass

    def close_all(self) -> None:
        while self.stack:
            self.close_top()

    def _on_escape(self, _e=None):
        if self.stack:
            self.close_top()

    def _on_backdrop_click(self, _e=None):
        """
        Close only if the top dialog allows backdrop dismiss.
        Always break to stop click-through.
        """
        if not self.stack:
            return "break"

        top = self.stack[-1]
        dismiss = bool(getattr(top, "dismiss_on_backdrop", False))
        if dismiss:
            self.close_top()
        return "break"


class BaseDialog(ttk.Frame):
    dismiss_on_backdrop: bool = True  # default; can be overridden

    def __init__(self, host: DialogHost, title: str, width: int = 560):
        super().__init__(host)
        self.host = host
        self["padding"] = 16
        self.configure(style="Card.TFrame")

        # fixed width dialog feel
        header = ttk.Frame(self, style="InCard.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text=title, style="DialogTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="✕", command=self.host.close_top, width=3).grid(row=0, column=1, sticky="e")
        ttk.Separator(header, style="Thin.TSeparator").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        self.body = ttk.Frame(self, style="InCard.TFrame")
        self.body.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.footer = ttk.Frame(self, style="InCard.TFrame")
        self.footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        self.footer.columnconfigure(0, weight=1)


class InfoDialog(BaseDialog):
    dismiss_on_backdrop = True

    def __init__(self, host: DialogHost, info_text: str):
        super().__init__(host, "INFO", width=640)
        txt = ScrolledText(self.body, height=14, wrap="word")
        txt.insert("1.0", info_text)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)

        ttk.Button(self.footer, text="OK", command=self.host.close_top, width=10).grid(row=0, column=0, sticky="e")


class ErrorDialog(BaseDialog):
    dismiss_on_backdrop = True

    def __init__(self, host: DialogHost, title: str, message: str):
        super().__init__(host, title, width=640)
        lbl = ttk.Label(self.body, text=message, style="Error.TLabel", wraplength=600, justify="left")
        lbl.pack(fill="x", expand=False)

        ttk.Button(self.footer, text="Đóng", command=self.host.close_top, width=10).grid(row=0, column=0, sticky="e")

class EditKeyDialog(BaseDialog):
    """Prompt user for a key before allowing config edits."""
    dismiss_on_backdrop = False

    def __init__(self, host: DialogHost, app: "LASERLINKAPP", *, on_success):
        super().__init__(host, "ENTER KEY", width=520)
        self.app = app
        self._on_success = on_success

        wrap = ttk.Frame(self.body, style="InCard.TFrame")
        wrap.pack(fill="x", expand=False)

        ttk.Label(
            wrap,
            text="Nhập mã key để mở chức năng sửa config.ini",
            style="Muted.TLabel",
            wraplength=480,
            justify="left",
        ).pack(anchor="w")

        self.v_key = tk.StringVar(value="")
        self.ent = ttk.Entry(wrap, textvariable=self.v_key, show="•")
        self.ent.pack(fill="x", pady=(10, 0))

        self.lbl_err = ttk.Label(wrap, text="", style="Error.TLabel", wraplength=480, justify="left")
        self.lbl_err.pack(anchor="w", pady=(8, 0))

        right = ttk.Frame(self.footer, style="InCard.TFrame")
        right.grid(row=0, column=0, sticky="e")
        ttk.Button(right, text="Cancel", command=self.host.close_top, width=10).pack(side="right", padx=(8, 0))
        ttk.Button(right, text="OK", command=self._ok, width=10).pack(side="right")

        self.ent.bind("<Return>", lambda _e: self._ok())
        self.ent.focus_set()

    def _ok(self):
        key = (self.v_key.get() or "").strip()
        if self.app._check_edit_key(key):
            self.app._unlock_edit()
            self.host.close_top()
            self._on_success()
            return

        self.v_key.set("")
        self.lbl_err.configure(text="Sai key. Vui lòng thử lại hoặc liên hệ TE/Engineer. 5935 - 70626")
        self.ent.focus_set()


class EditConfigDialog(BaseDialog):
    # Avoid accidental close while editing; use explicit Cancel/Save.
    dismiss_on_backdrop = False

    def __init__(self, host: DialogHost, app: "LASERLINKAPP"):
        super().__init__(host, "EDIT CONFIG.INI", width=740)
        self.app = app

        ports = [""] + list_ports()

        # Vars
        snap = app.get_config_snapshot()
        self.v_com_laser = tk.StringVar(value=snap.get("COM_LASER", ""))
        self.v_com_sfc   = tk.StringVar(value=snap.get("COM_SFC", ""))
        self.v_com_scan  = tk.StringVar(value=snap.get("COM_SCAN", ""))

        self.v_baud_laser = tk.StringVar(value=str(snap.get("BAUDRATE_LASER", "9600")))
        self.v_baud_sfc   = tk.StringVar(value=str(snap.get("BAUDRATE_SFC", "9600")))
        self.v_baud_scan  = tk.StringVar(value=str(snap.get("BAUDRATE_SCAN", "9600")))


        grid = ttk.Frame(self.body, style="InCard.TFrame")
        grid.pack(fill="both", expand=True)
        for c in range(2):
            grid.columnconfigure(c, weight=1)

        def row(r: int, label: str, var: tk.StringVar, choices: Optional[list[str]] = None):
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="w", pady=6, padx=(0, 10))
            if choices:
                cb = ttk.Combobox(grid, textvariable=var, values=choices, state="readonly")
                cb.grid(row=r, column=1, sticky="ew", pady=6)
            else:
                ent = ttk.Entry(grid, textvariable=var)
                ent.grid(row=r, column=1, sticky="ew", pady=6)

        # If no pyserial, show Entry widgets.
        port_choices = ports if ports and _HAS_SERIAL else None
        row(0, "COM_LASER", self.v_com_laser, port_choices)
        row(1, "COM_SFC", self.v_com_sfc, port_choices)
        row(2, "COM_SCAN", self.v_com_scan, port_choices)

        ttk.Separator(grid, style="Thin.TSeparator").grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)

        row(4, "BAUDRATE_LASER", self.v_baud_laser, None)
        row(5, "BAUDRATE_SFC", self.v_baud_sfc, None)
        row(6, "BAUDRATE_SCAN", self.v_baud_scan, None)

        hint = ttk.Label(
            self.body,
            text="Tip: nếu máy không có pyserial thì combobox port sẽ thành entry để bạn nhập tay.",
            style="Muted.TLabel",
        )
        hint.pack(anchor="w", pady=(10, 0))

        # Footer buttons
        left = ttk.Frame(self.footer, style="InCard.TFrame")
        left.grid(row=0, column=0, sticky="w")
        ttk.Button(left, text="Scan Ports", command=self._scan_ports).pack(side="left")

        right = ttk.Frame(self.footer, style="InCard.TFrame")
        right.grid(row=0, column=0, sticky="e")
        ttk.Button(right, text="Cancel", command=self.host.close_top, width=10).pack(side="right", padx=(8, 0))
        ttk.Button(right, text="Save", command=self._save, width=10).pack(side="right")

    def _scan_ports(self):
        info = "pyserial chưa có nên không scan được." if not _HAS_SERIAL else "\n".join(list_ports()) or "(No ports found)"
        self.host.show(InfoDialog(self.host, f"Available ports:\n{info}"))

    def _save(self):
        # basic validation
        def is_int(s: str) -> bool:
            try:
                int(s.strip())
                return True
            except Exception:
                return False

        bad = []
        if not is_int(self.v_baud_laser.get()): bad.append("BAUDRATE_LASER")
        if not is_int(self.v_baud_sfc.get()): bad.append("BAUDRATE_SFC")
        if not is_int(self.v_baud_scan.get()): bad.append("BAUDRATE_SCAN")
        if bad:
            self.host.show(ErrorDialog(self.host, "INVALID BAUDRATE", "Các field baudrate phải là số: " + ", ".join(bad)))
            return

        # Apply
        com_updates = {
            "COM_LASER": self.v_com_laser.get().strip(),
            "COM_SFC":   self.v_com_sfc.get().strip(),
            "COM_SCAN":  self.v_com_scan.get().strip(),
        }
        baud_updates = {
            "BAUDRATE_LASER": int(self.v_baud_laser.get().strip()),
            "BAUDRATE_SFC":   int(self.v_baud_sfc.get().strip()),
            "BAUDRATE_SCAN":  int(self.v_baud_scan.get().strip()),
        }

        ok, msg = self.app.save_config_values(com_updates, baud_updates)
        if ok:
            self.host.close_top()
            self.app.set_status("OK", f"Saved: {self.app.config_path.name}")
        else:
            self.host.show(ErrorDialog(self.host, "SAVE FAILED", msg))


# -----------------------------
# Main App
# -----------------------------
class LASERLINKAPP(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LASERLINK")
        self.configure(bg=BG)

        # BookyApp-ish min/max sizing
        self.minsize(980, 640)
        self.maxsize(1280, 820)

        # ttk theme
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD_BG, borderwidth=1, relief="solid")
        style.configure("InCard.TFrame", background=CARD_BG, borderwidth=0, relief="flat")
        style.configure("Thin.TSeparator", background=BORDER)
        style.configure("TLabel", background=CARD_BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD_BG, foreground=MUTED)
        style.configure("DialogTitle.TLabel", background=CARD_BG, foreground=TEXT, font=("TkDefaultFont", 12, "bold"))
        style.configure("Error.TLabel", background=CARD_BG, foreground=ERR_FG)

        # ---- Status styles (for background coloring) ----
        style.configure("StatusCard.TFrame", background=CARD_BG, borderwidth=1, relief="solid")
        style.configure("StatusTitle.TLabel", background=CARD_BG, foreground=MUTED)
        style.configure("StatusBig.TLabel",   background=CARD_BG, foreground=TEXT)
        style.configure("StatusDesc.TLabel",  background=CARD_BG, foreground=MUTED)

        self._style = style  # giữ lại để update runtime

        # Xanh / Đỏ / Lục / Vàng / Trắng (high-contrast cho công nhân)
        self.STATUS_THEMES = {
            # trắng
            "IDLE":       ("#FFFFFF", TEXT, MUTED),
            "READY":      ("#FFFFFF", TEXT, MUTED),
            "STOPPED":    ("#FFFFFF", TEXT, MUTED),

            # xanh (blue) cho “đang chạy/đang test”
            "LISTENING":  ("#0EA5E9", "#FFFFFF", "#E5E7EB"),
            "TESTING":    ("#2563EB", "#FFFFFF", "#E5E7EB"),
            "STANDBY":    ("#0EA5E9", "#FFFFFF", "#E5E7EB"),

            # lục (green) cho OK/PASS
            "OK":         ("#22C55E", "#FFFFFF", "#ECFDF5"),
            "PASS":       ("#22C55E", "#FFFFFF", "#ECFDF5"),

            # vàng (yellow) cho WARN
            "WARN":       ("#F59E0B", "#111827", "#111827"),
            "WARNING":    ("#F59E0B", "#111827", "#111827"),

            # đỏ (red) cho FAIL/ERROR
            "FAIL":       ("#EF4444", "#FFFFFF", "#FEE2E2"),
            "ERROR":      ("#DC2626", "#FFFFFF", "#FEE2E2"),
        }

        # Main content container
        self.container = ttk.Frame(self, padding=18, style="TFrame")
        self.container.place(x=0, y=0, relwidth=1, relheight=1)

        self.container.columnconfigure(0, weight=1)
        self.container.rowconfigure(1, weight=1)

        # Header: Status card
        self.status_card = ttk.Frame(self.container, style="StatusCard.TFrame", padding=16)
        self.status_card.grid(row=0, column=0, sticky="ew")
        self.status_card.columnconfigure(0, weight=1)

        self.status_title = ttk.Label(self.status_card, text="STATUS", style="StatusTitle.TLabel")
        self.status_title.grid(row=0, column=0, sticky="w")

        self.status_big = ttk.Label(self.status_card, text="IDLE", style="StatusBig.TLabel", font=("TkDefaultFont", 26, "bold"))
        self.status_big.grid(row=1, column=0, sticky="w", pady=(6, 0))

        self.status_desc = ttk.Label(self.status_card, text="Ready.", style="StatusDesc.TLabel")
        self.status_desc.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # Content row: left config + right log
        self.content = ttk.Frame(self.container, style="TFrame")
        self.content.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        self.content.columnconfigure(0, weight=0)
        self.content.columnconfigure(1, weight=1)
        self.content.rowconfigure(0, weight=1)

        # Left card (fixed width feel)
        self.left = ttk.Frame(self.content, style="Card.TFrame", padding=14)
        self.left.grid(row=0, column=0, sticky="nsw", padx=(0, 14))
        self.left.configure(width=360)
        self.left.grid_propagate(False)

        # Right card: Log
        self.right = ttk.Frame(self.content, style="Card.TFrame", padding=14)
        self.right.grid(row=0, column=1, sticky="nsew")
        self.right.rowconfigure(1, weight=1)
        self.right.columnconfigure(0, weight=1)

        # Footer row
        self.footer = ttk.Frame(self.container, style="TFrame")
        self.footer.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.footer.columnconfigure(0, weight=1)

        # Dialog host (overlay in parent)
        self.dialog_host = DialogHost(self)

        # 1) init logger hub
        self.logger, self.log_buff = build_log_buffer("LASERLINK", max_buffer=5000)
        # track last log object rendered (IMPORTANT for trimmed ring-buffer)
        self._last_log_obj = None

        # 2) attach CFG logging -> goes into logger -> into log_buff
        self.cfg = CFG
        self.config_path = Path(getattr(self.cfg, "config_path", app_dir() / "config.ini"))

        # --- edit-config lock ---
        self._edit_key: str = os.environ.get(EDIT_KEY_ENV, DEFAULT_EDIT_KEY)
        self._edit_unlocked_until: float = 0.0

        if self.cfg is not None and hasattr(self.cfg, "set_logger"):
            self.cfg.set_logger(self.append_log)
        # if self.cfg is not None and hasattr(self.cfg, "set_logger"):
        #     self.cfg.set_logger(self.logger.debug)  # or .debug if you want more verbose

        # 3) start UI pump to display logs from log_buff (main thread safe)
        self.after(100, self._pump_log_buffer)
        
        # Log state
        self._log_lines: int = 0
        self._log_max_lines: int = 120

        # Mock UI state
        self._mock_running: bool = False
        self._mock_after_id: Optional[str] = None
        self._mock_i: int = 0
        self._mock_seq: list[tuple[str, str]] = [
            ("IDLE", "Ready."),
            ("LISTENING", "Waiting for LASER trigger..."),
            ("TESTING", "Sending to SFC..."),
            ("PASS", "SFC: PASSED=1"),
            ("TESTING", "Next cycle..."),
            ("FAIL", "SFC: FAIL"),
            ("WARN", "Retrying / Port unstable..."),
            ("ERROR", "Timeout / No response..."),
        ]

        # Build left panel widgets
        # ttk.Label(self.left, text="CONFIG", style="Muted.TLabel").pack(anchor="w")
        # ttk.Label(self.left, text=str(self.config_path), style="Muted.TLabel", wraplength=330).pack(anchor="w", pady=(4, 12))
        # ttk.Label(self.left, text=self.config_path.name, style="Muted.TLabel", wraplength=220).pack(anchor="w", pady=(4, 12))

        btns = ttk.Frame(self.left, style="InCard.TFrame")
        btns.pack(fill="x")

        ttk.Button(btns, text="Reload", command=self.reload_config).pack(fill="x")
        ttk.Button(btns, text="Edit comfig", command=self.open_edit_config).pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Info", command=self.open_info).pack(fill="x", pady=(8, 0))

        ttk.Separator(self.left, style="Thin.TSeparator").pack(fill="x", pady=14)

        self.btn_mock = ttk.Button(self.left, text="Start Mock UX (disabled)", command=self._toggle_mock)
        self.btn_mock.pack(fill="x")
        self.btn_mock.configure(state="disabled")  # disable mock UX for now

        # Right panel: log
        ttk.Label(self.right, text="LOG", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.log = ScrolledText(self.right, height=14, wrap="word")
        self.log.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.log.configure(state="disabled")

        # Footer content
        ttk.Label(
            self.footer,
            text="Tip: dùng Edit để sửa config.ini cho đúng COM/BAUDRATE trên máy.",
            foreground=MUTED,
            background=BG,
        ).grid(row=0, column=0, sticky="w")

        self.reload_config()
        self.set_status("IDLE", "Ready.")

        # ---- core runtime ----
        self._core_q = queue.SimpleQueue()
        self._core_thread: Optional[threading.Thread] = None
        self.bridge = None
        self._last_result_ts = 0.0
        self._last_result_status = ""

        # đảm bảo có handler đóng app
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # start core
        self._start_core()

        # poll core state
        self.after(100, self._poll_core_state)


    def _resolve_config_path(self, config_path: str | os.PathLike[str]) -> Path:
        p = Path(config_path)
        if p.is_absolute():
            return p
        # Prefer app_dir() to keep config next to exe/entry
        return app_dir() / p

    def _check_edit_key(self, key: str) -> bool:
        return (key or "").strip() == (self._edit_key or "").strip()

    def _unlock_edit(self) -> None:
        self._edit_unlocked_until = time.monotonic() + EDIT_UNLOCK_TTL_SEC

    def _is_edit_unlocked(self) -> bool:
        return time.monotonic() < float(getattr(self, "_edit_unlocked_until", 0.0))

    # -----------------------------
    # Mock UX / Status testing
    # -----------------------------
    def init_mock_ui(self, enabled: bool = True, *, interval_ms: int = 900) -> None:
        """
        Start/stop a status "demo" loop so you can evaluate the STATUS card UX
        without connecting any COM / running any backend.
        """
        if not enabled:
            self._stop_mock_ui()
            return

        self._mock_running = True
        self.btn_mock.configure(text="Stop Mock UX")

        def tick():
            if not self._mock_running:
                return
            code, desc = self._mock_seq[self._mock_i % len(self._mock_seq)]
            self._mock_i += 1
            self.set_status(code, desc)
            self.logger.info(f"[MOCK] {code}: {desc}")
            self._mock_after_id = self.after(interval_ms, tick)

        # Kick immediately
        tick()

    def _stop_mock_ui(self) -> None:
        return None
        self._mock_running = False
        self.btn_mock.configure(text="Start Mock UX")
        if self._mock_after_id:
            try:
                self.after_cancel(self._mock_after_id)
            except Exception:
                pass
            self._mock_after_id = None

    def _toggle_mock(self) -> None:
        return None
        if self._mock_running:
            self._stop_mock_ui()
            self.set_status("IDLE", "Mock stopped.")
            self.logger.info("[MOCK] stopped")
        else:
            self.init_mock_ui(True)

    # -----------------------------
    # UI helpers
    # -----------------------------
    def _pump_log_buffer(self):
        try:
            buf = getattr(self, "log_buff", None)
            if not buf:
                return

            lock = getattr(self.logger, "_laserlink_lock", None)

            def compute_new_lines():
                MAX_PUSH_PER_TICK = 250
                last_obj = getattr(self, "_last_log_obj", None)

                if last_obj is None:
                    nl = buf[-MAX_PUSH_PER_TICK:]
                else:
                    idx = -1
                    for i in range(len(buf) - 1, -1, -1):
                        if buf[i] is last_obj:
                            idx = i
                            break
                    nl = buf[idx + 1:] if idx >= 0 else buf[-MAX_PUSH_PER_TICK:]

                if len(nl) > MAX_PUSH_PER_TICK:
                    nl = nl[-MAX_PUSH_PER_TICK:]

                new_last = buf[-1] if buf else last_obj
                return list(nl), new_last

            if lock:
                with lock:
                    new_lines, new_last = compute_new_lines()
            else:
                new_lines, new_last = compute_new_lines()

            if new_lines:
                for line in new_lines:
                    self._append_log_view(line)
                self._last_log_obj = new_last

        finally:
            self.after(100, self._pump_log_buffer)


    def append_log(self, s: str, level: int = logging.INFO) -> None:
        """
        Public API: safe to call from any thread.
        Unified flow: append_log -> logger -> log_buff -> _pump_log_buffer -> UI.
        """
        msg = (s or "").rstrip("\r\n")
        try:
            self.logger.log(level, msg)
        except Exception:
            pass


    def _append_log_view(self, s: str) -> None:
        """
        UI-only: must be called in Tk main thread.
        """
        line = (s or "").rstrip() + "\n"
        self.log.configure(state="normal")
        self.log.insert("end", line)
        self._log_lines += 1

        if self._log_lines > self._log_max_lines:
            extra = self._log_lines - self._log_max_lines
            try:
                self.log.delete("1.0", f"{extra + 1}.0")
                self._log_lines = self._log_max_lines
            except tk.TclError:
                pass

        self.log.see("end")
        self.log.configure(state="disabled")

    # def set_status(self, code: str, desc: str = ""):
    #     code_u = (code or "").upper()
    #     self.status_big.configure(text=code_u)
    #     self.status_desc.configure(text=desc or "")

    #     # Simple status styling (foreground only to avoid ttk theme fights)
    #     if code_u in ("OK", "PASS", "READY"):
    #         self._set_status_colors(OK_FG)
    #     elif code_u in ("ERROR", "FAIL"):
    #         self._set_status_colors(ERR_FG)
    #     elif code_u in ("WARN", "WARNING"):
    #         self._set_status_colors(WARN_FG)
    #     else:
    #         self._set_status_colors(TEXT)

    def _set_status_colors(self, fg: str):
        self.status_big.configure(foreground=fg)
    
    def _apply_status_theme(self, bg: str, big_fg: str, sub_fg: str) -> None:
        # Update styles so whole status card changes background
        self._style.configure("StatusCard.TFrame", background=bg)
        self._style.configure("StatusTitle.TLabel", background=bg, foreground=sub_fg)
        self._style.configure("StatusBig.TLabel", background=bg, foreground=big_fg)
        self._style.configure("StatusDesc.TLabel", background=bg, foreground=sub_fg)

    def set_status(self, code: str, desc: str = ""):
        code_u = (code or "").upper()
        self.status_big.configure(text=code_u)
        self.status_desc.configure(text=desc or "")

        bg, big_fg, sub_fg = self.STATUS_THEMES.get(code_u, self.STATUS_THEMES["IDLE"])
        self._apply_status_theme(bg, big_fg, sub_fg)

    # -----------------------------
    # Actions
    # -----------------------------
    def open_info(self):
        info: list[str] = []
        info.append("COM Config Utility (Tkinter)")
        info.append(f"Config path: {self.config_path.name}")
        info.append("")
        info.append("Detected ports:")
        ports = list_ports()
        info.extend(ports if ports else ["(pyserial missing or no ports found)"])
        info.append("")
        info.append("Current loaded values:")
        snap = self.get_config_snapshot()
        for k in sorted(snap.keys()):
            info.append(f"  {k} = {snap[k]}")
        self.dialog_host.show(InfoDialog(self.dialog_host, "\n".join(info)))

    def open_edit_config(self):
        if self._is_edit_unlocked():
            self.dialog_host.show(EditConfigDialog(self.dialog_host, self))
            return

        def _go_edit():
            self.dialog_host.show(EditConfigDialog(self.dialog_host, self))

        self.dialog_host.show(EditKeyDialog(self.dialog_host, self, on_success=_go_edit))

    def get_config_snapshot(self) -> dict[str, str]:
        """
        Snapshot current config for UI (strings), sourced from src.core.CFG.
        """
        if self.cfg is None:
            return {
                "COM_LASER": "COM1",
                "COM_SFC": "COM2",
                "COM_SCAN": "COM3",
                "BAUDRATE_LASER": "9600",
                "BAUDRATE_SFC": "9600",
                "BAUDRATE_SCAN": "9600",
            }

        # Let CFG handle auto-reload based on mtime.
        self.cfg.reload_if_changed()
        com = self.cfg.com
        baud = self.cfg.baudrate

        return {
            "COM_LASER": str(getattr(com, "COM_LASER", "")),
            "COM_SFC": str(getattr(com, "COM_SFC", "")),
            "COM_SCAN": str(getattr(com, "COM_SCAN", "")),
            "BAUDRATE_LASER": str(getattr(baud, "BAUDRATE_LASER", 9600)),
            "BAUDRATE_SFC": str(getattr(baud, "BAUDRATE_SFC", 9600)),
            "BAUDRATE_SCAN": str(getattr(baud, "BAUDRATE_SCAN", 9600)),
        }

    def reload_config(self):
        if self.cfg is None:
            self.logger.info("[ERROR] CFG import failed: cannot load config via src.core.CFG")
            self.set_status("ERROR", "CFG import failed")
            return

        try:
            self.cfg.reload(force=True)
            self.logger.info("[OK] Reloaded config.ini via CFG")
            self.set_status("OK", "Config loaded")
        except Exception as e:
            self.logger.info(f"[ERROR] CFG reload failed: {e}")
            self.set_status("ERROR", "Config reload failed")
            self.dialog_host.show(ErrorDialog(self.dialog_host, "CFG RELOAD FAILED", str(e)))

    def save_config_values(self, com_updates: dict[str, str], baud_updates: dict[str, int]) -> tuple[bool, str]:
        """
        Save COM/BAUDRATE via CFG to keep GUI synced with src.core config pipeline.
        Requires ConfigManager.update_sections() to be added (see patch below).
        """
        if self.cfg is None:
            return False, "CFG import failed (src.core.CFG is None)"

        try:
            # If you added update_sections() in ConfigManager, use it.
            if hasattr(self.cfg, "update_sections"):
                ok = bool(self.cfg.update_sections({
                    "COM": com_updates,
                    "BAUDRATE": {k: str(v) for k, v in baud_updates.items()},
                }, make_backup=True, reload_after=True))
                if ok:
                    self.logger.info("[OK] Saved config.ini via CFG.update_sections()")
                    return True, ""
                return False, "CFG.update_sections() returned False"

            return False, "Missing CFG.update_sections(). Please apply core patch."

        except Exception as e:
            return False, f"Write failed: {e}"

    # -----------------------------
    # --------- Core logic ---------
    # -----------------------------
    def _start_core(self) -> None:
        if self.cfg is None or LaserSfcBridge is None:
            self.append_log("[CORE] LaserSfcBridge import failed -> core not started", logging.ERROR)
            self.set_status("ERROR", "Core not available")
            return

        def on_result(status: str, laser_req: str, sfc_resp: str) -> None:
            # chạy trong core thread -> chỉ push queue
            try:
                self._core_q.put((time.monotonic(), status, laser_req, sfc_resp))
            except Exception:
                pass

        try:
            self.bridge = LaserSfcBridge(
                self.cfg,
                log=self.append_log,       # thread-safe (đi qua logger)
                on_result=on_result,
                sfc_timeout=5.0,
                idle_sleep=0.01,
                break_on_reload=False,
            )
        except Exception as e:
            self.append_log(f"[CORE] init failed: {e}", logging.ERROR)
            self.set_status("ERROR", "Core init failed")
            return

        self._core_thread = threading.Thread(target=self.bridge.run_forever, daemon=True)
        self._core_thread.start()
        self.append_log("[CORE] started", logging.INFO)
        self.set_status("LISTENING", "Waiting for LASER trigger...")

    def _poll_core_state(self) -> None:
        try:
            # 1) Drain result queue (PASS/FAIL/TIMEOUT/...)
            while True:
                try:
                    ts, status, laser_req, sfc_resp = self._core_q.get_nowait()
                except Exception:
                    break

                self._last_result_ts = ts
                self._last_result_status = (status or "").upper()

                # Flash PASS/FAIL rõ ràng cho công nhân
                if self._last_result_status in ("PASS", "FAIL"):
                    desc = f"SFC: {sfc_resp}"
                    self.set_status(self._last_result_status, desc[:120])
                elif self._last_result_status in ("TIMEOUT", "SFC_ERROR"):
                    self.set_status("ERROR", f"{status}: {str(sfc_resp)[:120]}")
                else:
                    self.set_status("WARN", f"{status}: {str(sfc_resp)[:120]}")

            # 2) Nếu không có “result flash” gần đây -> bám theo mode (Listening/Testing/Error)
            if self.bridge is not None:
                now = time.monotonic()
                flash_active = (now - float(self._last_result_ts)) < 1.2 and self._last_result_status in ("PASS", "FAIL")

                if not flash_active:
                    ok, com_laser, txt = self.bridge.get_status_triplet()
                    mode = (self.bridge.get_mode() or "").upper()

                    if mode == "LISTENING":
                        self.set_status("LISTENING", f"LASER={com_laser}")
                    elif mode == "TESTING":
                        st, last_req, _ = self.bridge.get_last_result()
                        self.set_status("TESTING", f"LASER->SFC... ({str(last_req)[:60]})")
                    elif mode == "ERROR":
                        self.set_status("ERROR", self.bridge.get_last_error()[:140])
                    elif mode == "STOPPED":
                        self.set_status("STOPPED", "Core stopped")
                    else:
                        self.set_status("STANDBY", txt[:140])

        except Exception as e:
            # tuyệt đối không để poll làm crash UI
            try:
                self.append_log(f"[UI] _poll_core_state error: {e}", logging.ERROR)
            except Exception:
                pass
        finally:
            self.after(100, self._poll_core_state)

    def _on_close(self) -> None:
        # Không join trực tiếp (sẽ freeze UI). Request stop rồi poll thread.
        try:
            self.set_status("STOPPED", "Stopping core...")
        except Exception:
            pass

        try:
            if self.bridge is not None:
                self.bridge.request_stop()
        except Exception:
            pass

        self._close_deadline = time.monotonic() + 6.0  # tối đa chờ 6s (read_timeout)
        self.after(50, self._finalize_close)

    def _finalize_close(self) -> None:
        try:
            th = self._core_thread
            if th and th.is_alive():
                # quá deadline thì vẫn đóng UI (core thread là daemon)
                if time.monotonic() < getattr(self, "_close_deadline", 0):
                    self.after(50, self._finalize_close)
                    return
            self.destroy()
        except Exception:
            # fallback cực đoan: vẫn thoát
            try:
                self.destroy()
            except Exception:
                pass
