"""Näytteiden generointi ACE-Step 1.5:llä, valinnaisesti koulutetun LoRAn kanssa.

Syöte: JSON-tiedosto (ks. server.py: _sample_job). Tulos: @@RESULT {"files": [...], "seeds": [...]}
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path


def emit(tag: str, obj) -> None:
    print(f"@@{tag} " + json.dumps(obj, ensure_ascii=False), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    spec = json.loads(open(ap.parse_args().spec, encoding="utf-8").read())

    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationConfig, GenerationParams, generate_music

    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / f".tmp_{int(time.time())}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    print(f"[sample] Ladataan malli {spec['model_dir']}…", flush=True)
    dit = AceStepHandler()
    msg, ok = dit.initialize_service(project_root=spec["ace_dir"], config_path=spec["model_dir"],
                                     device="cuda", quantization=None)
    if not ok:
        print(f"[sample] Mallin alustus epäonnistui: {msg}", flush=True)
        return 1

    lora = spec.get("lora_path")
    if lora:
        print(f"[sample] Ladataan LoRA: {lora}", flush=True)
        m = dit.load_lora(lora)
        print(f"[sample] {m}", flush=True)
        if not str(m).startswith("✅"):
            return 1
        print(f"[sample] {dit.set_use_lora(True)}", flush=True)
        print(f"[sample] {dit.set_lora_scale(float(spec.get('lora_scale', 1.0)))}", flush=True)

    instrumental = bool(spec.get("instrumental"))
    params = GenerationParams(
        caption=spec["caption"],
        lyrics="[Instrumental]" if instrumental else spec.get("lyrics", ""),
        instrumental=instrumental,
        vocal_language=spec.get("language") or "unknown",
        duration=float(spec.get("duration", 60)),
        bpm=int(spec["bpm"]) if spec.get("bpm") else None,
        keyscale=spec.get("keyscale") or "",
        inference_steps=int(spec["steps"]),
        shift=float(spec["shift"]),
        guidance_scale=float(spec.get("guidance", 7.0)),
        seed=int(spec.get("seed", -1)),
        thinking=False,
    )
    seed = int(spec.get("seed", -1))
    count = int(spec.get("count", 1))
    config = GenerationConfig(
        batch_size=count,
        audio_format="flac",
        use_random_seed=seed < 0,
        seeds=None if seed < 0 else [seed + i for i in range(count)],
    )
    print(f"[sample] Generoidaan {count} näyte(ttä), {params.duration:.0f} s…", flush=True)
    result = generate_music(dit, None, params, config, save_dir=str(tmp_dir))
    if not result.success:
        print(f"[sample] Generointi epäonnistui: {result.error}", flush=True)
        return 1

    files, seeds = [], []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i, a in enumerate(result.audios):
        src = Path(a["path"])
        dst = out_dir / f"{spec.get('name_prefix', 'sample')}_{stamp}_{i + 1}{src.suffix}"
        shutil.move(str(src), dst)
        files.append(dst.name)
        seeds.append((a.get("params") or {}).get("seed"))
    shutil.rmtree(tmp_dir, ignore_errors=True)
    emit("RESULT", {"files": files, "seeds": seeds})
    return 0


if __name__ == "__main__":
    sys.exit(main())
