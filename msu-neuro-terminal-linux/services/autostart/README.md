# Автозапуск Intan‑сервера

Скрипты для настройки автозапуска сервера Intan через systemd при загрузке системы.

---

## Содержание

- [Файлы](#файлы)
- [Установка автозапуска](#установка-автозапуска)
- [Удаление автозапуска](#удаление-автозапуска)
- [Управление сервисом](#управление-сервисом)
- [Шаблон unit‑файла](#шаблон-unit-файла)

---

## Файлы

| Файл | Назначение |
|------|------------|
| `install_autostart.sh` | Установка и включение systemd‑сервиса |
| `uninstall_autostart.sh` | Удаление автозапуска |

---

## Установка автозапуска

Скрипт автоматически поднимает права (sudo). Требуется NOPASSWD для пользователя в `/etc/sudoers.d/` (например: `admin ALL=(ALL) NOPASSWD: ALL`).

```bash
# Из корня проекта
cd /path/to/Stimulator_2.0_orangepizero2w
bash services/autostart/install_autostart.sh
```

Или с полным путём:

```bash
bash /home/admin/Stimulator_2.0_orangepizero2w/services/autostart/install_autostart.sh
```

**Что делает скрипт:**

1. Находит шаблон сервиса в `services/deploy/systemd/intan-server.service`
2. Подставляет реальные пути (WorkingDirectory, ExecStart) из структуры проекта
3. Копирует unit в `/etc/systemd/system/`
4. Выполняет `systemctl daemon-reload`
5. Включает и сразу запускает сервис `intan-server.service`

---

## Удаление автозапуска

```bash
bash services/autostart/uninstall_autostart.sh
```

Скрипт сам поднимет права. Отключает и останавливает сервис, удаляет unit‑файл.

---

## Управление сервисом

| Действие | Команда |
|----------|---------|
| Статус | `sudo systemctl status intan-server.service` |
| Логи в реальном времени | `journalctl -u intan-server.service -f` |
| Перезапуск | `sudo systemctl restart intan-server.service` |
| Остановка | `sudo systemctl stop intan-server.service` |
| Включить автозапуск | `sudo systemctl enable intan-server.service` |
| Выключить автозапуск | `sudo systemctl disable intan-server.service` |

---

## Шаблон unit‑файла

Используется `services/deploy/systemd/intan-server.service`. Скрипт установки подменяет в нём:

- `WorkingDirectory` — на `services/server/`
- `ExecStart` — на путь к `intan_server.py` и `python3`

Точные пути зависят от расположения проекта на диске.
