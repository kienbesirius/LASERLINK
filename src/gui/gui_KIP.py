# # KPI.py
"""
gui_KIP.py

KPIWidget – self-contained KPI tracker + renderer + hourly production overlay.

Shift definition:
  - Day shift  : 07:30 -> 19:30
  - Night shift: 19:30 -> 07:30 (next day)

"KPI day" is counted from 07:30 to next 07:30.
So events between 00:00-07:29 belong to the previous KPI day.

Update API:
  - New (event mode): update_kpi(True/False, cycle_time=...)
      -> stores (timestamp, PASS/FAIL, shift), aggregates by KPI day and hour buckets
  - Legacy: update_kpi(rep_pass=..., rep_total=..., cycle_times=[...]/avg_cycle=...)
      -> absolute counters only (no hourly/history)
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from typing import Deque, Dict, Iterable, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageTk  # type: ignore
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False


def _safe_avg(values: Iterable[float]) -> Optional[float]:
    vals = list(values) if values is not None else []
    return (sum(vals) / len(vals)) if vals else None


def _floor_hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class KPIEvent:
    ts: datetime
    ok: bool
    shift: str      # "DAY" | "NIGHT"
    kpi_day: str    # YYYY-MM-DD (KPI day key)
    cycle_time: Optional[float] = None


class KPIWidget(ttk.Frame):
    _DAY_START = dtime(7, 30)
    _NIGHT_START = dtime(19, 30)

    def __init__(
        self,
        master,
        *,
        donut_size: int = 54,
        bg: str = "#ffffff",
        base_ring: str = "#ffa494",
        pass_ring: str = "#7bff82",
        text_color: str = "#222222",
        link_color: str = "#1a73e8",
        label_prefix: str = "cycle_time:",
        font_pct=("Segoe UI", 9, "normal"),
        font_avg=("Segoe UI", 9, "normal"),
        font_prod=("Segoe UI", 9, "normal"),
        font_link=("Segoe UI", 9, "underline"),
        show_shift_summary: bool = False,
        font_shift=("Segoe UI", 9, "normal"),
        show_hourly_line: bool = True,
        hourly_tick_ms: int = 5000,
        padding: int = 0,
        keep_days: int = 3,
        keep_events_per_day: int = 500,
        **kwargs,
    ):
        super().__init__(master, padding=padding, **kwargs)

        self._bg = bg
        self._base_ring = base_ring
        self._pass_ring = pass_ring
        self._text_color = text_color
        self._link_color = link_color
        self._label_prefix = label_prefix
        self._font_pct = font_pct
        self._font_avg = font_avg
        self._font_prod = font_prod
        self._font_link = font_link
        self._show_shift_summary = bool(show_shift_summary)
        self._font_shift = font_shift
        self._show_hourly_line = bool(show_hourly_line)
        self._hourly_tick_ms = max(int(hourly_tick_ms), 1000)
        self._keep_days = max(int(keep_days or 1), 1)
        self._keep_events_per_day = max(int(keep_events_per_day or 50), 50)
        
        # Init styles
        self._style = ttk.Style(self)
        # NEW: frame background styles (to avoid gray stripes)
        self._frame_style = f"KPI.Frame.{id(self)}.TFrame"
        self._prodrow_style = f"KPI.ProdRow.{id(self)}.TFrame"

        self._avg_style = f"KPI.Avg.{id(self)}.TLabel"
        self._prod_style = f"KPI.Prod.{id(self)}.TLabel"
        self._shift_style = f"KPI.Shift.{id(self)}.TLabel"

        # KPI-day stores (OrderedDict to evict oldest)
        self._days: "OrderedDict[str, dict]" = OrderedDict()
        self._active_day: str = self._calc_kpi_day_key(datetime.now())
        self._ensure_day(self._active_day)

        # display counters (active day)
        self._rep_pass = 0
        self._rep_total = 0
        self._avg_cycle: Optional[float] = None

        self._imgtk = None

        # overlay dialog handle
        self._overlay: Optional[tk.Frame] = None

        # ===== layout =====
        self.columnconfigure(1, weight=1)

        self.donut = tk.Canvas(
            self,
            width=donut_size,
            height=donut_size,
            highlightthickness=0,
            bg=self._bg,
        )
        # donut spans rows to align left nicely
        self.donut.grid(row=0, column=0, rowspan=2, sticky="nsew")

        self.avg_var = tk.StringVar(value=f"{self._label_prefix} --.- s")
        self.avg_lbl = ttk.Label(self, textvariable=self.avg_var)
        self.avg_lbl.grid(row=0, column=1, sticky="nsew")

        # Hourly production line (text + link)
        self.prod_row = ttk.Frame(self, style=self._prodrow_style)
        self.prod_row.grid(row=1, column=1, sticky="nsew")

        # Layout as two rows: production text on top, "more" link on the second row
        self.prod_row.columnconfigure(0, weight=1)

        self.prod_var = tk.StringVar(value="pass: --")
        self.prod_lbl = ttk.Label(self.prod_row, textvariable=self.prod_var)
        self.prod_lbl.grid(row=0, column=0, sticky="w")

        self.more_lbl = ttk.Label(
            self.prod_row,
            text=">> more <<",
            cursor="hand2",
            font=self._font_link,
            background=self._bg,
        )
        self.more_lbl.grid(row=1, column=0, sticky="w")
        self.more_lbl.bind("<Button-1>", lambda e: self.open_hourly_dialog())

        # Shift summary (optional)
        self.shift_var = tk.StringVar(value="")
        self.shift_lbl = ttk.Label(self, textvariable=self.shift_var)
        if self._show_shift_summary:
            self.shift_lbl.grid(row=2, column=1, sticky="nsew")
        else:
            self.shift_lbl.grid_forget()

        if not self._show_hourly_line:
            self.prod_row.grid_remove()

        # ===== styling =====
        self.configure(style=self._frame_style)
        self._style.configure(self._frame_style, background=self._bg)
        self._style.configure(self._prodrow_style, background=self._bg)

        self.avg_lbl.configure(style=self._avg_style, font=self._font_avg)
        self.prod_lbl.configure(style=self._prod_style, font=self._font_prod, background=self._bg,)
        self.shift_lbl.configure(style=self._shift_style, font=self._font_shift)

        self._style.configure(self._avg_style, background=self._bg, foreground=self._text_color, font=self._font_avg)
        self._style.configure(self._prod_style, background=self._bg, foreground=self._text_color, font=self._font_prod)
        self._style.configure(self._shift_style, background=self._bg, foreground=self._text_color, font=self._font_shift)

        self.donut.bind("<Configure>", lambda e: self._redraw())
        self.after_idle(self._sync_from_active_day)

        # periodic tick: update "current hour" line + handle KPI day rollover at 07:30
        self._tick_job = None
        self._start_tick()

    # ===== properties =====
    @property
    def rep_pass(self) -> int:
        return int(self._rep_pass)

    @property
    def rep_total(self) -> int:
        return int(self._rep_total)

    @property
    def rep_fail(self) -> int:
        return int(self._rep_total - self._rep_pass)

    @property
    def active_kpi_day(self) -> str:
        return self._active_day

    # ===== public API =====
    def update_kpi(
        self,
        ok: Optional[bool] = None,
        *,
        # legacy absolute update
        rep_pass: Optional[int] = None,
        rep_total: Optional[int] = None,
        # cycle timing
        cycle_time: Optional[float] = None,
        cycle_times: Optional[Iterable[float]] = None,
        avg_cycle: Optional[float] = None,
        # time injection (testing)
        ts: Optional[datetime] = None,
    ) -> None:
        """
        Preferred (event mode):
            update_kpi(ok=True/False, cycle_time=1.23)

        Legacy (absolute counters):
            update_kpi(rep_pass=12, rep_total=15, cycle_times=[...])
        """

        # Thread-safe: if called from worker thread, bounce to main thread
        if threading.current_thread() is not threading.main_thread():
            self.after(0, lambda: self.update_kpi(
                ok,
                rep_pass=rep_pass, rep_total=rep_total,
                cycle_time=cycle_time, cycle_times=cycle_times, avg_cycle=avg_cycle,
                ts=ts
            ))
            return

        # legacy mode
        if ok is None and rep_pass is not None and rep_total is not None:
            self._rep_pass = int(rep_pass or 0)
            self._rep_total = int(rep_total or 0)
            if avg_cycle is None and cycle_times is not None:
                avg_cycle = _safe_avg(cycle_times)
            self._avg_cycle = avg_cycle
            self._update_avg_label()
            self._update_shift_label()
            self._update_current_hour_label()
            self.after_idle(self._redraw)
            return

        # event mode
        if ok is None:
            return

        ts = ts or datetime.now()

        day_key, shift = self._calc_day_and_shift(ts)
        self._ensure_day(day_key)

        ev = KPIEvent(ts=ts, ok=bool(ok), shift=shift, kpi_day=day_key, cycle_time=cycle_time)

        day = self._days[day_key]

        # store events (for "last N events" / debug)
        events: Deque[KPIEvent] = day["events"]
        events.append(ev)
        while len(events) > self._keep_events_per_day:
            events.popleft()

        # aggregate shift totals
        bucket = day["stats"][shift]
        bucket["total"] += 1
        if ev.ok:
            bucket["pass"] += 1
        if cycle_time is not None:
            bucket["sum_cycle"] += float(cycle_time)
            bucket["n_cycle"] += 1

        # aggregate "current clock-hour" (HH:00-HH+1:00)
        hour_start = _floor_hour(ts)
        hmap: Dict[datetime, dict] = day["clock_hours"]
        h = hmap.setdefault(hour_start, {"total": 0, "pass": 0})
        h["total"] += 1
        if ev.ok:
            h["pass"] += 1

        # aggregate shift/hour bucket (for dialog)
        label = self._find_shift_bucket_label(day_key, shift, ts)
        sb: "OrderedDict[str, dict]" = day["shift_buckets"][shift]
        sb[label]["total"] += 1
        if ev.ok:
            sb[label]["pass"] += 1

        # switch active day if needed
        if day_key != self._active_day:
            self._active_day = day_key

        self._sync_from_active_day()

    def set_theme(
        self,
        *,
        bg: Optional[str] = None,
        base_ring: Optional[str] = None,
        pass_ring: Optional[str] = None,
        text_color: Optional[str] = None,
    ) -> None:
        if bg is not None:
            self._bg = bg
            self.donut.configure(bg=bg)
            self._style.configure(self._avg_style, background=bg)
            self._style.configure(self._prod_style, background=bg)
            self._style.configure(self._shift_style, background=bg)
            self._style.configure(self._frame_style, background=bg)
            self._style.configure(self._prodrow_style, background=bg)
        if base_ring is not None:
            self._base_ring = base_ring
        if pass_ring is not None:
            self._pass_ring = pass_ring
        if text_color is not None:
            self._text_color = text_color
            self._style.configure(self._avg_style, foreground=text_color, background=self._bg)
            self._style.configure(self._prod_style, foreground=text_color, background=self._bg)
            self._style.configure(self._shift_style, foreground=text_color, background=self._bg)

            # ✅ prod text theo link_color
            self._style.configure(self._prod_style, foreground=text_color, background=self._bg)

            self.more_lbl.configure(foreground=text_color, background=self._bg)
            self.prod_lbl.configure(foreground=text_color, background=self._bg)

        self.after_idle(self._redraw)

    def set_show_shift_summary(self, show: bool) -> None:
        self._show_shift_summary = bool(show)
        if self._show_shift_summary:
            self.shift_lbl.grid(row=2, column=1, sticky="w", pady=(2, 0))
        else:
            self.shift_lbl.grid_forget()

    def set_show_hourly_line(self, show: bool) -> None:
        self._show_hourly_line = bool(show)
        if self._show_hourly_line:
            self.prod_row.grid()
        else:
            self.prod_row.grid_remove()

    def open_hourly_dialog(self) -> None:
        """Open nested overlay dialog (covers app window)."""
        # Thread-safe
        if threading.current_thread() is not threading.main_thread():
            self.after(0, self.open_hourly_dialog)
            return

        if self._overlay is not None and self._overlay.winfo_exists():
            # already open -> bring to front
            self._overlay.lift()
            return

        root = self.winfo_toplevel()
        root.update_idletasks()

        overlay = tk.Frame(root, bg="#000000")
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.lift()
        overlay.focus_set()
        self._overlay = overlay

        # click outside -> close (optional)
        overlay.bind("<Escape>", lambda e: self._close_overlay())
        overlay.bind("<Button-1>", lambda e: self._close_overlay())

        # center card
        card = ttk.Frame(overlay, padding=14)
        card.place(relx=0.5, rely=0.5, anchor="center", relwidth=1, relheight=1)
        card.bind("<Button-1>", lambda e: "break")  # prevent overlay click close when clicking inside

        # header
        header = ttk.Frame(card)
        header.pack(fill="x")

        title = ttk.Label(header, text="Thống kê sản lượng theo giờ", font=("Segoe UI", 12, "bold"))
        title.pack(side="left")

        close_btn = tk.Label(
            header, text="✕", cursor="hand2",
            bg=self._bg, fg=self._text_color, font=("Segoe UI", 12, "bold")
        )
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda e: self._close_overlay())

        sub = ttk.Label(card, text=f"KPI day: {self._active_day}  (07:30 → 07:30 ngày hôm sau)")
        sub.pack(anchor="w", pady=(6, 10))

        nb = ttk.Notebook(card)
        nb.pack(fill="both", expand=True)

        tab_day = ttk.Frame(nb, padding=(8, 8))
        tab_night = ttk.Frame(nb, padding=(8, 8))
        nb.add(tab_day, text="Ca sáng (07:30–19:30)")
        nb.add(tab_night, text="Ca tối (19:30–07:30)")

        self._build_hourly_table(tab_day, shift="DAY")
        self._build_hourly_table(tab_night, shift="NIGHT")

    # ===== internal: overlay =====
    def _close_overlay(self) -> None:
        if self._overlay is not None and self._overlay.winfo_exists():
            self._overlay.destroy()
        self._overlay = None

    def _build_hourly_table(self, parent: ttk.Frame, *, shift: str) -> None:
        cols = ("time", "pass", "fail", "total", "yield")
        tree = ttk.Treeview(parent, columns=cols, show="headings", height=12)
        tree.pack(side="left", fill="both", expand=True)

        vsb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        vsb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=vsb.set)

        tree.heading("time", text="Khung giờ")
        tree.heading("pass", text="PASS")
        tree.heading("fail", text="FAIL")
        tree.heading("total", text="TOTAL")
        tree.heading("yield", text="Yield")

        tree.column("time", width=160, anchor="w")
        tree.column("pass", width=70, anchor="e")
        tree.column("fail", width=70, anchor="e")
        tree.column("total", width=70, anchor="e")
        tree.column("yield", width=70, anchor="e")

        # Pull buckets (pre-filled ordered)
        day = self._days.get(self._active_day)
        if not day:
            return

        sb: "OrderedDict[str, dict]" = day["shift_buckets"][shift]
        p_sum = 0
        t_sum = 0
        for label, st in sb.items():
            p = int(st["pass"])
            t = int(st["total"])
            f = t - p
            y = (p / t * 100.0) if t > 0 else 100.0
            tree.insert("", "end", values=(label, p, f, t, f"{y:.1f}%"))
            p_sum += p
            t_sum += t

        f_sum = t_sum - p_sum
        y_sum = (p_sum / t_sum * 100.0) if t_sum > 0 else 100.0
        tree.insert("", "end", values=("— Tổng", p_sum, f_sum, t_sum, f"{y_sum:.1f}%"))

    # ===== internal: day structures =====
    def _ensure_day(self, day_key: str) -> None:
        if day_key in self._days:
            self._days.move_to_end(day_key)
            return

        # prebuild shift buckets in order (so dialog always shows full hour ranges even if 0)
        day_date = datetime.fromisoformat(day_key).date()
        boundaries_day = self._build_hour_boundaries(
            datetime.combine(day_date, self._DAY_START),
            datetime.combine(day_date, self._NIGHT_START),
        )
        boundaries_night = self._build_hour_boundaries(
            datetime.combine(day_date, self._NIGHT_START),
            datetime.combine(day_date + timedelta(days=1), self._DAY_START),
        )

        labels_day = self._boundaries_to_labels(boundaries_day)
        labels_night = self._boundaries_to_labels(boundaries_night)

        shift_buckets_day: "OrderedDict[str, dict]" = OrderedDict((lb, {"pass": 0, "total": 0}) for lb in labels_day)
        shift_buckets_night: "OrderedDict[str, dict]" = OrderedDict((lb, {"pass": 0, "total": 0}) for lb in labels_night)

        self._days[day_key] = {
            "events": deque(),
            "clock_hours": {},  # HH:00->HH+1:00 mapping (datetime -> stats)
            "bucket_boundaries": {"DAY": boundaries_day, "NIGHT": boundaries_night},
            "shift_buckets": {"DAY": shift_buckets_day, "NIGHT": shift_buckets_night},
            "stats": {
                "DAY": {"total": 0, "pass": 0, "sum_cycle": 0.0, "n_cycle": 0},
                "NIGHT": {"total": 0, "pass": 0, "sum_cycle": 0.0, "n_cycle": 0},
            },
        }

        while len(self._days) > self._keep_days:
            self._days.popitem(last=False)

    def _calc_kpi_day_key(self, ts: datetime) -> str:
        if ts.time() < self._DAY_START:
            return (ts.date() - timedelta(days=1)).isoformat()
        return ts.date().isoformat()

    def _calc_day_and_shift(self, ts: datetime) -> Tuple[str, str]:
        t = ts.time()
        if self._DAY_START <= t < self._NIGHT_START:
            return ts.date().isoformat(), "DAY"
        if t < self._DAY_START:
            return (ts.date() - timedelta(days=1)).isoformat(), "NIGHT"
        return ts.date().isoformat(), "NIGHT"

    # ===== internal: hourly buckets (for dialog) =====
    def _build_hour_boundaries(self, start: datetime, end: datetime) -> List[datetime]:
        """
        Build boundaries aligned to clock hours.
        Example 07:30->19:30 => [07:30,08:00,09:00,...,19:00,19:30]
        """
        out = [start]
        cur = start
        if (cur.minute, cur.second, cur.microsecond) != (0, 0, 0):
            nxt = _floor_hour(cur) + timedelta(hours=1)
            if nxt < end:
                out.append(nxt)
                cur = nxt
        while cur + timedelta(hours=1) < end:
            cur = cur + timedelta(hours=1)
            out.append(cur)
        if out[-1] != end:
            out.append(end)
        return out

    def _boundaries_to_labels(self, bounds: List[datetime]) -> List[str]:
        labels: List[str] = []
        for i in range(len(bounds) - 1):
            a = bounds[i]
            b = bounds[i + 1]
            labels.append(f"{a:%H:%M}–{b:%H:%M}")
        return labels

    def _find_shift_bucket_label(self, day_key: str, shift: str, ts: datetime) -> str:
        day = self._days[day_key]
        bounds: List[datetime] = day["bucket_boundaries"][shift]
        # linear scan is fine (<= 14 boundaries). Keep it simple.
        for i in range(len(bounds) - 1):
            if bounds[i] <= ts < bounds[i + 1]:
                return f"{bounds[i]:%H:%M}–{bounds[i+1]:%H:%M}"
        # edge case: ts == end
        return f"{bounds[-2]:%H:%M}–{bounds[-1]:%H:%M}"

    # ===== internal: sync UI =====
    def _sync_from_active_day(self) -> None:
        self._ensure_day(self._active_day)
        stats = self._days[self._active_day]["stats"]

        total = stats["DAY"]["total"] + stats["NIGHT"]["total"]
        passed = stats["DAY"]["pass"] + stats["NIGHT"]["pass"]
        self._rep_total = int(total)
        self._rep_pass = int(passed)

        sum_cycle = stats["DAY"]["sum_cycle"] + stats["NIGHT"]["sum_cycle"]
        n_cycle = stats["DAY"]["n_cycle"] + stats["NIGHT"]["n_cycle"]
        self._avg_cycle = (sum_cycle / n_cycle) if n_cycle > 0 else None

        self._update_avg_label()
        self._update_shift_label()
        self._update_current_hour_label()
        self.after_idle(self._redraw)

    def _update_avg_label(self) -> None:
        self.avg_var.set(
            f"{self._label_prefix} {self._avg_cycle:.3f} s" if self._avg_cycle is not None else f"{self._label_prefix} --.- s"
        )

    def _update_shift_label(self) -> None:
        if not self._show_shift_summary:
            self.shift_var.set("")
            return

        s_day = self._days[self._active_day]["stats"]["DAY"]
        s_night = self._days[self._active_day]["stats"]["NIGHT"]

        def _rate(p: int, t: int) -> float:
            return (p / t * 100.0) if t > 0 else 100.0

        self.shift_var.set(
            f"{self._active_day} | "
            f"DAY {s_day['pass']}/{s_day['total']} ({_rate(s_day['pass'], s_day['total']):.1f}%)  | "
            f"NIGHT {s_night['pass']}/{s_night['total']} ({_rate(s_night['pass'], s_night['total']):.1f}%)"
        )

    def _update_current_hour_label(self) -> None:
        if not self._show_hourly_line:
            return

        now = datetime.now()
        # detect KPI day rollover even without events
        day_now = self._calc_kpi_day_key(now)
        if day_now != self._active_day:
            self._active_day = day_now
            self._ensure_day(self._active_day)
            self._sync_from_active_day()
            return

        hour_start = _floor_hour(now)
        hour_end = hour_start + timedelta(hours=1)

        day = self._days.get(self._active_day)
        if not day:
            self.prod_var.set("pass: --")
            return

        hmap: Dict[datetime, dict] = day["clock_hours"]
        st = hmap.get(hour_start)

        if st is None:
            pass_n = 0
            total_n = 0
        else:
            pass_n = int(st.get("pass", 0))
            total_n = int(st.get("total", 0))

        self.prod_var.set(f"pass ({hour_start:%H:%M}–{hour_end:%H:%M}): {pass_n}")

        # disable link if no event data at all (legacy-only)
        has_any_event = (day["stats"]["DAY"]["total"] + day["stats"]["NIGHT"]["total"]) > 0
        self.more_lbl.configure(state="normal" if has_any_event else "disabled")

    # ===== internal: periodic tick =====
    def _start_tick(self) -> None:
        if self._tick_job is not None:
            try:
                self.after_cancel(self._tick_job)
            except Exception:
                pass
        self._tick_job = self.after(self._hourly_tick_ms, self._on_tick)

    def _on_tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._update_current_hour_label()
        self._start_tick()

    # ===== donut draw =====
    def _redraw(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        W = max(int(self.donut.winfo_width() or 0), 1)
        H = max(int(self.donut.winfo_height() or 0), 1)
        size = max(0, min(W, H))
        if size < 6:
            self.after(30, self._redraw)
            return

        W = H = size
        total = self._rep_total
        pass_rate = (self._rep_pass / total) if total > 0 else 0.0
        pass_rate = min(max(pass_rate, 0.0), 1.0)
        pass_pct = int(round(pass_rate * 100)) if total > 0 else None

        self.donut.delete("all")

        if _HAS_PIL:
            S = 4
            w2, h2 = W * S, H * S
            img = Image.new("RGBA", (w2, h2), self._bg)
            dr = ImageDraw.Draw(img)

            pad = 1 * S
            ring_w = max(8 * S, 2)
            hole_pad = max(18 * S, 6)

            x0, y0 = pad, pad
            x1, y1 = w2 - pad, h2 - pad

            dr.ellipse((x0, y0, x1, y1), outline=self._base_ring, width=ring_w)

            if total > 0 and pass_rate > 0:
                start = 270
                end = start - 360 * pass_rate
                dr.arc((x0, y0, x1, y1), start=end, end=start, fill=self._pass_ring, width=ring_w)

            dr.ellipse((x0 + hole_pad, y0 + hole_pad, x1 - hole_pad, y1 - hole_pad), fill=self._bg)

            img_small = img.resize((W, H), Image.Resampling.LANCZOS)
            self._imgtk = ImageTk.PhotoImage(img_small)
            self.donut.create_image(0, 0, anchor="nw", image=self._imgtk)
        else:
            pad = 2
            ring_w = max(min(W, H) // 6, 6)
            hole_pad = max(min(W, H) // 3, 16)

            x0, y0 = pad, pad
            x1, y1 = W - pad, H - pad

            self.donut.create_oval(x0, y0, x1, y1, outline=self._base_ring, width=ring_w)

            if total > 0 and pass_rate > 0:
                extent = -360 * pass_rate
                self.donut.create_arc(
                    x0, y0, x1, y1,
                    start=90,
                    extent=extent,
                    style="arc",
                    outline=self._pass_ring,
                    width=ring_w,
                )

            self.donut.create_oval(
                x0 + hole_pad, y0 + hole_pad, x1 - hole_pad, y1 - hole_pad,
                outline=self._bg, fill=self._bg
            )

        self.donut.create_text(
            W / 2, H / 2,
            text=f"{pass_pct}%" if pass_pct is not None else "--%",
            fill=self._text_color,
            font=self._font_pct,
        )


__all__ = ["KPIWidget", "KPIEvent"]
