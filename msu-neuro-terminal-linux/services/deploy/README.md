# Deploy и конфигурация systemd

Шаблоны и конфигурационные файлы для развёртывания Intan‑сервера.

---

## Содержание

- [Структура](#структура)
- [intan-server.service](#intan-serverservice)
- [Установка](#установка)
- [Ручная установка](#ручная-установка)
- [Примечания](#примечания)

---

## Структура

```
deploy/
├── README.md                      # Этот файл
└── systemd/
    └── intan-server.service       # unit‑файл для systemd
```

---

## intan-server.service

Unit‑файл для systemd, описывающий сервис Intan TCP/UDP сервера.

| Параметр | Значение | Описание |
|----------|----------|----------|
| `WorkingDirectory` | `.../services/server` | Рабочая папка (заменяется при установке) |
| `ExecStart` | `python3 intan_server.py --verbose` | Команда запуска (путь подставляется) |
| `Restart` | always | Перезапуск при падении |
| `RestartSec` | 2 | Задержка перед перезапуском |
| `User` | root | Пользователь (нужен для GPIO/SPI) |
| `Environment` | PYTHONUNBUFFERED=1 | Небуферизованный вывод Python |

**Зависимости:** `network-online.target` — ожидание поднятия сети.

---

## Установка

Unit‑файл не копируется вручную. Используется скрипт установки автозапуска:

```bash
sudo services/autostart/install_autostart.sh
```

Он читает шаблон из `services/deploy/systemd/intan-server.service`, подставляет актуальные пути и устанавливает в `/etc/systemd/system/`.

---

## Ручная установка

Если нужна ручная установка:

1. Скопируйте `intan-server.service` в `/etc/systemd/system/`
2. Отредактируйте `WorkingDirectory` и `ExecStart` под свои пути
3. Выполните:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now intan-server.service
```

---

## Примечания

Жёстко заданные пути в шаблоне (`/home/admin/Stimulator_2.0_orangepizero2w/`) используются как значения по умолчанию; при установке через `install_autostart.sh` они заменяются на реальные пути проекта.
