#!/usr/bin/env bash
# Одноразовая настройка прав для доступа к GPIO и SPI без root.
# Запуск: sudo bash setup_permissions.sh (или sudo ./setup_permissions.sh)
# После установки: перелогиньтесь или выполните newgrp gpio

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите с правами root: sudo bash $0"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

# Создаём группу gpio, если её нет
if ! getent group gpio >/dev/null 2>&1; then
  groupadd gpio
  echo "Создана группа gpio"
fi

# Добавляем пользователя в группу gpio
usermod -aG gpio "$REAL_USER"
echo "Пользователь $REAL_USER добавлен в группу gpio"

# Udev-правило для GPIO (sysfs)
cat > /etc/udev/rules.d/99-intan-gpio.rules << 'EOF'
# Intan RHS2116: доступ к GPIO для группы gpio
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", ACTION=="add", RUN+="/bin/sh -c 'chown root:gpio /sys/class/gpio/export /sys/class/gpio/unexport 2>/dev/null; chmod 220 /sys/class/gpio/export /sys/class/gpio/unexport 2>/dev/null'"
SUBSYSTEM=="gpio", KERNEL=="gpio[0-9]*", ACTION=="add", RUN+="/bin/sh -c 'for f in direction value edge active_low; do [ -f /sys/class/gpio/%k/$f ] && chown root:gpio /sys/class/gpio/%k/$f && chmod 660 /sys/class/gpio/%k/$f; done'"
EOF
echo "Установлено udev-правило для GPIO"

# Udev-правило для SPI (spidev)
cat > /etc/udev/rules.d/99-intan-spidev.rules << 'EOF'
# Intan RHS2116: доступ к spidev для группы gpio
SUBSYSTEM=="spidev", KERNEL=="spidev*", MODE="0660", GROUP="gpio"
EOF
echo "Установлено udev-правило для SPI"

# Перезагрузка udev
udevadm control --reload-rules
udevadm trigger --subsystem-match=gpio
udevadm trigger --subsystem-match=spidev

# Исправляем права на уже экспортированные GPIO (напр. gpio226)
# На некоторых системах chown для sysfs не применяется — даём доступ через chmod
for gpiodir in /sys/class/gpio/gpio*; do
  if [[ -d "$gpiodir" ]]; then
    chown -R root:gpio "$gpiodir" 2>/dev/null || true
    chmod 666 "$gpiodir"/direction "$gpiodir"/value "$gpiodir"/edge "$gpiodir"/active_low 2>/dev/null || true
  fi
done
chown root:gpio /sys/class/gpio/export /sys/class/gpio/unexport 2>/dev/null || true
chmod 222 /sys/class/gpio/export /sys/class/gpio/unexport 2>/dev/null || true

# Права на spidev
for spidev in /dev/spidev*; do
  [[ -e "$spidev" ]] && chown root:gpio "$spidev" && chmod 660 "$spidev"
done

echo ""
echo "Готово. Дальнейшие шаги:"
echo "  1. Перелогиньтесь (выйдите и войдите снова) ИЛИ выполните: newgrp gpio"
echo "  2. После этого скрипты смогут работать с GPIO/SPI без sudo"
echo ""
echo "Для проверки после перелогина:"
echo "  groups   # должна быть группа gpio"
echo "  python3 ${SCRIPT_DIR}/intan_server.py --verbose"
