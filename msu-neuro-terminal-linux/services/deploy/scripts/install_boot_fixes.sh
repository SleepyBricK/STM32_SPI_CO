#!/usr/bin/env bash
# Сетевые и boot-фиксы: wait-online для wlan0, show-ip-on-boot.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  exec sudo "$SCRIPT_PATH" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WAIT_ONLINE_BIN=""
for candidate in \
  "$(command -v systemd-networkd-wait-online 2>/dev/null || true)" \
  /usr/lib/systemd/systemd-networkd-wait-online \
  /lib/systemd/systemd-networkd-wait-online; do
  if [[ -n "${candidate}" && -x "${candidate}" ]]; then
    WAIT_ONLINE_BIN="${candidate}"
    break
  fi
done

if [[ -z "${WAIT_ONLINE_BIN}" ]]; then
  echo "systemd-networkd-wait-online not found"
  exit 1
fi

install -m 0755 "${SCRIPT_DIR}/show-ip-on-boot.sh" /usr/local/bin/show-ip-on-boot.sh

mkdir -p /etc/systemd/system/systemd-networkd-wait-online.service.d
cat > /etc/systemd/system/systemd-networkd-wait-online.service.d/wlan0.conf <<EOF
[Service]
ExecStart=
ExecStart=${WAIT_ONLINE_BIN} --interface=wlan0 --timeout=90
EOF

mkdir -p /etc/systemd/system/show-ip-on-boot.service.d
cat > /etc/systemd/system/show-ip-on-boot.service.d/after-wait-online.conf <<'EOF'
[Unit]
After=systemd-networkd-wait-online.service
Wants=systemd-networkd-wait-online.service
EOF

systemctl daemon-reload
systemctl enable systemd-networkd-wait-online.service

echo "Boot fixes installed:"
echo "  /usr/local/bin/show-ip-on-boot.sh"
echo "  systemd-networkd-wait-online (wlan0, 90s)"
