# GUI‑клиент Intan RHS2116

Графический клиент для управления Intan RHS2116. Запускается на ПК и подключается к **intan_server** на Orange Pi Zero 2W.

**Актуальный скрипт:** `intan_gui_new.py`  
**Архитектура:** ПК → TCP/UDP → Orange Pi (`--backend usb`) → USB STM32 (`0483:5741`) → SPI → Intan RHS2116

---

## Файлы

| Файл | Назначение |
|------|------------|
| `intan_gui_new.py` | **Основной GUI** (актуальная версия) |
| `intan_gui_clientv5_linux.py` | Предыдущая версия (legacy) |
| `launch_intan_gui.sh` | Запуск `intan_gui_new.py` |
| `intan-gui.desktop` | Шаблон ярлыка |
| `install_desktop_launcher.sh` | Установка ярлыка в меню |

---

## Зависимости

```bash
pip install numpy matplotlib
```

`tkinter` — из пакета python3-tk.

---

## Запуск

```bash
cd services/gui
python3 intan_gui_new.py
# или
./launch_intan_gui.sh
```

---

## Подключение

1. На Orange Pi должен работать сервер:
   ```bash
   sudo systemctl status intan-server   # --backend usb
   ```
2. В GUI: **Host** = IP платы, **Port** = `9000` (TCP).
3. **Проверить Intan** — быстрый `read_register 255` (chip ID 32), без полной init.
4. **Инициализация** — полная настройка стимуляции на STM32 (до ~2 мин).

UDP-регистрация: порт **9001** на вкладке «Регистрация».

---

## Таймауты

GUI увеличивает таймауты TCP для команд через USB-сопроцессор (init до 120 с, read_register 15 с). При таймауте во время записи остановите UDP STREAM и повторите проверку.
