"""MCP-сервер Qamyal: Claude Desktop и Claude Code ищут в записях, читают расшифровки, ведут задачи.

Работает сам по себе — окно Qamyal может быть закрыто: читает папку «Записи» напрямую.
Протокол — MCP по stdin/stdout (JSON-RPC 2.0, одно сообщение — одна строка).
Подключается кнопкой в Настройках (claude_link.py) или вручную:
    claude mcp add -s user qamyal -- <python> -X utf8 <путь к app>/mcp_server.py
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
OUT = sys.stdout.buffer
sys.stdout = sys.stderr            # случайный print из модулей программы не должен попасть в протокол

from common import (JOBS, TASKS_STATE, backup, find_job, fmt_duration, fmt_time, job_id, list_jobs,  # noqa: E402
                    load_projects, load_settings, parse_time, projects_of, read_json, settle_tasks, task_key,
                    write_json)

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
CHUNK = 60000                      # столько знаков расшифровки за один вызов — дальше «продолжение»

INSTRUCTIONS = """Qamyal — программа пользователя для записи и расшифровки рабочих созвонов и встреч.
У каждой записи: название, дата, участники, проекты, краткое содержание, решения, задачи и расшифровка по репликам со временем.
Как отвечать на вопросы о разговорах:
- сначала find_records или search_transcripts, потом read_record нужных записей;
- ссылайся на запись и время: «Вертикальная секция в 3D», 21.10, 12:34;
- письма и напоминания — только по сказанному и решённому, без своих предложений и дат;
- add_task и mark_task меняют данные пользователя — вызывай их, только когда он сам об этом просит."""


class Problem(Exception):
    """Ошибка в запросе — Claude увидит текст и поправит вызов."""


# ---------- данные ----------

def _ts(j: dict) -> float:
    return j.get("ts") or j.get("added") or 0


def _records() -> list:
    """[(папка, job, итог)] готовых записей, новые сверху."""
    out = []
    for d, j in list_jobs():
        res = read_json(d / "черновики" / "итог.json") if j.get("status") == "done" else None
        if res:
            out.append((d, j, res))
    out.sort(key=lambda x: -_ts(x[1]))
    return out


def _low(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


def _day(s: str, end: bool = False) -> float:
    """«2026-10-27», «27.10.2026», «27.10» → начало (или конец) дня."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{1,2}\.\d{1,2}", s):
        s = f"{s}.{datetime.now().year}"
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).timestamp() + (86400 if end else 0)
        except ValueError:
            pass
    raise Problem(f"Не понял дату «{s}» — нужно ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")


def _in_range(j: dict, a: dict) -> bool:
    ts = _ts(j)
    return (not a.get("date_from") or ts >= _day(a["date_from"])) and \
           (not a.get("date_to") or ts < _day(a["date_to"], end=True))


def _person_in(res: dict, who: str) -> bool:
    who = _low(who)
    return any(who in _low(p) for p in res.get("participants") or []) or \
        any(who in _low(t.get("who")) for t in res.get("turns") or [])


def _project_ok(j: dict, res: dict, project: str) -> bool:
    return not project or any(_low(project) in _low(p) for p in projects_of(j) or projects_of(res))


def _link(jid: str, t=None) -> str:
    try:
        port = (JOBS / ".port").read_text().strip()
    except OSError:
        port = "47615"
    return f"http://127.0.0.1:{port}/#/job/{jid}" + (f"?t={int(t)}" if t is not None else "")


def _job(jid: str):
    d = find_job((jid or "").strip())
    if not d:
        raise Problem(f"Записи {jid} нет — номер берётся из find_records или search_transcripts")
    j = read_json(d / "job.json", {})
    res = read_json(d / "черновики" / "итог.json") if j.get("status") == "done" else None
    if not res:
        raise Problem("Эта запись ещё расшифровывается")
    return d, j, res


def _dur(sec: float) -> str:
    return fmt_duration(sec) if sec >= 60 else f"{round(sec)} с"


def _head(d: Path, j: dict, res: dict) -> str:
    ps = projects_of(j) or projects_of(res)
    bits = [j.get("date", ""), _dur(j.get("duration") or res.get("duration") or 0)]
    if ps:
        bits.append("проект: " + ", ".join(ps))
    return f"«{res.get('title') or j.get('name', d.name)}» — запись {job_id(d, j)} · " + " · ".join(b for b in bits if b)


def _tasks(records=None) -> list:
    done = read_json(TASKS_STATE, {}) or {}
    out = []
    for d, j, res in records if records is not None else _records():
        jid = job_id(d, j)
        for t in res.get("tasks") or []:
            t = t if isinstance(t, dict) else {"what": str(t)}
            key = task_key(jid, t.get("what", ""))
            out.append({"key": key, "job": jid, "title": res.get("title", ""), "date": j.get("date", ""),
                        "ts": _ts(j), "who": t.get("who", ""), "what": t.get("what", ""), "when": t.get("when", ""),
                        "t": t.get("t"), "done": bool(done.get(key)),
                        "project": t.get("project") or next(iter(projects_of(j)), "")})
    return out


def _task_line(t: dict, with_record: bool = True) -> str:
    s = f"[{'сделано' if t['done'] else 'открыто'}] " + (f"{t['who']}: " if t["who"] else "") + t["what"]
    if t["when"]:
        s += f" (срок: {t['when']})"
    if t["project"]:
        s += f" — {t['project']}"
    if with_record:
        days = max(0, int((time.time() - t["ts"]) / 86400))
        s += f"\n    из «{t['title']}», {t['date'][:10]} ({days} дн. назад); запись {t['job']}" + \
             (f" · {t['t']}" if t.get("t") else "")
    return s + f"; ключ {t['key']}"


# ---------- инструменты ----------

def find_records(a: dict) -> str:
    words = _low(a.get("query")).split()
    hits = []
    for d, j, res in _records():
        if not (_in_range(j, a) and _project_ok(j, res, a.get("project", ""))
                and (not a.get("person") or _person_in(res, a["person"]))):
            continue
        hay = _low(" ".join([res.get("title", ""), " ".join(res.get("summary") or []),
                             " ".join(res.get("participants") or []), " ".join(projects_of(j))]))
        if all(w in hay for w in words):
            hits.append((d, j, res))
    limit = int(a.get("limit") or 30)
    if not hits:
        return "Ничего не нашлось. Попробуйте search_transcripts — он ищет по самим разговорам."
    lines = [f"Найдено записей: {len(hits)}" + (f", показаны {limit} новых" if len(hits) > limit else "")]
    for d, j, res in hits[:limit]:
        lead = (res.get("summary") or [""])[0]
        lines.append(f"\n{_head(d, j, res)}\n    участники: {', '.join(res.get('participants') or []) or '—'}; "
                     f"задач: {len(res.get('tasks') or [])}" + (f"\n    {lead}" if lead else ""))
    return "\n".join(lines)


def search_transcripts(a: dict) -> str:
    words = _low(a.get("text")).split()
    if not words:
        raise Problem("Нужен текст для поиска")
    limit, hits, total = int(a.get("limit") or 40), [], 0
    for d, j, res in _records():
        if not (_in_range(j, a) and _project_ok(j, res, a.get("project", ""))):
            continue
        for t in res.get("turns") or []:
            if a.get("person") and _low(a["person"]) not in _low(t.get("who")):
                continue
            low = _low(t["text"])
            if all(w in low for w in words):
                total += 1
                if len(hits) < limit:
                    i = low.find(words[0])
                    s = max(0, i - 120)
                    snip = ("…" if s else "") + t["text"][s:s + 400] + ("…" if s + 400 < len(t["text"]) else "")
                    hits.append(f"«{res.get('title', '')}», {j.get('date', '')[:10]} — запись {job_id(d, j)} · "
                                f"{fmt_time(t['t'])}\n    {t['who']}: {snip}")
    if not hits:
        return "В разговорах не нашлось. Слова ищутся как есть — попробуйте другую форму слова или часть слова."
    return f"Найдено реплик: {total}" + (f", показаны первые {limit}" if total > limit else "") + "\n\n" + "\n".join(hits)


def read_record(a: dict) -> str:
    d, j, res = _job(a.get("record", ""))
    part = a.get("part") or "summary"
    lines = []
    if part in ("summary", "all"):
        jid = job_id(d, j)
        lines += [_head(d, j, res), f"Участники: {', '.join(res.get('participants') or []) or '—'}",
                  f"Открыть в Qamyal: {_link(jid)}"]
        for title, key in (("Кратко", "summary"), ("Решения", "decisions")):
            if res.get(key):
                lines += ["", title + ":"] + [f"- {x}" for x in res[key]]
        tasks = _tasks([(d, j, res)])
        if tasks:
            lines += ["", "Задачи:"] + [f"- {_task_line(t, with_record=False)}" for t in tasks]
        marks = j.get("marks") or res.get("marks") or []
        if marks:
            lines += ["", "Моменты, отмеченные во время записи:"] + \
                     [f"- {fmt_time(m['t'])}" + (f" — {m['note']}" if m.get("note") else "") for m in marks]
        total = sum(x["sec"] for x in res.get("stats") or []) or 1
        if res.get("stats"):
            lines += ["", "Кто сколько говорил: " + ", ".join(
                f"{x['who']} {_dur(x['sec'])} ({x['sec'] * 100 // total}%)" for x in res["stats"])]
        turns = res.get("turns") or []
        if part == "summary" and turns:
            lines += ["", f"Расшифровка: {len(turns)} реплик, {fmt_time(turns[0]['t'])}–{fmt_time(turns[-1]['t'])} — "
                          "read_record с part=transcript"]
    if part in ("transcript", "all"):
        try:
            a_from = parse_time(a["from"]) if a.get("from") else 0
            a_to = parse_time(a["to"]) if a.get("to") else None
        except ValueError:
            raise Problem("Время — в виде ММ:СС или Ч:ММ:СС") from None
        if part == "transcript":
            lines.append(_head(d, j, res))
        lines.append("")
        size = 0
        for t in res.get("turns") or []:
            if t["t"] < a_from or (a_to is not None and t["t"] > a_to):
                continue
            line = f"[{fmt_time(t['t'])}] {t['who']}: {t['text']}"
            if size + len(line) > CHUNK:
                lines.append(f"\n(дальше не поместилось — read_record с part=transcript, from={fmt_time(t['t'])})")
                break
            lines.append(line)
            size += len(line)
    return "\n".join(lines)


def list_tasks(a: dict) -> str:
    me = _low(load_settings()["user_name"])
    scope, state = a.get("scope") or "all", a.get("status") or "open"
    items = []
    for t in _tasks():
        mine = me and me in _low(t["who"])
        if scope == "mine" and not mine or scope == "owed" and (mine or not t["who"]):
            continue
        if state != "all" and t["done"] != (state == "done"):
            continue
        if a.get("project") and _low(a["project"]) not in _low(t["project"]):
            continue
        if a.get("person") and _low(a["person"]) not in _low(t["who"]):
            continue
        items.append(t)
    head = f"Пользователь: {load_settings()['user_name']}. Задач: {len(items)}"
    if not items:
        return head + "."
    items.sort(key=lambda t: -t["ts"])
    return head + "\n\n" + "\n".join(f"- {_task_line(t)}" for t in items[:int(a.get("limit") or 100)])


def mark_task(a: dict) -> str:
    key = (a.get("key") or "").strip()
    task = next((t for t in _tasks() if t["key"] == key), None)
    if not task:
        raise Problem(f"Задачи с ключом {key} нет — ключ берётся из list_tasks или read_record")
    done = read_json(TASKS_STATE, {}) or {}
    done[key] = bool(a.get("done", True))
    write_json(TASKS_STATE, done)
    task["done"] = done[key]
    return "Отмечено: " + _task_line(task)


def add_task(a: dict) -> str:
    import render
    d, j, res = _job(a.get("record", ""))
    what = (a.get("what") or "").strip()
    if not what:
        raise Problem("Нужно, что сделать (what)")
    t = {"who": (a.get("who") or "").strip(), "what": what, "when": (a.get("when") or "").strip(),
         "project": (a.get("project") or "").strip()}
    if a.get("t"):
        t["t"] = a["t"]
    backup(d, f"задача из Claude: {what[:60]}")
    res.setdefault("tasks", []).append(t)
    settle_tasks(res)                       # проект — один из проектов записи
    render.render(d, res)                   # итог.json, Word и HTML записи — с новой задачей
    jid = job_id(d, j)
    return "Добавлено в «{}»: {}".format(res.get("title", ""), _task_line(
        next(x for x in _tasks([(d, j, res)]) if x["key"] == task_key(jid, what)), with_record=False))


def list_projects(a: dict) -> str:
    stat = {}
    for d, j, res in _records():
        for p in projects_of(j) or projects_of(res):
            n, last = stat.get(p, (0, ""))
            stat[p] = (n + 1, last or j.get("date", "")[:10])
    lines = []
    for p in load_projects():
        n, last = stat.pop(p["name"], (0, ""))
        lines.append(f"- {p['name']}: записей {n}" + (f", последняя {last}" if last else "") +
                     (f"\n    {p['about']}" if p.get("about") else ""))
    lines += [f"- {p}: записей {n}, последняя {last}" for p, (n, last) in stat.items()]
    return "\n".join(lines) or "Проектов пока нет."


def my_speech(a: dict) -> str:
    import insights
    me = load_settings()["user_name"]
    r = insights.speech(me)
    if not r["all"]:
        return f"Нет записей, где узнан голос «{me}»."

    def line(x):
        fill = ", ".join(f"«{w}» {n}" for w, n in x["top_fillers"])
        return (f"записей {x['records']}, доля речи {x['share']}%, темп {x['wpm'] or '—'} сл/мин, "
                f"слов-паразитов {x['fillers_per100']} на 100 слов ({fill or 'нет'}), "
                f"перебивает {x['by_me_h']}/ч, перебивают пользователя {x['to_me_h']}/ч, "
                f"самый длинный монолог {fmt_time(x['longest'])}")
    lines = [f"Речь пользователя ({me}). Перебивания — примерно, по разметке голосов.", "", "Всего: " + line(r["all"]),
             "", "По неделям:"] + [f"- {w['label']}: {line(w)}" for w in r["weeks"][:8]]
    return "\n".join(lines)


def _schema(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
FILTERS = {
    "project": {**S, "description": "Проект (часть названия)"},
    "date_from": {**S, "description": "С какого дня: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ"},
    "date_to": {**S, "description": "По какой день включительно"},
}
READ = {"readOnlyHint": True, "openWorldHint": False}
TOOLS = [
    (find_records, "Список записей (созвонов, встреч): название, дата, участники, проекты, первая строка краткого "
                   "содержания. Без условий — самые новые. query ищет по названию, краткому содержанию и участникам.",
     _schema({"query": {**S, "description": "Слова для поиска"},
              "person": {**S, "description": "Участник (часть имени)"}, **FILTERS,
              "limit": {"type": "integer", "description": "Сколько записей (по умолчанию 30)"}}), READ),
    (search_transcripts, "Поиск по самим разговорам: реплики, где есть все слова, — с номером записи, временем и "
                         "говорящим. Ищет подстроку: «скважин» найдёт и «скважина», и «скважины».",
     _schema({"text": {**S, "description": "Слова, которые должны быть в реплике"},
              "person": {**S, "description": "Кто говорит (часть имени)"}, **FILTERS,
              "limit": {"type": "integer", "description": "Сколько реплик (по умолчанию 40)"}}, ["text"]), READ),
    (read_record, "Одна запись. part=summary — краткое содержание, решения, задачи (с ключами), кто сколько говорил; "
                  "part=transcript — расшифровка по репликам [ММ:СС] Кто: текст (from/to — кусок по времени); "
                  "part=all — всё вместе.",
     _schema({"record": {**S, "description": "Номер записи"},
              "part": {"type": "string", "enum": ["summary", "transcript", "all"]},
              "from": {**S, "description": "С какого времени, ММ:СС"},
              "to": {**S, "description": "По какое время, ММ:СС"}}, ["record"]), READ),
    (list_tasks, "Задачи из всех созвонов. scope: mine — пользователя, owed — что обещали ему другие, all — все. "
                 "status: open (по умолчанию), done, all. У каждой — сколько дней прошло, запись и ключ.",
     _schema({"scope": {"type": "string", "enum": ["mine", "owed", "all"]},
              "status": {"type": "string", "enum": ["open", "done", "all"]},
              "person": {**S, "description": "Исполнитель (часть имени)"},
              "project": {**S, "description": "Проект (часть названия)"},
              "limit": {"type": "integer"}}), READ),
    (mark_task, "Отметить задачу сделанной (done=true) или снова открытой (done=false). Только по просьбе пользователя.",
     _schema({"key": {**S, "description": "Ключ задачи из list_tasks"}, "done": {"type": "boolean"}}, ["key"]),
     {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    (add_task, "Добавить задачу к записи — она появится в Qamyal, в Word и HTML этой записи. Только по просьбе "
               "пользователя.",
     _schema({"record": {**S, "description": "Номер записи, к которой относится задача"},
              "what": {**S, "description": "Что сделать"},
              "who": {**S, "description": "Кто сделает (имя как в записи)"},
              "when": {**S, "description": "Срок, как его назвали"},
              "project": {**S, "description": "Один из проектов записи; пусто — главный"},
              "t": {**S, "description": "ММ:СС — где в разговоре об этом договорились"}}, ["record", "what"]),
     {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}),
    (list_projects, "Проекты: описание, сколько записей, дата последней.", _schema({}), READ),
    (my_speech, "Как говорит пользователь на созвонах: доля речи, темп, слова-паразиты, перебивания, самый длинный "
                "монолог — всего и по неделям. Считается на компьютере по разметке голосов.", _schema({}), READ),
]
BY_NAME = {fn.__name__: fn for fn, *_ in TOOLS}


# ---------- протокол ----------

def handle(msg: dict):
    method, mid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:                          # уведомление (initialized, cancelled…) — ответа не ждут
        return None
    if method == "initialize":
        ver = p.get("protocolVersion")
        return {"protocolVersion": ver if ver in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "qamyal", "title": "Qamyal", "version": "1.0"},
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": fn.__name__, "description": desc, "inputSchema": schema, "annotations": ann}
                          for fn, desc, schema, ann in TOOLS]}
    if method == "tools/call":
        fn = BY_NAME.get(p.get("name"))
        if not fn:
            raise LookupError(f"Нет инструмента {p.get('name')}")
        try:
            text, err = fn(p.get("arguments") or {}), False
        except Problem as e:
            text, err = str(e), True
        except Exception as e:  # noqa: BLE001 — сбой одного вызова не роняет сервер
            text, err = f"Ошибка Qamyal: {type(e).__name__}: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": err}
    raise LookupError(f"Метод {method} не поддерживается")


def send(obj: dict) -> None:
    OUT.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    OUT.flush()


def main() -> None:
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Не JSON"}})
            continue
        try:
            res = handle(msg)
            if res is not None:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": res})
        except LookupError as e:
            send({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": str(e)}})


if __name__ == "__main__":
    main()
