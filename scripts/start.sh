#!/usr/bin/env bash
# Käynnistää Sointu Studion web-palvelimen (systemd ajaa tämän käyttäjänä 'studio').
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
# shellcheck disable=SC1091
source "$DIR/.paths"
# shellcheck disable=SC1091
source "$DIR/.env"
set +a

PY="$ACE_DIR/.venv/bin/python"
APPLIO_PYTHON="$APPLIO_DIR/.venv/bin/python"
export APPLIO_PYTHON

# faster-whisper (CTranslate2) tarvitsee pip-pakettien mukana tulevat cuBLAS/cuDNN-kirjastot
NV_LIBS="$("$PY" - <<'PY'
import glob, os, site
paths = []
for sp in site.getsitepackages():
    paths += glob.glob(os.path.join(sp, "nvidia", "*", "lib"))
print(":".join(sorted(set(paths))))
PY
)"
# Asetetaan vain analyysityölle (Applion PyTorchilla on omat versionsa näistä kirjastoista)
export WHISPER_LD_LIBRARY_PATH="$NV_LIBS"
export PYTHONUNBUFFERED=1
export HOME="$DIR"

cd "$DIR"
exec "$PY" -m uvicorn studio.server:app \
  --host "${STUDIO_HOST:-127.0.0.1}" --port "${STUDIO_PORT:-7860}" \
  --timeout-graceful-shutdown 20 --log-level warning
