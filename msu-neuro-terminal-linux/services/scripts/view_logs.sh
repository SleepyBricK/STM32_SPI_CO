#!/bin/bash
# Скрипт для просмотра логов Intan сервера

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="${SCRIPT_DIR}/../server"
LOG_FILE="${SERVER_DIR}/intan_server.log"

echo "=== Просмотр логов Intan сервера ==="
echo ""

# Проверяем, запущен ли сервер
PID=$(pgrep -f "intan.*server|python.*intan_server")
if [ -z "$PID" ]; then
    echo "⚠ Сервер не запущен"
    echo ""
    echo "Для запуска с логированием в файл:"
    echo "  ./start_with_logs.sh"
    echo "  или: cd ${SERVER_DIR} && python3 intan_server.py --verbose 2>&1 | tee intan_server.log"
    echo ""
    echo "Или для запуска в фоне с логированием:"
    echo "  ./start_with_logs.sh"
    exit 1
fi

echo "✓ Сервер запущен (PID: $PID)"
echo ""

# Проверяем, есть ли файл логов
if [ -f "$LOG_FILE" ]; then
    echo "=== Последние 50 строк из файла логов ==="
    tail -50 "$LOG_FILE"
    echo ""
    echo "Для просмотра в реальном времени:"
    echo "  tail -f $LOG_FILE"
else
    echo "⚠ Файл логов не найден"
    echo ""
    echo "Логи выводятся в консоль, где запущен сервер."
    echo ""
    echo "Для просмотра логов процесса:"
    echo "  ps aux | grep $PID"
    echo ""
    echo "Для перенаправления вывода в файл перезапустите сервер:"
    echo "  ./start_with_logs.sh"
fi

echo ""
echo "=== Полезные команды ==="
echo "  # Просмотр последних 100 строк:"
echo "  tail -100 $LOG_FILE"
echo ""
echo "  # Просмотр в реальном времени:"
echo "  tail -f $LOG_FILE"
echo ""
echo "  # Поиск ошибок:"
echo "  grep -i error $LOG_FILE"
echo ""
echo "  # Поиск по ключевому слову (например, 'регистрация'):"
echo "  grep -i 'регистрация' $LOG_FILE"
