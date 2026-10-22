"""RVC-koulutus Appliolla: ajaa rvc/train/train.py:n ja raportoi edistymisen studiolle.

Applio tulostaa epochit stdoutiin, mutta loss-arvot menevät vain TensorBoardiin,
joten taustasäie lukee ne tapahtumatiedostoista ja välittää ne @@LOSS-riveinä.
Ajetaan Applion Python-ympäristössä, työhakemistona Applio-kansio.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

RE_EPOCH = re.compile(r"\| epoch=(\d+) \| step=(\d+) \| time=[\d:]+ \| training_speed=(\d+):(\d+):(\d+)")
RE_LOWEST = re.compile(r"lowest_value=([\d.]+) \(epoch (\d+) and step (\d+)\)")


def emit(tag: str, obj) -> None:
    print(f"@@{tag} " + json.dumps(obj), flush=True)


class LossWatcher(threading.Thread):
    """Lukee TensorBoard-tiedostoista epoch-tason loss/g/mel-arvot."""

    def __init__(self, event_dir: Path, step_to_epoch: dict):
        super().__init__(daemon=True)
        self.event_dir = event_dir
        self.step_to_epoch = step_to_epoch
        self.stop = threading.Event()
        self.sent = 0

    def collect(self) -> list:
        try:
            from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
        except Exception:
            return []
        if not self.event_dir.is_dir():
            return []
        acc = EventAccumulator(str(self.event_dir), size_guidance={"scalars": 0})
        acc.Reload()
        if "loss/g/mel" not in acc.Tags().get("scalars", []):
            return []
        pts = []
        for ev in acc.Scalars("loss/g/mel"):
            ep = self.step_to_epoch.get(ev.step)
            if ep is not None:
                pts.append([ep, round(float(ev.value), 4)])
        dedup = {p[0]: p for p in pts}
        return [dedup[k] for k in sorted(dedup)]

    def flush(self) -> None:
        try:
            pts = self.collect()
            if len(pts) != self.sent:
                self.sent = len(pts)
                emit("LOSS", {"metric": "mel", "points": pts})
        except Exception as exc:
            print(f"[loss] {exc}", flush=True)

    def run(self) -> None:
        while not self.stop.wait(20):
            self.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    spec = json.loads(Path(ap.parse_args().spec).read_text(encoding="utf-8"))
    applio = Path.cwd()
    sys.path.insert(0, str(applio))
    from rvc.lib.tools.prerequisites_download import ensure_pretrained
    from rvc.lib.user_config import get_logs_dir

    exp = spec["exp"]
    exp_dir = Path(get_logs_dir()) / exp
    if not (exp_dir / "config.json").exists():
        print(f"Kokeen asetukset puuttuvat ({exp_dir}/config.json). Ajettiinko esikäsittely ja piirteiden irrotus?", flush=True)
        return 1
    pg, pd = ensure_pretrained(spec["vocoder"], int(spec["sample_rate"]))
    print(f"[koulutus] Esikoulutettu pohja: {Path(pg).name}, {Path(pd).name}", flush=True)

    cmd = [sys.executable, str(applio / "rvc" / "train" / "train.py"),
           exp, str(spec["save_every"]), str(spec["epochs"]), pg, pd, str(spec["gpu"]),
           str(spec["batch_size"]), str(spec["sample_rate"]),
           "True",                          # save_only_latest: vain viimeisin G/D-checkpoint (säästää levyä)
           "True",                          # save_every_weights: pieni .pth-malli joka tallennuksella
           "False",                         # cache_data_in_gpu
           str(bool(spec["cleanup"])),      # aloita alusta (uusi ajo) vai jatka
           spec["vocoder"],
           "False"]                         # checkpointing
    step_to_epoch: dict = {}
    watcher = LossWatcher(exp_dir / "eval", step_to_epoch)
    watcher.start()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1, errors="replace")

    def forward(sig, _frame):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        m = RE_EPOCH.search(line)
        if m:
            ep, st = int(m.group(1)), int(m.group(2))
            secs = int(m.group(3)) * 3600 + int(m.group(4)) * 60 + int(m.group(5))
            step_to_epoch[st] = ep
            emit("EPOCH", {"epoch": ep, "max": int(spec["epochs"]), "step": st, "secs": secs})
            lo = RE_LOWEST.search(line)
            if lo:
                emit("BEST", {"value": float(lo.group(1)), "epoch": int(lo.group(2)), "step": int(lo.group(3))})
        if "| batch=" in line:      # erävaiheen rivit ovat tiheitä – ei lokiin
            continue
        print(line, flush=True)
    code = proc.wait()
    watcher.stop.set()
    time.sleep(1)
    watcher.flush()
    if code not in (0,):
        print(f"[koulutus] train.py päättyi koodilla {code}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
