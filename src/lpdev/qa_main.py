"""Entry point for the QA timesheet binary.

First run (or ``--setup``) starts the interactive wizard; every run stops a
previous instance via a pid file next to the database, frees the port, opens
the browser tab and runs the timesheet-only service until Ctrl+C. On exit the
browser tab of this instance is closed best-effort via osascript.
"""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from lpdev.config import load_config, resolve_paths
from lpdev.main import bootstrap, create_app
from lpdev.qa_setup import QA_CONFIG_PATH, read_port, run_wizard_if_needed

PID_FILE_NAME = "lpdev-qa.pid"
_STOP_TIMEOUT_SEC = 10.0
_OUR_NAMES = ("lpdev", "python")


class RunError(Exception):
    pass


def qa_pid_path(db_path: Path) -> Path:
    return db_path.parent / PID_FILE_NAME


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_is_ours(pid: int) -> bool:
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="], capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    comm = proc.stdout.decode().strip().lower()
    if not comm:
        return True
    return any(name in comm for name in _OUR_NAMES)


def stop_pid(pid: int, timeout_sec: float = _STOP_TIMEOUT_SEC) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(int(timeout_sec * 10)):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def kill_previous_instance(pid_path: Path) -> bool:
    pid = read_pid(pid_path)
    stopped = False
    if pid is not None and pid != os.getpid() and pid_alive(pid) and pid_is_ours(pid):
        stop_pid(pid)
        stopped = True
    pid_path.unlink(missing_ok=True)
    return stopped


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def close_browser_tabs(port: int) -> None:
    fragment = f"127.0.0.1:{port}/timesheet"
    scripts = [
        f'tell application "Google Chrome" to repeat with w in windows\nclose (every tab of w whose URL contains "{fragment}")\nend repeat',
        f'tell application "Safari" to repeat with w in windows\nclose (every tab of w whose URL contains "{fragment}")\nend repeat',
    ]
    for script in scripts:
        try:
            subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lpdev-qa", description="Таймшит для QA (только вкладка /timesheet)")
    parser.add_argument("--setup", action="store_true", help="перепройти мастер настройки")
    parser.add_argument("--port", type=int, help="порт (по умолчанию — из настроек, иначе 8124)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    return parser.parse_args(argv)


def resolve_config_path() -> Path:
    return Path(os.environ.get("LPDEV_CONFIG") or QA_CONFIG_PATH).expanduser()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    config_path = resolve_config_path()
    os.environ.setdefault("LPDEV_CONFIG", str(config_path))
    try:
        run_wizard_if_needed(force=args.setup, config_path=config_path)
    except Exception as exc:
        print(f"Настройка не завершена: {exc}", file=sys.stderr)
        return 2

    port = args.port or read_port(config_path)
    config = load_config(config_path, timesheet_only=True)
    paths = resolve_paths(config)

    pid_path = qa_pid_path(paths.db)
    if kill_previous_instance(pid_path):
        close_browser_tabs(port)

    if port_in_use(port):
        print(
            f"Порт {port} занят — не удалось запуститься. Если старый инстанс не остановился, "
            "закройте его (Ctrl+C в его окне) и запустите снова.",
            file=sys.stderr,
        )
        return 1

    atexit.register(close_browser_tabs, port)
    try:
        pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
        pid_path.chmod(0o600)
        if not args.no_browser:
            url = f"http://127.0.0.1:{port}/timesheet"
            timer = threading.Timer(1.0, webbrowser.open, [url])
            timer.daemon = True
            timer.start()
        ctx, _ = bootstrap(timesheet_only=True)
        app = create_app(ctx, port, timesheet_only=True)
        uvicorn.run(app, host="127.0.0.1", port=port, workers=1)
    finally:
        pid_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
