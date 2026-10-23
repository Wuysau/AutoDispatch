#!/usr/bin/env bash
# Optional diagnostics only. Default production adapter is Windows, per user instruction.
set -eu
repo=${1:?Usage: bash wsl-doctor.sh /home/user/projects/AetherTrade}
case "$repo" in /home/*) ;; *) echo 'Native /home/... clone required; no /mnt shared environments.' >&2; exit 2;; esac
git -C "$repo" status --short
git -C "$repo" remote -v
candidate=$(command -v opencode)
case "$candidate" in /mnt/*|*.exe) echo 'Windows OpenCode found; native Linux adapter unavailable.' >&2; exit 2;; esac
opencode run --help
python3 --version
