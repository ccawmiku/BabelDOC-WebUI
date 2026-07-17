from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_URL = "http://127.0.0.1:8787/"
HEALTH_URL = f"{APP_URL}api/health"
STARTUP_TIMEOUT_SECONDS = 15 * 60


def candidate_directories() -> list[Path]:
    executable_dir = Path(sys.executable).resolve().parent
    source_dir = Path(__file__).resolve().parent
    return [executable_dir, Path.cwd().resolve(), source_dir]


def find_project_root(candidates: list[Path] | None = None) -> Path | None:
    seen: set[Path] = set()
    for candidate in candidates or candidate_directories():
        for directory in (candidate, *candidate.parents):
            if directory in seen:
                continue
            seen.add(directory)
            if (directory / "start-web.bat").is_file() and (
                directory / "pyproject.toml"
            ).is_file():
                return directory
    return None


def service_is_ready(timeout: float = 1.0) -> bool:
    try:
        # The URL is a fixed loopback HTTP endpoint, never user-controlled.
        with urllib.request.urlopen(  # noqa: S310
            HEALTH_URL,
            timeout=timeout,
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def show_error(message: str) -> None:
    ctypes.windll.user32.MessageBoxW(0, message, "BabelDOC Web", 0x10)


def start_service(project_root: Path) -> subprocess.Popen[bytes]:
    if getattr(sys, "frozen", False):
        # PyInstaller temporarily prepends its extraction directory to the DLL
        # search path. Reset it before starting the system PowerShell/Python.
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    start_script = project_root / "scripts" / "start-web.ps1"
    if not powershell.is_file():
        raise FileNotFoundError("Windows PowerShell is unavailable")
    if not start_script.is_file():
        raise FileNotFoundError("scripts/start-web.ps1 is unavailable")
    log_dir = project_root / ".babeldoc-web"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "launcher.log"
    log = log_path.open("ab")
    child_environment = os.environ.copy()
    for name in tuple(child_environment):
        if name.startswith("_PYI_"):
            child_environment.pop(name)
    child_environment.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
    child_environment["BABELDOC_DIAGNOSTIC_LOG"] = str(log_path)
    try:
        # The executable and script are resolved trusted local paths.
        return subprocess.Popen(  # noqa: S603
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(start_script),
                "-NoBrowser",
            ],
            cwd=project_root,
            env=child_environment,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup_info,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    finally:
        log.close()


def wait_for_service(process: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if service_is_ready():
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def main() -> int:
    if service_is_ready():
        webbrowser.open(APP_URL)
        return 0

    project_root = find_project_root()
    if project_root is None:
        show_error(
            "找不到 start-web.bat。请把 BabelDOC-Web.exe 放在项目根目录后重试。"
        )
        return 1

    try:
        process = start_service(project_root)
    except OSError as error:
        show_error(f"无法启动本地服务：{error}")
        return 1
    if not wait_for_service(process):
        show_error(
            "本地服务启动失败。请运行 start-web.bat 查看详细错误，"
            "或检查 .babeldoc-web\\launcher.log。支持 Python 3.10–3.13。"
        )
        return 1

    webbrowser.open(APP_URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
