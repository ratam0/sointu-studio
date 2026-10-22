"""Äänimallit (RVC / Applio): datan valmistelu, koulutus, mallit ja äänen muunnos."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import store as music_store
from .config import (APPLIO_DIR, APPLIO_LOGS_DIR, APPLIO_PYTHON, AUDIO_EXTS, CONVERSIONS_DIR,
                     DATA_DIR, DEREVERB_MODEL, KARAOKE_MODEL, SEPARATOR_MODELS_DIR, VOCAL_MODEL,
                     VOICES_DIR, WORKERS_DIR, applio_env)
from .jobs import Job, Step, new_job_id
from .runtime import RE_TAG, err, jobs, probe_duration, to_wav

router = APIRouter()
_lock = threading.RLock()


# ======================================================================= tallennus
def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _vdir(vid: str) -> Path:
    if not re.fullmatch(r"v[0-9a-f]{10}", vid):
        raise KeyError(vid)
    return VOICES_DIR / vid


def _slug(text: str) -> str:
    t = text.lower().translate(str.maketrans("äöåéü", "aoaeu"))
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:24] or "aani"


def get_voice(vid: str) -> dict:
    f = _vdir(vid) / "voice.json"
    with _lock:
        if not f.exists():
            raise KeyError(vid)
        return json.loads(f.read_text(encoding="utf-8"))


def update_voice(vid: str, fn) -> dict:
    with _lock:
        v = get_voice(vid)
        fn(v)
        _atomic_write(_vdir(vid) / "voice.json", v)
        return v


def _voice(vid: str) -> dict:
    try:
        return get_voice(vid)
    except KeyError:
        err(404, "Äänimallia ei löydy")


def _file(v: dict, fid: str) -> dict:
    for f in v["files"]:
        if f["id"] == fid:
            return f
    err(404, "Tiedostoa ei löydy")


def _run(v: dict, rid: str) -> dict:
    for r in v.get("runs", []):
        if r["id"] == rid:
            return r
    err(404, "Koulutusajoa ei löydy")


def list_weights(exp: str) -> list[dict]:
    d = APPLIO_LOGS_DIR / exp
    out = []
    if d.is_dir():
        for f in d.glob(f"{exp}_*e_*s.pth"):
            m = re.match(rf"{re.escape(exp)}_(\d+)e_(\d+)s\.pth$", f.name)
            if m:
                out.append({"file": f.name, "epoch": int(m.group(1)), "step": int(m.group(2)),
                            "size_mb": round(f.stat().st_size / 1e6, 1)})
    out.sort(key=lambda x: x["epoch"], reverse=True)
    return out


def index_path(exp: str) -> Optional[Path]:
    p = APPLIO_LOGS_DIR / exp / f"{exp}.index"
    return p if p.exists() else None


def _view(v: dict) -> dict:
    v = dict(v)
    inc = [f for f in v["files"] if f.get("include", True)]
    v["stats"] = {
        "raw_secs": round(sum(f["duration"] for f in inc), 1),
        "clean_secs": round(sum(f.get("clean_secs") or 0 for f in inc if f.get("prepared")), 1),
        "prepared": sum(1 for f in inc if f.get("prepared")),
        "included": len(inc),
    }
    for r in v.get("runs", []):
        r["weights"] = list_weights(r["exp"])
        r["has_index"] = index_path(r["exp"]) is not None
    v["jobs"] = jobs.list(project_id=v["id"], limit=30)
    return v


# ======================================================================= äänet
class VoiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.get("/api/voices")
def voices_list():
    out = []
    for f in sorted(VOICES_DIR.glob("v*/voice.json")):
        try:
            v = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        done = [r for r in v.get("runs", []) if list_weights(r["exp"])]
        out.append({"id": v["id"], "name": v["name"], "created": v["created"],
                    "files": len(v["files"]), "models": len(done)})
    out.sort(key=lambda x: x["created"], reverse=True)
    return out


@router.post("/api/voices")
def voices_create(body: VoiceIn):
    vid = "v" + uuid.uuid4().hex[:10]
    d = _vdir(vid)
    for sub in ("raw", "clean", "runs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    v = {"id": vid, "name": body.name.strip(), "created": time.time(), "files": [], "runs": []}
    with _lock:
        _atomic_write(d / "voice.json", v)
    return _view(v)


@router.get("/api/voices/{vid}")
def voices_get(vid: str):
    return _view(_voice(vid))


@router.patch("/api/voices/{vid}")
def voices_patch(vid: str, body: VoiceIn):
    _voice(vid)
    return _view(update_voice(vid, lambda v: v.__setitem__("name", body.name.strip())))


def _busy(owner_id: str) -> bool:
    return any(j["project_id"] == owner_id and j["status"] in ("running", "queued")
               for j in jobs.list(limit=500))


@router.delete("/api/voices/{vid}")
def voices_delete(vid: str):
    v = _voice(vid)
    if _busy(vid):
        err(409, "Äänimallilla on käynnissä oleva työ. Pysäytä se ensin.")
    for r in v.get("runs", []):
        shutil.rmtree(APPLIO_LOGS_DIR / r["exp"], ignore_errors=True)
    with _lock:
        shutil.rmtree(_vdir(vid), ignore_errors=True)
    return {"ok": True}


# ======================================================================= tiedostot
@router.post("/api/voices/{vid}/upload")
def voice_upload(vid: str, files: list[UploadFile] = File(...)):
    _voice(vid)
    d = _vdir(vid)
    added, errors = [], []
    for uf in files:
        name = Path(uf.filename or "audio").name
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXTS:
            errors.append(f"{name}: tiedostomuotoa {ext or '?'} ei tueta")
            continue
        with tempfile.NamedTemporaryFile(suffix=ext, dir=d, delete=False) as tmp:
            shutil.copyfileobj(uf.file, tmp)
            tmp_path = Path(tmp.name)
        fid = "f" + uuid.uuid4().hex[:10]
        dst = d / "raw" / f"{fid}.wav"
        ok, msg = to_wav(tmp_path, dst, sr=44100, channels=2)
        tmp_path.unlink(missing_ok=True)
        if not ok:
            errors.append(f"{name}: muunnos epäonnistui ({msg})")
            dst.unlink(missing_ok=True)
            continue
        dur = probe_duration(dst)
        if dur < 2:
            errors.append(f"{name}: liian lyhyt")
            dst.unlink(missing_ok=True)
            continue
        added.append({"id": fid, "filename": name, "raw": f"raw/{fid}.wav", "duration": round(dur, 1),
                      "include": True, "separate": True, "karaoke": False, "dereverb": True,
                      "prepared": False, "clean": None, "clean_secs": None, "notes": []})
    update_voice(vid, lambda v: v["files"].extend(added))
    return {"added": len(added), "errors": errors}


class FilePatch(BaseModel):
    include: Optional[bool] = None
    separate: Optional[bool] = None
    karaoke: Optional[bool] = None
    dereverb: Optional[bool] = None


@router.patch("/api/voices/{vid}/files/{fid}")
def voice_file_patch(vid: str, fid: str, body: FilePatch):
    _file(_voice(vid), fid)
    ch = body.model_dump(exclude_unset=True)

    def fn(v):
        f = next(x for x in v["files"] if x["id"] == fid)
        if any(k in ch and ch[k] != f.get(k) for k in ("separate", "karaoke", "dereverb")):
            f["prepared"] = False   # käsittelyasetus muuttui → valmistellaan uudelleen
        f.update(ch)
    return _view(update_voice(vid, fn))


@router.delete("/api/voices/{vid}/files/{fid}")
def voice_file_delete(vid: str, fid: str):
    f = _file(_voice(vid), fid)
    d = _vdir(vid)
    (d / f["raw"]).unlink(missing_ok=True)
    if f.get("clean"):
        (d / f["clean"]).unlink(missing_ok=True)
    update_voice(vid, lambda v: v.__setitem__("files", [x for x in v["files"] if x["id"] != fid]))
    return {"ok": True}


@router.get("/api/voices/{vid}/files/{fid}/audio")
def voice_file_audio(vid: str, fid: str, kind: str = "raw"):
    f = _file(_voice(vid), fid)
    rel = f.get("clean") if kind == "clean" else f["raw"]
    if not rel or not (_vdir(vid) / rel).exists():
        err(404, "Tiedostoa ei ole vielä olemassa")
    return FileResponse(_vdir(vid) / rel, media_type="audio/wav")


# ======================================================================= valmistelu
class PrepareIn(BaseModel):
    file_ids: list[str] = []
    only_missing: bool = True


@router.post("/api/voices/{vid}/prepare")
def voice_prepare(vid: str, body: PrepareIn):
    v = _voice(vid)
    d = _vdir(vid)
    todo = [f for f in v["files"] if f.get("include", True)
            and (not body.file_ids or f["id"] in body.file_ids)
            and not (body.only_missing and f.get("prepared"))]
    if not todo:
        err(400, "Kaikki mukana olevat tiedostot on jo valmisteltu.")
    jid = new_job_id()
    spec = DATA_DIR / "logs" / f"{jid}.spec.json"
    spec.write_text(json.dumps({
        "models_dir": str(SEPARATOR_MODELS_DIR),
        "models": {"separate": VOCAL_MODEL, "karaoke": KARAOKE_MODEL, "dereverb": DEREVERB_MODEL},
        "work_dir": str(d / "work"),
        "files": [{"id": f["id"], "title": f["filename"], "path": str(d / f["raw"]),
                   "out": str(d / "clean" / f"{f['id']}.wav"),
                   "separate": f["separate"], "karaoke": f["karaoke"], "dereverb": f["dereverb"]}
                  for f in todo],
    }, ensure_ascii=False), encoding="utf-8")

    def on_line(job: Job, line: str):
        m = RE_TAG.match(line)
        if not m:
            return
        tag, data = m.group(1), json.loads(m.group(2))
        if tag == "PROGRESS":
            job.progress.update(stage=data["stage"], i=data["i"], n=data["n"], title=data["title"])
        elif tag == "RESULT":
            def fn(v):
                for f in v["files"]:
                    if f["id"] == data["id"]:
                        f["notes"] = data.get("notes", [])
                        if data.get("ok"):
                            f["prepared"] = True
                            f["clean"] = f"clean/{f['id']}.wav"
                            f["clean_secs"] = data["clean_secs"]
            update_voice(vid, fn)

    job = Job(id=jid, kind="voice_prepare", project_id=vid,
              title=f"Datan valmistelu: {v['name']} ({len(todo)} tiedostoa)",
              meta={"files": len(todo)},
              steps=[Step("Erotellaan laulu ja siivotaan",
                          cmd=[APPLIO_PYTHON, str(WORKERS_DIR / "rvc_prepare.py"), str(spec)],
                          cwd=str(APPLIO_DIR), env=applio_env())],
              on_line=on_line)
    jobs.submit(job)
    return job.public()


# ======================================================================= koulutus
class VoiceTrainIn(BaseModel):
    name: str = Field("", max_length=40)
    epochs: int = Field(300, ge=10, le=2000)
    save_every: int = Field(25, ge=1, le=500)
    batch_size: int = Field(8, ge=1, le=32)
    sample_rate: int = 40000
    vocoder: str = "HiFi-GAN"


_RE_PRE = re.compile(r"(\d+)%\|")


def _train_parser(job: Job, line: str):
    pr = job.progress
    m = RE_TAG.match(line)
    if not m:
        return
    tag, data = m.group(1), json.loads(m.group(2))
    if tag == "EPOCH":
        pr.update(epoch=data["epoch"], max_epochs=data["max"], step=data["step"])
        if data.get("secs"):
            times = pr.setdefault("_ep_times", [])
            times.append(data["secs"])
            recent = times[-10:]
            pr["eta_secs"] = int((data["max"] - data["epoch"]) * sum(recent) / len(recent))
    elif tag == "LOSS":
        pr["epochs"] = data["points"][-3000:]
        if data["points"]:
            pr["last_loss"] = round(data["points"][-1][1], 4)
    elif tag == "BEST":
        pr["best"] = data


def _finish_run(vid: str, rid: str):
    def on_finish(job: Job):
        def fn(v):
            for r in v.get("runs", []):
                if r["id"] == rid:
                    r["status"] = job.status
                    r["epochs_done"] = job.progress.get("epoch", r.get("epochs_done", 0))
                    if job.progress.get("epochs"):
                        r["loss_curve"] = job.progress["epochs"][-1000:]
                    if job.progress.get("best"):
                        r["best"] = job.progress["best"]
        job.progress.pop("_ep_times", None)
        update_voice(vid, fn)
    return on_finish


def _mark_running(vid: str, rid: str):
    return lambda job: update_voice(vid, lambda v: [r.update(status="running") for r in v["runs"] if r["id"] == rid])


def _train_steps(vid: str, run: dict, cleanup: bool, epochs: int) -> list[Step]:
    exp = run["exp"]
    p = run["params"]
    spec = _vdir(vid) / "runs" / run["id"] / "train_spec.json"
    spec.write_text(json.dumps({
        "exp": exp, "epochs": epochs, "save_every": p["save_every"], "batch_size": p["batch_size"],
        "sample_rate": p["sample_rate"], "vocoder": p["vocoder"], "cleanup": cleanup, "gpu": "0",
    }), encoding="utf-8")
    return [
        Step(f"Koulutetaan ({epochs} epochia)",
             cmd=[APPLIO_PYTHON, str(WORKERS_DIR / "rvc_train.py"), str(spec)],
             cwd=str(APPLIO_DIR), env=applio_env()),
        Step("Rakennetaan hakuindeksi (.index)",
             cmd=[APPLIO_PYTHON, "rvc/train/process/extract_index.py", str(APPLIO_LOGS_DIR / exp), "Auto"],
             cwd=str(APPLIO_DIR), env=applio_env()),
    ]


@router.post("/api/voices/{vid}/train")
def voice_train(vid: str, body: VoiceTrainIn):
    v = _voice(vid)
    if body.sample_rate not in (32000, 40000, 48000):
        err(400, "Näytetaajuus: 32000, 40000 tai 48000")
    if body.vocoder not in ("HiFi-GAN", "RefineGAN"):
        err(400, "Vokooderi: HiFi-GAN tai RefineGAN")
    if body.vocoder == "RefineGAN" and body.sample_rate == 48000:
        err(400, "RefineGAN tukee vain 32 kHz:n esikoulutettua mallia.")
    ready = [f for f in v["files"] if f.get("include", True) and f.get("prepared") and f.get("clean")]
    if not ready:
        err(400, "Ei valmisteltua dataa. Aja ensin 'Valmistele data'.")
    clean_secs = sum(f.get("clean_secs") or 0 for f in ready)
    if clean_secs < 60:
        err(400, f"Puhdasta laulua on vain {clean_secs:.0f} s. Tarvitaan vähintään minuutti (suositus 10–30 min).")

    rid = "r" + uuid.uuid4().hex[:10]
    exp = f"{_slug(v['name'])}-{rid[1:7]}"
    rdir = _vdir(vid) / "runs" / rid
    ds = rdir / "dataset"
    ds.mkdir(parents=True, exist_ok=True)
    for f in ready:
        dst = ds / f"{f['id']}.wav"
        if not dst.exists():
            os.symlink(_vdir(vid) / f["clean"], dst)
    name = body.name.strip() or f"{_slug(v['name'])}-{time.strftime('%m%d-%H%M')}"
    jid = new_job_id()
    run = {"id": rid, "name": name, "exp": exp, "created": time.time(), "job_id": jid,
           "status": "queued", "params": body.model_dump(), "files": len(ready),
           "clean_secs": round(clean_secs, 1), "epochs_done": 0}
    update_voice(vid, lambda v: v["runs"].insert(0, run))

    cores = str(max(1, min(16, (os.cpu_count() or 2) - 1)))
    exp_dir = str(APPLIO_LOGS_DIR / exp)
    env = applio_env()
    steps = [
        Step("Valmistellaan", fn=_mark_running(vid, rid)),
        Step("Pilkotaan data (esikäsittely)",
             cmd=[APPLIO_PYTHON, "rvc/train/preprocess/preprocess.py", exp_dir, str(ds),
                  str(body.sample_rate), cores, "Automatic", "False", "3.0", "0.3", "none"],
             cwd=str(APPLIO_DIR), env=env),
        Step("Irrotetaan sävelkorkeus (RMVPE) ja piirteet (ContentVec)",
             cmd=[APPLIO_PYTHON, "rvc/train/extract/extract.py", exp_dir, "rmvpe", cores, "0",
                  str(body.sample_rate), "contentvec", "None", "2"],
             cwd=str(APPLIO_DIR), env=env),
        *_train_steps(vid, run, cleanup=True, epochs=body.epochs),
    ]
    job = Job(id=jid, kind="voice_train", project_id=vid,
              title=f"Äänimallin koulutus: {name} ({clean_secs / 60:.1f} min laulua)",
              meta={"run_id": rid, "exp": exp}, steps=steps,
              on_line=_train_parser, on_finish=_finish_run(vid, rid))
    job.progress.update(epoch=0, max_epochs=body.epochs)
    jobs.submit(job)
    return job.public()


class ResumeIn(BaseModel):
    extra_epochs: int = Field(100, ge=1, le=2000)


@router.post("/api/voices/{vid}/runs/{rid}/resume")
def voice_resume(vid: str, rid: str, body: ResumeIn):
    v = _voice(vid)
    run = _run(v, rid)
    if not list(Path(APPLIO_LOGS_DIR / run["exp"]).glob("G_*.pth")):
        err(400, "Ajolla ei ole jatkettavaa checkpointia.")
    done = run.get("epochs_done") or 0
    total = done + body.extra_epochs
    jid = new_job_id()

    def fn(v):
        r = next(x for x in v["runs"] if x["id"] == rid)
        r.update(status="queued", job_id=jid)
        r["params"]["epochs"] = total
    update_voice(vid, fn)
    run = _run(get_voice(vid), rid)
    job = Job(id=jid, kind="voice_train", project_id=vid,
              title=f"Jatkokoulutus: {run['name']} (epoch {done} → {total})",
              meta={"run_id": rid, "exp": run["exp"]},
              steps=[Step("Valmistellaan", fn=_mark_running(vid, rid)),
                     *_train_steps(vid, run, cleanup=False, epochs=total)],
              on_line=_train_parser, on_finish=_finish_run(vid, rid))
    job.progress.update(epoch=done, max_epochs=total,
                        epochs=[p for p in (run.get("loss_curve") or []) if p[0] <= done])
    jobs.submit(job)
    return job.public()


@router.post("/api/voices/{vid}/runs/{rid}/index")
def voice_index(vid: str, rid: str):
    v = _voice(vid)
    run = _run(v, rid)
    job = Job(id=new_job_id(), kind="voice_index", project_id=vid, title=f"Hakuindeksi: {run['name']}",
              steps=[Step("Rakennetaan hakuindeksi",
                          cmd=[APPLIO_PYTHON, "rvc/train/process/extract_index.py",
                               str(APPLIO_LOGS_DIR / run["exp"]), "Auto"],
                          cwd=str(APPLIO_DIR), env=applio_env())])
    jobs.submit(job)
    return job.public()


@router.delete("/api/voices/{vid}/runs/{rid}")
def voice_run_delete(vid: str, rid: str):
    v = _voice(vid)
    run = _run(v, rid)
    try:
        if jobs.get(run["job_id"]).status in ("queued", "running"):
            err(409, "Ajo on käynnissä. Pysäytä se ensin.")
    except KeyError:
        pass
    shutil.rmtree(APPLIO_LOGS_DIR / run["exp"], ignore_errors=True)
    shutil.rmtree(_vdir(vid) / "runs" / rid, ignore_errors=True)
    update_voice(vid, lambda v: v.__setitem__("runs", [r for r in v["runs"] if r["id"] != rid]))
    return {"ok": True}


@router.get("/api/voices/{vid}/runs/{rid}/download/{weight}")
def voice_download(vid: str, rid: str, weight: str):
    v = _voice(vid)
    run = _run(v, rid)
    w = next((x for x in list_weights(run["exp"]) if x["file"] == weight), None)
    if not w:
        err(404, "Mallitiedostoa ei löydy")
    tmpdir = Path(tempfile.mkdtemp(prefix="dl_", dir=DATA_DIR))
    stage = tmpdir / "model"
    stage.mkdir()
    base = f"{_slug(v['name'])}_{w['epoch']}e"
    shutil.copy2(APPLIO_LOGS_DIR / run["exp"] / weight, stage / f"{base}.pth")
    idx = index_path(run["exp"])
    if idx:
        shutil.copy2(idx, stage / f"{base}.index")
    (stage / "info.json").write_text(json.dumps({
        "voice": v["name"], "epoch": w["epoch"], "step": w["step"],
        "sample_rate": run["params"]["sample_rate"], "vocoder": run["params"]["vocoder"],
        "f0_method": "rmvpe", "embedder": "contentvec", "trained_minutes": round(run.get("clean_secs", 0) / 60, 1),
        "applio": "3.7.0",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = shutil.make_archive(str(tmpdir / base), "zip", stage)
    return FileResponse(archive, filename=f"{base}.zip", media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, tmpdir, True))


@router.get("/api/rvc/models")
def rvc_models():
    """Kaikki käyttökelpoiset äänimallit muunnosta varten."""
    out = []
    for f in sorted(VOICES_DIR.glob("v*/voice.json")):
        try:
            v = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in v.get("runs", []):
            for w in list_weights(r["exp"]):
                out.append({"voice_id": v["id"], "voice": v["name"], "run_id": r["id"], "run": r["name"],
                            "weight": w["file"], "epoch": w["epoch"],
                            "best": (r.get("best") or {}).get("epoch"),
                            "has_index": index_path(r["exp"]) is not None})
    return out


# ======================================================================= muunnos (ja ACE → oma ääni)
CONV_FILE = CONVERSIONS_DIR / "conversions.json"
INPUTS_DIR = CONVERSIONS_DIR / "inputs"
INPUTS_DIR.mkdir(parents=True, exist_ok=True)


def _convs() -> list[dict]:
    with _lock:
        if not CONV_FILE.exists():
            return []
        return json.loads(CONV_FILE.read_text(encoding="utf-8"))


def _save_convs(fn):
    with _lock:
        data = _convs()
        fn(data)
        _atomic_write(CONV_FILE, data)


@router.post("/api/conversions/input")
def conversion_input(file: UploadFile = File(...)):
    name = Path(file.filename or "audio").name
    ext = Path(name).suffix.lower()
    if ext not in AUDIO_EXTS:
        err(400, f"Tiedostomuotoa {ext or '?'} ei tueta")
    iid = "i" + uuid.uuid4().hex[:10]
    with tempfile.NamedTemporaryFile(suffix=ext, dir=INPUTS_DIR, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    dst = INPUTS_DIR / f"{iid}.wav"
    ok, msg = to_wav(tmp_path, dst, sr=44100, channels=2)
    tmp_path.unlink(missing_ok=True)
    if not ok:
        err(400, f"Muunnos epäonnistui: {msg}")
    (INPUTS_DIR / f"{iid}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    return {"input_id": iid, "name": name, "duration": round(probe_duration(dst), 1)}


@router.get("/api/conversions/sources")
def conversion_sources():
    """ACE-näytteet, joita voi käyttää muunnoksen lähteenä."""
    out = []
    for p in music_store.list_projects():
        try:
            proj = music_store.get_project(p["id"])
        except KeyError:
            continue
        for s in proj.get("samples", []):
            for f in s.get("files", []):
                out.append({"project_id": p["id"], "project": p["name"], "file": f,
                            "caption": s["caption"], "created": s["created"],
                            "instrumental": s.get("instrumental", False)})
    out.sort(key=lambda x: x["created"], reverse=True)
    return out[:200]


class ConvertIn(BaseModel):
    source: str                          # "upload" | "ace"
    input_id: Optional[str] = None
    project_id: Optional[str] = None
    file: Optional[str] = None
    voice_id: str
    run_id: str
    weight: str
    separate: bool = True                # erota laulu taustasta ennen muunnosta
    dereverb: bool = False
    pitch: int = Field(0, ge=-24, le=24)
    index_rate: float = Field(0.5, ge=0, le=1)
    protect: float = Field(0.33, ge=0, le=0.5)
    rms_mix: float = Field(1.0, ge=0, le=1)
    f0_method: str = "rmvpe"
    vocal_gain_db: float = Field(0, ge=-20, le=20)
    inst_gain_db: float = Field(0, ge=-20, le=20)


@router.post("/api/conversions")
def conversion_create(body: ConvertIn):
    if body.source == "upload":
        if not body.input_id or not re.fullmatch(r"i[0-9a-f]{10}", body.input_id):
            err(400, "Lataa ensin äänitiedosto")
        src = INPUTS_DIR / f"{body.input_id}.wav"
        meta = json.loads((INPUTS_DIR / f"{body.input_id}.json").read_text(encoding="utf-8")) \
            if (INPUTS_DIR / f"{body.input_id}.json").exists() else {}
        src_label = meta.get("name", "ladattu tiedosto")
    elif body.source == "ace":
        try:
            pdir = music_store.project_dir(body.project_id or "")
            proj = music_store.get_project(body.project_id)
        except KeyError:
            err(404, "Musiikkiprojektia ei löydy")
        if not body.file or not re.fullmatch(r"[\w.\-]+", body.file):
            err(400, "Virheellinen tiedosto")
        src = pdir / "samples" / body.file
        sample = next((s for s in proj.get("samples", []) if body.file in s.get("files", [])), None)
        src_label = f"{proj['name']}: {sample['caption'][:60] if sample else body.file}"
    else:
        err(400, "Tuntematon lähde")
    if not src.exists():
        err(404, "Lähdetiedostoa ei löydy")
    if body.f0_method not in ("rmvpe", "fcpe", "crepe"):
        err(400, "Tuntematon sävelkorkeusmenetelmä")

    v = _voice(body.voice_id)
    run = _run(v, body.run_id)
    if not (APPLIO_LOGS_DIR / run["exp"] / body.weight).exists() or "/" in body.weight:
        err(404, "Mallitiedostoa ei löydy")
    idx = index_path(run["exp"])

    cid = "c" + uuid.uuid4().hex[:10]
    out_dir = CONVERSIONS_DIR / cid
    out_dir.mkdir(parents=True, exist_ok=True)
    jid = new_job_id()
    spec = DATA_DIR / "logs" / f"{jid}.spec.json"
    spec.write_text(json.dumps({
        "input": str(src), "out_dir": str(out_dir), "models_dir": str(SEPARATOR_MODELS_DIR),
        "vocal_model": VOCAL_MODEL, "dereverb_model": DEREVERB_MODEL,
        "separate": body.separate, "dereverb": body.dereverb,
        "pth": str(APPLIO_LOGS_DIR / run["exp"] / body.weight), "index": str(idx) if idx else "",
        "pitch": body.pitch, "index_rate": body.index_rate if idx else 0.0, "protect": body.protect,
        "rms_mix": body.rms_mix, "f0_method": body.f0_method,
        "vocal_gain_db": body.vocal_gain_db, "inst_gain_db": body.inst_gain_db,
    }, ensure_ascii=False), encoding="utf-8")
    epoch = re.search(r"_(\d+)e_", body.weight)
    rec = {"id": cid, "created": time.time(), "source": body.source, "source_label": src_label,
           "voice": v["name"], "voice_id": v["id"], "run": run["name"],
           "epoch": int(epoch.group(1)) if epoch else None,
           "params": body.model_dump(exclude={"input_id"}), "outputs": {}, "job_id": jid, "status": "queued"}
    _save_convs(lambda d: d.insert(0, rec))

    def on_line(job: Job, line: str):
        m = RE_TAG.match(line)
        if not m:
            return
        tag, data = m.group(1), json.loads(m.group(2))
        if tag == "PROGRESS":
            job.progress.update(stage=data["stage"], i=data["i"], n=data["n"], title=data["title"])
        elif tag == "RESULT":
            rec["outputs"] = data["outputs"]

    def on_finish(job: Job):
        def fn(d):
            for r in d:
                if r["id"] == cid:
                    r["status"] = job.status
                    r["outputs"] = rec["outputs"]
        _save_convs(fn)

    title = "ACE → oma ääni" if body.source == "ace" else "Äänen muunnos"
    job = Job(id=jid, kind="convert", project_id="convert", title=f"{title}: {v['name']} ← {src_label[:50]}",
              meta={"conversion_id": cid},
              steps=[Step("Muunnetaan",
                          cmd=[APPLIO_PYTHON, str(WORKERS_DIR / "rvc_convert.py"), str(spec)],
                          cwd=str(APPLIO_DIR), env=applio_env())],
              on_line=on_line, on_finish=on_finish)
    jobs.submit(job)
    return job.public()


@router.get("/api/conversions")
def conversion_list():
    return _convs()


@router.get("/api/conversions/{cid}/files/{name}")
def conversion_file(cid: str, name: str):
    if not re.fullmatch(r"c[0-9a-f]{10}", cid) or not re.fullmatch(r"[\w.\-]+", name):
        err(400, "Virheellinen nimi")
    f = CONVERSIONS_DIR / cid / name
    if not f.exists():
        err(404, "Tiedostoa ei löydy")
    return FileResponse(f, filename=name)


@router.delete("/api/conversions/{cid}")
def conversion_delete(cid: str):
    if not re.fullmatch(r"c[0-9a-f]{10}", cid):
        err(400, "Virheellinen tunniste")
    shutil.rmtree(CONVERSIONS_DIR / cid, ignore_errors=True)
    _save_convs(lambda d: d.__setitem__(slice(None), [r for r in d if r["id"] != cid]))
    return {"ok": True}


# ======================================================================= käynnistys
def _reconcile() -> None:
    for f in VOICES_DIR.glob("v*/voice.json"):
        vid = f.parent.name
        try:
            def fn(v):
                for r in v.get("runs", []):
                    if r.get("status") in ("queued", "running"):
                        try:
                            j = jobs.get(r.get("job_id", ""))
                            if j.status not in ("queued", "running"):
                                r["status"] = j.status
                        except KeyError:
                            r["status"] = "interrupted"
            update_voice(vid, fn)
        except Exception:
            continue

    def cfn(d):
        for r in d:
            if r.get("status") in ("queued", "running"):
                try:
                    j = jobs.get(r["job_id"])
                    if j.status not in ("queued", "running"):
                        r["status"] = j.status
                except KeyError:
                    r["status"] = "interrupted"
    if CONV_FILE.exists():
        _save_convs(cfn)


_reconcile()
