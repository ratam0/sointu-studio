"""Asetukset ympäristömuuttujista (.paths ja .env ladataan start.sh:ssa)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("STUDIO_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("STUDIO_DATA_DIR", ROOT / "data"))
PASSWORD = os.environ.get("STUDIO_PASSWORD", "")

# ---- ACE-Step (musiikki)
ACE_DIR = Path(os.environ.get("ACE_DIR", ROOT / "vendor" / "ACE-Step-1.5"))
CHECKPOINTS_DIR = Path(os.environ.get("ACESTEP_CHECKPOINTS_DIR", ROOT / "models" / "ace"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
CAPTION_LM = os.environ.get("CAPTION_LM", "acestep-5Hz-lm-1.7B")
WHISPER_LD_LIBRARY_PATH = os.environ.get("WHISPER_LD_LIBRARY_PATH", "")
_venv_py = ACE_DIR / ".venv" / "bin" / "python"
PYTHON = str(_venv_py if _venv_py.exists() else Path(sys.executable))

# ---- Applio (RVC-äänimallit) ja vokaalierottelu
APPLIO_DIR = Path(os.environ.get("APPLIO_DIR", ROOT / "vendor" / "Applio"))
_applio_py = APPLIO_DIR / ".venv" / "bin" / "python"
APPLIO_PYTHON = os.environ.get("APPLIO_PYTHON") or str(_applio_py if _applio_py.exists() else Path(sys.executable))
APPLIO_LOGS_DIR = Path(os.environ.get("APPLIO_LOGS_DIR", DATA_DIR / "rvc" / "logs"))
APPLIO_CONFIG_DIR = Path(os.environ.get("APPLIO_CONFIG_DIR", DATA_DIR / "rvc" / "config"))
SEPARATOR_MODELS_DIR = Path(os.environ.get("SEPARATOR_MODELS_DIR", ROOT / "models" / "separator"))
VOCAL_MODEL = os.environ.get("VOCAL_MODEL", "model_bs_roformer_ep_317_sdr_12.9755.ckpt")
KARAOKE_MODEL = os.environ.get("KARAOKE_MODEL", "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt")
DEREVERB_MODEL = os.environ.get("DEREVERB_MODEL", "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt")

WORKERS_DIR = Path(__file__).resolve().parent / "workers"
WEB_DIR = Path(__file__).resolve().parent / "web"

PROJECTS_DIR = DATA_DIR / "projects"
VOICES_DIR = DATA_DIR / "voices"
CONVERSIONS_DIR = DATA_DIR / "conversions"
LOGS_DIR = DATA_DIR / "logs"
for _d in (PROJECTS_DIR, VOICES_DIR, CONVERSIONS_DIR, LOGS_DIR, APPLIO_LOGS_DIR, APPLIO_CONFIG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Mallivariantit: kansio checkpoints-hakemistossa + oikeat inferenssiasetukset
VARIANTS = {
    "turbo": {"dir": "acestep-v15-turbo", "shift": 3.0, "steps": 8, "label": "Turbo – nopea"},
    "base": {"dir": "acestep-v15-base", "shift": 1.0, "steps": 50, "label": "Base – laadukkain"},
    "sft": {"dir": "acestep-v15-sft", "shift": 1.0, "steps": 50, "label": "SFT – hienosäädetty"},
}

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".aiff", ".aif", ".wma"}


def available_variants() -> list[dict]:
    out = []
    for key, v in VARIANTS.items():
        installed = (CHECKPOINTS_DIR / v["dir"]).is_dir()
        out.append({"id": key, "label": v["label"], "installed": installed,
                    "shift": v["shift"], "steps": v["steps"]})
    return out


def applio_env() -> dict:
    """Ympäristö Applio-aliprosesseille."""
    return {
        "APPLIO_LOGS_DIR": str(APPLIO_LOGS_DIR),
        "APPLIO_CONFIG_DIR": str(APPLIO_CONFIG_DIR),
        "APPLIO_ROOT": str(APPLIO_DIR),
        "PYTHONPATH": str(APPLIO_DIR),
    }
