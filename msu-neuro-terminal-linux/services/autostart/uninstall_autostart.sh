#!/usr/bin/env bash
set -euo pipefail

# Автоподнятие до root (sudoers NOPASSWD — без ввода пароля)
if [[ "${EUID}" -ne 0 ]]; then
  SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  exec sudo "$SCRIPT_PATH" "$@"
fi

systemctl disable --now intan-server.service || true
rm -f /etc/systemd/system/intan-server.service
systemctl daemon-reload

echo "Autostart removed."
