#!/bin/sh
# Отдельный build tree (480 MHz + Intan). То же, что дефолтный build/ после cmake reconfigure.
set -e
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cmake -S "$ROOT" -B "$ROOT/build480" \
  -DCMAKE_TOOLCHAIN_FILE="$ROOT/cmake/gcc-arm-none-eabi.cmake" \
  -DWITH_INTAN_HW=ON \
  -DBOARD_SYSCLK_480=ON
cmake --build "$ROOT/build480"
echo "OK: $ROOT/build480/WeActSTM32H743.elf (SYSCLK 480 MHz)"
