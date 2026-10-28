"""Обновление Qamyal для Mac изнутри программы. Новые сборки выкладывает GitHub (выпуск «mac-latest»,
файл latest.json: номер сборки и имя образа). Оболочка окна (QamyalApp.swift) передаёт свой номер сборки
и путь к Qamyal.app (QAMYAL_BUILD, QAMYAL_APP); само обновление делает mac/update.sh — скачивает образ
через curl, без пометки «из интернета», поэтому macOS не просит разрешения заново."""
import json
import os
import time
import urllib.request

from common import JOBS, ROOT, read_json, write_json
from sysdeps import IS_MAC, spawn_detached

RELEASE = "https://github.com/dannysngr/qamyal/releases/download/mac-latest/"
CACHE = JOBS / ".update.json"
EVERY = 6 * 3600                                 # проверять не чаще раза в 6 часов


def _here() -> tuple:
    return os.environ.get("QAMYAL_BUILD", ""), os.environ.get("QAMYAL_APP", "")


def check(force: bool = False) -> dict:
    build, app = _here()
    if not (IS_MAC and build and app):
        return {"supported": False}
    c = read_json(CACHE, {}) or {}
    if force or time.time() - c.get("at", 0) > EVERY:
        try:
            try:
                import truststore                # сертификаты системы: Python от uv своих не везёт
                truststore.inject_into_ssl()
            except ImportError:
                pass
            with urllib.request.urlopen(RELEASE + "latest.json", timeout=15) as r:
                c = {"at": time.time(), "latest": json.load(r)}
        except (OSError, ValueError):
            c = {"at": time.time(), "latest": c.get("latest")}
        JOBS.mkdir(exist_ok=True)
        write_json(CACHE, c)
    latest = c.get("latest") or {}
    newer = str(latest.get("build", "")) > build    # номера сборок — ГГГГММДДЧЧММ, сравниваются как строки
    return {"supported": True, "current": build, "available": newer and bool(latest.get("dmg")),
            "version": latest.get("version", "")}


def start() -> None:
    st = check()
    if not st.get("available"):
        raise RuntimeError("Новой версии нет")
    latest = (read_json(CACHE, {}) or {}).get("latest") or {}
    spawn_detached(["/bin/bash", str(ROOT / "mac" / "update.sh"), RELEASE + latest["dmg"], _here()[1]], ROOT)
