#!/usr/bin/env bash
# Быстрая проверка USB STM32 на Orange Pi (intan_pi_gui_guide §2.2, §4.5).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "${SCRIPT_DIR}/../../server" && pwd)"

if ! lsusb -d 0483:5741 >/dev/null 2>&1; then
  echo "STM32 0483:5741 not found" >&2
  exit 1
fi

python3 - "${SERVER_DIR}" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from intan_usb_transport import IntanUsbTransport

t = IntanUsbTransport(reset_on_open=False, verbose=True)
t.open()

def run(cmd: str, timeout_ms: int = 15000) -> str:
    print(f">>> {cmd}")
    reply = t.run_intan_command(cmd, timeout_ms=timeout_ms)
    print(reply)
    return reply

try:
    run("PING", 5000)
    run("STATS", 5000)
    run("INIT_STIM", 15000)
    run("WRITE 42 0 1 0", 15000)
    run("CLEAR_COMP", 15000)
    run("PATTERN_CLEAR", 5000)
    run("PATTERN_ADD_RAW 0x802C0001")
    run("PATTERN_ADD_RAW 0xA02A0001")
    run("PATTERN_ADD_DELAY_US 100")
    run("PATTERN_ADD_RAW 0xA02A0000")
    run("PATTERN_ADD_WRITE 42 0 1 0")
    run("PATTERN_RUN 1", 60000)
    run("READ 42")
    print("test_stm32_usb: OK")
finally:
    t.close()
PY
