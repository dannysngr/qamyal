"""Модели для Qamyal на Mac: скачивает то, чего ещё нет в <папка>/models.
GigaAM и разметку голосов обычно привозим готовыми (комплект с Windows), Whisper качаем с Hugging Face.
Запуск: .venv/bin/python mac/download_models.py <папка Qamyal>"""
import sys
import tarfile
import urllib.request
from pathlib import Path

root = Path(sys.argv[1]).expanduser()
models = root / "models"
models.mkdir(parents=True, exist_ok=True)


def fetch(url: str, dest: Path) -> None:
    print(f"  скачиваю {url}", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)


# 1. разметка голосов (sherpa-onnx): сегментация pyannote 3.0 + отпечатки голоса wespeaker
diar = models / "diar"
seg = diar / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
emb = diar / "wespeaker_en_voxceleb_resnet34_LM.onnx"
diar.mkdir(exist_ok=True)
if not seg.exists():
    arc = diar / "seg.tar.bz2"
    fetch("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
          "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", arc)
    with tarfile.open(arc) as t:
        t.extractall(diar)
    arc.unlink()
if not emb.exists():
    fetch("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
          "wespeaker_en_voxceleb_resnet34_LM.onnx", emb)
print("разметка голосов: готово", flush=True)

# 2. GigaAM (основное распознавание)
import gigaam  # noqa: E402

gigaam.load_model("v3_e2e_rnnt", device="cpu", download_root=str(models / "gigaam"))
print("GigaAM: готово", flush=True)

# 3. Whisper large-v3-turbo (второй черновик: английские слова и имена)
from faster_whisper.utils import download_model  # noqa: E402

download_model("large-v3-turbo", cache_dir=str(models / "whisper"))
print("Whisper: готово", flush=True)
