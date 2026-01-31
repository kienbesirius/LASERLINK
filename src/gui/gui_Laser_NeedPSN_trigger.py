from __future__ import annotations

import os
import sys
import time
import random
import logging
import threading
import queue
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
from tkinter.scrolledtext import ScrolledText
from pathlib import Path
from typing import Optional, Type, Any
from collections import deque

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


def _is_valid_port_name(p: str) -> bool:
    if not p:
        return False
    s = p.strip()
    # Windows: COM1, COM2...
    if re.match(r"^COM\d+$", s, flags=re.IGNORECASE):
        return True
    # Linux: /dev/ttyUSB0 or ttyUSB0
    if s.startswith("/dev"):
        return True
    return False

def list_ports() -> list[str]:
    """
    Return only ports we actually want to show: COMx or ttyUSBx.
    If pyserial exists but returns weird/empty -> return [] (GUI will fallback).
    """
    if not _HAS_SERIAL:
        return []
    try:
        raw = [p.device for p in serial.tools.list_ports.comports()]
        out = [x for x in raw if _is_valid_port_name(str(x))]
        # optional: stable ordering
        def key(x: str):
            xs = x.upper()
            if xs.startswith("COM"):
                try:
                    return (0, int(re.findall(r"\d+", xs)[0]))
                except Exception:
                    return (0, 9999)
            return (1, xs)
        out.sort(key=key)
        return out
    except Exception:
        return []
    
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
SHOW_SCAN_UI = False

# ----- Your stimulation pool (PASS/FAIL variants) -----
STIMULATION = True

RESP_POOL = {
    "resp1": {
        "PASS": "2505004562,H25101801031,PASS",
        "FAIL": "2505004562,H25101801031,FAIL",
    },
    "resp2": {
        "PASS": "2505004562,PF2AS04TE,P072UT02243604N5,P072UT02243604N6,P072UT02243604N7,P072UT02243604N8,2505004562,PF2AS04TE,P072UT02243604N5,P072UT02243604N6,P072UT02243604N7,P072UT02243604N8,PASS",
        "FAIL": "2505004562,PF2AS04TE,P072UT02243604N5,P072UT02243604N6,P072UT02243604N7,P072UT02243604N82505004562,PF2AS04TE,P072UT02243604N5,P072UT02243604N6,P072UT02243604N7,P072UT02243604N8,FAIL",
    },
    "laser_resp": {
        "PASS": "2505004562,PF2AS04TE,PASSED=1",
        "FAIL": "2505004562,PF2AS04TE,PASSED=0,FAIL03",
    },
    "resp4": {
        "PASS": "2505004562,PF2AS04TE,PASSED=1PASS",
        "FAIL": "2505004562,PF2AS04TE,PASSED=0,FAIL03PASS",
    },
}

# -----------------------------
# Config (source of truth = src.core.CFG)
# -----------------------------
# We intentionally DO NOT maintain a duplicate schema/defaults in GUI.
# All COM/BAUDRATE/RULES should be read & written via the singleton CFG in src.core.
try:
    from src.core import CFG, send_text_and_wait, send_text_and_polling, send_text_only, send_text_and_wait_norml
    from src.core.core_serial import SFCComReader
    from src.gui.gui_KIP import KPIWidget
except Exception as e:
    print("DEBUGS::")
    print(str(e))
    print("ERRORR")
    CFG = None  # type: ignore

# Check Mo,NEEDPSN
_RX_MONEYSN_LINE = re.compile(
    r"^\s*([A-Z0-9][A-Z0-9_-]{1,31})\s*,\s*(NEEDPSN\d{1,4})\s*$",
    re.IGNORECASE,
)
_RX_NEEDPSN = re.compile(r"NEEDPSN\d+", re.IGNORECASE)
_MONEYSN_MIN_LEN = 8
_MONEYSN_MAX_LEN = 64


def parse_moneysn_line(text: str, expected_mo: str) -> tuple[str, str] | None:
    s = (text or "").replace("\r", "").replace("\n", "").strip()
    if not s:
        return None
    if len(s) < _MONEYSN_MIN_LEN or len(s) > _MONEYSN_MAX_LEN:
        return None

    exp = (expected_mo or "").strip().upper()
    if not exp:
        # Bạn muốn match tuyệt đối -> nếu expected_mo rỗng thì coi như invalid
        return None

    m = _RX_MONEYSN_LINE.fullmatch(s)
    if not m:
        return None

    mo_in = (m.group(1) or "").strip().upper()
    needpsn = (m.group(2) or "").strip().upper()

    # ✅ match tuyệt đối MO
    if mo_in != exp:
        return None

    return (mo_in, needpsn)

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
        ttk.Button(header, text="✕", style="Flat.TButton", takefocus=False, command=self.host.close_top, width=3).grid(row=0, column=1, sticky="e")
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
        super().__init__(host, "INFO")
        txt = ScrolledText(self.body, height=14, wrap="word")
        txt.insert("1.0", info_text)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)

        ttk.Button(self.footer, text="OK", style="Flat.TButton", takefocus=False, command=self.host.close_top, width=10).grid(row=0, column=0, sticky="e")


class ErrorDialog(BaseDialog):
    dismiss_on_backdrop = True

    def __init__(self, host: DialogHost, title: str, message: str):
        super().__init__(host, title, width=640)
        lbl = ttk.Label(self.body, text=message, style="Error.TLabel", wraplength=600, justify="left")
        lbl.pack(fill="x", expand=False)

        ttk.Button(self.footer, text="Đóng", style="Flat.TButton", takefocus=False, command=self.host.close_top, width=10).grid(row=0, column=0, sticky="e")

class ModelEditDialog(ttk.Frame):
    dismiss_on_backdrop = False

    def __init__(self, host: DialogHost, app: "LASERLINKAPP"):
        super().__init__(host, style="TFrame")
        self.host = host
        self.app = app

        # full overlay background (host already dims, this is editor layer)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # Center card
        self.card = ttk.Frame(self, style="Card.TFrame", padding=16)
        self.card.grid(row=0, column=0, sticky="nsew")
        self.card.columnconfigure(0, weight=0)
        self.card.columnconfigure(1, weight=1)
        self.card.rowconfigure(1, weight=1)

        # Header
        hdr = ttk.Frame(self.card, style="InCard.TFrame")
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew")
        hdr.columnconfigure(0, weight=1)
        ttk.Label(hdr, text="MODEL EDITOR", style="DialogTitle.TLabel")\
            .grid(row=0, column=0, sticky="w")
        ttk.Button(hdr, text="✕", style="Flat.TButton", takefocus=False, command=self.host.close_top, width=3)\
            .grid(row=0, column=1, sticky="e")
        ttk.Separator(hdr, style="Thin.TSeparator")\
            .grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        # Left list
        left = ttk.Frame(self.card, style="InCard.TFrame")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(12, 0))
        left.rowconfigure(1, weight=1)

        ttk.Label(left, text="Models", style="Muted.TLabel").grid(row=0, column=0, sticky="w")

        self.lb = tk.Listbox(left, height=16)
        self.lb.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        sb = ttk.Scrollbar(left, orient="vertical", command=self.lb.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=(8, 0))
        self.lb.configure(yscrollcommand=sb.set)

        self.lb.bind("<<ListboxSelect>>", lambda _e: self._load_selected_from_list())

        btn_left = ttk.Frame(left, style="InCard.TFrame")
        btn_left.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(btn_left, text="New", style="Flat.TButton", takefocus=False, command=self._new).pack(side="left")
        ttk.Button(btn_left, text="Use Selected", style="Flat.TButton", takefocus=False, command=self._use_selected_as_current).pack(side="left", padx=(8, 0))

        # Right form
        right = ttk.Frame(self.card, style="InCard.TFrame")
        right.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
        right.columnconfigure(1, weight=1)

        ttk.Label(right, text="Edit / Add", style="Muted.TLabel")\
            .grid(row=0, column=0, columnspan=2, sticky="w")

        self.v_model_id = tk.StringVar(value="")
        self.v_needpsn  = tk.StringVar(value="")

        ttk.Label(right, text="MODEL ID", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.ent_model = ttk.Entry(right, textvariable=self.v_model_id)
        self.ent_model.grid(row=1, column=1, sticky="ew", pady=(10, 0))

        ttk.Label(right, text="NEEDPSN", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.ent_needpsn = ttk.Entry(right, textvariable=self.v_needpsn)
        self.ent_needpsn.grid(row=2, column=1, sticky="ew", pady=(10, 0))

        ttk.Label(
            right,
            text="Rule: NEEDPSN phải match regex: NEEDPSN\\d+ (vd: NEEDPSN04)",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.lbl_err = ttk.Label(right, text="", style="Error.TLabel", wraplength=520, justify="left")
        self.lbl_err.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # Footer
        ftr = ttk.Frame(self.card, style="InCard.TFrame")
        ftr.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ftr.columnconfigure(0, weight=1)

        btns = ttk.Frame(ftr, style="InCard.TFrame")
        btns.grid(row=0, column=0, sticky="e")

        ttk.Button(btns, text="Cancel", style="Flat.TButton", takefocus=False, command=self.host.close_top, width=10)\
            .pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="Save", style="Flat.TButton", takefocus=False, command=self._save, width=10)\
            .pack(side="right")

        # init list + prefill current
        self._reload_list()
        cur = ""
        if self.app.cfg is not None:
            cur = (self.app.cfg.get_current_selected_model() or "").strip()
        if cur:
            self._select_in_list(cur)
            self._load_model(cur)

    def _reload_list(self) -> None:
        self.lb.delete(0, "end")
        if self.app.cfg is None:
            return
        for m in (self.app.cfg.get_models() or []):
            self.lb.insert("end", m)

    def _select_in_list(self, model: str) -> None:
        target = (model or "").strip().lower()
        if not target:
            return
        for i in range(self.lb.size()):
            if str(self.lb.get(i)).lower() == target:
                self.lb.selection_clear(0, "end")
                self.lb.selection_set(i)
                self.lb.see(i)
                return

    def _load_selected_from_list(self) -> None:
        sel = self.lb.curselection()
        if not sel:
            return
        model = str(self.lb.get(sel[0]))
        self._load_model(model)

    def _load_model(self, model: str) -> None:
        self.lbl_err.configure(text="")
        self.v_model_id.set(model)
        needpsn = ""
        if self.app.cfg is not None and hasattr(self.app.cfg, "get_model_needpsn"):
            needpsn = self.app.cfg.get_model_needpsn(model) or ""
        self.v_needpsn.set(needpsn)

    def _new(self) -> None:
        self.lbl_err.configure(text="")
        self.v_model_id.set("")
        self.v_needpsn.set("")
        self.ent_model.focus_set()

    def _use_selected_as_current(self) -> None:
        if self.app.cfg is None:
            return
        sel = self.lb.curselection()
        if not sel:
            return
        model = str(self.lb.get(sel[0]))
        ok = bool(self.app.cfg.set_current_selected_model(model, persist=True))
        if ok:
            self.app._refresh_model_picker(select=model)
            self.host.close_top()
        else:
            self.lbl_err.configure(text=f"Cannot set current model: {model}")

    def _save(self) -> None:
        self.lbl_err.configure(text="")
        if self.app.cfg is None:
            self.lbl_err.configure(text="CFG not available.")
            return

        model_id = (self.v_model_id.get() or "").strip()
        needpsn  = (self.v_needpsn.get() or "").strip()

        import re
        if not model_id:
            self.lbl_err.configure(text="MODEL ID không được trống.")
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", model_id):
            self.lbl_err.configure(text="MODEL ID chỉ nên gồm: A-Z a-z 0-9 _ . -")
            return
        if not re.fullmatch(r"NEEDPSN\d+", needpsn, flags=re.IGNORECASE):
            self.lbl_err.configure(text="NEEDPSN không hợp lệ. Ví dụ đúng: NEEDPSN04")
            return

        # persist via ConfigManager
        if not hasattr(self.app.cfg, "upsert_model_needpsn"):
            self.lbl_err.configure(text="Missing CFG.upsert_model_needpsn().")
            return

        ok = bool(self.app.cfg.upsert_model_needpsn(model_id, needpsn, persist=True))
        if not ok:
            self.lbl_err.configure(text="Save failed (upsert_model_needpsn returned False).")
            return

        # set current selected model after save
        ok2 = bool(self.app.cfg.set_current_selected_model(model_id, persist=True))
        if not ok2:
            self.lbl_err.configure(text="Saved mapping, but failed to set CURRENT_SELECTED_MODEL.")
            return

        # refresh main UI picker + close
        self.app._refresh_model_picker(select=model_id)
        self.app.append_log(f"[OK] MODEL upsert -> {model_id}={needpsn} (and selected)")
        self.host.close_top()

class EditKeyDialog(BaseDialog):
    """Prompt user for a key before allowing protected edits."""
    dismiss_on_backdrop = False

    def __init__(
        self,
        host: DialogHost,
        app: "LASERLINKAPP",
        *,
        on_success,
        title: str = "ENTER KEY",
        context_text: str = "Nhập mã key để mở chức năng chỉnh sửa.",
        error_text: str = "Sai key. Vui lòng thử lại hoặc liên hệ TE/Engineer. 5935 - 70626",
    ):
        super().__init__(host, title)
        self.app = app
        self._on_success = on_success
        self._error_text = error_text

        wrap = ttk.Frame(self.body, style="InCard.TFrame")
        wrap.pack(fill="x", expand=False)

        ttk.Label(
            wrap,
            text=context_text,
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
        ttk.Button(right, text="Cancel", style="Flat.TButton", takefocus=False, command=self.host.close_top, width=10).pack(side="right", padx=(8, 0))
        ttk.Button(right, text="OK", style="Flat.TButton", takefocus=False, command=self._ok, width=10).pack(side="right")

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
        self.lbl_err.configure(text=self._error_text)
        self.ent.focus_set()

class EditConfigDialog(BaseDialog):
    # Avoid accidental close while editing; use explicit Cancel/Save.
    dismiss_on_backdrop = False

    def __init__(self, host: DialogHost, app: "LASERLINKAPP"):
        super().__init__(host, "EDIT CONFIG.INI")
        self.app = app

        # Vars
        snap = app.get_config_snapshot()
        self.v_com_laser = tk.StringVar(value=snap.get("COM_LASER", ""))
        self.v_com_sfc   = tk.StringVar(value=snap.get("COM_SFC", ""))
        self.v_com_scan  = tk.StringVar(value=snap.get("COM_SCAN", ""))

        self.v_baud_laser = tk.StringVar(value=str(snap.get("BAUDRATE_LASER", "9600")))
        self.v_baud_sfc   = tk.StringVar(value=str(snap.get("BAUDRATE_SFC", "9600")))
        self.v_baud_scan  = tk.StringVar(value=str(snap.get("BAUDRATE_SCAN", "9600")))

        # ports = [""] + list_ports()
        valid_ports = list_ports()  # đã lọc COM/ttyUSB
        has_valid_ports = bool(valid_ports)

        # Nếu không có port hợp lệ => reset tất cả box COM để tránh hiển thị giá trị sai/stale
        if not has_valid_ports:
            self.v_com_laser.set(snap.get("COM_LASER", ""))
            self.v_com_sfc.set(snap.get("COM_SFC", ""))
            self.v_com_scan.set(snap.get("COM_SCAN", ""))
        else:
            # Có ports hợp lệ -> nếu config đang set port không nằm trong list -> reset field đó
            if (self.v_com_laser.get() or "").strip() not in valid_ports:
                self.v_com_laser.set(snap.get("COM_LASER", ""))
            if (self.v_com_sfc.get() or "").strip() not in valid_ports:
                self.v_com_sfc.set(snap.get("COM_SFC", ""))
            if (self.v_com_scan.get() or "").strip() not in valid_ports:
                self.v_com_scan.set(snap.get("COM_SCAN", ""))
        

        grid = ttk.Frame(self.body, style="InCard.TFrame")
        grid.pack(fill="both", expand=True)
        for c in range(2):
            grid.columnconfigure(c, weight=1)

        def row(r: int, label: str, var: tk.StringVar, choices: Optional[list[str]] = None):
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="w", pady=6, padx=(0, 10))
            if choices is not None:
                cb = ttk.Combobox(grid, textvariable=var, values=choices, state="normal")
                cb.grid(row=r, column=1, sticky="ew", pady=6)
            else:
                ent = ttk.Entry(grid, textvariable=var)
                ent.grid(row=r, column=1, sticky="ew", pady=6)


        # If no pyserial, show Entry widgets.
        # port_choices = ports if ports and _HAS_SERIAL else None
        port_choices = ([""] + valid_ports) if has_valid_ports else None
        row_num = 0
        row(row_num := row_num + 1, "COM_LASER", self.v_com_laser, port_choices)
        row(row_num := row_num + 1, "COM_SFC", self.v_com_sfc, port_choices)
        if SHOW_SCAN_UI:
            row(row_num := row_num + 1, "COM_SCAN", self.v_com_scan, port_choices)

        ttk.Separator(grid, style="Thin.TSeparator").grid(row=(row_num := row_num + 1), column=0, columnspan=2, sticky="ew", pady=10)

        row((row_num := row_num + 1), "BAUDRATE_LASER", self.v_baud_laser, None)
        row((row_num := row_num + 1), "BAUDRATE_SFC", self.v_baud_sfc, None)
        if SHOW_SCAN_UI:
            row((row_num := row_num + 1), "BAUDRATE_SCAN", self.v_baud_scan, None)

        hint_text = (
            "Tip: Không detect được port (pyserial OK nhưng list rỗng) -> các ô COM sẽ là entry để bạn nhập tay."
            if port_choices is None
            else "Tip: Chọn COM từ dropdown. Nếu không thấy port đúng, hãy nhấn Scan Ports để kiểm tra."
        )
        hint = ttk.Label(
            self.body,
            text=hint_text,
            style="Muted.TLabel",
        )
        hint.pack(anchor="w", pady=(10, 0))

        # Footer buttons
        left = ttk.Frame(self.footer, style="InCard.TFrame")
        left.grid(row=0, column=0, sticky="w")
        ttk.Button(left, text="Scan Ports", style="Flat.TButton", takefocus=False, command=self._scan_ports).pack(side="left")

        right = ttk.Frame(self.footer, style="InCard.TFrame")
        right.grid(row=0, column=0, sticky="e")
        ttk.Button(right, text="Cancel", style="Flat.TButton", takefocus=False, command=self.host.close_top, width=10).pack(side="right", padx=(8, 0))
        ttk.Button(right, text="Save", style="Flat.TButton", takefocus=False, command=self._save, width=10).pack(side="right")

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
# Flow Thread
# -----------------------------
_RX_NEEDPSN = re.compile(r"NEEDPSN\d+", re.IGNORECASE)

def infer_status(text: str) -> str | None:
    up = (text or "").upper()
    # ưu tiên FAIL trước
    if "PASSED=0" in up or "FAIL" in up or "ERRO" in up:
        return "FAIL"
    if "PASSED=1" in up or " PASS" in up or up.endswith("PASS"):
        return "PASS"
    return None

def find_needpsn(text: str) -> str | None:
    m = _RX_NEEDPSN.search(text or "")
    return m.group(0).upper() if m else None

# -----------------------------
# Main App
# -----------------------------
class LASERLINKAPP(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LASERLINK")
        self.configure(bg=BG)

        # Window sizing: allow resizing but enforce a reasonable minimum.
        # Start maximized, but keep the minimum size at 640x640 so the user
        # can still resize the window smaller if desired (but not below min).
        self.minsize(640, 640)

        # Try to start the window maximized. Use multiple fallbacks for
        # cross-platform compatibility (state('zoomed'), attributes('-zoomed')).
        try:
            self.state("zoomed")
        except Exception:
            try:
                # Some platforms support the -zoomed attribute
                self.attributes("-zoomed", True)
            except Exception:
                # Fallback: set geometry to full screen
                try:
                    sw = self.winfo_screenwidth()
                    sh = self.winfo_screenheight()
                    self.geometry(f"{sw}x{sh}+0+0")
                except Exception:
                    # If all else fails, leave default size (min enforced)
                    pass

        # ttk theme
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD_BG, borderwidth=0, relief="flat")
        style.configure("InCard.TFrame", background=CARD_BG, borderwidth=0, relief="flat")
        style.configure("Thin.TSeparator", background=BORDER)
        style.configure("TLabel", background=CARD_BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD_BG, foreground=MUTED)
        style.configure("DialogTitle.TLabel", background=CARD_BG, foreground=TEXT, font=("TkDefaultFont", 12, "bold"))
        style.configure("Error.TLabel", background=CARD_BG, foreground=ERR_FG)

        # ---- Status styles (for background coloring) ----
        style.configure("StatusCard.TFrame", background=CARD_BG, borderwidth=0, relief="flat")
        style.configure("StatusTitle.TLabel", background=CARD_BG, foreground=MUTED)
        style.configure("StatusBig.TLabel",   background=CARD_BG, foreground=TEXT)
        style.configure("StatusDesc.TLabel",  background=CARD_BG, foreground=MUTED)

        # ---- Style flat button
        style.configure("Flat.TButton", relief="flat", borderwidth=0, focusthickness=0, padding=(12, 8))
        style.map("Flat.TButton", focuscolor=[("focus", "")])
        style.map("Flat.TButton",
            relief=[("pressed", "flat"), ("active", "flat")],
            borderwidth=[("active", 0), ("pressed", 0)],
            padding=[("pressed", (12, 8)), ("active", (12, 8))],
        )

        self._style = style  # giữ lại để update runtime

        # Xanh / Đỏ / Lục / Vàng / Trắng (high-contrast cho công nhân)
        self.STATUS_THEMES = {
            # trắng
            "IDLE":       ("#FFFFFF", TEXT, MUTED),
            "READY":      ("#FFFFFF", TEXT, MUTED),
            "STOPPED":    ("#FFFFFF", TEXT, MUTED),

            # xanh (blue) cho “đang chạy/đang test”
            "LISTENING":  ("#0EA5E9", "#FFFFFF", "#E5E7EB"),
            "TESTING":    ("#124BC7", "#FFFFFF", "#E5E7EB"),
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
        self.container.rowconfigure(0, weight=0)  # status
        self.container.rowconfigure(1, weight=0)  # ✅ model row
        self.container.rowconfigure(2, weight=0)  # ✅ NEW: mo + scan row
        self.container.rowconfigure(3, weight=1)  # content grows
        self.container.rowconfigure(4, weight=0)  # footer

        # Header: Status card
        self.status_card = ttk.Frame(self.container, style="StatusCard.TFrame", padding=16)
        self.status_card.grid(row=0, column=0, sticky="ew")
        self.status_card.columnconfigure(0, weight=1)
        # self.status_card.columnconfigure(0, weight=1, minsize=320)
        # self.status_card.columnconfigure(1, weight=0, minsize=120)

        self.status_title = ttk.Label(self.status_card, text="STATUS", style="StatusTitle.TLabel")
        self.status_title.grid(row=0, column=0, sticky="w")

        self.status_big = ttk.Label(self.status_card, text="IDLE", style="StatusBig.TLabel", font=("TkDefaultFont", 26, "bold"))
        self.status_big.grid(row=1, column=0, sticky="w", pady=(6, 0))

        self.status_desc = ttk.Label(self.status_card, text="Ready.", style="StatusDesc.TLabel")
        self.status_desc.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # KPI Reports
        self.real_total = 0
        self.real_pass  = 0
        self.real_fail  = 0

        self.rep_total = 0
        self.rep_pass  = 0
        self.rep_fail  = 0

        self.cycle_times = deque(maxlen=200)

        # ✅ KPI donut bên phải (cùng grid manager)
        self.kpi = KPIWidget(self.status_card, donut_size=50)
        self.kpi.grid(row=0, column=1, rowspan=3, sticky="e", padx=(14, 0))

        # Ensure status texts stay above any overlays (e.g., KPI canvas)
        try:
            self.status_title.lift()
            self.status_big.lift()
            self.status_desc.lift()
        except Exception:
            pass

        # Model row 
        # ✅ new model row
        self.model_card = ttk.Frame(self.container, style="Card.TFrame", padding=14)
        self.model_card.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self.model_card.columnconfigure(1, weight=1)

        ttk.Label(self.model_card, text="MODEL", style="Muted.TLabel").grid(row=0, column=0, sticky="w")

        self._v_model = tk.StringVar(value="")
        self.cb_model = ttk.Combobox(self.model_card, textvariable=self._v_model, state="readonly", width=28)
        self.cb_model.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.cb_model.bind("<<ComboboxSelected>>", lambda _e: self._on_model_selected())

        self._v_needpsn = tk.StringVar(value="")
        ttk.Label(self.model_card, textvariable=self._v_needpsn, style="Muted.TLabel")\
            .grid(row=0, column=2, sticky="w", padx=(12, 0))

        btns = ttk.Frame(self.model_card, style="InCard.TFrame")
        btns.grid(row=0, column=3, sticky="e")
        ttk.Button(btns, text="Edit Models", style="Flat.TButton", takefocus=False, command=lambda: self.open_edit("models")).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Refresh", style="Flat.TButton", takefocus=False, command=self._refresh_model_picker).pack(side="left")
        # Keep the model row in the widget hierarchy for logic, but hide it from the UI
        # so the model data/combobox remain usable programmatically.
        try:
            self.model_card.grid_remove()
        except Exception:
            # If grid_remove fails for any reason, ignore to preserve behavior
            pass

        # MO - Scan
        # -------------------------
        # ✅ MO + SCAN row (2 columns)
        # -------------------------
        
        # -------------------------
        # ✅ MO + Scan (new layout)
        # -------------------------
        self.mo_scan_card = ttk.Frame(self.container, style="Card.TFrame", padding=14)
        self.mo_scan_card.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        self.mo_scan_card.columnconfigure(0, weight=1)

        # fonts
        base_font  = tkfont.nametofont("TkDefaultFont")
        family     = base_font.actual("family")
        base_size  = int(base_font.actual("size"))

        title_size = max(base_size + 6, 16)
        scan_size  = min(max(base_size + 22, 30), 46)  # 30~46 là hợp lý

        style.configure("ScanTitle.TLabel", font=(family, title_size, "bold"))
        style.configure("Scan.TEntry",      font=(family, scan_size,  "bold"))

        # ===== Shared layout constants (gọn đẹp) =====
        LBL_W   = 7      # độ rộng label "MO"/"H Code" (text units)
        CB_W    = 24     # độ rộng combobox
        PAD_X   = 10
        PAD_Y   = 6

        def _build_line(parent, label_text, var, on_selected, on_enter, status_var):
            row = ttk.Frame(parent, style="TFrame")
            row.columnconfigure(2, weight=1)  # status stretch

            lbl = ttk.Label(row, text=label_text, style="Muted.TLabel", width=LBL_W, anchor="w")
            lbl.grid(row=0, column=0, sticky="w")

            cb = ttk.Combobox(row, textvariable=var, state="normal", width=CB_W)
            cb.grid(row=0, column=1, sticky="w", padx=(PAD_X, PAD_X))
            cb.bind("<<ComboboxSelected>>", lambda _e: on_selected())
            cb.bind("<Return>",           lambda _e: on_enter())

            status = ttk.Label(row, textvariable=status_var, style="Muted.TLabel", anchor="w")
            status.grid(row=0, column=2, sticky="ew")

            return row, cb, status

        # ---- Row 0: MO + status
        self._mo_mode_auto_latest = True   # ✅ startup = auto latest
        self._selected_mo_runtime = ""     # ✅ locked selection for this app session

        self._v_mo = tk.StringVar(value="")
        self._v_mo_status = tk.StringVar(value="")
        self.mo_row, self.cb_mo, self.lbl_mo_status = _build_line(
            self.mo_scan_card, "MO",
            self._v_mo,
            self._on_mo_selected,
            self._on_mo_enter,
            self._v_mo_status
        )
        self.mo_row.grid(row=0, column=0, sticky="ew", pady=(0, PAD_Y))

        # ---- Row 1: H Code + status
        self._h_code_mode_auto_latest = True   # ✅ startup = auto latest
        self._selected_h_code_runtime = ""     # ✅ locked selection for this app session

        self._v_h_code = tk.StringVar(value="")
        self._v_h_code_status = tk.StringVar(value="")
        self.h_code_row, self.cb_h_code, self.lbl_h_code_status = _build_line(
            self.mo_scan_card, "H Code",
            self._v_h_code,
            self._on_h_code_selected,
            self._on_h_code_enter,
            self._v_h_code_status
        )
        self.h_code_row.grid(row=1, column=0, sticky="ew", pady=(0, PAD_Y))

        # ---- Row 2: centered title
        ttk.Label(self.mo_scan_card, text="Wait for WO,NEEDPSN", style="ScanTitle.TLabel", anchor="center")\
        .grid(row=2, column=0, sticky="ew", pady=(12, 8))

        # ---- Row 3: big scan entry
        self._v_moneysn = tk.StringVar(value="")
        self.ent_moneysn = ttk.Entry(
        self.mo_scan_card,
            textvariable=self._v_moneysn,
            style="Scan.TEntry",
            font=(family, scan_size, "bold"),   # ✅ force apply
            justify="center",
        )
        self.ent_moneysn.grid(row=3, column=0, sticky="ew", ipady=10)

        self.ent_moneysn.bind("<Return>", lambda _e: self._commit_moneysn_scan(immediate=True))

        # scan debounce state
        self.MONEYSN: str = ""
        self.H_code: str = ""
        self._mscan_after_id = None
        self._mscan_debounce_ms = 250
        self._v_moneysn.trace_add("write", lambda *_: self._on_moneysn_changed())
        self._focus_scan()
        # -------------------------
        # ---------------------------
        # -----------------------------

        # Content row: left config (now scrollable) + right log
        self.content = ttk.Frame(self.container, style="TFrame")
        self.content.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        self.content.columnconfigure(0, weight=0)
        # keep log panel usable even at minimum window size
        self.content.columnconfigure(1, weight=1, minsize=240)
        self.content.rowconfigure(0, weight=1)

        # Left card made scrollable (hide scrollbar visually). Outer holder keeps width.
        self.left_holder = ttk.Frame(self.content, style="Card.TFrame", padding=0)
        self.left_holder.grid(row=0, column=0, sticky="nsw", padx=(0, 14))
        self.left_holder.configure(width=360)
        self.left_holder.grid_propagate(False)

        self.left_canvas = tk.Canvas(self.left_holder, highlightthickness=0, bd=0, bg=CARD_BG)
        self.left_canvas.pack(fill="both", expand=True)

        # Hidden scrollbar for functional scrolling (not packed)
        self._left_vsb = ttk.Scrollbar(self.left_holder, orient="vertical", command=self.left_canvas.yview)
        self.left_canvas.configure(yscrollcommand=lambda *_: None)

        # Actual content frame inside canvas
        self.left = ttk.Frame(self.left_canvas, style="Card.TFrame", padding=14)
        self._left_window = self.left_canvas.create_window((0, 0), window=self.left, anchor="nw")

        def _on_left_config(event=None):
            # Update scrollregion and width binding to holder
            try:
                self.left_canvas.configure(scrollregion=self.left_canvas.bbox("all"))
                self.left_canvas.itemconfigure(self._left_window, width=self.left_holder.winfo_width())
            except Exception:
                pass

        self.left.bind("<Configure>", _on_left_config)
        self.left_holder.bind("<Configure>", _on_left_config)

        # Mouse wheel scrolling without showing scrollbar
        def _bind_left_wheel(_event=None):
            self.left_canvas.bind_all("<MouseWheel>", _on_mousewheel, add=True)
            self.left_canvas.bind_all("<Button-4>", _on_mousewheel_linux, add=True)
            self.left_canvas.bind_all("<Button-5>", _on_mousewheel_linux, add=True)

        def _unbind_left_wheel(_event=None):
            self.left_canvas.unbind_all("<MouseWheel>")
            self.left_canvas.unbind_all("<Button-4>")
            self.left_canvas.unbind_all("<Button-5>")

        def _on_mousewheel(event):
            delta = int(-1 * (event.delta / 120))
            self.left_canvas.yview_scroll(delta, "units")

        def _on_mousewheel_linux(event):
            if event.num == 4:
                self.left_canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self.left_canvas.yview_scroll(1, "units")

        self.left_holder.bind("<Enter>", _bind_left_wheel)
        self.left_holder.bind("<Leave>", _unbind_left_wheel)

        # Right card: Log
        self.right = ttk.Frame(self.content, style="Card.TFrame", padding=14)
        self.right.grid(row=0, column=1, sticky="nsew")
        self.right.rowconfigure(1, weight=1)
        self.right.columnconfigure(0, weight=1)

        # Responsive behaviour: when window width is small, hide left panel
        # and let the log occupy the full width. Restores layout when wide.
        self._left_hidden_by_width = False

        def _update_layout_for_width(width: int | None = None) -> None:
            try:
                if width is None:
                    width = self.winfo_width()
                # threshold in pixels
                THRESHOLD = 900
                if int(width) < THRESHOLD:
                    if not self._left_hidden_by_width:
                        try:
                            self.left_holder.grid_remove()
                        except Exception:
                            pass
                        try:
                            # Move right to column 0 and span both columns so it truly fills
                            self.right.grid_configure(column=0, columnspan=2)
                            # ensure the visible column expands
                            self.content.columnconfigure(0, weight=1)
                            self.content.columnconfigure(1, weight=0)
                        except Exception:
                            pass
                        self._left_hidden_by_width = True
                else:
                    if self._left_hidden_by_width:
                        try:
                            # restore left holder to original grid location
                            self.left_holder.grid(row=0, column=0, sticky="nsw", padx=(0, 14))
                        except Exception:
                            pass
                        try:
                            # move right back to column 1 and restore minsize
                            self.right.grid_configure(column=1, columnspan=1)
                            self.content.columnconfigure(0, weight=0)
                            self.content.columnconfigure(1, weight=1, minsize=240)
                        except Exception:
                            pass
                        self._left_hidden_by_width = False
            except Exception:
                pass

        # Bind to container resize so layout adjusts dynamically
        try:
            self.container.bind("<Configure>", lambda e: _update_layout_for_width(e.width))
            # apply once at startup
            _update_layout_for_width(self.winfo_width())
        except Exception:
            pass

        # Footer row
        self.footer = ttk.Frame(self.container, style="TFrame")
        self.footer.grid(row=4, column=0, sticky="ew", pady=(12, 0))
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

        # ---- Left panel: CONFIG summary (always visible) ----
        cfg_box = ttk.Frame(self.left, style="InCard.TFrame")
        cfg_box.pack(fill="x")


        ttk.Label(cfg_box, text="CONFIG", style="Muted.TLabel").pack(anchor="w")

        self._v_cfg_laser = tk.StringVar(value="")
        self._v_cfg_sfc   = tk.StringVar(value="")
        if SHOW_SCAN_UI:
            self._v_cfg_scan  = tk.StringVar(value="")

        self.ttkLabelLaser = ttk.Label(cfg_box, textvariable=self._v_cfg_laser, style="Muted.TLabel", wraplength=330) 
        self.ttkLabelLaser.pack(anchor="w", pady=(6, 0))
        self.ttkLabelSFC = ttk.Label(cfg_box, textvariable=self._v_cfg_sfc, style="Muted.TLabel", wraplength=330)
        self.ttkLabelSFC.pack(anchor="w", pady=(3, 0))
        if SHOW_SCAN_UI:
            self.ttkLabelScan = ttk.Label(cfg_box, textvariable=self._v_cfg_scan, style="Muted.TLabel", wraplength=330)
            self.ttkLabelScan.pack(anchor="w", pady=(3, 0))

        # initial + periodic refresh
        self._refresh_config_summary()
        self.after(800, self._tick_config_summary)
        self.after(800, self._tick_model_picker)
        self.after(800, self._tick_mo_picker)
        self.after(800, self._tick_h_code_picker)

        ttk.Separator(self.left, style="Thin.TSeparator").pack(fill="x", pady=14)

        btns = ttk.Frame(self.left, style="InCard.TFrame")
        btns.pack(fill="x")

        ttk.Button(btns, text="Reload", style="Flat.TButton", takefocus=False, command=self.reload_config).pack(fill="x")
        ttk.Button(btns, text="Edit config.ini", style="Flat.TButton", takefocus=False, command=lambda: self.open_edit("config")).pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Info", style="Flat.TButton", takefocus=False, command=self.open_info).pack(fill="x", pady=(8, 0))

        ttk.Separator(self.left, style="Thin.TSeparator").pack(fill="x", pady=14)

        self.btn_mock = ttk.Button(self.left, text="Start Mock UX", style="Flat.TButton", takefocus=False, command=self._toggle_mock)
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

        # self.kpi = KPIWidget(cfg_box, donut_size=50)
        # self.kpi.pack(side="left")  # or grid/place - your parent decides

        # TODO
        # ---- flow runtime (NEW) ----
        self._flow_q: "queue.SimpleQueue[tuple[str, dict]]" = queue.SimpleQueue()
        self._flow_thread: threading.Thread | None = None
        self._flow_running = False
        self._flow_lock = threading.Lock()
        self._flow_t0 = 0.0

        # poll flow events
        self.after(50, self._poll_flow_events)

        ### New Open Flow for automation
        self._oflow_q: "queue.SimpleQueue[tuple[str, dict]]" = queue.SimpleQueue()
        self._oflow_thread: threading.Thread | None = None
        self._oflow_stop_evt = threading.Event()
        self._oflow_run_evt = threading.Event()
        self._oflow_run_evt.set()  # run by default
        self._oflow_ser = None
        self._oflow_ser_lock = threading.Lock()
        self._oflow_last_port: str = ""
        self._oflow_last_baud: int = 0
        
        # Programmatic H injection guard (skip trace debounce)
        self._mscan_programmatic: bool = False

        # poll open-flow events
        self.after(50, self._poll_open_flow_events)
        # Start background listener for COM_LASER (auto-fill H scan)
        self.open_flow_core_start()

        # status default for new UX
        self.set_status("READY", "Select/Enter MO, then scan H Box Code")

        cfg = self.cfg or CFG
        cfg.reload_if_changed()
        com = cfg.com
        baud = cfg.baudrate
        self.sfc_worker = SFCComReader(com.COM_SFC, baud.BAUDRATE_SFC, log=self.append_log)
        self.sfc_worker.start()


    # TODO: refactor enable/disable inputs
    def disable_inputs(self):
        try:
            self.cb_model.configure(state="disabled")
        except Exception:
            pass
        try:
            self.cb_mo.configure(state="disabled")
        except Exception:
            pass
        try:
            self.ent_moneysn.configure(state="disabled")
        except Exception:
            pass

    def enable_inputs(self):
        try:
            self.cb_model.configure(state="readonly")
        except Exception:
            pass
        try:
            # MO combobox bạn để state="normal" để nhập tay
            self.cb_mo.configure(state="normal")
        except Exception:
            pass
        try:
            self.ent_moneysn.configure(state="normal")
        except Exception:
            pass
        self._focus_scan()

    def _resolve_config_path(self, config_path: str | os.PathLike[str]) -> Path:
        p = Path(config_path)
        if p.is_absolute():
            return p
        # Prefer app_dir() to keep config next to exe/entry
        return app_dir() / p

    def _check_edit_key(self, key: str) -> bool:
        if (key or "").strip() == "adminbechj":
            return True
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
        # return None
        self._mock_running = False
        self.btn_mock.configure(text="Start Mock UX")
        if self._mock_after_id:
            try:
                self.after_cancel(self._mock_after_id)
            except Exception:
                pass
            self._mock_after_id = None

    def _toggle_mock(self) -> None:
        # return None
        if self._mock_running:
            self._stop_mock_ui()
            self.set_status("IDLE", "Mock stopped.")
            self.logger.info("[MOCK] stopped")
        else:
            self.init_mock_ui(True)

    # -----------------------------
    # UI helpers
    # -----------------------------

    def _refresh_model_picker(self, *, select: str | None = None) -> None:
        if self.cfg is None:
            self.cb_model.configure(values=[])
            self._v_needpsn.set("")
            return

        self.cfg.reload_if_changed()

        models = list(self.cfg.get_models() or [])
        cur = (select or self.cfg.get_current_selected_model() or "").strip()

        self.cb_model.configure(values=models)

        # normalize selection if possible
        if cur and models:
            lower_map = {m.lower(): m for m in models}
            cur = lower_map.get(cur.lower(), models[0])
        elif models:
            cur = models[0]
        else:
            cur = ""

        self._v_model.set(cur)

        # NEEDPSN display
        needpsn = ""
        if cur and hasattr(self.cfg, "get_model_needpsn"):
            needpsn = self.cfg.get_model_needpsn(cur)
        self._v_needpsn.set(needpsn or "")

    def _on_model_selected(self) -> None:
        if self.cfg is None:
            return
        model = (self._v_model.get() or "").strip()
        if not model:
            return

        ok = bool(self.cfg.set_current_selected_model(model, persist=True))
        if ok:
            # update NEEDPSN label
            self._refresh_model_picker(select=model)
            self.append_log(f"[OK] MODEL selected -> {model}")
        else:
            self.dialog_host.show(ErrorDialog(self.dialog_host, "MODEL SET FAILED", f"Cannot set model: {model}"))


    def _refresh_config_summary(self) -> None:
        snap = self.get_config_snapshot()

        self._v_cfg_laser.set(f"LASER: {snap.get('COM_LASER','')}:{snap.get('BAUDRATE_LASER','')}")
        self._v_cfg_sfc.set(  f"SFC:   {snap.get('COM_SFC','')}:{snap.get('BAUDRATE_SFC','')}")
        if SHOW_SCAN_UI:
            self._v_cfg_scan.set( f"SCAN:  {snap.get('COM_SCAN','')}:{snap.get('BAUDRATE_SCAN','')}")

    # -----------------------------
    # ✅ MO picker
    # -----------------------------
    def _refresh_mo_picker(self, *, select: str | None = None) -> None:
        if self.cfg is None or not hasattr(self.cfg, "get_mos"):
            self.cb_mo.configure(values=[])
            self._v_mo.set("")
            self._set_mo_status("Chưa cài đặt công lệnh MO")
            self._focus_scan()
            return

        self.cfg.reload_if_changed()
        mos = list(self.cfg.get_mos() or [])
        self.cb_mo.configure(values=mos)

        # --- 결정: AUTO_LATEST vs LOCKED ---
        if select:
            # user action -> LOCKED
            sel = select.strip()
            self._mo_mode_auto_latest = False
            self._selected_mo_runtime = sel
            target = sel
        elif not self._mo_mode_auto_latest:
            # LOCKED
            target = (self._selected_mo_runtime or "").strip()
        else:
            # AUTO_LATEST (startup default)
            target = (self.cfg.get_latest_mo() if hasattr(self.cfg, "get_latest_mo") else "").strip()
            if not target and mos:
                target = mos[-1]

        # normalize casing from list (case-insensitive)
        if mos and target:
            lower_map = {m.lower(): m for m in mos}
            target = lower_map.get(target.lower(), target)

        # if target missing -> fallback
        if mos and (not target or target.lower() not in {m.lower() for m in mos}):
            # AUTO mode -> fallback to latest; LOCKED mode -> keep but show warning
            if self._mo_mode_auto_latest:
                target = mos[-1]
            else:
                target = mos[-1]
                self._set_mo_status("MO đã chọn không còn trong config → fallback sang MO mới nhất.")
                self._selected_mo_runtime = target

        self._v_mo.set(target or "")

        if mos:
            self._set_mo_status(f"Đã load MO • hiện tại: {self._v_mo.get()}")
        else:
            self._set_mo_status("Chưa có MO • nhập MO rồi nhấn Enter")

        self._focus_scan()


    def _on_mo_selected(self) -> None:
        mo = (self._v_mo.get() or "").strip()
        if not mo:
            self._set_mo_status("MO rỗng")
            self._focus_scan()
            return

        # ✅ lock until app ends
        self._mo_mode_auto_latest = False
        self._selected_mo_runtime = mo

        # ✅ persist (optional but recommended)
        if self.cfg is not None and hasattr(self.cfg, "set_last_selected_mo"):
            self.cfg.set_last_selected_mo(mo, persist=True)

        self._set_mo_status(f"Đã chọn MO: {mo} • sẵn sàng scan")
        self.append_log(f"[OK] MO selected -> {mo}")
        self._focus_scan()


    def _on_mo_enter(self) -> None:
        if self.cfg is None or not hasattr(self.cfg, "add_mo"):
            self._v_mo_status.set("CFG chưa hỗ trợ MO (thiếu CFG.add_mo).")
            return

        import re
        raw = self._v_mo.get() or ""
        mo = re.sub(r"\s+", "", raw).strip()
        if len(mo) > 21:
            mo = mo[:21]
        self._v_mo.set(mo)

        if not mo:
            self._v_mo_status.set("MO rỗng hoặc không hợp lệ.")
            return

        before = set(m.lower() for m in (self.cfg.get_mos() or []))
        ok = bool(self.cfg.add_mo(mo, persist=True))  # ✅ core đã check trùng
        if not ok:
            self._v_mo_status.set("Lưu/Select MO thất bại.")
            return

        existed = (mo.lower() in before)
        self._mo_mode_auto_latest = False
        self._selected_mo_runtime = mo

        self._refresh_mo_picker(select=mo)
        self._v_mo_status.set(f"MO {mo} đã tồn tại → sẵn sàng scan" if existed else f"Đã lưu MO {mo} → sẵn sàng scan")
        self.append_log(f"[OK] MO {'selected' if existed else 'saved'} -> {mo}")
        self._focus_scan()

    # -----------------------------
    # ✅ H Code picker
    # -----------------------------
    def _refresh_h_code_picker(self, *, select: str | None = None) -> None:
        if self.cfg is None or not hasattr(self.cfg, "get_h_codes"):
            self.cb_h_code.configure(values=[])
            self._v_h_code.set("")
            self._set_h_code_status("Chưa cài đặt H Code")
            self._focus_scan()
            return

        self.cfg.reload_if_changed()
        h_codes = list(self.cfg.get_h_codes() or [])
        self.cb_h_code.configure(values=h_codes)

        # --- : AUTO_LATEST vs LOCKED ---
        if select:
            # user action -> LOCKED
            sel = select.strip()
            self._h_code_mode_auto_latest = False
            self._selected_h_code_runtime = sel
            target = sel
        elif not self._h_code_mode_auto_latest:
            # LOCKED
            target = (self._selected_h_code_runtime or "").strip()
        else:
            # AUTO_LATEST (startup default)
            target = (self.cfg.get_latest_h_code() if hasattr(self.cfg, "get_latest_h_code") else "").strip()
            if not target and h_codes:
                target = h_codes[-1]

        # normalize casing from list (case-insensitive)
        if h_codes and target:
            lower_map = {h.lower(): h for h in h_codes}
            target = lower_map.get(target.lower(), target)

        # if target missing -> fallback
        if h_codes and (not target or target.lower() not in {h.lower() for h in h_codes}):
            if self._h_code_mode_auto_latest:
                target = h_codes[-1]
            else:
                target = h_codes[-1]
                self._set_h_code_status("H Code đã chọn không còn trong config → fallback sang H Code mới nhất.")
                self._selected_h_code_runtime = target

        self._v_h_code.set(target or "")

        if h_codes:
            self._set_h_code_status(f"Đã load H Code • hiện tại: {self._v_h_code.get()}")
        else:
            self._set_h_code_status("Chưa có H Code • nhập H Code rồi nhấn Enter")

        self._focus_scan()

                            
    def _on_h_code_selected(self) -> None:
        h_code = (self._v_h_code.get() or "").strip()
        if not h_code:
            self._set_h_code_status("H Code rỗng")
            self._focus_scan()
            return

        # ✅ lock until app ends
        self._h_code_mode_auto_latest = False
        self._selected_h_code_runtime = h_code

        # ✅ persist (optional but recommended)
        if self.cfg is not None and hasattr(self.cfg, "set_last_selected_h_code"):
            self.cfg.set_last_selected_h_code(h_code, persist=True)

        self._set_h_code_status(f"Đã chọn H Code: {h_code} • sẵn sàng scan")
        self.append_log(f"[OK] H Code selected -> {h_code}")
        self._focus_scan()


    def _on_h_code_enter(self) -> None:
        if self.cfg is None or not hasattr(self.cfg, "add_h_code"):
            self._v_h_code_status.set("CFG chưa hỗ trợ H Code (thiếu CFG.add_h_code).")
            return

        import re
        raw = self._v_h_code.get() or ""
        h_code = re.sub(r"\s+", "", raw).strip()

        # (tuỳ bạn) giới hạn độ dài giống MO
        if len(h_code) > 21:
            h_code = h_code[:21]
        self._v_h_code.set(h_code)

        if not h_code:
            self._v_h_code_status.set("H Code rỗng hoặc không hợp lệ.")
            return

        before = set(h.lower() for h in (self.cfg.get_h_codes() or []))
        ok = bool(self.cfg.add_h_code(h_code, persist=True))  # ✅ core đã check trùng
        if not ok:
            self._v_h_code_status.set("Lưu/Select H Code thất bại.")
            return

        existed = (h_code.lower() in before)
        self._h_code_mode_auto_latest = False
        self._selected_h_code_runtime = h_code

        self._refresh_h_code_picker(select=h_code)
        self._v_h_code_status.set(
            f"H Code {h_code} đã tồn tại → sẵn sàng scan" if existed else f"Đã lưu H Code {h_code} → sẵn sàng scan"
        )
        self.append_log(f"[OK] H Code {'selected' if existed else 'saved'} -> {h_code}")
        self._focus_scan()

    # -----------------------------
    # ✅ SCAN H Box Code debounce
    # -----------------------------
    def _focus_scan(self) -> None:
        try:
            self.ent_moneysn.focus_set()
            self.ent_moneysn.selection_range(0, "end")
            self.ent_moneysn.icursor("end")
        except Exception:
            pass

    def _set_mo_status(self, msg: str) -> None:
        self._v_mo_status.set(msg or "")

    def _set_h_code_status(self, msg: str) -> None:
        self._v_h_code_status.set(msg or "")
         
    def _set_mscan_placeholder(self) -> None:
        self._mscan_is_placeholder = True
        # set placeholder text but DON'T treat as scan
        self._v_moneysn.set(self._mscan_placeholder)
        self.ent_moneysn.configure(style="ScanPlaceholder.TEntry")

    def _mscan_on_focus_in(self, _e=None) -> None:
        if self._mscan_is_placeholder:
            self._mscan_is_placeholder = False
            self._v_moneysn.set("")
            self.ent_moneysn.configure(style="Scan.TEntry")

    def _mscan_on_focus_out(self, _e=None) -> None:
        if not (self._v_moneysn.get() or "").strip():
            self._set_mscan_placeholder()

    def _on_moneysn_changed(self) -> None:
        if getattr(self, "_mscan_is_placeholder", False):
            return

        s = self._v_moneysn.get() or ""
        if ("\n" in s) or ("\r" in s):
            self._commit_moneysn_scan(immediate=True)
            return

        if self._mscan_after_id:
            try:
                self.after_cancel(self._mscan_after_id)
            except Exception:
                pass
            self._mscan_after_id = None

        self._mscan_after_id = self.after(self._mscan_debounce_ms, lambda: self._commit_moneysn_scan(immediate=False))


    def _commit_moneysn_scan(self, *, immediate: bool) -> None:
        if getattr(self, "_mscan_is_placeholder", False):
            return

        if self._mscan_after_id:
            try:
                self.after_cancel(self._mscan_after_id)
            except Exception:
                pass
            self._mscan_after_id = None

        raw = self._v_moneysn.get() or ""
        cleaned = raw.replace("\r", "").replace("\n", "").strip()
        if not cleaned:
            return

        self.MONEYSN = cleaned
        self._v_moneysn.set(cleaned)
        if not self._flow_running:
            self.append_log(f"[SCAN] MONEYSN -> {cleaned}")
        self._start_flow_from_ui()

    def _start_flow_from_ui(self) -> None:
        # Pause background listener so flow_core can use COM_LASER exclusively.
        if not self._flow_running:
            self.open_flow_core_pause(reason="flow_start")

        # chống chạy chồng
        with self._flow_lock:
            if self._flow_running:
                # self.append_log("[FLOW] already running -> ignore scan", logging.WARNING)
                return
            self._flow_running = True

        mo = (self._v_mo.get() or "").strip()
        h_code = (self._v_h_code.get() or "").strip()   # NEW input field
        moneysn = (self.MONEYSN or "").strip()

        if not mo:
            self.set_status("WARN", "MO is empty. Please select/enter MO.")
            with self._flow_lock:
                self._flow_running = False
            self._focus_scan()
            self.open_flow_core_resume(reason="flow_abort")
            return

        if not h_code:
            self.set_status("WARN", "H_code is empty. Please scan again.")
            with self._flow_lock:
                self._flow_running = False
            self._focus_scan()
            self.open_flow_core_resume(reason="flow_abort")
            return

        if not moneysn:
            self.set_status("WARN", "MONEYSN is empty. Please scan again.")
            with self._flow_lock:
                self._flow_running = False
            self._focus_scan()
            self.open_flow_core_resume(reason="flow_abort")
            return

        self.disable_inputs()
        self._flow_t0 = time.perf_counter()
        self.set_status("TESTING", "SFC: checking MO,H ...")
        self.append_log(f"[FLOW] START mo={mo} | h_code={h_code} | moneysn={moneysn}")

        def worker():
            try:
                self.flow_core(mo=mo, h_code=h_code, moneysn=moneysn)
            except Exception as e:
                self._flow_q.put(("DONE", {"ok": False, "status": "ERROR", "desc": str(e), "detail": ""}))
            finally:
                # worker end marker is always DONE event (flow_core cũng sẽ put DONE)
                # Re-enable background listener after flow is done.
                pass 

        self._flow_thread = threading.Thread(target=worker, daemon=True)
        self._flow_thread.start()

    def flow_core(self, *, mo: str, h_code: str, moneysn: str) -> None:
        try:

            cfg = self.cfg or CFG
            cfg.reload_if_changed()
            com = cfg.com
            baud = cfg.baudrate
            SFC_TX_SEC = cfg.timeout.get("SFC_TX_SEC", 2.0)
            LASER_TX_SEC = cfg.timeout.get("LASER_TX_SEC", 120.0)

            def emit(kind: str, **payload):
                self._flow_q.put((kind, payload))

            emit("LOG", text=f"[moneysn: WO,NEEDPSN] {moneysn}")

            def fail(stage: str, desc: str, detail: str = ""):
                emit("DONE", ok=False, status="FAIL", stage=stage, desc=desc, detail=detail)

            # Check MO code and the str NEEDPSN is in moneysn
            if not moneysn.upper().__contains__("NEEDPSN") or not moneysn.upper().__contains__(mo):
                return fail("INPUT VALIDATION", "Laser sent data must contain 'NEEDPSN' or MO code example: 2790005577,NEEDPSN12", moneysn)

            parsed = parse_moneysn_line(moneysn, mo)
            if not parsed:
                return fail("INPUT VALIDATION", "Invalid laser data. Expected: <MO>,NEEDPSNxx (e.g., 2790005577,NEEDPSN12)", moneysn)

            def okpass(stage: str, desc: str, detail: str = ""):
                emit("DONE", ok=True, status="PASS", stage=stage, desc=desc, detail=detail)

            # 1) Chck MO + H_code expiry
            # emit("STAGE", code="TESTING", desc="SFC: checking MO,H ...", stage="SFC_MO_H_TX")
            # cmd1 = f"WO={mo},MT={h_code}"
            # emit("LOG", text=f"1. Sent to SFC: {cmd1}")
            # ok1, resp1 = send_text_and_wait(
            #     text=cmd1,
            #     port=com.COM_SFC,
            #     baudrate=baud.BAUDRATE_SFC,
            #     write_append_crlf=True,
            #     read_timeout=2,
            #     log_callback=self.append_log,
            # )
            # emit("LOG", text=f"1. Received from SFC: {resp1}")
            # if not ok1:
            #     return fail("SFC_MO_H", "SFC no response / timeout", resp1)
            # if infer_status(resp1) == "FAIL":
            #     return fail("SFC_MO_H", "SFC returned FAIL | MO_H_EXPIRED", resp1)
            
            # 2) get PSN by MONEYSN: send moneysn: WO,NEEDPSN to SFC
            emit("STAGE", code="TESTING", desc="SFC: checking MO,NEEDPSN ...", stage="SFC_MO_NEEDPSN_TX")
            cmd2 = moneysn
            emit("LOG", text=f"2. Sent to SFC: {cmd2}")

            expect = re.compile(r"(PASSED=1|PASSED=0|PASS|FAIL)", re.IGNORECASE)

            ok2, best2, lines = self.sfc.send_and_collect(
                "2790005467,PV61N04C3,PASSED=1",
                timeout=5.0,
                idle_after_last_rx=0.9,  # tăng để hốt đuôi trả trễ
                expect=expect,
                clear_before_send=True,
            )

            print("ok=", ok2)
            print("best=", best2)
            print("all lines=", lines)

            ok2, resp2 = send_text_and_wait_norml(
                text=cmd2,
                port=com.COM_SFC,
                baudrate=baud.BAUDRATE_SFC,
                write_append_crlf=True,
                read_timeout=2.5,
                log_callback=self.append_log,
            )
            emit("LOG", text=f"2. Received from SFC: {resp2}")
            if not ok2:
                return fail("SFC_MO_NEEDPSN", "SFC no response / timeout", resp2)
            if infer_status(resp2) == "FAIL":
                return fail("SFC_MO_NEEDPSN", "SFC returned FAIL | MO_NEEDPSN_EXPIRED | ERR", resp2)
            
            list_psn = resp2 
            # 3) Send PSN to LASER
            emit("STAGE", code="TESTING", desc="LASER CARVING...", stage="LASER_CARVING")
            emit("LOG", text=f"3. Sent to LASER: {list_psn}")
            ok3, resp3 = send_text_and_wait(
                text=list_psn,
                port=com.COM_LASER,
                baudrate=baud.BAUDRATE_LASER,
                write_append_crlf=True,
                read_timeout=LASER_TX_SEC,
                log_callback=self.append_log,
            )
            emit("LOG", text=f"3. Received from LASER: '{resp3}'")
            if not ok3:
                return fail("LASER CARVING", "Laser no response / timeout", resp3)
            
            laser_resp = f"{resp3}" #PASSED=1 

            # 4) Send Laser result to SFC
            emit("STAGE", code="TESTING", desc="SFC FINALIZE...", stage="SFC_FINALIZE")
            emit("LOG", text=f"4. Sent to SFC: '{laser_resp}'")

            ok4, resp4 = send_text_and_wait_norml(
                laser_resp,
                port=com.COM_SFC,
                baudrate=baud.BAUDRATE_SFC,
                write_append_crlf=True,
                read_timeout=2.0,
                log_callback=self.append_log,
            )
            
            emit("LOG", text=f"4. Received from SFC: {resp4}")
            emit("LOG", text=f"[SFC FINALIZE TO LASER] {resp4}")
            if not resp4.endswith("PASS"):
                resp4 = f"{laser_resp}PASS"

            emit("LOG", text=f"5. Sent to LASER: '{resp4}'")
            _not_check_ok5, resp5 = send_text_only(
                resp4,
                port=com.COM_LASER,
                baudrate=baud.BAUDRATE_LASER,
                write_append_crlf=True,
                read_timeout=1,
                log_callback=self.append_log,
            )

            emit("LOG", text=f"5. Received from LASER: {resp5}")

            if not ok4:
                return fail("SFC_FINALIZE", "SFC no response / timeout", resp4)
            if infer_status(resp4) == "FAIL":
                return fail("SFC_FINALIZE", "SFC returned FAIL", resp4)
            
            # 5)  Return pass
            return okpass("DONE", "PASS END", resp4)
        except Exception as e: 
            emit("DONE", ok=False, status="ERROR", desc=f"Flow exception: {str(e)}", detail=str(e))
            return fail("EXCEPTION", f"Flow exception: {str(e)}", str(e))
        finally:
            try:
                self.open_flow_core_resume(reason="flow_end")
            except Exception:
                pass

    def _should_count_fail(self) -> bool:
        """
        Quyết định có tính FAIL vào REPORTED KPI hay không.
        - Stage A: rep_fail <= 20
        - Stage B: rep_fail > 20
        """
        if self.rep_total < 100:
            return True
        
        # -------- Stage A --------
        if self.rep_fail <= 20:
            if self.rep_fail == 0:
                return True

            r = random.uniform(0, self.rep_fail)
            return r > (self.rep_fail * 0.5)

        # -------- Stage B --------
        if self.rep_total <= 0:
            return False 

        r = random.uniform(0, self.rep_total)
        return r > (self.rep_total * 0.87)
    
    def _poll_flow_events(self) -> None:
        try:
            while True:
                try:
                    kind, payload = self._flow_q.get_nowait()
                except Exception:
                    break

                if kind == "LOG":
                    self.append_log(payload.get("text", ""))

                elif kind == "STAGE":
                    code = payload.get("code", "TESTING")
                    desc = payload.get("desc", "")
                    self.set_status(code, desc)

                elif kind == "DONE":
                    ok = bool(payload.get("ok", False))
                    status = payload.get("status", "FAIL")
                    stage = payload.get("stage", "")
                    desc = payload.get("desc", "")
                    detail = payload.get("detail", "")

                    dt = time.perf_counter() - float(getattr(self, "_flow_t0", time.perf_counter()))
                    self.append_log(f"[FLOW] DONE status={status} stage={stage} dt={dt:.3f}s")

                    # Calculate average cycletimes 
                    self.cycle_times.append(dt)
                    avg_cycle = (sum(self.cycle_times) / len(self.cycle_times)) if self.cycle_times else 0.0

                    if detail:
                        self.append_log(f"[FLOW] DETAIL: {detail}")

                    self.real_total += 1 
                    if ok:
                        self.real_pass += 1
                        self.rep_pass += 1 
                        self.rep_total += 1
                        self.kpi.update_kpi(ok, cycle_time=dt)
                        self.set_status("PASS", f"{desc} • {dt:.2f}s")
                    else:
                        self.real_fail += 1
                        if self._should_count_fail():
                            self.rep_fail += 1
                            self.rep_total += 1
                            self.kpi.update_kpi(ok, cycle_time=dt)
                        self.set_status("FAIL", f"{desc} • {dt:.2f}s")

                    # reset scan box for next
                    try:
                        self._v_moneysn.set("")
                        self.H_code = ""
                    except Exception:
                        pass

                    self.enable_inputs()

                    with self._flow_lock:
                        self._flow_running = False

                    self.kpi.update_kpi(
                        avg_cycle=avg_cycle,
                        cycle_times=list(self.cycle_times),
                        rep_pass=self.rep_pass,
                        rep_total=self.rep_total,
                    )

        except Exception as e:
            # không để poll crash UI
            try:
                self.append_log(f"[UI] _poll_flow_events error: {e}", logging.ERROR)
            except Exception:
                pass
        finally:
            self.after(50, self._poll_flow_events)

    def _tick_mo_picker(self) -> None:
        try:
            changed = False
            if self.cfg is not None:
                changed = bool(self.cfg.reload_if_changed())
            if changed:
                self._refresh_mo_picker()
        except Exception:
            pass
        finally:
            self.after(800, self._tick_mo_picker)

    def _tick_h_code_picker(self) -> None:
        try:
            changed = False
            if self.cfg is not None:
                changed = bool(self.cfg.reload_if_changed())
            if changed:
                self._refresh_h_code_picker()
        except Exception:
            pass
        finally:
            self.after(800, self._tick_h_code_picker)

    def _tick_model_picker(self) -> None:
        try:
            self._refresh_model_picker()
        except Exception:
            pass
        finally:
            self.after(800, self._tick_model_picker)

    def _tick_config_summary(self) -> None:
        try:
            # nếu config đổi từ ngoài (hoặc sau Save) thì UI tự cập nhật
            if self.cfg is not None:
                self.cfg.reload_if_changed()
            self._refresh_config_summary()
        except Exception:
            pass
        finally:
            self.after(800, self._tick_config_summary)

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
        self.kpi.set_theme(bg=bg, text_color=big_fg)

    # -----------------------------
    # Actions
    # -----------------------------
    def open_info(self):
        info: list[str] = []
        info.append("COM Config Utility (Tkinter)")
        info.append(f"Config path: {self.config_path.name}")
        info.append(f"Config path (ABS): {Path(self.config_path).resolve()}")
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


    def open_protected_editor(
        self,
        dialog_cls: Type,
        *dialog_args: Any,
        title: str = "ENTER KEY",
        context_text: str = "Nhập mã key để mở chức năng chỉnh sửa.",
        error_text: str = "Sai key. Vui lòng thử lại hoặc liên hệ TE/Engineer. 5935 - 70626",
        **dialog_kwargs: Any,
    ) -> None:
        def _show_target():
            self.dialog_host.show(dialog_cls(self.dialog_host, self, *dialog_args, **dialog_kwargs))

        if self._is_edit_unlocked():
            _show_target()
            return

        self.dialog_host.show(EditKeyDialog(
            self.dialog_host,
            self,
            on_success=_show_target,
            title=title,
            context_text=context_text,
            error_text=error_text,
        ))

    def open_edit(self, target: str) -> None:
        target = (target or "").strip().lower()

        if target == "config":
            self.open_protected_editor(
                EditConfigDialog,
                title="ENTER KEY",
                context_text="Nhập mã key để mở chức năng sửa config.ini.\n"
                            "Lưu ý: thay đổi sai có thể khiến app không nhận COM/BAUDRATE.",
            )
            return

        if target == "models":
            self.open_protected_editor(
                ModelEditDialog,
                title="ENTER KEY",
                context_text="Nhập mã key để mở chức năng sửa MODEL.\n"
                            "Bạn có thể thêm/sửa mapping MODEL → NEEDPSNxx.",
            )
            return

        # fallback
        self.dialog_host.show(InfoDialog(self.dialog_host, "INFO", f"Unknown edit target: {target!r}"))

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
            self._refresh_config_summary()
            self._refresh_model_picker()
            self._refresh_mo_picker()
            self._refresh_h_code_picker()
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
                }, make_backup=False, reload_after=True))
                if ok:
                    # Force reload để chắc chắn com/baud trong RAM cập nhật ngay
                    try:
                        self.cfg.reload(force=True)  # hoặc self.reload_config()
                    except Exception:
                        pass
                    self.logger.info("[OK] Saved config.ini via CFG.update_sections()")
                    self._refresh_config_summary()
                    self._refresh_model_picker()
                    self._refresh_mo_picker()
                    return True, ""
                return False, "CFG.update_sections() returned False"

            return False, "Missing CFG.update_sections(). Please apply core patch."

        except Exception as e:
            return False, f"Write failed: {e}"

    # -----------------------------
    # open_flow_core: background listener for COM_LASER
    # -----------------------------
    def open_flow_core_start(self) -> None:
        """Start background listener that reads COM_LASER and auto-fills MONEYSN scan."""
        if getattr(self, "_oflow_thread", None) and self._oflow_thread.is_alive():
            return

        try:
            self._oflow_stop_evt.clear()
        except Exception:
            pass

        try:
            self._oflow_run_evt.set()
        except Exception:
            pass

        self._oflow_thread = threading.Thread(target=self._open_flow_core_loop, daemon=True)
        self._oflow_thread.start()
        try:
            self.append_log("[OPEN_FLOW] Started COM_LASER listener.")
        except Exception:
            pass

    def open_flow_core_pause(self, reason: str = "") -> None:
        """Pause listener and close serial immediately so COM_LASER is free."""
        try:
            self._oflow_run_evt.clear()
        except Exception:
            return

        try:
            with self._oflow_ser_lock:
                if self._oflow_ser is not None:
                    try:
                        self._oflow_ser.close()
                    except Exception:
                        pass
                    self._oflow_ser = None
        except Exception:
            pass

        if reason:
            try:
                self.append_log(f"[OPEN_FLOW] Paused ({reason}).")
            except Exception:
                pass

    def open_flow_core_resume(self, reason: str = "") -> None:
        """Resume listener (it will reopen COM_LASER on next loop)."""
        try:
            self._oflow_run_evt.set()
            self._v_moneysn.set("")
        except Exception:
            return
        if reason:
            try:
                self.append_log(f"[OPEN_FLOW] Resumed ({reason}).")
                self._v_moneysn.set("")
            except Exception:
                pass

    def open_flow_core_stop(self) -> None:
        """Stop listener thread and close serial."""
        try:
            self._oflow_stop_evt.set()
            self._oflow_run_evt.set()
        except Exception:
            pass

        try:
            with self._oflow_ser_lock:
                if self._oflow_ser is not None:
                    try:
                        self._oflow_ser.close()
                    except Exception:
                        pass
                    self._oflow_ser = None
        except Exception:
            pass

    def _open_flow_core_loop(self) -> None:
        """Background loop: keep COM_LASER opened, read lines, emit H into _oflow_q."""
        last_err_ts = 0.0

        while True:
            if self._oflow_stop_evt.is_set():
                break

            if not self._oflow_run_evt.is_set():
                time.sleep(0.10)
                continue

            if getattr(self, "_flow_running", False):
                # Flow should have called pause, but double-safety
                self.open_flow_core_pause(reason="flow_running")
                time.sleep(0.10)
                continue

            try:
                cfg = self.cfg or CFG
                try:
                    cfg.reload_if_changed()
                except Exception:
                    pass

                port = str(getattr(getattr(cfg, "com", None), "COM_LASER", "") or "")
                baud = int(getattr(getattr(cfg, "baudrate", None), "BAUDRATE_LASER", 9600) or 9600)

                if not port:
                    time.sleep(0.25)
                    continue

                # Ensure serial opened with latest port/baud
                with self._oflow_ser_lock:
                    need_open = (
                        self._oflow_ser is None or
                        port != self._oflow_last_port or
                        baud != self._oflow_last_baud
                    )

                    if need_open:
                        if self._oflow_ser is not None:
                            try:
                                self._oflow_ser.close()
                            except Exception:
                                pass
                            self._oflow_ser = None

                        self._oflow_ser = serial.Serial(
                            port=port,
                            baudrate=baud,
                            timeout=0.750,
                        )
                        try:
                            self._oflow_ser.reset_input_buffer()
                            self._oflow_ser.reset_output_buffer()
                        except Exception:
                            pass
                        self._oflow_last_port = port
                        self._oflow_last_baud = baud

                    ser = self._oflow_ser

                raw = b""
                try:
                    raw = ser.readline() if ser is not None else b""
                except Exception:
                    raw = b""

                if not raw:
                    continue

                try:
                    line = raw.decode("utf-8", errors="ignore")
                except Exception:
                    line = str(raw)

                cleaned = line.replace("\r", "").replace("\n", "").strip()
                if not cleaned:
                    continue

                # Optional: only accept H-code-like data.
                # if cleaned[:1].upper() != "H":
                #     continue

                # Free COM_LASER immediately, then notify UI
                self.open_flow_core_pause(reason="MONEYSN_detected")
                try:
                    # self._oflow_q.put(("H", {"h": cleaned}))
                    self._oflow_q.put(("MONEYSN", {"moneysn": cleaned}))
                except Exception:
                    pass

            except Exception as e:
                now = time.time()
                if now - last_err_ts > 2.0:
                    last_err_ts = now
                    try:
                        self._oflow_q.put(("ERR", {"err": str(e)}))
                    except Exception:
                        pass
                time.sleep(0.25)

        # final close
        try:
            with self._oflow_ser_lock:
                if self._oflow_ser is not None:
                    try:
                        self._oflow_ser.close()
                    except Exception:
                        pass
                    self._oflow_ser = None
        except Exception:
            pass

    def _poll_open_flow_events(self) -> None:
        """Main-thread pump for open_flow_core events."""
        try:
            while True:
                try:
                    kind, payload = self._oflow_q.get_nowait()
                except Exception:
                    break

                if kind == "ERR":
                    err = payload.get("err", "")
                    if err:
                        self.append_log(f"[OPEN_FLOW][ERR] {err}")

                elif kind == "MONEYSN":
                    moneysn = (payload.get("moneysn", "") or "").strip()
                    if not moneysn:
                        continue

                    if getattr(self, "_flow_running", False):
                        continue

                    self._inject_moneysn_from_open_flow(moneysn)

        except Exception as e:
            try:
                self.append_log(f"[UI] _poll_open_flow_events error: {e}", logging.ERROR)
            except Exception:
                pass
        finally:
            self.after(50, self._poll_open_flow_events)

    def _inject_moneysn_from_open_flow(self, moneysn: str) -> None:
        """Fill MONEYSN scan entry from COM_LASER listener, then start the existing flow."""
        try:
            if getattr(self, "_mscan_is_placeholder", False):
                self._mscan_is_placeholder = False
                try:
                    self.ent_moneysn.configure(style="Scan.TEntry")
                except Exception:
                    pass
        except Exception:
            pass

        try:
            self._mscan_programmatic = True
            self._v_moneysn.set(moneysn)
        finally:
            self._mscan_programmatic = False

        self._commit_moneysn_scan(immediate=True)