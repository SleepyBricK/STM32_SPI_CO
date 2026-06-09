#!/usr/bin/env bash
# Ждёт появления STM32 (0483:5741) на USB после power-on.
# USB3300 + STM32H743 инициализируются асинхронно, часто позже network-online.
set -euo pipefail

VID="${1:-0483}"
PID="${2:-5741}"
TIMEOUT_SEC="${3:-90}"
SETTLE_SEC="${4:-3}"

if ! command -v lsusb >/dev/null 2>&1; then
  echo "wait_usb_stm32: lsusb not found" >&2
  exit 1
fi

id="${VID}:${PID}"
deadline=$((SECONDS + TIMEOUT_SEC))

while (( SECONDS < deadline )); do
  if lsusb -d "$id" >/dev/null 2>&1; then
    sleep "$SETTLE_SEC"
    echo "wait_usb_stm32: ${id} ready"
    exit 0
  fi
  sleep 1
done

echo "wait_usb_stm32: ${id} not found within ${TIMEOUT_SEC}s" >&2
exit 1
