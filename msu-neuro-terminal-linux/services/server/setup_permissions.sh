#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите один раз с root-правами: sudo bash $0 [gpio_number] [username]"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_OWNER="$(stat -c '%U' "${SCRIPT_DIR}")"
GPIO_NUMBER="${1:-${INTAN_GPIO:-226}}"
TARGET_USER="${2:-${INTAN_USER:-${SUDO_USER:-${PROJECT_OWNER}}}}"
TARGET_GROUP="intan"
HELPER_PATH="/usr/local/sbin/intan-apply-permissions.sh"
UDEV_RULE_PATH="/etc/udev/rules.d/60-intan-permissions.rules"
SERVICE_PATH="/etc/systemd/system/intan-permissions.service"

if ! [[ "${GPIO_NUMBER}" =~ ^[0-9]+$ ]]; then
  echo "Неверный номер GPIO: ${GPIO_NUMBER}"
  exit 1
fi

if ! id "${TARGET_USER}" >/dev/null 2>&1; then
  echo "Пользователь не найден: ${TARGET_USER}"
  exit 1
fi

if [[ "${TARGET_USER}" == "root" ]]; then
  echo "Нужно указать обычного пользователя, а не root."
  echo "Пример: sudo bash $0 ${GPIO_NUMBER} orangepi"
  exit 1
fi

TARGET_PRIMARY_GROUP="$(id -gn "${TARGET_USER}")"

if ! getent group "${TARGET_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${TARGET_GROUP}"
fi

usermod -aG "${TARGET_GROUP}" "${TARGET_USER}"

cat > "${HELPER_PATH}" <<EOF
#!/usr/bin/env bash
set -euo pipefail

TARGET_GROUP="${TARGET_GROUP}"
GPIO_NUMBER="${GPIO_NUMBER}"

set_path_permissions() {
  local path="\$1"
  local mode="\$2"

  if [[ -e "\${path}" ]]; then
    chgrp "${TARGET_GROUP}" "\${path}" || true
    chmod "\${mode}" "\${path}" || true
  fi
}

set_path_permissions "/sys/class/gpio/export" 220
set_path_permissions "/sys/class/gpio/unexport" 220

GPIO_PATH="/sys/class/gpio/gpio\${GPIO_NUMBER}"
if [[ ! -d "\${GPIO_PATH}" && -w "/sys/class/gpio/export" ]]; then
  echo "\${GPIO_NUMBER}" > "/sys/class/gpio/export" 2>/dev/null || true
  sleep 0.1
fi

if [[ -d "\${GPIO_PATH}" ]]; then
  chgrp "${TARGET_GROUP}" "\${GPIO_PATH}" || true
  chmod 750 "\${GPIO_PATH}" || true
  set_path_permissions "\${GPIO_PATH}/direction" 660
  set_path_permissions "\${GPIO_PATH}/value" 660
  set_path_permissions "\${GPIO_PATH}/edge" 660
  set_path_permissions "\${GPIO_PATH}/active_low" 660
fi

for dev in /dev/spidev* /dev/intan; do
  if [[ -e "\${dev}" ]]; then
    chgrp "${TARGET_GROUP}" "\${dev}" || true
    chmod 660 "\${dev}" || true
  fi
done
EOF

chmod 0755 "${HELPER_PATH}"

cat > "${UDEV_RULE_PATH}" <<EOF
SUBSYSTEM=="spidev", GROUP="${TARGET_GROUP}", MODE="0660"
KERNEL=="intan", GROUP="${TARGET_GROUP}", MODE="0660"
ACTION=="add", SUBSYSTEM=="spidev", RUN+="${HELPER_PATH}"
ACTION=="add", KERNEL=="intan", RUN+="${HELPER_PATH}"
ACTION=="add", KERNEL=="gpio${GPIO_NUMBER}", RUN+="${HELPER_PATH}"
EOF

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=Apply Intan GPIO/SPI permissions
After=systemd-udevd.service local-fs.target
Wants=systemd-udevd.service

[Service]
Type=oneshot
ExecStart=${HELPER_PATH}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

udevadm control --reload-rules
systemctl daemon-reload
systemctl enable --now intan-permissions.service
"${HELPER_PATH}"

cat <<EOF
Готово.

Настроены права для:
- пользователя: ${TARGET_USER}
- основной группы сервиса: ${TARGET_PRIMARY_GROUP}
- группы устройств: ${TARGET_GROUP}
- GPIO: ${GPIO_NUMBER}

Что дальше:
1. Перелогиньтесь под пользователем ${TARGET_USER} или выполните: newgrp ${TARGET_GROUP}
2. Запускайте сервер и утилиты без sudo, например:
   cd "${SCRIPT_DIR}"
   python3 intan_server.py --verbose

Если используете systemd-автозапуск, переустановите unit:
  sudo bash ../autostart/install_autostart.sh
EOF
