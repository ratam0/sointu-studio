"""GPU-työjono: yksi työ kerrallaan (analyysi, koulutus, näytteet).

Jokainen työ koostuu askelista. Askel on joko komento (ajetaan aliprosessina omassa
prosessiryhmässään, jotta pysäytys tavoittaa myös lapsiprosessit) tai Python-funktio.
Rivit kirjoitetaan lokitiedostoon ja välitetään työtyyppikohtaiselle jäsentimelle.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import ACE_DIR, CHECKPOINTS_DIR, DATA_DIR, LOGS_DIR

JOBS_FILE = DATA_DIR / "jobs.json"


@dataclass
class Step:
    title: str
    cmd: Optional[list[str]] = None
    fn: Optional[Callable[["Job"], None]] = None
    skip_if: Optional[Callable[[], bool]] = None
    on_success: Optional[Callable[["Job"], None]] = None
    ok_codes: tuple = (0,)
    cwd: Optional[str] = None
    env: Optional[dict] = None


@dataclass
class Job:
    id: str
    kind: str                     # analyze | train | sample
    title: str
    project_id: str
    meta: dict = field(default_factory=dict)
    status: str = "queued"        # queued | running | done | failed | stopped | interrupted
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    step_index: int = 0
    step_title: str = ""
    progress: dict = field(default_factory=dict)
    error: str = ""
    steps: list[Step] = field(default_factory=list, repr=False)
    on_line: Optional[Callable[["Job", str], None]] = field(default=None, repr=False)
    on_finish: Optional[Callable[["Job"], None]] = field(default=None, repr=False)
    _proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    _stop: bool = field(default=False, repr=False)
    _tail: deque = field(default_factory=lambda: deque(maxlen=400), repr=False)

    @property
    def log_path(self) -> Path:
        return LOGS_DIR / f"{self.id}.log"

    def public(self, light: bool = False) -> dict:
        progress = self.progress
        if light:
            progress = {k: v for k, v in progress.items()
                        if not k.startswith("_") and k not in ("steps", "epochs")}
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "project_id": self.project_id,
            "meta": self.meta, "status": self.status, "created": self.created,
            "started": self.started, "finished": self.finished,
            "step_index": self.step_index, "step_count": len(self.steps) or self.meta.get("step_count", 0),
            "step_title": self.step_title, "progress": progress, "error": self.error,
        }

    def log(self, line: str) -> None:
        line = line.rstrip("\n")
        self._tail.append(line)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: deque[str] = deque()
        self._cv = threading.Condition()
        self._plock = threading.Lock()
        self._load_history()
        threading.Thread(target=self._worker, name="gpu-queue", daemon=True).start()

    # ------------------------------------------------------------ julkinen API
    def submit(self, job: Job) -> Job:
        job.meta["step_count"] = len(job.steps)
        with self._cv:
            self._jobs[job.id] = job
            self._queue.append(job.id)
            self._persist()
            self._cv.notify()
        return job

    def get(self, jid: str) -> Job:
        return self._jobs[jid]

    def list(self, project_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
        if project_id:
            jobs = [j for j in jobs if j.project_id == project_id]
        return [j.public(light=True) for j in jobs[:limit]]

    def active(self) -> Optional[Job]:
        for j in self._jobs.values():
            if j.status == "running":
                return j
        return None

    def queue_position(self, jid: str) -> int:
        try:
            return list(self._queue).index(jid) + 1
        except ValueError:
            return 0

    def stop(self, jid: str) -> None:
        job = self._jobs[jid]
        with self._cv:
            if job.status == "queued":
                try:
                    self._queue.remove(jid)
                except ValueError:
                    pass
                job.status = "stopped"
                job.finished = time.time()
                self._persist()
                return
        if job.status != "running":
            return
        job._stop = True
        proc = job._proc
        if proc and proc.poll() is None:
            job.log("[studio] Pysäytetään… (SIGINT, checkpointit säilyvät)")
            try:
                os.killpg(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                return

            def _escalate():
                for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 15)):
                    try:
                        proc.wait(timeout=wait)
                        return
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, sig)
                        except ProcessLookupError:
                            return
            threading.Thread(target=_escalate, daemon=True).start()

    def read_log(self, jid: str, offset: int = 0, max_bytes: int = 200_000) -> tuple[str, int]:
        path = LOGS_DIR / f"{jid}.log"
        if not path.exists():
            return "", 0
        size = path.stat().st_size
        if offset > size:
            offset = 0
        if size - offset > max_bytes:
            offset = size - max_bytes
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        return data.decode("utf-8", errors="replace"), offset + len(data)

    # ------------------------------------------------------------ sisäinen
    def _env(self) -> dict:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["ACESTEP_CHECKPOINTS_DIR"] = str(CHECKPOINTS_DIR)
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"
        env["TQDM_MININTERVAL"] = "5"
        return env

    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                jid = self._queue.popleft()
            job = self._jobs.get(jid)
            if not job or job.status != "queued":
                continue
            self._run(job)

    def _run(self, job: Job) -> None:
        job.status = "running"
        job.started = time.time()
        self._persist()
        job.log(f"[studio] Aloitetaan: {job.title}")
        try:
            for i, step in enumerate(job.steps):
                if job._stop:
                    break
                job.step_index = i
                job.step_title = step.title
                if step.skip_if and step.skip_if():
                    job.log(f"[studio] Ohitetaan ({i + 1}/{len(job.steps)}): {step.title} – jo valmiina")
                    continue
                job.log(f"[studio] Vaihe {i + 1}/{len(job.steps)}: {step.title}")
                if step.fn:
                    step.fn(job)
                else:
                    code = self._run_cmd(job, step.cmd, step.cwd, step.env)
                    if job._stop:
                        break
                    if code not in step.ok_codes:
                        raise RuntimeError(f"Vaihe '{step.title}' epäonnistui (koodi {code}). Katso loki.")
                if step.on_success:
                    step.on_success(job)
            job.status = "stopped" if job._stop else "done"
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
            job.log(f"[studio] VIRHE: {exc}")
            if not isinstance(exc, RuntimeError):
                job.log(traceback.format_exc())
        finally:
            job.finished = time.time()
            job._proc = None
            if job.on_finish:
                try:
                    job.on_finish(job)
                except Exception:  # noqa: BLE001
                    job.log(traceback.format_exc())
            job.log(f"[studio] Tila: {job.status}")
            self._persist()

    def _run_cmd(self, job: Job, cmd: list[str], cwd: Optional[str] = None,
                 extra_env: Optional[dict] = None) -> int:
        job.log("$ " + " ".join(cmd))
        env = self._env()
        if extra_env:
            env.update({k: v for k, v in extra_env.items() if v is not None})
        proc = subprocess.Popen(
            cmd, cwd=cwd or str(ACE_DIR), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, bufsize=1, text=True, errors="replace",
        )
        job._proc = proc
        assert proc.stdout is not None
        buf = ""
        last_cr = 0.0
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            if chunk == "\n":
                if buf.strip():
                    self._line(job, buf)
                buf = ""
            elif chunk == "\r":
                # Edistymispalkit (\r) kirjataan korkeintaan 3 s välein
                now = time.time()
                if buf.strip() and now - last_cr > 3:
                    self._line(job, buf)
                    last_cr = now
                buf = ""
            else:
                buf += chunk
        if buf.strip():
            self._line(job, buf)
        return proc.wait()

    def _line(self, job: Job, line: str) -> None:
        job.log(line)
        now = time.time()
        if now - getattr(self, "_last_persist", 0) > 30:
            self._last_persist = now
            self._persist()
        if job.on_line:
            try:
                job.on_line(job, line)
            except Exception as exc:  # noqa: BLE001
                job.log(f"[studio] jäsennysvirhe: {exc}")

    def _persist(self) -> None:
        data = [j.public(light=True) for j in sorted(self._jobs.values(), key=lambda j: j.created)][-300:]
        with self._plock:
            tmp = JOBS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, JOBS_FILE)

    def _load_history(self) -> None:
        if not JOBS_FILE.exists():
            return
        try:
            data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        for d in data:
            job = Job(id=d["id"], kind=d["kind"], title=d["title"], project_id=d["project_id"],
                      meta=d.get("meta", {}), status=d["status"], created=d["created"],
                      started=d.get("started"), finished=d.get("finished"),
                      step_index=d.get("step_index", 0), step_title=d.get("step_title", ""),
                      progress=d.get("progress", {}), error=d.get("error", ""))
            if job.status in ("queued", "running"):
                job.status = "interrupted"
                job.error = "Palvelin käynnistettiin uudelleen kesken työn."
                job.finished = job.finished or time.time()
            self._jobs[job.id] = job


def new_job_id() -> str:
    return "j" + uuid.uuid4().hex[:10]
