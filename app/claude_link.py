"""Подключение Qamyal к ИИ-чатам как MCP-сервера (mcp_server.py): Claude Desktop — через его файл настроек,
Claude Code — командой `claude mcp add` (для всех папок пользователя), Codex — разделом
[mcp_servers.qamyal] в ~/.codex/config.toml (если Codex установлен)."""
import json
import re
import subprocess
import tomllib
from pathlib import Path

from common import APP, codex_home, find_claude, find_codex, read_json, write_json
from sysdeps import HIDDEN, claude_desktop_configs, pipe_python

NAME = "qamyal"
SERVER = APP / "mcp_server.py"


def entry() -> dict:
    return {"command": pipe_python(), "args": ["-X", "utf8", str(SERVER)]}


def _ours(x) -> bool:
    return isinstance(x, dict) and (x.get("args") or [""])[-1] == str(SERVER)


def _codex_config() -> Path:
    return codex_home() / "config.toml"


def _codex_has() -> bool:
    try:
        cfg = tomllib.loads(_codex_config().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return _ours((cfg.get("mcp_servers") or {}).get(NAME))


def status(s: dict) -> dict:
    code = ((read_json(Path.home() / ".claude.json", {}) or {}).get("mcpServers") or {}).get(NAME)
    desktop = claude_desktop_configs()
    return {"code": _ours(code), "code_installed": bool(find_claude(s)),
            "desktop": any(_ours(((read_json(p, {}) or {}).get("mcpServers") or {}).get(NAME)) for p in desktop),
            "desktop_installed": bool(desktop),
            "codex": _codex_has(), "codex_installed": bool(find_codex(s))}


def _claude(s: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run([find_claude(s), "mcp", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, **HIDDEN)


def _codex(on: bool) -> None:
    """Раздел [mcp_servers.qamyal] (и его подразделы) — убрать и, если on, вписать заново в конец.
    Остальной файл не трогаем: правим текст, а не пересобираем TOML."""
    path = _codex_config()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    tomllib.loads(text)                                      # битый файл — ошибка, не перезаписываем
    out, skip = [], False
    for line in text.splitlines():
        head = re.match(r"\s*\[\[?\s*([^\]]+?)\s*\]", line)
        if head:
            name = head.group(1).replace('"', "")
            skip = name == f"mcp_servers.{NAME}" or name.startswith(f"mcp_servers.{NAME}.")
        if not skip:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    if on:
        e = entry()                                          # строки JSON — годные строки TOML
        out += [""] * bool(out) + [f"[mcp_servers.{NAME}]", f"command = {json.dumps(e['command'], ensure_ascii=False)}",
                f"args = [{', '.join(json.dumps(a, ensure_ascii=False) for a in e['args'])}]"]
    new = "\n".join(out) + "\n" if out else ""
    tomllib.loads(new)
    if new != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(path)


def connect(s: dict, on: bool = True) -> dict:
    """on=False — убрать Qamyal отовсюду."""
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
    if find_codex(s) or not on:
        try:
            _codex(on)
        except (OSError, ValueError) as e:
            errors.append(f"Codex: не удалось изменить {_codex_config()} — {e}")
    return {**status(s), "errors": errors}
