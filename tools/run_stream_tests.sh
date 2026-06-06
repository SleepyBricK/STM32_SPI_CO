#!/usr/bin/env bash
# Полный прогон host-тестов stream (после прошивки).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== PING ==="
python3 tools/usb_intan_cmd.py PING --no-reset

echo ""
echo "=== STATS ==="
python3 tools/usb_intan_cmd.py STATS --no-reset

echo ""
echo "=== stream_validate_quick ==="
python3 tools/stream_validate_quick.py

echo ""
echo "=== ch2 analyze 500k ==="
python3 tools/ch_stream_capture_analyze.py --channel 2 -n 500000 --no-reset

echo ""
echo "=== ch2 plot 3s ==="
python3 tools/ch_stream_plot.py --channel 2 --duration 3 --no-reset \
  --out tools/ch2_stream_3s.png

echo ""
echo "=== RR8 channel scan 0-7 ==="
python3 tools/usb_intan_scan_range.py --first 0 --count 8 --no-reset

echo ""
echo "=== ALL PASSED ==="
