"""Сравнение моделей распознавания речи на одних и тех же фрагментах записей.

Запуск:
  python -I compare.py --minutes 10 файл1.m4a файл2.mp3 ...

Для каждого файла в results\\<имя>\\ появляются:
  fragment.wav          — фрагмент, который распознавали (16 кГц, моно)
  <движок>.txt / .json  — расшифровка с отметками времени
  compare.html          — расшифровки рядом + плеер, клик по времени включает запись
и общий results\\timing.md со скоростью каждого движка.
Уже готовые расшифровки повторно не считаются, так что скрипт можно перезапускать.
"""
import argparse
import gc
import html
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\Расшифровка")
os.environ.setdefault("HF_HOME", str(ROOT / "models" / "hf"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

SR = 16000
THREADS = 2  # физические ядра i5-4300U

ENGINES = {
    "gigaam": "GigaAM-v3 e2e (Сбер)",
    "turbo": "Whisper large-v3-turbo",
    "large-v3": "Whisper large-v3",
}


def fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def cut_fragment(src: Path, dst: Path, start_min: float, minutes: float) -> None:
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"]
    if start_min:
        cmd += ["-ss", str(start_min * 60)]
    cmd += ["-i", str(src)]
    if minutes:
        cmd += ["-t", str(minutes * 60)]
    cmd += ["-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(dst)]
    subprocess.run(cmd, check=True)


def load_wav(path: Path) -> np.ndarray:
    audio, sr = sf.read(str(path), dtype="float32")
    assert sr == SR, sr
    return audio


# ---------- Whisper (faster-whisper / CTranslate2) ----------

def whisper_engine(size: str):
    from faster_whisper import WhisperModel

    t = time.time()
    model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=THREADS,
                         download_root=str(ROOT / "models" / "whisper"))
    load_s = time.time() - t

    def run(audio: np.ndarray):
        segments, _ = model.transcribe(
            audio, language="ru", beam_size=5,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        return [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]

    return model, load_s, run


# ---------- GigaAM-v3 (короткие куски по VAD, без pyannote и HF-токена) ----------

def speech_chunks(audio: np.ndarray, max_len: float = 22.0, max_gap: float = 1.0):
    """Нарезает запись на куски речи до max_len секунд по паузам (Silero VAD)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=200,
                      max_speech_duration_s=max_len)
    chunks = []
    for ts in get_speech_timestamps(audio, opts, sampling_rate=SR):
        s, e = ts["start"] / SR, ts["end"] / SR
        if chunks and s - chunks[-1][1] <= max_gap and e - chunks[-1][0] <= max_len:
            chunks[-1][1] = e
        else:
            chunks.append([s, e])
    return chunks


def gigaam_engine():
    import sentencepiece as spm
    import torch
    import gigaam
    from gigaam.utils import AudioDataset

    # sentencepiece на Windows не открывает пути с кириллицей — отдаём ему байты файла.
    def _load_from_bytes(self, model_file=None, model_proto=None):
        if model_proto is None:
            with open(model_file, "rb") as f:
                model_proto = f.read()
        return self.LoadFromSerializedProto(model_proto)

    spm.SentencePieceProcessor.load = _load_from_bytes

    torch.set_num_threads(THREADS)
    t = time.time()
    model = gigaam.load_model("v3_e2e_rnnt", device="cpu",
                              download_root=str(ROOT / "models" / "gigaam"))
    load_s = time.time() - t

    @torch.inference_mode()
    def run(audio: np.ndarray, batch: int = 4):
        chunks = speech_chunks(audio)
        out = []
        for i in range(0, len(chunks), batch):
            part = chunks[i:i + batch]
            wavs = [torch.from_numpy(audio[int(s * SR):int(e * SR)].copy()) for s, e in part]
            wav_pad, wav_lens = AudioDataset.collate(wavs)
            encoded, encoded_len = model.forward(wav_pad, wav_lens)
            for (s, e), (text, _) in zip(part, model._decode(encoded, encoded_len, wav_lens)):
                if text.strip():
                    out.append({"start": s, "end": e, "text": text.strip()})
        return out

    return model, load_s, run


def make_engine(name: str):
    return gigaam_engine() if name == "gigaam" else whisper_engine(name)


# ---------- вывод ----------

def write_txt(path: Path, segs) -> None:
    path.write_text("\n".join(f"[{fmt(s['start'])}] {s['text']}" for s in segs) + "\n",
                    encoding="utf-8")


def write_html(rec_dir: Path, title: str, duration: float, window: float = 30.0) -> None:
    present = [e for e in ENGINES if (rec_dir / f"{e}.json").exists()]
    data = {e: json.loads((rec_dir / f"{e}.json").read_text(encoding="utf-8")) for e in present}
    rows = []
    for w in range(int(duration // window) + 1):
        a, b = w * window, (w + 1) * window
        cells = []
        for e in present:
            txt = " ".join(s["text"] for s in data[e]["segments"] if a <= s["start"] < b)
            cells.append(f"<td>{html.escape(txt)}</td>")
        rows.append(f'<tr><th><a href="#" data-t="{a}">{fmt(a)}</a></th>{"".join(cells)}</tr>')
    heads = "".join(
        f"<th>{html.escape(ENGINES[e])}<small>обработка {fmt(data[e]['proc_s'])}"
        f" · {data[e]['proc_s'] / duration:.2f}× длины записи</small></th>" for e in present)
    doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>{html.escape(title)} — сравнение</title>
<style>
body{{font:15px/1.45 system-ui,Segoe UI,sans-serif;margin:0;background:#fafafa;color:#222}}
header{{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:10px 16px;z-index:1}}
h1{{font-size:17px;margin:0 0 6px}} audio{{width:100%}}
table{{border-collapse:collapse;width:100%;table-layout:fixed}}
th,td{{border-bottom:1px solid #e3e3e3;padding:8px 10px;vertical-align:top;text-align:left}}
thead th{{background:#f0f0f0;position:sticky;top:96px}} small{{display:block;font-weight:normal;color:#666}}
tbody th{{width:58px;font-weight:normal}} tbody th a{{color:#0a58ca;text-decoration:none}}
tr.now td,tr.now th{{background:#fff7d6}}
</style></head><body>
<header><h1>{html.escape(title)}</h1><audio id="a" controls src="fragment.wav"></audio></header>
<table><thead><tr><th>Время</th>{heads}</tr></thead><tbody>
{chr(10).join(rows)}
</tbody></table>
<script>
const a=document.getElementById('a'),rows=[...document.querySelectorAll('tbody tr')];
document.querySelectorAll('a[data-t]').forEach(x=>x.onclick=e=>{{e.preventDefault();a.currentTime=+x.dataset.t;a.play();}});
a.ontimeupdate=()=>{{const i=Math.floor(a.currentTime/{window});rows.forEach((r,j)=>r.classList.toggle('now',j===i));}};
</script></body></html>"""
    (rec_dir / "compare.html").write_text(doc, encoding="utf-8")


def write_timing(out: Path) -> None:
    lines = ["| Запись | Длина | " + " | ".join(ENGINES.values()) + " |",
             "|---|---|" + "---|" * len(ENGINES)]
    total = {e: [0.0, 0.0] for e in ENGINES}
    for rec in sorted(p for p in out.iterdir() if p.is_dir()):
        meta = rec / "meta.json"
        if not meta.exists():
            continue
        dur = json.loads(meta.read_text(encoding="utf-8"))["duration"]
        cells = []
        for e in ENGINES:
            f = rec / f"{e}.json"
            if f.exists():
                p = json.loads(f.read_text(encoding="utf-8"))["proc_s"]
                total[e][0] += p
                total[e][1] += dur
                cells.append(fmt(p))
            else:
                cells.append("—")
        lines.append(f"| {rec.name} | {fmt(dur)} | " + " | ".join(cells) + " |")
    per_hour = []
    for e in ENGINES:
        p, d = total[e]
        per_hour.append(f"≈ {fmt(p / d * 3600)}" if d else "—")
    lines.append("| **Час записи займёт** | | " + " | ".join(per_hour) + " |")
    (out / "timing.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "results")
    ap.add_argument("--engines", default="gigaam,turbo,large-v3")
    ap.add_argument("--start", type=float, default=0, help="начало фрагмента, мин")
    ap.add_argument("--minutes", type=float, default=0, help="длина фрагмента, мин (0 = вся запись)")
    args = ap.parse_args()

    recs = []
    for src in args.files:
        rec_dir = args.out / src.stem
        rec_dir.mkdir(parents=True, exist_ok=True)
        frag = rec_dir / "fragment.wav"
        if not frag.exists():
            cut_fragment(src, frag, args.start, args.minutes)
        audio = load_wav(frag)
        dur = len(audio) / SR
        (rec_dir / "meta.json").write_text(json.dumps(
            {"source": str(src), "start_min": args.start, "duration": dur}, ensure_ascii=False),
            encoding="utf-8")
        recs.append((rec_dir, src.stem, audio, dur))
        print(f"{src.name}: фрагмент {fmt(dur)}", flush=True)

    # Движок снаружи, файлы внутри: быстрые результаты появляются раньше медленных.
    for eng in args.engines.split(","):
        todo = [r for r in recs if not (r[0] / f"{eng}.json").exists()]
        if not todo:
            continue
        model, load_s, run = make_engine(eng)
        print(f"[{eng}] модель загружена за {load_s:.0f} с", flush=True)
        for rec_dir, title, audio, dur in todo:
            t = time.time()
            segs = run(audio)
            proc = time.time() - t
            (rec_dir / f"{eng}.json").write_text(json.dumps(
                {"engine": ENGINES[eng], "proc_s": proc, "load_s": load_s, "segments": segs},
                ensure_ascii=False, indent=1), encoding="utf-8")
            write_txt(rec_dir / f"{eng}.txt", segs)
            write_html(rec_dir, title, dur)
            write_timing(args.out)
            print(f"[{eng}] {title}: {fmt(proc)} на {fmt(dur)} записи ({proc / dur:.2f}×)", flush=True)
        del model, run
        gc.collect()

    print("Готово:", args.out, flush=True)


if __name__ == "__main__":
    sys.exit(main())
