"""Yhteiset palvelinosat: työjono ja apufunktiot."""
from __future__ import annotations

import re
import subprocess

from fastapi import HTTPException

from .jobs import JobManager

jobs = JobManager()

RE_TAG = re.compile(r"^@@(\w+) (.*)$")


def err(status: int, msg: str):
    raise HTTPException(status_code=status, detail=msg)


def probe_duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=60)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def to_wav(src, dst, sr: int = 48000, channels: int = 2) -> tuple[bool, str]:
    """Muuntaa minkä tahansa äänitiedoston WAV-muotoon ffmpegillä."""
    proc = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn",
                           "-ac", str(channels), "-ar", str(sr), "-c:a", "pcm_s16le", str(dst)],
                          capture_output=True, text=True, timeout=900)
    return proc.returncode == 0, proc.stderr.strip()[:300]
