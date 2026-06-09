# Intan RHS2116 TCP/UDP сервер

Сервер для работы с чипом Intan RHS2116 на Orange Pi Zero 2W. Запускает TCP‑сервер (управление стимуляцией) и UDP‑сервер (потоковая передача ADC‑данных).

---

## Содержание

- [Файлы](#файлы)
- [Зависимости](#зависимости)
- [Запуск](#запуск)
- [Аргументы командной строки](#аргументы-командной-строки)
- [Протокол TCP](#протокол-tcp)
- [Аппаратура](#аппаратура)
- [Автозапуск и логирование](#автозапуск-и-логирование)

---

## Файлы

| Файл | Назначение |
|------|------------|
| `intan_server.py` | Главный процесс, запускает TCP и UDP серверы |
| `intan_tcp_server.py` | TCP‑сервер для команд стимуляции (из GUI) |
| `intan_udp_recorder.py` | UDP‑сервер для потоковой регистрации ADC |
| `stimulate_channel0.py` | Низкоуровневые функции: SPI, GPIO, регистры Intan |
| `setup_permissions.sh` | Одноразовая настройка прав для GPIO/SPI (udev, группа gpio) |

---

## Зависимости

```bash
sudo apt-get install python3-spidev
pip install numpy   # опционально, для оптимизаций
```

---

## Запуск

После выполнения `setup_permissions.sh` и перелогина:

```bash
# Базовый запуск (порты по умолчанию)
python3 intan_server.py --verbose

# С указанием портов и устройства
python3 intan_server.py --tcp-port 9000 --udp-port 9001 --gpio 226 --device /dev/spidev1.1 --verbose
```

Без настройки прав потребуется `sudo python3 intan_server.py ...`.

---

## Аргументы командной строки

| Аргумент | По умолчанию | Описание |
|----------|--------------|----------|
| `--tcp-port` | 9000 | Порт TCP для управления стимуляцией |
| `--udp-port` | 9001 | Порт UDP для потоковой передачи данных |
| `-g`, `--gpio` | 226 | Номер GPIO для PH2 (питание Intan) |
| `-d`, `--device` | `/dev/spidev1.1` | Путь к SPI устройству |
| `-v`, `--verbose` | — | Подробный вывод в консоль |

---

## Протокол TCP

JSON‑команды построчно. Ответ — одна строка JSON.

Примеры:

```json
{"cmd": "ping"}
{"cmd": "pulse", "channels": "0-3", "neg": 0, "pos": 20}
{"cmd": "sawtooth", "channels": "0,1", "pos": 50, "steps": 50, "duration": 0.001}
{"cmd": "stop"}
```

---

## Аппаратура

| Параметр | Значение |
|----------|----------|
| SPI | `/dev/spidev1.1` (Orange Pi Zero 2W) |
| GPIO 226 | Вывод PH2 для управления питанием Intan |

### Права доступа

Для работы с GPIO и SPI один раз выполните:

```bash
sudo bash setup_permissions.sh
```

Скрипт создаёт группу `gpio`, добавляет пользователя, настраивает udev для `/dev/spidev*` и `/sys/class/gpio/`. После перелогина (`newgrp gpio` или выход/вход) запуск без root возможен.

---

## Автозапуск и логирование

**Автозапуск:** сервер можно запускать как systemd‑сервис. См. [services/autostart/](../autostart/README.md) и [services/deploy/](../deploy/README.md).

**Логирование:**

- При `--verbose` — вывод в stdout
- При запуске через systemd: `journalctl -u intan-server.service -f`
- При запуске через `start_with_logs.sh` — файл `intan_server.log` в этой папке
