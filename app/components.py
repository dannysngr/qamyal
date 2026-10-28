"""Что программа докачивает по кнопке в Настройках: распознавание речи, Whisper, Claude Code.
Установщик для Mac ставит только саму программу; тяжёлое (библиотеки ≈ 230 МБ, модели от 0,6 до 3,6 ГБ)
пользователь скачивает сам, когда захочет. Скачивание идёт отдельным процессом
(python components.py <id> [вариант Whisper]) и переживает закрытие окна; ход — в Записи/.components.json.
Оборванная загрузка продолжается с того же места."""
import importlib.util
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from common import JOBS, MODELS, ROOT, find_claude, load_settings, read_json, save_settings, write_json
from sysdeps import IS_WIN, find_program, gui_python, machine_info, open_console, pid_alive, spawn_detached

STATUS = JOBS / ".components.json"
GIGAAM_URL = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM"
GIGAAM_MD5 = "2730de7545ac43ad256485a462b0a27a"          # v3_e2e_rnnt, как в gigaam._MODEL_HASHES
SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SPEECH_MODULES = ["torch", "torchaudio", "gigaam", "faster_whisper", "sherpa_onnx", "sentencepiece"]

# parts — что именно скачивается: пользователь должен видеть, что ставит к себе (размеры — сколько качается)
INFO = {
    "speech": {"title": "Распознавание речи и голосов", "size": "≈ 720 МБ", "need": True,
               "about": "Без него записи только сохраняются, но не расшифровываются.",
               "parts": [
                   {"name": "GigaAM v3", "what": "модель распознавания русской речи от Сбера (SaluteDevices)",
                    "size": "449 МБ", "from": "cdn.chatwm.opensmodel.sberdevices.ru"},
                   {"name": "pyannote segmentation 3.0 и WeSpeaker ResNet34",
                    "what": "модели разметки голосов: где кто говорит и чей это голос",
                    "size": "34 МБ", "from": "github.com/k2-fsa/sherpa-onnx"},
                   {"name": "PyTorch, ONNX Runtime, sherpa-onnx, faster-whisper",
                    "what": "библиотеки, которые запускают эти модели на компьютере",
                    "size": "≈ 230 МБ", "from": "pypi.org"}],
               "note": "У GigaAM есть и другие модели, но они почти одинаковые по размеру и качеству. Эта — лучшая "
                       "из них для русской речи: ставит знаки препинания и знает время каждого слова. "
                       "Поэтому выбора здесь нет."},
    "whisper": {"title": "Whisper — второй черновик", "size": "", "need": False,
                "about": "Модель распознавания речи от OpenAI (сборка для faster-whisper, с huggingface.co). "
                         "Лучше английские слова и имена. Выберите один вариант: больше — точнее, но дольше обработка.",
                "parts": []},
    "claude": {"title": "Claude Code — краткое содержание и задачи", "size": "", "need": False,
               "about": "Нужна подписка Claude (Pro или Max). Без него итог — текст разговора без краткого содержания и задач.",
               "parts": [
                   {"name": "Claude Code",
                    "what": "программа Anthropic: краткое содержание, решения, задачи; получает только текст, не звук",
                    "size": "", "from": "claude.ai"}]},
}

# Варианты Whisper (репозитории и ревизии закреплены). speed — доля от длины записи на этап Whisper
# для оценки «осталось ≈»: turbo измерен на ноутбуке Дэни, остальные — прикидка по устройству моделей.
WHISPER_MODELS = {
    "large-v3": {"repo": "Systran/faster-whisper-large-v3", "rev": "edaa852ec7e145841d8ffdb056a99866b5f0a478",
                 "files": ["config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json", "model.bin"],
                 "size": "3,1 ГБ", "speed": 4.0,
                 "what": "самая большая и точная, но обрабатывает в несколько раз дольше turbo"},
    "large-v3-turbo": {"repo": "dropbox-dash/faster-whisper-large-v3-turbo",
                       "rev": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
                       "files": ["config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json", "model.bin"],
                       "size": "1,6 ГБ", "speed": 1.3,
                       "what": "почти так же точна, как large-v3, но в разы быстрее"},
    "small": {"repo": "Systran/faster-whisper-small", "rev": "536b0662742c02347bc0e980a01041f333bce120",
              "files": ["config.json", "tokenizer.json", "vocabulary.txt", "model.bin"],
              "size": "486 МБ", "speed": 0.5,
              "what": "быстрая; русские слова узнаёт заметно хуже, английские слова и имена подсказывает"},
    "base": {"repo": "Systran/faster-whisper-base", "rev": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
             "files": ["config.json", "tokenizer.json", "vocabulary.txt", "model.bin"],
             "size": "148 МБ", "speed": 0.25,
             "what": "самая лёгкая и быстрая; по-русски часто ошибается — для слабого компьютера"},
}
WHISPER_DEFAULT = "large-v3-turbo"


# ---------- что уже есть ----------

def _has_modules() -> bool:
    importlib.invalidate_caches()
    return all(importlib.util.find_spec(m) is not None for m in SPEECH_MODULES)


def _diar_files() -> list:
    d = MODELS / "diar"
    return [d / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx", d / "wespeaker_en_voxceleb_resnet34_LM.onnx"]


def _gigaam_files() -> list:
    return [MODELS / "gigaam" / "v3_e2e_rnnt.ckpt", MODELS / "gigaam" / "v3_e2e_rnnt_tokenizer.model"]


def whisper_model() -> str:
    """Какой вариант Whisper выбран."""
    key = load_settings().get("whisper_model")
    return key if key in WHISPER_MODELS else WHISPER_DEFAULT


def whisper_dir(key: str) -> Path:
    return MODELS / "whisper" / key                # готовая папка модели (а не кэш Hugging Face)


def whisper_downloaded(key: str) -> bool:
    if (whisper_dir(key) / "model.bin").exists():
        return True
    # в Windows модель когда-то скачал сам faster-whisper — в кэш Hugging Face
    return any((MODELS / "whisper").glob(f"models--*faster-whisper-{key}/snapshots/*/model.bin"))


def whisper_path() -> str:
    """Что отдавать WhisperModel: папку скачанной модели или имя (тогда — кэш Hugging Face)."""
    key = whisper_model()
    return str(whisper_dir(key)) if (whisper_dir(key) / "model.bin").exists() else key


def whisper_speed() -> float:
    return WHISPER_MODELS[whisper_model()]["speed"]


def recommend_whisper(m: dict) -> tuple:
    """Какой вариант Whisper советовать этому компьютеру и почему. Модель работает на процессоре:
    в памяти large-v3 занимает ~2 ГБ, turbo ~1 ГБ, small ~0,3 ГБ, и рядом ещё GigaAM и сама система."""
    ram, cores = m.get("ram_gb") or 0, m.get("cores") or 0
    if ram and ram < 4:
        return "base", "памяти меньше 4 ГБ — большие модели не поместятся"
    if ram and ram < 6:
        return "small", "памяти меньше 6 ГБ — большие модели будут тормозить компьютер"
    if ram >= 16 and cores >= 10:
        return "large-v3", "памяти и ядер хватит на самую точную модель; если важнее скорость — large-v3-turbo"
    return "large-v3-turbo", "лучшее сочетание точности и скорости для этого компьютера"


_speech_ok = [False]


def speech_ready() -> bool:
    if not _speech_ok[0]:                 # окно спрашивает часто; раз появилось — уже не пропадёт
        _speech_ok[0] = all(p.exists() for p in _gigaam_files() + _diar_files()) and _has_modules()
    return _speech_ok[0]


def whisper_ready() -> bool:
    return whisper_downloaded(whisper_model())


def ready(cid: str) -> bool:
    if cid == "speech":
        return speech_ready()
    if cid == "whisper":
        return whisper_ready()
    return bool(find_claude(load_settings()))


def current() -> dict:
    """Идущая или последняя загрузка."""
    st = read_json(STATUS, {}) or {}
    # pid 0 — процесс только запускается (несколько секунд)
    lost = st.get("pid") and not pid_alive(st["pid"]) or not st.get("pid") and time.time() - st.get("at", 0) > 60
    if st.get("state") == "running" and lost:
        st.update(state="error", error="Загрузка прервалась. Нажмите «Скачать» ещё раз — она продолжится с того же места.")
    return st


def status() -> dict:
    """Для окна: что установлено и что сейчас качается."""
    st = current()
    items = [{"id": k, **v, "ready": ready(k), "after": "speech" if k == "whisper" else ""} for k, v in INFO.items()]
    active = whisper_model()
    machine = machine_info()
    advice, why = recommend_whisper(machine)
    items[1]["variants"] = [{"key": k, "size": m["size"], "what": m["what"], "best": k == advice,
                             "downloaded": whisper_downloaded(k), "active": k == active,
                             "deletable": (whisper_dir(k) / "model.bin").exists() and k != active}
                            for k, m in WHISPER_MODELS.items()]
    items[1]["advice"] = {"key": advice, "why": why, "machine": machine}
    return {"items": items, "job": st}


def use_whisper(key: str) -> None:
    if key not in WHISPER_MODELS or not whisper_downloaded(key):
        raise RuntimeError("Этот вариант Whisper ещё не скачан")
    s = load_settings()
    s["whisper_model"] = key
    save_settings(s)


def delete_whisper(key: str) -> None:
    """Удалить скачанный вариант (только свою папку модели; выбранный — нельзя)."""
    import shutil
    if key not in WHISPER_MODELS or key == whisper_model():
        raise RuntimeError("Выбранный вариант удалить нельзя — сначала выберите другой")
    st = current()
    if st.get("state") == "running" and st.get("model") == key:
        raise RuntimeError("Этот вариант сейчас скачивается")
    shutil.rmtree(whisper_dir(key), ignore_errors=True)
    shutil.rmtree(whisper_dir(key).with_name(key + ".part"), ignore_errors=True)


# ---------- запуск из окна ----------

def start(cid: str, model: str = "") -> None:
    if current().get("state") == "running":
        raise RuntimeError("Уже идёт загрузка — дождитесь её конца")
    if cid == "claude":
        s = load_settings()
        s["claude_pending"] = True            # когда Claude появится, итог начнёт делать он
        save_settings(s)
        if IS_WIN:
            open_console(["powershell", "-NoProfile", "-Command",
                          "irm https://claude.ai/install.ps1 | iex; & $env:USERPROFILE\\.local\\bin\\claude.exe"],
                         "Установка Claude Code")
        else:
            open_console(["/bin/bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash && ~/.local/bin/claude"],
                         "Установка Claude Code")
        return
    if cid == "whisper":
        if not speech_ready():
            raise RuntimeError("Сначала скачайте распознавание речи")
        if model not in WHISPER_MODELS:
            raise RuntimeError("Нет такого варианта Whisper")
    JOBS.mkdir(exist_ok=True)
    write_json(STATUS, {"id": cid, "model": model, "state": "running", "pid": 0, "step": "", "done": 0, "total": 0,
                        "at": time.time()})
    spawn_detached([gui_python(), str(Path(__file__).resolve()), cid, model], ROOT / "app")


def claude_appeared(s: dict) -> bool:
    """Claude поставили кнопкой — переключаем итог на него (один раз)."""
    if s.get("claude_pending") and find_claude(s):
        s.pop("claude_pending")
        s["editor"] = "claude"
        save_settings(s)
        return True
    return False


# ---------- сама загрузка (в отдельном процессе) ----------

class Job:
    def __init__(self, cid: str, model: str = ""):
        self.model = model
        self.st = {"id": cid, "model": model, "state": "running", "pid": os.getpid(), "step": "", "done": 0,
                   "total": 0, "at": time.time()}
        self.base, self.last = 0, 0.0
        self.save()

    def save(self, force: bool = True) -> None:
        if force or time.time() - self.last > 0.7:
            self.last = time.time()
            write_json(STATUS, self.st)

    def step(self, text: str, total: int = 0) -> None:
        self.st.update(step=text, done=0, total=total)
        self.save()


def _head_size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers.get("Content-Length") or 0)


def fetch(job: Job, url: str, dest: Path, size: int = 0) -> None:
    """Скачать файл с докачкой (.part остаётся при обрыве и продолжается при следующем запуске)."""
    if dest.exists():
        job.st["done"] += size or dest.stat().st_size
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    start = job.st["done"]
    for attempt in range(1, 8):
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:      # сервер не умеет докачку — начинаем файл заново
                    have = 0
                with part.open("ab" if have else "wb") as f:
                    got = have
                    while True:
                        buf = r.read(1 << 16)
                        if not buf:
                            break
                        f.write(buf)
                        got += len(buf)
                        job.st["done"] = start + got
                        job.save(force=False)
            part.replace(dest)
            job.save()
            return
        except urllib.error.HTTPError as e:
            if e.code == 416:                     # уже всё скачано
                part.replace(dest)
                return
            if attempt == 7:
                raise
        except OSError:
            if attempt == 7:
                raise
        time.sleep(min(30, 3 * attempt))


def pip_install(args: list) -> None:
    import subprocess
    uv = find_program("uv", [ROOT / "bin" / ("uv.exe" if IS_WIN else "uv")])
    py = sys.executable
    cmd = [uv, "pip", "install", "--python", py, *args] if uv else [py, "-m", "pip", "install", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       **({"creationflags": 0x08000000} if IS_WIN else {}))
    if r.returncode:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        raise RuntimeError("Библиотеки не поставились: " + " ".join(tail))


def install_speech(job: Job) -> None:
    import hashlib
    import tarfile
    if not _has_modules():
        job.step("Библиотеки распознавания (≈ 230 МБ)")
        req = ROOT / "mac" / "requirements-speech.txt"
        pip_install(["-r", str(req)])
        src = next((p for p in (ROOT / "gigaam", ROOT / "downloads" / "gigaam" / "GigaAM-main") if p.exists()), None)
        if src is None:
            raise RuntimeError("Нет исходников GigaAM — переустановите Qamyal")
        pip_install([str(src)])
    ckpt, tok = _gigaam_files()
    if not ckpt.exists():
        job.step("Модель GigaAM", _head_size(f"{GIGAAM_URL}/{ckpt.name}"))
        fetch(job, f"{GIGAAM_URL}/{ckpt.name}", ckpt)
        with ckpt.open("rb") as f:
            md5 = hashlib.file_digest(f, "md5").hexdigest()
        if md5 != GIGAAM_MD5:
            ckpt.unlink()
            raise RuntimeError("Модель GigaAM скачалась с ошибкой. Нажмите «Скачать» ещё раз.")
    fetch(job, f"{GIGAAM_URL}/{tok.name}", tok)
    seg, emb = _diar_files()
    job.step("Разметка голосов", 33_000_000)
    if not seg.exists():
        arc = MODELS / "diar" / "seg.tar.bz2"
        fetch(job, f"{SHERPA}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", arc)
        with tarfile.open(arc) as t:
            t.extractall(MODELS / "diar", filter="data")
        arc.unlink()
    fetch(job, f"{SHERPA}/speaker-recongition-models/{emb.name}", emb)


def install_whisper(job: Job) -> None:
    key = job.model
    m = WHISPER_MODELS[key]
    url = f"https://huggingface.co/{m['repo']}/resolve/{m['rev']}"
    sizes = {f: _head_size(f"{url}/{f}") for f in m["files"]}
    job.step(f"Whisper {key}", sum(sizes.values()))
    dest = whisper_dir(key)
    tmp = dest.with_name(key + ".part")
    for f in m["files"]:
        fetch(job, f"{url}/{f}", tmp / f, sizes[f])
    tmp.replace(dest)                             # папка появляется целиком — модель либо есть, либо нет
    use_whisper(key)                              # скачали — значит, этот вариант и нужен


def main(cid: str, model: str = "") -> None:
    try:
        import truststore                         # сертификаты системы: Python от uv своих не везёт
        truststore.inject_into_ssl()
    except ImportError:
        pass
    job = Job(cid, model)
    try:
        {"speech": install_speech, "whisper": install_whisper}[cid](job)
        job.st.update(state="done", step="", error="")
    except Exception as e:  # noqa: BLE001 — показываем в окне
        job.st.update(state="error", error=str(e)[:400] or e.__class__.__name__)
    job.save()


if __name__ == "__main__":
    main(*sys.argv[1:3])
