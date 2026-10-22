"""Sointu Studio – FastAPI-palvelin (musiikkiosio + yhteiset reitit)."""
from __future__ import annotations

import base64
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import store
from .config import (ACE_DIR, AUDIO_EXTS, CAPTION_LM, CHECKPOINTS_DIR, DATA_DIR, PASSWORD,
                     PYTHON, VARIANTS, WEB_DIR, WHISPER_LD_LIBRARY_PATH, WHISPER_MODEL,
                     WORKERS_DIR, available_variants)
from .jobs import Job, Step, new_job_id
from .runtime import RE_TAG as _RE_TAG, err as _err, jobs, probe_duration as _probe_duration

app = FastAPI(title="Sointu Studio", docs_url=None, redoc_url=None)


def _reconcile_runs() -> None:
    """Uudelleenkäynnistyksen jälkeen kesken jääneet ajot merkitään keskeytyneiksi."""
    for p in store.list_projects():
        def fn(proj):
            for r in proj.get("runs", []):
                if r.get("status") in ("queued", "running"):
                    try:
                        j = jobs.get(r.get("job_id", ""))
                        if j.status in ("queued", "running"):
                            continue
                        r["status"] = j.status
                        r["epochs_done"] = j.progress.get("epoch", r.get("epochs_done", 0))
                    except KeyError:
                        r["status"] = "interrupted"
        store.update_project(p["id"], fn)


_reconcile_runs()


# ======================================================================= auth
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if PASSWORD and request.url.path != "/healthz":
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = secrets.compare_digest(pw.encode(), PASSWORD.encode())
            except Exception:
                ok = False
        if not ok:
            return Response("Kirjautuminen vaaditaan", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Sointu Studio", charset="UTF-8"'})
    return await call_next(request)


def _project(pid: str) -> dict:
    try:
        return store.get_project(pid)
    except KeyError:
        _err(404, "Projektia ei löydy")


# ======================================================================= tila
_gpu_cache: dict = {"t": 0, "v": None}


def _gpu_info() -> Optional[dict]:
    if time.time() - _gpu_cache["t"] < 3:
        return _gpu_cache["v"]
    info = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
        name, used, total, util, temp = [x.strip() for x in out.splitlines()[0].split(",")]
        info = {"name": name, "mem_used": int(used), "mem_total": int(total),
                "util": int(util), "temp": int(temp)}
    except Exception:
        info = None
    _gpu_cache.update(t=time.time(), v=info)
    return info


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/status")
def status():
    active = jobs.active()
    du = shutil.disk_usage(DATA_DIR)
    return {
        "gpu": _gpu_info(),
        "variants": available_variants(),
        "active_job": active.public(light=True) if active else None,
        "queued": sum(1 for j in jobs.list(limit=500) if j["status"] == "queued"),
        "disk_free_gb": round(du.free / 1e9, 1),
        "ace_installed": (ACE_DIR / "acestep").is_dir(),
        "whisper_model": WHISPER_MODEL,
    }


# ======================================================================= projektit
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ProjectPatch(BaseModel):
    name: Optional[str] = Field(None, max_length=80)
    trigger_tag: Optional[str] = Field(None, max_length=40)
    tag_position: Optional[str] = None


@app.get("/api/projects")
def projects_list():
    return store.list_projects()


@app.post("/api/projects")
def projects_create(body: ProjectIn):
    return store.create_project(body.name)


def _project_view(p: dict) -> dict:
    for t in p["tracks"]:
        ok, why = store.track_ready(t)
        t["ready"], t["ready_reason"] = ok, why
    for r in p.get("runs", []):
        try:
            r["checkpoints"] = store.list_checkpoints(p["id"], r["id"])
        except KeyError:
            r["checkpoints"] = []
    p["jobs"] = jobs.list(project_id=p["id"], limit=30)
    return p


@app.get("/api/projects/{pid}")
def projects_get(pid: str):
    return _project_view(_project(pid))


@app.patch("/api/projects/{pid}")
def projects_patch(pid: str, body: ProjectPatch):
    _project(pid)

    def fn(p):
        if body.name is not None:
            p["name"] = body.name.strip() or p["name"]
        if body.trigger_tag is not None:
            p["trigger_tag"] = re.sub(r"[^\w\- ]", "", body.trigger_tag).strip()
        if body.tag_position in ("prepend", "append", "replace"):
            p["tag_position"] = body.tag_position
    return _project_view(store.update_project(pid, fn))


@app.delete("/api/projects/{pid}")
def projects_delete(pid: str):
    _project(pid)
    active = jobs.active()
    if active and active.project_id == pid:
        _err(409, "Projektilla on käynnissä oleva työ. Pysäytä se ensin.")
    store.delete_project(pid)
    return {"ok": True}


# ======================================================================= kappaleet
@app.post("/api/projects/{pid}/upload")
def tracks_upload(pid: str, files: list[UploadFile] = File(...)):
    _project(pid)
    pdir = store.project_dir(pid)
    added, errors = [], []
    for uf in files:
        name = Path(uf.filename or "audio").name
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXTS:
            errors.append(f"{name}: tiedostomuotoa {ext or '?'} ei tueta")
            continue
        with tempfile.NamedTemporaryFile(suffix=ext, dir=pdir, delete=False) as tmp:
            shutil.copyfileobj(uf.file, tmp)
            tmp_path = Path(tmp.name)
        tid_file = f"audio/{uuid.uuid4().hex[:10]}.wav"
        dst = pdir / tid_file
        # 48 kHz stereo WAV: ACE-Stepin sisäinen formaatti, poistaa formaattiongelmat
        proc = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(tmp_path),
                               "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(dst)],
                              capture_output=True, text=True, timeout=600)
        tmp_path.unlink(missing_ok=True)
        if proc.returncode != 0 or not dst.exists():
            errors.append(f"{name}: muunnos epäonnistui ({proc.stderr.strip()[:200]})")
            dst.unlink(missing_ok=True)
            continue
        dur = _probe_duration(dst)
        if dur < 5:
            errors.append(f"{name}: liian lyhyt ({dur:.1f} s)")
            dst.unlink(missing_ok=True)
            continue
        added.append(store.new_track(name, tid_file, dur))

    def fn(p):
        p["tracks"].extend(added)
    store.update_project(pid, fn)
    return {"added": len(added), "errors": errors}


class TrackPatch(BaseModel):
    title: Optional[str] = None
    include: Optional[bool] = None
    caption: Optional[str] = Field(None, max_length=512)
    genre: Optional[str] = Field(None, max_length=200)
    lyrics: Optional[str] = Field(None, max_length=4096)
    is_instrumental: Optional[bool] = None
    bpm: Optional[int] = Field(None, ge=0, le=300)
    keyscale: Optional[str] = Field(None, max_length=20)
    timesignature: Optional[str] = Field(None, max_length=5)
    language: Optional[str] = Field(None, max_length=10)


@app.patch("/api/projects/{pid}/tracks/{tid}")
def tracks_patch(pid: str, tid: str, body: TrackPatch):
    _project(pid)
    changes = body.model_dump(exclude_unset=True)

    def fn(p):
        t = store.find_track(p, tid)
        for k, v in changes.items():
            if k == "bpm" and v == 0:
                v = None
            t[k] = v
            if k in store.AUTO_FIELDS and k not in t["edited"]:
                t["edited"].append(k)
    try:
        p = store.update_project(pid, fn)
    except KeyError:
        _err(404, "Kappaletta ei löydy")
    t = store.find_track(p, tid)
    t["ready"], t["ready_reason"] = store.track_ready(t)
    return t


@app.delete("/api/projects/{pid}/tracks/{tid}")
def tracks_delete(pid: str, tid: str):
    p = _project(pid)
    try:
        t = store.find_track(p, tid)
    except KeyError:
        _err(404, "Kappaletta ei löydy")
    (store.project_dir(pid) / t["file"]).unlink(missing_ok=True)
    store.update_project(pid, lambda p: p.__setitem__("tracks", [x for x in p["tracks"] if x["id"] != tid]))
    return {"ok": True}


@app.get("/api/projects/{pid}/tracks/{tid}/audio")
def tracks_audio(pid: str, tid: str):
    p = _project(pid)
    try:
        t = store.find_track(p, tid)
    except KeyError:
        _err(404, "Kappaletta ei löydy")
    return FileResponse(store.project_dir(pid) / t["file"], media_type="audio/wav")


# ======================================================================= analyysi
class AnalyzeIn(BaseModel):
    track_ids: list[str] = []
    stages: list[str] = ["meta", "lyrics", "caption"]
    only_missing: bool = True
    language: Optional[str] = None


@app.post("/api/projects/{pid}/analyze")
def analyze(pid: str, body: AnalyzeIn):
    p = _project(pid)
    stages = [s for s in ("meta", "lyrics", "caption") if s in body.stages]
    if not stages:
        _err(400, "Valitse vähintään yksi analyysivaihe")
    pdir = store.project_dir(pid)
    spec_tracks = []
    for t in p["tracks"]:
        if body.track_ids and t["id"] not in body.track_ids:
            continue
        todo = [s for s in stages if not (body.only_missing and t.get("analyzed", {}).get(s))]
        if todo:
            spec_tracks.append({"id": t["id"], "path": str((pdir / t["file"]).resolve()),
                                "title": t.get("title") or t["filename"], "stages": todo})
    if not spec_tracks:
        _err(400, "Ei analysoitavaa – kaikki valitut vaiheet on jo tehty.")

    jid = new_job_id()
    spec_path = DATA_DIR / "logs" / f"{jid}.spec.json"
    spec_path.write_text(json.dumps({
        "tracks": spec_tracks, "stages": stages, "whisper_model": WHISPER_MODEL,
        "caption_lm": CAPTION_LM, "language": body.language or None,
        "ace_dir": str(ACE_DIR), "checkpoints_dir": str(CHECKPOINTS_DIR),
    }, ensure_ascii=False), encoding="utf-8")

    def on_line(job: Job, line: str):
        m = _RE_TAG.match(line)
        if not m:
            return
        tag, data = m.group(1), json.loads(m.group(2))
        if tag == "STAGE":
            job.progress.update(stage=data["stage"], i=0, n=data["n"], title="")
        elif tag == "PROGRESS":
            job.progress.update(stage=data["stage"], i=data["i"], n=data["n"], title=data["title"])
        elif tag == "RESULT":
            if data.get("failed"):
                store.update_project(pid, lambda p: _add_note(p, data["track_id"], data["stage"], data["notes"]))
            else:
                store.apply_analysis(pid, data["track_id"], data["stage"], data["fields"], data["notes"])

    job = Job(id=jid, kind="analyze", project_id=pid,
              title=f"Analyysi: {len(spec_tracks)} kappaletta ({', '.join(stages)})",
              meta={"stages": stages, "tracks": len(spec_tracks)},
              steps=[Step("Analysoidaan kappaleet", cmd=[PYTHON, str(WORKERS_DIR / "analyze.py"), str(spec_path)],
                          env={"LD_LIBRARY_PATH": WHISPER_LD_LIBRARY_PATH or None})],
              on_line=on_line)
    jobs.submit(job)
    return job.public()


def _add_note(p: dict, tid: str, stage: str, notes: list[str]):
    try:
        t = store.find_track(p, tid)
    except KeyError:
        return
    t["notes"] = [n for n in t.get("notes", []) if not n.startswith(f"[{stage}]")] + [f"[{stage}] {n}" for n in notes]


# ======================================================================= koulutus
class TrainIn(BaseModel):
    name: str = Field("", max_length=60)
    variant: str = "base"
    epochs: int = Field(200, ge=1, le=5000)
    lr: float = Field(1e-4, gt=0, le=1.0)
    rank: int = Field(64, ge=4, le=256)
    alpha: int = Field(128, ge=1, le=512)
    dropout: float = Field(0.1, ge=0, le=0.5)
    batch_size: int = Field(1, ge=1, le=8)
    grad_accum: int = Field(4, ge=1, le=32)
    save_every: int = Field(25, ge=1, le=1000)
    attention: str = "both"
    vram_profile: str = "normal"      # saver | normal | fast
    max_duration: float = Field(240, ge=30, le=600)
    seed: int = 42


_RE_STEP = re.compile(r"Epoch (\d+)/(\d+), Step (\d+), Loss: ([\d.eE+-]+|nan)")
_RE_EPOCH = re.compile(r"\[OK\] Epoch (\d+)/(\d+) in ([\d.]+)s, Loss: ([\d.eE+-]+|nan)")


def _train_line_parser(job: Job, line: str):
    pr = job.progress
    m = _RE_STEP.search(line)
    if m:
        ep, mx, st, loss = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        pr.update(epoch=ep, max_epochs=mx, step=st)
        if loss != "nan":
            pts = pr.setdefault("steps", [])
            pts.append([st, round(float(loss), 5)])
            if len(pts) > 4000:
                del pts[::2]
        return
    m = _RE_EPOCH.search(line)
    if m:
        ep, mx, secs, loss = int(m.group(1)), int(m.group(2)), float(m.group(3)), m.group(4)
        pr.update(epoch=ep, max_epochs=mx, epoch_secs=secs)
        if loss != "nan":
            pr.setdefault("epochs", []).append([ep, round(float(loss), 5)])
            pr["last_loss"] = round(float(loss), 5)
        times = pr.setdefault("_ep_times", [])
        times.append(secs)
        recent = times[-10:]
        pr["eta_secs"] = int((mx - ep) * sum(recent) / len(recent))
        return
    if "Preprocess" in line or "[OK] Preprocessing" in line:
        pr["stage"] = "preprocess"


def _variant_or_400(variant: str) -> dict:
    v = VARIANTS.get(variant)
    if not v:
        _err(400, f"Tuntematon malli: {variant}")
    if not (CHECKPOINTS_DIR / v["dir"]).is_dir():
        _err(400, f"Mallia {variant} ei ole ladattu. Aja: uv run acestep-download --model {v['dir']} --skip-main")
    return v


def _train_cmd(variant: str, v: dict, tensors: Path, out: Path, b: TrainIn, epochs: int,
               resume: Optional[Path] = None) -> list[str]:
    cmd = [PYTHON, "-m", "acestep.training_v2.cli.train_fixed", "--plain", "--yes",
           "--checkpoint-dir", str(CHECKPOINTS_DIR), "--model-variant", variant,
           "--dataset-dir", str(tensors), "--output-dir", str(out),
           "--epochs", str(epochs), "--lr", str(b.lr), "--batch-size", str(b.batch_size),
           "--gradient-accumulation", str(b.grad_accum), "--adapter-type", "lora",
           "--rank", str(b.rank), "--alpha", str(b.alpha), "--dropout", str(b.dropout),
           "--attention-type", b.attention, "--save-every", str(b.save_every),
           "--shift", str(v["shift"]), "--num-inference-steps", str(v["steps"]),
           "--seed", str(b.seed), "--log-every", "5"]
    if b.vram_profile == "saver":
        cmd += ["--optimizer-type", "adamw8bit", "--offload-encoder", "--gradient-checkpointing"]
    elif b.vram_profile == "fast":
        cmd += ["--optimizer-type", "adamw", "--no-gradient-checkpointing"]
    else:
        cmd += ["--optimizer-type", "adamw", "--gradient-checkpointing"]
    if resume:
        cmd += ["--resume-from", str(resume)]
    return cmd


def _finish_run(pid: str, rid: str):
    def on_finish(job: Job):
        def fn(p):
            for r in p.get("runs", []):
                if r["id"] == rid:
                    r["status"] = job.status
                    r["finished"] = job.finished
                    r["last_loss"] = job.progress.get("last_loss")
                    r["epochs_done"] = job.progress.get("epoch", r.get("epochs_done", 0))
                    r["loss_curve"] = job.progress.get("epochs", [])[-1000:]
        job.progress.pop("_ep_times", None)
        store.update_project(pid, fn)
    return on_finish


@app.post("/api/projects/{pid}/train")
def train(pid: str, body: TrainIn):
    p = _project(pid)
    v = _variant_or_400(body.variant)
    ds = store.build_dataset_json(p)
    n = len(ds["samples"])
    if n == 0:
        _err(400, "Yksikään kappale ei ole valmis koulutukseen. Täytä kuvaus ja lyriikat (tai merkitse instrumentaaliksi).")
    pdir = store.project_dir(pid)
    h = store.dataset_hash(ds, body.variant, body.max_duration)
    tensors = pdir / "tensors" / h
    tensors.mkdir(parents=True, exist_ok=True)
    (tensors / "dataset.json").write_text(json.dumps(ds, ensure_ascii=False, indent=2), encoding="utf-8")
    marker = tensors / ".complete"

    rid = "r" + uuid.uuid4().hex[:10]
    out = store.run_dir(pid, rid)
    out.mkdir(parents=True, exist_ok=True)
    (out / "dataset.json").write_text(json.dumps(ds, ensure_ascii=False, indent=2), encoding="utf-8")
    name = body.name.strip() or f"{p['trigger_tag'] or 'lora'}-{time.strftime('%m%d-%H%M')}"

    def mark_done(job: Job):
        if any(not f.name.endswith(".tmp.pt") for f in tensors.glob("*.pt")):
            marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            raise RuntimeError("Esikäsittely ei tuottanut yhtään tensoritiedostoa. Katso loki.")

    pre_cmd = [PYTHON, "-m", "acestep.training_v2.cli.train_fixed", "--plain", "--preprocess",
               "--checkpoint-dir", str(CHECKPOINTS_DIR), "--model-variant", body.variant,
               "--dataset-json", str(tensors / "dataset.json"), "--tensor-output", str(tensors),
               "--max-duration", str(body.max_duration)]
    steps = [
        Step("Esikäsittely (VAE-latentit + tekstienkoodaus)", cmd=pre_cmd,
             skip_if=marker.exists, on_success=mark_done),
        Step(f"LoRA-koulutus ({body.epochs} epochia)",
             cmd=_train_cmd(body.variant, v, tensors, out, body, body.epochs)),
    ]
    jid = new_job_id()
    run = {"id": rid, "name": name, "created": time.time(), "variant": body.variant,
           "params": body.model_dump(), "samples": n, "tensors": h, "job_id": jid,
           "status": "queued", "trigger_tag": ds["metadata"]["custom_tag"],
           "shift": v["shift"], "steps": v["steps"]}
    store.update_project(pid, lambda p: p.setdefault("runs", []).insert(0, run))
    job = Job(id=jid, kind="train", project_id=pid, title=f"Koulutus: {name} ({n} kappaletta, {body.variant})",
              meta={"run_id": rid, "variant": body.variant, "samples": n},
              steps=steps, on_line=_train_line_parser, on_finish=_finish_run(pid, rid))
    job.progress.update(epoch=0, max_epochs=body.epochs)

    def on_start_status(job: Job):
        store.update_project(pid, lambda p: [r.update(status="running") for r in p["runs"] if r["id"] == rid])
    job.steps.insert(0, Step("Valmistellaan", fn=on_start_status))
    jobs.submit(job)
    return job.public()


class ResumeIn(BaseModel):
    extra_epochs: int = Field(100, ge=1, le=5000)


@app.post("/api/projects/{pid}/runs/{rid}/resume")
def run_resume(pid: str, rid: str, body: ResumeIn):
    p = _project(pid)
    run = next((r for r in p.get("runs", []) if r["id"] == rid), None)
    if not run:
        _err(404, "Ajoa ei löydy")
    cks = [c for c in store.list_checkpoints(pid, rid) if c.get("resumable")]
    if not cks:
        _err(400, "Ajolla ei ole jatkettavaa checkpointia (training_state.pt puuttuu).")
    last = cks[0]
    v = _variant_or_400(run["variant"])
    b = TrainIn(**run["params"])
    tensors = store.project_dir(pid) / "tensors" / run["tensors"]
    if not (tensors / ".complete").exists():
        _err(400, "Ajon esikäsitellyt tensorit puuttuvat.")
    total = last["epoch"] + body.extra_epochs
    out = store.run_dir(pid, rid)
    jid = new_job_id()
    cmd = _train_cmd(run["variant"], v, tensors, out, b, total, resume=store.checkpoint_path(pid, rid, last["id"]))

    def fn(p):
        for r in p["runs"]:
            if r["id"] == rid:
                r["status"] = "queued"
                r["job_id"] = jid
                r["params"]["epochs"] = total
    store.update_project(pid, fn)
    job = Job(id=jid, kind="train", project_id=pid,
              title=f"Jatkokoulutus: {run['name']} (epoch {last['epoch']} → {total})",
              meta={"run_id": rid, "variant": run["variant"], "resume_from": last["epoch"]},
              steps=[Step("Valmistellaan", fn=lambda j: store.update_project(
                         pid, lambda p: [r.update(status="running") for r in p["runs"] if r["id"] == rid])),
                     Step(f"Jatketaan koulutusta epochiin {total}", cmd=cmd)],
              on_line=_train_line_parser, on_finish=_finish_run(pid, rid))
    job.progress.update(epoch=last["epoch"], max_epochs=total,
                        epochs=[e for e in (run.get("loss_curve") or []) if e[0] <= last["epoch"]])
    jobs.submit(job)
    return job.public()


@app.delete("/api/projects/{pid}/runs/{rid}")
def run_delete(pid: str, rid: str):
    p = _project(pid)
    run = next((r for r in p.get("runs", []) if r["id"] == rid), None)
    if not run:
        _err(404, "Ajoa ei löydy")
    try:
        j = jobs.get(run.get("job_id", ""))
        if j.status in ("queued", "running"):
            _err(409, "Ajo on käynnissä. Pysäytä se ensin.")
    except KeyError:
        pass
    shutil.rmtree(store.run_dir(pid, rid), ignore_errors=True)
    store.update_project(pid, lambda p: p.__setitem__("runs", [r for r in p["runs"] if r["id"] != rid]))
    return {"ok": True}


@app.get("/api/projects/{pid}/runs/{rid}/download/{cid}")
def run_download(pid: str, rid: str, cid: str):
    p = _project(pid)
    run = next((r for r in p.get("runs", []) if r["id"] == rid), None)
    if not run:
        _err(404, "Ajoa ei löydy")
    try:
        src = store.checkpoint_path(pid, rid, cid)
    except KeyError:
        _err(404, "Checkpointia ei löydy")
    if not src.is_dir():
        _err(404, "Checkpointia ei löydy")
    tmpdir = Path(tempfile.mkdtemp(prefix="dl_", dir=DATA_DIR))
    stage = tmpdir / "lora"
    stage.mkdir()
    for f in src.iterdir():
        if f.is_file() and f.name != "training_state.pt":
            shutil.copy2(f, stage / f.name)
    info = {
        "name": run["name"], "base_model": VARIANTS[run["variant"]]["dir"], "checkpoint": cid,
        "trigger_tag": run.get("trigger_tag", ""), "recommended_shift": run.get("shift"),
        "recommended_inference_steps": run.get("steps"), "lora_rank": run["params"]["rank"],
        "lora_alpha": run["params"]["alpha"], "trained_on_tracks": run.get("samples"),
    }
    (stage / "studio_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    safe = re.sub(r"[^\w\-]+", "_", f"{run['name']}_{cid}")
    archive = shutil.make_archive(str(tmpdir / safe), "zip", stage)
    return FileResponse(archive, filename=f"{safe}.zip", media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, tmpdir, True))


# ======================================================================= näytteet
class SampleIn(BaseModel):
    run_id: Optional[str] = None
    checkpoint: Optional[str] = "final"
    variant: Optional[str] = None
    caption: str = Field(min_length=1, max_length=512)
    lyrics: str = Field("", max_length=4096)
    instrumental: bool = False
    language: str = "unknown"
    duration: float = Field(60, ge=10, le=600)
    bpm: Optional[int] = Field(None, ge=30, le=300)
    keyscale: str = ""
    seed: int = -1
    count: int = Field(1, ge=1, le=4)
    lora_scale: float = Field(1.0, ge=0, le=2)
    guidance: float = Field(7.0, ge=1, le=15)
    add_trigger: bool = True


@app.post("/api/projects/{pid}/sample")
def sample(pid: str, body: SampleIn):
    p = _project(pid)
    lora_path, run = None, None
    if body.run_id:
        run = next((r for r in p.get("runs", []) if r["id"] == body.run_id), None)
        if not run:
            _err(404, "Ajoa ei löydy")
        try:
            lora_path = store.checkpoint_path(pid, body.run_id, body.checkpoint or "final")
        except KeyError:
            _err(404, "Checkpointia ei löydy")
        if not lora_path.is_dir():
            _err(400, "Valittua checkpointia ei ole vielä tallennettu.")
    variant = body.variant or (run["variant"] if run else "turbo")
    v = _variant_or_400(variant)
    caption = body.caption.strip()
    tag = (run or {}).get("trigger_tag") or p.get("trigger_tag", "")
    if run and body.add_trigger and tag and tag.lower() not in caption.lower():
        caption = f"{tag}, {caption}"
    sid = "s" + uuid.uuid4().hex[:8]
    jid = new_job_id()
    out_dir = store.project_dir(pid) / "samples"
    spec_path = DATA_DIR / "logs" / f"{jid}.spec.json"
    spec_path.write_text(json.dumps({
        "ace_dir": str(ACE_DIR), "model_dir": v["dir"], "out_dir": str(out_dir),
        "lora_path": str(lora_path) if lora_path else None, "lora_scale": body.lora_scale,
        "caption": caption, "lyrics": body.lyrics, "instrumental": body.instrumental,
        "language": body.language, "duration": body.duration, "bpm": body.bpm,
        "keyscale": body.keyscale, "seed": body.seed, "count": body.count,
        "steps": v["steps"], "shift": v["shift"], "guidance": body.guidance, "name_prefix": sid,
    }, ensure_ascii=False), encoding="utf-8")

    record = {"id": sid, "created": time.time(), "caption": caption, "lyrics": body.lyrics,
              "instrumental": body.instrumental, "duration": body.duration, "variant": variant,
              "run_id": body.run_id, "run_name": run["name"] if run else None,
              "checkpoint": body.checkpoint if run else None, "lora_scale": body.lora_scale,
              "files": [], "seeds": [], "job_id": jid}

    def on_line(job: Job, line: str):
        m = _RE_TAG.match(line)
        if m and m.group(1) == "RESULT":
            data = json.loads(m.group(2))
            record["files"], record["seeds"] = data["files"], data["seeds"]

    def on_finish(job: Job):
        if record["files"]:
            store.update_project(pid, lambda p: p.setdefault("samples", []).insert(0, record))

    label = f"{run['name']} / {body.checkpoint}" if run else f"perusmalli ({variant})"
    job = Job(id=jid, kind="sample", project_id=pid, title=f"Näyte: {label}",
              meta={"sample_id": sid, "run_id": body.run_id},
              steps=[Step("Generoidaan", cmd=[PYTHON, str(WORKERS_DIR / "generate.py"), str(spec_path)])],
              on_line=on_line, on_finish=on_finish)
    jobs.submit(job)
    return job.public()


@app.get("/api/projects/{pid}/samples/{fname}")
def sample_file(pid: str, fname: str):
    _project(pid)
    if not re.fullmatch(r"[\w.\-]+", fname):
        _err(400, "Virheellinen nimi")
    f = store.project_dir(pid) / "samples" / fname
    if not f.exists():
        _err(404, "Tiedostoa ei löydy")
    return FileResponse(f, filename=fname)


@app.delete("/api/projects/{pid}/samples/{sid}")
def sample_delete(pid: str, sid: str):
    p = _project(pid)
    rec = next((s for s in p.get("samples", []) if s["id"] == sid), None)
    if not rec:
        _err(404, "Näytettä ei löydy")
    for f in rec.get("files", []):
        (store.project_dir(pid) / "samples" / f).unlink(missing_ok=True)
    store.update_project(pid, lambda p: p.__setitem__("samples", [s for s in p["samples"] if s["id"] != sid]))
    return {"ok": True}


# ======================================================================= työt
@app.get("/api/jobs")
def jobs_list(project: Optional[str] = None):
    return jobs.list(project_id=project)


@app.get("/api/jobs/{jid}")
def jobs_get(jid: str):
    try:
        j = jobs.get(jid)
    except KeyError:
        _err(404, "Työtä ei löydy")
    d = j.public()
    d["queue_position"] = jobs.queue_position(jid)
    d["progress"] = {k: v for k, v in d["progress"].items() if not k.startswith("_")}
    # Uudelleenkäynnistyksen jälkeen loss-käyrä haetaan ajon tiedoista
    if j.kind == "train" and not d["progress"].get("epochs"):
        try:
            p = store.get_project(j.project_id)
            run = next((r for r in p.get("runs", []) if r["id"] == j.meta.get("run_id")), None)
            if run and run.get("loss_curve"):
                d["progress"]["epochs"] = run["loss_curve"]
        except KeyError:
            pass
    return d


@app.get("/api/jobs/{jid}/log")
def jobs_log(jid: str, offset: int = 0):
    text, new_offset = jobs.read_log(jid, offset)
    return {"text": text, "offset": new_offset}


@app.post("/api/jobs/{jid}/stop")
def jobs_stop(jid: str):
    try:
        jobs.stop(jid)
    except KeyError:
        _err(404, "Työtä ei löydy")
    return {"ok": True}


# ======================================================================= äänimallit ja muunnos
from .voice import router as voice_router  # noqa: E402

app.include_router(voice_router)

# ======================================================================= web-UI
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.exception_handler(HTTPException)
async def http_exc(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
