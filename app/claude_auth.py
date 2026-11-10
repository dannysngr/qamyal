"""Вход в Claude Code из окна Qamyal, без Терминала.
`claude auth login` сам открывает браузер со страницей входа и, когда человек вошёл, завершается.
Если браузер не открылся или сайт вместо возврата показал код, окно показывает ссылку и поле для кода,
а код уходит программе на вход (stdin)."""
import json
import re
import subprocess
import threading
import time

from common import find_claude, load_settings
from sysdeps import HIDDEN

_login = {"proc": None, "url": "", "out": ""}
_cache = [0.0, None]                     # [когда, ответ status()] — окно спрашивает каждые полторы секунды


def status(fresh: bool = False) -> dict:
    """{"installed": …, "logged_in": …}; без fresh — ответ не старше 30 секунд."""
    if not fresh and _cache[1] is not None and time.time() - _cache[0] < 30:
        return _cache[1]
    exe = find_claude(load_settings())
    st = {"installed": bool(exe), "logged_in": False}
    if exe:
        try:
            p = subprocess.run([exe, "auth", "status", "--json"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=20, **HIDDEN)
            st["logged_in"] = bool(json.loads(p.stdout or "{}").get("loggedIn"))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    _cache[:] = [time.time(), st]
    return st


def is_auth_error(err: str) -> bool:
    """Запись не прошла редактуру, потому что Claude не вошёл или вход истёк."""
    e = (err or "").lower()
    return e.startswith("claude") and any(k in e for k in ("authenticate", "oauth", "/login", "not logged in"))


def _running() -> bool:
    p = _login["proc"]
    return bool(p) and p.poll() is None


def _read(p) -> None:
    for line in p.stdout:
        _login["out"] = (_login["out"] + line)[-2000:]
        m = re.search(r"https://\S*oauth\S*", line)
        if m:
            _login["url"] = m.group(0)
    p.wait()
    _cache[1] = None                     # вход мог измениться — следующий status() спросит заново


def start_login() -> None:
    if _running():
        return
    exe = find_claude(load_settings())
    if not exe:
        raise RuntimeError("Claude Code не установлен — сначала нажмите «Установить»")
    p = subprocess.Popen([exe, "auth", "login"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **HIDDEN)
    _login.update(proc=p, url="", out="")
    threading.Thread(target=_read, args=(p,), daemon=True).start()


def send_code(code: str) -> None:
    code = code.strip()
    if not code:
        raise RuntimeError("Вставьте код со страницы входа")
    if not _running():
        raise RuntimeError("Вход уже закончился — нажмите «Войти» ещё раз")
    _login["proc"].stdin.write(code + "\n")
    _login["proc"].stdin.flush()


def cancel_login() -> None:
    if _running():
        _login["proc"].terminate()


def login_state() -> dict:
    """Для окна входа: идёт ли вход, ссылка на страницу входа, вошли ли, что пошло не так."""
    running = _running()
    st = {"running": running, "url": _login["url"], "logged_in": False, "error": ""}
    if _login["proc"] and not running:
        st["logged_in"] = status(fresh=True)["logged_in"]
        if not st["logged_in"]:
            # подсказка «Paste code here if prompted >» без перевода строки приклеивается к следующему тексту
            out = re.sub(r"[^\n]*prompted\s*>\s*", "", _login["out"])
            lines = [x for x in out.splitlines() if x.strip() and "http" not in x]
            st["error"] = (lines[-1] if lines else "Вход не завершён")[:300]
    return st
