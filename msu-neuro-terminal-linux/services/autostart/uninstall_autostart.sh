#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash $0"
  exit 1
fi

systemctl disable --now intan-server.service || true
rm -f /etc/systemd/system/intan-server.service
systemctl daemon-reload

echo "Autostart removed."
