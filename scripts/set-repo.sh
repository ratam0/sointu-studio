#!/usr/bin/env bash
# Asettaa GitHub-käyttäjänimen asennuskomentoon ja README:hen ennen ensimmäistä pushia.
#   scripts/set-repo.sh <github-käyttäjä> [repon-nimi]
set -euo pipefail
[[ $# -ge 1 ]] || { echo "Käyttö: $0 <github-käyttäjä> [repon-nimi]"; exit 1; }
USER_NAME="$1"; REPO_NAME="${2:-sointu-studio}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for f in "$DIR/install.sh" "$DIR/README.md"; do
  sed -i "s#__GITHUB_USER__/sointu-studio#$USER_NAME/$REPO_NAME#g; s#__GITHUB_USER__#$USER_NAME#g" "$f"
done
echo "Asennuskomento on nyt:"
echo "  curl -fsSL https://raw.githubusercontent.com/$USER_NAME/$REPO_NAME/main/install.sh | sudo bash"
