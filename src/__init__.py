# src.__init__.py
from __future__ import annotations

import sys
import os
import time
from pathlib import Path
from src.utils.resource_path import app_dir
from typing import Callable, Dict, List, Optional, Tuple, Union, Set

src = Path(__file__).resolve()
root = Path(__file__).resolve().parent.parent.parent 
while not src.name.endswith("src") and not src.name.startswith("src"):
    src = src.parent
    if(root.name == src.name):
        break

sys.path.insert(0, src)
import re

# ---------- DEFAULT FIELDS (có thể mở rộng sau) ----------
DEFAULTS: Dict[str, Dict[str, str]] = {
    "MO": {},
    "MO_PICKER": {                 # ✅ NEW
        "LAST_SELECTED_MO": "",
    },
    "H_CODE": {},
    "H_CODE_PICKER": {                 # ✅ NEW
        "LAST_SELECTED_H_CODE": "",
    },
    "MODEL": {
        "31-010815": "NEEDPSN06",
        },
    "MODEL_PICKER": {
        "CURRENT_SELECTED_MODEL": "31-010815",
    },
    "COM": {
        "COM_LASER": "COM7",
        "COM_SFC": "COM3",
        "COM_SCAN": "COM999999999",
    },
    "BAUDRATE": {
        "BAUDRATE_LASER": "9600",
        "BAUDRATE_SFC": "9600",
        "BAUDRATE_SCAN": "9600",
    },
    "TIMEOUT":{
        "LASER_TX_SEC": "120",
        "SFC_TX_SEC": "7"
    },
    # Break rules cho kiểu đọc readline (text/line-based COM)
    "SERIAL_READLINE_BREAK": {
        # TOKENS: ưu tiên cao (từ trái sang phải)
        # Quy ước:
        # - END:xxx  -> break khi response kết thúc bằng xxx (endswith)
        # - IN:xxx   -> break khi response chứa xxx (substring)
        # - nếu không prefix -> mặc định IN
        "TOKENS": "UNDO, END:END, MATCHREGEX:NEEDPSN\d+\s*$, MATCHREGEX:FAIL\d+PASS\s*$, MATCHREGEX:FAIL\d+\s*$, MATCHREGEX:PASSED=[01]PASS\s*$, MATCHREGEX:PASSED=[01]\s*$",
        # ALWAYS_LAST: nhóm keyword “cứng” luôn để cuối (PASS/FAIL/ERRO)
        "ALWAYS_LAST": "END:PASSED=1, END:PASSED=0, IN:NEEDPSN, END:PASS, END:FAIL, END:ERRO",
    },
}

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# key=value hoặc key: value (bỏ qua comment)
_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*[:=]")
_NEEDPSN_RX = re.compile(r"^NEEDPSN\d+$", re.IGNORECASE)
_MO_KEY_RX = re.compile(r"^mo\d+$", re.IGNORECASE)
_H_CODE_KEY_RX = re.compile(r"^h_code\d+$", re.IGNORECASE)

def _is_valid_mo_value(val: str) -> bool:
    v = (val or "").strip()
    if not v:
        return False
    # không cho whitespace (space/tab/newline)
    if any(ch.isspace() for ch in v):
        return False
    return len(v) <= 21

def sanitize_ini_inplace(
    path: Path,
    *,
    schema: Dict[str, Dict[str, str]],  # DEFAULTS
    log: Optional[Callable[[str], None]] = None,
    make_backup: bool = True,
) -> bool:
    """
    Sanitize INI theo schema:
      - Section phải nằm trong schema (DEFAULTS)
      - Key trong section phải thuộc schema[section]
      - Dòng rác / key sai section / section lạ -> comment hoá
      - Xoá BOM
    Return True nếu có thay đổi và ghi file OK.
    Return False nếu không đổi gì, hoặc không salvage được (caller sẽ reset).
    """

    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    # ---- Build allowed maps (case-insensitive) ----
    # Map section_lower -> canonical section name
    sec_map = {sec.lower(): sec for sec in schema.keys()}
    # Map canonical section -> allowed_keys_lower
    allowed_keys: Dict[str, Set[str]] = {sec: {k.lower() for k in kv.keys()} for sec, kv in schema.items()}

    try:
        if not path.exists():
            return False
    except Exception as e:
        _log(f"[WARN] sanitize: cannot stat {path}: {e}")
        return False

    # ---- READ ----
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        _log(f"[WARN] sanitize: read failed {path}: {e}")
        return False

    original_raw_for_backup = raw

    changed = False
    # Remove BOM
    if raw.startswith("\ufeff"):
        raw = raw.lstrip("\ufeff")
        changed = True

    lines = raw.splitlines(keepends=True)
    out: list[str] = []

    current_section: Optional[str] = None   # canonical section name
    in_disabled_section: bool = False       # section lạ -> comment hoá toàn bộ block

    def _ensure_newline(line: str) -> str:
        # nếu thiếu newline thì thêm \n để file đẹp và ổn định
        if line.endswith("\r\n") or line.endswith("\n"):
            return line
        return line + "\n"

    for line in lines:
        s = line.strip()

        # Blank
        if not s:
            out.append(_ensure_newline(line))
            continue

        # Comment lines
        if s.startswith(("#", ";")):
            out.append(_ensure_newline(line))
            continue

        # Section header?
        msec = _SECTION_RE.match(s)
        if msec:
            sec_name = msec.group(1).strip()
            sec_lower = sec_name.lower()

            if sec_lower in sec_map:
                # valid section
                current_section = sec_map[sec_lower]
                in_disabled_section = False
                out.append(_ensure_newline(line))
            else:
                # unknown section -> disable whole block
                current_section = None
                in_disabled_section = True
                out.append(_ensure_newline(f"; [SANITIZED][UNKNOWN_SECTION] {s}"))
                changed = True
            continue

        # If we are inside a disabled (unknown) section -> comment everything
        if in_disabled_section:
            out.append(_ensure_newline(f"; [SANITIZED][IN_UNKNOWN_SECTION] {s}"))
            changed = True
            continue

        # Key-value line?
        mk = _KEY_RE.match(line)
        if mk:
            key = mk.group(1).strip()
            key_lower = key.lower()

            # Key appears before any valid section
            if current_section is None:
                out.append(_ensure_newline(f"; [SANITIZED][KEY_OUTSIDE_SECTION] {s}"))
                changed = True
                continue
            
            # ✅ SPECIAL: MODEL section allows dynamic keys, only validate VALUE
            if current_section == "MODEL":
                # parse value (very light, supports key=value or key: value)
                m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*[:=]\s*(.*?)\s*$", line)
                if not m:
                    out.append(_ensure_newline(f"; [SANITIZED][MODEL_BAD_KV] {s}"))
                    changed = True
                    continue
                val = (m.group(2) or "").strip()
                if not _NEEDPSN_RX.fullmatch(val):
                    out.append(_ensure_newline(f"; [SANITIZED][MODEL_INVALID_VALUE] {s}"))
                    changed = True
                    continue

                out.append(_ensure_newline(line))
                continue
            
            # ✅ SPECIAL: MO section allows dynamic keys mo1/mo2/... and validates VALUE
            if current_section == "MO":
                m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*[:=]\s*(.*?)\s*$", line)
                if not m:
                    out.append(_ensure_newline(f"; [SANITIZED][MO_BAD_KV] {s}"))
                    changed = True
                    continue

                k = (m.group(1) or "").strip()
                v = (m.group(2) or "").strip()

                if not _MO_KEY_RX.fullmatch(k):
                    out.append(_ensure_newline(f"; [SANITIZED][MO_INVALID_KEY] {s}"))
                    changed = True
                    continue
            
                if not _is_valid_mo_value(v):
                    out.append(_ensure_newline(f"; [SANITIZED][MO_INVALID_VALUE] {s}"))
                    changed = True
                    continue

                out.append(_ensure_newline(line))
                continue

            if current_section == "H_CODE":
                m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*[:=]\s*(.*?)\s*$", line)
                if not m:
                    out.append(_ensure_newline(f"; [SANITIZED][MO_BAD_KV] {s}"))
                    changed = True
                    continue

                k = (m.group(1) or "").strip()
                v = (m.group(2) or "").strip()

                if not _H_CODE_KEY_RX.fullmatch(k):
                    out.append(_ensure_newline(f"; [SANITIZED][MO_INVALID_KEY] {s}"))
                    changed = True
                    continue
                out.append(_ensure_newline(line))
                continue

            # Validate key belongs to this section
            if key_lower not in allowed_keys.get(current_section, set()):
                out.append(_ensure_newline(
                    f"; [SANITIZED][INVALID_KEY_FOR_SECTION {current_section}] {s}"
                ))
                changed = True
                continue

            # Valid key for section -> keep
            out.append(_ensure_newline(line))
            continue

        # Everything else is junk -> commentize
        out.append(_ensure_newline(f"; [SANITIZED][JUNK] {s}"))
        changed = True

    # Must have at least one valid section header after sanitize
    has_any_valid_section = any(_SECTION_RE.match(l.strip()) and (_SECTION_RE.match(l.strip()).group(1).strip().lower() in sec_map)
                                for l in out if l.strip() and not l.strip().startswith(";"))
    if not has_any_valid_section:
        _log(f"[WARN] sanitize: no valid section found after sanitize: {path}")
        return False

    if not changed:
        return False

    new_text = "".join(out)

    # ---- WRITE (backup + fallback) ----
    try:
        if make_backup:
            ts = time.strftime("%Y%m%d_%H%M%S")
            bak = path.with_suffix(path.suffix + f".bak_{ts}")
            try:
                bak.write_text(original_raw_for_backup, encoding="utf-8")
                _log(f"[INFO] sanitize: backup saved -> {bak}")
            except Exception as e:
                _log(f"[WARN] sanitize: backup write failed ({bak}): {e}")

        path.write_text(new_text, encoding="utf-8")
        _log(f"[INFO] sanitize: sanitized -> {path}")
        return True

    except Exception as e:
        _log(f"[ERROR] sanitize: write failed {path}: {e}")
        try:
            fail_out = path.with_suffix(path.suffix + ".failed_sanitize")
            fail_out.write_text(new_text, encoding="utf-8")
            _log(f"[INFO] sanitize: wrote sanitized content to -> {fail_out}")
        except Exception as e2:
            _log(f"[ERROR] sanitize: fallback write failed: {e2}")
        return False

def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _parse_sections(lines: List[str]) -> Dict[str, Tuple[int, int]]:
    """
    Return mapping: section_name -> (start_index_of_header, end_index_exclusive)
    """
    sections: Dict[str, Tuple[int, int]] = {}
    current_name = None
    current_start = None

    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue

        m = _SECTION_RE.match(s)
        if m:
            name = m.group(1).strip()
            if current_name is not None and current_start is not None:
                sections[current_name] = (current_start, i)
            current_name = name
            current_start = i

    if current_name is not None and current_start is not None:
        sections[current_name] = (current_start, len(lines))

    return sections


def _existing_keys_in_section(lines: List[str], start: int, end: int) -> set[str]:
    keys: set[str] = set()
    for i in range(start + 1, end):
        raw = lines[i]
        stripped = raw.lstrip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        m = _KEY_RE.match(raw)
        if m:
            keys.add(m.group(1).strip().lower())
    return keys


def ensure_config_ini(log_callback=print) -> bool:
    """
    Ensure config.ini tồn tại và có đủ các field trong DEFAULTS.
    - Không override toàn bộ file.
    - Chỉ thêm section/key còn thiếu.
    Return True nếu có thay đổi file, False nếu không đổi gì.
    """
    config_path = app_dir() / "config.ini"
    cfg_path = Path(config_path)

    # 1) Chưa tồn tại -> tạo mới
    if not cfg_path.exists():
        content_lines: List[str] = []
        nl = "\n"
        for sec, kv in DEFAULTS.items():
            content_lines.append(f"[{sec}]{nl}")
            for k, v in kv.items():
                content_lines.append(f"{k}={v}{nl}")
            content_lines.append(nl)

        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("".join(content_lines), encoding="utf-8")
        return True

    sanitize_ini_inplace(path=cfg_path, schema=DEFAULTS, log=log_callback, make_backup=False)
    # 2) Tồn tại -> patch theo dòng
    text = cfg_path.read_text(encoding="utf-8", errors="replace")
    nl = _detect_newline(text)
    lines = text.splitlines(keepends=True)

    changed = False
    sections = _parse_sections(lines)

    # helper: đảm bảo file kết thúc bằng newline để chèn dễ
    def _ensure_trailing_newline():
        nonlocal changed
        if lines and not lines[-1].endswith(("\n", "\r\n")):
            lines[-1] = lines[-1] + nl
            changed = True

    _ensure_trailing_newline()

    for sec, kv in DEFAULTS.items():
        if sec not in sections:
            # Append new section at end (không đụng nội dung cũ)
            if lines and lines[-1].strip() != "":
                lines.append(nl)  # chừa 1 dòng trống trước section mới
            lines.append(f"[{sec}]{nl}")
            for k, v in kv.items():
                lines.append(f"{k}={v}{nl}")
            lines.append(nl)
            changed = True

            # update sections map after append
            sections = _parse_sections(lines)
            continue

        start, end = sections[sec]
        existing = _existing_keys_in_section(lines, start, end)

        missing_items = [(k, v) for k, v in kv.items() if k.lower() not in existing]
        if not missing_items:
            continue

        # Chèn trước "end", nhưng giữ các dòng trống cuối section (nếu có)
        insert_at = end
        while insert_at > start + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1

        patch_lines = [f"{k}={v}{nl}" for k, v in missing_items]
        lines[insert_at:insert_at] = patch_lines
        changed = True

        # vì lines thay đổi, cần parse lại index cho chắc
        sections = _parse_sections(lines)

    if changed:
        cfg_path.write_text("".join(lines), encoding="utf-8")
    return changed
