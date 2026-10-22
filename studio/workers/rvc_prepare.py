"""Äänimallin datan valmistelu: laulun erottelu, taustalaulujen poisto, kaiun poisto, hiljaisuuksien leikkaus.

Ajetaan Applion Python-ympäristössä. Jokainen erottelumalli ladataan kerran ja ajetaan
kaikille sitä tarvitseville tiedostoille, jotta GPU-muisti riittää.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

from sep_common import Stems, emit, free_gpu, pick, progress, trim_silence

STAGES = [
    # (vaihe, kentät, haluttu stem, vältettävä)
    ("separate", "Erotellaan laulu taustasta", ["vocals"], ["instrument"]),
    ("karaoke", "Poistetaan taustalaulut", ["vocals"], ["instrument"]),
    ("dereverb", "Poistetaan kaiku", ["noreverb", "dry"], ["reverb"]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    spec = json.loads(Path(ap.parse_args().spec).read_text(encoding="utf-8"))
    files = spec["files"]
    work = Path(spec["work_dir"])
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    current = {f["id"]: f["path"] for f in files}
    notes: dict[str, list[str]] = {f["id"]: [] for f in files}
    failed: set[str] = set()

    stems = None
    for stage, label, prefer, avoid in STAGES:
        todo = [f for f in files if f.get(stage) and f["id"] not in failed]
        if not todo:
            continue
        try:
            if stems is None:
                stems = Stems(spec["models_dir"], str(work / "out"))
            stems.load(spec["models"][stage])
        except Exception as exc:
            traceback.print_exc()
            for f in todo:
                notes[f["id"]].append(f"{label.lower()} epäonnistui: {exc}")
                failed.add(f["id"])
            continue
        for i, f in enumerate(todo, 1):
            progress(label, i, len(todo), f["title"])
            try:
                res = stems.run(current[f["id"]])
                keep, _ = pick(res, prefer, avoid)
                dst = work / f"{f['id']}_{stage}.wav"
                shutil.move(keep, dst)
                current[f["id"]] = str(dst)
                for p in res.values():
                    Path(p).unlink(missing_ok=True)
            except Exception as exc:
                traceback.print_exc()
                notes[f["id"]].append(f"{label.lower()} epäonnistui: {exc}")
                failed.add(f["id"])
        free_gpu()

    n = len(files)
    for i, f in enumerate(files, 1):
        progress("Leikataan hiljaisuudet", i, n, f["title"])
        if f["id"] in failed:
            emit("RESULT", {"id": f["id"], "ok": False, "notes": notes[f["id"]]})
            continue
        try:
            total, kept = trim_silence(current[f["id"]], f["out"])
            if kept < 5:
                notes[f["id"]].append(f"vain {kept:.0f} s laulua jäljellä – tarkista, onko tiedostossa laulua")
            elif kept < total * 0.3:
                notes[f["id"]].append(f"laulua {kept:.0f} s / {total:.0f} s (paljon taukoja tai soolo-osuuksia)")
            emit("RESULT", {"id": f["id"], "ok": kept >= 1, "clean_secs": round(kept, 1),
                            "raw_secs": round(total, 1), "notes": notes[f["id"]]})
        except Exception as exc:
            traceback.print_exc()
            emit("RESULT", {"id": f["id"], "ok": False, "notes": notes[f["id"]] + [f"leikkaus epäonnistui: {exc}"]})
            failed.add(f["id"])
    shutil.rmtree(work, ignore_errors=True)
    return 1 if len(failed) == n else 0


if __name__ == "__main__":
    sys.exit(main())
