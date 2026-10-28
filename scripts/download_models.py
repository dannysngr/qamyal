"""Скачивает веса моделей для сравнения в D:\\Расшифровка\\models."""
import os
import time

ROOT = r"D:\Расшифровка\models"
os.environ.setdefault("HF_HOME", os.path.join(ROOT, "hf"))

from faster_whisper.utils import download_model  # noqa: E402
import gigaam  # noqa: E402

for name in ("large-v3-turbo", "large-v3"):
    t = time.time()
    path = download_model(name, cache_dir=os.path.join(ROOT, "whisper"))
    print(f"whisper {name}: {path} ({time.time() - t:.0f} s)", flush=True)

t = time.time()
gigaam.load_model("v3_e2e_rnnt", device="cpu", download_root=os.path.join(ROOT, "gigaam"))
print(f"gigaam v3_e2e_rnnt: ok ({time.time() - t:.0f} s)", flush=True)
