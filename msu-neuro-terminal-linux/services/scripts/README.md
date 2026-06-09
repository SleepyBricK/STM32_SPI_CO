# Скрипты запуска и управления

Вспомогательные shell‑скрипты для запуска сервера и просмотра логов.

---

## Содержание

- [Файлы](#файлы)
- [start_with_logs.sh](#start_with_logssh)
- [view_logs.sh](#view_logssh)
- [Расположение логов](#расположение-логов)
- [Зависимости](#зависимости)

---

## Файлы

| Файл | Назначение |
|------|------------|
| `start_with_logs.sh` | Запуск сервера с логированием в файл |
| `view_logs.sh` | Просмотр логов сервера |

---

## start_with_logs.sh

Запускает `intan_server.py` в фоне с перенаправлением вывода в `intan_server.log`. Для доступа к SPI/GPIO скрипт автоматически поднимает права (sudo). Требуется NOPASSWD в sudoers.

**Использование:**

```bash
cd /path/to/Stimulator_2.0_orangepizero2w
bash services/scripts/start_with_logs.sh
```

Примечание: при ручном запуске без автозапуска предварительно выполните `setup_permissions.sh` (см. [services/server/](../server/README.md)).

**Действия скрипта:**

1. Поднимает права (sudo) при необходимости
2. Останавливает старый процесс сервера (если есть)
3. Запускает сервер в фоне с `nohup`
4. Пишет логи в `services/server/intan_server.log`

---

## view_logs.sh

Проверяет, запущен ли сервер, и показывает последние 50 строк логов. Прав root не требует.

**Использование:**

```bash
bash services/scripts/view_logs.sh
```

**Вывод:**

- Статус сервера (PID, если запущен)
- Последние 50 строк из `intan_server.log`
- Подсказки по командам: `tail -f`, `grep` и т.п.

---

## Расположение логов

Файл логов: `services/server/intan_server.log`

Просмотр в реальном времени:

```bash
tail -f services/server/intan_server.log
```

---

## Зависимости

- `bash`
- Сервер должен находиться в `services/server/` (относительно скриптов)
