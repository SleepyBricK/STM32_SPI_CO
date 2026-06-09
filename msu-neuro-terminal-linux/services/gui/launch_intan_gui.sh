#!/bin/bash
# Запуск GUI Intan RHS2116 (актуальная версия intan_gui_new.py).
# Можно запускать двойным щелчком в файловом менеджере.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

exec python3 intan_gui_new.py "$@"
