"""Распознавание: подготовка звука, GigaAM, разметка голосов, Whisper."""
import bisect
import gc
import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from common import MODELS, fmt_time
from sysdeps import HIDDEN, find_program

SR = 16000


def ffmpeg_exe() -> str:
    exe = find_program("ffmpeg")
    if exe:
        return exe
    try:                                     # на Mac ставим pip-пакет со своей сборкой ffmpeg
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        pass
    raise RuntimeError("Не найден ffmpeg — он нужен, чтобы читать аудиофайлы.")


def prepare(src: Path, wav: Path, mp3: Path) -> float:
    """Звук для моделей (16 кГц моно) и лёгкий mp3 для плеера. Возвращает длину, с."""
    ff = ffmpeg_exe()
    base = [ff, "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1"]
    subprocess.run(base + ["-ar", str(SR), "-c:a", "pcm_s16le", str(wav)],
                   check=True, capture_output=True, **HIDDEN)
    subprocess.run(base + ["-ar", "22050", "-c:a", "libmp3lame", "-b:a", "48k", str(mp3)],
                   check=True, capture_output=True, **HIDDEN)
    return sf.info(str(wav)).duration


def load_wav(path: Path) -> np.ndarray:
    audio, sr = sf.read(str(path), dtype="float32")
    assert sr == SR, sr
    return audio


def decode(path: Path) -> np.ndarray:
    """Любой аудиофайл → 16 кГц моно (для старых записей, где wav уже удалён)."""
    pcm = subprocess.run([ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", str(path), "-vn",
                          "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True, **HIDDEN).stdout
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768


def decode_range(path: Path, start: float, dur: float) -> np.ndarray:
    """Кусок записи (16 кГц моно) — для образца голоса из одной реплики."""
    pcm = subprocess.run([ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-ss", str(max(0.0, start)),
                          "-t", str(dur), "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True, **HIDDEN).stdout
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768


def peaks(audio: np.ndarray, n: int = 1600) -> list:
    """Огибающая для рисования волны: n значений 0..1."""
    if not len(audio):
        return []
    n = max(1, min(n, len(audio) // 160))
    a = np.abs(audio[: len(audio) // n * n]).reshape(n, -1)
    p = np.sqrt(np.mean(a ** 2, axis=1))
    p = p / (np.percentile(p, 99) + 1e-9)
    return [round(float(x), 2) for x in np.clip(p, 0, 1)]


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _join_punct(words: list) -> list:
    """Знаки препинания, выделенные в отдельные «слова», приклеиваем к предыдущему."""
    out = []
    for w in words:
        if out and not any(ch.isalnum() for ch in w["w"]):
            out[-1]["w"] += w["w"]
            out[-1]["e"] = w["e"]
        else:
            out.append(w)
    return out


# ---------- GigaAM ----------

def speech_chunks(audio: np.ndarray, max_len: float = 22.0, max_gap: float = 1.0) -> list:
    """Куски речи до max_len секунд, нарезанные по паузам (Silero VAD)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=200, max_speech_duration_s=max_len)
    chunks = []
    for ts in get_speech_timestamps(audio, opts, sampling_rate=SR):
        s, e = ts["start"] / SR, ts["end"] / SR
        if chunks and s - chunks[-1][1] <= max_gap and e - chunks[-1][0] <= max_len:
            chunks[-1][1] = e
        else:
            chunks.append([s, e])
    return chunks


def cover_chunks(audio: np.ndarray, target: float = 16.0, max_len: float = 22.0) -> list:
    """Вся запись кусками до max_len с разрезами в самых тихих местах.
    Детектор речи на тихой/далёкой речи (телефон на громкой связи) пропускает целые фразы,
    поэтому не выбрасываем ничего, кроме совсем беззвучных кусков."""
    hop = SR // 10
    n = len(audio) // hop
    if not n:
        return []
    rms = np.sqrt(np.mean(audio[: n * hop].reshape(n, hop) ** 2, axis=1))
    chunks, start = [], 0
    while start < n:
        if n - start <= int(max_len * 10):
            end = n
        else:
            lo, hi = start + int(target * 0.75 * 10), start + int(max_len * 10)
            end = lo + int(np.argmin(rms[lo:hi]))      # самый тихий момент — пауза между фразами
        seg = rms[start:end]
        if len(seg) and seg.max() > 0.003:            # совсем беззвучное не распознаём
            chunks.append([start / 10, end / 10])
        start = max(end, start + 1)
    return chunks


def load_gigaam(threads: int):
    import sentencepiece as spm
    import torch
    import gigaam

    # sentencepiece на Windows не открывает пути с кириллицей — отдаём ему байты файла.
    def _load_from_bytes(self, model_file=None, model_proto=None):
        if model_proto is None:
            with open(model_file, "rb") as f:
                model_proto = f.read()
        return self.LoadFromSerializedProto(model_proto)

    spm.SentencePieceProcessor.load = _load_from_bytes
    torch.set_num_threads(threads)
    return gigaam.load_model("v3_e2e_rnnt", device="cpu", download_root=str(MODELS / "gigaam"))


def gigaam_text(model, audio: np.ndarray) -> str:
    """Короткий кусок (до 25 с) → текст. Для живой расшифровки."""
    import torch
    with torch.inference_mode():
        wav = torch.from_numpy(audio.astype(np.float32).copy()).unsqueeze(0)
        lens = torch.tensor([wav.shape[-1]])
        enc, enc_len = model.forward(wav, lens)
        return model._decode(enc, enc_len, lens)[0][0].strip()


def run_gigaam(audio: np.ndarray, threads: int, progress) -> list:
    import torch
    from gigaam.utils import AudioDataset

    model = load_gigaam(threads)
    chunks = cover_chunks(audio)
    segs, batch = [], 4
    with torch.inference_mode():
        for i in range(0, len(chunks), batch):
            part = chunks[i:i + batch]
            wavs = [torch.from_numpy(audio[int(s * SR):int(e * SR)].copy()) for s, e in part]
            wav_pad, wav_lens = AudioDataset.collate(wavs)
            enc, enc_len = model.forward(wav_pad, wav_lens)
            for (s, e), (text, words) in zip(part, model._decode(enc, enc_len, wav_lens, True)):
                if not text.strip():
                    continue
                ws = [{"w": w.text, "s": round(s + w.start, 2), "e": round(s + w.end, 2)}
                      for w in (words or [])]
                segs.append({"s": round(s, 2), "e": round(e, 2), "text": text.strip(),
                             "words": _join_punct(ws)})
            progress(min(99, (i + len(part)) * 100 // max(1, len(chunks))))
    del model
    gc.collect()
    return segs


# ---------- разметка голосов ----------

def run_diar(audio: np.ndarray, threads: int, threshold: float, progress, num_clusters: int = -1) -> list:
    """Разметка голосов. num_clusters > 0 — если известно, сколько людей говорило."""
    import sherpa_onnx

    d = MODELS / "diar"
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(d / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx")),
            num_threads=threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(d / "wespeaker_en_voxceleb_resnet34_LM.onnx"), num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_clusters, threshold=threshold),
        min_duration_on=0.3, min_duration_off=0.5)
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)

    def cb(done, total):
        progress(min(99, done * 100 // max(1, total)))
        return 0

    res = sd.process(audio, callback=cb).sort_by_start_time()
    return [{"s": round(r.start, 2), "e": round(r.end, 2), "spk": int(r.speaker)} for r in res]


def decode_channel(path: Path, ch: int) -> np.ndarray:
    """Один канал стереофайла → 16 кГц моно (созвон: 0 — микрофон, 1 — звук компьютера)."""
    pcm = subprocess.run([ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", str(path), "-vn",
                          "-af", f"pan=mono|c0=c{ch}", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True, **HIDDEN).stdout
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768


def _speech_mask(audio: np.ndarray, frame: int) -> np.ndarray:
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    mask = np.zeros(len(audio) // frame + 1, dtype=bool)
    opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=100)
    for ts in get_speech_timestamps(audio, opts, sampling_rate=SR):
        mask[ts["start"] // frame: ts["end"] // frame + 1] = True
    return mask


def _mask_segments(mask: np.ndarray, step: float, spk: int, min_len=0.4, max_gap=0.5) -> list:
    segs, start = [], None
    for i, on in enumerate(np.append(mask, False)):
        if on and start is None:
            start = i
        elif not on and start is not None:
            s, e = start * step, i * step
            if segs and s - segs[-1]["e"] <= max_gap:
                segs[-1]["e"] = round(e, 2)
            else:
                segs.append({"s": round(s, 2), "e": round(e, 2), "spk": spk})
            start = None
    return [x for x in segs if x["e"] - x["s"] >= min_len]


def run_diar_call(mic: np.ndarray, system: np.ndarray, threads: int, threshold: float, progress,
                  n_remote: int = -1) -> list:
    """Созвон: в левом канале — микрофон (хозяин записи, всегда «спикер 0»), в правом — собеседники.
    Микрофон слышит и эхо из динамиков, поэтому там, где звучит правый канал, речь считаем чужой."""
    frame = SR // 10
    n = min(len(mic), len(system))
    sys_mask = _speech_mask(system[:n], frame)
    if sys_mask.mean() < 0.02:
        # звук компьютера не записался (звонок шёл через другое устройство или телефон на громкой
        # связи) — весь разговор в микрофоне: обычная разметка, без «спикера 0 = хозяин записи»
        return [{**x, "spk": x["spk"] + 1}
                for x in run_diar(mic[:n] + system[:n], threads, threshold, progress,
                                  n_remote + 1 if n_remote > 0 else -1)]
    mic_mask = _speech_mask(mic[:n], frame)
    local = _mask_segments(mic_mask & ~sys_mask, 0.1, 0)
    remote = []
    if sys_mask.any():
        remote = [{**x, "spk": x["spk"] + 1}
                  for x in run_diar(system[:n], threads, threshold, progress, n_remote)]
        if not remote:
            remote = _mask_segments(sys_mask, 0.1, 1)
    return sorted(local + remote, key=lambda x: x["s"])


# ---------- Whisper ----------

def run_whisper(audio: np.ndarray, threads: int, beam: int, progress) -> list:
    from faster_whisper import WhisperModel
    from components import whisper_path

    model = WhisperModel(whisper_path(), device="cpu", compute_type="int8", cpu_threads=threads,
                         download_root=str(MODELS / "whisper"))
    dur = len(audio) / SR
    segments, _ = model.transcribe(
        audio, language="ru", beam_size=beam, word_timestamps=True,
        # порог ниже стандартного: иначе тихая речь (телефон на громкой связи) выпадает целиком
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500, "threshold": 0.3},
        condition_on_previous_text=False)
    segs = []
    for seg in segments:
        ws = [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
              for w in (seg.words or []) if w.word.strip()]
        segs.append({"s": round(seg.start, 2), "e": round(seg.end, 2), "text": seg.text.strip(),
                     "words": _join_punct(ws)})
        progress(min(99, int(seg.end * 100 / dur)))
    del model
    gc.collect()
    return segs


# ---------- реплики: слова + голоса ----------

def label_speakers(diar: list) -> dict:
    """Номера кластеров → «Спикер N» по порядку первого появления."""
    labels = {}
    for d in diar:
        if d["spk"] not in labels:
            labels[d["spk"]] = f"Спикер {len(labels) + 1}"
    return labels


def speaker_at(diar: list, s: float, e: float, starts: list) -> int:
    best, best_ov, near, near_d = None, 0.0, None, 1e9
    # отрезки голосов отсортированы по началу и редко длиннее двух минут
    for d in diar[bisect.bisect_left(starts, s - 120):]:
        if d["s"] > e + 2:
            break
        ov = min(e, d["e"]) - max(s, d["s"])
        if ov > best_ov:
            best, best_ov = d["spk"], ov
        dist = max(d["s"] - e, s - d["e"], 0)
        if dist < near_d:
            near, near_d = d["spk"], dist
    if best is not None:
        return best
    return near if near_d <= 1.5 else -1


def to_turns(segs: list, diar: list, mark_every: float = 12.0) -> list:
    """Слова раскладываются по говорящим; подряд идущие слова одного человека — одна реплика.
    Внутри длинных реплик ставятся отметки [ММ:СС], чтобы редактор мог точно датировать абзацы."""
    labels = label_speakers(diar)
    starts = [d["s"] for d in diar]
    turns = []
    prev = None
    for seg in segs:
        words = seg.get("words") or [{"w": seg["text"], "s": seg["s"], "e": seg["e"]}]
        for w in words:
            spk = speaker_at(diar, w["s"], w["e"], starts) if diar else -1
            if spk == -1:
                spk = prev if prev is not None else -1
            if turns and spk == prev and w["s"] - turns[-1]["e"] < 3.0:
                t = turns[-1]
                pause = w["s"] - t["e"] > 0.8 or t["text"].endswith((".", "?", "!", "…"))
                if w["s"] - t["mark"] >= mark_every and pause:
                    t["text"] += f" [{fmt_time(w['s'])}]"
                    t["mark"] = w["s"]
                t["text"] += " " + w["w"]
                t["e"] = w["e"]
            else:
                turns.append({"s": w["s"], "e": w["e"], "mark": w["s"],
                              "who": labels.get(spk, "Спикер ?"), "text": w["w"]})
            prev = spk
    return turns
