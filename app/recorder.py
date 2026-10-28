"""Диктофон: запись с микрофона, звука компьютера (созвоны) или обоих сразу + живая расшифровка."""
import os
import queue
import subprocess
import threading
import time
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import soundcard as sc   # импорт здесь, в главном потоке: он поднимает COM на всё время работы
import soundfile as sf

from common import APP, INCOMING, PAUSE, SERVICES, create_job, load_settings, spawn_worker, write_json
from sysdeps import IS_MAC, IS_WIN

SR = 16000
BLOCK = 1600                     # 0.1 с
warnings.filterwarnings("ignore", message="data discontinuity")
TAP = APP / "mac" / "QamyalAudioTap"     # macOS: помощник, который отдаёт звук компьютера (собирает install.sh)


def com_init() -> None:
    """Каждый поток, который трогает звуковые устройства Windows, должен включить у себя COM."""
    if IS_WIN:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)   # COINIT_MULTITHREADED, как в soundcard


class MacSystemAudio:
    """Звук компьютера в macOS. Системного «прослушивания динамиков» там нет: помощник QamyalAudioTap
    (Core Audio tap, разрешение «Запись системного звука») пишет 16 кГц float32 моно в stdout.
    Интерфейс — как у устройства soundcard: with dev.recorder(...) as r: r.record(numframes)."""
    name = "Звук компьютера"

    def recorder(self, samplerate, channels, blocksize):
        return _TapReader()


class _TapReader:
    def __enter__(self):
        if not TAP.exists():
            raise RuntimeError("не собран помощник записи звука (запустите mac/install.sh ещё раз)")
        self.p = subprocess.Popen([str(TAP), str(SR)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self

    def record(self, numframes):
        need, buf = numframes * 4, b""
        while len(buf) < need:
            chunk = self.p.stdout.read(need - len(buf))
            if not chunk:
                err = self.p.stderr.read().decode("utf-8", "replace").strip()
                raise RuntimeError(err or "помощник записи звука остановился")
            buf += chunk
        return np.frombuffer(buf, dtype=np.float32)

    def __exit__(self, *exc):
        self.p.terminate()
        try:
            self.p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.p.kill()
        return False


def devices() -> dict:
    com_init()
    return {"mics": [m.name for m in sc.all_microphones()],
            "default_mic": sc.default_microphone().name,
            "speaker": sc.default_speaker().name}


def level(block: np.ndarray) -> float:
    """Громкость блока 0..1 (по шкале -60..0 дБ)."""
    rms = float(np.sqrt(np.mean(block ** 2))) + 1e-9
    return round(max(0.0, min(1.0, (20 * np.log10(rms) + 60) / 60)), 3)


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"            # idle | recording | paused | saving
        self.reset()

    def reset(self):
        self.path = None
        self.source = "mic"
        self.name = ""
        self.started = 0.0
        self.frames = 0
        self.marks = []
        self.levels = {"mic": 0.0, "system": 0.0}
        self.error = ""
        self.live = None
        self.speakers = None
        self.service = ""
        self.t0 = None              # якоря «секунда записи ↔ время на часах» (старт и после каждой паузы)
        self.anchors = []
        self._was_paused = True
        self.probe, self.probe_log = None, None
        self.scout, self.scout_log = None, None
        self.threads = []
        self.queues = {}
        self.running = False
        self.last_job = None

    # ---------- управление ----------

    def start(self, source: str, name: str, live: bool, mic_name: str = "", speakers: int = 0,
              service: str = "") -> None:
        com_init()
        with self.lock:
            if self.state != "idle":
                raise RuntimeError("Запись уже идёт")
            self.reset()
            self.source, self.name = source, name.strip()
            self.service = service if source in ("system", "both") and service in SERVICES else ""
            self.speakers = speakers if speakers > 0 else None
            devs = {}
            if source in ("mic", "both"):
                devs["mic"] = sc.get_microphone(mic_name) if mic_name else sc.default_microphone()
            if source in ("system", "both") and IS_MAC:
                devs["system0"] = MacSystemAudio()          # весь звук компьютера, с любых устройств
            elif source in ("system", "both"):
                # слушаем все устройства вывода: звонок может идти в гарнитуру, а не в основные динамики
                for i, spk in enumerate(sc.all_speakers() or [sc.default_speaker()]):
                    try:
                        devs[f"system{i}"] = sc.get_microphone(id=str(spk.name), include_loopback=True)
                    except Exception:  # noqa: BLE001 — устройство без захвата звука просто пропускаем
                        pass
            INCOMING.mkdir(parents=True, exist_ok=True)
            self.path = INCOMING / f"rec-{datetime.now():%Y%m%d-%H%M%S}.flac"
            self.file = sf.SoundFile(str(self.path), "w", SR, 2 if source == "both" else 1,
                                     format="FLAC", subtype="PCM_16")
            self.started = time.time()
            self.running = True
            self.state = "recording"
            for key, dev in devs.items():
                self.queues[key] = queue.Queue(maxsize=600)
                t = threading.Thread(target=self._read, args=(key, dev), daemon=True)
                t.start()
                self.threads.append(t)
            w = threading.Thread(target=self._write, daemon=True)
            w.start()
            self.threads.append(w)
            if self.service == "skype" and IS_WIN:      # Lync SDK есть только для Windows
                self._start_skype_probe()
            elif self.service and IS_WIN:
                self._start_scout()
            if live:
                from live import LiveProcess
                self.live = LiveProcess()
                PAUSE.write_text(str(os.getpid()))   # обработчику — пауза, процессор нужен здесь

    def pause(self):
        if self.state == "recording":
            self.state = "paused"

    def resume(self):
        if self.state == "paused":
            self.state = "recording"

    def mark(self, note: str = "") -> dict:
        m = {"t": round(self.frames / SR, 1), "note": note.strip()}
        if self.state in ("recording", "paused"):
            self.marks.append(m)
        return m

    def note(self, i: int, text: str):
        if 0 <= i < len(self.marks):
            self.marks[i]["note"] = text.strip()

    def stop(self, discard: bool = False):
        """Останавливает запись; если не discard — ставит её в очередь на расшифровку."""
        with self.lock:
            if self.state == "idle":
                return None
            self.state = "saving"
            self.running = False
            for t in self.threads:
                t.join(timeout=3)
            self.file.close()
            skype_log = self._stop_skype_probe()
            scout_log = self._stop_scout()
            live_segs = self.live.stop() if self.live else []
            PAUSE.unlink(missing_ok=True)
            job = None
            dur = self.frames / SR
            if discard or dur < 1.0:
                self.path.unlink(missing_ok=True)
                if skype_log:
                    skype_log.unlink(missing_ok=True)
                if scout_log:
                    scout_log.unlink(missing_ok=True)
            else:
                s = load_settings()
                when = datetime.fromtimestamp(self.started)
                title = self.name or (f"Созвон · {SERVICES[self.service]} {when:%d.%m %H-%M}" if self.service
                                      else f"Запись {when:%d.%m %H-%M}")
                extra = {"marks": self.marks, "recorded": self.source, "num_speakers": self.speakers,
                         "service": self.service, "rec_t0": self.t0, "rec_anchors": self.anchors}
                job = create_job(Path(self.path), s, move=True, name=title, when=self.started, extra=extra)
                if live_segs:
                    write_json(job / "черновики" / "живая.json", live_segs)
                if skype_log and skype_log.exists():
                    os.replace(skype_log, job / "черновики" / "skype.jsonl")
                if scout_log and scout_log.exists():
                    os.replace(scout_log, job / "черновики" / "разведка-окон.jsonl")
                spawn_worker()
            self.state = "idle"
            self.last_job = job
            self.live = None                     # процесс живой расшифровки завершён — его память вернулась системе
            return job

    # ---------- Skype для бизнеса: кто говорит (официальный Lync SDK) ----------

    def _start_skype_probe(self):
        """Разведчик пишет участников звонка и «начал/перестал говорить» с точным временем."""
        exe = APP / "skype" / "SkypeProbe.exe"
        self.probe, self.probe_log = None, None
        if not exe.exists():
            return
        self.probe_log = self.path.with_suffix(".skype.jsonl")
        try:
            self.probe = subprocess.Popen([str(exe), str(self.probe_log)], creationflags=0x08000000)
        except OSError:
            self.probe = None

    def _stop_skype_probe(self):
        p = getattr(self, "probe", None)
        if p is not None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self.probe = None
        return getattr(self, "probe_log", None)

    # ---------- остальные сервисы: разведка окна созвона (пока только собираем, как кто показан) ----------

    def _start_scout(self):
        """Разведчик пишет, что меняется в окне Zoom, Teams, Телемоста, браузера… во время созвона.
        По этим записям потом делается распознавание «кто говорит» для каждого приложения."""
        exe = APP / "scout" / "CallScout.exe"
        self.scout, self.scout_log = None, None
        if not exe.exists():
            return
        self.scout_log = self.path.with_suffix(".scout.jsonl")
        try:
            self.scout = subprocess.Popen([str(exe), str(self.scout_log)], creationflags=0x08000000)
        except OSError:
            self.scout = None

    def _stop_scout(self):
        p = getattr(self, "scout", None)
        if p is not None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self.scout = None
        return getattr(self, "scout_log", None)

    # ---------- потоки ----------

    def _read(self, key, dev):
        com_init()
        try:
            # Core Audio принимает буфер не больше 512 отсчётов — на Mac оставляем его размер по умолчанию
            with dev.recorder(samplerate=SR, channels=1, blocksize=None if IS_MAC else BLOCK) as r:
                while self.running:
                    data = r.record(numframes=BLOCK)
                    block = (data[:, 0] if data.ndim > 1 else data).astype(np.float32)
                    try:
                        self.queues[key].put_nowait(block)
                    except queue.Full:
                        pass
        except Exception as e:  # noqa: BLE001 — показываем ошибку устройства в окне
            if key == "mic" or not any(k.startswith("system") and k != key for k in self.queues):
                self.error = f"{'Микрофон' if key == 'mic' else 'Звук компьютера'}: {e}"
            self.queues.pop(key, None)

    def _take(self, key: str, n: int) -> np.ndarray:
        """n отсчётов из очереди устройства; темп задаёт главный поток, в тишине петля даёт нули."""
        buf = self.pend.get(key, np.zeros(0, dtype=np.float32))
        q = self.queues.get(key)
        while q is not None and len(buf) < n:
            try:
                buf = np.concatenate([buf, q.get_nowait()])
            except queue.Empty:
                break
        out = np.zeros(n, dtype=np.float32)
        k = min(n, len(buf))
        out[:k], buf = buf[:k], buf[k:]
        self.pend[key] = buf[-SR // 2:] if len(buf) > SR else buf    # отставание больше секунды не копим
        return out

    def _write(self):
        self.pend = {}
        systems = [k for k in self.queues if k.startswith("system")]
        main = "mic" if "mic" in self.queues else systems[0]
        while self.running or (main in self.queues and not self.queues[main].empty()):
            try:
                a = self.queues[main].get(timeout=0.5)
            except (queue.Empty, KeyError):
                if main not in self.queues:
                    break
                continue
            others = [k for k in systems if k != main and k in self.queues]
            if self.source == "both":
                b = sum((self._take(k, len(a)) for k in others), np.zeros_like(a))
                self.levels = {"mic": level(a), "system": level(b)}
                frame, mono = np.stack([a, b], axis=1), (a + b) / 2
            elif self.source == "system":
                a = a + sum((self._take(k, len(a)) for k in others), np.zeros_like(a))
                self.levels = {"system": level(a), "mic": 0.0}
                frame, mono = a, a
            else:
                self.levels = {"mic": level(a), "system": 0.0}
                frame, mono = a, a
            if self.state == "paused":
                self._was_paused = True
                continue                     # пауза: слушаем устройство, но не пишем
            if self._was_paused:             # первый блок после старта или паузы — якорь времени
                wall = time.time() - len(a) / SR
                self.anchors.append([round(self.frames / SR, 2), round(wall, 3)])
                self.t0 = self.t0 or wall
                self._was_paused = False
            self.file.write(frame)
            if self.live:
                self.live.feed(mono, self.frames / SR)
            self.frames += len(a)

    # ---------- состояние для окна ----------

    def status(self, live_from: int = 0) -> dict:
        st = {"state": self.state, "elapsed": round(self.frames / SR, 1), "source": self.source,
              "name": self.name, "service": SERVICES.get(self.service, ""),
              "levels": self.levels, "marks": self.marks, "error": self.error,
              "live": bool(self.live)}
        if self.live:
            st["live_segments"] = self.live.segments[live_from:]
            st["live_from"] = live_from
            st["live_lag"] = self.live.lag(self.frames / SR)
            st["live_ready"] = self.live.ready
        if self.last_job is not None and self.state == "idle":
            from common import job_id
            st["last_job"] = job_id(self.last_job)
        return st


def recover_orphans() -> list:
    """Записи, оставшиеся после аварийного закрытия программы, ставим в очередь."""
    out = []
    if not INCOMING.exists():
        return out
    s = load_settings()
    for f in INCOMING.glob("rec-*.flac"):
        try:
            if f.stat().st_size < 10_000:
                f.unlink()
                continue
            when = datetime.strptime(f.stem[4:], "%Y%m%d-%H%M%S").timestamp()
            out.append(create_job(f, s, move=True, name="Восстановленная запись", when=when,
                                  extra={"recorded": "recovered"}))
        except (OSError, ValueError):
            pass
    return out
