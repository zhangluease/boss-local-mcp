"""Install the pinned official Chrome for Testing inside the project."""

from __future__ import annotations

import ctypes
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from config import ROOT

MANIFEST = json.loads(files("boss_local_assets").joinpath("browser.json").read_text(encoding="utf-8"))
CHROME_VERSION = MANIFEST["version"]


class BrowserInstallError(RuntimeError):
    pass


class BrowserVersionError(BrowserInstallError):
    pass


def browser_platform(system=None, machine=None):
    system = system or sys.platform
    machine = (machine or platform.machine()).lower()
    if system == "darwin":
        result = "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64" if machine in ("x86_64", "amd64") else None
    elif system == "win32":
        result = "win64" if machine in ("amd64", "x86_64") else "win32" if machine in ("x86", "i386", "i686") else None
    elif system.startswith("linux"):
        result = "linux-arm64" if machine in ("aarch64", "arm64") else "linux64" if machine in ("x86_64", "amd64") else None
    else:
        result = None
    if not result:
        raise BrowserInstallError(f"尚不支持此浏览器平台：{system}/{machine}")
    return result


def browser_directory():
    path = Path(os.getenv("BOSS_BROWSER_DIR", str(ROOT / ".browsers"))).expanduser()
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def managed_executable(platform_name=None, directory=None):
    platform_name = platform_name or browser_platform()
    directory = Path(directory) if directory is not None else browser_directory()
    return directory / CHROME_VERSION / platform_name / MANIFEST["executables"][platform_name]


def configured_executable():
    override = os.getenv("BOSS_CHROME_PATH")
    if not override:
        return managed_executable()
    path = Path(override).expanduser()
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def require_version(product):
    match = re.search(r"\b\d+\.\d+\.\d+\.\d+\b", product or "")
    actual = match.group(0) if match else "未知"
    if actual != CHROME_VERSION:
        raise BrowserVersionError(f"Chrome 版本不一致：要求 {CHROME_VERSION}，实际 {actual}。请退出旧的 MCP 专用 Chrome，运行 boss-local-mcp install-browser 后重试；不会自动关闭现有浏览器。")
    return actual


def _windows_version(path):
    # Read PE version resources without starting the Windows GUI executable.
    from ctypes import wintypes
    library = ctypes.WinDLL("version", use_last_error=True)
    library.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    library.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    library.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    library.GetFileVersionInfoW.restype = wintypes.BOOL
    library.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT)]
    library.VerQueryValueW.restype = wintypes.BOOL
    size = library.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(size)
    if not library.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer, length = wintypes.LPVOID(), wintypes.UINT()
    if not library.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        raise BrowserInstallError("无法读取 Chrome 的文件版本")
    words = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD * 13)).contents
    if words[0] != 0xFEEF04BD:
        raise BrowserInstallError("Chrome 文件版本资源无效")
    return ".".join(str(value) for value in (words[4] >> 16, words[4] & 0xFFFF, words[5] >> 16, words[5] & 0xFFFF))


def executable_version(path):
    path = Path(path)
    if not path.is_file():
        raise BrowserInstallError(f"未安装项目浏览器：{path}。请运行 boss-local-mcp install-browser。")
    if sys.platform == "win32":
        return _windows_version(path)
    result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=15, check=True)
    match = re.search(r"\b\d+\.\d+\.\d+\.\d+\b", result.stdout)
    if not match:
        raise BrowserInstallError(f"无法确认 Chrome 版本：{path}")
    return match.group(0)


def _download(url, target, progress):
    progress(f"下载 {url}")
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as output:
        expected = int(response.headers.get("Content-Length", "0"))
        total, next_notice = 0, 32 * 1024 * 1024
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            total += len(chunk)
            if total >= next_notice:
                progress(f"已下载 {total // 1024 // 1024} MiB" + (f" / {expected // 1024 // 1024} MiB" if expected else ""))
                next_notice += 32 * 1024 * 1024
        if expected and total != expected:
            raise BrowserInstallError("Chrome 下载不完整，请重新运行安装命令")


def _extract(archive, destination):
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            if not (destination / item.filename).resolve().is_relative_to(destination.resolve()):
                raise BrowserInstallError("浏览器压缩包包含无效路径")
        if sys.platform == "darwin":
            # macOS unzip preserves framework symlinks, executable modes, and
            # the complete .app bundle; flattening Contents breaks Chrome.
            subprocess.run(["/usr/bin/unzip", "-q", str(archive), "-d", str(destination)], check=True)
        else:
            package.extractall(destination)
            if os.name != "nt":
                for item in package.infolist():
                    mode = item.external_attr >> 16
                    if not item.is_dir() and mode:
                        (destination / item.filename).chmod(stat.S_IMODE(mode) & 0o777)


def install_browser(progress=None):
    progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
    platform_name = browser_platform()
    root = browser_directory()
    executable = managed_executable(platform_name, root)
    destination = root / CHROME_VERSION / platform_name
    if executable.is_file():
        require_version(executable_version(executable))
        return {"status": "already_installed", "version": CHROME_VERSION, "platform": platform_name, "path": str(executable)}
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f".install-{CHROME_VERSION}-{platform_name}.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise BrowserInstallError(f"已有浏览器安装任务或遗留锁：{lock}。确认没有安装任务后再移除此锁。") from None
    os.close(descriptor)
    try:
        if destination.exists():
            raise BrowserInstallError(f"发现不完整的安装目录：{destination}。请移除此目录后重试。")
        with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as temporary:
            temporary = Path(temporary)
            archive, unpacked = temporary / "chrome.zip", temporary / "unpacked"
            unpacked.mkdir()
            url = f"{MANIFEST['download_base']}/{CHROME_VERSION}/{platform_name}/chrome-{platform_name}.zip"
            _download(url, archive, progress)
            progress("解压并校验 Chrome 版本")
            _extract(archive, unpacked)
            staged = unpacked / MANIFEST["executables"][platform_name]
            require_version(executable_version(staged))
            destination.parent.mkdir(parents=True, exist_ok=True)
            unpacked.rename(destination)
    finally:
        lock.unlink(missing_ok=True)
    return {"status": "installed", "version": CHROME_VERSION, "platform": platform_name, "path": str(executable)}
