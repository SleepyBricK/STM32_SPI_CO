#!/usr/bin/env bash
# Показывает IPv4 на консоли и в journal после реального подключения Wi-Fi.
set -euo pipefail

WAIT_SEC="${1:-60}"
POLL_SEC="${2:-1}"

ip_addr=""
iface=""
deadline=$((SECONDS + WAIT_SEC))

while (( SECONDS < deadline )); do
  if default_line=$(ip -4 route show default 2>/dev/null | awk 'NR==1{print; exit}'); then
    iface=$(awk '{print $5}' <<<"$default_line" || true)
    if [[ -n "${iface:-}" ]]; then
      ip_addr=$(ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk 'NR==1{print $4; exit}' || true)
    fi
  fi

  if [[ -z "${ip_addr:-}" ]]; then
    ip_addr=$(ip -4 -o addr show scope global 2>/dev/null | awk 'NR==1{print $4; exit}' || true)
  fi

  if [[ -n "${ip_addr:-}" ]]; then
    break
  fi

  sleep "$POLL_SEC"
done

if [[ -z "${iface:-}" ]]; then
  iface="unknown"
fi

if [[ -z "${ip_addr:-}" ]]; then
  msg="[BOOT] IP не назначен за ${WAIT_SEC}s (iface=$iface)"
else
  msg="[BOOT] IP адрес: $ip_addr (iface=$iface)"
fi

echo "$msg"

if [[ -w /dev/tty1 ]]; then
  echo "$msg" > /dev/tty1
fi
