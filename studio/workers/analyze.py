"""Kappaleiden automaattinen analyysi koulutusdataa varten.

Ajetaan ACE-Stepin Python-ympäristössä, työhakemistona ACE-Step-1.5.
Syöte: JSON-tiedosto {"tracks": [{"id", "path", "title"}], "stages": [...], ...}
Tulos: stdout-rivit muotoa  @@RESULT {"track_id", "stage", "fields", "notes"}

Vaiheet (ajetaan erikseen, jotta GPU-muisti vapautuu välissä):
  meta     – BPM, sävellaji, tahtilaji (librosa)
  lyrics   – laulun tunnistus + lyriikat + kieli (faster-whisper)
  caption  – tyylikuvaus ja genre (ACE-Stepin oma 5Hz-kielimalli)
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import traceback

import numpy as np


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


# ---------------------------------------------------------------- meta (librosa)
_NOTES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _key_from_chroma(chroma_mean: np.ndarray) -> tuple[str, float]:
    best, best_r = "C major", -2.0
    c = chroma_mean - chroma_mean.mean()
    for i in range(12):
        for prof, mode in ((_MAJOR, "major"), (_MINOR, "minor")):
            p = np.roll(prof, i)
            p = p - p.mean()
            r = float(np.dot(c, p) / (np.linalg.norm(c) * np.linalg.norm(p) + 1e-9))
            if r > best_r:
                best, best_r = f"{_NOTES[i]} {mode}", r
    return best, best_r


def stage_meta(tracks: list[dict]) -> None:
    import librosa

    for i, t in enumerate(tracks):
        progress("meta", i + 1, len(tracks), t["title"])
        notes = []
        try:
            y, sr = librosa.load(t["path"], sr=22050, mono=True, duration=240)
            y_h, y_p = librosa.effects.hpss(y)
            onset = librosa.onset.onset_strength(y=y_p, sr=sr)
            tempo, _ = librosa.beat.beat_track(onset_envelope=onset, sr=sr)
            bpm = float(np.atleast_1d(tempo)[0])
            # Puolitus/tuplaus järkevälle välille
            while bpm > 0 and bpm < 60:
                bpm *= 2
            while bpm > 200:
                bpm /= 2
            chroma = librosa.feature.chroma_cqt(y=y_h, sr=sr)
            key, conf = _key_from_chroma(chroma.mean(axis=1))
            if conf < 0.5:
                notes.append(f"sävellaji epävarma ({key}), tarkista")
            emit("RESULT", {"track_id": t["id"], "stage": "meta",
                            "fields": {"bpm": int(round(bpm)) if bpm else None, "keyscale": key,
                                       "timesignature": "4"},
                            "notes": notes})
        except Exception as exc:
            print(f"[meta] {t['title']}: {exc}", flush=True)
            emit("RESULT", {"track_id": t["id"], "stage": "meta", "fields": {},
                            "notes": [f"analyysi epäonnistui: {exc}"]})


# ---------------------------------------------------------------- lyrics (whisper)
_HALLUCINATIONS = {"thank you.", "thanks for watching!", "thank you for watching.",
                   "kiitos katsomisesta.", "you", "♪", "music", "[music]", "(music)"}


def _load_whisper(model_name: str):
    from faster_whisper import WhisperModel
    try:
        m = WhisperModel(model_name, device="cuda", compute_type="float16")
        print(f"[lyrics] Whisper {model_name} GPU:lla", flush=True)
        return m
    except Exception as exc:
        print(f"[lyrics] GPU ei käytettävissä Whisperille ({exc}); käytetään CPU:ta (hitaampi)", flush=True)
        return WhisperModel(model_name, device="cpu", compute_type="int8")


def stage_lyrics(tracks: list[dict], model_name: str, language: str | None) -> None:
    model = _load_whisper(model_name)
    for i, t in enumerate(tracks):
        progress("lyrics", i + 1, len(tracks), t["title"])
        notes = []
        try:
            segments, info = model.transcribe(
                t["path"], language=language or None, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 700},
                beam_size=5, condition_on_previous_text=False,
                no_speech_threshold=0.6, temperature=0.0,
            )
            kept = []
            for s in segments:
                txt = s.text.strip()
                if not txt or txt.lower() in _HALLUCINATIONS:
                    continue
                if s.no_speech_prob > 0.6 or s.avg_logprob < -1.0 or s.compression_ratio > 2.4:
                    continue
                kept.append((s.start, s.end, txt))
            words = sum(len(x[2].split()) for x in kept)
            sung_seconds = sum(e - s for s, e, _ in kept)
            instrumental = words < 15 or sung_seconds < 10
            fields: dict = {"is_instrumental": instrumental}
            if instrumental:
                fields["lyrics"] = "[Instrumental]"
                fields["language"] = "unknown"
                if words > 0:
                    notes.append(f"tunnistettiin vain {words} sanaa – merkitty instrumentaaliksi, tarkista")
            else:
                # Kappaleet erotellaan tauoista (> 2.5 s) – lisää itse [Verse]/[Chorus]-merkinnät
                lines, prev_end = [], None
                for s, e, txt in kept:
                    if prev_end is not None and s - prev_end > 2.5:
                        lines.append("")
                    lines.append(txt)
                    prev_end = e
                fields["lyrics"] = "\n".join(lines).strip()
                fields["language"] = info.language if info.language_probability > 0.5 else "unknown"
                notes.append("lyriikat tunnistettu automaattisesti – tarkista ja lisää [Verse]/[Chorus]")
                if info.language_probability < 0.7:
                    notes.append(f"kieli epävarma ({info.language} {info.language_probability:.0%})")
            emit("RESULT", {"track_id": t["id"], "stage": "lyrics", "fields": fields, "notes": notes})
        except Exception as exc:
            traceback.print_exc()
            emit("RESULT", {"track_id": t["id"], "stage": "lyrics", "fields": {},
                            "notes": [f"tunnistus epäonnistui: {exc}"]})
    del model
    free_gpu()


# ---------------------------------------------------------------- caption (ACE 5Hz LM)
def stage_caption(tracks: list[dict], ace_dir: str, ckpt_dir: str, lm_name: str) -> None:
    from acestep.handler import AceStepHandler
    from acestep.llm_inference import LLMHandler

    print("[caption] Ladataan ACE-Step (turbo DiT + VAE) ja kielimalli…", flush=True)
    dit = AceStepHandler()
    msg, ok = dit.initialize_service(project_root=ace_dir, config_path="acestep-v15-turbo", device="cuda")
    if not ok:
        raise RuntimeError(f"DiT-alustus epäonnistui: {msg}")
    llm = LLMHandler()
    msg, ok = llm.initialize(checkpoint_dir=ckpt_dir, lm_model_path=lm_name, backend="pt", device="cuda")
    if not ok:
        raise RuntimeError(f"Kielimallin alustus epäonnistui: {msg}")

    for i, t in enumerate(tracks):
        progress("caption", i + 1, len(tracks), t["title"])
        try:
            codes = dit.convert_src_audio_to_codes(t["path"])
            if not codes or str(codes).startswith("❌"):
                raise RuntimeError(f"audion koodaus epäonnistui: {codes}")
            meta, status = llm.understand_audio_from_codes(audio_codes=codes, temperature=0.7,
                                                           use_constrained_decoding=True)
            if not meta:
                raise RuntimeError(status)
            caption = (meta.get("caption") or "").strip()
            genre = meta.get("genres") or meta.get("genre") or ""
            if isinstance(genre, list):
                genre = ", ".join(genre)
            fields = {"caption": caption, "genre": str(genre).strip()}
            emit("RESULT", {"track_id": t["id"], "stage": "caption", "fields": fields,
                            "notes": ["kuvaus generoitu – muokkaa tarvittaessa tarkemmaksi"]})
        except Exception as exc:
            traceback.print_exc()
            emit("RESULT", {"track_id": t["id"], "stage": "caption", "fields": {},
                            "notes": [f"kuvauksen generointi epäonnistui: {exc}"]})
    del llm, dit
    free_gpu()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    args = ap.parse_args()
    spec = json.loads(open(args.spec, encoding="utf-8").read())
    tracks = spec["tracks"]
    stages = spec.get("stages", ["meta", "lyrics", "caption"])
    failed = False
    for stage in stages:
        stage_tracks = [t for t in tracks if stage in t.get("stages", stages)]
        if not stage_tracks:
            continue
        emit("STAGE", {"stage": stage, "n": len(stage_tracks)})
        try:
            if stage == "meta":
                stage_meta(stage_tracks)
            elif stage == "lyrics":
                stage_lyrics(stage_tracks, spec.get("whisper_model", "large-v3"), spec.get("language"))
            elif stage == "caption":
                stage_caption(stage_tracks, spec["ace_dir"], spec["checkpoints_dir"], spec.get("caption_lm", "acestep-5Hz-lm-1.7B"))
        except Exception as exc:
            traceback.print_exc()
            print(f"[{stage}] VIRHE: {exc}", flush=True)
            for t in stage_tracks:
                emit("RESULT", {"track_id": t["id"], "stage": stage, "fields": {},
                                "notes": [f"vaihe epäonnistui: {exc}"], "failed": True})
            failed = True
        free_gpu()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
