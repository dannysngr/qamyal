"""Запуск программы Qamyal: локальный сервер + окно.
Windows: окно Chrome без адресной строки (одно на компьютер).
macOS: окно рисует оболочка Qamyal.app (mac/QamyalApp.swift); она запускает этот файл с --no-window --parent PID,
и сервер завершается вместе с ней."""
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from common import APP, JOBS, LOG, PAUSE, SERVER, pid_alive, read_json, write_json
from sysdeps import IS_WIN

if IS_WIN:
    import ctypes
    from ctypes import wintypes

JOBS.mkdir(exist_ok=True)
if sys.stdout is None:                       # pythonw: печать и предупреждения — в журнал
    sys.stdout = sys.stderr = open(JOBS / ".app.log", "a", encoding="utf-8", buffering=1)


def chrome_exe():
    import winreg
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            p = winreg.QueryValue(root, key)
            if p and Path(p).exists():
                return p
        except OSError:
            pass
    for p in (Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
              Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
              Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe")):
        if p.exists():
            return str(p)
    return None


PROFILE = APP / ".ui-profile"
user32 = ctypes.windll.user32 if IS_WIN else None


def window_rect():
    """Размер окна по умолчанию: компактный, по центру рабочей области экрана."""
    wa = wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(wa), 0)        # SPI_GETWORKAREA
    aw, ah = wa.right - wa.left, wa.bottom - wa.top
    w, h = min(1060, int(aw * 0.85)), min(640, int(ah * 0.92))
    return wa.left + (aw - w) // 2, wa.top + (ah - h) // 2, w, h


def reset_window_size():
    """Один раз забываем размер окна, который Chrome запомнил раньше (он был слишком большим)."""
    mark = PROFILE / ".window-v2"
    prefs = PROFILE / "Default" / "Preferences"
    if mark.exists() or not prefs.exists():
        return
    try:
        import json
        d = json.loads(prefs.read_text(encoding="utf-8"))
        d.get("browser", {}).pop("app_window_placement", None)
        prefs.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        mark.write_text("1")
    except (OSError, ValueError):
        pass


def open_window(port: int, page: str = ""):
    url = f"http://127.0.0.1:{port}/{page}"
    exe = chrome_exe()
    if not exe:
        webbrowser.open(url)
        return None
    reset_window_size()
    x, y, w, h = window_rect()
    return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={PROFILE}",
                             f"--window-size={w},{h}", f"--window-position={x},{y}",
                             "--no-first-run", "--no-default-browser-check",
                             "--disable-features=Translate,MediaRouter", "--disable-sync"])


def find_window():
    """Уже открытое окно программы (окно Chrome-приложения с заголовком Qamyal)."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(h, _):
        if user32.IsWindowVisible(h):
            title, cls = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(h, title, 64)
            user32.GetClassNameW(h, cls, 64)
            if title.value == "Qamyal" and cls.value.startswith("Chrome_WidgetWin"):
                found.append(h)
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else None


def focus_window(h) -> None:
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)                                       # SW_RESTORE
    if not user32.SetForegroundWindow(h):
        user32.keybd_event(0x12, 0, 0, 0)                             # Alt снимает запрет Windows
        user32.keybd_event(0x12, 0, 2, 0)                             # на перевод окна вперёд
        user32.SetForegroundWindow(h)


def single_instance() -> bool:
    """True — мы первые; иначе программа уже запускается или работает."""
    global _mutex
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = k32.CreateMutexW(None, False, "Local\\Qamyal")
    return ctypes.get_last_error() != 183                             # ERROR_ALREADY_EXISTS


_mutex = None


def alive(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/jobs", timeout=2).read()
        return True
    except OSError:
        return False


PORT_FILE = JOBS / ".port"
DEFAULT_PORT = 47615


def pick_port() -> int:
    """Всегда один и тот же адрес: уже открытое окно после перезапуска сервера подключается само."""
    try:
        wanted = int(PORT_FILE.read_text())
    except (OSError, ValueError):
        wanted = DEFAULT_PORT
    for p in (wanted, DEFAULT_PORT, 0):
        with socket.socket() as s:
            if not IS_WIN:      # macOS держит порт закрытого сервера ещё минуту (TIME_WAIT); в Windows этот флаг — «отнять чужой»
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                continue
            port = s.getsockname()[1]
        PORT_FILE.write_text(str(port))
        return port
    raise OSError("нет свободного порта")


def main():
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1]) if "--port" in args else 0
    parent = int(args[args.index("--parent") + 1]) if "--parent" in args else 0   # окно-оболочка macOS
    headless = "--no-window" in args or not IS_WIN

    first = headless or single_instance()
    deadline = time.time() + (0 if first else 30)    # второй ярлык подряд — ждём, пока первый поднимет сервер
    while True:
        info = read_json(SERVER)
        if info and pid_alive(info.get("pid", 0)) and alive(info["port"]):
            if not headless:
                h = find_window()                # программа уже открыта — показываем её окно, второе не нужно
                if h:
                    focus_window(h)
                else:
                    open_window(info["port"])
            return
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    if not first:
        return

    import server
    from recorder import recover_orphans
    recover_orphans()
    try:
        prev = int(PORT_FILE.read_text())          # адрес, на котором работало прошлое окно
    except (OSError, ValueError):
        prev = 0
    port = port or pick_port()
    httpd = server.serve(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    import os
    write_json(SERVER, {"pid": os.getpid(), "port": port})
    server.ensure_worker()

    proc = None
    if not headless:
        h = find_window()
        if h and port == prev:
            focus_window(h)                      # окно осталось от упавшего сервера — оно переподключится само
        else:
            proc = open_window(port)
    try:
        while True:
            time.sleep(2)
            if parent and not pid_alive(parent):
                break                            # оболочку macOS закрыли — запись сохранится в finally
            if headless:
                continue
            # окно считаем закрытым, только если его нет на экране: свёрнутое окно Chrome опрашивает
            # программу редко, а «свой» процесс Chrome бывает уже не тем, что держит окно
            window_gone = (proc is None or proc.poll() is not None) and find_window() is None
            idle = time.time() - server.last_ping[0] > 20
            if window_gone and idle:
                if server.rec.state != "idle":
                    server.rec.stop()            # окно закрыли во время записи — сохраняем её
                break
    except KeyboardInterrupt:
        pass                                     # оболочка macOS завершается (SIGINT) — запись сохранит finally
    finally:
        if server.rec.state != "idle":
            server.rec.stop()
        PAUSE.unlink(missing_ok=True)
        info = read_json(SERVER)
        if info and info.get("pid") == os.getpid():
            SERVER.unlink(missing_ok=True)
        httpd.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise
