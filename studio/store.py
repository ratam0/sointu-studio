"""Projektien, kappaleiden, koulutusajojen ja näytteiden tallennus JSON-tiedostoihin."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import PROJECTS_DIR

_lock = threading.RLock()

# Kentät, joita analyysi saa täyttää automaattisesti (ellei käyttäjä ole muokannut niitä)
AUTO_FIELDS = ("caption", "genre", "lyrics", "is_instrumental", "bpm", "keyscale",
               "timesignature", "language")


def _new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:10]


def _slug(text: str) -> str:
    t = text.lower().translate(str.maketrans("äöåéü", "aoaeu"))
    s = re.sub(r"[^a-z0-9]+", "", t)
    return s[:24] or "mystyle"


def _atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def project_dir(pid: str) -> Path:
    if not re.fullmatch(r"p[0-9a-f]{10}", pid):
        raise KeyError(pid)
    return PROJECTS_DIR / pid


def _pfile(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def list_projects() -> list[dict]:
    out = []
    with _lock:
        for f in sorted(PROJECTS_DIR.glob("p*/project.json")):
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "id": p["id"], "name": p["name"], "created": p["created"],
                "tracks": len(p.get("tracks", [])),
                "runs": len(p.get("runs", [])),
                "trigger_tag": p.get("trigger_tag", ""),
            })
    out.sort(key=lambda x: x["created"], reverse=True)
    return out


def create_project(name: str) -> dict:
    pid = _new_id("p")
    d = project_dir(pid)
    for sub in ("audio", "runs", "tensors", "samples"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    p = {
        "id": pid, "name": name.strip() or "Nimetön projekti", "created": time.time(),
        "trigger_tag": _slug(name), "tag_position": "prepend",
        "tracks": [], "runs": [], "samples": [],
    }
    with _lock:
        _atomic_write(_pfile(pid), p)
    return p


def get_project(pid: str) -> dict:
    with _lock:
        f = _pfile(pid)
        if not f.exists():
            raise KeyError(pid)
        return json.loads(f.read_text(encoding="utf-8"))


def save_project(p: dict) -> None:
    with _lock:
        _atomic_write(_pfile(p["id"]), p)


def update_project(pid: str, fn) -> dict:
    """Lukitsee projektin, ajaa fn(p) ja tallentaa."""
    with _lock:
        p = get_project(pid)
        fn(p)
        save_project(p)
        return p


def delete_project(pid: str) -> None:
    with _lock:
        shutil.rmtree(project_dir(pid), ignore_errors=True)


# ------------------------------------------------------------------ kappaleet

def new_track(filename: str, rel_file: str, duration: float) -> dict:
    stem = Path(filename).stem
    return {
        "id": _new_id("t"), "filename": filename, "file": rel_file,
        "duration": round(duration, 2), "include": True,
        "caption": "", "genre": "", "lyrics": "", "is_instrumental": None,
        "bpm": None, "keyscale": "", "timesignature": "4", "language": "unknown",
        "analyzed": {"meta": False, "lyrics": False, "caption": False},
        "edited": [], "notes": [], "title": stem,
    }


def find_track(p: dict, tid: str) -> dict:
    for t in p["tracks"]:
        if t["id"] == tid:
            return t
    raise KeyError(tid)


def apply_analysis(pid: str, tid: str, stage: str, fields: dict, notes: list[str]) -> None:
    def fn(p):
        try:
            t = find_track(p, tid)
        except KeyError:
            return
        for k, v in fields.items():
            if k in AUTO_FIELDS and k not in t.get("edited", []):
                t[k] = v
        t.setdefault("analyzed", {})[stage] = True
        t["notes"] = [n for n in t.get("notes", []) if not n.startswith(f"[{stage}]")]
        t["notes"] += [f"[{stage}] {n}" for n in notes]
    update_project(pid, fn)


def track_ready(t: dict) -> tuple[bool, str]:
    if not t.get("include", True):
        return False, "ei mukana"
    if not t.get("caption"):
        return False, "kuvaus puuttuu"
    if t.get("is_instrumental") is None:
        return False, "laulu/instrumentaali valitsematta"
    if not t.get("is_instrumental") and not t.get("lyrics", "").strip():
        return False, "lyriikat puuttuvat"
    return True, ""


# ------------------------------------------------------------------ dataset.json

def build_dataset_json(p: dict) -> dict:
    d = project_dir(p["id"])
    tag = p.get("trigger_tag", "").strip()
    samples = []
    for t in p["tracks"]:
        ok, _ = track_ready(t)
        if not ok:
            continue
        instr = bool(t.get("is_instrumental"))
        lyrics = "[Instrumental]" if instr else t.get("lyrics", "").strip()
        samples.append({
            "id": t["id"],
            "audio_path": str((d / t["file"]).resolve()),
            "filename": Path(t["file"]).name,
            "caption": t.get("caption", "").strip(),
            "genre": t.get("genre", "").strip(),
            "lyrics": lyrics,
            "raw_lyrics": "" if instr else lyrics,
            "formatted_lyrics": "",
            "bpm": t.get("bpm") or None,
            "keyscale": t.get("keyscale") or "",
            "timesignature": t.get("timesignature") or "4",
            "duration": t.get("duration", 0),
            "language": "unknown" if instr else (t.get("language") or "unknown"),
            "is_instrumental": instr,
            "custom_tag": tag,
            "labeled": True,
            "prompt_override": None,
        })
    return {
        "metadata": {
            "name": p["name"], "custom_tag": tag,
            "tag_position": p.get("tag_position", "prepend"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "num_samples": len(samples),
            "all_instrumental": all(s["is_instrumental"] for s in samples) if samples else True,
            "genre_ratio": 0,
        },
        "samples": samples,
    }


def dataset_hash(ds: dict, variant: str, max_duration: float) -> str:
    payload = json.dumps({"s": ds["samples"], "tag": ds["metadata"]["custom_tag"],
                          "pos": ds["metadata"]["tag_position"], "v": variant,
                          "md": max_duration}, sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


# ------------------------------------------------------------------ ajot ja checkpointit

def run_dir(pid: str, rid: str) -> Path:
    if not re.fullmatch(r"r[0-9a-f]{10}", rid):
        raise KeyError(rid)
    return project_dir(pid) / "runs" / rid


def list_checkpoints(pid: str, rid: str) -> list[dict]:
    """Palauttaa käyttökelpoiset adapterit: final + epoch-checkpointit."""
    rd = run_dir(pid, rid)
    out = []
    final = rd / "final"
    if (final / "adapter_config.json").exists() or (final / "lokr_weights.safetensors").exists():
        out.append({"id": "final", "label": "Valmis (final)", "epoch": None, "loss": None})
    ck = rd / "checkpoints"
    if ck.is_dir():
        items = []
        for c in ck.iterdir():
            m = re.match(r"epoch_(\d+)(?:_loss_([\d.]+))?$", c.name)
            if m and ((c / "adapter_config.json").exists() or (c / "lokr_weights.safetensors").exists()):
                items.append({"id": c.name, "epoch": int(m.group(1)),
                              "loss": float(m.group(2)) if m.group(2) else None,
                              "label": f"Epoch {m.group(1)}" + (f" · loss {m.group(2)}" if m.group(2) else ""),
                              "resumable": (c / "training_state.pt").exists()})
        items.sort(key=lambda x: x["epoch"], reverse=True)
        out += items
    return out


def checkpoint_path(pid: str, rid: str, cid: str) -> Path:
    rd = run_dir(pid, rid)
    if cid == "final":
        return rd / "final"
    if not re.fullmatch(r"epoch_\d+(_loss_[\d.]+)?", cid):
        raise KeyError(cid)
    return rd / "checkpoints" / cid
