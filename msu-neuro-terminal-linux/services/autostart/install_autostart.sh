#!/usr/bin/env bash
set -euo pipefail

# Автоподнятие до root (sudoers NOPASSWD — без ввода пароля)
if [[ "${EUID}" -ne 0 ]]; then
  SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  exec sudo "$SCRIPT_PATH" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SERVICES_DIR="${PROJECT_ROOT}/services"
SERVER_DIR="${SERVICES_DIR}/server"
SERVICE_TEMPLATE="${SERVICES_DIR}/deploy/systemd/intan-server.service"
SERVICE_PATH="/etc/systemd/system/intan-server.service"
WAIT_USB_SCRIPT="${SERVICES_DIR}/deploy/scripts/wait_usb_stm32.sh"
BOOT_FIXES_SCRIPT="${SERVICES_DIR}/deploy/scripts/install_boot_fixes.sh"
PYTHON_BIN="$(command -v python3 || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "python3 not found in PATH"
  exit 1
fi

if [[ ! -f "${SERVICE_TEMPLATE}" ]]; then
  echo "Service template not found: ${SERVICE_TEMPLATE}"
  exit 1
fi

if [[ ! -f "${WAIT_USB_SCRIPT}" ]]; then
  echo "USB wait script not found: ${WAIT_USB_SCRIPT}"
  exit 1
fi

chmod +x "${WAIT_USB_SCRIPT}"
if [[ -f "${BOOT_FIXES_SCRIPT}" ]]; then
  chmod +x "${BOOT_FIXES_SCRIPT}"
  bash "${BOOT_FIXES_SCRIPT}"
fi

tmp_file="$(mktemp)"
trap 'rm -f "${tmp_file}"' EXIT

sed \
  -e "s|^WorkingDirectory=.*$|WorkingDirectory=${SERVER_DIR}|" \
  -e "s|^ExecStartPre=.*$|ExecStartPre=${WAIT_USB_SCRIPT}|" \
  -e "s|^ExecStart=.*$|ExecStart=${PYTHON_BIN} ${SERVER_DIR}/intan_server.py --backend usb --no-usb-reset --verbose|" \
  "${SERVICE_TEMPLATE}" > "${tmp_file}"

install -m 0644 "${tmp_file}" "${SERVICE_PATH}"
systemctl daemon-reload
systemctl enable --now intan-server.service

echo "Autostart installed and started."
echo "Check status: systemctl status intan-server.service"
echo "Live logs:    journalctl -u intan-server.service -f"
