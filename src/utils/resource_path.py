# src.utils.resource_path.py
from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from pathlib import Path
from typing import Mapping, List, Union, Callable, Optional

PathLike = Union[str, os.PathLike, Path]
# TÌM THƯ MỤC SRC
src = Path(__file__).resolve()
root = Path(__file__).resolve().parent.parent.parent 
while not src.name.endswith("src") and not src.name.startswith("src"):
    SRC_PATH = src = src.parent
    if(root.name == src.name):
        break

ASSETS_PATH = SRC_PATH / "assets"
ICONS_PATH = ASSETS_PATH / "icons"
IMAGES_PATH = ASSETS_PATH / "images"

def app_dir() -> Path:
    """Folder cài đặt: nơi đặt entry.py (dev) hoặc nơi đặt exe (bundled)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent

def bundled_dir() -> Path:
    """Folder resource đóng gói (PyInstaller): sys._MEIPASS nếu có."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir())).resolve()
    return app_dir()  # dev: dùng luôn app_dir để nhất quán

def external_path(relative_path: str) -> str:
    """File nằm cạnh exe/entry.py."""
    return str(app_dir() / relative_path)

def bundled_path(relative_path: str) -> str:
    """File đã add-data vào gói."""
    return str(bundled_dir() / relative_path)

# ------------------------------- ENSURE LOCAL DIRECTORIES ----------------------------------
@dataclass
class MkdirError:
    name: str
    path: Path
    error: str

def ensure_local_directories(
    folders: Mapping[str, PathLike]
) -> tuple[bool, list[MkdirError]]:
    """
    Tạo các thư mục local nếu chưa tồn tại.

    Parameters
    ----------
    folders : Mapping[str, PathLike]
        Mapping {name: path}
        - name: tên logic để debug (vd: "LOGS", "BACKUP", "CACHE")
        - path: đường dẫn thư mục (str / Path)

        Ví dụ:
            folders = {
                "LOGS":   "/home/te/Documents/Proby/logs",
                "BACKUP": "/home/te/Documents/Proby/backup",
                "TEMP":   "/tmp/proby",
            }

    Returns
    -------
    (ok, errors)
        ok: True nếu tất cả thư mục đều tạo được (hoặc đã tồn tại)
        errors: danh sách lỗi (rỗng nếu ok=True)

    Notes
    -----
    - Hàm sẽ cố tạo TẤT CẢ thư mục, không dừng ở lỗi đầu tiên.
    - Dùng Path.mkdir(parents=True, exist_ok=True) để tạo cả cây thư mục.
    """
    errors: List[MkdirError] = []

    for name, path in folders.items():
        p = Path(path).expanduser()

        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            errors.append(MkdirError(name=name, path=p, error=str(e)))

    return (len(errors) == 0), errors
# ------------------------------------------------------------------------------------------