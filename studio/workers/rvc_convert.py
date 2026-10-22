"""Äänen muunnos RVC-mallilla: (erottelu) → (kaiunpoisto) → RVC → miksaus takaisin taustan kanssa.

Ajetaan Applion Python-ympäristössä, työhakemistona Applio-kansio.
Tulos: @@RESULT {"outputs": {"mix": ..., "vocals": ..., "instrumental": ..., "source_vocals": ...}}
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from sep_common import Stems, emit, free_gpu, pick, progress


def mix(vocals: str, inst: str, out: str, vg: float, ig: float) -> None:
    filt = (f"[0:a]aresample=44100,volume={vg}dB[v];[1:a]aresample=44100,volume={ig}dB[i];"
            f"[v][i]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97:level=false[o]")
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", vocals, "-i", inst,
                    "-filter_complex", filt, "-map", "[o]", out], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    s = json.loads(Path(ap.parse_args().spec).read_text(encoding="utf-8"))
    out = Path(s["out_dir"])
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path.cwd()))
    stages = 1 + int(s["separate"]) + int(s["dereverb"]) + int(s["separate"])
    k = 0

    vocals, inst = s["input"], None
    stems = None
    if s["separate"]:
        k += 1
        progress("Erotellaan laulu taustasta", k, stages, "")
        stems = Stems(s["models_dir"], str(work))
        stems.load(s["vocal_model"])
        res = stems.run(vocals)
        v, other = pick(res, ["vocals"], ["instrument"])
        vocals = str(shutil.move(v, out / "source_vocals.wav"))
        if other:
            inst = str(shutil.move(other, out / "instrumental.wav"))
    if s["dereverb"]:
        k += 1
        progress("Poistetaan kaiku laulusta", k, stages, "")
        stems = stems or Stems(s["models_dir"], str(work))
        stems.load(s["dereverb_model"])
        res = stems.run(vocals)
        dry, _ = pick(res, ["noreverb", "dry"], ["reverb"])
        vocals = str(shutil.move(dry, work / "vocals_dry.wav"))
    stems = None
    free_gpu()

    k += 1
    progress("Muunnetaan ääni RVC-mallilla", k, stages, Path(s["pth"]).name)
    from rvc.infer.infer import VoiceConverter
    vc = VoiceConverter()
    conv = out / "vocals.wav"
    vc.convert_audio(
        audio_input_path=vocals, audio_output_path=str(conv), model_path=s["pth"],
        index_path=s["index"], pitch=int(s["pitch"]), f0_method=s["f0_method"],
        index_rate=float(s["index_rate"]), volume_envelope=float(s["rms_mix"]),
        protect=float(s["protect"]), split_audio=False, embedder_model="contentvec",
        export_format="WAV",
    )
    if not conv.exists():
        print("RVC-muunnos ei tuottanut tiedostoa (katso yllä oleva loki).", flush=True)
        return 1
    del vc
    free_gpu()

    outputs = {"vocals": conv.name}
    if s["separate"]:
        outputs["source_vocals"] = "source_vocals.wav"
    if inst:
        k += 1
        progress("Miksataan laulu ja tausta", k, stages, "")
        mix(str(conv), inst, str(out / "mix.flac"), float(s["vocal_gain_db"]), float(s["inst_gain_db"]))
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(out / "mix.flac"),
                        "-b:a", "320k", str(out / "mix.mp3")], check=True)
        outputs.update(mix="mix.mp3", mix_flac="mix.flac", instrumental="instrumental.wav")
    shutil.rmtree(work, ignore_errors=True)
    emit("RESULT", {"outputs": outputs})
    return 0


if __name__ == "__main__":
    sys.exit(main())
