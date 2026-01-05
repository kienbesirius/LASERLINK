# KPI.py
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from collections.abc import Iterable
from typing import Optional

# Optional: smooth donut (like your Booky _draw_donut)
try:
    from PIL import Image, ImageDraw, ImageTk  # type: ignore
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False


def _safe_avg(values: Iterable[float]) -> Optional[float]:
    vals = list(values) if values is not None else []
    return (sum(vals) / len(vals)) if vals else None


class KPIWidget(ttk.Frame):
    """
    Draw-only KPI widget:
      - Pass/Fail donut (percentage in center)
      - Avg cycle time label
    Parent handles placement (grid/pack/place).

    Usage:
        kpi = KPIWidget(parent)
        kpi.update_kpi(rep_pass=12, rep_total=15, cycle_times=[0.8, 1.0, 0.9])
    """

    def __init__(
        self,
        master,
        *,
        donut_size: int = 54,
        bg: str = "#ffffff",
        base_ring: str = "#ffa494",   # fail-ish base ring
        pass_ring: str = "#7bff82",   # success
        text_color: str = "#222222",
        label_prefix: str = "cycle_time:",
        font_pct=("Segoe UI", 9, "normal"),
        font_avg=("Segoe UI", 9, "normal"),
        padding: int = 0,
        **kwargs,
    ):
        super().__init__(master, padding=padding, **kwargs)

        self._bg = bg
        self._base_ring = base_ring
        self._pass_ring = pass_ring
        self._text_color = text_color
        self._label_prefix = label_prefix
        self._font_pct = font_pct
        self._font_avg = font_avg

        self._rep_pass = 0
        self._rep_total = 0
        self._avg_cycle = None  # seconds
        self._imgtk = None      # keep reference (PIL)

        # layout: canvas + label (parent can still pack/grid this frame anywhere)
        self.columnconfigure(1, weight=1)

        self.donut = tk.Canvas(
            self,
            width=donut_size,
            height=donut_size,
            highlightthickness=0,
            bg=self._bg,
        )
        self.donut.grid(row=0, column=0, sticky="w")


        self.avg_var = tk.StringVar(value=f"{self._label_prefix} --.- s")
        self.avg_lbl = ttk.Label(self, textvariable=self.avg_var)
        self.avg_lbl.grid(row=0, column=1, sticky="nsew", padx=(0, 0))

        self._style = ttk.Style(self)
        self._avg_style = f"KPI.Avg.{id(self)}.TLabel"
        self._frame_style = f"KPI.{id(self)}.TFrame"
        self._label_style = f"KPI.{id(self)}.TLabel"
        self.avg_lbl.configure(style=self._avg_style, font=self._font_avg)

        # apply initial theme to label
        self._style.configure(self._avg_style, background=self._bg, foreground=self._text_color, font=self._font_avg)

        # redraw if resized (when parent decides to stretch it)
        self.donut.bind("<Configure>", lambda e: self._redraw())

        # self._redraw()
        self.after_idle(self._redraw)

    # ---- public API ----
    def update_kpi(
        self,
        *,
        rep_pass: int,
        rep_total: int,
        cycle_times: Optional[Iterable[float]] = None,
        avg_cycle: Optional[float] = None,
    ) -> None:
        """
        Update display values.
        - rep_pass / rep_total controls donut percentage.
        - Provide cycle_times OR avg_cycle for avg label.
        """
        self._rep_pass = int(rep_pass or 0)
        self._rep_total = int(rep_total or 0)

        if avg_cycle is None and cycle_times is not None:
            avg_cycle = _safe_avg(cycle_times)

        self._avg_cycle = avg_cycle

        if self._avg_cycle is None:
            self.avg_var.set(f"{self._label_prefix} --.- s")
        else:
            self.avg_var.set(f"{self._label_prefix} {self._avg_cycle:.3f} s")

        # self._redraw()
        self.after_idle(self._redraw)

    def set_theme(
        self,
        *,
        bg: Optional[str] = None,
        base_ring: Optional[str] = None,
        pass_ring: Optional[str] = None,
        text_color: Optional[str] = None,
    ) -> None:
        """Optional: update colors to match your UI theme."""
        if bg is not None:
            self._bg = bg
            self.donut.configure(bg=bg)
            self._style.configure(self._frame_style, background=bg)
            self._style.configure(self._label_style, background=bg)
        if base_ring is not None:
            self._base_ring = base_ring
        if pass_ring is not None:
            self._pass_ring = pass_ring
        if text_color is not None:
            self._text_color = text_color
            # update label foreground too
            try:
                self._style.configure(self._avg_style, foreground=text_color, background=self._bg)
            except Exception:
                pass

        # self._redraw()
        self.after_idle(self._redraw)

    # ---- internal ----
    def _redraw(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        # lấy size an toàn (Tk đôi khi trả 1 khi chưa layout xong)
        w = int(self.winfo_width() or 0)
        h = int(self.winfo_height() or 0)
        size = max(0, min(w, h))

        # nếu quá nhỏ -> delay redraw (đợi layout xong)
        if size < 6:
            try:
                self.after(30, self._redraw)
            except Exception:
                pass
            return

        # ring width phải <= (size//2 - 1) để bbox không bị âm
        ring_w = int(getattr(self, "ring_w", 6))  # hoặc cách bạn đang tính
        ring_w = max(1, min(ring_w, (size // 2) - 1))

        pad = ring_w // 2 + 1
        x0, y0 = pad, pad
        x1, y1 = size - pad, size - pad

        # guard cuối cùng: bbox phải hợp lệ
        if x1 < x0 or y1 < y0:
            return
        
        # canvas actual size
        # W = max(int(self.donut.winfo_width()), 1)
        # H = max(int(self.donut.winfo_height()), 1)
        W = H = size
        
        total = self._rep_total
        pass_rate = (self._rep_pass / total) if total > 0 else 0.0
        pass_rate = min(max(pass_rate, 0.0), 1.0)
        pass_pct = int(round(pass_rate * 100)) if total > 0 else None

        self.donut.delete("all")

        if _HAS_PIL:
            # smooth donut (like your Booky implementation)
            S = 4
            w2, h2 = W * S, H * S
            img = Image.new("RGBA", (w2, h2), self._bg)
            dr = ImageDraw.Draw(img)

            pad = 1 * S
            ring_w = max(8 * S, 2)
            hole_pad = max(18 * S, 6)

            x0, y0 = pad, pad
            x1, y1 = w2 - pad, h2 - pad

            # base ring
            dr.ellipse((x0, y0, x1, y1), outline=self._base_ring, width=ring_w)

            # pass arc
            if total > 0 and pass_rate > 0:
                start = 270  # 12h
                end = start - 360 * pass_rate
                dr.arc((x0, y0, x1, y1), start=end, end=start, fill=self._pass_ring, width=ring_w)

            # hole
            dr.ellipse((x0 + hole_pad, y0 + hole_pad, x1 - hole_pad, y1 - hole_pad), fill=self._bg)

            img_small = img.resize((W, H), Image.Resampling.LANCZOS)
            self._imgtk = ImageTk.PhotoImage(img_small)
            self.donut.create_image(0, 0, anchor="nw", image=self._imgtk)
        else:
            # pure Tk fallback (slightly less smooth)
            pad = 2
            ring_w = max(min(W, H) // 6, 6)
            hole_pad = max(min(W, H) // 3, 16)

            x0, y0 = pad, pad
            x1, y1 = W - pad, H - pad

            # base ring
            self.donut.create_oval(x0, y0, x1, y1, outline=self._base_ring, width=ring_w)

            if total > 0 and pass_rate > 0:
                # tk arc: start at 90° (12h) but direction differs; use extent negative for clockwise-ish
                extent = -360 * pass_rate
                self.donut.create_arc(
                    x0, y0, x1, y1,
                    start=90,
                    extent=extent,
                    style="arc",
                    outline=self._pass_ring,
                    width=ring_w,
                )

            # hole (paint over center)
            self.donut.create_oval(
                x0 + hole_pad, y0 + hole_pad, x1 - hole_pad, y1 - hole_pad,
                outline=self._bg, fill=self._bg
            )

        # center % text
        self.donut.create_text(
            W / 2, H / 2,
            text=f"{pass_pct}%" if pass_pct is not None else "--%",
            fill=self._text_color,
            font=self._font_pct,
        )
