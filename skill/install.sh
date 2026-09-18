#!/usr/bin/env bash
# Installs the /chess launcher skill into Hermes by symlinking this repo's
# skill/ and scripts/ into ~/.hermes/skills/games/chess/. Editing the repo
# then updates the live skill immediately (the Coin Scout pattern).
#
# Layout confirmed against the existing games/ skills already installed:
#   ~/.hermes/skills/games/chess-coach/SKILL.md
#   ~/.hermes/skills/trading/coin-scout/SKILL.md
#   ~/.hermes/skills/trading/coin-scout/scripts -> <repo>/skills/coin-scout/scripts
# i.e. one directory per skill directly under a category folder, holding
# SKILL.md and a scripts/ dir at that same level. Not run automatically —
# the user runs this by hand.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_DIR="${HOME}/.hermes/skills/games/chess"

mkdir -p "${SKILL_DIR}"
echo "Skill directory: ${SKILL_DIR}"

link() {
  local target="$1" link_path="$2"
  if [ -L "${link_path}" ]; then
    rm -f "${link_path}"
  elif [ -e "${link_path}" ]; then
    echo "Refusing to overwrite existing non-symlink: ${link_path}" >&2
    exit 1
  fi
  ln -s "${target}" "${link_path}"
  echo "Linked ${link_path} -> ${target}"
}

link "${REPO_ROOT}/skill/SKILL.md" "${SKILL_DIR}/SKILL.md"
link "${REPO_ROOT}/scripts" "${SKILL_DIR}/scripts"

echo "Done. '/chess' should be available next time Hermes rescans its skills."
