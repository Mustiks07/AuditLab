#!/usr/bin/env bash
# Prerequisites for run_review.sh inside WSL2 Ubuntu (HANDOFF section 1). Run as your normal user;
# sudo asks for YOUR password. Installs host prerequisites only (Docker Engine, Python 3.11, unzip,
# curl) BEFORE the offline check; nothing of the AuditLab project is installed.
#
# The WSL2 VM itself is sized in %USERPROFILE%\.wslconfig on Windows:
#   [wsl2]
#   processors=2
#   memory=3800MB      # 3 984 588 800 B <= 4 000 000 000 B (not 4 GiB)
#   swap=0
# then `wsl --shutdown` in PowerShell.
#
# Without the sudo password, run it as root from Windows instead (no password is changed):
#   wsl.exe -d Ubuntu -u root -- bash prepare_wsl2.sh --user <your WSL user>
set -euo pipefail

TARGET_USER="${USER:-}"
if [ "${1:-}" = "--user" ] && [ -n "${2:-}" ]; then TARGET_USER="$2"; fi
if [ "$(id -u)" -eq 0 ]; then
  sudo() { "$@"; }   # already root (wsl -u root): run directly
  [ "$TARGET_USER" != "root" ] && [ -n "$TARGET_USER" ] || { echo "as root, pass --user <your WSL user>" >&2; exit 64; }
fi
id "$TARGET_USER" >/dev/null || { echo "unknown user $TARGET_USER" >&2; exit 64; }

echo "== packages: Docker Engine (Ubuntu archive), unzip, curl"
sudo apt-get update
sudo apt-get install -y docker.io unzip curl
sudo systemctl enable --now docker
sudo usermod -aG docker "$TARGET_USER"

echo "== Python 3.11"
if command -v python3.11 >/dev/null; then
  echo "python3.11 already present: $(python3.11 -V)"
elif sudo apt-get install -y python3.11 2>/dev/null; then
  echo "python3.11 from the Ubuntu archive"
else
  sudo apt-get install -y software-properties-common
  if sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update && sudo apt-get install -y python3.11; then
    echo "python3.11 from deadsnakes"
  else
    echo "python3.11 is not packaged for $(. /etc/os-release; echo "$VERSION_ID"); install it another way" >&2
    exit 1
  fi
fi

echo "== result"
python3.11 -VV
sudo docker version --format 'Docker {{.Server.Version}}'
sudo docker info --format 'store={{.Driver}} cgroup={{.CgroupVersion}} driver={{.CgroupDriver}}'
echo "Done. Run 'wsl --shutdown' in PowerShell once, so the docker group applies to your user."
