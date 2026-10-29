"""Подключение Qamyal к Claude как MCP-сервера (mcp_server.py): Claude Desktop — через его файл настроек,
Claude Code — командой `claude mcp add` (для всех папок пользователя)."""
import subprocess
from pathlib import Path

from common import APP, find_claude, read_json, write_json
from sysdeps import HIDDEN, claude_desktop_configs, pipe_python

NAME = "qamyal"
SERVER = APP / "mcp_server.py"


def entry() -> dict:
    return {"command": pipe_python(), "args": ["-X", "utf8", str(SERVER)]}


def _ours(x) -> bool:
    return isinstance(x, dict) and (x.get("args") or [""])[-1] == str(SERVER)


def status(s: dict) -> dict:
    code = ((read_json(Path.home() / ".claude.json", {}) or {}).get("mcpServers") or {}).get(NAME)
    desktop = claude_desktop_configs()
    return {"code": _ours(code), "code_installed": bool(find_claude(s)),
            "desktop": any(_ours(((read_json(p, {}) or {}).get("mcpServers") or {}).get(NAME)) for p in desktop),
            "desktop_installed": bool(desktop)}


def _claude(s: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run([find_claude(s), "mcp", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, **HIDDEN)


def connect(s: dict, on: bool = True) -> dict:
    """on=False — убрать Qamyal из обоих мест."""
    errors = []
    for p in claude_desktop_configs():
        cfg = read_json(p, None) if p.exists() else {}      # битый файл не перезаписываем
        if not isinstance(cfg, dict):
            errors.append(f"Не удалось прочитать {p} — файл настроек Claude Desktop повреждён")
            continue
        servers = cfg.setdefault("mcpServers", {})
        if on:
            servers[NAME] = entry()
        elif servers.pop(NAME, None) is None:
            continue
        write_json(p, cfg, indent=2)
    if find_claude(s):
        _claude(s, "remove", "-s", "user", NAME)            # прежний путь (программу перенесли) — заменяем
        if on:
            e = entry()
            r = _claude(s, "add", "-s", "user", NAME, "--", e["command"], *e["args"])
            if r.returncode:
                errors.append("Claude Code: " + (r.stderr or r.stdout).strip()[-300:])
    return {**status(s), "errors": errors}
