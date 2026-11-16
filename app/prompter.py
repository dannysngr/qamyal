"""Суфлёр: во время записи ловит вопросы в живой расшифровке и сразу подсказывает, что ответить.

Как работает (отдельный поток рядом с живой расшифровкой):
1. Когда набралось ~минута разговора, Claude по тексту и списку проектов решает, о каком проекте речь.
   Потом раз в несколько минут проверяет ещё раз — тема могла смениться.
2. Справку по проекту программа собирает сама из готовых записей (краткое содержание, решения,
   открытые задачи) — без Claude, мгновенно.
3. Каждая фраза с «?» — вопрос. Claude отвечает по справке и последним минутам разговора;
   если там ответа нет — ищет в записях через MCP Qamyal (только он, без остальных подключений:
   иначе каждый вызов тащит за собой ~100 тысяч токенов чужих инструментов).
   Вопросы без смысла для подсказки («как слышно?») Claude пропускает.
Одновременно идёт только один вызов: вопросы, пришедшие за это время, уходят следующим вызовом вместе."""
import json
import re
import subprocess
import threading
import time

from common import (APP, TASKS_STATE, find_claude, job_id, list_jobs, load_settings, projects_block,
                    projects_of, read_json, task_key)
from sysdeps import HIDDEN

PROJECT_AFTER = 400        # столько знаков разговора нужно, чтобы угадывать проект
PROJECT_EVERY = 300        # с — так часто перепроверяем проект
CONTEXT_SEC = 240          # столько последних секунд разговора видит Claude при ответе
BRIEF_RECORDS = 8          # столько последних записей проекта идёт в справку
BRIEF_MAX = 14000          # знаков справки — больше не нужно для подсказки
MIN_QUESTION = 12          # короче — не вопрос по делу («Да?», «Слышно?»)
TOOLS = ["mcp__qamyal__search_transcripts", "mcp__qamyal__read_record", "mcp__qamyal__find_records",
         "mcp__qamyal__list_tasks"]

PROJECT_SYSTEM = """По началу рабочего разговора определи, о каком проекте из списка идёт речь.
Если разговор явно не о проектах из списка или понять пока нельзя — пустая строка.
Ответь только JSON, без пояснений: {"project": "название из списка или пустая строка"}"""

ANSWER_SYSTEM = """Ты — суфлёр на рабочем созвоне. Пользователь (его зовут {user}) идёт по разговору, а ты по ходу подсказываешь ему ответы на прозвучавшие вопросы — как шпаргалка, которую он читает за пару секунд.

На вход: СПРАВКА по проекту (прошлые встречи: краткое содержание, решения, открытые задачи), последние минуты РАЗГОВОРА (автоматическая живая расшифровка — без имён говорящих, с ошибками распознавания) и ВОПРОСЫ из этих минут.

Для каждого вопроса реши, нужна ли подсказка. Не нужна: бытовые и технические вопросы («как слышно?», «видно экран?»), риторические, вопросы, на которые в разговоре уже ответили, и вопросы, на которые ни справка, ни записи ничего не дают.
Если в справке ответа нет, а он мог прозвучать на прошлых встречах, — поищи инструментами qamyal (search_transcripts, потом read_record). Ищи быстро: подсказка нужна, пока разговор не ушёл дальше.

Подсказка — 1–3 коротких предложения по существу: факт, цифра, решение, кто за что отвечает, срок. Если опираешься на прошлую встречу — в конце в скобках её название и дату. Только то, что есть в справке, записях или разговоре; ничего не выдумывай и не предлагай от себя.

Ответь только JSON, без пояснений и без markdown:
{{"hints": [{{"q": "вопрос коротко, как прозвучал", "a": "подсказка"}}]}} — пустой список, если подсказки не нужны."""


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


def questions(text: str) -> list:
    """Вопросы из фразы живой расшифровки: предложения, которые кончаются «?»."""
    out = []
    for q in re.findall(r"[^.!?…]*\?", text or ""):
        q = q.strip(" ,—-")
        if len(q) >= MIN_QUESTION:
            out.append(q)
    return out


def brief(project: str) -> str:
    """Справка по проекту из готовых записей: новые сверху, с открытыми задачами."""
    low = project.lower()
    done = read_json(TASKS_STATE, {}) or {}
    parts, tasks = [], []
    for d, j in list_jobs():
        if j.get("status") != "done":
            continue
        res = read_json(d / "черновики" / "итог.json")
        if not res or not any(low == p.lower() for p in projects_of(j) or projects_of(res)):
            continue
        jid = job_id(d, j)
        for t in res.get("tasks") or []:
            t = t if isinstance(t, dict) else {"what": str(t)}
            if not done.get(task_key(jid, t.get("what", ""))):
                tasks.append(f"- {t.get('who') + ': ' if t.get('who') else ''}{t.get('what', '')}"
                             f"{' (срок: ' + t['when'] + ')' if t.get('when') else ''} — «{res.get('title', '')}»")
        if len(parts) < BRIEF_RECORDS:
            head = f"«{res.get('title') or j.get('name', '')}», {str(j.get('date', ''))[:10]}"
            body = "\n".join(f"- {x}" for x in res.get("summary") or [])
            dec = "\n".join(f"- {x}" for x in res.get("decisions") or [])
            parts.append(f"### {head}\n{body}" + (f"\nРешили:\n{dec}" if dec else ""))
    text = "\n\n".join(parts) or "(по этому проекту готовых записей пока нет)"
    if tasks:
        text += "\n\n### Открытые задачи проекта\n" + "\n".join(tasks[:40])
    return text[:BRIEF_MAX]


def _mcp_config() -> str:
    """Только MCP Qamyal — файл рядом с папкой редактора (её же Claude видит как рабочую)."""
    import claude_link
    cfg = APP / "editor_cwd" / "suflyor-mcp.json"
    cfg.parent.mkdir(exist_ok=True)
    data = json.dumps({"mcpServers": {"qamyal": {"type": "stdio", **claude_link.entry()}}}, ensure_ascii=False)
    if not cfg.exists() or cfg.read_text(encoding="utf-8") != data:
        cfg.write_text(data, encoding="utf-8")
    return str(cfg)


def ask_claude(system: str, prompt: str, tools: bool = False, timeout: int = 120) -> str:
    s = load_settings()
    exe = find_claude(s)
    if not exe:
        raise RuntimeError("нет Claude Code")
    cmd = [exe, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
           "--strict-mcp-config", "--system-prompt", system]
    if tools:
        cmd += ["--mcp-config", _mcp_config(), "--allowedTools", ",".join(TOOLS)]
    if s.get("claude_model"):
        cmd += ["--model", s["claude_model"]]
    p = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, cwd=APP / "editor_cwd",
                       timeout=timeout, **HIDDEN)
    try:
        res = json.loads(p.stdout.decode("utf-8", "replace"))
    except ValueError:
        raise RuntimeError((p.stderr.decode("utf-8", "replace") or "Claude не ответил").strip()[:200])
    if res.get("is_error"):
        raise RuntimeError(str(res.get("result"))[:200])
    return res.get("result") or ""


def available() -> bool:
    s = load_settings()
    return bool(s.get("prompter", True)) and s.get("editor") == "claude" and bool(find_claude(s))


class Prompter:
    def __init__(self, live, name: str = ""):
        self.live, self.name = live, name
        self.hints = []                 # [{"t": сек записи, "q": вопрос, "a": подсказка, "project": …}]
        self.project, self._brief = "", ""
        self.busy = ""                  # что суфлёр делает сейчас — для окна
        self.error = ""
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self) -> list:
        self.running = False
        return self.hints

    def status(self) -> dict:
        return {"project": self.project, "busy": self.busy, "error": self.error, "hints": self.hints}

    # ---------- работа ----------

    def _loop(self):
        seen, checked, waiting = 0, 0.0, []
        while self.running:
            time.sleep(2)
            segs = list(self.live.segments)
            for s in segs[seen:]:
                waiting += [(s["t"], q) for q in questions(s["text"])]
            seen = len(segs)
            size = sum(len(s["text"]) for s in segs)
            try:
                if size >= PROJECT_AFTER and time.time() - checked > (PROJECT_EVERY if self.project else 60):
                    checked = time.time()
                    self._detect(segs)
                if waiting and self.running:
                    batch, waiting = waiting[-4:], []        # отстали — важнее последние вопросы
                    self._answer(segs, batch)
                self.error = ""
            except Exception as e:  # noqa: BLE001 — подсказка необязательна, запись идёт дальше
                self.error = f"Суфлёр: {str(e)[:160]}"
            self.busy = ""

    def _detect(self, segs: list) -> None:
        self.busy = "Определяю проект…"
        talk = " ".join(s["text"] for s in segs)[-5000:]
        name = f"Название записи: {self.name}\n\n" if self.name else ""
        r = _json(ask_claude(PROJECT_SYSTEM, f"ПРОЕКТЫ:\n{projects_block()}\n\n{name}РАЗГОВОР:\n{talk}"))
        p = str(r.get("project") or "").strip()
        if p and p != self.project:
            self.project, self._brief = p, brief(p)

    def _answer(self, segs: list, batch: list) -> None:
        self.busy = "Готовлю подсказку…"
        last = segs[-1]["t"] if segs else 0
        talk = "\n".join(f"[{int(s['t'] // 60):02d}:{int(s['t'] % 60):02d}] {s['text']}"
                         for s in segs if s["t"] >= last - CONTEXT_SEC)
        qs = "\n".join(f"- {q}" for _, q in batch)
        head = f"ПРОЕКТ: {self.project}\n\nСПРАВКА:\n{self._brief}" if self.project else \
            "ПРОЕКТ: пока не ясен — справки нет; ищи в записях, если вопрос о прошлых договорённостях."
        prompt = f"{head}\n\nРАЗГОВОР (последние минуты):\n{talk}\n\nВОПРОСЫ:\n{qs}"
        system = ANSWER_SYSTEM.format(user=load_settings().get("user_name") or "пользователь")
        r = _json(ask_claude(system, prompt, tools=True))
        t = batch[-1][0]
        for h in r.get("hints") or []:
            if isinstance(h, dict) and str(h.get("a") or "").strip():
                self.hints.append({"t": t, "q": str(h.get("q") or "").strip(), "a": str(h["a"]).strip(),
                                   "project": self.project, "at": time.time()})
