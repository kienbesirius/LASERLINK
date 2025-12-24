# src.gui.gui.py
from __future__ import annotations

import os
import sys
import time
import random
import configparser
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from pathlib import Path
src = Path(__file__).resolve()

while not src.name.endswith("src") and not src.name.startswith("src"):
    src = src.parent

sys.path.insert(0, src)

# Optional dependencies (nice-to-have)
try:
    from PIL import Image, ImageTk  # type: ignore
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

try:
    import serial.tools.list_ports  # type: ignore
    _HAS_SERIAL = True
except Exception:
    _HAS_SERIAL = False


# -----------------------------
# Theme constants (Light, "uy tín")
# -----------------------------
BG = "#ECEEF2"          # outer gray
CARD_BG = "#FFFFFF"     # card
BORDER = "#D6DAE3"
TEXT = "#111827"
MUTED = "#6B7280"

OK_BG = "#E7F7EE"
OK_FG = "#0F5132"

ERR_BG = "#FCE8E8"
ERR_FG = "#842029"

WARN_BG = "#FFF4E5"
WARN_FG = "#7A4B00"


# -----------------------------
# Config schema (validate keys/sections)
# (đúng hướng bạn nói: section chỉ được chứa param hợp lệ)
# -----------------------------
SCHEMA = {
    "COM": {"COM_LASER", "COM_SFC", "COM_SCAN"},
    "BAUDRATE": {"BAUDRATE_LASER", "BAUDRATE_SFC", "BAUDRATE_SCAN"},
    "SERIAL_READLINE_BREAK": {"TOKENS", "ALWAYS_LAST"},
}


def sanitize_and_validate_ini(text: str) -> tuple[configparser.ConfigParser | None, list[str]]:
    """
    Parse INI text, validate:
    - Only allow known sections in SCHEMA
    - Each section only allows known keys
    Return (config, errors).
    """
    errors: list[str] = []
    cp = configparser.ConfigParser()
    try:
        cp.read_string(text)
    except Exception as e:
        return None, [f"INI parse error: {e}"]

    for sec in cp.sections():
        if sec not in SCHEMA:
            errors.append(f"Section không hợp lệ: [{sec}] (allowed: {', '.join(SCHEMA.keys())})")
            continue
        allowed = SCHEMA[sec]
        for k in cp[sec].keys():
            # configparser lowercases keys by default when iterating; use original via cp[sec]._options? không ổn.
            # Nên check theo upper.
            if k.upper() not in allowed:
                errors.append(f"Key không hợp lệ trong [{sec}]: {k} (allowed: {', '.join(sorted(allowed))})")

    return cp, errors


def read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text_file_atomic(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    # backup
    if os.path.exists(path):
        bk = path + f".bak_{time.strftime('%Y%m%d_%H%M%S')}"
        try:
            os.replace(path, bk)
        except Exception:
            pass
    os.replace(tmp, path)


def list_ports() -> list[str]:
    if not _HAS_SERIAL:
        return []
    out = []
    for p in serial.tools.list_ports.comports():
        out.append(p.device)
    return out


# -----------------------------
# Background grain (moving) on Canvas behind cards
# -----------------------------
class GrainBackground:
    def __init__(self, parent: tk.Widget):
        self.canvas = tk.Canvas(parent, highlightthickness=0, bd=0, bg=BG)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)

        self._enabled = True
        self._last_w = 0
        self._last_h = 0
        self._img_id = None
        self._tkimg = None  # keep ref
        self._tick_ms = 140  # nhẹ, không mỏi mắt

        parent.bind("<Configure>", self._on_resize)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self.canvas.delete("grain")
            self._tkimg = None

    def _on_resize(self, _e=None):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 2 or h <= 2:
            return
        if (w, h) != (self._last_w, self._last_h):
            self._last_w, self._last_h = w, h
            self._render_once()
        self._schedule()

    def _schedule(self):
        self.canvas.after_cancel(getattr(self, "_after_id", ""))
        self._after_id = self.canvas.after(self._tick_ms, self._render_once)

    def _render_once(self):
        if not self._enabled:
            return
        w, h = self._last_w, self._last_h
        if w <= 2 or h <= 2:
            return

        # Best: PIL noise with alpha (mờ)
        if _HAS_PIL:
            # generate small tile then upscale slightly to reduce cost
            tile = 160
            img = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
            px = img.load()
            for y in range(tile):
                for x in range(tile):
                    # grain density low
                    if random.random() < 0.12:
                        a = random.randint(10, 22)  # low alpha
                        v = random.randint(60, 140)
                        px[x, y] = (v, v, v, a)

            # tile fill
            full = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            for yy in range(0, h, tile):
                for xx in range(0, w, tile):
                    full.alpha_composite(img, (xx, yy))

            self._tkimg = ImageTk.PhotoImage(full)
            self.canvas.delete("grain")
            self._img_id = self.canvas.create_image(0, 0, anchor="nw", image=self._tkimg, tags=("grain",))
        else:
            # Fallback: draw some stipple rectangles
            self.canvas.delete("grain")
            for _ in range(140):
                x = random.randint(0, max(1, w - 1))
                y = random.randint(0, max(1, h - 1))
                s = random.randint(1, 2)
                self.canvas.create_rectangle(x, y, x + s, y + s, outline="", fill="#BFC6D4", tags=("grain",))


# -----------------------------
# In-app DialogHost (nested overlay)
# -----------------------------
class DialogHost(ttk.Frame):
    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.place(x=0, y=0, relwidth=1, relheight=1)
        self.lower()  # keep behind by default? we'll lift on show
        self.place_forget()

        self.stack: list[ttk.Frame] = []

        # dim background (still inside same window)
        self.dim = tk.Canvas(self, highlightthickness=0, bd=0, bg="#000000")
        self.dim.place(x=0, y=0, relwidth=1, relheight=1)
        # Tk can't do alpha on frame reliably; fake dim by drawing a rectangle w/ stipple
        self.dim.create_rectangle(0, 0, 9999, 9999, fill="#000000", stipple="gray50", outline="")

        self.bind_all("<Escape>", self._on_escape, add=True)

    def show(self, dialog: ttk.Frame) -> None:
        if not self.winfo_ismapped():
            self.place(x=0, y=0, relwidth=1, relheight=1)
            self.lift()

        # hide previous top (but keep in stack)
        if self.stack:
            self.stack[-1].place_forget()

        self.stack.append(dialog)
        dialog.place(relx=0.5, rely=0.5, anchor="center")
        dialog.lift()

        # trap clicks: overlay eats events
        self.dim.lift()

        # ensure dialog on top of dim
        dialog.lift()

    def close_top(self) -> None:
        if not self.stack:
            return
        top = self.stack.pop()
        top.destroy()

        if self.stack:
            self.stack[-1].place(relx=0.5, rely=0.5, anchor="center")
            self.stack[-1].lift()
        else:
            self.place_forget()

    def _on_escape(self, _e=None):
        if self.stack:
            self.close_top()


class BaseDialog(ttk.Frame):
    def __init__(self, host: DialogHost, title: str, width: int = 560):
        super().__init__(host)
        self.host = host
        self["padding"] = 16

        self.configure(style="Card.TFrame")
        self._w = width

        # fixed size dialog feel
        self.update_idletasks()
        self.place_configure(width=self._w)

        header = ttk.Frame(self, style="Card.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text=title, style="DialogTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="✕", command=self.host.close_top, width=3).grid(row=0, column=1, sticky="e")

        self.body = ttk.Frame(self, style="Card.TFrame")
        self.body.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.columnconfigure(0, weight=1)

        self.footer = ttk.Frame(self, style="Card.TFrame")
        self.footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        self.footer.columnconfigure(0, weight=1)


class InfoDialog(BaseDialog):
    def __init__(self, host: DialogHost, info_text: str):
        super().__init__(host, "INFO", width=620)
        txt = ScrolledText(self.body, height=14, wrap="word")
        txt.insert("1.0", info_text)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)

        ttk.Button(self.footer, text="OK", command=self.host.close_top, width=10).grid(row=0, column=0, sticky="e")


class ErrorDialog(BaseDialog):
    def __init__(self, host: DialogHost, title: str, message: str):
        super().__init__(host, title, width=620)
        lbl = ttk.Label(self.body, text=message, style="Error.TLabel", wraplength=580, justify="left")
        lbl.pack(fill="x", expand=False)

        ttk.Button(self.footer, text="Đóng", command=self.host.close_top, width=10).grid(row=0, column=0, sticky="e")


class EditConfigDialog(BaseDialog):
    def __init__(self, host: DialogHost, app: "ComConfigApp"):
        super().__init__(host, "EDIT CONFIG.INI", width=720)
        self.app = app

        ports = [""] + list_ports()
        # Vars
        self.v_com_laser = tk.StringVar(value=app.cfg_values.get("COM_LASER", ""))
        self.v_com_sfc = tk.StringVar(value=app.cfg_values.get("COM_SFC", ""))
        self.v_com_scan = tk.StringVar(value=app.cfg_values.get("COM_SCAN", ""))

        self.v_baud_laser = tk.StringVar(value=app.cfg_values.get("BAUDRATE_LASER", "9600"))
        self.v_baud_sfc = tk.StringVar(value=app.cfg_values.get("BAUDRATE_SFC", "9600"))
        self.v_baud_scan = tk.StringVar(value=app.cfg_values.get("BAUDRATE_SCAN", "9600"))

        grid = ttk.Frame(self.body, style="Card.TFrame")
        grid.pack(fill="both", expand=True)
        for c in range(2):
            grid.columnconfigure(c, weight=1)

        def row(r: int, label: str, var: tk.StringVar, choices: list[str] | None = None):
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="w", pady=6, padx=(0, 10))
            if choices is not None:
                cb = ttk.Combobox(grid, textvariable=var, values=choices, state="readonly")
                cb.grid(row=r, column=1, sticky="ew", pady=6)
            else:
                ent = ttk.Entry(grid, textvariable=var)
                ent.grid(row=r, column=1, sticky="ew", pady=6)

        row(0, "COM_LASER", self.v_com_laser, ports if ports else None)
        row(1, "COM_SFC", self.v_com_sfc, ports if ports else None)
        row(2, "COM_SCAN", self.v_com_scan, ports if ports else None)

        ttk.Separator(grid).grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)

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
        left = ttk.Frame(self.footer, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="w")
        ttk.Button(left, text="Scan Ports", command=self._scan_ports).pack(side="left")

        right = ttk.Frame(self.footer, style="Card.TFrame")
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
        self.app.cfg_values["COM_LASER"] = self.v_com_laser.get().strip()
        self.app.cfg_values["COM_SFC"] = self.v_com_sfc.get().strip()
        self.app.cfg_values["COM_SCAN"] = self.v_com_scan.get().strip()
        self.app.cfg_values["BAUDRATE_LASER"] = self.v_baud_laser.get().strip()
        self.app.cfg_values["BAUDRATE_SFC"] = self.v_baud_sfc.get().strip()
        self.app.cfg_values["BAUDRATE_SCAN"] = self.v_baud_scan.get().strip()

        ok, msg = self.app.save_config_values()
        if ok:
            self.host.close_top()
            self.app.set_status("OK", f"Saved: {os.path.basename(self.app.config_path)}")
        else:
            self.host.show(ErrorDialog(self.host, "SAVE FAILED", msg))


# -----------------------------
# Main App
# -----------------------------
class ComConfigApp(tk.Tk):
    def __init__(self, config_path: str = "config.ini"):
        super().__init__()
        self.title("COM Config Utility")
        self.configure(bg=BG)

        # BookyApp-ish min/max sizing (bạn chỉnh số này theo gui.py gốc)
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
        style.configure("TLabel", background=CARD_BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD_BG, foreground=MUTED)
        style.configure("DialogTitle.TLabel", background=CARD_BG, foreground=TEXT, font=("TkDefaultFont", 12, "bold"))
        style.configure("Error.TLabel", background=CARD_BG, foreground=ERR_FG)

        # Background grain behind everything
        self.grain = GrainBackground(self)

        # Main content container (cards sit above grain)
        self.container = ttk.Frame(self, padding=18, style="TFrame")
        self.container.place(x=0, y=0, relwidth=1, relheight=1)

        self.container.columnconfigure(0, weight=1)
        self.container.rowconfigure(1, weight=1)

        # Header: Status card
        self.status_card = ttk.Frame(self.container, style="Card.TFrame", padding=16)
        self.status_card.grid(row=0, column=0, sticky="ew")
        self.status_card.columnconfigure(0, weight=1)

        self.status_title = ttk.Label(self.status_card, text="STATUS", style="Muted.TLabel")
        self.status_title.grid(row=0, column=0, sticky="w")

        self.status_big = ttk.Label(self.status_card, text="IDLE", font=("TkDefaultFont", 26, "bold"))
        self.status_big.grid(row=1, column=0, sticky="w", pady=(6, 0))

        self.status_desc = ttk.Label(self.status_card, text="Ready.", style="Muted.TLabel")
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

        # Config state
        self.config_path = config_path
        self.cfg_values: dict[str, str] = {}

        # Build left panel widgets
        ttk.Label(self.left, text="CONFIG", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(self.left, text=os.path.abspath(self.config_path), style="Muted.TLabel", wraplength=330).pack(anchor="w", pady=(4, 12))

        btns = ttk.Frame(self.left, style="Card.TFrame")
        btns.pack(fill="x")
        ttk.Button(btns, text="Reload", command=self.reload_config).pack(fill="x")
        ttk.Button(btns, text="Edit COM/BAUDRATE", command=self.open_edit_config).pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Info", command=self.open_info).pack(fill="x", pady=(8, 0))

        ttk.Separator(self.left).pack(fill="x", pady=14)

        self.chk_grain = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.left, text="Enable moving grain", variable=self.chk_grain, command=self._toggle_grain).pack(anchor="w")

        # Right panel: log
        ttk.Label(self.right, text="LOG", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.log = ScrolledText(self.right, height=14, wrap="word")
        self.log.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.log.configure(state="disabled")

        # Footer content
        ttk.Label(self.footer, text="Tip: dùng Edit để sửa config.ini cho đúng COM/BAUDRATE trên máy.", foreground=MUTED, background=BG)\
            .grid(row=0, column=0, sticky="w")

        self.reload_config()
        self.set_status("IDLE", "Ready.")

    def _toggle_grain(self):
        self.grain.set_enabled(self.chk_grain.get())

    def append_log(self, s: str):
        self.log.configure(state="normal")
        self.log.insert("end", s.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, code: str, desc: str = ""):
        # simple status styling
        code_u = (code or "").upper()
        self.status_big.configure(text=code_u)
        self.status_desc.configure(text=desc or "")

        # Change card background subtly (optional)
        if code_u in ("OK", "PASS", "READY"):
            self._set_status_card_colors(OK_BG, OK_FG)
        elif code_u in ("ERROR", "FAIL"):
            self._set_status_card_colors(ERR_BG, ERR_FG)
        elif code_u in ("WARN", "WARNING"):
            self._set_status_card_colors(WARN_BG, WARN_FG)
        else:
            self._set_status_card_colors(CARD_BG, TEXT)

    def _set_status_card_colors(self, bg: str, fg: str):
        # For ttk, easiest: set direct bg on tk widgets; keep ttk frames white normally.
        # Here we only tint big label background via tk.Label? But we use ttk.Label.
        # So: keep it simple—just change big label fg.
        self.status_big.configure(foreground=fg)

    def open_info(self):
        info = []
        info.append("COM Config Utility (Tkinter)")
        info.append(f"Config path: {os.path.abspath(self.config_path)}")
        info.append("")
        info.append("Detected ports:")
        ports = list_ports()
        info.extend(ports if ports else ["(pyserial missing or no ports found)"])
        info.append("")
        info.append("Current loaded values:")
        for k in sorted(self.cfg_values.keys()):
            info.append(f"  {k} = {self.cfg_values[k]}")
        self.dialog_host.show(InfoDialog(self.dialog_host, "\n".join(info)))

    def open_edit_config(self):
        self.dialog_host.show(EditConfigDialog(self.dialog_host, self))

    def reload_config(self):
        if not os.path.exists(self.config_path):
            self.cfg_values = {
                "COM_LASER": "",
                "COM_SFC": "",
                "COM_SCAN": "",
                "BAUDRATE_LASER": "9600",
                "BAUDRATE_SFC": "9600",
                "BAUDRATE_SCAN": "9600",
            }
            self.append_log(f"[WARN] config.ini not found, using defaults.")
            self.set_status("WARN", "config.ini not found (defaults loaded)")
            return

        text = read_text_file(self.config_path)
        cp, errs = sanitize_and_validate_ini(text)
        if cp is None or errs:
            self.append_log("[ERROR] Invalid config.ini")
            for e in errs:
                self.append_log("  - " + e)
            self.set_status("ERROR", "Invalid config.ini")
            self.dialog_host.show(ErrorDialog(self.dialog_host, "CONFIG INVALID", "\n".join(errs) if errs else "Unknown error"))
            return

        # Extract only the parts UI cares about
        self.cfg_values = {}
        self.cfg_values["COM_LASER"] = cp.get("COM", "COM_LASER", fallback="")
        self.cfg_values["COM_SFC"] = cp.get("COM", "COM_SFC", fallback="")
        self.cfg_values["COM_SCAN"] = cp.get("COM", "COM_SCAN", fallback="")

        self.cfg_values["BAUDRATE_LASER"] = cp.get("BAUDRATE", "BAUDRATE_LASER", fallback="9600")
        self.cfg_values["BAUDRATE_SFC"] = cp.get("BAUDRATE", "BAUDRATE_SFC", fallback="9600")
        self.cfg_values["BAUDRATE_SCAN"] = cp.get("BAUDRATE", "BAUDRATE_SCAN", fallback="9600")

        self.append_log("[OK] Loaded config.ini")
        self.set_status("OK", "Config loaded")

    def save_config_values(self) -> tuple[bool, str]:
        # Build a minimal INI content (you có thể merge giữ nguyên các section khác nếu muốn)
        cp = configparser.ConfigParser()
        cp["COM"] = {
            "COM_LASER": self.cfg_values.get("COM_LASER", ""),
            "COM_SFC": self.cfg_values.get("COM_SFC", ""),
            "COM_SCAN": self.cfg_values.get("COM_SCAN", ""),
        }
        cp["BAUDRATE"] = {
            "BAUDRATE_LASER": self.cfg_values.get("BAUDRATE_LASER", "9600"),
            "BAUDRATE_SFC": self.cfg_values.get("BAUDRATE_SFC", "9600"),
            "BAUDRATE_SCAN": self.cfg_values.get("BAUDRATE_SCAN", "9600"),
        }

        # If you want preserve SERIAL_READLINE_BREAK: read old and copy over
        if os.path.exists(self.config_path):
            old = configparser.ConfigParser()
            try:
                old.read(self.config_path, encoding="utf-8")
                if old.has_section("SERIAL_READLINE_BREAK"):
                    cp["SERIAL_READLINE_BREAK"] = dict(old["SERIAL_READLINE_BREAK"])
            except Exception:
                pass

        # Serialize
        from io import StringIO
        buf = StringIO()
        cp.write(buf)
        content = buf.getvalue()

        # Validate before write (schema)
        cp2, errs = sanitize_and_validate_ini(content)
        if cp2 is None or errs:
            return False, "Internal validation failed:\n" + "\n".join(errs)

        try:
            write_text_file_atomic(self.config_path, content)
            self.append_log("[OK] Saved config.ini (backup created if existed)")
            return True, ""
        except Exception as e:
            return False, f"Write failed: {e}"


if __name__ == "__main__":
    app = ComConfigApp("config.ini")
    app.mainloop()
