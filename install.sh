#!/usr/bin/env bash
# Sointu Studio – asennus yhdellä komennolla:
#
#   curl -fsSL https://raw.githubusercontent.com/ratam0/sointu-studio/main/install.sh | sudo bash
#
# Valinnat (ympäristömuuttujina ennen bashia, esim. "| sudo NO_BASE=1 bash"):
#   STUDIO_REPO   git-osoite, josta studio haetaan (oletus alla)
#   STUDIO_BRANCH haara (oletus main)
#   STUDIO_DIR    asennuskansio (oletus /opt/sointu)
#   STUDIO_PORT   web-käyttöliittymän portti (oletus 7860)
#   NO_BASE=1     älä lataa ACE-Stepin base-mallia (säästää ~5 GB)
set -euo pipefail

REPO="${STUDIO_REPO:-https://github.com/ratam0/sointu-studio.git}"
BRANCH="${STUDIO_BRANCH:-main}"
DIR="${STUDIO_DIR:-/opt/sointu}"

say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mVirhe: %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "Sointu Studio asennetaan Linux-palvelimelle."
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null || die "Aja root-käyttäjänä tai asenna sudo."
  # Ajetaan uudelleen rootina; toimii myös 'curl … | bash' -muodossa
  if [[ -f "${BASH_SOURCE[0]:-}" ]]; then exec sudo -E bash "${BASH_SOURCE[0]}" "$@"; fi
  die "Aja komento muodossa: curl -fsSL … | sudo bash"
fi

# Ajetaanko suoraan kloonatusta reposta?
SELF_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if ! command -v git >/dev/null; then
  say "Asennetaan git"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates >/dev/null
fi

# Kansiot omistaa palvelukäyttäjä 'studio'; sallitaan rootin git-komennot niissä
git config --system --get-all safe.directory 2>/dev/null | grep -qxF '*' || git config --system --add safe.directory '*'

if [[ -n "$SELF_DIR" && -f "$SELF_DIR/scripts/setup.sh" && "$SELF_DIR" != "$DIR" ]]; then
  say "Kopioidaan studio kansioon $DIR"
  mkdir -p "$DIR"
  cp -a "$SELF_DIR/." "$DIR/"
elif [[ -d "$DIR/.git" ]]; then
  say "Päivitetään studio ($DIR)"
  git -C "$DIR" fetch --quiet origin "$BRANCH"
  git -C "$DIR" checkout --quiet "$BRANCH"
  git -C "$DIR" reset --quiet --hard "origin/$BRANCH"
elif [[ ! -f "$DIR/scripts/setup.sh" ]]; then
  [[ "$REPO" == *"__GITHUB"*"_USER__"* ]] && die "Repo-osoite puuttuu. Aja ensin 'scripts/set-repo.sh <github-käyttäjä>' ennen pushia, tai anna STUDIO_REPO=…"
  say "Haetaan studio: $REPO"
  mkdir -p "$(dirname "$DIR")"
  git clone --quiet --branch "$BRANCH" "$REPO" "$DIR"
fi

if [[ ! -f "$DIR/scripts/setup.sh" || ! -d "$DIR/studio/web" || ! -d "$DIR/studio/workers" ]]; then
  die "Repon rakenne on väärä: scripts/setup.sh, studio/web tai studio/workers puuttuu.
  Selainlataus litistää helposti kansiot. Tarkista, että GitHubissa näkyvät kansiot
  'scripts' ja 'studio' (ja studio-kansiossa 'web' ja 'workers'), poista sitten
  vanha asennuskansio:  sudo rm -rf $DIR  ja aja asennus uudelleen."
fi
exec bash "$DIR/scripts/setup.sh" "$@"
