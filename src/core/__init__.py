# src.core.__init__.py
from __future__ import annotations

import configparser
import os
import re
import time
import tempfile
import serial
import binascii
from src import *
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

# =========================
# 3) Data structures
# =========================

@dataclass(frozen=True)
class ModelPickerConfig:
    CURRENT_SELECTED_MODEL: str

@dataclass(frozen=True)
class MoPickerConfig:
    LAST_SELECTED_MO: str

@dataclass(frozen=True)
class HCodePickerConfig:
    LAST_SELECTED_H_CODE: str

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
_SEC_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KV_RE  = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*[:=]\s*(.*?)\s*$")
_NEEDPSN_RX = re.compile(r"^NEEDPSN\d+$", re.IGNORECASE)
_MODEL_KEY_RX = re.compile(r"^[A-Za-z0-9_.-]+$")  # hợp với ini key pattern bạn đang dùng

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
        # if m and int(m.group(2)) >= 10 and not p.startswith("\\\\.\\"):
        #     return "\\\\.\\" + p
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
    SEC_MO = "MO" 
    SEC_H_CODE = "H_CODE"
    SEC_MO_PICKER = "MO_PICKER"
    SEC_H_CODE_PICKER = "H_CODE_PICKER"
    SEC_MODEL = "MODEL"
    SEC_MODEL_PICKER = "MODEL_PICKER"

    def __init__(self, config_path: Path, log: Callable[[str], None] = print):
        self.config_path = Path(config_path)
        self.log = log
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(self.config_path, encoding="utf-8")
        self._cp = cp
        self._mtime_ns: int = -1
        self._com: Optional[ComConfig] = None
        self._baud: Optional[BaudrateConfig] = None
        self._rules: List[BreakRule] = []
        self._models: dict[str, str] = {}
        self._models_picker: Optional[ModelPickerConfig] = None

        self._mos: dict[int, str] = {}
        self._mo_picker: Optional[MoPickerConfig] = None
        self._latest_mo: str = ""

        self._h_codes: dict[int, str] = {}
        self._h_code_picker: Optional[HCodePickerConfig] = None
        self._latest_h_code: str = ""

        self.timeout: dict[str, float] = {}
        
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

        # ✅ QUAN TRỌNG: đọc lại file vào ConfigParser
        try:
            self._cp.read(self.config_path, encoding="utf-8")
        except Exception as e:
            self.log(f"[WARN] re-read config failed: {e}")
            
        # ---- load COM ----
        self._cp.read(self.config_path, encoding="utf-8")
        com_laser = self._cp.get(self.SEC_COM, "COM_LASER", fallback="COM1")
        com_sfc   = self._cp.get(self.SEC_COM, "COM_SFC",   fallback="COM2")
        com_scan  = self._cp.get(self.SEC_COM, "COM_SCAN",  fallback="COM3")

        self._com = ComConfig(
            COM_LASER=normalize_windows_com_port(com_laser),
            COM_SFC=normalize_windows_com_port(com_sfc),
            COM_SCAN=normalize_windows_com_port(com_scan),
        )

        # ----- BAUDRATE -----
        def get_int(section: str, key: str, default: int) -> int:
            raw = self._cp.get(section, key, fallback=str(default)).strip()
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


        # ---- Load Models ----
        self._load_models()
        self._load_mos()   # ✅ NEW
        self._load_h_codes()
        
        last_sel = self._cp.get(self.SEC_MO_PICKER, "LAST_SELECTED_MO", fallback="").strip()
        last_h_code_selected = self._cp.get(self.SEC_H_CODE_PICKER, "LAST_SELECTED_H_CODE", fallback="").strip()


        self._mo_picker = MoPickerConfig(LAST_SELECTED_MO=last_sel)
        self._h_code_picker = HCodePickerConfig(LAST_SELECTED_H_CODE=last_h_code_selected)
        
        self.timeout = self._load_timeout_dict()
        return True

    def _load_timeout_dict(self) -> dict[str, float]:
        """
        Return TIMEOUT section as float dict.
        Uses DEFAULTS fallback, clamps invalid/<=0 back to default.
        """
        out: dict[str, float] = {}
        defaults = DEFAULTS.get("TIMEOUT", {})

        # If you store configparser as self._cp (or similar), use it here.
        # Replace self._cp with your actual configparser object.
        cp = self._cp

        for key, default_s in defaults.items():
            raw = ""
            try:
                raw = cp.get("TIMEOUT", key, fallback=default_s)
            except Exception:
                raw = default_s

            try:
                val = float(str(raw).strip())
            except Exception:
                val = float(default_s)

            if val <= 0:
                val = float(default_s)

            out[key] = val

        return out

    def _load_models(self):
        def _parse_section_pairs(sec_name: str) -> list[tuple[str, str]]:
            try:
                raw = self.config_path.read_text(encoding="utf-8", errors="replace")
                # self.log(f"[DEBUG] Reading models from config.ini\n {raw}\n --- END ---\n")
                # te@bekomachj:~/Documents/LaserLink$ /bin/python3 /home/te/Documents/LaserLink/run.py
                # [DEBUG] Reading models from config.ini
                #  [COM]
                # COM_LASER=COM32
                # COM_SFC=/dev/ttyS11
                # COM_SCAN=/dev/ttyS11

                # [BAUDRATE]
                # BAUDRATE_LASER=96002
                # BAUDRATE_SFC=9600
                # BAUDRATE_SCAN=9600

                # [SERIAL_READLINE_BREAK]
                # TOKENS=UNDO, END:END, MATCHREGEX:NEEDPSN\d+\s*$, MATCHREGEX:FAIL\d+PASS\s*$, MATCHREGEX:FAIL\d+\s*$, MATCHREGEX:PASSED=[01]PASS\s*$, MATCHREGEX:PASSED=[01]\s*$
                # ALWAYS_LAST=END:PASSED=1, END:PASSED=0, IN:NEEDPSN, END:PASS, END:FAIL, END:ERRO

                # [MODEL]
                # XX-XXX0123=NEEDPSN04
                # XX-XXX0124=NEEDPSN05
                # XX-XXX0125=NEEDPSN06
                # XX-XXX0126=NEEDPSN07

                # [MODEL_PICKER]
                # CURRENT_SELECTED_MODEL=XX-XXX0123

                #  --- END ---
            except Exception:
                return []
            lines = raw.splitlines()

            pairs: list[tuple[str, str]] = []
            in_sec = False
            for ln in lines:
                s = ln.strip()
                if not s or s.startswith(("#", ";")):
                    continue

                msec = _SEC_RE.match(s)
                if msec:
                    name = msec.group(1).strip()
                    if name.lower() == sec_name.lower():
                        in_sec = True
                    else:
                        if in_sec:
                            break
                        in_sec = False
                    continue

                if not in_sec:
                    continue

                mkv = _KV_RE.match(ln)
                if not mkv:
                    continue
                k = mkv.group(1).strip()
                v = mkv.group(2).strip()
                pairs.append((k, v))
            return pairs

        # 1) models list (preserve original case + order)
        model_pairs = _parse_section_pairs(self.SEC_MODEL)
        models_ordered: dict[str, str] = {}
        canon_by_lower: dict[str, str] = {}

        for k, v in model_pairs:
            lk = k.lower()
            if lk in canon_by_lower:
                continue
            canon_by_lower[lk] = k
            models_ordered[k] = v

        self._models = models_ordered

        # 2) current selected model
        picker_pairs = _parse_section_pairs(self.SEC_MODEL_PICKER)
        cur = ""
        for k, v in picker_pairs:
            if k.strip().lower() == "current_selected_model":
                cur = v.strip()
                break

        if not cur:
            cur = next(iter(self._models.keys()), "")

        # normalize to canonical casing if possible
        if cur and self._models:
            c = canon_by_lower.get(cur.lower())
            if c:
                cur = c
            else:
                # không khớp list -> fallback
                self.log(f"[WARN] CURRENT_SELECTED_MODEL {cur!r} not in [MODEL] -> fallback to first model")
                cur = next(iter(self._models.keys()), "")
        # self.log(f"[DEBUG] --- Loaded Models ---\n {self._models}\n --- END --- \n")
        self._model_picker = ModelPickerConfig(CURRENT_SELECTED_MODEL=cur)
        # self.log(f"[DEBUG] --- Selected Model ---\n {self._model_picker}\n --- END --- \n")

        # 2025-12-26 09:32:43 | INFO     | LASERLINK | [DEBUG] --- Loaded Models ---
        # {'XX-XXX0123': 'NEEDPSN04', 'XX-XXX0124': 'NEEDPSN05', 'XX-XXX0125': 'NEEDPSN06', 'XX-XXX0126': 'NEEDPSN07'}
        # --- END --- 
        # 2025-12-26 09:32:43 | INFO     | LASERLINK | [DEBUG] --- Selected Model ---
        # ModelPickerConfig(CURRENT_SELECTED_MODEL='XX-XXX0123')
        # --- END ---

    def _load_mos(self) -> None:
        # parse raw section pairs giống _load_models() để giữ đúng thứ tự file
        try:
            raw = self.config_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            self._mos = {}
            self._latest_mo = ""
            return

        lines = raw.splitlines()
        in_sec = False
        pairs: list[tuple[str, str]] = []

        for ln in lines:
            s = ln.strip()
            if not s or s.startswith(("#", ";")):
                continue

            msec = _SEC_RE.match(s)
            if msec:
                name = msec.group(1).strip()
                if name.lower() == self.SEC_MO.lower():
                    in_sec = True
                else:
                    if in_sec:
                        break
                    in_sec = False
                continue

            if not in_sec:
                continue

            mkv = _KV_RE.match(ln)
            if not mkv:
                continue
            k = mkv.group(1).strip()
            v = mkv.group(2).strip()
            pairs.append((k, v))

        import re
        mos: dict[int, str] = {}
        for k, v in pairs:
            m = re.match(r"^mo(\d+)$", (k or "").strip(), flags=re.IGNORECASE)
            if not m:
                continue
            try:
                idx = int(m.group(1))
            except Exception:
                continue
            val = (v or "").strip()
            if not val:
                continue
            mos[idx] = val

        self._mos = mos
        self._latest_mo = mos[max(mos.keys())] if mos else ""

    def get_mos(self) -> list[str]:
        self.reload_if_changed()
        if not self._mos:
            return []
        return [self._mos[i] for i in sorted(self._mos.keys())]

    def get_latest_mo(self) -> str:
        self.reload_if_changed()
        return self._latest_mo or ""

    def add_mo(self, mo_value: str, *, persist: bool = True) -> bool:
        import re
        v = re.sub(r"\s+", "", (mo_value or "")).strip()
        if not v:
            return False
        if len(v) > 21:
            v = v[:21]

        self.reload_if_changed()

        # ✅ nếu đã có -> chỉ set selected (KHÔNG thêm mới)
        existing_lower = {str(val).lower(): str(val) for val in (self._mos or {}).values()}
        if v.lower() in existing_lower:
            canon = existing_lower[v.lower()]
            self._mo_picker = MoPickerConfig(LAST_SELECTED_MO=canon)
            if persist:
                return bool(self.update_sections(
                    {self.SEC_MO_PICKER: {"LAST_SELECTED_MO": canon}},
                    make_backup=False,
                    reload_after=True,
                ))
            return True

        # ✅ chưa có -> add mới [Save single MO]
        key = f"mo1"
        self._mos[1] = v

        # # ✅ chưa có -> add mới [Save many MO]
        # next_idx = (max(self._mos.keys()) + 1) if self._mos else 1
        # key = f"mo{next_idx}"

        # self._mos[next_idx] = v

        self._latest_mo = v
        self._mo_picker = MoPickerConfig(LAST_SELECTED_MO=v)

        if persist:
            return bool(self.update_sections(
                {
                    self.SEC_MO: {key: v},
                    self.SEC_MO_PICKER: {"LAST_SELECTED_MO": v},
                },
                make_backup=False,
                reload_after=True,
            ))
        return True


    def get_last_selected_mo(self) -> str:
        self.reload_if_changed()
        return self._mo_picker.LAST_SELECTED_MO if self._mo_picker else ""

    def set_last_selected_mo(self, mo_value: str, *, persist: bool = True) -> bool:
        import re
        v = re.sub(r"\s+", "", (mo_value or "")).strip()
        if not v:
            return False
        if len(v) > 21:
            v = v[:21]

        self.reload_if_changed()
        self._mo_picker = MoPickerConfig(LAST_SELECTED_MO=v)

        if persist:
            return bool(self.update_sections(
                {self.SEC_MO_PICKER: {"LAST_SELECTED_MO": v}},
                make_backup=False,
                reload_after=True,
            ))
        return True


    def _load_h_codes(self) -> None:
        # parse raw section pairs giống _load_models() để giữ đúng thứ tự file
        try:
            raw = self.config_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            self._h_codes = {}
            self._latest_h_code = ""
            return

        lines = raw.splitlines()
        in_sec = False
        pairs: list[tuple[str, str]] = []

        for ln in lines:
            s = ln.strip()
            if not s or s.startswith(("#", ";")):
                continue

            msec = _SEC_RE.match(s)
            if msec:
                name = msec.group(1).strip()
                if name.lower() == self.SEC_H_CODE.lower():
                    in_sec = True
                else:
                    if in_sec:
                        break
                    in_sec = False
                continue

            if not in_sec:
                continue

            mkv = _KV_RE.match(ln)
            if not mkv:
                continue
            k = mkv.group(1).strip()
            v = mkv.group(2).strip()
            pairs.append((k, v))

        import re
        h_codes: dict[int, str] = {}
        for k, v in pairs:
            m = re.match(r"^h_code(\d+)$", (k or "").strip(), flags=re.IGNORECASE)
            if not m:
                continue
            try:
                idx = int(m.group(1))
            except Exception:
                continue
            val = (v or "").strip()
            if not val:
                continue
            h_codes[idx] = val

        self._h_codes = h_codes
        self._latest_h_code = h_codes[max(h_codes.keys())] if h_codes else ""

    def get_h_codes(self) -> list[str]:
        self.reload_if_changed()
        if not self._h_codes:
            return []
        return [self._h_codes[i] for i in sorted(self._h_codes.keys())]

    def get_latest_h_code(self) -> str:
        self.reload_if_changed()
        return self._latest_h_code or ""

    def add_h_code(self, h_code_value: str, *, persist: bool = True) -> bool:
        import re
        v = re.sub(r"\s+", "", (h_code_value or "")).strip()
        if not v:
            return False
        if len(v) > 21:
            v = v[:21]

        self.reload_if_changed()

        # ✅ nếu đã có -> chỉ set selected (KHÔNG thêm mới)
        existing_lower = {str(val).lower(): str(val) for val in (self._h_codes or {}).values()}
        if v.lower() in existing_lower:
            canon = existing_lower[v.lower()]
            self._h_code_picker = HCodePickerConfig(LAST_SELECTED_H_CODE=canon)
            if persist:
                return bool(self.update_sections(
                    {self.SEC_H_CODE_PICKER: {"LAST_SELECTED_H_CODE": canon}},
                    make_backup=False,
                    reload_after=True,
                ))
            return True

        # ✅ chưa có -> add mới [Save single H CODE]
        key = f"h_code1"
        self._h_codes[1] = v

        # # ✅ chưa có -> add mới [Save many MO]
        # next_idx = (max(self._mos.keys()) + 1) if self._mos else 1
        # key = f"mo{next_idx}"

        # self._mos[next_idx] = v

        self._latest_h_code = v
        self._h_code_picker = HCodePickerConfig(LAST_SELECTED_H_CODE=v)

        if persist:
            return bool(self.update_sections(
                {
                    self.SEC_H_CODE: {key: v},
                    self.SEC_H_CODE_PICKER: {"LAST_SELECTED_H_CODE": v},
                },
                make_backup=False,
                reload_after=True,
            ))
        return True


    def get_last_selected_h_code(self) -> str:
        self.reload_if_changed()
        return self._h_code_picker.LAST_SELECTED_H_CODE if self._h_code_picker else ""

    def set_last_selected_h_code(self, h_code_value: str, *, persist: bool = True) -> bool:
        import re
        v = re.sub(r"\s+", "", (h_code_value or "")).strip()
        if not v:
            return False
        if len(v) > 21:
            v = v[:21]

        self.reload_if_changed()
        self._h_code_picker = HCodePickerConfig(LAST_SELECTED_H_CODE=v)

        if persist:
            return bool(self.update_sections(
                {self.SEC_H_CODE_PICKER: {"LAST_SELECTED_H_CODE": v}},
                make_backup=False,
                reload_after=True,
            ))
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

    def get_model_needpsn(self, model_id: str) -> str:
        self.reload_if_changed()
        m = (model_id or "").strip()
        if not m:
            return ""
        # case-insensitive lookup
        for k, v in (self._models or {}).items():
            if k.lower() == m.lower():
                return str(v)
        return ""

    def upsert_model_needpsn(self, model_id: str, needpsn: str, *, persist: bool = True) -> bool:
        mid = (model_id or "").strip()
        np  = (needpsn or "").strip()

        if not mid or not _MODEL_KEY_RX.fullmatch(mid):
            return False
        if not _NEEDPSN_RX.fullmatch(np):
            return False

        self.reload_if_changed()

        # canonicalize key casing if model already exists
        lower_map = {k.lower(): k for k in self._models.keys()}
        canon_mid = lower_map.get(mid.lower(), mid)

        canon_np = np.upper()

        # update cache
        self._models[canon_mid] = canon_np

        if persist:
            ok = bool(self.update_sections(
                {self.SEC_MODEL: {canon_mid: canon_np}},
                make_backup=False,
                reload_after=True,
            ))
            if not ok:
                return False

        return True
    # ----------------------------
    # -------- Model API ---------
    #-----------------------------

    def get_models(self) -> list[str]:
        self.reload_if_changed()
        return list(self._models.keys())
    
    def get_current_selected_model(self) -> str:
        self.reload_if_changed()
        return self._model_picker.CURRENT_SELECTED_MODEL if self._model_picker else ""
    
    def set_current_selected_model(self, model: str, *, persist: bool = True) -> bool:
        model = (model or "").strip()
        if not model:
            return False
        self.reload_if_changed()

        canon = None
        if self._models:
            lower_map = {k.lower(): k for k in self._models.keys()}
            canon = lower_map.get(model.lower())
            if canon is None:
                return False
            model = canon

        self._model_picker = ModelPickerConfig(CURRENT_SELECTED_MODEL=model)

        if persist: 
            try:
                self.update_sections(
                    {self.SEC_MODEL_PICKER: {"CURRENT_SELECTED_MODEL": model}},
                    make_backup=False,
                    reload_after=True,
                )
            except Exception as e:
                return False
            
        return True
    
    @property
    def current_selected_model(self)->str:
        return self.get_current_selected_model()
    
    #--------------------------------
    # -------- End ConfigManager ----
    # -------------------------------
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

import unicodedata

def sanitize_response(s: str) -> str:
    # normalize unicode (BOM, fullwidth, etc.)
    s = unicodedata.normalize("NFKC", s)
    # remove common invisible chars
    s = s.replace("\ufeff", "").replace("\u200b", "").replace("\x00", "")
    # remove other ASCII control chars except \r\n\t
    s = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    return s

def _write_readback_temp_txt(
    content: str,
    *,
    prefix: str = "sfc_resp_",
    suffix: str = ".txt",
    temp_dir: str | Path | None = None,
    log_callback: Callable[[str], None] = print,
) -> str:
    """
    Write content to a temp UTF-8 txt file, log its content, then read back and return.
    """
    tmp_dir = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.gettempdir())
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # mkstemp: lấy fd + path, tránh vấn đề Windows đang mở file khi reopen
    fd, tmp_path_s = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(tmp_dir))
    tmp_path = Path(tmp_path_s)

    try:
        # đóng fd ngay để có thể open lại bình thường (đặc biệt trên Windows)
        os.close(fd)

        tmp_path.write_text(content, encoding="utf-8", errors="strict")

        # log nội dung file vừa ghi
        file_text = tmp_path.read_text(encoding="utf-8", errors="replace")
        log_callback(f"[debug][tempfile] wrote: {tmp_path}")
        log_callback(f"[debug][tempfile] content:\n{file_text}")

        # đọc lại lần nữa để trả về (đúng theo yêu cầu “đọc file rồi trả”)
        readback = tmp_path.read_text(encoding="utf-8", errors="replace")
        return readback

    finally:
        # bạn muốn giữ file để debug thì comment dòng unlink này lại
        try:
            tmp_path.unlink(missing_ok=True)  # py3.8+ ok; nếu thấp hơn thì dùng exists()
        except Exception:
            pass

def _save_raw_capture(
    raw: bytes,
    *,
    prefix: str,
    temp_dir: str | Path | None,
    log_callback: Callable[[str], None],
) -> tuple[Path, Path]:
    """
    Save raw bytes to .bin and hexdump to .hex.txt under temp dir.
    Return (bin_path, hex_path).
    """
    td = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.gettempdir())
    td.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    bin_path = td / f"{prefix}_{ts}.bin"
    hex_path = td / f"{prefix}_{ts}.hex.txt"

    bin_path.write_bytes(raw)

    # hexdump (group 16 bytes/line for readability)
    lines = []
    for i in range(0, len(raw), 16):
        chunk = raw[i : i + 16]
        hexs = binascii.hexlify(chunk).decode("ascii")
        spaced = " ".join(hexs[j:j+2] for j in range(0, len(hexs), 2))
        ascii_preview = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append(f"{i:08x}  {spaced:<47}  |{ascii_preview}|")

    hex_path.write_text("\n".join(lines), encoding="utf-8", errors="replace")

    log_callback(f"[debug][raw] saved bin: {bin_path}")
    log_callback(f"[debug][raw] saved hex: {hex_path}")

    return bin_path, hex_path


import codecs
from typing import Optional

def decode_if_bom(raw: bytes) -> Optional[str]:
    """
    Nếu raw bắt đầu bằng BOM (UTF-8/16/32), decode đúng encoding và trả về str.
    Nếu không có BOM -> return None.
    """
    if not raw:
        return None

    # UTF-8 BOM
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")

    # UTF-16 BOM
    if raw.startswith(codecs.BOM_UTF16_LE):
        return raw.decode("utf-16le", errors="replace")
    if raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16be", errors="replace")

    # UTF-32 BOM (ít gặp nhưng thêm cho đủ)
    if raw.startswith(codecs.BOM_UTF32_LE):
        return raw.decode("utf-32le", errors="replace")
    if raw.startswith(codecs.BOM_UTF32_BE):
        return raw.decode("utf-32be", errors="replace")

    return None


def strip_bom_chars(s: str) -> str:
    """
    BOM trong unicode string thường là '\ufeff'.
    Chỉ strip BOM, không đụng ký tự khác.
    """
    if not s:
        return s
    # Thường BOM ở đầu; nhưng đôi khi bị chèn nhiều chỗ -> replace nhẹ
    return s.lstrip("\ufeff").replace("\ufeff", "")

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
            send_bytes = send_str.encode("ascii", errors="replace")

            # Reset buffer để tránh dính data cũ (stale) từ lần trước
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            ser.write(send_bytes)
            ser.flush()

            # ---- WAIT RESPONSE ----
            deadline = time.time() + read_timeout
            response = ""
            raw_buf = bytearray()   # <-- NEW: gom raw bytes

            IDLE_AFTER_MATCH = 0.2
            post_match_deadline = None

            while time.time() < deadline:
                # readline() phù hợp khi thiết bị có '\n' kết thúc dòng
                line = ser.readline()
                if line:

                    raw_buf.extend(line)  # <-- NEW
                    # log raw bytes của chunk này (ngắn gọn)
                    log_callback(f"[debug][{port}][raw] {binascii.hexlify(line).decode('ascii')}")
                    # Decode text: ưu tiên utf-8, fallback latin-1 để không crash
                    try:
                        decoded = line.decode("ascii")
                    except Exception:
                        try:
                            decoded = line.decode("utf-8")
                        except Exception:
                            decoded = line.decode("latin-1", errors="ignore")

                    response += decoded
                    log_callback(f"[debug][{port}] -> {decoded!r}")

                    # Dừng sớm nếu đã thấy keyword trạng thái
                    # (tuỳ protocol, bạn có thể đổi keyword)
                    # upper = response.upper()
                    # TODO: READ LAW FROM CONFIG TO CHECK THE BREAK CONDITIONS
                    if should_break(response, rules):
                        post_match_deadline = time.time() + IDLE_AFTER_MATCH
                else:
                    if post_match_deadline and time.time() >= post_match_deadline:
                        break
                    # Ngủ nhẹ để tránh while loop ăn CPU 100%
                    time.sleep(0.001)

            # upper = response.upper()
            # if "FAIL" in upper or "ERRO" in upper:
            #     return False, f"{port} FAIL/ERRO - {response.strip()}"

            # ---- NEW: log tổng quan raw capture ----
            raw_bytes = bytes(raw_buf)
            if raw_bytes:
                # check dấu hiệu UTF-16/padding
                has_nul = (b"\x00" in raw_bytes)
                log_callback(f"[debug][raw] total_len={len(raw_bytes)} has_NUL={has_nul}")

                # lưu file để soi
                _save_raw_capture(
                    raw_bytes,
                    prefix=f"{port}_laser_resp",
                    temp_dir=app_dir(),
                    log_callback=log_callback,
                )

            if response.strip():
                log_callback("[Original]")
                log_callback(f"[debug] resp_repr={response!r}")
                
                # --- BOM-aware normalize ---
                fixed = None

                # 1) ưu tiên BOM từ raw_bytes (đúng nguồn nhất)
                bom_decoded = decode_if_bom(raw_bytes)
                if bom_decoded is not None:
                    fixed = bom_decoded
                    log_callback("[BOM Detected in RAW] decoded_from_raw_bytes")

                # 2) nếu không có BOM bytes nhưng string có BOM char
                elif "\ufeff" in response:
                    fixed = strip_bom_chars(response)
                    log_callback("[BOM Char Detected in STR] stripped_unicode_bom")

                # 3) nếu không có vấn đề -> giữ như cũ
                final_resp = fixed if fixed is not None else response

                # (tuỳ bạn) nếu bạn muốn “clean” thêm như sanitize_response, chỉ áp khi đã fixed
                # nếu không muốn thay đổi khi bình thường -> đừng áp cho case fixed is None
                if fixed is not None:
                    final_resp = sanitize_response(final_resp)

                log_callback("[Final Used]")
                log_callback(f"[debug] resp_repr={final_resp!r}")
                is_bom = '\\ufeff' in final_resp
                log_callback(f"[debug] has_BOM={is_bom} len={len(final_resp)}")

                # ---- write temp -> log -> read back -> return ----
                readback = _write_readback_temp_txt(
                    final_resp.strip(),
                    temp_dir=app_dir(),
                    log_callback=log_callback,
                )
                return True, readback.strip()


            return False, "No response (timeout)"

    except serial.SerialException as e:
        log_callback(f"[ERROR] Serial error on {port}: {e}")
        return False, f"Serial error: {e}"

def send_text_only(
    text: str,
    port: str = "COM7",
    baudrate: int = 9600,
    write_append_crlf: bool = True,
    read_timeout: float = 5.0,
    log_callback: Callable[[str], None] = print,
) -> Tuple[bool, str]:
    try:
        CFG.reload_if_changed()
        rules = CFG.rules

        with serial.Serial(port, baudrate, timeout=0) as ser:
            # ---- SEND ----
            # Nhiều thiết bị text-based yêu cầu CRLF để kết thúc frame/lệnh.
            send_str = text + ("\r\n" if write_append_crlf else "")
            send_bytes = send_str.encode("ascii", errors="replace")

            # Reset buffer để tránh dính data cũ (stale) từ lần trước
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            ser.write(send_bytes)
            ser.flush()
            return True, "Sent successfully"

    except serial.SerialException as e:
        log_callback(f"[ERROR] Serial error on {port}: {e}")
        return False, f"Serial error: {e}"


def send_text_and_polling(
    text: str,
    port: str = "COM7",
    baudrate: int = 9600,
    write_append_crlf: bool = True,
    read_timeout: float = 2.0,
    log_callback: Callable[[str], None] = print,
    rules: Optional[List["BreakRule"]] = None,
    idle_after_match: float = 0.2,   # chờ thêm sau khi match break
    idle_no_new_data: float = 0.3,   # nếu đã có data rồi mà im lặng quá lâu thì coi như xong
) -> Tuple[bool, str]:
    try:
        # nếu bạn vẫn muốn lấy rules từ CFG thì cứ làm như cũ, hoặc truyền rules vào
        if rules is None:
            CFG.reload_if_changed()
            rules = CFG.rules

        # timeout=0 => non-blocking (read trả ngay). Ta tự timeout bằng deadline
        with serial.Serial(port, baudrate, timeout=0, write_timeout=1.0) as ser:
            # ---- SEND ----
            send_str = text + ("\r\n" if write_append_crlf else "")
            send_bytes = send_str.encode("utf-8", errors="replace")

            ser.reset_input_buffer()
            ser.reset_output_buffer()

            ser.write(send_bytes)
            ser.flush()

            # ---- WAIT RESPONSE (BYTE-BASED) ----
            deadline = time.time() + read_timeout
            response = ""  # decode dần ra string để check break rules
            last_rx_time = None
            post_match_deadline = None

            while time.time() < deadline:
                n = ser.in_waiting
                if n:
                    chunk = ser.read(n)
                    last_rx_time = time.time()

                    # decode chunk (ưu tiên utf-8, fallback latin-1)
                    try:
                        decoded = chunk.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded = chunk.decode("latin-1", errors="ignore")

                    response += decoded
                    log_callback(f"[debug][{port}] rx={decoded!r}")

                    # nếu match điều kiện kết thúc: đừng break ngay, chờ thêm chút để hốt đuôi
                    if should_break(response, rules):
                        post_match_deadline = time.time() + idle_after_match

                else:
                    now = time.time()

                    # nếu đã match break trước đó và đã “idle đủ lâu” => kết thúc
                    if post_match_deadline and now >= post_match_deadline:
                        break

                    # nếu đã nhận được data rồi nhưng im lặng quá lâu => cũng có thể kết thúc
                    if last_rx_time and (now - last_rx_time) >= idle_no_new_data:
                        break

                    time.sleep(0.01)

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
