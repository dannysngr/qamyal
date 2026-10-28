"""Узнавание людей по голосу: отпечатки голосов участников и их поиск в новых записях."""
import json
import time
from pathlib import Path

import numpy as np

from common import MODELS, ROOT

VOICES = ROOT / "Голоса" / "voices.json"
MEAN = Path(__file__).with_name("voice_mean.npy")   # «средний голос» — вычитается перед сравнением
SR = 16000
MATCH = 0.30          # сходство (после вычитания среднего), начиная с которого голос узнан
SAME = 0.50           # куски разметки с таким сходством между собой — один и тот же человек
MARGIN = 0.10         # насколько лучший кандидат должен опережать второго
_extractor = None
_mean = None


def center(v) -> np.ndarray:
    """Без вычитания «среднего голоса» все голоса похожи друг на друга на 0.8–0.95."""
    global _mean
    if _mean is None:
        _mean = np.load(MEAN) if MEAN.exists() else np.zeros(256, dtype=np.float32)
    v = np.asarray(v, dtype=np.float32) - _mean
    return v / (np.linalg.norm(v) + 1e-9)


def extractor(threads: int = 2):
    global _extractor
    if _extractor is None:
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(MODELS / "diar" / "wespeaker_en_voxceleb_resnet34_LM.onnx"), num_threads=threads)
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    return _extractor


def embed(audio: np.ndarray) -> np.ndarray:
    ex = extractor()
    st = ex.create_stream()
    st.accept_waveform(SR, audio)
    st.input_finished()
    v = np.array(ex.compute(st), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def cluster_embeddings(audio: np.ndarray, diar: list, max_sec: float = 90.0) -> dict:
    """Отпечаток голоса каждого «спикера» разметки: среднее по его самым длинным кускам."""
    out = {}
    for spk in sorted({d["spk"] for d in diar}):
        segs = sorted((d for d in diar if d["spk"] == spk and d["e"] - d["s"] >= 1.5),
                      key=lambda d: d["e"] - d["s"], reverse=True)
        vecs, total = [], 0.0
        for d in segs:
            if total >= max_sec:
                break
            s, e = d["s"], min(d["e"], d["s"] + 15)
            vecs.append(embed(audio[int(s * SR):int(e * SR)]) * (e - s))
            total += e - s
        if vecs:
            v = np.sum(vecs, axis=0)
            out[spk] = {"emb": (v / (np.linalg.norm(v) + 1e-9)).tolist(), "sec": round(total, 1),
                        "talk": round(sum(d["e"] - d["s"] for d in diar if d["spk"] == spk), 1)}
    return out


# ---------- база голосов ----------

def load_db() -> dict:
    try:
        return json.loads(VOICES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_db(db: dict) -> None:
    VOICES.parent.mkdir(exist_ok=True)
    tmp = VOICES.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
    tmp.replace(VOICES)


def profile(entry: dict) -> np.ndarray:
    """Голос человека = взвешенное среднее отпечатков из всех записей, где он узнан."""
    # образцы, подтверждённые вручную, весят втрое больше автоматических
    v = np.sum([np.array(j["emb"]) * j["sec"] * (3 if j.get("manual") else 1) for j in entry["jobs"].values()], axis=0)
    return center(v / (np.linalg.norm(v) + 1e-9))


def match(clusters: dict, labels: dict, db: dict = None, exclusive: bool = False,
          exclude: set = frozenset()) -> dict:
    """«Спикер N» → {name, score} для голосов, похожих на известных людей.
    exclusive — число людей известно и голоса уже разделены по людям: каждый голос — отдельный человек,
    распределяем один к одному по наибольшему сходству (без запаса на сомнение)."""
    db = load_db() if db is None else db
    if not db or not clusters:
        return {}
    profs = {name: profile(e) for name, e in db.items() if e.get("jobs") and name not in exclude}
    if exclusive:
        pairs = sorted(((float(center(c["emb"]) @ p), int(spk), name)
                        for spk, c in clusters.items() for name, p in profs.items()), reverse=True)
        res, used_s, used_n = {}, set(), set()
        for score, spk, name in pairs:
            if score < MATCH or spk in used_s or name in used_n:
                continue
            res[labels.get(spk, f"Спикер {spk}")] = {"name": name, "score": round(score, 2), "spk": spk}
            used_s.add(spk)
            used_n.add(name)
        return res
    pairs = []
    for spk, c in clusters.items():
        v = center(c["emb"])
        scores = sorted(((float(v @ p), name) for name, p in profs.items()), reverse=True)
        best, name = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        need = MATCH if c.get("talk", 0) >= 20 else MATCH + 0.10   # по короткому куску отпечаток шумный
        if best >= need and best - second >= MARGIN:
            pairs.append((best, int(spk), name))
    # один человек может оказаться в нескольких кусках разметки — каждому куску его имя
    return {labels.get(spk, f"Спикер {spk}"): {"name": name, "score": round(score, 2), "spk": spk}
            for score, spk, name in pairs}


def merge_similar(diar: list, clusters: dict, keep: set = frozenset(), same: float = SAME,
                  target: int = 0):
    """Разметка дробит одного человека на несколько «спикеров» — склеиваем куски с похожим голосом
    (средняя связь: сравниваем голос группы целиком, а не отдельные куски).
    target > 0 — пользователь знает, сколько было людей: клеим, пока групп не станет столько."""
    groups = {int(k): {"vec": center(c["emb"]) * c["talk"], "raw": np.array(c["emb"]) * c["talk"],
                       "talk": c["talk"], "sec": c["sec"]} for k, c in clusters.items() if int(k) not in keep}
    remap = {}
    while len(groups) > 1:
        best, pair = -1.0, None
        keys = sorted(groups)
        for i, a in enumerate(keys):
            va = groups[a]["vec"] / (np.linalg.norm(groups[a]["vec"]) + 1e-9)
            for b in keys[i + 1:]:
                vb = groups[b]["vec"] / (np.linalg.norm(groups[b]["vec"]) + 1e-9)
                sim = float(va @ vb)
                if sim > best:
                    best, pair = sim, (a, b)
        if target and len(groups) <= target:
            break                               # людей столько, сколько сказал пользователь
        if best < same and not target:
            break
        a, b = pair if groups[pair[0]]["talk"] >= groups[pair[1]]["talk"] else pair[::-1]
        for k in ("vec", "raw", "talk", "sec"):
            groups[a][k] = groups[a][k] + groups[b][k]
        del groups[b]
        remap[b] = a
        for k, v in list(remap.items()):
            if v == b:
                remap[k] = a
    if not remap:
        return diar, clusters
    new = []
    for d in diar:
        d = {**d, "spk": remap.get(d["spk"], d["spk"])}
        if new and new[-1]["spk"] == d["spk"] and d["s"] - new[-1]["e"] < 0.5:
            new[-1]["e"] = max(new[-1]["e"], d["e"])
        else:
            new.append(d)
    out = {k: v for k, v in clusters.items() if int(k) in keep}
    for k, g in groups.items():
        raw = g["raw"] / (np.linalg.norm(g["raw"]) + 1e-9)
        out[str(k)] = {"emb": raw.tolist(), "sec": round(g["sec"], 1), "talk": round(g["talk"], 1)}
    return new, out


def who_is_who(diar: list, turns: list) -> dict:
    """По итоговым репликам (с именами) решаем, кто есть кто в разметке голосов: номер → имя."""
    votes = {}
    ends = [t["t"] for t in turns[1:]] + [1e9]
    for t, end in zip(turns, ends):
        name = t["who"]
        if name.startswith("Спикер"):
            continue
        for d in diar:
            ov = min(end, d["e"]) - max(t["t"], d["s"])
            if ov > 0:
                votes.setdefault(d["spk"], {}).setdefault(name, 0.0)
                votes[d["spk"]][name] += ov
    out = {}
    for spk, v in votes.items():
        name, sec = max(v.items(), key=lambda x: x[1])
        if sec >= 0.6 * sum(v.values()) and sec >= 5:
            out[spk] = name
    return out


def full_mapping(diar: list, turns: list, found: dict) -> dict:
    """Кто есть кто: по итоговым репликам, а для голосов, у которых реплик мало, — по узнаванию голоса."""
    m = who_is_who(diar, turns)
    names = {t["who"] for t in turns}
    for v in (found or {}).values():
        if v["spk"] not in m and v["name"] in names:
            m[v["spk"]] = v["name"]
    return m


def longest_clip(diar: list, spk: int, max_len: float = 8.0) -> list:
    segs = [d for d in diar if d["spk"] == spk]
    if not segs:
        return []
    d = max(segs, key=lambda x: x["e"] - x["s"])
    return [round(d["s"], 1), round(min(d["e"], d["s"] + max_len), 1)]


def learn(job_id: str, title: str, clusters: dict, mapping: dict, diar: list = None) -> list:
    """Запоминаем голоса из записи. Повторное обучение на той же записи заменяет её вклад
    (образцы, подтверждённые вручную, — ключи «запись#…» — не трогаем; убранные вручную — не берём снова)."""
    db = load_db()
    for e in db.values():
        e["jobs"].pop(job_id, None)
    learned = []
    by_name = {}
    for spk, name in mapping.items():
        c = clusters.get(str(spk)) or clusters.get(spk)
        if c and c["sec"] >= 10:
            by_name.setdefault(name, []).append((int(spk), c))
    for name, cs in by_name.items():
        spk, best = max(cs, key=lambda x: x[1]["sec"])   # если человек раздроблен — берём основной кусок
        e = db.setdefault(name, {"jobs": {}})
        if job_id in e.get("blocked", []):
            continue
        e["jobs"][job_id] = {"emb": best["emb"], "sec": best["sec"], "title": title, "at": time.time(),
                             "spk": spk, "clip": longest_clip(diar or [], spk)}
        learned.append(name)
    db = {k: v for k, v in db.items() if v["jobs"] or v.get("blocked")}
    save_db(db)
    return learned


def add_sample(name: str, job_id: str, title: str, audio: np.ndarray, start: float) -> bool:
    """Подтверждённый образец: пользователь сам сказал, кто говорит в этом куске."""
    if len(audio) < SR * 2:
        return False
    v = embed(audio[: SR * 30])
    db = load_db()
    e = db.setdefault(name, {"jobs": {}})
    sec = round(min(len(audio), SR * 30) / SR, 1)
    e["jobs"][f"{job_id}#{int(start)}"] = {"emb": v.tolist(), "sec": sec, "title": title, "at": time.time(),
                                           "manual": True, "clip": [round(start, 1), round(start + min(sec, 8), 1)]}
    save_db(db)
    return True


def move(name: str, key: str, to: str, audio_of_clip) -> bool:
    """Образец голоса оказался чужим: убираем у name и отдаём человеку to как подтверждённый
    (отпечаток — именно по тому куску, который пользователь прослушал)."""
    db = load_db()
    x = (db.get(name) or {}).get("jobs", {}).get(key)
    if not x or not x.get("clip"):
        return False
    s, e = x["clip"]
    jid = key.split("#")[0]
    forget(name, key)
    return add_sample(to, jid, x.get("title", ""), audio_of_clip(jid, s, e - s), s)


def forget(name: str, key: str) -> None:
    """Убрать образец (чужой голос) и не брать его снова при пересведении записи."""
    db = load_db()
    e = db.get(name)
    if not e:
        return
    e["jobs"].pop(key, None)
    jid = key.split("#")[0]
    if "#" not in key:
        e.setdefault("blocked", []).append(jid)
    if not e["jobs"] and not e.get("blocked"):
        db.pop(name)
    save_db(db)


def talk_stats(diar: list, mapping: dict, labels: dict) -> list:
    tot = {}
    for d in diar:
        name = mapping.get(d["spk"]) or labels.get(d["spk"], "?")
        tot[name] = tot.get(name, 0) + d["e"] - d["s"]
    return [{"who": k, "sec": round(v)} for k, v in sorted(tot.items(), key=lambda x: -x[1])]


def merge_small(diar: list, clusters: dict, keep: set = frozenset(), min_sec: float = 8.0,
                min_share: float = 0.05, target: int = 0):
    """Мелкие «спикеры» (обрывки по несколько секунд) приклеиваем к самому похожему крупному голосу.
    keep — номера, которые не трогаем (в созвоне микрофон хозяина записи) и к которым не клеим чужих.
    target > 0 — число людей известно: оставляем столько самых разговорчивых, даже если кто-то говорил мало."""
    talk = {}
    for d in diar:
        talk[d["spk"]] = talk.get(d["spk"], 0) + d["e"] - d["s"]
    total = sum(v for k, v in talk.items() if k not in keep) or 1
    if target:
        big = sorted((k for k in talk if k not in keep), key=lambda k: -talk[k])[:target]
    else:
        big = [k for k, v in talk.items() if k not in keep and (v >= min_sec or v >= min_share * total)]
    if not big:
        big = [max((k for k in talk if k not in keep), key=lambda k: talk[k], default=None)]
        big = [b for b in big if b is not None]
    remap = {}
    for spk in talk:
        if spk in keep or spk in big:
            continue
        c = clusters.get(str(spk)) or clusters.get(spk)
        if c and big and all(str(b) in clusters or b in clusters for b in big):
            v = center(c["emb"])
            remap[spk] = max(big, key=lambda b: float(v @ center((clusters.get(str(b)) or clusters[b])["emb"])))
        elif big:
            # без отпечатка (одни короткие куски) — к тому, кто говорил рядом по времени
            own = [d for d in diar if d["spk"] == spk]
            mid = (own[0]["s"] + own[-1]["e"]) / 2
            near = min((d for d in diar if d["spk"] in big), key=lambda d: abs((d["s"] + d["e"]) / 2 - mid))
            remap[spk] = near["spk"]
    if not remap:
        return diar, clusters
    new = [{**d, "spk": remap.get(d["spk"], d["spk"])} for d in diar]
    merged = []
    for d in new:
        if merged and merged[-1]["spk"] == d["spk"] and d["s"] - merged[-1]["e"] < 0.5:
            merged[-1]["e"] = max(merged[-1]["e"], d["e"])
        else:
            merged.append(dict(d))
    kept = {k: v for k, v in clusters.items() if int(k) not in remap}
    for k, v in kept.items():
        v["talk"] = round(sum(d["e"] - d["s"] for d in merged if d["spk"] == int(k)), 1)
    return merged, kept


def describe(matches: dict) -> str:
    out = []
    for lab, m in sorted(matches.items()):
        if m.get("how") == "mic":
            out.append(f"{lab} — это {m['name']}: его голос записан микрофоном этого компьютера (надёжно)")
        elif m.get("how") == "skype":
            out.append(f"{lab} — это {m['name']}: так показал Skype — кто говорил в звонке (надёжно; "
                       f"короткие реплики на стыке двух говорящих могут быть приписаны соседу)")
        elif m["score"] >= 0.5:
            out.append(f"{lab} — по голосу {m['name']}, почти наверняка (сходство {m['score']:.2f}); "
                       f"упоминание этого имени в третьем лице этому не противоречит — так бывает и при оговорке")
        else:
            out.append(f"{lab} — по голосу {m['name']} (сходство {m['score']:.2f})")
    return "\n".join(out)
