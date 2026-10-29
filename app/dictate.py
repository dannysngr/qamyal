"""Вопрос голосом: короткая запись с микрофона → текст GigaAM.
Модель грузится в отдельном процессе (LiveProcess) сразу при нажатии — пока человек говорит;
после ответа процесс завершается и отдаёт память."""
import threading

import numpy as np
import soundcard as sc

from recorder import BLOCK, SR, com_init, level
from sysdeps import IS_MAC

MAX_LEN = 90.0          # с — дольше вопрос не слушаем


class Dictation:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.live = None
        self.thread = None
        self.level = 0.0
        self.error = ""

    def start(self, mic_name: str = "") -> None:
        from live import LiveProcess
        with self.lock:
            if self.running:
                return
            com_init()
            mic = sc.get_microphone(mic_name) if mic_name else sc.default_microphone()
            self.error, self.level = "", 0.0
            self.live = LiveProcess()
            self.running = True
            self.thread = threading.Thread(target=self._read, args=(mic,), daemon=True)
            self.thread.start()

    def stop(self) -> str:
        with self.lock:
            if not self.live:
                return ""
            self.running = False
            self.thread.join(timeout=5)
            live, self.live = self.live, None
        text = " ".join(s["text"] for s in live.stop()).strip()
        if not text and self.error:
            raise RuntimeError(self.error)
        return text

    def status(self) -> dict:
        return {"on": self.running, "level": self.level, "error": self.error}

    def _read(self, mic):
        com_init()
        t, live = 0.0, self.live
        try:
            with mic.recorder(samplerate=SR, channels=1, blocksize=None if IS_MAC else BLOCK) as r:
                while self.running and t < MAX_LEN:
                    data = r.record(numframes=BLOCK)
                    block = (data[:, 0] if data.ndim > 1 else data).astype(np.float32)
                    self.level = level(block)
                    live.feed(block, t)
                    t += len(block) / SR
        except Exception as e:  # noqa: BLE001 — ошибку устройства показываем в окне
            self.error = f"Микрофон: {e}"
        self.running, self.level = False, 0.0
