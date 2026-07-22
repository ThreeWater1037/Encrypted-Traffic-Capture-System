"""跨平台浏览器可执行文件发现。

Worker 能力探测和实际 Selenium 启动必须调用同一入口，避免主控显示浏览器可用，
执行时却使用另一套路径规则。不同子机器可通过环境变量覆盖自动探测结果。
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Mapping


BROWSER_ENV_VARS = {
    "chrome": "CHROME_BINARY",
    "edge": "EDGE_BINARY",
    "firefox": "FIREFOX_BINARY",
}

_PATH_NAMES = {
    "chrome": ("chrome", "chrome.exe", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
    "edge": ("msedge", "msedge.exe", "microsoft-edge", "microsoft-edge-stable"),
    "firefox": ("firefox", "firefox.exe"),
}

_WINDOWS_APP_NAMES = {
    "chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "firefox": ("firefox.exe",),
}

_WINDOWS_RELATIVE_PATHS = {
    "chrome": (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Chromium/Application/chrome.exe"),
    ),
    "edge": (
        ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
    ),
    "firefox": (
        ("PROGRAMFILES", "Mozilla Firefox/firefox.exe"),
        ("PROGRAMFILES(X86)", "Mozilla Firefox/firefox.exe"),
        ("LOCALAPPDATA", "Mozilla Firefox/firefox.exe"),
    ),
}

_POSIX_CANDIDATES = {
    "chrome": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ),
    "edge": (
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
    ),
    "firefox": (
        "/Applications/Firefox.app/Contents/MacOS/firefox",
        "/Applications/Firefox Developer Edition.app/Contents/MacOS/firefox",
        "/Applications/Firefox Nightly.app/Contents/MacOS/firefox",
        "/usr/bin/firefox",
        "/usr/local/bin/firefox",
    ),
}


def _existing_file(value: str | Path | None) -> str | None:
    """规范化候选路径并仅返回真实文件。"""
    if not value:
        return None
    expanded = os.path.expandvars(str(value)).strip().strip('"')
    path = Path(expanded).expanduser()
    return str(path.resolve()) if path.is_file() else None


def _windows_app_path(executable_names: tuple[str, ...]) -> str | None:
    """从 Windows App Paths 注册表读取安装程序登记的真实位置。"""
    try:
        import winreg
    except ImportError:
        return None

    views = [0]
    for flag_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        flag = getattr(winreg, flag_name, 0)
        if flag not in views:
            views.append(flag)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for executable in executable_names:
            key_name = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
            for view in views:
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                except OSError:
                    continue
                found = _existing_file(value)
                if found:
                    return found
    return None


def discover_browser(
    browser: str,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> str | None:
    """按覆盖变量、PATH、注册表和系统安装目录依次发现浏览器。"""
    name = browser.strip().lower()
    if name not in BROWSER_ENV_VARS:
        raise ValueError(f"不支持的浏览器名称：{browser}")
    environment = os.environ if environ is None else environ

    override = _existing_file(environment.get(BROWSER_ENV_VARS[name]))
    if override:
        return override

    for executable in _PATH_NAMES[name]:
        found = shutil.which(executable)
        if found:
            normalized = _existing_file(found)
            if normalized:
                return normalized

    current_platform = platform_name or platform.system()
    if current_platform.lower() == "windows":
        registered = _windows_app_path(_WINDOWS_APP_NAMES[name])
        if registered:
            return registered
        for root_variable, relative_path in _WINDOWS_RELATIVE_PATHS[name]:
            root = environment.get(root_variable)
            found = _existing_file(Path(root) / relative_path if root else None)
            if found:
                return found

    for candidate in _POSIX_CANDIDATES[name]:
        found = _existing_file(candidate)
        if found:
            return found
    return None
