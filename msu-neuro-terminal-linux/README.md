# Stimulator 2.0 — Orange Pi Zero 2W

Комплекс для управления нейроинтерфейсом на базе **Intan RHS2116** (16‑канальный усилитель и стимулятор): регистрация нейросигналов, электрическая стимуляция и управление по Wi‑Fi через TCP и UDP. Сервер работает на **Orange Pi Zero 2W**, клиент — на ПК.

---

## Содержание

- [Обзор](#обзор)
- [Аппаратура](#аппаратура)
- [Архитектура](#архитектура)
- [Компоненты](#компоненты)
- [Быстрый старт](#быстрый-старт)
- [Сеть и порты](#сеть-и-порты)
- [Структура проекта](#структура-проекта)
- [Документация](#документация)

---

## Обзор

Stimulator 2.0 объединяет:

- **Аппаратуру** — Orange Pi Zero 2W + USB‑сопроцессор STM32H743 с Intan RHS2116 (legacy: прямой SPI/GPIO на Pi).
- **Сервер** — Python‑приложение на плате: TCP‑сервер для команд стимуляции и UDP‑сервер для потоковой передачи ADC‑данных.
- **Клиенты** — десктопное приложение (Python/Tkinter) для управления стимуляцией и приёма регистрации.

Поддерживаются: регистрация сигналов с 16 каналов, стимуляция по паттернам STM32 (`PATTERN_ADD_RAW`), измерение импеданса.

---

## Аппаратура

**Orange Pi Zero 2W** — хост Linux. **Intan RHS2116** на плате **STM32H743** (USB 2.0 HS, `0483:5741`).

| Параметр | Значение |
|----------|----------|
| USB | STM32 coprocessor `0483:5741` |
| Прошивка | [STM32_SPI_CO](https://github.com/SleepyBricK/STM32_SPI_CO) |
| Legacy SPI (опционально) | `/dev/spidev1.1`, GPIO 226 |
| Intan RHS2116 | 16 каналов AC/DC, стимуляция |

---

## Архитектура

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ПК (Windows / Linux / macOS)                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │ GUI (intan_gui_new.py)                                                  ││
│  │ • Паттерны стимуляции (блоки → pattern_load → pattern_run)             ││
│  │ • Импеданс, recovery; регистрация EMG/ADC                              ││
│  │ • Регистрация EMG/ADC, графики, сохранение в CSV                        ││
│  └─────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                     TCP (порт 9000) │ UDP (порт 9001)
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Orange Pi Zero 2W (intan_server.py)                      │
│  • TCP‑сервер: команды ping, pulse, sawtooth, stop и др.                    │
│  • UDP‑сервер: потоковая отправка ADC‑данных на ПК                          │
│  • USB → STM32 → SPI → Intan RHS2116                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Компоненты

| Компонент | Путь | Описание |
|-----------|------|----------|
| **Intan TCP/UDP сервер** | `services/server/` | Сервер на плате: TCP для стимуляции, UDP для регистрации. [Подробнее →](services/server/README.md) |
| **GUI‑клиент (Python)** | `services/gui/` | Десктопное приложение на Tkinter: стимуляция, паттерны, регистрация, графики. [Подробнее →](services/gui/README.md) |
| **Скрипты** | `services/scripts/` | Запуск сервера с логами, просмотр логов. [Подробнее →](services/scripts/README.md) |
| **Автозапуск** | `services/autostart/` | Установка systemd‑сервиса для автозапуска при загрузке. [Подробнее →](services/autostart/README.md) |
| **Deploy** | `services/deploy/` | Конфигурация systemd (unit‑файл). [Подробнее →](services/deploy/README.md) |
| **Документация** | `docs/` | Дополнительные материалы. |

---

## Быстрый старт

### 0. Первоначальная настройка (один раз)

Настройка прав для доступа к GPIO и SPI без root:

```bash
sudo bash services/server/setup_permissions.sh
```

После выполнения перелогиньтесь или выполните `newgrp gpio`. Требуется NOPASSWD в sudoers (например: `admin ALL=(ALL) NOPASSWD: ALL`).

### 1. Запуск сервера на плате (Orange Pi Zero 2W)

**С автозапуском при загрузке** (рекомендуется):

```bash
bash services/autostart/install_autostart.sh
```

Скрипт сам поднимет права. После установки сервер стартует при включении платы.

**С логированием в файл** (ручной запуск):

```bash
bash services/scripts/start_with_logs.sh
```

**Вручную:**

```bash
cd services/server
python3 intan_server.py --verbose
```

(После `setup_permissions.sh` root не нужен; иначе — `sudo python3 ...`.)

### 2. Проверка сервера

```bash
systemctl status intan-server.service
journalctl -u intan-server.service -f
```

### 3. Запуск GUI на ПК (с ПК в сети)

```bash
cd services/gui
pip install numpy matplotlib   # при необходимости
python3 intan_gui_new.py
```

Укажите IP платы (Orange Pi) и порт 9000, нажмите «Подключиться».

### 4. Регистрация данных

1. В GUI подключитесь к серверу по TCP.
2. Настройте приём UDP‑данных (порт 9001) в интерфейсе.
3. Данные приходят с платы по UDP.

---

## Сеть и порты

| Параметр | Значение | Где задаётся |
|----------|----------|--------------|
| TCP порт | 9000 | Аргумент `--tcp-port` в `intan_server.py` |
| UDP порт | 9001 | Аргумент `--udp-port` в `intan_server.py` |
| IP платы | DHCP или статический | Сеть Orange Pi |
| SPI | `/dev/spidev1.1` | Аргумент `--device` |
| GPIO | 226 | Аргумент `--gpio` |

Плата и ПК должны быть в одной сети (Wi‑Fi или Ethernet).

---

## Структура проекта

```
Stimulator_2.0_orangepizero2w/
├── README.md                      # Этот файл
├── docs/                          # Дополнительная документация
│   └── README.txt
└── services/
    ├── server/                    # Intan TCP/UDP сервер (на плате)
    │   ├── intan_server.py
    │   ├── intan_tcp_server.py
    │   ├── intan_udp_recorder.py
    │   ├── stimulate_channel0.py
    │   ├── setup_permissions.sh   # Одноразовая настройка GPIO/SPI
    │   └── README.md
    ├── gui/                       # GUI‑клиент (на ПК)
    │   ├── intan_gui_new.py
    │   └── README.md
    ├── scripts/                   # Скрипты запуска и логов
    │   ├── start_with_logs.sh
    │   ├── view_logs.sh
    │   └── README.md
    ├── autostart/                 # Установка автозапуска
    │   ├── install_autostart.sh
    │   ├── uninstall_autostart.sh
    │   └── README.md
    └── deploy/                    # Конфигурация systemd
        ├── systemd/
        │   └── intan-server.service
        └── README.md
```

---

## Документация

- [Intan RHS2116 Datasheet](https://intantech.com/files/Intan_RHS2116_datasheet.pdf)
- [Server](services/server/README.md) — протокол TCP, аргументы, аппаратура.
- [GUI](services/gui/README.md) — подключение, паттерны, переменные окружения EMG.
- [Scripts](services/scripts/README.md) — запуск с логами, просмотр логов.
- [Autostart](services/autostart/README.md) — установка и удаление systemd‑сервиса.
- [Deploy](services/deploy/README.md) — unit‑файл systemd.
