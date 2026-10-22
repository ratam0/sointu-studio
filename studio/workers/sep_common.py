"""Yhteiset apuvälineet: vokaalien erottelu (audio-separator), hiljaisuuden poisto ja tulosteet."""
from __future__ import annotations

import gc
import json
import logging
import os
import re
from pathlib import Path


def emit(tag: str, obj) -> None:
    print(f"@@{tag} " + json.dumps(obj, ensure_ascii=False), flush=True)


def progress(stage: str, i: int, n: int, title: str) -> None:
    emit("PROGRESS", {"stage": stage, "i": i, "n": n, "title": title})


def free_gpu() -> None:
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


class Stems:
    """Lataa erottelumallin kerran ja ajaa sen useille tiedostoille."""

    def __init__(self, models_dir: str, out_dir: str):
        from audio_separator.separator import Separator
        os.makedirs(out_dir, exist_ok=True)
        self.out_dir = out_dir
        self.sep = Separator(model_file_dir=models_dir, output_dir=out_dir, output_format="WAV",
                             sample_rate=44100, log_level=logging.WARNING)
        self.loaded = None

    def load(self, model: str) -> None:
        if self.loaded != model:
            print(f"[erottelu] Ladataan malli {model}", flush=True)
            self.sep.load_model(model_filename=model)
            self.loaded = model

    def run(self, path: str) -> dict[str, str]:
        """Palauttaa {stem_nimi_pienellä: polku}."""
        outs = self.sep.separate(path)
        res = {}
        for o in outs:
            p = o if os.path.isabs(o) else os.path.join(self.out_dir, o)
            m = re.search(r"_\(([^)]+)\)", Path(p).name)
            res[(m.group(1) if m else Path(p).stem).lower()] = p
        return res


def pick(stems: dict[str, str], prefer: list[str], avoid: list[str] = ()) -> tuple[str, str | None]:
    """Valitsee halutun stemin; palauttaa (haluttu, muu)."""
    keys = list(stems)
    chosen = None
    for want in prefer:
        for k in keys:
            if want in k.replace(" ", "").replace("_", "").replace("-", ""):
                chosen = k
                break
        if chosen:
            break
    if not chosen:
        rest = [k for k in keys if not any(a in k for a in avoid)]
        chosen = (rest or keys)[0]
    other = next((stems[k] for k in keys if k != chosen), None)
    return stems[chosen], other


def trim_silence(src: str, dst: str, top_db: float = 40.0, pad: float = 0.15) -> tuple[float, float]:
    """Poistaa hiljaiset kohdat; palauttaa (alkuperäinen_kesto, jäljelle_jäänyt_kesto)."""
    import librosa
    import numpy as np
    import soundfile as sf

    y, sr = librosa.load(src, sr=None, mono=True)
    total = len(y) / sr
    intervals = librosa.effects.split(y, top_db=top_db, frame_length=2048, hop_length=512)
    p = int(pad * sr)
    parts = []
    for s, e in intervals:
        if (e - s) / sr < 0.3:       # yksittäiset napsahdukset pois
            continue
        parts.append(y[max(0, s - p):min(len(y), e + p)])
    if not parts:
        return total, 0.0
    out = np.concatenate(parts)
    peak = float(np.max(np.abs(out))) or 1.0
    out = out * min(1.0, 0.95 / peak)
    sf.write(dst, out, sr, subtype="PCM_16")
    return total, len(out) / sr
