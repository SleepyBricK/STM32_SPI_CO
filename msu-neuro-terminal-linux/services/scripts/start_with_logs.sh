#!/bin/bash
# Скрипт для запуска сервера с логированием в файл

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="${SCRIPT_DIR}/../server"
cd "$SERVER_DIR"

# Останавливаем старый процесс, если запущен
OLD_PID=$(pgrep -f "intan.*server|python.*intan_server")
if [ -n "$OLD_PID" ]; then
    echo "Останавливаем старый процесс (PID: $OLD_PID)..."
    kill $OLD_PID
    sleep 2
fi

# Запускаем сервер с логированием
echo "Запуск сервера с логированием в файл intan_server.log..."
echo "Для просмотра логов в реальном времени: tail -f intan_server.log"
echo ""

nohup python3 intan_server.py --verbose > intan_server.log 2>&1 &

NEW_PID=$!
sleep 1

if ps -p $NEW_PID > /dev/null 2>&1; then
    echo "✓ Сервер запущен (PID: $NEW_PID)"
    echo "Логи сохраняются в: ${SERVER_DIR}/intan_server.log"
    echo ""
    echo "Просмотр логов:"
    echo "  tail -f intan_server.log"
    echo "  или"
    echo "  ./view_logs.sh"
else
    echo "❌ Ошибка запуска сервера"
    echo "Проверьте логи: cat intan_server.log"
fi
