"""Живая расшифровка во время записи: GigaAM по кускам речи, отставание ~10–20 с.
Работает отдельным процессом (LiveProcess): после записи он завершается, и память модели (~2 ГБ)
гарантированно возвращается системе — внутри сервера Python её не отдавал."""
import json
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from common import load_settings
from sysdeps import HIDDEN, pipe_python

SR = 16000
HEAD = struct.Struct("<dI")    # кусок звука в трубе: время записи (с), число отсчётов float32
MAX_CHUNK = 18.0       # с — дольше не ждём паузы, режем принудительно
MIN_PAUSE = 0.6        # с тишины после речи — конец фразы


class Live:
    def __init__(self):
        self.buf = np.zeros(0, dtype=np.float32)
        self.buf_t = 0.0               # время записи, с которого начинается buf
        self.segments = []             # [{"t": сек, "text": ...}]
        self.lock = threading.Lock()
        self.running = True
        self.ready = False
        self.done_t = 0.0              # до какого времени записи текст уже готов
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def feed(self, block: np.ndarray, t: float) -> None:
        with self.lock:
            if not len(self.buf):
                self.buf_t = t
            self.buf = np.concatenate([self.buf, block])

    def lag(self, now: float) -> float:
        return round(max(0.0, now - self.done_t), 1)

    def stop(self) -> list:
        self.running = False
        self.thread.join(timeout=60)
        return self.segments

    def _loop(self):
        import asr
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        model = asr.load_gigaam(load_settings()["threads"])
        self.ready = True
        opts = VadOptions(min_silence_duration_ms=int(MIN_PAUSE * 1000), speech_pad_ms=150)
        final = False
        while True:
            if not self.running:
                final = True
            with self.lock:
                buf, t0 = self.buf.copy(), self.buf_t
            dur = len(buf) / SR
            if dur < 2.0 and not final:
                time.sleep(0.5)
                continue
            speech = get_speech_timestamps(buf, opts, sampling_rate=SR) if dur >= 0.5 else []
            # где резать: конец последней фразы, после которой уже есть пауза
            cut = 0
            for sp in speech:
                if (len(buf) - sp["end"]) / SR >= MIN_PAUSE:
                    cut = sp["end"]
            if final:
                cut = len(buf)
            elif not cut and dur >= MAX_CHUNK:
                cut = len(buf)
            if not cut:
                if not speech:                         # одна тишина — выбрасываем, кроме хвоста
                    self._drop(max(0, len(buf) - SR // 2))
                time.sleep(0.5)
                if final:
                    break
                continue
            parts = [sp for sp in speech if sp["start"] < cut]
            if parts:
                a, b = parts[0]["start"], min(parts[-1]["end"], cut)
                piece = buf[a:b]
                for k in range(0, len(piece), int(22 * SR)):     # GigaAM берёт до 25 с
                    sub = piece[k:k + int(22 * SR)]
                    if len(sub) < SR // 4:
                        continue
                    try:
                        text = asr.gigaam_text(model, sub)
                    except Exception:  # noqa: BLE001 — живой текст необязателен
                        text = ""
                    if text:
                        self.segments.append({"t": round(t0 + (a + k) / SR, 1), "text": text})
            self._drop(cut)
            self.done_t = t0 + cut / SR
            if final:
                break
        del model

    def _drop(self, n: int) -> None:
        with self.lock:
            self.buf = self.buf[n:]
            self.buf_t += n / SR


class LiveProcess:
    """Тот же интерфейс, что у Live, но модель живёт в дочернем процессе: звук — ему в stdin,
    готовые фразы — обратно строками JSON."""

    def __init__(self):
        self.segments = []
        self.ready = False
        self.done_t = 0.0
        self.proc = subprocess.Popen([pipe_python(), "-X", "utf8", str(Path(__file__).resolve()), "--child"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=str(Path(__file__).parent), **HIDDEN)
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()

    def feed(self, block: np.ndarray, t: float) -> None:
        try:
            self.proc.stdin.write(HEAD.pack(t, len(block)) + block.astype("<f4").tobytes())
        except (OSError, ValueError):
            pass                                   # процесс упал — живой текст необязателен, запись идёт дальше

    def lag(self, now: float) -> float:
        return round(max(0.0, now - self.done_t), 1)

    def stop(self) -> list:
        try:
            self.proc.stdin.close()                # конец звука: дочерний дорасшифрует хвост и выйдет
        except OSError:
            pass
        self.thread.join(timeout=60)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return self.segments

    def _listen(self):
        for line in self.proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("ready"):
                self.ready = True
            if "seg" in m:
                self.segments.append(m["seg"])
            if "done_t" in m:
                self.done_t = m["done_t"]


def child():
    """Дочерний процесс LiveProcess: читает звук из stdin, пишет фразы в stdout, на конце stdin — выходит."""
    out = sys.stdout
    lv = Live()

    def read():
        inp = sys.stdin.buffer
        while True:
            head = inp.read(HEAD.size)
            if len(head) < HEAD.size:
                break
            t, n = HEAD.unpack(head)
            data = inp.read(n * 4)
            if len(data) < n * 4:
                break
            lv.feed(np.frombuffer(data, dtype="<f4"), t)
        lv.running = False

    threading.Thread(target=read, daemon=True).start()
    sent, ready, done = 0, False, None
    while True:
        alive = lv.thread.is_alive()
        if lv.ready and not ready:
            ready = True
            out.write(json.dumps({"ready": True}) + "\n")
        for s in lv.segments[sent:]:
            out.write(json.dumps({"seg": s}) + "\n")
        sent = len(lv.segments)
        if lv.done_t != done:
            done = lv.done_t
            out.write(json.dumps({"done_t": done}) + "\n")
        out.flush()
        if not alive:
            break
        time.sleep(0.3)


if __name__ == "__main__":
    if "--child" in sys.argv:
        child()
