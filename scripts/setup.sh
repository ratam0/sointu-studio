#!/usr/bin/env bash
# Sointu Studio – varsinainen asennus. Ajetaan install.sh:n kautta rootina.
# Turvallinen ajaa uudelleen: päivittää ja korjaa asennuksen, ei hävitä dataa.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# ---------------------------------------------------------------- kiinnitetyt versiot
ACE_REPO="https://github.com/ace-step/ACE-Step-1.5.git"
ACE_COMMIT="ca1e85fe9430179831e6bc6be790c332190a3866"     # 2026-08-29, testattu
APPLIO_REPO="https://github.com/IAHispano/Applio.git"
APPLIO_TAG="3.7.0"                                          # 2026-10-17
SEPARATOR_VERSION="0.47.1"
VOCAL_MODEL="model_bs_roformer_ep_317_sdr_12.9755.ckpt"
KARAOKE_MODEL="mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
DEREVERB_MODEL="dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt"

STUDIO_USER="studio"
PORT="${STUDIO_PORT:-7860}"
NO_BASE="${NO_BASE:-0}"
SKIP_MODELS="${SKIP_MODELS:-0}"

VENDOR="$DIR/vendor"
ACE_DIR="$VENDOR/ACE-Step-1.5"
APPLIO_DIR="$VENDOR/Applio"
MODELS="$DIR/models"
DATA="$DIR/data"
LOG="$DIR/install.log"

export UV_PYTHON_INSTALL_DIR="$DIR/.python"
export UV_CACHE_DIR="$DIR/.cache/uv"
export UV_HTTP_TIMEOUT=300
export UV_LINK_MODE=copy
export HF_HOME="$MODELS/hf"
export ACESTEP_CHECKPOINTS_DIR="$MODELS/ace"
export APPLIO_LOGS_DIR="$DATA/rvc/logs"
export APPLIO_CONFIG_DIR="$DATA/rvc/config"

STEP=0
TOTAL=10
step() { STEP=$((STEP + 1)); printf '\n\033[1;36m[%d/%d] %s\033[0m\n' "$STEP" "$TOTAL" "$*"; }
ok()   { printf '\033[32m  ✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mAsennus keskeytyi: %s\033[0m\n  Loki: %s\n' "$*" "$LOG" >&2; exit 1; }
run()  { "$@" >>"$LOG" 2>&1 || die "komento epäonnistui: $*"; }
trap 'die "odottamaton virhe rivillä $LINENO"' ERR

[[ $EUID -eq 0 ]] || die "Aja rootina (sudo)."
: >"$LOG"
echo "Sointu Studio -asennus $(date)" >>"$LOG"

# ================================================================ 1. tarkistukset
step "Tarkistetaan palvelin"
command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || die "systemd puuttuu. Tämä asennin on tarkoitettu tavalliselle VPS:lle (ei konteille)."
command -v apt-get >/dev/null || die "Tuetaan Ubuntu/Debian-pohjaisia järjestelmiä (apt-get puuttuu)."
. /etc/os-release && ok "Käyttöjärjestelmä: $PRETTY_NAME"
command -v nvidia-smi >/dev/null || die "nvidia-smi puuttuu. Asenna NVIDIA-ajurit (esim. 'ubuntu-drivers install'), käynnistä uudelleen ja aja asennus uudestaan."
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
GPU_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -dc '0-9')"
DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
ok "GPU: $GPU_NAME, ${GPU_MB} MB, ajuri $DRIVER"
(( GPU_MB >= 15000 )) || warn "Alle 16 GB VRAMia: ACE-koulutus vaatii Säästö-profiilin ja RVC pienemmän batchin."
DRIVER_MAJOR="${DRIVER%%.*}"
(( DRIVER_MAJOR >= 570 )) || warn "Ajuri $DRIVER voi olla liian vanha CUDA 12.8 -paketeille (suositus ≥ 570)."
FREE_GB="$(df -BG --output=avail "$DIR" | tail -1 | tr -dc '0-9')"
ok "Vapaata levyä: ${FREE_GB} GB"
(( FREE_GB >= 60 )) || warn "Suositus on vähintään 60 GB vapaata (mallit ~35 GB + data)."

# ================================================================ 2. järjestelmäpaketit
step "Asennetaan järjestelmäpaketit"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -qq
run apt-get install -y -qq git git-lfs ffmpeg curl ca-certificates build-essential libsndfile1 \
    libsamplerate0 portaudio19-dev pkg-config
ok "git, ffmpeg, kääntäjät"

if ! id "$STUDIO_USER" >/dev/null 2>&1; then
  run useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin "$STUDIO_USER"
  ok "Palvelukäyttäjä '$STUDIO_USER' luotu"
fi
mkdir -p "$VENDOR" "$MODELS/ace" "$MODELS/separator" "$MODELS/hf" "$DATA" "$APPLIO_LOGS_DIR" "$APPLIO_CONFIG_DIR" "$DIR/backups"

if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >>"$LOG" 2>&1 \
    || die "uv:n asennus epäonnistui"
fi
ok "uv $(uv --version | awk '{print $2}')"

# ================================================================ 3. ACE-Step
step "ACE-Step 1.5 (musiikkimalli)"
if [[ ! -d "$ACE_DIR/.git" ]]; then run git clone --quiet "$ACE_REPO" "$ACE_DIR"; fi
run git -C "$ACE_DIR" fetch --quiet origin
run git -C "$ACE_DIR" checkout --quiet --force "$ACE_COMMIT"
ok "versio $(git -C "$ACE_DIR" rev-parse --short HEAD)"
echo "  PyTorch + riippuvuudet (ensimmäisellä kerralla 5–10 min)…"
(cd "$ACE_DIR" && run uv sync --python 3.11)
run uv pip install --python "$ACE_DIR/.venv/bin/python" \
    "faster-whisper>=1.1" "librosa>=0.10" "rich>=13" "bitsandbytes>=0.45"
"$ACE_DIR/.venv/bin/python" -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" >>"$LOG" 2>&1 \
  || die "ACE-Stepin PyTorch ei näe GPU:ta. Tarkista ajurit (nvidia-smi)."
ok "PyTorch näkee GPU:n"

# ================================================================ 4. Applio (RVC)
step "Applio $APPLIO_TAG (RVC-äänimallit)"
if [[ ! -d "$APPLIO_DIR/.git" ]]; then run git clone --quiet "$APPLIO_REPO" "$APPLIO_DIR"; fi
run git -C "$APPLIO_DIR" fetch --quiet --tags origin
run git -C "$APPLIO_DIR" checkout --quiet --force "$APPLIO_TAG"
if [[ ! -x "$APPLIO_DIR/.venv/bin/python" ]]; then run uv venv --python 3.12 "$APPLIO_DIR/.venv"; fi
APY="$APPLIO_DIR/.venv/bin/python"
echo "  PyTorch + riippuvuudet (5–10 min)…"
run uv pip install --python "$APY" -r "$APPLIO_DIR/requirements-engine.txt" \
    --extra-index-url https://download.pytorch.org/whl/cu128 --index-strategy unsafe-best-match
# Vokaalien erottelu. 'onnx-weekly' törmäisi Applion onnx-pakettiin, ja sitä
# tarvitaan vain MDX-malleille, joita studio ei käytä → asennetaan ilman sitä.
run uv pip install --python "$APY" --no-deps "audio-separator==$SEPARATOR_VERSION"
run uv pip install --python "$APY" "beartype>=0.18.5,<0.19" "rotary-embedding-torch>=0.6.1,<0.7" \
    ml_collections pydub pyyaml julius resampy six python-ffmpeg
"$APY" - >>"$LOG" 2>&1 <<'PY' || die "Applion ympäristö ei toimi (katso loki)."
import torch, faiss, librosa
from audio_separator.separator import Separator
assert torch.cuda.is_available(), "Applion PyTorch ei näe GPU:ta"
print("ok", torch.__version__)
PY
ok "Applio ja vokaalierottelu asennettu"
# Tarkkuus: bf16 Ampere-korteilla ja uudemmilla, muuten fp16
PREC="$("$APY" -c "import torch;print('bf16' if torch.cuda.is_bf16_supported() else 'fp16')")"
cat > "$APPLIO_CONFIG_DIR/config.json" <<EOF
{ "precision": "$PREC", "discord_presence": false }
EOF
ok "Koulutustarkkuus: $PREC"

# ================================================================ 5. mallit
step "Ladataan mallit (~35 GB ensimmäisellä kerralla)"
if [[ "$SKIP_MODELS" == "1" ]]; then
  warn "SKIP_MODELS=1 – mallien lataus ohitettu"
else
  echo "  ACE-Step: turbo DiT, VAE, tekstienkooderi, 1.7B-kielimalli…"
  run "$ACE_DIR/.venv/bin/acestep-download" --dir "$MODELS/ace"
  if [[ "$NO_BASE" != "1" ]]; then
    echo "  ACE-Step: base-malli…"
    run "$ACE_DIR/.venv/bin/acestep-download" --dir "$MODELS/ace" --model acestep-v15-base --skip-main
  fi
  echo "  Applio: esikoulutetut RVC-mallit, RMVPE, ContentVec…"
  (cd "$APPLIO_DIR" && run "$APY" core.py prerequisites --pretraineds-hifigan --models --no-exe)
  echo "  Vokaalierottelu: Roformer-mallit…"
  "$APY" - "$MODELS/separator" "$VOCAL_MODEL" "$KARAOKE_MODEL" "$DEREVERB_MODEL" >>"$LOG" 2>&1 <<'PY' || die "Erottelumallien lataus epäonnistui"
import sys
from audio_separator.separator import Separator
d, *models = sys.argv[1:]
s = Separator(model_file_dir=d, output_dir=d, info_only=True)
for m in models:
    s.download_model_files(m)
    print("ok", m)
PY
  echo "  Whisper large-v3 (lyriikoiden tunnistus)…"
  "$ACE_DIR/.venv/bin/python" -c "from faster_whisper.utils import download_model; download_model('large-v3')" >>"$LOG" 2>&1 \
    || warn "Whisper-mallin esilataus epäonnistui – se ladataan ensimmäisessä analyysissä"
  ok "Mallit ladattu ($(du -sh "$MODELS" | cut -f1))"
fi

# ================================================================ 6. asetukset
step "Asetukset"
ENV_FILE="$DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
# Sointu Studio -asetukset. Muutosten jälkeen: studio restart
STUDIO_HOST=127.0.0.1
STUDIO_PORT=$PORT
# Valinnainen salasana (SSH-tunneli suojaa jo yhteyden). Aseta: studio password
STUDIO_PASSWORD=
WHISPER_MODEL=large-v3
CAPTION_LM=acestep-5Hz-lm-1.7B
EOF
  ok "Luotu $ENV_FILE"
else
  ok "Käytetään olemassa olevaa $ENV_FILE"
fi
# Polut kirjoitetaan aina uudelleen (eivät ole käyttäjän säädettäviä)
cat > "$DIR/.paths" <<EOF
STUDIO_ROOT=$DIR
ACE_DIR=$ACE_DIR
APPLIO_DIR=$APPLIO_DIR
ACESTEP_CHECKPOINTS_DIR=$MODELS/ace
SEPARATOR_MODELS_DIR=$MODELS/separator
HF_HOME=$MODELS/hf
STUDIO_DATA_DIR=$DATA
APPLIO_LOGS_DIR=$APPLIO_LOGS_DIR
APPLIO_CONFIG_DIR=$APPLIO_CONFIG_DIR
VOCAL_MODEL=$VOCAL_MODEL
KARAOKE_MODEL=$KARAOKE_MODEL
DEREVERB_MODEL=$DEREVERB_MODEL
EOF
cat > "$DIR/.versions" <<EOF
studio=$(git -C "$DIR" rev-parse --short HEAD 2>/dev/null || cat "$DIR/VERSION")
ace_step=$(git -C "$ACE_DIR" rev-parse --short HEAD)
applio=$APPLIO_TAG
audio_separator=$SEPARATOR_VERSION
installed=$(date '+%Y-%m-%d %H:%M')
EOF
ok "Polut ja versiot tallennettu"

# ================================================================ 7. oikeudet
step "Käyttöoikeudet"
chown -R "$STUDIO_USER:$STUDIO_USER" "$DIR"
chmod 750 "$DIR"; chmod 640 "$ENV_FILE"
chmod +x "$DIR/install.sh" "$DIR/scripts/"*.sh "$DIR/scripts/studio"
ok "Kansio $DIR kuuluu käyttäjälle $STUDIO_USER"

# ================================================================ 8. komento
step "studio-komento"
ln -sf "$DIR/scripts/studio" /usr/local/bin/studio
ok "Komento 'studio' käytössä (studio help)"

# ================================================================ 9. palvelu
step "systemd-palvelu"
cat > /etc/systemd/system/sointu-studio.service <<EOF
[Unit]
Description=Sointu Studio (ACE-Step + RVC)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$STUDIO_USER
Group=$STUDIO_USER
WorkingDirectory=$DIR
ExecStart=$DIR/scripts/start.sh
Restart=on-failure
RestartSec=5
KillMode=mixed
KillSignal=SIGINT
TimeoutStopSec=60
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
run systemctl daemon-reload
run systemctl enable sointu-studio
run systemctl restart sointu-studio
ok "sointu-studio käynnistetty"

# ================================================================ 10. terveystarkistus
step "Tarkistetaan, että studio vastaa"
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/healthz" 2>/dev/null; then OKH=1; break; fi
  sleep 2
done
[[ "${OKH:-0}" == "1" ]] || die "Studio ei vastaa portissa $PORT. Katso: journalctl -u sointu-studio -n 50"
ok "Studio vastaa"

trap - ERR
IP="$(curl -fsS --max-time 4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
SSH_USER="${SUDO_USER:-root}"
cat <<EOF

$(printf '\033[1;32m')Sointu Studio on asennettu.$(printf '\033[0m')

Avaa omalla koneellasi SSH-tunneli:

    ssh -N -L $PORT:127.0.0.1:$PORT $SSH_USER@$IP

ja selaimessa:  http://localhost:$PORT

Hyödyllisiä komentoja palvelimella:
    studio status      tila, GPU, versiot
    studio logs -f     palvelun loki
    studio update      päivitä studio
    studio help        kaikki komennot
EOF
