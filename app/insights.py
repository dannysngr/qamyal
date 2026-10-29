"""Обзор по всем записям: разбор своей речи (считается здесь, без ИИ), хронология решений проекта,
итог недели и предложения для словаря (их готовит редактор — Claude и т. п.)."""
import bisect
import hashlib
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import editor
from common import (JOBS, TASKS_STATE, job_id, list_jobs, projects_of, read_glossary, read_json, task_key,
                    write_json)

CACHE = JOBS / ".обзор"
SUGGEST = JOBS / ".словарь-предложения.json"
WEEKS_DIR = JOBS / "Итоги недели"


def _ts(j: dict) -> float:
    """Когда записан разговор; у файлов, добавленных без даты записи, — когда добавлен."""
    return j.get("ts") or j.get("added") or 0


def _done_jobs() -> list:
    """[(папка, job, итог)] готовых записей, от старых к новым."""
    out = []
    for d, j in list_jobs():
        if j.get("status") != "done":
            continue
        res = read_json(d / "черновики" / "итог.json")
        if res:
            out.append((d, j, res))
    out.sort(key=lambda x: _ts(x[1]))
    return out


def _cached(name: str, payload: str, fresh: bool, make, peek: bool = False):
    """Ответ ИИ по тем же данным — из кэша: повторно открыть страницу не стоит ещё одного запроса.
    peek — только посмотреть, готово ли (ИИ не звать): нет в кэше → None."""
    key = hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]
    path = CACHE / f"{name}-{key}.json"
    if not fresh and path.exists():
        return read_json(path)
    if peek:
        return None
    data = make()
    CACHE.mkdir(parents=True, exist_ok=True)
    write_json(path, data)
    return data


# ---------- разбор своей речи ----------

FILLERS = ["ну", "вот", "типа", "короче", "как бы", "это самое", "так сказать", "в общем", "в принципе",
           "значит", "собственно", "блин", "э", "ээ", "эээ", "эм", "мм"]
_speech_cache = {}


def _words(text: str) -> list:
    return [w for w in re.split(r"[^\wЁё\-]+", text.lower().replace("ё", "е")) if w]


def _fillers(words: list) -> dict:
    out = {}
    for i, w in enumerate(words):
        for f in FILLERS:
            parts = f.split()
            if words[i:i + len(parts)] == parts:
                out[f] = out.get(f, 0) + 1
    return out


def record_speech(d: Path, j: dict, res: dict, me: str):
    """Речь пользователя в одной записи: доля, темп, слова-паразиты, перебивания, самый длинный монолог.
    Кто где говорит — из разметки голосов, слова — из черновика GigaAM (в нём паразиты ещё не вычищены)."""
    drafts = d / "черновики"
    files = [drafts / n for n in ("голоса.json", "кто-есть-кто.json", "gigaam.json", "итог.json")]
    try:
        stamp = tuple(f.stat().st_mtime for f in files)
    except OSError:
        return None
    hit = _speech_cache.get(d)
    if hit and hit[0] == (stamp, me):
        return hit[1]
    dia = sorted(read_json(files[0], []) or [], key=lambda x: x["s"])
    names = read_json(files[1], {}) or {}
    mine = {int(k) for k, v in names.items() if v == me}
    out = None
    if dia and mine:
        my_sec = sum(x["e"] - x["s"] for x in dia if x["spk"] in mine)
        stats = res.get("stats") or []
        total = sum(x.get("sec", 0) for x in stats) or sum(x["e"] - x["s"] for x in dia)
        share_sec = next((x["sec"] for x in stats if x.get("who") == me), my_sec)
        # слова — к тому, чей отрезок голоса идёт в середине слова
        starts = [x["s"] for x in dia]
        my_words = []
        for seg in read_json(files[2], []) or []:
            for w in seg.get("words") or []:
                mid = (w["s"] + w["e"]) / 2
                k = bisect.bisect_right(starts, mid) - 1
                while k >= 0 and dia[k]["e"] < mid and starts[k] > mid - 30:
                    k -= 1
                if k >= 0 and dia[k]["s"] <= mid <= dia[k]["e"] and dia[k]["spk"] in mine:
                    my_words.append(w["w"])
        words = _words(" ".join(my_words))
        # перебивания: вступил, когда другой говорил ещё хотя бы секунду, и сам проговорил не меньше секунды
        by_me = to_me = 0
        for i, b in enumerate(dia):
            if b["e"] - b["s"] < 1.0:
                continue
            for a in dia[max(0, i - 30):i]:
                if (a["spk"] in mine) != (b["spk"] in mine) and a["e"] - b["s"] >= 1.0:
                    if b["spk"] in mine:
                        by_me += 1
                    else:
                        to_me += 1
                    break
        # самый длинный монолог: подряд свои отрезки, пока другой не заговорит дольше секунды
        longest, run = 0.0, None
        for x in dia:
            if x["spk"] in mine:
                run = [run[0] if run else x["s"], x["e"]]
                longest = max(longest, run[1] - run[0])
            elif x["e"] - x["s"] >= 1.0:
                run = None
        out = {"job": job_id(d, j), "title": res.get("title") or j.get("name", ""), "date": j.get("date", ""),
               "ts": _ts(j), "total": round(total), "mine": round(share_sec), "my_dia": round(my_sec, 1),
               "words": len(words), "fillers": _fillers(words), "by_me": by_me, "to_me": to_me,
               "longest": round(longest)}
    _speech_cache[d] = ((stamp, me), out)
    return out


def _monday(ts: float) -> datetime:
    day = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def week_label(monday: datetime) -> str:
    sunday = monday + timedelta(days=6)
    return f"{monday:%d.%m}–{sunday:%d.%m.%Y}"


def _sum_speech(items: list) -> dict:
    total = sum(x["total"] for x in items)
    mine = sum(x["mine"] for x in items)
    my_dia = sum(x["my_dia"] for x in items)
    words = sum(x["words"] for x in items)
    fillers = {}
    for x in items:
        for f, n in x["fillers"].items():
            fillers[f] = fillers.get(f, 0) + n
    hours = total / 3600 or 1
    return {"records": len(items), "total": total, "mine": mine,
            "share": round(100 * mine / total) if total else 0,
            "wpm": round(words / (my_dia / 60)) if my_dia >= 30 else None,
            "words": words,
            "fillers_per100": round(100 * sum(fillers.values()) / words, 1) if words else 0,
            "top_fillers": sorted(fillers.items(), key=lambda x: -x[1])[:4],
            "by_me_h": round(sum(x["by_me"] for x in items) / hours, 1),
            "to_me_h": round(sum(x["to_me"] for x in items) / hours, 1),
            "by_me": sum(x["by_me"] for x in items), "to_me": sum(x["to_me"] for x in items),
            "longest": max((x["longest"] for x in items), default=0)}


def speech(me: str) -> dict:
    recs = [r for r in (record_speech(d, j, res, me) for d, j, res in _done_jobs()) if r]
    weeks = {}
    for r in recs:
        weeks.setdefault(_monday(r["ts"]), []).append(r)
    return {"me": me, "all": _sum_speech(recs) if recs else None,
            "weeks": [{"label": week_label(m), **_sum_speech(items)}
                      for m, items in sorted(weeks.items(), reverse=True)],
            "records": sorted(recs, key=lambda x: -x["ts"])[:12]}


# ---------- хронология решений проекта ----------

TIMELINE_SYSTEM = """Ты ведёшь хронологию решений по одному проекту по кратким содержаниям рабочих созвонов. Пиши по-русски, коротко, только то, что есть в записях: ничего не додумывай и не советуй.

Из каждого созвона возьми решения и важные договорённости, которые относятся к проекту «{project}»: сроки, выбор вариантов, что отложили или отменили. Мелочи и поручения без решения пропускай. Если решение повторяется в нескольких созвонах без изменений — оставь его один раз, у первого созвона.
Сравни решения между созвонами: если более поздний созвон изменил или отменил решение, у раннего поставь status "изменено" или "отменено", в later — номер записи, где это случилось, в note — что именно поменялось, одной фразой. Иначе status "в силе", later и note пустые.

Ответь только JSON, без markdown:
{{"items": [{{"job": "номер записи", "what": "решение одной фразой", "status": "в силе", "later": "", "note": ""}}]}}
Порядок — от ранних созвонов к поздним."""


def project_names() -> list:
    count = {}
    for d, j, res in _done_jobs():
        for p in projects_of(j):
            count[p] = count.get(p, 0) + 1
    return [{"name": p, "count": n} for p, n in sorted(count.items(), key=lambda x: -x[1])]


def timeline(project: str, s: dict, fresh: bool = False, peek: bool = False) -> dict:
    recs = [(d, j, res) for d, j, res in _done_jobs() if project in projects_of(j)]
    if not recs:
        raise editor.EditorError("По этому проекту нет готовых записей")
    blocks, meta = [], {}
    for d, j, res in recs:
        jid = job_id(d, j)
        meta[jid] = {"title": res.get("title") or j.get("name", ""), "date": j.get("date", "")}
        dec = res.get("decisions") or []
        blocks.append(f"ЗАПИСЬ #{jid} · {j.get('date', '')} · «{meta[jid]['title']}»\n"
                      + "Решения:\n" + ("\n".join(f"- {x}" for x in dec) or "- (не записаны)") + "\n"
                      + "Кратко:\n" + "\n".join(f"- {x}" for x in res.get("summary") or []))
    prompt = "\n\n".join(blocks)

    def make():
        out = editor.ask_json(TIMELINE_SYSTEM.format(project=project), prompt, s)
        items = [x for x in out.get("items") or [] if isinstance(x, dict) and x.get("job") in meta and x.get("what")]
        return {"items": items, "at": time.time()}

    data = _cached("хронология", project + "\n" + prompt, fresh, make, peek) or {"items": None}
    return {**data, "records": meta, "project": project}


# ---------- итог недели ----------

WEEK_SYSTEM = """Ты готовишь итог рабочей недели по кратким содержаниям созвонов за эту неделю. Записи делает {user}. Пиши по-русски, деловым языком, коротко. Только то, что сказано и решено в созвонах: без своих советов, оценок и новых сроков.

Простой текст, без markdown и таблиц. Структура:
первая строка — «Итог недели {period}»;
«Созвоны» — список: дата, название;
дальше раздел на каждый проект (заголовок — название проекта): «Решения» по пунктам; «Сделано» — задачи с пометкой [сделано]; «Открыто» — задачи с пометкой [открыто]: кто — что — срок, если назван;
в конце — «Без проекта», если были такие созвоны. Пустые подразделы не пиши."""


def weeks() -> list:
    """Недели, в которые есть записи, — от новых к старым."""
    seen = {}
    for d, j, res in _done_jobs():
        m = _monday(_ts(j))
        seen[m] = seen.get(m, 0) + 1
    return [{"start": f"{m:%Y-%m-%d}", "label": week_label(m), "count": n}
            for m, n in sorted(seen.items(), reverse=True)]


def week_digest(start: str, s: dict, fresh: bool = False, peek: bool = False) -> dict:
    monday = datetime.strptime(start, "%Y-%m-%d")
    end = monday + timedelta(days=7)
    done_map = read_json(TASKS_STATE, {}) or {}
    blocks = []
    for d, j, res in _done_jobs():
        if not monday <= datetime.fromtimestamp(_ts(j)) < end:
            continue
        jid = job_id(d, j)
        tasks = []
        for t in res.get("tasks") or []:
            t = t if isinstance(t, dict) else {"what": str(t)}
            mark = "сделано" if done_map.get(task_key(jid, t.get("what", ""))) else "открыто"
            tasks.append(f"- [{mark}] {t.get('who') or 'не назначено'} — {t.get('what', '')}"
                         + (f" — срок: {t['when']}" if t.get("when") else "")
                         + (f" (проект: {t['project']})" if t.get("project") else ""))
        blocks.append(f"СОЗВОН {j.get('date', '')} · «{res.get('title') or j.get('name', '')}»"
                      f" · проекты: {', '.join(projects_of(j)) or 'нет'}\n"
                      + "Кратко:\n" + "\n".join(f"- {x}" for x in res.get("summary") or []) + "\n"
                      + "Решения:\n" + ("\n".join(f"- {x}" for x in res.get("decisions") or []) or "- нет") + "\n"
                      + "Задачи:\n" + ("\n".join(tasks) or "- нет"))
    if not blocks:
        raise editor.EditorError("За эту неделю нет готовых записей")
    period = week_label(monday)
    prompt = "\n\n".join(blocks)
    system = WEEK_SYSTEM.format(user=s.get("user_name", "Дэни"), period=period)
    data = _cached("неделя", system + prompt, fresh, lambda: {"text": editor.ask_text(system, prompt, s)}, peek)
    return {**(data or {"text": None}), "label": period}


def week_word(start: str, text: str) -> Path:
    from docx import Document
    monday = datetime.strptime(start, "%Y-%m-%d")
    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    path = WEEKS_DIR / f"Итог недели {monday:%Y-%m-%d}.docx"
    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    doc.save(str(path))
    return path


# ---------- предложения для словаря ----------

TERMS_SYSTEM = """Ты пополняешь словарь программы расшифровки рабочих разговоров. По словарю редактор правильно пишет термины, названия и имена, которые модель распознавания речи (GigaAM) слышит с ошибками.

Тебе дают текущий СЛОВАРЬ и КАНДИДАТОВ: слово из готовой расшифровки, сколько раз встретилось, кусок готового текста и как то же место записал GigaAM.
Выбери тех, кого стоит добавить: специальные термины, названия программ и компаний, аббревиатуры, редкие имена и фамилии — в первую очередь те, что GigaAM пишет неправильно. Обычные слова, общеизвестные названия и то, что уже есть в словаре, пропускай. Если в готовом тексте у слова стоит (?), а по черновику видно, как правильно, — предложи правильное написание.
Для каждого: term — как правильно писать; note — что это, 2–6 слов, только если ясно из контекста, иначе пусто; errors — как это место записал GigaAM, до 3 вариантов, только реально встреченные в черновиках (пусто, если GigaAM записал верно).

Ответь только JSON, без markdown: {"items": [{"term": "JewelSuite", "note": "программа геомоделирования", "errors": ["джувел сьют"]}]}
Не больше 15 предложений: лучше меньше, но полезных."""


def _known_terms() -> set:
    known = set()
    for line in read_glossary().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s+[—–-]\s+", line, maxsplit=1)
        known.add(re.sub(r"\s*\(.*?\)\s*", " ", parts[0]).strip().lower())
        for w in (parts[1].split(",") if len(parts) > 1 else []):
            known.add(w.strip().lower())
    return {k for k in known if k}


def _is_known(term: str, known: set) -> bool:
    t = term.lower()
    return t in known or any(t in k.split() or k in t.split() for k in known if " " in k or " " in t)


def term_candidates(limit: int = 60) -> list:
    known = _known_terms()
    found = {}
    for d, j, res in _done_jobs()[-20:]:
        people = {t.get("who", "") for t in res.get("turns") or []}
        segs = read_json(d / "черновики" / "gigaam.json", []) or []
        turns = res.get("turns") or []
        for i, t in enumerate(turns):
            text = t.get("text", "")
            cands = set(re.findall(r"[A-Za-z][A-Za-z0-9\-]+(?:\s+[A-Z][A-Za-z0-9\-]+)*", text))
            unsure = set(re.findall(r"([\wЁё\-]+)\s*\(\?\)", text))
            for sent in re.split(r"(?<=[.!?…])\s+", text):
                cands |= set(re.findall(r"(?<=\s)[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁа-яё]+)?", sent))
            for c in cands | unsure:
                if c in people or _is_known(c, known) or len(c) < 3:
                    continue
                x = found.setdefault(c, {"n": 0, "unsure": False, "ctx": None})
                x["n"] += 1
                x["unsure"] = x["unsure"] or c in unsure
                if x["ctx"] is None or (c in unsure and "(?)" not in x["ctx"][0]):
                    a = max(0, text.find(c) - 80)
                    t0 = t.get("t", 0)
                    t1 = turns[i + 1]["t"] if i + 1 < len(turns) else t0 + 30
                    heard = " ".join(sg.get("text", "") for sg in segs if sg["e"] >= t0 - 1 and sg["s"] <= t1 + 1)
                    x["ctx"] = (text[a:a + 200], heard[:260])
    ranked = sorted(found.items(), key=lambda kv: (not kv[1]["unsure"], -kv[1]["n"]))[:limit]
    return [{"term": k, **v} for k, v in ranked if v["unsure"] or v["n"] >= 2]


def suggestions() -> dict:
    data = read_json(SUGGEST, {}) or {}
    known = _known_terms()
    items = [x for x in data.get("items") or [] if not _is_known(x["term"], known)]
    return {"items": items, "at": data.get("at")}


def find_terms(s: dict) -> dict:
    cands = term_candidates()
    data = read_json(SUGGEST, {}) or {}
    dismissed = set(data.get("dismissed") or [])
    cands = [c for c in cands if c["term"].lower() not in dismissed]
    items = []
    if cands:
        lines = [f"«{c['term']}» — {c['n']} раз(а)\n  в тексте: …{c['ctx'][0]}…\n  GigaAM: …{c['ctx'][1]}…"
                 for c in cands]
        prompt = f"СЛОВАРЬ:\n{read_glossary().strip() or '(пусто)'}\n\nКАНДИДАТЫ:\n" + "\n".join(lines)
        out = editor.ask_json(TERMS_SYSTEM, prompt, s)
        for x in out.get("items") or []:
            term = str(x.get("term") or "").strip()
            if term and term.lower() not in dismissed:
                items.append({"term": term, "note": str(x.get("note") or "").strip(),
                              "errors": [str(e).strip() for e in x.get("errors") or [] if str(e).strip()][:3]})
    write_json(SUGGEST, {"items": items, "at": time.time(), "dismissed": sorted(dismissed)})
    return suggestions()


def dismiss_term(term: str) -> dict:
    data = read_json(SUGGEST, {}) or {}
    data["dismissed"] = sorted(set(data.get("dismissed") or []) | {term.lower()})
    data["items"] = [x for x in data.get("items") or [] if x["term"].lower() != term.lower()]
    write_json(SUGGEST, data)
    return suggestions()
