"""Installed-edition identity, kept independent of Qt and instrument drivers."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import sys

APP_ID = "{D9D0E8E6-83E5-46F4-B5B1-9B3D7EE9C523}"
APP_MUTEX = r"Local\LabControlCenter.App"
SETUP_MUTEX = r"Local\LabControlCenter.Setup"
MARKER_NAME = "installed-mode.json"
PENDING_MARKER_NAME = "install-pending.json"
MARKER_FORMAT = "lab-control-center-installed"
_mutex_handle = None


def installed_info() -> dict | None:
    if not getattr(sys, "frozen", False):
        return None
    executable_root = Path(sys.executable).resolve().parent
    if (executable_root / PENDING_MARKER_NAME).exists():
        raise RuntimeError("安装尚未完成，请重新运行安装程序；用户数据及备份仍保留")
    marker = executable_root / MARKER_NAME
    if not marker.exists():
        return None
    try:
        raw = marker.read_bytes()
        if len(raw) > 8192:
            raise ValueError("标记过大")
        data = json.loads(raw.decode("utf-8-sig"))
        if (not isinstance(data, dict) or data.get("format") != MARKER_FORMAT
                or data.get("schema_version") != 1 or data.get("app_id") != APP_ID
                or not isinstance(data.get("data_dir"), str)):
            raise ValueError("标记格式无效")
        suffix = data.get("instance_suffix", "")
        if not isinstance(suffix, str) or (suffix and re.fullmatch(r"-Test-[A-Za-z0-9_-]{1,40}", suffix) is None):
            raise ValueError("安装实例标识无效")
        path = Path(data["data_dir"])
        if not path.is_absolute() or not path.name:
            raise ValueError("数据目录无效")
        install_root = marker.parent
        resolved = path.resolve()
        if resolved == install_root or resolved.is_relative_to(install_root):
            raise ValueError("数据目录不能位于程序目录内")
        return data
    except (OSError, ValueError, RecursionError) as exc:
        raise RuntimeError(f"安装标记损坏，请重新安装以恢复数据目录设置：{exc}") from exc


def is_installed() -> bool:
    return installed_info() is not None


def acquire_application_mutex() -> None:
    """Keep the installer from replacing a live installed application."""
    global _mutex_handle
    if os.name != "nt" or _mutex_handle is not None:
        return
    info = installed_info()
    if info is None:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, APP_MUTEX + info.get("instance_suffix", ""))
    error = ctypes.get_last_error()
    if not handle:
        raise RuntimeError("无法创建安装程序互斥锁，请稍后重试")
    if error == 183:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)
        raise RuntimeError("安装版已经运行，请使用已打开的窗口")
    # Windows closes it at process termination, after all instrument threads exit.
    _mutex_handle = handle
