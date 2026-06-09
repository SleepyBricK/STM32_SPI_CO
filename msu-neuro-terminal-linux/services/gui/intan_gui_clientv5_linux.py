#!/usr/bin/env python3
"""
Расширенный GUI‑клиент для управления стимуляцией Intan RHS2116 через TCP.

Этот скрипт запускается на ПК (Windows / Linux / macOS) и подключается к
TCP‑серверу на плате (intan_tcp_server.py).

Требуется только стандартная библиотека Python (Tkinter и socket).
"""

import json
import math
import os
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from datetime import datetime
import re
import csv
import numpy as np

# Импорт matplotlib для графиков
try:
    import matplotlib
    matplotlib.use('TkAgg')  # Используем TkAgg backend для интеграции с tkinter
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib import pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    print("Предупреждение: matplotlib не установлен. Графики будут недоступны.")
    print("Установите: pip install matplotlib")


# ============================================================================
# КОНВЕРТАЦИЯ ADC RHS2116 В МИКРОВОЛЬТЫ
# ============================================================================

def rhs2116_ac_uV(adc: int) -> float:
    """
    Конвертирует 16-битное unsigned ADC значение RHS2116 в микровольты для AC-coupled high-gain каналов.
    
    RHS2116 datasheet: Velec(AC) = 0.195 µV × (ADC result – 32768)
    
    Args:
        adc: Unsigned 16-bit ADC значение (0..65535) - СТАРШИЕ 16 бит результата CONVERT
    
    Returns:
        Напряжение в микровольтах (float), центрированное около нуля
    
    Примечания:
        - Виртуальный ноль = 32768 (середина диапазона unsigned 16-bit)
        - Всегда вычитаем 32768 перед умножением на 0.195
        - Если НЕ вычесть 32768 → получите постоянные ~6000 µV (ОШИБКА)
        - Не используем int16 для промежуточных вычислений (избегаем переполнения)
        - При неподвижной расслабленной руке среднее должно быть близко к 0 µV
        - RMS в покое: ~5–20 µV
        - RMS при сжатии: десятки–сотни µV
    """
    # КРИТИЧНО: всегда вычитаем 32768 (виртуальный ноль)
    # Если НЕ вычесть 32768 → получите постоянные ~6000 µV (ОШИБКА)
    # Используем int() для явного преобразования, но не int16 (чтобы избежать переполнения)
    signed_code = int(adc) - 32768
    # Формула из даташита: 0.195 µV на LSB
    velec_uV = signed_code * 0.195
    return float(velec_uV)


# Конфигурация для псевдодифференциального EMG
# Можно настроить через переменные окружения или константы
EMG_CH_A = int(os.environ.get('EMG_CH_A', '0'))  # Канал A для дифференциала (по умолчанию 0)
EMG_CH_B = int(os.environ.get('EMG_CH_B', '2'))  # Канал B для дифференциала (по умолчанию 2)
EMG_USE_DIFFERENTIAL = os.environ.get('EMG_USE_DIFFERENTIAL', 'true').lower() == 'true'  # По умолчанию используем дифференциал


class IntanTcpClient:
    """Клиент для общения с TCP‑сервером Intan."""

    def __init__(self):
        self.sock = None
        self.lock = threading.Lock()

    def connect(self, host: str, port: int, timeout: float = 3.0):
        with self.lock:
            if self.sock is not None:
                self.sock.close()
                self.sock = None
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            # Переведём в буферизованный режим для работы построчно
            self.sock = s
            # Читаем приветственное сообщение от сервера
            try:
                self._read_line(timeout=2.0)
            except Exception:
                pass  # Игнорируем ошибки при чтении приветствия

    def close(self):
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def _read_line(self, timeout: float = 5.0) -> str:
        """Читает одну строку из сокета (до символа \\n)."""
        if self.sock is None:
            raise RuntimeError("Нет активного подключения к серверу")
        
        self.sock.settimeout(timeout)
        chunks = []
        while True:
            ch = self.sock.recv(1)
            if not ch:
                raise RuntimeError("Соединение закрыто сервером")
            if ch == b"\n":
                break
            chunks.append(ch)
        
        line = b"".join(chunks).decode("utf-8").strip()
        return line

    def send_command(self, cmd: dict, timeout: float = 5.0) -> dict:
        """
        Отправляет JSON‑команду и возвращает JSON‑ответ.
        """
        data = (json.dumps(cmd) + "\n").encode("utf-8")

        with self.lock:
            if self.sock is None:
                raise RuntimeError("Нет активного подключения к серверу")

            self.sock.sendall(data)
            line = self._read_line(timeout)
            
            if not line:
                raise RuntimeError("Пустой ответ от сервера")
            try:
                return json.loads(line)
            except Exception as e:
                raise RuntimeError(f"Неверный JSON от сервера: {e}, данные: {line}")

    def send_line(self, line: str, timeout: float = 5.0) -> str:
        """
        Отправляет одну текстовую команду на сервер (для RHS2116: CLEAR, WRITE, CONVERT и т.д.).
        Ожидается, что сервер поддерживает команду send_line и возвращает ответ в поле response/result.
        """
        resp = self.send_command({"cmd": "send_line", "line": line}, timeout=timeout)
        if resp.get("status") != "ok" and "response" not in resp and "result" not in resp:
            err = resp.get("error", "Неизвестная ошибка")
            raise RuntimeError(err)
        return resp.get("response", resp.get("result", "")).strip()


class IntanGuiApp(tk.Tk):
    """Основное окно GUI с расширенными настройками."""

    def __init__(self):
        super().__init__()
        self.title("Intan RHS2116 Control Panel")
        self.geometry("1000x800")
        self.minsize(900, 700)
        
        # Настройка стилей
        self._setup_styles()
        
        self.client = IntanTcpClient()
        self.connected = False
        self.last_impedance_data = None  # данные последнего измерения импеданса для экспорта CSV

        # UDP регистрация - инициализация переменных
        self.udp_sock = None
        self.udp_registered = False
        self.udp_listening = False
        self.udp_listen_thread = None
        self.recording_packet_count = 0
        self.recording_graph_data = {}  # Данные для графика
        self.recording_hex_data = []  # Сырые hex данные для последующего парсинга
        self.recording_active = False  # Флаг активной регистрации
        
        # Конструктор паттернов
        self.pattern_blocks = []  # Список блоков паттерна
        

        self._create_widgets()

    def _setup_styles(self):
        """Настройка стилей для красивого интерфейса"""
        style = ttk.Style()
        
        # Используем современную тему
        try:
            style.theme_use('clam')
        except:
            pass
        
        # Настройка цветов
        style.configure('Title.TLabel', font=('Arial', 12, 'bold'))
        style.configure('Header.TLabel', font=('Arial', 10, 'bold'))
        style.configure('Status.TLabel', font=('Arial', 9))
        style.configure('Success.TLabel', foreground='green', font=('Arial', 9, 'bold'))
        style.configure('Error.TLabel', foreground='red', font=('Arial', 9, 'bold'))
        
        # Стили для кнопок
        style.configure('Primary.TButton', font=('Arial', 10, 'bold'))
        style.configure('Danger.TButton', font=('Arial', 10, 'bold'))

    def _create_widgets(self):
        pad = 8
        pad_small = 4

        # Главная рамка с отступами
        main_frame = ttk.Frame(self, padding=10)
        main_frame.pack(fill="both", expand=True)

        # Верхняя панель: подключение и статус
        top_frame = ttk.Frame(main_frame)
        top_frame.pack(fill="x", pady=(0, pad))

        # Рамка подключения
        conn_frame = ttk.LabelFrame(top_frame, text="🔌 Подключение", padding=pad)
        conn_frame.pack(side="left", fill="x", expand=True, padx=(0, pad))

        conn_inner = ttk.Frame(conn_frame)
        conn_inner.pack(fill="x")

        ttk.Label(conn_inner, text="Host:").grid(row=0, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_host = tk.StringVar(value="192.168.31.191")
        host_entry = ttk.Entry(conn_inner, textvariable=self.var_host, width=18)
        host_entry.grid(row=0, column=1, sticky="w", padx=pad_small, pady=pad_small)

        ttk.Label(conn_inner, text="Port:").grid(row=0, column=2, sticky="w", padx=pad_small, pady=pad_small)
        self.var_port = tk.StringVar(value="9000")
        port_entry = ttk.Entry(conn_inner, textvariable=self.var_port, width=8)
        port_entry.grid(row=0, column=3, sticky="w", padx=pad_small, pady=pad_small)

        self.btn_connect = ttk.Button(
            conn_inner, text="🔗 Подключиться", command=self.on_connect, style='Primary.TButton'
        )
        self.btn_connect.grid(row=0, column=4, sticky="w", padx=pad_small, pady=pad_small)

        self.btn_ping = ttk.Button(
            conn_inner, text="📡 Ping", command=self.on_ping, state="disabled"
        )
        self.btn_ping.grid(row=0, column=5, sticky="w", padx=pad_small, pady=pad_small)

        # Рамка статуса
        status_frame = ttk.LabelFrame(top_frame, text="📊 Статус", padding=pad)
        status_frame.pack(side="right", fill="x", padx=(pad, 0))

        self.status_label = ttk.Label(
            status_frame, text="● Отключено", style='Error.TLabel'
        )
        self.status_label.pack(anchor="w")

        # Создаем Notebook для вкладок
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill="both", expand=True, pady=(pad, 0))

        # Вкладка 1: Основные команды
        tab_main = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_main, text="⚙️ Основное")

        # Вкладка 2: Импульсная стимуляция
        tab_pulse = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_pulse, text="⚡ Импульсы")

        # Вкладка 3: Пилообразная стимуляция
        tab_sawtooth = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_sawtooth, text="📈 Пила")

        # Вкладка 4: Паттерн команд
        tab_pattern = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_pattern, text="🎯 Паттерн")

        # Вкладка 5: Справка по регистрам
        tab_registers = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_registers, text="📚 Справка")

        # Вкладка 6: Регистрация данных
        tab_recording = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_recording, text="📊 Регистрация")

        # Вкладка 7: Измерения (температура и импеданс)
        tab_measurements = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_measurements, text="🌡️ Измерения")

        # ========== ВКЛАДКА 1: ОСНОВНОЕ ==========
        # Рамка основных команд
        cmd_frame = ttk.LabelFrame(tab_main, text="🎮 Основные команды", padding=pad)
        cmd_frame.pack(fill="x", pady=(0, pad))

        cmd_buttons = ttk.Frame(cmd_frame)
        cmd_buttons.pack(fill="x")

        self.btn_init = ttk.Button(
            cmd_buttons, text="🔧 Инициализация", command=self.on_init, state="disabled", width=18
        )
        self.btn_init.pack(side="left", padx=pad_small, pady=pad_small)

        self.btn_stop = ttk.Button(
            cmd_buttons, text="⏹ Остановить", command=self.on_stop, state="disabled", 
            style='Danger.TButton', width=18
        )
        self.btn_stop.pack(side="left", padx=pad_small, pady=pad_small)

        self.btn_check_intan = ttk.Button(
            cmd_buttons, text="🔍 Проверить Intan", command=self.on_check_intan, state="disabled", width=18
        )
        self.btn_check_intan.pack(side="left", padx=pad_small, pady=pad_small)

        # Рамка логов
        log_frame = ttk.LabelFrame(tab_main, text="📋 Лог событий", padding=pad)
        log_frame.pack(fill="both", expand=True)

        # Панель управления логом
        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.pack(fill="x", pady=(0, pad_small))

        ttk.Button(log_toolbar, text="🗑 Очистить", command=self.on_clear_log).pack(side="right", padx=pad_small)
        ttk.Button(log_toolbar, text="💾 Сохранить", command=self.on_save_log).pack(side="right", padx=pad_small)

        self.txt_log = scrolledtext.ScrolledText(
            log_frame, height=20, state="disabled", wrap=tk.WORD,
            font=('Consolas', 9), bg='#f8f8f8', fg='#333333'
        )
        self.txt_log.pack(fill="both", expand=True)

        # ========== ВКЛАДКА 2: ИМПУЛЬСЫ ==========
        # Рамка каналов
        channels_frame = ttk.LabelFrame(tab_pulse, text="📡 Каналы", padding=pad)
        channels_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(channels_frame, text="Каналы:").grid(row=0, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_channels = tk.StringVar(value="0")
        ttk.Entry(channels_frame, textvariable=self.var_channels, width=25).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(
            channels_frame, 
            text='(формат: "0", "0,1,2", "0-3", "0,2-4")',
            font=('Arial', 8), foreground='gray'
        ).grid(row=0, column=2, columnspan=2, sticky="w", padx=pad_small, pady=pad_small)

        # Рамка токов
        current_frame = ttk.LabelFrame(tab_pulse, text="⚡ Токи стимуляции", padding=pad)
        current_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(current_frame, text="Отрицательный ток (µA):").grid(
            row=0, column=0, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_neg = tk.StringVar(value="0")
        ttk.Entry(current_frame, textvariable=self.var_neg, width=15).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(current_frame, text="(0-255)").grid(
            row=0, column=2, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(current_frame, text="Положительный ток (µA):").grid(
            row=0, column=3, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_pos = tk.StringVar(value="20")
        ttk.Entry(current_frame, textvariable=self.var_pos, width=15).grid(
            row=0, column=4, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(current_frame, text="(0-255)").grid(
            row=0, column=5, sticky="w", padx=pad_small, pady=pad_small
        )

        # Рамка параметров импульса
        pulse_params_frame = ttk.LabelFrame(tab_pulse, text="⏱ Параметры импульса", padding=pad)
        pulse_params_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(pulse_params_frame, text="Длительность (мс):").grid(
            row=0, column=0, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_pulse_duration = tk.StringVar(value="1.0")
        ttk.Entry(pulse_params_frame, textvariable=self.var_pulse_duration, width=15).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(pulse_params_frame, text="Задержка между импульсами (мс):").grid(
            row=0, column=2, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_inter_pulse_delay = tk.StringVar(value="1.0")
        ttk.Entry(pulse_params_frame, textvariable=self.var_inter_pulse_delay, width=15).grid(
            row=0, column=3, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(pulse_params_frame, text="Повторений:").grid(
            row=1, column=0, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_repeat_count = tk.StringVar(value="1")
        ttk.Entry(pulse_params_frame, textvariable=self.var_repeat_count, width=15).grid(
            row=1, column=1, sticky="w", padx=pad_small, pady=pad_small
        )

        # Кнопка импульса
        btn_pulse_frame = ttk.Frame(tab_pulse)
        btn_pulse_frame.pack(fill="x", pady=pad)

        self.btn_pulse = ttk.Button(
            btn_pulse_frame, text="🚀 Отправить импульс", command=self.on_pulse, 
            state="disabled", style='Primary.TButton', width=25
        )
        self.btn_pulse.pack(side="left", padx=pad_small, pady=pad_small)

        # ========== ВКЛАДКА 3: ПИЛА ==========
        # Рамка каналов для пилы
        saw_channels_frame = ttk.LabelFrame(tab_sawtooth, text="📡 Каналы", padding=pad)
        saw_channels_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(saw_channels_frame, text="Каналы:").grid(row=0, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_saw_channels = tk.StringVar(value="0")
        ttk.Entry(saw_channels_frame, textvariable=self.var_saw_channels, width=25).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(
            saw_channels_frame, 
            text='(формат: "0", "0,1,2", "0-3")',
            font=('Arial', 8), foreground='gray'
        ).grid(row=0, column=2, columnspan=2, sticky="w", padx=pad_small, pady=pad_small)

        # Рамка параметров пилы
        saw_params_frame = ttk.LabelFrame(tab_sawtooth, text="📊 Параметры пилообразной стимуляции", padding=pad)
        saw_params_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(saw_params_frame, text="Макс. положительный ток (µA):").grid(
            row=0, column=0, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_saw_pos = tk.StringVar(value="50")
        ttk.Entry(saw_params_frame, textvariable=self.var_saw_pos, width=15).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(saw_params_frame, text="(0-255)").grid(
            row=0, column=2, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(saw_params_frame, text="Количество шагов:").grid(
            row=0, column=3, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_saw_steps = tk.StringVar(value="50")
        ttk.Entry(saw_params_frame, textvariable=self.var_saw_steps, width=15).grid(
            row=0, column=4, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(saw_params_frame, text="Длительность (мс):").grid(
            row=1, column=0, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_saw_duration = tk.StringVar(value="1.0")
        ttk.Entry(saw_params_frame, textvariable=self.var_saw_duration, width=15).grid(
            row=1, column=1, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(saw_params_frame, text="Количество циклов:").grid(
            row=1, column=3, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_saw_repeat = tk.StringVar(value="1")
        ttk.Entry(saw_params_frame, textvariable=self.var_saw_repeat, width=15).grid(
            row=1, column=4, sticky="w", padx=pad_small, pady=pad_small
        )

        # Кнопка пилы
        btn_saw_frame = ttk.Frame(tab_sawtooth)
        btn_saw_frame.pack(fill="x", pady=pad)

        self.btn_saw = ttk.Button(
            btn_saw_frame, text="🚀 Запустить пилообразную стимуляцию", 
            command=self.on_sawtooth, state="disabled", style='Primary.TButton', width=30
        )
        self.btn_saw.pack(side="left", padx=pad_small, pady=pad_small)

        # ========== ВКЛАДКА 4: ПАТТЕРН ==========
        # Создаем Notebook для подвкладок: Конструктор и Текстовый редактор
        pattern_notebook = ttk.Notebook(tab_pattern)
        pattern_notebook.pack(fill="both", expand=True)
        
        # Подвкладка 1: Визуальный конструктор
        tab_constructor = ttk.Frame(pattern_notebook, padding=pad)
        pattern_notebook.add(tab_constructor, text="🔧 Конструктор")
        
        # Подвкладка 2: Текстовый редактор
        tab_text_editor = ttk.Frame(pattern_notebook, padding=pad)
        pattern_notebook.add(tab_text_editor, text="📝 Текстовый редактор")
        
        # ========== ПОДВКЛАДКА 1: КОНСТРУКТОР ==========
        # Создаем панель конструктора
        constructor_main_frame = ttk.Frame(tab_constructor)
        constructor_main_frame.pack(fill="both", expand=True)
        
        # Левая панель: список блоков
        constructor_left_frame = ttk.Frame(constructor_main_frame, width=500)
        constructor_left_frame.pack(side="left", fill="both", expand=True, padx=(0, pad))
        
        # Правая панель: палитра блоков и предпросмотр
        constructor_right_frame = ttk.Frame(constructor_main_frame, width=350)
        constructor_right_frame.pack(side="right", fill="both", padx=(pad, 0))
        constructor_right_frame.pack_propagate(False)
        
        # Рамка палитры блоков
        palette_frame = ttk.LabelFrame(constructor_right_frame, text="🎨 Палитра блоков", padding=pad)
        palette_frame.pack(fill="x", pady=(0, pad))
        
        # Кнопки добавления блоков
        palette_buttons_frame = ttk.Frame(palette_frame)
        palette_buttons_frame.pack(fill="x")
        
        self.btn_add_step_size = ttk.Button(
            palette_buttons_frame, text="📏 Шаг стимуляции", 
            command=lambda: self.add_pattern_block("step_size"), width=22
        )
        self.btn_add_step_size.pack(fill="x", padx=pad_small, pady=pad_small)
        
        self.btn_add_current = ttk.Button(
            palette_buttons_frame, text="⚡ Настроить ток", 
            command=lambda: self.add_pattern_block("current"), width=22
        )
        self.btn_add_current.pack(fill="x", padx=pad_small, pady=pad_small)
        
        self.btn_add_polarity = ttk.Button(
            palette_buttons_frame, text="🔀 Установить полярность", 
            command=lambda: self.add_pattern_block("polarity"), width=22
        )
        self.btn_add_polarity.pack(fill="x", padx=pad_small, pady=pad_small)
        
        self.btn_add_enable = ttk.Button(
            palette_buttons_frame, text="▶ Включить стимуляцию", 
            command=lambda: self.add_pattern_block("enable"), width=22
        )
        self.btn_add_enable.pack(fill="x", padx=pad_small, pady=pad_small)
        
        self.btn_add_disable = ttk.Button(
            palette_buttons_frame, text="⏹ Выключить стимуляцию", 
            command=lambda: self.add_pattern_block("disable"), width=22
        )
        self.btn_add_disable.pack(fill="x", padx=pad_small, pady=pad_small)
        
        self.btn_add_delay = ttk.Button(
            palette_buttons_frame, text="⏱ Задержка", 
            command=lambda: self.add_pattern_block("delay"), width=22
        )
        self.btn_add_delay.pack(fill="x", padx=pad_small, pady=pad_small)
        
        self.btn_add_comment = ttk.Button(
            palette_buttons_frame, text="💬 Комментарий", 
            command=lambda: self.add_pattern_block("comment"), width=22
        )
        self.btn_add_comment.pack(fill="x", padx=pad_small, pady=pad_small)
        
        # Рамка предпросмотра
        preview_frame = ttk.LabelFrame(constructor_right_frame, text="👁 Предпросмотр команд", padding=pad)
        preview_frame.pack(fill="both", expand=True)
        
        self.pattern_preview_text = scrolledtext.ScrolledText(
            preview_frame, height=10, state="disabled", wrap=tk.WORD,
            font=('Consolas', 9), bg='#f8f8f8', fg='#333333'
        )
        self.pattern_preview_text.pack(fill="both", expand=True)

        # Canvas для предварительного просмотра формы тока
        # Рамка для выбора канала и метрик
        signal_control_frame = ttk.Frame(preview_frame)
        signal_control_frame.pack(fill="x", pady=(0, pad_small))
        
        ttk.Label(signal_control_frame, text="Канал для визуализации:").pack(side="left", padx=(0, pad_small))
        self.var_signal_channel = tk.StringVar(value="0")
        signal_channel_entry = ttk.Entry(signal_control_frame, textvariable=self.var_signal_channel, width=5)
        signal_channel_entry.pack(side="left", padx=(0, pad_small))
        signal_channel_entry.bind('<KeyRelease>', lambda e: self.update_pattern_signal_preview())
        
        # Метрики будут отображаться справа
        self.signal_metrics_label = ttk.Label(signal_control_frame, text="", font=('Arial', 9))
        self.signal_metrics_label.pack(side="right", padx=(pad_small, 0))
        
        self.pattern_signal_canvas = tk.Canvas(
            preview_frame, bg='#ffffff', height=250,
            highlightthickness=1, highlightbackground='#cccccc'
        )
        self.pattern_signal_canvas.pack(fill="both", expand=True, pady=(pad_small, 0))
        
        # Рамка списка блоков
        blocks_frame = ttk.LabelFrame(constructor_left_frame, text="📋 Блоки паттерна", padding=pad)
        blocks_frame.pack(fill="both", expand=True, pady=(0, pad))
        
        # Canvas для блоков с прокруткой
        blocks_canvas_frame = ttk.Frame(blocks_frame)
        blocks_canvas_frame.pack(fill="both", expand=True)
        
        self.blocks_canvas = tk.Canvas(
            blocks_canvas_frame, bg='#ffffff', highlightthickness=0
        )
        blocks_scrollbar = ttk.Scrollbar(blocks_canvas_frame, orient="vertical", command=self.blocks_canvas.yview)
        blocks_scrollbar.pack(side="right", fill="y")
        self.blocks_canvas.pack(side="left", fill="both", expand=True)
        self.blocks_canvas.configure(yscrollcommand=blocks_scrollbar.set)
        
        # Фрейм для блоков внутри canvas
        self.blocks_container = ttk.Frame(self.blocks_canvas)
        self.blocks_window = self.blocks_canvas.create_window(0, 0, anchor="nw", window=self.blocks_container)
        
        # Привязываем обновление прокрутки
        self.blocks_container.bind('<Configure>', lambda e: self.blocks_canvas.configure(scrollregion=self.blocks_canvas.bbox("all")))
        self.blocks_canvas.bind('<Configure>', lambda e: self.blocks_canvas.itemconfig(self.blocks_window, width=e.width))
        
        # Список блоков паттерна
        self.pattern_blocks = []  # Список словарей с информацией о блоках
        
        # Кнопки управления конструктором
        constructor_btn_frame = ttk.Frame(constructor_left_frame)
        constructor_btn_frame.pack(fill="x")
        
        self.btn_generate_pattern = ttk.Button(
            constructor_btn_frame, text="🔄 Сгенерировать паттерн", 
            command=self.generate_pattern_from_blocks, style='Primary.TButton', width=22
        )
        self.btn_generate_pattern.pack(side="left", padx=pad_small, pady=pad_small)
        
        self.btn_clear_blocks = ttk.Button(
            constructor_btn_frame, text="🗑 Очистить все", 
            command=self.clear_all_blocks, width=18
        )
        self.btn_clear_blocks.pack(side="left", padx=pad_small, pady=pad_small)
        
        self.btn_load_example_blocks = ttk.Button(
            constructor_btn_frame, text="📄 Пример", 
            command=self.load_example_blocks, width=18
        )
        self.btn_load_example_blocks.pack(side="left", padx=pad_small, pady=pad_small)
        
        # ========== ПОДВКЛАДКА 2: ТЕКСТОВЫЙ РЕДАКТОР ==========
        # Создаем панель с редактором и визуализацией
        pattern_main_frame = ttk.Frame(tab_text_editor)
        pattern_main_frame.pack(fill="both", expand=True)
        
        # Левая панель: редактор
        pattern_left_frame = ttk.Frame(pattern_main_frame)
        pattern_left_frame.pack(side="left", fill="both", expand=True, padx=(0, pad))
        
        # Правая панель: визуализация и подсказки
        pattern_right_frame = ttk.Frame(pattern_main_frame, width=350)
        pattern_right_frame.pack(side="right", fill="both", padx=(pad, 0))
        pattern_right_frame.pack_propagate(False)
        
        # Рамка редактора паттерна
        pattern_editor_frame = ttk.LabelFrame(pattern_left_frame, text="📝 Редактор паттерна команд", padding=pad)
        pattern_editor_frame.pack(fill="both", expand=True, pady=(0, pad))

        # Подсказка в отдельной рамке (компактная)
        help_frame = ttk.LabelFrame(pattern_left_frame, text="ℹ️ Справка", padding=pad)
        help_frame.pack(fill="x", pady=(0, pad))

        help_text = """Формат: WRITE reg value [U] [M] | READ reg | CLEAR | DELAY X | # комментарий"""
        ttk.Label(
            help_frame, text=help_text, justify="left", 
            font=('Consolas', 8), foreground='#555555'
        ).pack(anchor="w")

        # Текстовое поле для паттерна
        pattern_text_frame = ttk.Frame(pattern_editor_frame)
        pattern_text_frame.pack(fill="both", expand=True)

        self.txt_pattern = scrolledtext.ScrolledText(
            pattern_text_frame, height=18, wrap=tk.NONE, 
            font=('Consolas', 10), bg='#ffffff', fg='#000000',
            insertbackground='#000000'
        )
        self.txt_pattern.pack(fill="both", expand=True)
        
        # Привязываем событие изменения текста для обновления визуализации
        # Используем lambda, чтобы метод вызывался позже
        self.txt_pattern.bind('<KeyRelease>', lambda e: self.on_pattern_text_change())
        self.txt_pattern.bind('<Button-1>', lambda e: self.on_pattern_text_change())

        # Пример паттерна по умолчанию
        default_pattern = """# Пример паттерна стимуляции канала 0
# ВАЖНО: Настройка шага стимуляции и bias (обязательно!)
WRITE 34 0x00E2 U  # Шаг 1 µA (диапазон ±255 µA)
WRITE 35 0x00AA U  # PBIAS/NBIAS для шага 1 µA

# Настройка токов стимуляции (формат 0x80XX, где XX - значение тока в µA)
WRITE 64 0x8014 U  # Отрицательный ток 20 µA (канал 0)
WRITE 96 0x8014 U  # Положительный ток 20 µA (канал 0)

# Установка полярности (положительная для канала 0)
WRITE 44 0x0001 U
# Включение стимуляции канала 0
WRITE 42 0x0001 U
# Задержка (10 раз READ 255)
DELAY 10
# Выключение стимуляции
WRITE 42 0x0000 U
"""
        self.txt_pattern.insert("1.0", default_pattern)

        # ========== ПРАВАЯ ПАНЕЛЬ: ВИЗУАЛИЗАЦИЯ ==========
        # Рамка визуализации паттерна
        viz_frame = ttk.LabelFrame(pattern_right_frame, text="📊 Визуализация паттерна", padding=pad)
        viz_frame.pack(fill="both", expand=True, pady=(0, pad))
        
        # Canvas для визуализации
        viz_canvas_frame = ttk.Frame(viz_frame)
        viz_canvas_frame.pack(fill="both", expand=True)
        
        self.pattern_viz_canvas = tk.Canvas(
            viz_canvas_frame, bg='#f8f8f8', highlightthickness=1, 
            highlightbackground='#cccccc', height=300
        )
        self.pattern_viz_canvas.pack(fill="both", expand=True)
        
        # Scrollbar для визуализации
        viz_scrollbar = ttk.Scrollbar(viz_canvas_frame, orient="vertical", command=self.pattern_viz_canvas.yview)
        viz_scrollbar.pack(side="right", fill="y")
        self.pattern_viz_canvas.configure(yscrollcommand=viz_scrollbar.set)
        
        # Рамка подсказок
        hints_frame = ttk.LabelFrame(pattern_right_frame, text="💡 Подсказки", padding=pad)
        hints_frame.pack(fill="both", expand=True)
        
        self.hints_text = scrolledtext.ScrolledText(
            hints_frame, height=10, state="disabled", wrap=tk.WORD,
            font=('Arial', 9), bg='#f0f8ff', fg='#333333'
        )
        self.hints_text.pack(fill="both", expand=True)
        
        # Рамка параметров паттерна
        pattern_params_frame = ttk.LabelFrame(pattern_left_frame, text="⚙️ Параметры выполнения", padding=pad)
        pattern_params_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(pattern_params_frame, text="Количество повторений:").grid(
            row=0, column=0, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_pattern_repeat = tk.StringVar(value="1")
        ttk.Entry(pattern_params_frame, textvariable=self.var_pattern_repeat, width=15).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(
            pattern_params_frame, 
            text="(1-10000, выполняется на устройстве)",
            font=('Arial', 8), foreground='gray'
        ).grid(row=0, column=2, sticky="w", padx=pad_small, pady=pad_small)

        # Индикатор загрузки паттерна
        pattern_status_frame = ttk.Frame(pattern_left_frame)
        pattern_status_frame.pack(fill="x", pady=(0, pad))

        self.pattern_status_label = ttk.Label(
            pattern_status_frame, text="● Паттерн не загружен", style='Error.TLabel'
        )
        self.pattern_status_label.pack(side="left", padx=pad_small)

        # Кнопки управления паттерном
        pattern_btn_frame = ttk.Frame(pattern_left_frame)
        pattern_btn_frame.pack(fill="x")

        self.btn_pattern_load = ttk.Button(
            pattern_btn_frame, text="💾 Загрузить в память", 
            command=self.on_pattern_load, state="disabled", style='Primary.TButton', width=20
        )
        self.btn_pattern_load.pack(side="left", padx=pad_small, pady=pad_small)

        self.btn_pattern_run = ttk.Button(
            pattern_btn_frame, text="🚀 Запустить из памяти", 
            command=self.on_pattern_run, state="disabled", style='Primary.TButton', width=20
        )
        self.btn_pattern_run.pack(side="left", padx=pad_small, pady=pad_small)

        self.btn_pattern_clear = ttk.Button(
            pattern_btn_frame, text="🗑 Очистить редактор", command=self.on_pattern_clear, width=16
        )
        self.btn_pattern_clear.pack(side="left", padx=pad_small, pady=pad_small)

        self.btn_pattern_load_example = ttk.Button(
            pattern_btn_frame, text="📄 Пример", command=self.on_pattern_load_example, width=12
        )
        self.btn_pattern_load_example.pack(side="left", padx=pad_small, pady=pad_small)
        
        # Кнопка для копирования из конструктора
        self.btn_copy_from_constructor = ttk.Button(
            pattern_btn_frame, text="📋 Из конструктора", 
            command=self.copy_pattern_from_constructor, width=16
        )
        self.btn_copy_from_constructor.pack(side="left", padx=pad_small, pady=pad_small)

        # Кнопки экспорта/импорта паттерна
        self.btn_pattern_export = ttk.Button(
            pattern_btn_frame, text="💾 Экспорт", 
            command=self.on_pattern_export, width=12
        )
        self.btn_pattern_export.pack(side="left", padx=pad_small, pady=pad_small)

        self.btn_pattern_import = ttk.Button(
            pattern_btn_frame, text="📂 Импорт", 
            command=self.on_pattern_import, width=12
        )
        self.btn_pattern_import.pack(side="left", padx=pad_small, pady=pad_small)

        # ========== ВКЛАДКА 5: СПРАВКА ПО РЕГИСТРАМ ==========
        # Создаем панель с поиском и информацией о регистрах
        registers_main_frame = ttk.Frame(tab_registers)
        registers_main_frame.pack(fill="both", expand=True)

        # Левая панель: список регистров
        registers_left_frame = ttk.Frame(registers_main_frame, width=300)
        registers_left_frame.pack(side="left", fill="both", padx=(0, pad))
        registers_left_frame.pack_propagate(False)

        # Поиск регистра
        search_frame = ttk.LabelFrame(registers_left_frame, text="🔍 Поиск регистра", padding=pad)
        search_frame.pack(fill="x", pady=(0, pad))

        self.var_register_search = tk.StringVar()
        self.var_register_search.trace('w', self.on_register_search_change)
        search_entry = ttk.Entry(search_frame, textvariable=self.var_register_search, width=25)
        search_entry.pack(fill="x", padx=pad_small, pady=pad_small)

        # Список регистров
        list_frame = ttk.LabelFrame(registers_left_frame, text="📋 Регистры", padding=pad)
        list_frame.pack(fill="both", expand=True)

        # Scrollbar для списка
        list_scrollbar = ttk.Scrollbar(list_frame)
        list_scrollbar.pack(side="right", fill="y")

        self.register_listbox = tk.Listbox(
            list_frame, yscrollcommand=list_scrollbar.set,
            font=('Consolas', 10), selectmode=tk.SINGLE
        )
        self.register_listbox.pack(fill="both", expand=True)
        self.register_listbox.bind('<<ListboxSelect>>', self.on_register_select)
        list_scrollbar.config(command=self.register_listbox.yview)

        # Правая панель: детальная информация
        registers_right_frame = ttk.Frame(registers_main_frame)
        registers_right_frame.pack(side="right", fill="both", expand=True)

        # Информация о регистре
        info_frame = ttk.LabelFrame(registers_right_frame, text="📖 Информация о регистре", padding=pad)
        info_frame.pack(fill="both", expand=True)

        self.register_info_text = scrolledtext.ScrolledText(
            info_frame, height=25, state="disabled", wrap=tk.WORD,
            font=('Arial', 10), bg='#ffffff', fg='#000000'
        )
        self.register_info_text.pack(fill="both", expand=True)

        # Инициализация списка регистров
        self.init_register_list()

        # ========== ВКЛАДКА 6: РЕГИСТРАЦИЯ ДАННЫХ ==========
        # Рамка подключения к UDP серверу
        recording_conn_frame = ttk.LabelFrame(tab_recording, text="🔌 Подключение к UDP серверу", padding=pad)
        recording_conn_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(recording_conn_frame, text="UDP Host:").grid(row=0, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_udp_host = tk.StringVar(value="192.168.31.191")
        ttk.Entry(recording_conn_frame, textvariable=self.var_udp_host, width=18).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(recording_conn_frame, text="UDP Port:").grid(row=0, column=2, sticky="w", padx=pad_small, pady=pad_small)
        self.var_udp_port = tk.StringVar(value="9001")
        ttk.Entry(recording_conn_frame, textvariable=self.var_udp_port, width=8).grid(
            row=0, column=3, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(recording_conn_frame, text="Listen Port:").grid(row=0, column=4, sticky="w", padx=pad_small, pady=pad_small)
        self.var_listen_port = tk.StringVar(value="9002")
        ttk.Entry(recording_conn_frame, textvariable=self.var_listen_port, width=8).grid(
            row=0, column=5, sticky="w", padx=pad_small, pady=pad_small
        )

        self.btn_udp_register = ttk.Button(
            recording_conn_frame, text="📡 Зарегистрироваться", 
            command=self.on_udp_register, style='Primary.TButton'
        )
        self.btn_udp_register.grid(row=0, column=6, sticky="w", padx=pad_small, pady=pad_small)

        self.btn_udp_unregister = ttk.Button(
            recording_conn_frame, text="❌ Отменить регистрацию", 
            command=self.on_udp_unregister, state="disabled"
        )
        self.btn_udp_unregister.grid(row=0, column=7, sticky="w", padx=pad_small, pady=pad_small)

        # Статус регистрации
        self.udp_status_label = ttk.Label(
            recording_conn_frame, text="● Не зарегистрирован", style='Error.TLabel'
        )
        self.udp_status_label.grid(row=1, column=0, columnspan=8, sticky="w", padx=pad_small, pady=pad_small)

        # Рамка параметров регистрации
        recording_params_frame = ttk.LabelFrame(tab_recording, text="⚙️ Параметры регистрации", padding=pad)
        recording_params_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(recording_params_frame, text="Каналы:").grid(row=0, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_recording_channels = tk.StringVar(value="0-15")
        ttk.Entry(recording_params_frame, textvariable=self.var_recording_channels, width=20).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(recording_params_frame, text="Частота (Hz):").grid(row=0, column=2, sticky="w", padx=pad_small, pady=pad_small)
        self.var_sample_rate = tk.StringVar(value="40000")
        ttk.Entry(recording_params_frame, textvariable=self.var_sample_rate, width=12).grid(
            row=0, column=3, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(recording_params_frame, text="Длительность (с):").grid(row=0, column=4, sticky="w", padx=pad_small, pady=pad_small)
        self.var_recording_duration = tk.StringVar(value="")
        ttk.Entry(recording_params_frame, textvariable=self.var_recording_duration, width=12).grid(
            row=0, column=5, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(recording_params_frame, text="(пусто = бесконечно)").grid(
            row=0, column=6, sticky="w", padx=pad_small, pady=pad_small
        )

        # Рамка для настройки ADC bias (Register 0)
        adc_bias_frame = ttk.LabelFrame(tab_recording, text="⚙️ Настройки ADC (Register 0)", padding=pad)
        adc_bias_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(adc_bias_frame, text="ADC sampling rate (kS/s):").grid(row=0, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_adc_sampling_rate = tk.StringVar(value="480")
        adc_rate_entry = ttk.Entry(adc_bias_frame, textvariable=self.var_adc_sampling_rate, width=12)
        adc_rate_entry.grid(row=0, column=1, sticky="w", padx=pad_small, pady=pad_small)
        adc_rate_entry.bind('<KeyRelease>', self._on_adc_rate_changed)

        ttk.Label(adc_bias_frame, text="ADC buffer bias:").grid(row=0, column=2, sticky="w", padx=pad_small, pady=pad_small)
        self.var_adc_buffer_bias = tk.StringVar(value="3")
        ttk.Entry(adc_bias_frame, textvariable=self.var_adc_buffer_bias, width=8).grid(
            row=0, column=3, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(adc_bias_frame, text="MUX bias:").grid(row=0, column=4, sticky="w", padx=pad_small, pady=pad_small)
        self.var_mux_bias = tk.StringVar(value="5")
        ttk.Entry(adc_bias_frame, textvariable=self.var_mux_bias, width=8).grid(
            row=0, column=5, sticky="w", padx=pad_small, pady=pad_small
        )

        self.btn_apply_adc_bias = ttk.Button(
            adc_bias_frame, text="Применить настройки ADC", 
            command=self.on_apply_adc_bias, style='Primary.TButton', width=22
        )
        self.btn_apply_adc_bias.grid(row=0, column=6, sticky="w", padx=pad_small, pady=pad_small)

        self.btn_auto_adc_bias = ttk.Button(
            adc_bias_frame, text="Авто (по частоте)", 
            command=self.on_auto_adc_bias, width=18
        )
        self.btn_auto_adc_bias.grid(row=0, column=7, sticky="w", padx=pad_small, pady=pad_small)

        # Подсказка с таблицей
        hint_label = ttk.Label(
            adc_bias_frame, 
            text="Подсказка: ≤120→(32,40), 140→(16,40), 175→(8,40), 220→(8,32), 280→(8,26), 350→(4,18), 440→(3,16), ≥440→(3,5)",
            font=('Arial', 8), foreground='gray'
        )
        hint_label.grid(row=1, column=0, columnspan=8, sticky="w", padx=pad_small, pady=(0, pad_small))

        # Рамка для настройки аппаратных фильтров
        filter_frame = ttk.LabelFrame(tab_recording, text="🔧 Аппаратные фильтры", padding=pad)
        filter_frame.pack(fill="x", pady=(0, pad))

        # Верхняя частота среза (fH) - Register 4-5
        ttk.Label(filter_frame, text="Верхняя частота (fH):").grid(row=0, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_fh_freq = tk.StringVar(value="500")
        fh_freq_entry = ttk.Entry(filter_frame, textvariable=self.var_fh_freq, width=10)
        fh_freq_entry.grid(row=0, column=1, sticky="w", padx=pad_small, pady=pad_small)
        ttk.Label(filter_frame, text="Hz").grid(row=0, column=2, sticky="w", padx=(0, pad_small), pady=pad_small)

        ttk.Label(filter_frame, text="Register 4 (hex):").grid(row=0, column=3, sticky="w", padx=pad_small, pady=pad_small)
        self.var_reg4 = tk.StringVar(value="0x015E")
        ttk.Entry(filter_frame, textvariable=self.var_reg4, width=10).grid(row=0, column=4, sticky="w", padx=pad_small, pady=pad_small)

        ttk.Label(filter_frame, text="Register 5 (hex):").grid(row=0, column=5, sticky="w", padx=pad_small, pady=pad_small)
        self.var_reg5 = tk.StringVar(value="0x01AB")
        ttk.Entry(filter_frame, textvariable=self.var_reg5, width=10).grid(row=0, column=6, sticky="w", padx=pad_small, pady=pad_small)

        # Нижняя частота среза (fL) - Register 6-7
        ttk.Label(filter_frame, text="Нижняя частота (fL):").grid(row=1, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_fl_freq = tk.StringVar(value="20")
        fl_freq_entry = ttk.Entry(filter_frame, textvariable=self.var_fl_freq, width=10)
        fl_freq_entry.grid(row=1, column=1, sticky="w", padx=pad_small, pady=pad_small)
        ttk.Label(filter_frame, text="Hz").grid(row=1, column=2, sticky="w", padx=(0, pad_small), pady=pad_small)

        ttk.Label(filter_frame, text="Register 6 (hex):").grid(row=1, column=3, sticky="w", padx=pad_small, pady=pad_small)
        self.var_reg6 = tk.StringVar(value="0x0036")
        ttk.Entry(filter_frame, textvariable=self.var_reg6, width=10).grid(row=1, column=4, sticky="w", padx=pad_small, pady=pad_small)

        ttk.Label(filter_frame, text="Register 7 (hex):").grid(row=1, column=5, sticky="w", padx=pad_small, pady=pad_small)
        self.var_reg7 = tk.StringVar(value="0x000A")
        ttk.Entry(filter_frame, textvariable=self.var_reg7, width=10).grid(row=1, column=6, sticky="w", padx=pad_small, pady=pad_small)

        # DSP HPF cutoff - Register 1
        ttk.Label(filter_frame, text="DSP HPF cutoff:").grid(row=2, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_dsp_cutoff = tk.StringVar(value="9")
        ttk.Entry(filter_frame, textvariable=self.var_dsp_cutoff, width=10).grid(row=2, column=1, sticky="w", padx=pad_small, pady=pad_small)
        ttk.Label(filter_frame, text="(0-15, для f_sample≈1kHz: 8-10)").grid(row=2, column=2, sticky="w", padx=(0, pad_small), pady=pad_small)

        ttk.Label(filter_frame, text="Register 1 (hex):").grid(row=2, column=3, sticky="w", padx=pad_small, pady=pad_small)
        self.var_reg1 = tk.StringVar(value="0x951A")
        ttk.Entry(filter_frame, textvariable=self.var_reg1, width=10).grid(row=2, column=4, sticky="w", padx=pad_small, pady=pad_small)

        # Кнопки
        self.btn_apply_filters = ttk.Button(
            filter_frame, text="Применить фильтры", 
            command=self.on_apply_filters, style='Primary.TButton', width=20
        )
        self.btn_apply_filters.grid(row=2, column=5, columnspan=2, sticky="w", padx=pad_small, pady=pad_small)

        self.btn_auto_filters_emg = ttk.Button(
            filter_frame, text="Авто (ЭМГ)", 
            command=self.on_auto_filters_emg, width=15
        )
        self.btn_auto_filters_emg.grid(row=3, column=0, columnspan=2, sticky="w", padx=pad_small, pady=pad_small)

        # Подсказка
        filter_hint = ttk.Label(
            filter_frame, 
            text="Рекомендации для ЭМГ: fH=500 Hz (Reg4=0x015E, Reg5=0x01AB), fL=20 Hz (Reg6=0x0036), DSP cutoff=9 (Reg1=0x951A)",
            font=('Arial', 8), foreground='gray'
        )
        filter_hint.grid(row=3, column=2, columnspan=5, sticky="w", padx=pad_small, pady=pad_small)

        # Кнопки управления регистрацией
        recording_btn_frame = ttk.Frame(tab_recording)
        recording_btn_frame.pack(fill="x", pady=pad)

        self.btn_start_recording = ttk.Button(
            recording_btn_frame, text="▶ Начать регистрацию", 
            command=self.on_start_recording, state="disabled", style='Primary.TButton', width=20
        )
        self.btn_start_recording.pack(side="left", padx=pad_small, pady=pad_small)

        self.btn_stop_recording = ttk.Button(
            recording_btn_frame, text="⏹ Остановить регистрацию", 
            command=self.on_stop_recording, state="disabled", style='Danger.TButton', width=22
        )
        self.btn_stop_recording.pack(side="left", padx=pad_small, pady=pad_small)

        # Рамка для графиков данных
        recording_data_frame = ttk.LabelFrame(tab_recording, text="📈 Данные регистрации", padding=pad)
        recording_data_frame.pack(fill="both", expand=True)

        # Статистика и кнопки управления
        stats_frame = ttk.Frame(recording_data_frame)
        stats_frame.pack(fill="x", pady=(0, pad))

        self.recording_stats_label = ttk.Label(
            stats_frame, text="Получено пакетов: 0 | Каналов: 0", style='Status.TLabel'
        )
        self.recording_stats_label.pack(side="left", padx=pad_small)

        # Кнопки управления графиком
        graph_btn_frame = ttk.Frame(stats_frame)
        graph_btn_frame.pack(side="right", padx=pad_small)

        self.btn_clear_graph = ttk.Button(
            graph_btn_frame, text="🗑 Очистить график", 
            command=self.on_clear_graph, width=18
        )
        self.btn_clear_graph.pack(side="left", padx=pad_small)

        self.btn_export_data = ttk.Button(
            graph_btn_frame, text="💾 Экспорт данных", 
            command=self.on_export_recording_data, width=18
        )
        self.btn_export_data.pack(side="left", padx=pad_small)

        self.btn_export_graph = ttk.Button(
            graph_btn_frame, text="📊 Экспорт графика", 
            command=self.on_export_graph, width=18
        )
        self.btn_export_graph.pack(side="left", padx=pad_small)

        self.btn_parse_data = ttk.Button(
            graph_btn_frame, text="🔍 Построить график", 
            command=self.on_parse_and_plot, width=18
        )
        self.btn_parse_data.pack(side="left", padx=pad_small)

        self.btn_refresh_text = ttk.Button(
            graph_btn_frame, text="🔄 Обновить текст", 
            command=self.on_refresh_text_display, width=18
        )
        self.btn_refresh_text.pack(side="left", padx=pad_small)

        # Создаем Notebook для вкладок: график и текст
        recording_notebook = ttk.Notebook(recording_data_frame)
        recording_notebook.pack(fill="both", expand=True)

        # Вкладка 1: График
        graph_tab = ttk.Frame(recording_notebook)
        recording_notebook.add(graph_tab, text="📊 График")

        # Вкладка 2: Текст
        text_tab = ttk.Frame(recording_notebook)
        recording_notebook.add(text_tab, text="📝 Текст")

        # График (если matplotlib доступен)
        if MATPLOTLIB_AVAILABLE:
            # Создаем фигуру matplotlib
            self.recording_figure = Figure(figsize=(10, 6), dpi=100)
            self.recording_ax = self.recording_figure.add_subplot(111)
            self.recording_ax.set_xlabel('Время (с)', fontsize=10)
            self.recording_ax.set_ylabel('Напряжение, µВ', fontsize=10)
            self.recording_ax.set_title('Регистрация данных Intan RHS2116', fontsize=12, fontweight='bold')
            self.recording_ax.grid(True, alpha=0.3)
            
            # Создаем canvas для графика
            self.recording_canvas = FigureCanvasTkAgg(self.recording_figure, graph_tab)
            self.recording_canvas.draw()
            self.recording_canvas.get_tk_widget().pack(side="top", fill="both", expand=True)
            
            # Добавляем toolbar для масштабирования
            toolbar_frame = ttk.Frame(graph_tab)
            toolbar_frame.pack(side="top", fill="x")
            self.recording_toolbar = NavigationToolbar2Tk(self.recording_canvas, toolbar_frame)
            self.recording_toolbar.update()
        else:
            # Если matplotlib недоступен, показываем сообщение
            no_graph_label = ttk.Label(
                graph_tab, 
                text="Для отображения графиков установите matplotlib:\npip install matplotlib",
                font=('Arial', 10), justify='center'
            )
            no_graph_label.pack(expand=True)

        # Текстовое поле для отображения данных
        self.recording_data_text = scrolledtext.ScrolledText(
            text_tab, height=15, state="disabled", wrap=tk.WORD,
            font=('Consolas', 9), bg='#f8f8f8', fg='#333333'
        )
        self.recording_data_text.pack(fill="both", expand=True)

        # ========== ВКЛАДКА 7: ИЗМЕРЕНИЯ (ТЕМПЕРАТУРА И ИМПЕДАНС) ==========
        # Рамка измерения температуры
        temp_frame = ttk.LabelFrame(tab_measurements, text="🌡️ Температура", padding=pad)
        temp_frame.pack(fill="x", pady=(0, pad))

        temp_btn_frame = ttk.Frame(temp_frame)
        temp_btn_frame.pack(fill="x", pady=pad_small)

        self.btn_read_temp = ttk.Button(
            temp_btn_frame, text="📊 Измерить температуру", 
            command=self.on_read_temperature, state="disabled", style='Primary.TButton', width=25
        )
        self.btn_read_temp.pack(side="left", padx=pad_small, pady=pad_small)

        self.temp_value_label = ttk.Label(
            temp_frame, text="Температура: --", font=('Arial', 12, 'bold')
        )
        self.temp_value_label.pack(side="left", padx=pad_small, pady=pad_small)

        # Рамка проверки тока стимуляции
        current_check_frame = ttk.LabelFrame(tab_measurements, text="🔌 Проверка тока стимуляции (осциллограф)", padding=pad)
        current_check_frame.pack(fill="x", pady=(0, pad))
        
        ttk.Label(current_check_frame, text="Метод измерения тока через осциллограф:", font=("TkDefaultFont", 9, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=pad_small, pady=pad_small)
        
        info_text = """Для измерения тока стимуляции используйте токовый шунт (резистор):
1. Подключите прецизионный резистор (R_shunt) последовательно с электродом
2. Измерьте напряжение на резисторе осциллографом
3. Введите номинал резистора и измеренное напряжение ниже
4. Система рассчитает ток: I = U / R_shunt"""
        
        info_label = ttk.Label(current_check_frame, text=info_text, justify="left", font=("TkDefaultFont", 8))
        info_label.grid(row=1, column=0, columnspan=3, sticky="w", padx=pad_small, pady=pad_small)
        
        ttk.Label(current_check_frame, text="Номинал шунта (кОм):").grid(row=2, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_shunt_resistance = tk.StringVar(value="1.0")
        ttk.Entry(current_check_frame, textvariable=self.var_shunt_resistance, width=10).grid(row=2, column=1, sticky="w", padx=pad_small, pady=pad_small)
        
        ttk.Label(current_check_frame, text="Напряжение на шунте (мВ):").grid(row=3, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_shunt_voltage = tk.StringVar(value="0.0")
        ttk.Entry(current_check_frame, textvariable=self.var_shunt_voltage, width=10).grid(row=3, column=1, sticky="w", padx=pad_small, pady=pad_small)
        
        def calculate_current():
            try:
                R_kohm = float(self.var_shunt_resistance.get())
                U_mv = float(self.var_shunt_voltage.get())
                
                if R_kohm <= 0:
                    self.log("Ошибка: сопротивление шунта должно быть > 0", "error")
                    return
                
                # Переводим в основные единицы
                R_ohm = R_kohm * 1000  # кОм -> Ом
                U_v = U_mv / 1000  # мВ -> В
                
                # Рассчитываем ток: I = U / R
                I_a = U_v / R_ohm
                I_ua = I_a * 1e6  # А -> мкА
                
                # Отображаем результат
                result_text = f"Измеренный ток: {I_ua:.2f} µA ({I_a*1e3:.3f} мА)"
                self.log(result_text, "info")
                
                # Показываем в отдельном окне с подробностями
                result_window = tk.Toplevel(self)
                result_window.title("Результат измерения тока")
                result_window.geometry("400x200")
                
                ttk.Label(result_window, text="Результаты измерения тока стимуляции", font=("TkDefaultFont", 10, "bold")).pack(pady=10)
                
                details = f"""Параметры измерения:
• Сопротивление шунта: {R_kohm} кОм ({R_ohm} Ом)
• Напряжение на шунте: {U_mv} мВ ({U_v} В)

Результат:
• Ток стимуляции: {I_ua:.2f} µA ({I_a*1e3:.3f} мА)

Формула: I = U / R = {U_v:.6f} В / {R_ohm} Ом = {I_a:.9f} А"""
                
                ttk.Label(result_window, text=details, justify="left", font=("TkDefaultFont", 9)).pack(pady=10, padx=20)
                
                ttk.Button(result_window, text="Закрыть", command=result_window.destroy).pack(pady=10)
                
            except ValueError:
                self.log("Ошибка: введите числовые значения", "error")
        
        ttk.Button(current_check_frame, text="Рассчитать ток", command=calculate_current).grid(row=4, column=0, columnspan=2, pady=pad_small)

        # Рамка измерения импеданса (RHS2116 Zcheck: Reg 2 — control, Reg 3 — DAC, см. RHS2116_impedance_measurement.md)
        impedance_frame = ttk.LabelFrame(tab_measurements, text="⚡ Импеданс (RHS2116 Zcheck)", padding=pad)
        impedance_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(impedance_frame, text="Канал (0–15):").grid(row=0, column=0, sticky="w", padx=pad_small, pady=pad_small)
        self.var_impedance_channel = tk.StringVar(value="0")
        ttk.Entry(impedance_frame, textvariable=self.var_impedance_channel, width=6).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(impedance_frame, text="Шкала C (Reg 2 [4:3]):").grid(row=0, column=2, sticky="w", padx=pad_small, pady=pad_small)
        self.var_impedance_scale = tk.StringVar(value="1 pF")
        scale_combo = ttk.Combobox(impedance_frame, textvariable=self.var_impedance_scale, width=10, state="readonly")
        scale_combo["values"] = ("0.1 pF", "1 pF", "10 pF")
        scale_combo.grid(row=0, column=3, sticky="w", padx=pad_small, pady=pad_small)

        ttk.Label(impedance_frame, text="Частота (Hz):").grid(row=0, column=4, sticky="w", padx=pad_small, pady=pad_small)
        self.var_impedance_freq = tk.StringVar(value="1000")
        ttk.Entry(impedance_frame, textvariable=self.var_impedance_freq, width=8).grid(
            row=0, column=5, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(impedance_frame, text="Усреднений:").grid(row=0, column=6, sticky="w", padx=pad_small, pady=pad_small)
        self.var_impedance_averages = tk.StringVar(value="10")
        tk.Spinbox(impedance_frame, textvariable=self.var_impedance_averages, from_=1, to=200, width=5).grid(
            row=0, column=7, sticky="w", padx=pad_small, pady=pad_small
        )

        self.var_impedance_auto_scale = tk.BooleanVar(value=False)
        ttk.Checkbutton(impedance_frame, text="Авто C (250 µV)", variable=self.var_impedance_auto_scale).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=pad_small, pady=pad_small
        )

        self.btn_measure_impedance = ttk.Button(
            impedance_frame, text="📊 Измерить импеданс",
            command=self.on_measure_impedance, state="disabled", style='Primary.TButton', width=20
        )
        self.btn_measure_impedance.grid(row=1, column=2, columnspan=2, sticky="w", padx=pad_small, pady=pad_small)

        self.btn_export_impedance_csv = ttk.Button(
            impedance_frame, text="📁 Выгрузить CSV",
            command=self.on_export_impedance_csv, state="disabled", width=15
        )
        self.btn_export_impedance_csv.grid(row=1, column=4, columnspan=2, sticky="w", padx=pad_small, pady=pad_small)

        self.impedance_value_label = ttk.Label(
            impedance_frame, text="Импеданс: --", font=('Arial', 12, 'bold')
        )
        self.impedance_value_label.grid(row=2, column=0, columnspan=7, sticky="w", padx=pad_small, pady=pad_small)

        ttk.Label(
            impedance_frame,
            text="Как RHD2000 (jonnew/impedance): bestAmplitude 250 µV, factorOutParallelCapacitance 10 pF. DAC 0.6125 V. Ограничения: ±5 mV насыщение.",
            font=('Arial', 8), foreground='gray'
        ).grid(row=3, column=0, columnspan=8, sticky="w", padx=pad_small, pady=(0, pad_small))

        # Рамка настройки восстановления заряда (Charge Recovery)
        recovery_frame = ttk.LabelFrame(tab_measurements, text="🔁 Восстановление заряда (Registers 36, 37)", padding=pad)
        recovery_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(recovery_frame, text="Register 36 (Vrecov), значение (hex или dec):").grid(
            row=0, column=0, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_recovery_reg36 = tk.StringVar(value="0x0080")
        ttk.Entry(recovery_frame, textvariable=self.var_recovery_reg36, width=12).grid(
            row=0, column=1, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(recovery_frame, text="Register 37 (Imax), значение (hex или dec):").grid(
            row=1, column=0, sticky="w", padx=pad_small, pady=pad_small
        )
        self.var_recovery_reg37 = tk.StringVar(value="0x4F00")
        ttk.Entry(recovery_frame, textvariable=self.var_recovery_reg37, width=12).grid(
            row=1, column=1, sticky="w", padx=pad_small, pady=pad_small
        )

        ttk.Label(
            recovery_frame,
            text="Рекомендуемые значения взяты из примера Intan (мягкое восстановление ~1 nA).",
            font=("Arial", 8),
            foreground="gray",
        ).grid(row=0, column=2, columnspan=3, sticky="w", padx=pad_small, pady=pad_small)

        btn_recovery_apply = ttk.Button(
            recovery_frame,
            text="✅ Применить 36/37",
            command=self.on_set_recovery_registers,
            style="Primary.TButton",
            width=20,
        )
        btn_recovery_apply.grid(row=2, column=0, columnspan=2, sticky="w", padx=pad_small, pady=pad_small)

    # ----------------- Помощники GUI -----------------

    def log(self, msg: str, level: str = "info"):
        """Добавляет сообщение в лог с временной меткой и цветом"""
        self.txt_log.configure(state="normal")
        
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        # Цвета для разных уровней
        colors = {
            "info": "#333333",
            "success": "#008000",
            "error": "#CC0000",
            "warning": "#FF6600"
        }
        
        color = colors.get(level, colors["info"])
        
        # Добавляем временную метку
        self.txt_log.insert("end", f"[{timestamp}] ", "timestamp")
        self.txt_log.insert("end", msg + "\n", level)
        
        # Настройка тегов для цветов
        self.txt_log.tag_config("timestamp", foreground="#666666", font=('Consolas', 9))
        self.txt_log.tag_config("info", foreground=colors["info"])
        self.txt_log.tag_config("success", foreground=colors["success"], font=('Consolas', 9, 'bold'))
        self.txt_log.tag_config("error", foreground=colors["error"], font=('Consolas', 9, 'bold'))
        self.txt_log.tag_config("warning", foreground=colors["warning"])
        
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def update_status(self, connected: bool, message: str = ""):
        """Обновляет статус подключения"""
        self.connected = connected
        if connected:
            self.status_label.config(text=f"● Подключено {message}", style='Success.TLabel')
        else:
            self.status_label.config(text="● Отключено", style='Error.TLabel')

    def set_connected_state(self, connected: bool):
        state = "normal" if connected else "disabled"
        self.btn_ping.configure(state=state)
        self.btn_init.configure(state=state)
        self.btn_pulse.configure(state=state)
        self.btn_saw.configure(state=state)
        self.btn_stop.configure(state=state)
        self.btn_pattern_load.configure(state=state)
        self.btn_pattern_run.configure(state=state)
        self.btn_read_temp.configure(state=state)
        self.btn_measure_impedance.configure(state=state)
        self.btn_check_intan.configure(state=state)
        self.btn_apply_adc_bias.configure(state=state)
        self.btn_apply_filters.configure(state=state)
        self.btn_auto_filters_emg.configure(state=state)
        self.update_status(connected)

    def on_clear_log(self):
        """Очищает лог"""
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.configure(state="disabled")
        self.log("Лог очищен", "info")

    def on_save_log(self):
        """Сохраняет лог в файл"""
        try:
            from tkinter import filedialog
            filename = filedialog.asksaveasfilename(
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
            )
            if filename:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(self.txt_log.get("1.0", tk.END))
                self.log(f"Лог сохранен в {filename}", "success")
        except Exception as e:
            self.log(f"Ошибка сохранения лога: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось сохранить лог: {e}")

    # ----------------- Обработчики событий -----------------

    def on_connect(self):
        host = self.var_host.get().strip()
        port_str = self.var_port.get().strip()
        try:
            port = int(port_str)
        except ValueError:
            messagebox.showerror("Ошибка", "Порт должен быть числом")
            return

        def worker():
            try:
                self.client.connect(host, port)
                self.log(f"Подключено к {host}:{port}", "success")
                self.set_connected_state(True)
                self.update_status(True, f"({host}:{port})")
                # Отправим ping для проверки
                try:
                    resp = self.client.send_command({"cmd": "ping"})
                    self.log(f"Ping: {resp.get('reply', 'ok')}", "success")
                except Exception as e:
                    self.log(f"Ping после подключения: ошибка: {e}", "warning")
            except Exception as e:
                self.log(f"Ошибка подключения: {e}", "error")
                messagebox.showerror("Ошибка подключения", str(e))
                self.set_connected_state(False)

        threading.Thread(target=worker, daemon=True).start()

    def _send_async(self, cmd: dict, description: str):
        def worker():
            try:
                self.log(f"Отправка: {json.dumps(cmd, indent=2, ensure_ascii=False)}", "info")
                resp = self.client.send_command(cmd)
                self.log(f"Ответ: {json.dumps(resp, indent=2, ensure_ascii=False)}", "success")
            except Exception as e:
                self.log(f"Ошибка команды '{description}': {e}", "error")
                messagebox.showerror("Ошибка", f"{description}: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_ping(self):
        self._send_async({"cmd": "ping"}, "ping")

    def on_init(self):
        self._send_async({"cmd": "init"}, "init")

    def on_check_intan(self):
        """Проверяет подключение Intan, читая регистр 255 до получения значения 32"""
        def worker():
            try:
                self.log("Начинаем проверку подключения Intan (чтение регистра 255)...", "info")
                max_attempts = 10
                success = False
                
                for attempt in range(1, max_attempts + 1):
                    try:
                        cmd = {"cmd": "read_register", "address": 255}
                        resp = self.client.send_command(cmd)
                        
                        if resp.get("status") == "ok":
                            value = resp.get("value", 0)
                            if isinstance(value, str):
                                # Если значение в hex формате, конвертируем
                                if value.startswith("0x") or value.startswith("0X"):
                                    value = int(value, 16)
                                else:
                                    value = int(value)
                            
                            self.log(f"Попытка {attempt}/{max_attempts}: регистр 255 = {value} (0x{value:04X})", "info")
                            
                            if value == 32:
                                success = True
                                self.after(0, lambda: messagebox.showinfo(
                                    "Успех", 
                                    f"Intan RHS2116 обнаружен!\nРегистр 255 = {value} (0x{value:04X})\nПопытка: {attempt}/{max_attempts}"
                                ))
                                self.after(0, lambda: self.log(f"✓ Intan RHS2116 успешно обнаружен (регистр 255 = {value})", "success"))
                                break
                        else:
                            error_msg = resp.get("error", "Неизвестная ошибка")
                            self.log(f"Попытка {attempt}/{max_attempts}: ошибка чтения регистра 255: {error_msg}", "warning")
                        
                        # Небольшая задержка между попытками
                        if attempt < max_attempts:
                            import time
                            time.sleep(0.1)
                            
                    except Exception as e:
                        self.log(f"Попытка {attempt}/{max_attempts}: исключение при чтении регистра 255: {e}", "warning")
                        if attempt < max_attempts:
                            import time
                            time.sleep(0.1)
                
                if not success:
                    self.after(0, lambda: messagebox.showerror(
                        "Ошибка",
                        f"Не удалось обнаружить Intan RHS2116 после {max_attempts} попыток.\n"
                        f"Ожидаемое значение регистра 255: 32 (0x0020)\n"
                        f"Проверьте подключение и инициализацию устройства."
                    ))
                    self.after(0, lambda: self.log(f"✗ Проверка Intan не удалась после {max_attempts} попыток", "error"))
                    
            except Exception as e:
                self.after(0, lambda: self.log(f"Ошибка проверки подключения Intan: {e}", "error"))
                self.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось проверить подключение Intan: {e}"))
        
        # Запускаем worker в отдельном потоке, чтобы не блокировать GUI
        threading.Thread(target=worker, daemon=True).start()

    def on_pulse(self):
        channels = self.var_channels.get().strip()
        try:
            neg = int(self.var_neg.get().strip())
            pos = int(self.var_pos.get().strip())
            pulse_duration_ms = float(self.var_pulse_duration.get().strip())
            inter_pulse_delay_ms = float(self.var_inter_pulse_delay.get().strip())
            repeat_count = int(self.var_repeat_count.get().strip())
            
            if not (0 <= neg <= 255 and 0 <= pos <= 255):
                messagebox.showerror("Ошибка", "Neg и Pos должны быть в диапазоне 0-255")
                return
            if pulse_duration_ms < 0 or inter_pulse_delay_ms < 0:
                messagebox.showerror("Ошибка", "Длительность и задержка должны быть >= 0")
                return
            if repeat_count < 1:
                messagebox.showerror("Ошибка", "Количество повторений должно быть >= 1")
                return
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверное значение параметра: {e}")
            return

        cmd = {
            "cmd": "pulse",
            "channels": channels,
            "neg": neg,
            "pos": pos,
            "pulse_duration": pulse_duration_ms / 1000.0,
            "inter_pulse_delay": inter_pulse_delay_ms / 1000.0,
            "repeat_count": repeat_count,
        }
        self._send_async(cmd, "pulse")

    def on_sawtooth(self):
        channels = self.var_saw_channels.get().strip()
        try:
            pos = int(self.var_saw_pos.get().strip())
            steps = int(self.var_saw_steps.get().strip())
            duration_ms = float(self.var_saw_duration.get().strip())
            repeat = int(self.var_saw_repeat.get().strip())
            
            if not (0 <= pos <= 255):
                messagebox.showerror("Ошибка", "Pos должен быть в диапазоне 0-255")
                return
            if steps < 1:
                messagebox.showerror("Ошибка", "Количество шагов должно быть >= 1")
                return
            if duration_ms <= 0:
                messagebox.showerror("Ошибка", "Длительность должна быть > 0")
                return
            if repeat < 1:
                messagebox.showerror("Ошибка", "Количество циклов должно быть >= 1")
                return
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверное значение параметра: {e}")
            return

        # Отправляем несколько циклов, если нужно
        for cycle in range(repeat):
            if repeat > 1:
                self.log(f"Цикл пилы {cycle + 1}/{repeat}", "info")
            cmd = {
                "cmd": "sawtooth",
                "channels": channels,
                "pos": pos,
                "steps": steps,
                "duration": duration_ms / 1000.0,
            }
            self._send_async(cmd, f"sawtooth (цикл {cycle + 1}/{repeat})")

    def on_stop(self):
        self._send_async({"cmd": "stop"}, "stop")

    def on_pattern_load(self):
        """Загружает паттерн в память устройства"""
        pattern_text = self.txt_pattern.get("1.0", tk.END)
        pattern_lines = [line.strip() for line in pattern_text.split('\n') if line.strip()]
        
        if len(pattern_lines) > 200:
            messagebox.showerror("Ошибка", "Максимум 200 команд в паттерне")
            return
        
        if not pattern_lines:
            messagebox.showerror("Ошибка", "Паттерн не может быть пустым")
            return
        
        # ВАЖНО: Проверяем наличие регистров 34-35 (step size и bias)
        # Эти регистры критически важны для работы стимуляторов
        has_reg34 = False
        has_reg35 = False
        has_stimulation = False  # Есть ли команды стимуляции (42, 44, 64-79, 96-111)
        
        for line in pattern_lines:
            if line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) >= 3 and parts[0].upper() == "WRITE":
                try:
                    reg = int(parts[1], 0)
                    if reg == 34:
                        has_reg34 = True
                    elif reg == 35:
                        has_reg35 = True
                    elif reg in [42, 44] or (64 <= reg <= 79) or (96 <= reg <= 111):
                        has_stimulation = True
                except:
                    pass
        
        # Если есть команды стимуляции, но нет регистров 34-35, предупреждаем
        if has_stimulation and (not has_reg34 or not has_reg35):
            msg = "⚠ Внимание: В паттерне отсутствуют регистры 34-35 (step size и bias),\n"
            msg += "которые критически важны для работы стимуляторов.\n\n"
            msg += "Добавить их автоматически в начало паттерна?"
            if messagebox.askyesno("Предупреждение", msg):
                # Добавляем регистры 34-35 в начало
                prepend_lines = [
                    "# ВАЖНО: Настройка шага стимуляции и bias (добавлено автоматически)",
                    "WRITE 34 0x00E2 U  # Шаг 1 µA (диапазон ±255 µA)",
                    "WRITE 35 0x00AA U  # PBIAS/NBIAS для шага 1 µA",
                    ""
                ]
                pattern_lines = prepend_lines + pattern_lines
                # Обновляем текст в редакторе
                new_text = "\n".join(pattern_lines) + "\n"
                self.txt_pattern.delete("1.0", tk.END)
                self.txt_pattern.insert("1.0", new_text)
                self.log("Регистры 34-35 добавлены в начало паттерна", "info")
        
        def worker():
            try:
                # КРИТИЧНО: Нормализуем паттерн на ПК перед отправкой на Orange Pi
                # Преобразуем значения токов в формат 0x80XX, чтобы паттерн был готов
                # к локальному выполнению на Orange Pi без задержек Wi-Fi
                normalized_lines = []
                for line in pattern_lines:
                    if line.startswith('#'):
                        normalized_lines.append(line)
                        continue
                    
                    parts = line.split()
                    if len(parts) >= 3 and parts[0].upper() == "WRITE":
                        try:
                            reg = int(parts[1], 0)
                            value = int(parts[2], 0)
                            
                            # Преобразуем значения для регистров токов стимуляции (64-79, 96-111)
                            # в формат 0x80XX на ПК, чтобы паттерн был готов к локальному выполнению
                            if (64 <= reg <= 79) or (96 <= reg <= 111):
                                # Если значение не в формате 0x80XX, преобразуем его
                                if value < 0x8000 or value > 0x80FF:
                                    if 0 <= value <= 255:
                                        value = 0x8000 | (value & 0xFF)
                                        # Обновляем строку с новым значением
                                        new_parts = parts.copy()
                                        new_parts[2] = f"0x{value:04X}"
                                        # Сохраняем остальные части (U, M, комментарии)
                                        if len(parts) > 3:
                                            new_parts.extend(parts[3:])
                                        line = " ".join(new_parts)
                        except:
                            pass  # Если не удалось преобразовать, оставляем как есть
                    
                    normalized_lines.append(line)
                
                cmd = {
                    "cmd": "pattern_load",
                    "commands": normalized_lines,
                }
                self.log(f"Загрузка паттерна в память Orange Pi...", "info")
                resp = self.client.send_command(cmd)
                commands_count = resp.get("commands_count", 0)
                self.log(f"✓ Паттерн загружен на Orange Pi: {commands_count} команд (готов к локальному выполнению)", "success")
                self.pattern_status_label.config(
                    text=f"● Паттерн загружен ({commands_count} команд)", 
                    style='Success.TLabel'
                )
                self.btn_pattern_run.configure(state="normal")
            except Exception as e:
                self.log(f"Ошибка загрузки паттерна: {e}", "error")
                messagebox.showerror("Ошибка", f"Не удалось загрузить паттерн: {e}")
                self.pattern_status_label.config(
                    text="● Ошибка загрузки", 
                    style='Error.TLabel'
                )
        
        threading.Thread(target=worker, daemon=True).start()

    def on_pattern_run(self):
        """Запускает паттерн из памяти устройства"""
        try:
            repeat_count = int(self.var_pattern_repeat.get().strip())
            if repeat_count < 1:
                messagebox.showerror("Ошибка", "Количество повторений должно быть >= 1")
                return
            if repeat_count > 10000:
                messagebox.showerror("Ошибка", "Количество повторений не должно превышать 10000")
                return
        except ValueError:
            messagebox.showerror("Ошибка", "Количество повторений должно быть числом")
            return
        
        cmd = {
            "cmd": "pattern_run",
            "repeat_count": repeat_count,
        }
        self._send_async(cmd, f"pattern_run (повторений: {repeat_count})")

    def on_pattern_clear(self):
        self.txt_pattern.delete("1.0", tk.END)
        self.on_pattern_text_change()

    def on_pattern_export(self):
        """Экспортирует текущий текстовый паттерн в файл."""
        try:
            from tkinter import filedialog
            filename = filedialog.asksaveasfilename(
                defaultextension=".pattern.txt",
                filetypes=[
                    ("Pattern files", "*.pattern.txt"),
                    ("Text files", "*.txt"),
                    ("All files", "*.*"),
                ],
                title="Экспорт паттерна",
            )
            if not filename:
                return
            
            pattern_text = self.txt_pattern.get("1.0", tk.END)
            with open(filename, "w", encoding="utf-8") as f:
                f.write(pattern_text)
            
            self.log(f"Паттерн экспортирован в {filename}", "success")
            messagebox.showinfo("Успех", f"Паттерн успешно экспортирован в:\n{filename}")
        except Exception as e:
            self.log(f"Ошибка экспорта паттерна: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось экспортировать паттерн: {e}")

    def on_pattern_import(self):
        """Импортирует паттерн из текстового файла и загружает его в редактор."""
        try:
            from tkinter import filedialog
            filename = filedialog.askopenfilename(
                filetypes=[
                    ("Pattern files", "*.pattern.txt"),
                    ("Text files", "*.txt"),
                    ("All files", "*.*"),
                ],
                title="Загрузка паттерна",
            )
            if not filename:
                return
            
            with open(filename, "r", encoding="utf-8") as f:
                pattern_text = f.read()
            
            self.txt_pattern.delete("1.0", tk.END)
            self.txt_pattern.insert("1.0", pattern_text)
            self.on_pattern_text_change()
            
            self.log(f"Паттерн загружен из {filename}", "success")
            messagebox.showinfo("Успех", f"Паттерн успешно загружен из:\n{filename}")
        except Exception as e:
            self.log(f"Ошибка загрузки паттерна: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось загрузить паттерн: {e}")

    def on_pattern_text_change(self, event=None):
        """Обновляет визуализацию при изменении текста паттерна"""
        try:
            if hasattr(self, 'pattern_viz_canvas') and hasattr(self, 'hints_text'):
                self.update_pattern_visualization()
                self.update_hints()
        except (AttributeError, tk.TclError):
            # Виджеты еще не определены, пропускаем
            pass

    def parse_pattern_commands(self):
        """Парсит паттерн и возвращает список команд"""
        pattern_text = self.txt_pattern.get("1.0", tk.END)
        pattern_lines = [line.strip() for line in pattern_text.split('\n') if line.strip()]
        
        commands = []
        for i, line in enumerate(pattern_lines):
            if line.startswith('#'):
                commands.append({"type": "comment", "line": i+1, "text": line})
                continue
            
            parts = line.split()
            if not parts:
                continue
            
            cmd_type = parts[0].upper()
            if cmd_type == "WRITE":
                if len(parts) >= 3:
                    try:
                        reg = int(parts[1], 0)
                        value = int(parts[2], 0)
                        u_flag = "U" in parts
                        m_flag = "M" in parts
                        commands.append({
                            "type": "WRITE", "line": i+1, "reg": reg, 
                            "value": value, "u": u_flag, "m": m_flag
                        })
                    except:
                        commands.append({"type": "error", "line": i+1, "text": line})
            elif cmd_type == "READ":
                if len(parts) >= 2:
                    try:
                        reg = int(parts[1], 0)
                        commands.append({"type": "READ", "line": i+1, "reg": reg})
                    except:
                        commands.append({"type": "error", "line": i+1, "text": line})
            elif cmd_type == "CLEAR":
                commands.append({"type": "CLEAR", "line": i+1})
            elif cmd_type == "DELAY":
                if len(parts) >= 2:
                    try:
                        count = int(parts[1])
                        commands.append({"type": "DELAY", "line": i+1, "count": count})
                    except:
                        commands.append({"type": "error", "line": i+1, "text": line})
            else:
                commands.append({"type": "error", "line": i+1, "text": line})
        
        return commands

    def update_pattern_visualization(self):
        """Обновляет визуализацию паттерна"""
        self.pattern_viz_canvas.delete("all")
        
        commands = self.parse_pattern_commands()
        if not commands:
            self.pattern_viz_canvas.create_text(
                10, 10, anchor="nw", text="Введите паттерн для визуализации",
                font=('Arial', 10), fill='#999999'
            )
            return
        
        # Цвета для разных типов команд
        colors = {
            "WRITE": "#4CAF50",
            "READ": "#2196F3",
            "CLEAR": "#FF9800",
            "DELAY": "#9C27B0",
            "comment": "#999999",
            "error": "#F44336"
        }
        
        y = 20
        x_start = 10
        box_width = 320
        box_height = 40
        spacing = 5
        
        for cmd in commands:
            cmd_type = cmd.get("type", "unknown")
            color = colors.get(cmd_type, "#CCCCCC")
            
            # Рисуем прямоугольник для команды
            self.pattern_viz_canvas.create_rectangle(
                x_start, y, x_start + box_width, y + box_height,
                fill=color, outline='#333333', width=1
            )
            
            # Текст команды
            if cmd_type == "WRITE":
                text = f"WRITE reg {cmd['reg']} = 0x{cmd['value']:04X}"
                if cmd.get('u'):
                    text += " U"
                if cmd.get('m'):
                    text += " M"
            elif cmd_type == "READ":
                text = f"READ reg {cmd['reg']}"
            elif cmd_type == "CLEAR":
                text = "CLEAR"
            elif cmd_type == "DELAY":
                text = f"DELAY {cmd['count']} (READ 255 x{cmd['count']})"
            elif cmd_type == "comment":
                text = cmd.get('text', '')[:40]
            elif cmd_type == "error":
                text = f"ОШИБКА: {cmd.get('text', '')[:30]}"
            else:
                text = str(cmd)
            
            # Обрезаем текст если слишком длинный
            if len(text) > 35:
                text = text[:32] + "..."
            
            self.pattern_viz_canvas.create_text(
                x_start + 5, y + box_height // 2,
                anchor="w", text=text,
                font=('Consolas', 9), fill='#FFFFFF' if cmd_type != "comment" else '#666666'
            )
            
            # Номер строки
            self.pattern_viz_canvas.create_text(
                x_start + box_width - 5, y + 5,
                anchor="ne", text=f"#{cmd.get('line', '?')}",
                font=('Arial', 7), fill='#FFFFFF' if cmd_type != "comment" else '#999999'
            )
            
            y += box_height + spacing
        
        # Обновляем scroll region
        self.pattern_viz_canvas.configure(scrollregion=self.pattern_viz_canvas.bbox("all"))

    def update_hints(self):
        """Обновляет подсказки на основе текущего паттерна"""
        self.hints_text.configure(state="normal")
        self.hints_text.delete("1.0", tk.END)
        
        commands = self.parse_pattern_commands()
        
        if not commands:
            hints = """💡 Подсказки:

• WRITE reg value [U] [M] - запись в регистр
  U = обновить triggered registers
  M = очистить compliance monitor

• READ reg - чтение регистра

• CLEAR - команда CLEAR для инициализации ADC

• DELAY X - задержка (X раз READ 255)

• # комментарий - строка комментария

Начните вводить паттерн, и здесь появятся подсказки!"""
        else:
            hints = "💡 Анализ паттерна:\n\n"
            
            # Статистика
            write_count = sum(1 for c in commands if c.get("type") == "WRITE")
            read_count = sum(1 for c in commands if c.get("type") == "READ")
            clear_count = sum(1 for c in commands if c.get("type") == "CLEAR")
            delay_count = sum(1 for c in commands if c.get("type") == "DELAY")
            error_count = sum(1 for c in commands if c.get("type") == "error")
            comment_count = sum(1 for c in commands if c.get("type") == "comment")
            
            hints += f"📊 Статистика:\n"
            hints += f"  • Всего команд: {len(commands)}\n"
            if write_count > 0:
                hints += f"  • WRITE: {write_count}\n"
            if read_count > 0:
                hints += f"  • READ: {read_count}\n"
            if clear_count > 0:
                hints += f"  • CLEAR: {clear_count}\n"
            if delay_count > 0:
                hints += f"  • DELAY: {delay_count}\n"
            if comment_count > 0:
                hints += f"  • Комментарии: {comment_count}\n"
            
            if error_count > 0:
                hints += f"\n⚠️ Ошибок: {error_count}\n"
                hints += "Проверьте синтаксис команд!\n"
            
            # Анализ регистров
            write_regs = [c.get("reg") for c in commands if c.get("type") == "WRITE"]
            if write_regs:
                hints += f"\n📝 Записываемые регистры:\n"
                unique_regs = sorted(set(write_regs))
                for reg in unique_regs[:10]:  # Показываем первые 10
                    count = write_regs.count(reg)
                    reg_name = self.get_register_name(reg)
                    hints += f"  • Reg {reg} ({reg_name}): {count} раз\n"
                if len(unique_regs) > 10:
                    hints += f"  ... и еще {len(unique_regs) - 10} регистров\n"
            
            # Общие подсказки
            hints += "\n💡 Полезные регистры:\n"
            hints += "  • 42 - Stimulation Enable\n"
            hints += "  • 44 - Polarity\n"
            hints += "  • 64-79 - Negative Current (channels 0-15)\n"
            hints += "  • 96-111 - Positive Current (channels 0-15)\n"
            hints += "  • 255 - Chip ID (для проверки)\n"
        
        self.hints_text.insert("1.0", hints)
        self.hints_text.configure(state="disabled")

    def get_register_name(self, reg):
        """Возвращает название регистра для подсказок"""
        reg_info = self.get_register_info(reg)
        return reg_info.get("name", "Unknown")

    def init_register_list(self):
        """Инициализирует список регистров"""
        self.register_listbox.delete(0, tk.END)
        for reg_num in sorted(self.REGISTERS_DB.keys()):
            reg_info = self.REGISTERS_DB[reg_num]
            name = reg_info.get("name", "Unknown")
            self.register_listbox.insert(tk.END, f"Reg {reg_num:3d}: {name}")
        # Выбираем первый регистр
        if self.register_listbox.size() > 0:
            self.register_listbox.selection_set(0)
            self.on_register_select(None)

    def on_register_search_change(self, *args):
        """Обработчик изменения поискового запроса"""
        search_text = self.var_register_search.get().lower()
        self.register_listbox.delete(0, tk.END)
        
        for reg_num in sorted(self.REGISTERS_DB.keys()):
            reg_info = self.REGISTERS_DB[reg_num]
            name = reg_info.get("name", "Unknown").lower()
            description = reg_info.get("description", "").lower()
            
            if search_text == "" or search_text in name or search_text in description or search_text in str(reg_num):
                self.register_listbox.insert(tk.END, f"Reg {reg_num:3d}: {reg_info.get('name', 'Unknown')}")
        
        # Выбираем первый результат
        if self.register_listbox.size() > 0:
            self.register_listbox.selection_set(0)
            self.on_register_select(None)

    def on_register_select(self, event):
        """Обработчик выбора регистра из списка"""
        selection = self.register_listbox.curselection()
        if not selection:
            return
        
        index = selection[0]
        item_text = self.register_listbox.get(index)
        # Извлекаем номер регистра
        reg_num = int(item_text.split(':')[0].split()[1])
        
        self.show_register_info(reg_num)

    def show_register_info(self, reg_num):
        """Показывает детальную информацию о регистре"""
        reg_info = self.get_register_info(reg_num)
        
        self.register_info_text.configure(state="normal")
        self.register_info_text.delete("1.0", tk.END)
        
        info = f"═══════════════════════════════════════════════════════\n"
        info += f"РЕГИСТР {reg_num}\n"
        info += f"═══════════════════════════════════════════════════════\n\n"
        
        info += f"📌 Название: {reg_info.get('name', 'Неизвестно')}\n\n"
        
        if 'description' in reg_info:
            info += f"📝 Описание:\n{reg_info['description']}\n\n"
        
        if 'type' in reg_info:
            info += f"🔧 Тип: {reg_info['type']}\n"
            if reg_info['type'] == 'triggered':
                info += "   ⚠️ Это triggered регистр - изменения применяются только при U flag = 1\n"
            elif reg_info['type'] == 'read-only':
                info += "   📖 Только для чтения\n"
            elif reg_info['type'] == 'read-write':
                info += "   ✏️ Чтение и запись\n"
            info += "\n"
        
        if 'bits' in reg_info:
            info += f"🔢 Разрядность: {reg_info['bits']} бит\n\n"
        
        if 'range' in reg_info:
            info += f"📊 Диапазон значений:\n"
            info += f"   {reg_info['range']}\n\n"
        
        if 'format' in reg_info:
            info += f"📋 Формат:\n"
            info += f"   {reg_info['format']}\n\n"
        
        if 'usage' in reg_info:
            info += f"💡 Использование:\n"
            info += f"   {reg_info['usage']}\n\n"
        
        if 'example' in reg_info:
            info += f"📝 Примеры команд:\n"
            for ex in reg_info['example']:
                info += f"   {ex}\n"
            info += "\n"
        
        if 'notes' in reg_info:
            info += f"⚠️ Важные замечания:\n"
            info += f"   {reg_info['notes']}\n\n"
        
        info += f"═══════════════════════════════════════════════════════\n"
        info += f"💻 Команда для записи:\n"
        info += f"   WRITE {reg_num} <значение> [U] [M]\n\n"
        info += f"💻 Команда для чтения:\n"
        info += f"   READ {reg_num}\n\n"
        
        if 'related' in reg_info:
            info += f"🔗 Связанные регистры:\n"
            for related_reg in reg_info['related']:
                related_info = self.get_register_info(related_reg)
                info += f"   • Reg {related_reg}: {related_info.get('name', 'Unknown')}\n"
        
        self.register_info_text.insert("1.0", info)
        self.register_info_text.configure(state="disabled")

    def get_register_info(self, reg_num):
        """Возвращает информацию о регистре"""
        return self.REGISTERS_DB.get(reg_num, {
            "name": "Unknown",
            "description": "Информация о регистре отсутствует"
        })

    # База данных регистров Intan RHS2116
    REGISTERS_DB = {
        0: {
            "name": "ADC Configuration (ADC Buffer Bias & MUX Bias)",
            "type": "read-write",
            "bits": 8,
            "range": "0-255",
            "description": "Регистр конфигурации ADC: биты [7:3] = ADC buffer bias, биты [2:0] = MUX bias. Критически важен для качества оцифровки сигналов. Значения зависят от частоты дискретизации ADC (см. таблицу в даташите).",
            "usage": "Устанавливается при инициализации чипа. Рекомендуемые значения (ADC buffer bias, MUX bias): ≤120 kS/s→(32,40), 140→(16,40), 175→(8,40), 220→(8,32), 280→(8,26), 350→(4,18), 440→(3,16), ≥440→(3,5).",
            "example": [
                "WRITE 0 0x00C5 U  # Для 480 kS/s (пример)",
                "# Используйте GUI для автоматического выбора значений по частоте"
            ],
            "notes": "Критически важно для качества оцифровки сигналов. Неправильные значения могут привести к нелинейности или артефактам. Используйте таблицу из даташита для выбора оптимальных значений."
        },
        1: {
            "name": "ADC Reference Bias",
            "type": "read-write",
            "bits": 8,
            "range": "0-255",
            "description": "Настройка опорного смещения ADC.",
            "usage": "Обычно устанавливается при инициализации."
        },
        2: {
            "name": "MUX Load",
            "type": "read-write",
            "bits": 8,
            "range": "0-255",
            "description": "Настройка нагрузки мультиплексора."
        },
        3: {
            "name": "Temperature Sensor",
            "type": "read-only",
            "bits": 16,
            "description": "Температурный датчик. Только для чтения.",
            "usage": "READ 3  # Чтение температуры"
        },
        4: {
            "name": "ADC Auxiliary Input",
            "type": "read-only",
            "bits": 16,
            "description": "Вспомогательный вход ADC."
        },
        5: {
            "name": "Supply Voltage Sensor",
            "type": "read-only",
            "bits": 16,
            "description": "Датчик напряжения питания. Только для чтения.",
            "usage": "READ 5  # Чтение напряжения питания"
        },
        6: {
            "name": "Lower Cutoff Frequency",
            "type": "read-write",
            "bits": 16,
            "range": "0x0000-0xFFFF (0.1 Hz - 1 kHz)",
            "description": "Нижняя частота среза усилителей. Настраивается для всех каналов одновременно.",
            "usage": "Устанавливает нижнюю границу полосы пропускания усилителей.",
            "example": [
                "WRITE 6 0x0000 U  # Минимальная частота (0.1 Hz)",
                "WRITE 6 0xFFFF U  # Максимальная частота (1 kHz)"
            ]
        },
        7: {
            "name": "Upper Cutoff Frequency",
            "type": "read-write",
            "bits": 16,
            "range": "0x0000-0xFFFF (100 Hz - 20 kHz)",
            "description": "Верхняя частота среза усилителей. Настраивается для всех каналов одновременно.",
            "usage": "Устанавливает верхнюю границу полосы пропускания усилителей.",
            "example": [
                "WRITE 7 0x0000 U  # Минимальная частота (100 Hz)",
                "WRITE 7 0xFFFF U  # Максимальная частота (20 kHz)"
            ]
        },
        8: {
            "name": "High-Gain Amplifier Power",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Управление питанием высокоусиливающих усилителей для каждого канала.",
            "usage": "1 = включить, 0 = выключить. Экономит энергию при отключении неиспользуемых каналов.",
            "example": [
                "WRITE 8 0xFFFF U  # Включить все каналы",
                "WRITE 8 0x0001 U  # Включить только канал 0"
            ],
            "notes": "Triggered регистр - требует U flag = 1"
        },
        9: {
            "name": "Low-Gain Amplifier Power",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Управление питанием низкоусиливающих (DC-coupled) усилителей для каждого канала.",
            "usage": "1 = включить, 0 = выключить.",
            "example": [
                "WRITE 9 0xFFFF U  # Включить все каналы",
                "WRITE 9 0x0001 U  # Включить только канал 0"
            ],
            "notes": "Triggered регистр - требует U flag = 1"
        },
        10: {
            "name": "Fast Settle",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Быстрое восстановление усилителей после стимуляции. Включает режим быстрого восстановления для уменьшения артефактов стимуляции.",
            "usage": "1 = включить быстрое восстановление, 0 = выключить. Обычно включается во время и сразу после стимуляции.",
            "example": [
                "WRITE 10 0xFFFF U  # Включить для всех каналов",
                "WRITE 10 0x0000 U  # Выключить"
            ],
            "notes": "Triggered регистр - требует U flag = 1. Используется для быстрого восстановления после стимуляции."
        },
        11: {
            "name": "High-Pass Filter Disable",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска",
            "description": "Отключение высокочастотных фильтров на каналах.",
            "usage": "1 = отключить фильтр, 0 = включить."
        },
        12: {
            "name": "Lower Cutoff Frequency Override",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска",
            "description": "Переопределение нижней частоты среза для быстрого восстановления после стимуляции.",
            "usage": "Используется во время и сразу после стимуляции для быстрого восстановления.",
            "example": [
                "WRITE 12 0xFFFF U  # Установить минимальную частоту для всех каналов"
            ],
            "notes": "Triggered регистр - требует U flag = 1"
        },
        13: {
            "name": "Auxiliary Digital Output",
            "type": "read-write",
            "bits": 16,
            "description": "Вспомогательные цифровые выходы."
        },
        14: {
            "name": "Auxiliary Digital Input",
            "type": "read-only",
            "bits": 16,
            "description": "Вспомогательные цифровые входы. Только для чтения."
        },
        15: {
            "name": "Impedance Check DAC",
            "type": "read-write",
            "bits": 16,
            "description": "DAC для измерения импеданса электродов."
        },
        16: {
            "name": "Impedance Check Load",
            "type": "read-write",
            "bits": 16,
            "description": "Настройка нагрузки для измерения импеданса."
        },
        17: {
            "name": "Impedance Check Frequency",
            "type": "read-write",
            "bits": 16,
            "description": "Частота для измерения импеданса электродов."
        },
        18: {
            "name": "Impedance Check Amplifier",
            "type": "read-write",
            "bits": 16,
            "format": "Битовая маска",
            "description": "Выбор усилителя для измерения импеданса (high-gain или low-gain)."
        },
        19: {
            "name": "Impedance Check Stimulus",
            "type": "read-write",
            "bits": 16,
            "format": "Битовая маска",
            "description": "Включение стимуляции для измерения импеданса."
        },
        20: {
            "name": "Impedance Check Connect All",
            "type": "read-write",
            "bits": 16,
            "description": "Подключение всех каналов для измерения импеданса."
        },
        21: {
            "name": "Impedance Check Series Resistance",
            "type": "read-write",
            "bits": 16,
            "description": "Настройка последовательного сопротивления для измерения импеданса."
        },
        32: {
            "name": "Stimulation Enable (Negative)",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Включение стимуляторов с отрицательной полярностью для каждого канала.",
            "usage": "1 = включить стимулятор с отрицательной полярностью, 0 = выключить. Работает вместе с регистром 42.",
            "example": [
                "WRITE 32 0x0001 U  # Включить отрицательную стимуляцию на канале 0",
                "WRITE 32 0xFFFF U  # Включить на всех каналах"
            ],
            "notes": "Triggered регистр - требует U flag = 1. Работает совместно с регистрами 33, 42, 44.",
            "related": [33, 42, 44]
        },
        33: {
            "name": "Stimulation Enable (Positive)",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Включение стимуляторов с положительной полярностью для каждого канала.",
            "usage": "1 = включить стимулятор с положительной полярностью, 0 = выключить. Работает вместе с регистром 42.",
            "example": [
                "WRITE 33 0x0001 U  # Включить положительную стимуляцию на канале 0",
                "WRITE 33 0xFFFF U  # Включить на всех каналах"
            ],
            "notes": "Triggered регистр - требует U flag = 1. Работает совместно с регистрами 32, 42, 44.",
            "related": [32, 42, 44]
        },
        34: {
            "name": "Stimulation Step Size",
            "type": "read-write",
            "bits": 8,
            "range": "0x00-0xFF (определяет шаг стимуляции)",
            "description": "Размер шага стимуляции. Определяет минимальный шаг изменения тока стимуляции.",
            "usage": "Значение 0xE2 соответствует шагу 1 µA (диапазон ±255 µA). Влияет на точность установки тока.",
            "example": [
                "WRITE 34 0x00E2 U  # Установить шаг 1 µA (диапазон ±255 µA)"
            ],
            "notes": "Критически важно для правильной работы стимуляции. Обычно устанавливается в 0xE2 для шага 1 µA.",
            "related": [35, 64, 96]
        },
        35: {
            "name": "Stimulation Bias",
            "type": "read-write",
            "bits": 8,
            "range": "0x00-0xFF",
            "description": "Смещение для стимуляции. Настраивается в зависимости от шага стимуляции.",
            "usage": "Значение 0xAA рекомендуется для шага 1 µA (регистр 34 = 0xE2).",
            "example": [
                "WRITE 35 0x00AA U  # Для шага 1 µA"
            ],
            "notes": "Должно соответствовать значению в регистре 34.",
            "related": [34]
        },
        36: {
            "name": "Charge Recovery Target Voltage",
            "type": "read-write",
            "bits": 16,
            "range": "0x0000-0xFFFF",
            "description": "Целевое напряжение для восстановления заряда. Обычно устанавливается в 0 (земля).",
            "usage": "Устанавливает целевое напряжение для схемы восстановления заряда.",
            "example": [
                "WRITE 36 0x0080 U  # Установить целевое напряжение в 0"
            ],
            "related": [37, 46, 48]
        },
        37: {
            "name": "Charge Recovery Current Limit",
            "type": "read-write",
            "bits": 16,
            "range": "0x0000-0xFFFF",
            "description": "Ограничение тока для восстановления заряда.",
            "usage": "Устанавливает максимальный ток для схемы восстановления заряда.",
            "example": [
                "WRITE 37 0x4F00 U  # Установить лимит тока 1 nA"
            ],
            "related": [36, 46, 48]
        },
        38: {
            "name": "DC-Coupled Amplifier Power",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Управление питанием DC-coupled (низкоусиливающих) усилителей для каждого канала.",
            "usage": "1 = включить, 0 = выключить. DC-coupled усилители используются для мониторинга напряжения электродов во время стимуляции.",
            "example": [
                "WRITE 38 0xFFFF U  # Включить все DC-coupled усилители",
                "WRITE 38 0x0001 U  # Включить только канал 0"
            ],
            "notes": "Triggered регистр - требует U flag = 1. Важно для мониторинга во время стимуляции.",
            "related": [9]
        },
        40: {
            "name": "Compliance Monitor",
            "type": "read-only",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Монитор соответствия (compliance monitor). Показывает, какие каналы превысили лимиты напряжения стимуляции.",
            "usage": "Только для чтения. Чтение с M flag = 1 очищает регистр. Показывает каналы с проблемами стимуляции.",
            "example": [
                "READ 40  # Чтение монитора соответствия",
                "READ 255 M  # Чтение с M flag для очистки монитора"
            ],
            "notes": "Только для чтения. Чтение с M flag = 1 очищает регистр. Важно для диагностики проблем стимуляции."
        },
        42: {
            "name": "Stimulator On/Off",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Включение/выключение стимуляторов для каждого канала. Главный переключатель стимуляции.",
            "usage": "1 = включить стимулятор, 0 = выключить. Должен быть установлен вместе с регистрами 32/33 и 44 для работы стимуляции.",
            "example": [
                "WRITE 42 0x0001 U  # Включить стимулятор на канале 0",
                "WRITE 42 0x0000 U  # Выключить все стимуляторы",
                "WRITE 42 0xFFFF U  # Включить все стимуляторы"
            ],
            "notes": "Triggered регистр - требует U flag = 1. КРИТИЧЕСКИ ВАЖЕН для управления стимуляцией. Работает совместно с регистрами 32, 33, 44, 64-79, 96-111.",
            "related": [32, 33, 44, 64, 96]
        },
        44: {
            "name": "Stimulator Polarity",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0 (1=положительная, 0=отрицательная), ... бит 15 = канал 15",
            "description": "Полярность стимуляторов для каждого канала. Определяет направление тока стимуляции.",
            "usage": "1 = положительная полярность (источник тока), 0 = отрицательная полярность (сток тока).",
            "example": [
                "WRITE 44 0x0001 U  # Положительная полярность на канале 0",
                "WRITE 44 0x0000 U  # Отрицательная полярность на всех каналах",
                "WRITE 44 0xFFFF U  # Положительная полярность на всех каналах"
            ],
            "notes": "Triggered регистр - требует U flag = 1. Определяет направление тока стимуляции.",
            "related": [42, 64, 96]
        },
        46: {
            "name": "Charge Recovery Switch",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Переключатель восстановления заряда. Подключает электроды к общему выводу (stim_GND) для быстрого восстановления заряда.",
            "usage": "1 = подключить к stim_GND (обычно земля), 0 = отключить. Используется после стимуляции для восстановления заряда.",
            "example": [
                "WRITE 46 0xFFFF U  # Подключить все каналы к земле для восстановления",
                "WRITE 46 0x0000 U  # Отключить все каналы"
            ],
            "notes": "Triggered регистр - требует U flag = 1. Используется для быстрого восстановления заряда после стимуляции.",
            "related": [36, 37, 48]
        },
        48: {
            "name": "Current-Limited Charge Recovery",
            "type": "triggered",
            "bits": 16,
            "format": "Битовая маска: бит 0 = канал 0, бит 1 = канал 1, ... бит 15 = канал 15",
            "description": "Включение схемы восстановления заряда с ограничением тока для каждого канала.",
            "usage": "1 = включить, 0 = выключить. Используется для медленного восстановления заряда с контролируемым током.",
            "example": [
                "WRITE 48 0xFFFF U  # Включить на всех каналах",
                "WRITE 48 0x0000 U  # Выключить"
            ],
            "notes": "Triggered регистр - требует U flag = 1. Работает совместно с регистрами 36 и 37.",
            "related": [36, 37, 46]
        },
        64: {
            "name": "Negative Stimulation Current (Channel 0)",
            "type": "triggered",
            "bits": 16,
            "range": "0x8000-0x80FF (0-255 µA при шаге 1 µA)",
            "format": "Биты [15:8] = 0x80 (фиксированные), биты [7:0] = величина тока (0-255)",
            "description": "Величина отрицательного тока стимуляции для канала 0. Устанавливает ток стока (sink current). ВАЖНО: Значение - это МНОЖИТЕЛЬ шага (Register 34), а не прямой ток! При step size = 1 µA (Register 34 = 0x00E2), значение 1 = 1 µA, значение 10 = 10 µA.",
            "usage": "Значение 0x8000 = 0 µA, 0x80FF = 255 µA (при шаге 1 µA). Для других каналов используйте регистры 65-79. КРИТИЧНО: Register 34 должен быть установлен в 0x00E2 перед установкой токов!",
            "example": [
                "WRITE 64 0x8000 U  # 0 µA (выключено)",
                "WRITE 64 0x8014 U  # 20 µA",
                "WRITE 64 0x80FF U  # 255 µA (максимум)"
            ],
            "notes": "Triggered регистр - требует U flag = 1. Регистры 64-79 для каналов 0-15 соответственно.",
            "related": [65, 96, 42, 44]
        },
        65: {
            "name": "Negative Stimulation Current (Channel 1)",
            "type": "triggered",
            "bits": 16,
            "range": "0x8000-0x80FF (0-255 µA при шаге 1 µA)",
            "description": "Величина отрицательного тока стимуляции для канала 1.",
            "related": [64, 97]
        },
        66: {
            "name": "Negative Stimulation Current (Channel 2)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 2.",
            "related": [64, 98]
        },
        67: {
            "name": "Negative Stimulation Current (Channel 3)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 3.",
            "related": [64, 99]
        },
        68: {
            "name": "Negative Stimulation Current (Channel 4)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 4.",
            "related": [64, 100]
        },
        69: {
            "name": "Negative Stimulation Current (Channel 5)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 5.",
            "related": [64, 101]
        },
        70: {
            "name": "Negative Stimulation Current (Channel 6)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 6.",
            "related": [64, 102]
        },
        71: {
            "name": "Negative Stimulation Current (Channel 7)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 7.",
            "related": [64, 103]
        },
        72: {
            "name": "Negative Stimulation Current (Channel 8)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 8.",
            "related": [64, 104]
        },
        73: {
            "name": "Negative Stimulation Current (Channel 9)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 9.",
            "related": [64, 105]
        },
        74: {
            "name": "Negative Stimulation Current (Channel 10)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 10.",
            "related": [64, 106]
        },
        75: {
            "name": "Negative Stimulation Current (Channel 11)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 11.",
            "related": [64, 107]
        },
        76: {
            "name": "Negative Stimulation Current (Channel 12)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 12.",
            "related": [64, 108]
        },
        77: {
            "name": "Negative Stimulation Current (Channel 13)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 13.",
            "related": [64, 109]
        },
        78: {
            "name": "Negative Stimulation Current (Channel 14)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 14.",
            "related": [64, 110]
        },
        79: {
            "name": "Negative Stimulation Current (Channel 15)",
            "type": "triggered",
            "description": "Величина отрицательного тока стимуляции для канала 15.",
            "related": [64, 111]
        },
        96: {
            "name": "Positive Stimulation Current (Channel 0)",
            "type": "triggered",
            "bits": 16,
            "range": "0x8000-0x80FF (0-255 µA при шаге 1 µA)",
            "format": "Биты [15:8] = 0x80 (фиксированные), биты [7:0] = величина тока (0-255)",
            "description": "Величина положительного тока стимуляции для канала 0. Устанавливает ток источника (source current). ВАЖНО: Значение - это МНОЖИТЕЛЬ шага (Register 34), а не прямой ток! При step size = 1 µA (Register 34 = 0x00E2), значение 1 = 1 µA, значение 10 = 10 µA.",
            "usage": "Значение 0x8000 = 0 µA, 0x80FF = 255 µA (при шаге 1 µA). Для других каналов используйте регистры 97-111. КРИТИЧНО: Register 34 должен быть установлен в 0x00E2 перед установкой токов!",
            "example": [
                "WRITE 96 0x8000 U  # 0 µA (выключено)",
                "WRITE 96 0x8014 U  # 20 µA",
                "WRITE 96 0x80FF U  # 255 µA (максимум)"
            ],
            "notes": "Triggered регистр - требует U flag = 1. Регистры 96-111 для каналов 0-15 соответственно.",
            "related": [97, 64, 42, 44]
        },
        97: {
            "name": "Positive Stimulation Current (Channel 1)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 1.",
            "related": [96, 65]
        },
        98: {
            "name": "Positive Stimulation Current (Channel 2)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 2.",
            "related": [96, 66]
        },
        99: {
            "name": "Positive Stimulation Current (Channel 3)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 3.",
            "related": [96, 67]
        },
        100: {
            "name": "Positive Stimulation Current (Channel 4)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 4.",
            "related": [96, 68]
        },
        101: {
            "name": "Positive Stimulation Current (Channel 5)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 5.",
            "related": [96, 69]
        },
        102: {
            "name": "Positive Stimulation Current (Channel 6)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 6.",
            "related": [96, 70]
        },
        103: {
            "name": "Positive Stimulation Current (Channel 7)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 7.",
            "related": [96, 71]
        },
        104: {
            "name": "Positive Stimulation Current (Channel 8)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 8.",
            "related": [96, 72]
        },
        105: {
            "name": "Positive Stimulation Current (Channel 9)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 9.",
            "related": [96, 73]
        },
        106: {
            "name": "Positive Stimulation Current (Channel 10)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 10.",
            "related": [96, 74]
        },
        107: {
            "name": "Positive Stimulation Current (Channel 11)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 11.",
            "related": [96, 75]
        },
        108: {
            "name": "Positive Stimulation Current (Channel 12)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 12.",
            "related": [96, 76]
        },
        109: {
            "name": "Positive Stimulation Current (Channel 13)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 13.",
            "related": [96, 77]
        },
        110: {
            "name": "Positive Stimulation Current (Channel 14)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 14.",
            "related": [96, 78]
        },
        111: {
            "name": "Positive Stimulation Current (Channel 15)",
            "type": "triggered",
            "description": "Величина положительного тока стимуляции для канала 15.",
            "related": [96, 79]
        },
        255: {
            "name": "Chip ID",
            "type": "read-only",
            "bits": 16,
            "range": "0x0020 (32) для RHS2116",
            "description": "Идентификатор чипа. Только для чтения. Используется для проверки правильности подключения и работы SPI интерфейса.",
            "usage": "READ 255  # Должно вернуть 0x0020 (32) для RHS2116",
            "example": [
                "READ 255  # Проверка идентификатора чипа",
                "READ 255 M  # Чтение с M flag для очистки compliance monitor"
            ],
            "notes": "Только для чтения. Значение 32 (0x0020) подтверждает, что это RHS2116. Чтение с M flag = 1 также очищает compliance monitor (регистр 40)."
        }
    }

    # ----------------- UDP Регистрация данных -----------------

    def on_udp_register(self):
        """Регистрируется на UDP сервере для получения данных"""
        try:
            udp_host = self.var_udp_host.get().strip()
            udp_port = int(self.var_udp_port.get().strip())
            listen_port = int(self.var_listen_port.get().strip())
        except ValueError:
            messagebox.showerror("Ошибка", "Порты должны быть числами")
            return

        def worker():
            try:
                # Создаем UDP сокет для отправки команд
                self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.udp_sock.bind(('0.0.0.0', listen_port))
                self.udp_sock.settimeout(1.0)

                # Отправляем регистрацию
                server_addr = (udp_host, udp_port)
                self.udp_sock.sendto(b"REGISTER", server_addr)
                self.log("Отправлена регистрация на UDP сервер", "info")

                # Ждем подтверждение
                try:
                    data, addr = self.udp_sock.recvfrom(1024)
                    if data == b"REGISTERED":
                        self.udp_registered = True
                        self.udp_status_label.config(
                            text=f"● Зарегистрирован на {udp_host}:{udp_port}", 
                            style='Success.TLabel'
                        )
                        self.btn_udp_register.configure(state="disabled")
                        self.btn_udp_unregister.configure(state="normal")
                        self.btn_start_recording.configure(state="normal")
                        self.log(f"Успешно зарегистрирован на UDP сервере {addr}", "success")
                        
                        # Запускаем поток приема данных
                        self.start_udp_listening()
                    else:
                        raise Exception(f"Неожиданный ответ: {data}")
                except socket.timeout:
                    raise Exception("Таймаут ожидания подтверждения регистрации")
            except Exception as e:
                self.log(f"Ошибка регистрации на UDP сервере: {e}", "error")
                messagebox.showerror("Ошибка", f"Не удалось зарегистрироваться: {e}")
                if self.udp_sock:
                    self.udp_sock.close()
                    self.udp_sock = None

        threading.Thread(target=worker, daemon=True).start()

    def on_udp_unregister(self):
        """Отменяет регистрацию на UDP сервере"""
        if not self.udp_sock or not self.udp_registered:
            return

        try:
            udp_host = self.var_udp_host.get().strip()
            udp_port = int(self.var_udp_port.get().strip())
            server_addr = (udp_host, udp_port)
            
            self.udp_sock.sendto(b"UNREGISTER", server_addr)
            self.udp_registered = False
            self.udp_listening = False
            
            self.udp_status_label.config(
                text="● Не зарегистрирован", 
                style='Error.TLabel'
            )
            self.btn_udp_register.configure(state="normal")
            self.btn_udp_unregister.configure(state="disabled")
            self.btn_start_recording.configure(state="disabled")
            self.btn_stop_recording.configure(state="disabled")
            
            if self.udp_sock:
                self.udp_sock.close()
                self.udp_sock = None
            
            self.log("Регистрация на UDP сервере отменена", "info")
        except Exception as e:
            self.log(f"Ошибка отмены регистрации: {e}", "error")

    def start_udp_listening(self):
        """Запускает поток для приема данных от UDP сервера"""
        if self.udp_listening:
            return
        
        self.udp_listening = True
        self.udp_listen_thread = threading.Thread(target=self._udp_listen_loop, daemon=True)
        self.udp_listen_thread.start()

    def _udp_listen_loop(self):
        """Цикл приема данных от UDP сервера"""
        while self.udp_listening and self.udp_sock:
            try:
                data, addr = self.udp_sock.recvfrom(4096)
                
                # Проверяем, является ли это текстовым ответом от сервера
                try:
                    text_response = data.decode('utf-8').strip()
                    if text_response in ["REGISTERED", "UNREGISTERED", "RECORDING_STARTED", "RECORDING_STOPPED"]:
                        # Это текстовый ответ, не бинарные данные
                        if text_response == "RECORDING_STARTED":
                            self.after(0, lambda: self.log("✓ Сервер подтвердил начало регистрации", "success"))
                            # Убеждаемся, что флаг recording_active установлен
                            if not self.recording_active:
                                self.after(0, lambda: self.log("⚠ ВНИМАНИЕ: recording_active=False, но сервер начал регистрацию. Устанавливаем флаг.", "warning"))
                                self.recording_active = True
                        elif text_response == "RECORDING_STOPPED":
                            self.after(0, lambda: self.log("✓ Сервер подтвердил остановку регистрации", "info"))
                            self.recording_active = False
                        continue  # Пропускаем текстовые ответы
                except (UnicodeDecodeError, AttributeError):
                    # Это бинарные данные, продолжаем обработку
                    pass
                
                # Сохраняем сырые бинарные данные без парсинга (оптимизировано для максимальной скорости)
                if self.recording_active:
                    # Данные приходят в бинарном формате напрямую (без hex конвертации)
                    # Новый формат: sample_count (4) + samples, минимум 4 байта для sample_count
                    # Старый формат: timestamp (8) + channel_count (4) = 12 байт минимум
                    if len(data) >= 4:  # Минимум: sample_count (4) для нового формата
                        # Диагностика: логируем первые несколько пакетов
                        if self.recording_packet_count < 3:
                            self.after(0, lambda: self.log(f"📦 Пакет #{self.recording_packet_count + 1}: размер={len(data)} байт, recording_active={self.recording_active}", "info"))
                        self.recording_hex_data.append(data)
                        self.recording_packet_count += 1
                        
                        # Диагностика: логируем первые несколько пакетов
                        if self.recording_packet_count <= 5:
                            self.after(0, lambda p=self.recording_packet_count, s=len(data): 
                                self.log(f"📦 Получен пакет #{p}, размер: {s} байт", "info"))
                        
                        # Обновляем только статистику (без парсинга, минимальные накладные расходы)
                        if self.recording_packet_count % 100 == 0:
                            self.after(0, lambda: self._update_recording_stats_only())
                    else:
                        # Диагностика: слишком маленький пакет
                        if self.recording_packet_count <= 5:
                            self.after(0, lambda s=len(data): 
                                self.log(f"⚠ Пропущен слишком маленький пакет: {s} байт (минимум 4)", "warning"))
                
            except socket.timeout:
                continue
            except Exception as e:
                if self.udp_listening:
                    self.log(f"Ошибка приема UDP данных: {e}", "error")

    def _update_recording_stats_only(self):
        """Обновляет только статистику (быстрее)"""
        try:
            # Показываем количество сохраненных пакетов
            saved_count = len(self.recording_hex_data)
            self.recording_stats_label.config(
                text=f"Получено пакетов: {self.recording_packet_count} | Сохранено: {saved_count} | Данные сохраняются..."
            )
        except Exception:
            pass  # Игнорируем ошибки при обновлении GUI

    def _parse_hex_data(self):
        """Парсит сохраненные hex данные и строит график"""
        try:
            self.recording_graph_data = {}
            
            if not self.recording_hex_data:
                self.log("Нет данных для парсинга", "warning")
                return
            
            total_packets = len(self.recording_hex_data)
            self.log(f"Начинаем парсинг {total_packets} пакетов...", "info")
            
            if total_packets == 0:
                self.log("Нет данных для парсинга", "warning")
                return
            
            # Сначала парсим все данные в структурированный формат
            parsed_count = 0
            skipped_count = 0
            total_samples = 0
            invalid_channels_logged = set()  # Для логирования невалидных каналов
            
            first_values_logged = False
            first_adc_values = []
            first_uv_values = []

            for idx, binary_data in enumerate(self.recording_hex_data):
                if len(binary_data) < 5:
                    skipped_count += 1
                    continue
                
                # Формат v2 (pipeline): ver=2, смещение обрабатывается в GUI
                # [ver=1byte][sample_count=4] per sample: timestamp(8) + pipeline_skip(2) + ch_count(2) + raw_count(2) + raw_count×uint16
                if binary_data[0] == 2:
                    try:
                        sample_count = struct.unpack('I', binary_data[1:5])[0]
                    except struct.error:
                        skipped_count += 1
                        continue
                    if sample_count == 0 or sample_count > 100:
                        skipped_count += 1
                        continue
                    offset = 5
                    for _ in range(sample_count):
                        if offset + 14 > len(binary_data):
                            break
                        timestamp, pipeline_skip, ch_count = struct.unpack('dHH', binary_data[offset:offset+12])
                        offset += 12
                        if offset + ch_count + 2 > len(binary_data):
                            break
                        ch_list = list(binary_data[offset:offset + ch_count])
                        offset += ch_count
                        raw_count = struct.unpack('H', binary_data[offset:offset+2])[0]
                        offset += 2
                        if offset + raw_count * 2 > len(binary_data):
                            break
                        raw_values = struct.unpack(f'<{raw_count}H', binary_data[offset:offset + raw_count * 2])
                        offset += raw_count * 2
                        if not (0 <= timestamp <= 4102444800) or pipeline_skip >= raw_count or pipeline_skip + ch_count > raw_count:
                            continue
                        if not hasattr(self, 'recording_start_time'):
                            self.recording_start_time = timestamp
                        relative_time = timestamp - self.recording_start_time
                        channel_data = {}
                        for i in range(ch_count):
                            ch_num = ch_list[i] if i < len(ch_list) else i
                            adc_val = raw_values[pipeline_skip + i]
                            if not first_values_logged and len(first_adc_values) < 10:
                                first_adc_values.append((ch_num, adc_val))
                            value_uv = rhs2116_ac_uV(adc_val)
                            channel_data[ch_num] = value_uv
                            if not first_values_logged and len(first_uv_values) < 10:
                                first_uv_values.append((ch_num, value_uv))
                        for channel, value_uv in channel_data.items():
                            if channel not in self.recording_graph_data:
                                self.recording_graph_data[channel] = {'time': [], 'values_uv': []}
                            self.recording_graph_data[channel]['time'].append(relative_time)
                            self.recording_graph_data[channel]['values_uv'].append(value_uv)
                        if EMG_USE_DIFFERENTIAL and EMG_CH_A in channel_data and EMG_CH_B in channel_data:
                            emg_uv = channel_data[EMG_CH_A] - channel_data[EMG_CH_B]
                            if 'EMG' not in self.recording_graph_data:
                                self.recording_graph_data['EMG'] = {'time': [], 'values_uv': []}
                            self.recording_graph_data['EMG']['time'].append(relative_time)
                            self.recording_graph_data['EMG']['values_uv'].append(emg_uv)
                        parsed_count += 1
                        total_samples += 1
                    if total_samples > 0:
                        first_values_logged = True
                    continue
                
                # Формат v1: sample_count (4), затем для каждого sample:
                #   timestamp (8), channel_count (4), channel_num (4) + ac_value (2) для каждого канала
                try:
                    sample_count = struct.unpack('I', binary_data[0:4])[0]
                except struct.error:
                    skipped_count += 1
                    continue
                
                # Проверяем разумность sample_count (защита от поврежденных данных)
                if sample_count == 0 or sample_count > 100:
                    # Пробуем парсить как старый формат (один sample на пакет)
                    if len(binary_data) >= 12:  # Минимум: timestamp (8) + channel_count (4)
                        try:
                            # Парсим как один sample (старый формат)
                            timestamp = struct.unpack('d', binary_data[0:8])[0]
                            if 0 <= timestamp <= 4102444800:  # Валидный timestamp
                                channel_count = struct.unpack('I', binary_data[8:12])[0]
                                if 0 < channel_count <= 16:  # Разумное количество каналов
                                    # Парсим как старый формат
                                    offset = 12
                                    if not hasattr(self, 'recording_start_time'):
                                        self.recording_start_time = timestamp
                                    relative_time = timestamp - self.recording_start_time
                                    
                                    channel_data = {}
                                    for i in range(channel_count):
                                        if offset + 6 > len(binary_data):
                                            break
                                        try:
                                            channel_num = struct.unpack('I', binary_data[offset:offset+4])[0]
                                            # Читаем как unsigned 16-bit (0..65535) - это правильный формат для RHS2116
                                            adc_value_unsigned = struct.unpack('H', binary_data[offset+4:offset+6])[0]
                                            
                                            if not first_values_logged and len(first_adc_values) < 10:
                                                first_adc_values.append((channel_num, adc_value_unsigned))
                                            
                                            if 0 <= channel_num <= 15:
                                                # Конвертируем ADC в микровольты используя правильную формулу
                                                value_uv = rhs2116_ac_uV(adc_value_unsigned)
                                                channel_data[channel_num] = value_uv
                                                
                                                if not first_values_logged and len(first_uv_values) < 10:
                                                    first_uv_values.append((channel_num, value_uv))
                                            offset += 6
                                        except struct.error:
                                            break
                                    
                                    # ВСЕГДА сохраняем отдельные каналы
                                    for channel, value_uv in channel_data.items():
                                        if channel not in self.recording_graph_data:
                                            self.recording_graph_data[channel] = {'time': [], 'values_uv': []}
                                        self.recording_graph_data[channel]['time'].append(relative_time)
                                        # Значение уже в микровольтах после rhs2116_ac_uV
                                        self.recording_graph_data[channel]['values_uv'].append(value_uv)
                                    
                                    # Дополнительно: если EMG режим включен и оба канала есть - вычисляем и сохраняем EMG
                                    if EMG_USE_DIFFERENTIAL:
                                        if EMG_CH_A in channel_data and EMG_CH_B in channel_data:
                                            # Вычисляем псевдодифференциальный EMG
                                            chA_uv = channel_data[EMG_CH_A]
                                            chB_uv = channel_data[EMG_CH_B]
                                            emg_uv = chA_uv - chB_uv
                                            # Сохраняем как виртуальный канал "EMG"
                                            if 'EMG' not in self.recording_graph_data:
                                                self.recording_graph_data['EMG'] = {'time': [], 'values_uv': []}
                                            self.recording_graph_data['EMG']['time'].append(relative_time)
                                            self.recording_graph_data['EMG']['values_uv'].append(emg_uv)
                                            
                                            # Диагностика: логируем первые несколько вычислений дифференциала
                                            if parsed_count == 0 and len(self.recording_graph_data['EMG']['values_uv']) == 1:
                                                self.log(f"✓ Первый дифференциальный EMG: Ch{EMG_CH_A}({chA_uv:.2f} µV) - Ch{EMG_CH_B}({chB_uv:.2f} µV) = {emg_uv:.2f} µV", "info")
                                        elif parsed_count == 0:
                                            # Логируем только при первом sample, если каналы отсутствуют
                                            missing = []
                                            if EMG_CH_A not in channel_data:
                                                missing.append(f"Ch{EMG_CH_A}")
                                            if EMG_CH_B not in channel_data:
                                                missing.append(f"Ch{EMG_CH_B}")
                                            self.log(f"⚠ Дифференциальный EMG не может быть вычислен: отсутствуют каналы {', '.join(missing)}. Доступные каналы: {list(channel_data.keys())}", "warning")
                                    
                                    # Диагностика: логируем первые несколько сохранений
                                    if parsed_count == 0 and len(channel_data) > 0:
                                        self.log(f"✓ Первый sample: сохранено {len(channel_data)} каналов: {list(channel_data.keys())}", "info")
                                    
                                    parsed_count += 1
                                    total_samples += 1
                                    continue
                        except (struct.error, ValueError):
                            pass
                    
                    skipped_count += 1
                    continue
                
                # Парсим каждый sample в пакете
                offset = 4
                packet_samples_parsed = 0
                
                for sample_idx in range(sample_count):
                    # Проверяем, что осталось достаточно данных
                    if offset + 12 > len(binary_data):  # Минимум: timestamp (8) + channel_count (4)
                        break
                    
                    try:
                        timestamp = struct.unpack('d', binary_data[offset:offset+8])[0]
                        offset += 8
                        channel_count = struct.unpack('I', binary_data[offset:offset+4])[0]
                        offset += 4
                    except struct.error:
                        break
                    
                    # Проверяем валидность timestamp (быстрая проверка)
                    if not (0 <= timestamp <= 4102444800):  # 1970-2100 год
                        # Пропускаем этот sample, но продолжаем парсить пакет
                        # Нужно пропустить данные каналов этого sample
                        if offset + channel_count * 6 <= len(binary_data):
                            offset += channel_count * 6
                        continue
                    
                    # Вычисляем относительное время (от начала регистрации)
                    if not hasattr(self, 'recording_start_time'):
                        self.recording_start_time = timestamp
                    
                    relative_time = timestamp - self.recording_start_time
                    
                    # Парсим данные каналов
                    channel_data = {}
                    for i in range(channel_count):
                        if offset + 6 > len(binary_data):
                            break
                        try:
                            channel_num = struct.unpack('I', binary_data[offset:offset+4])[0]
                            # Читаем как unsigned 16-bit (0..65535) - это правильный формат для RHS2116
                            adc_value_unsigned = struct.unpack('H', binary_data[offset+4:offset+6])[0]
                            
                            if not first_values_logged and len(first_adc_values) < 10:
                                first_adc_values.append((channel_num, adc_value_unsigned))
                            
                            if 0 <= channel_num <= 15:
                                # Конвертируем ADC в микровольты используя правильную формулу
                                value_uv = rhs2116_ac_uV(adc_value_unsigned)
                                channel_data[channel_num] = value_uv
                                
                                if not first_values_logged and len(first_uv_values) < 10:
                                    first_uv_values.append((channel_num, value_uv))
                            # Игнорируем невалидные номера каналов (логируем только первые несколько)
                            elif channel_num not in invalid_channels_logged and len(invalid_channels_logged) < 5:
                                self.log(f"⚠ Пропущен невалидный номер канала: {channel_num} (ожидается 0-15)", "warning")
                                invalid_channels_logged.add(channel_num)
                            
                            offset += 6
                        except struct.error:
                            break
                    
                    # ВСЕГДА сохраняем отдельные каналы
                    for channel, value_uv in channel_data.items():
                        if channel not in self.recording_graph_data:
                            self.recording_graph_data[channel] = {'time': [], 'values_uv': []}
                        self.recording_graph_data[channel]['time'].append(relative_time)
                        # Значение уже в микровольтах после rhs2116_ac_uV
                        self.recording_graph_data[channel]['values_uv'].append(value_uv)
                    
                    # Дополнительно: если EMG режим включен и оба канала есть - вычисляем и сохраняем EMG
                    if EMG_USE_DIFFERENTIAL:
                        if EMG_CH_A in channel_data and EMG_CH_B in channel_data:
                            # Вычисляем псевдодифференциальный EMG
                            chA_uv = channel_data[EMG_CH_A]
                            chB_uv = channel_data[EMG_CH_B]
                            emg_uv = chA_uv - chB_uv
                            # Сохраняем как виртуальный канал "EMG"
                            if 'EMG' not in self.recording_graph_data:
                                self.recording_graph_data['EMG'] = {'time': [], 'values_uv': []}
                            self.recording_graph_data['EMG']['time'].append(relative_time)
                            self.recording_graph_data['EMG']['values_uv'].append(emg_uv)
                    
                    packet_samples_parsed += 1
                    total_samples += 1
                
                if packet_samples_parsed > 0:
                    parsed_count += 1
                
                # Логируем прогресс только периодически (минимум накладных расходов)
                if total_samples % 1000 == 0:
                    total_points_now = sum(len(data['time']) for data in self.recording_graph_data.values())
                    self.log(f"Обработано {total_samples} сэмплов из {parsed_count} пакетов, точек: {total_points_now}", "info")
            
            # Подсчитываем общее количество точек по всем каналам
            total_graph_points = sum(len(data['time']) for data in self.recording_graph_data.values())
            
            if first_adc_values:
                self.log("=== Sanity-check: Первые 10 raw ADC значений ===", "info")
                for ch, adc_val in first_adc_values[:10]:
                    self.log(f"  Ch{ch}: ADC={adc_val} (0x{adc_val:04X})", "info")
                    # Предупреждение о возможном клиппинге
                    if adc_val == 0 or adc_val == 65535:
                        self.log(f"  ⚠ ВНИМАНИЕ: Ch{ch} показывает клиппинг (0 или 65535)!", "warning")
            
            if first_uv_values:
                self.log("=== Sanity-check: Первые 10 значений после конвертации (µV) ===", "info")
                for ch, uv_val in first_uv_values[:10]:
                    self.log(f"  Ch{ch}: {uv_val:8.2f} µV", "info")
            
            # Диагностика: проверяем статистику по каналам для выявления проблем с DC offset
            if self.recording_graph_data:
                self.log("=== Статистика по каналам (для диагностики DC offset) ===", "info")
                for channel in sorted(self.recording_graph_data.keys(), key=lambda x: (isinstance(x, str), x)):
                    data = self.recording_graph_data[channel]
                    if len(data['values_uv']) > 0:
                        values = np.array(data['values_uv'])
                        mean_val = np.mean(values)
                        std_val = np.std(values)
                        min_val = np.min(values)
                        max_val = np.max(values)
                        channel_name = f"Ch{channel}" if isinstance(channel, int) else str(channel)
                        self.log(f"  {channel_name}: mean={mean_val:8.2f} µV, std={std_val:8.2f} µV, range=[{min_val:8.2f}, {max_val:8.2f}] µV ({len(values)} точек)", "info")
                        # Предупреждение о большом DC offset
                        if abs(mean_val) > 1000:  # Если среднее больше 1 мВ
                            self.log(f"  ⚠ ВНИМАНИЕ: {channel_name} имеет большой DC offset ({mean_val:.2f} µV)! Возможна проблема с конвертацией или аппаратными настройками.", "warning")
            
            self.log(f"Парсинг завершен. Обработано {parsed_count} из {total_packets} пакетов ({total_samples} samples, пропущено: {skipped_count})", "success")
            self.log(f"Всего точек в графике: {total_graph_points} (каналов: {len(self.recording_graph_data)})", "info")
            
            # Информация о режиме EMG
            if EMG_USE_DIFFERENTIAL:
                self.log(f"Режим: Псевдодифференциальный EMG (Ch{EMG_CH_A} - Ch{EMG_CH_B})", "info")
            else:
                self.log("Режим: Отдельные каналы", "info")
            
            # Режим calibration/rest check (опционально, можно включить через переменную окружения)
            if os.environ.get('EMG_CALIBRATION_CHECK', 'false').lower() == 'true':
                self._perform_calibration_check()
            
            # Проверяем, сколько уникальных временных точек
            all_times = set()
            for channel_data in self.recording_graph_data.values():
                all_times.update(channel_data['time'])
            unique_times = len(all_times)
            self.log(f"Уникальных временных точек: {unique_times}", "info")
            
            if parsed_count < total_packets - skipped_count:
                self.log(f"⚠ Предупреждение: не все пакеты были обработаны ({parsed_count}/{total_packets}, пропущено: {skipped_count})", "warning")
            
            if unique_times == 1 and total_graph_points > 1:
                self.log("⚠ ВНИМАНИЕ: все точки имеют одинаковое время! Возможно, проблема с временными метками.", "warning")
            
            self.log(f"Вызов _update_recording_text_display из _parse_hex_data: {len(self.recording_graph_data)} каналов, {total_graph_points} точек", "info")
            self._update_recording_text_display()
            
            if total_graph_points == 0:
                self.log("⚠ ОШИБКА: после парсинга нет точек для графика!", "error")
                return
            
            # Только после успешного парсинга текста строим график
            if MATPLOTLIB_AVAILABLE and self.recording_graph_data:
                self._redraw_recording_graph()
                self.log("График построен", "success")
            elif not MATPLOTLIB_AVAILABLE:
                self.log("Matplotlib недоступен, график не построен", "warning")
            elif not self.recording_graph_data:
                self.log("Нет данных для построения графика", "warning")
            
        except Exception as e:
            self.log(f"Ошибка парсинга данных: {e}", "error")
            import traceback
            self.log(f"Детали ошибки: {traceback.format_exc()}", "error")
            messagebox.showerror("Ошибка", f"Не удалось обработать данные: {e}")
    
    def _perform_calibration_check(self):
        """
        Выполняет проверку калибровки/покоя на первых 2 секундах данных.
        Ожидание: mean ~ 0 µV, RMS в покое порядка десятков µV.
        """
        try:
            self.log("=== Calibration/Rest Check ===", "info")
            
            # Выбираем канал для проверки (EMG если есть, иначе первый доступный)
            check_channel = None
            if 'EMG' in self.recording_graph_data:
                check_channel = 'EMG'
                self.log("Проверка на канале: EMG (дифференциал)", "info")
            elif self.recording_graph_data:
                check_channel = list(self.recording_graph_data.keys())[0]
                self.log(f"Проверка на канале: {check_channel}", "info")
            else:
                self.log("⚠ Нет данных для проверки калибровки", "warning")
                return
            
            data = self.recording_graph_data[check_channel]
            times = data['time']
            values_uv = data['values_uv']
            
            if not times or not values_uv:
                self.log("⚠ Нет данных для проверки калибровки", "warning")
                return
            
            # Берем первые 2 секунды данных
            start_time = times[0]
            cutoff_time = start_time + 2.0  # 2 секунды
            
            check_values = []
            check_times = []
            for i, t in enumerate(times):
                if t <= cutoff_time:
                    check_values.append(values_uv[i])
                    check_times.append(t)
                else:
                    break
            
            if len(check_values) < 10:
                self.log(f"⚠ Недостаточно данных для проверки (только {len(check_values)} точек)", "warning")
                return
            
            # Вычисляем статистику
            mean_uv = sum(check_values) / len(check_values)
            
            # RMS
            squared_values = [v * v for v in check_values]
            mean_squared = sum(squared_values) / len(squared_values)
            rms_uv = math.sqrt(mean_squared)
            
            # Стандартное отклонение
            variance = sum((v - mean_uv) ** 2 for v in check_values) / len(check_values)
            std_uv = math.sqrt(variance)
            
            self.log(f"  Период проверки: {check_times[0]:.3f} - {check_times[-1]:.3f} с ({len(check_values)} точек)", "info")
            self.log(f"  Mean: {mean_uv:8.2f} µV (ожидается ~0 µV)", "info")
            self.log(f"  RMS:  {rms_uv:8.2f} µV (ожидается десятки µV в покое)", "info")
            self.log(f"  Std:  {std_uv:8.2f} µV", "info")
            
            # Проверки
            if abs(mean_uv) > 100:
                self.log(f"  ⚠ ВНИМАНИЕ: Mean слишком далеко от нуля ({mean_uv:.2f} µV)!", "warning")
            else:
                self.log(f"  ✓ Mean в норме (близко к нулю)", "success")
            
            if rms_uv < 1:
                self.log(f"  ⚠ ВНИМАНИЕ: RMS слишком мал ({rms_uv:.2f} µV) - возможно проблема с сигналом!", "warning")
            elif rms_uv > 1000:
                self.log(f"  ⚠ ВНИМАНИЕ: RMS слишком велик ({rms_uv:.2f} µV) - возможно артефакты!", "warning")
            else:
                self.log(f"  ✓ RMS в разумном диапазоне", "success")
            
        except Exception as e:
            self.log(f"Ошибка при проверке калибровки: {e}", "error")
            import traceback
            self.log(f"Детали: {traceback.format_exc()}", "error")

    def _update_recording_text_display(self):
        """Обновляет текстовое поле после парсинга данных"""
        try:
            self.log(f"Вызов _update_recording_text_display: recording_graph_data = {len(self.recording_graph_data) if self.recording_graph_data else 0} каналов", "info")
            self.recording_data_text.configure(state="normal")
            self.recording_data_text.delete("1.0", tk.END)
            
            if not self.recording_graph_data:
                self.recording_data_text.insert("1.0", "Нет данных для отображения\nПопробуйте нажать кнопку '🔍 Построить график' после остановки регистрации.")
                self.recording_data_text.configure(state="disabled")
                self.log("⚠ recording_graph_data пуст в _update_recording_text_display", "warning")
                return
            
            # Используем уже распарсенные данные из recording_graph_data
            # Собираем все временные метки и значения для отображения
            all_timestamps = set()
            for channel_data in self.recording_graph_data.values():
                if 'time' in channel_data and channel_data['time']:
                    all_timestamps.update(channel_data['time'])
            
            if not all_timestamps:
                self.recording_data_text.insert("1.0", 
                    "Данные распарсены, но временные метки отсутствуют.\n"
                    f"Каналов в recording_graph_data: {len(self.recording_graph_data)}\n"
                    "Проверьте логи на наличие ошибок парсинга.\n"
                )
                self.recording_data_text.configure(state="disabled")
                self.log("⚠ Нет временных меток для отображения", "warning")
                return
            
            # Сортируем временные метки - показываем ВСЕ точки
            sorted_timestamps = sorted(all_timestamps)
            timestamps_to_show = sorted_timestamps  # Показываем все точки
            
            # Восстанавливаем абсолютное время из относительного
            if hasattr(self, 'recording_start_time'):
                start_time = self.recording_start_time
            else:
                start_time = 0
            
            point_count = 0
            for rel_time in timestamps_to_show:
                abs_time = start_time + rel_time
                time_str = datetime.fromtimestamp(abs_time).strftime("%H:%M:%S.%f")[:-3]
                
                data_str = f"[{time_str}] "
                # Собираем значения всех валидных каналов (0-15) и виртуального канала "EMG" для этого момента времени
                # ВАЖНО: используем key для сортировки смешанных типов (int и str)
                valid_channels = [ch for ch in sorted(self.recording_graph_data.keys(), key=lambda x: (isinstance(x, str), x)) 
                                 if (isinstance(ch, int) and 0 <= ch <= 15) or ch == 'EMG']
                for channel in valid_channels:
                    channel_data = self.recording_graph_data[channel]
                    # Находим индекс этого времени в массиве времени канала
                    try:
                        time_idx = channel_data['time'].index(rel_time)
                        value_uv = channel_data['values_uv'][time_idx]
                        # Для виртуального канала "EMG" используем другое форматирование
                        if channel == 'EMG':
                            data_str += f"EMG:{value_uv:8.2f} µV "
                        else:
                            data_str += f"Ch{channel}:{value_uv:8.2f} µV "
                    except (ValueError, IndexError):
                        # Если для этого канала нет данных в этот момент времени
                        if channel == 'EMG':
                            data_str += f"EMG:        - "
                        else:
                            data_str += f"Ch{channel}:        - "
                
                data_str += "\n"
                self.recording_data_text.insert("end", data_str)
                point_count += 1
            
            # Все точки уже показаны, сообщение не нужно
            
            # Добавляем статистику (валидные каналы 0-15 и виртуальный канал "EMG")
            valid_channels_count = len([ch for ch in self.recording_graph_data.keys() 
                                       if isinstance(ch, int) and 0 <= ch <= 15])
            emg_channel_present = 'EMG' in self.recording_graph_data
            total_channels = len(self.recording_graph_data)
            total_points = len(sorted_timestamps)
            self.recording_data_text.insert("end", f"\n=== Статистика ===\n")
            self.recording_data_text.insert("end", f"Валидных каналов (0-15): {valid_channels_count}\n")
            if emg_channel_present:
                self.recording_data_text.insert("end", f"Виртуальный канал EMG: присутствует\n")
            if total_channels != valid_channels_count + (1 if emg_channel_present else 0):
                self.recording_data_text.insert("end", f"Всего каналов (включая невалидные): {total_channels}\n")
            self.recording_data_text.insert("end", f"Всего точек: {total_points}\n")
            self.recording_data_text.insert("end", f"Пакетов обработано: {len(self.recording_hex_data)}\n")
            
            # Добавляем статистику по каналам (среднее, RMS)
            self.recording_data_text.insert("end", f"\n=== Статистика по каналам ===\n")
            for channel in sorted(self.recording_graph_data.keys(), key=lambda x: (isinstance(x, str), x)):
                if (isinstance(channel, int) and 0 <= channel <= 15) or channel == 'EMG':
                    channel_data = self.recording_graph_data[channel]
                    if len(channel_data['values_uv']) > 0:
                        values = np.array(channel_data['values_uv'])
                        mean_val = np.mean(values)
                        std_val = np.std(values)
                        min_val = np.min(values)
                        max_val = np.max(values)
                        channel_name = f"Ch{channel}" if isinstance(channel, int) else "EMG"
                        self.recording_data_text.insert("end", 
                            f"{channel_name}: mean={mean_val:8.2f} µV, std={std_val:8.2f} µV, "
                            f"range=[{min_val:8.2f}, {max_val:8.2f}] µV\n")
            
            self.recording_data_text.see("1.0")
            self.recording_data_text.configure(state="disabled")
            
            self.log(f"Текстовое поле обновлено: показано {point_count} точек", "info")
        except Exception as e:
            self.log(f"Ошибка обновления текстового поля: {e}", "error")
            import traceback
            self.log(f"Детали ошибки: {traceback.format_exc()}", "error")


    def _redraw_recording_graph(self):
        """Перерисовывает график регистрации"""
        try:
            if not MATPLOTLIB_AVAILABLE:
                return
            
            # Фильтруем валидные каналы: числовые (0-15) и виртуальный канал "EMG"
            valid_channels = {}
            for ch, data in self.recording_graph_data.items():
                # Принимаем числовые каналы 0-15 и виртуальный канал "EMG"
                if (isinstance(ch, int) and 0 <= ch <= 15) or ch == 'EMG':
                    valid_channels[ch] = data
            
            if len(valid_channels) == 0:
                self.log("⚠ Нет валидных каналов для отображения", "warning")
                self.log(f"  Доступные каналы: {list(self.recording_graph_data.keys())}", "info")
                return
            
            if len(self.recording_graph_data) != len(valid_channels):
                invalid_count = len(self.recording_graph_data) - len(valid_channels)
                invalid_channels = [ch for ch in self.recording_graph_data.keys() if ch not in valid_channels]
                self.log(f"⚠ Отфильтровано {invalid_count} невалидных каналов: {invalid_channels}", "warning")
            
            # Подсчитываем общее количество точек только для валидных каналов
            total_points = sum(len(data['time']) for data in valid_channels.values())
            
            self.recording_ax.clear()
            self.recording_ax.set_xlabel('Время (с)', fontsize=10)
            self.recording_ax.set_ylabel('Напряжение, µВ', fontsize=10)
            self.recording_ax.set_title(
                f'Регистрация данных Intan RHS2116 (каналов: {len(valid_channels)}, точек: {total_points}, масштаб: µВ)', 
                fontsize=12, fontweight='bold'
            )
            self.recording_ax.grid(True, alpha=0.3)
            
            # Рисуем линии для каждого валидного канала
            colors = plt.cm.tab20(range(16))  # 16 разных цветов для каналов
            plotted_channels = 0
            plotted_points = 0
            
            for channel in sorted(valid_channels.keys(), key=lambda x: (isinstance(x, str), x)):
                data = valid_channels[channel]
                if len(data['time']) > 0:
                    # Для числовых каналов используем цвет по номеру, для EMG - специальный цвет
                    if isinstance(channel, int):
                        color = colors[channel % len(colors)]
                        label = f'Ch{channel} ({len(data["time"])} точек)'
                    else:
                        color = 'red'  # Красный для EMG
                        label = f'{channel} ({len(data["time"])} точек)'
                    
                    self.recording_ax.plot(
                        data['time'], 
                        data['values_uv'], 
                        label=label,
                        color=color,
                        linewidth=0.5,
                        alpha=0.7
                    )
                    plotted_channels += 1
                    plotted_points += len(data['time'])
            
            self.log(f"Построен график: {plotted_channels} каналов, {plotted_points} точек", "info")
            
            # Автоматическое масштабирование осей для лучшей видимости
            # Вычисляем общий диапазон всех данных
            all_values = []
            all_times = []
            for data in valid_channels.values():
                if len(data['values_uv']) > 0:
                    all_values.extend(data['values_uv'])
                    all_times.extend(data['time'])
            
            if all_values:
                # Масштабируем Y-ось с небольшим запасом (10%)
                y_min = min(all_values)
                y_max = max(all_values)
                y_range = y_max - y_min
                if y_range > 0:
                    y_margin = y_range * 0.1  # 10% запас
                    self.recording_ax.set_ylim(y_min - y_margin, y_max + y_margin)
                else:
                    # Если все значения одинаковы, устанавливаем небольшой диапазон вокруг значения
                    center = all_values[0]
                    self.recording_ax.set_ylim(center - 100, center + 100)
                
                # Масштабируем X-ось
                if all_times:
                    x_min = min(all_times)
                    x_max = max(all_times)
                    x_range = x_max - x_min
                    if x_range > 0:
                        x_margin = x_range * 0.02  # 2% запас
                        self.recording_ax.set_xlim(x_min - x_margin, x_max + x_margin)
                    else:
                        self.recording_ax.set_xlim(x_min - 0.1, x_max + 0.1)
                
                # Логируем диапазон для диагностики
                self.log(f"Диапазон Y: [{y_min:.2f}, {y_max:.2f}] µV, диапазон X: [{x_min:.2f}, {x_max:.2f}] с", "info")
            
            # Добавляем легенду, если каналов не слишком много
            if len(valid_channels) <= 8:
                self.recording_ax.legend(loc='upper right', fontsize=8, ncol=2)
            
            self.recording_canvas.draw()
        except Exception as e:
            pass  # Игнорируем ошибки

    def on_clear_graph(self):
        """Очищает график и данные"""
        if MATPLOTLIB_AVAILABLE:
            self.recording_graph_data = {}
            if hasattr(self, 'recording_start_time'):
                delattr(self, 'recording_start_time')
            self.recording_ax.clear()
            self.recording_ax.set_xlabel('Время (с)', fontsize=10)
            self.recording_ax.set_ylabel('Напряжение, µВ', fontsize=10)
            self.recording_ax.set_title('Регистрация данных Intan RHS2116 (масштаб: µВ)', fontsize=12, fontweight='bold')
            self.recording_ax.grid(True, alpha=0.3)
            self.recording_canvas.draw()
        
        # Очищаем текстовое поле
        self.recording_data_text.configure(state="normal")
        self.recording_data_text.delete("1.0", tk.END)
        self.recording_data_text.configure(state="disabled")
        
        # Очищаем все данные
        self.recording_packet_count = 0
        self.recording_hex_data = []
        self.recording_stats_label.config(text="Получено пакетов: 0 | Каналов: 0")
        self.log("График и данные очищены", "info")

    def on_export_recording_data(self):
        """Экспортирует данные регистрации в CSV файл"""
        if not self.recording_graph_data:
            messagebox.showwarning("Предупреждение", "Нет данных для экспорта")
            return
        
        try:
            filename = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
                title="Экспорт данных регистрации"
            )
            
            if not filename:
                return
            
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                
                # Заголовок
                # ВАЖНО: используем key для сортировки смешанных типов (int и str)
                channels = sorted(self.recording_graph_data.keys(), key=lambda x: (isinstance(x, str), x))
                header = ['Время (с)'] + [f'Канал {ch} (µВ)' if isinstance(ch, int) else f'{ch} (µВ)' for ch in channels]
                writer.writerow(header)
                
                # Находим максимальную длину данных
                max_len = max(len(self.recording_graph_data[ch]['time']) for ch in channels) if channels else 0
                
                # Записываем данные
                for i in range(max_len):
                    row = []
                    # Время из первого канала (все каналы должны иметь одинаковое время)
                    if channels and i < len(self.recording_graph_data[channels[0]]['time']):
                        row.append(self.recording_graph_data[channels[0]]['time'][i])
                    else:
                        row.append('')
                    
                    for ch in channels:
                        if i < len(self.recording_graph_data[ch]['values_uv']):
                            row.append(self.recording_graph_data[ch]['values_uv'][i])
                        else:
                            row.append('')
                    
                    writer.writerow(row)
            
            self.log(f"Данные экспортированы в {filename}", "success")
            messagebox.showinfo("Успех", f"Данные успешно экспортированы в {filename}")
        except Exception as e:
            self.log(f"Ошибка экспорта данных: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось экспортировать данные: {e}")

    def on_refresh_text_display(self):
        """Принудительно обновляет текстовое поле с данными"""
        try:
            self.log("Принудительное обновление текстового поля...", "info")
            if not self.recording_graph_data:
                if self.recording_hex_data:
                    self.log("recording_graph_data пуст, но есть hex_data. Запускаем парсинг...", "info")
                    self._parse_hex_data()
                else:
                    self.log("Нет данных для отображения", "warning")
                    self.recording_data_text.configure(state="normal")
                    self.recording_data_text.delete("1.0", tk.END)
                    self.recording_data_text.insert("1.0", 
                        "Нет данных для отображения.\n"
                        "Сначала выполните регистрацию и нажмите '🔍 Построить график'."
                    )
                    self.recording_data_text.configure(state="disabled")
                    return
            
            self._update_recording_text_display()
            self.log("Текстовое поле обновлено", "success")
        except Exception as e:
            self.log(f"Ошибка обновления текстового поля: {e}", "error")
            import traceback
            self.log(f"Детали: {traceback.format_exc()}", "error")
            messagebox.showerror("Ошибка", f"Не удалось обновить текстовое поле: {e}")

    def on_parse_and_plot(self):
        """Вручную запускает парсинг данных и построение графика"""
        if not self.recording_hex_data:
            messagebox.showwarning("Предупреждение", "Нет сохраненных данных для обработки.\nСначала выполните регистрацию.")
            return
        
        try:
            self.log(f"Начинаем обработку {len(self.recording_hex_data)} пакетов...", "info")
            self.recording_stats_label.config(
                text=f"Обработка {len(self.recording_hex_data)} пакетов..."
            )
            
            # Парсим данные и строим график
            self._parse_hex_data()
            
            # Обновляем текстовое поле и статистику после обработки
            self._update_recording_text_display()
            self.recording_stats_label.config(
                text=f"Обработано пакетов: {len(self.recording_hex_data)} | График построен"
            )
            
            messagebox.showinfo("Успех", f"График успешно построен!\nОбработано пакетов: {len(self.recording_hex_data)}")
        except Exception as e:
            self.log(f"Ошибка при построении графика: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось построить график: {e}")

    def on_export_graph(self):
        """Экспортирует график в файл (PNG, PDF, SVG)"""
        if not MATPLOTLIB_AVAILABLE:
            messagebox.showwarning("Предупреждение", "Matplotlib не установлен. График недоступен.")
            return
        
        if not self.recording_graph_data:
            # Если график не построен, но есть hex данные, предлагаем построить
            if self.recording_hex_data:
                if messagebox.askyesno("График не построен", 
                                      "График еще не построен. Построить его сейчас?"):
                    self.on_parse_and_plot()
                    # Повторяем попытку экспорта
                    if not self.recording_graph_data:
                        return
                else:
                    return
            else:
                messagebox.showwarning("Предупреждение", "Нет данных для экспорта")
                return
        
        try:
            filename = filedialog.asksaveasfilename(
                defaultextension=".png",
                filetypes=[
                    ("PNG files", "*.png"),
                    ("PDF files", "*.pdf"),
                    ("SVG files", "*.svg"),
                    ("All files", "*.*")
                ],
                title="Экспорт графика"
            )
            
            if not filename:
                return
            
            # Сохраняем текущий график
            self.recording_figure.savefig(filename, dpi=300, bbox_inches='tight')
            
            self.log(f"График экспортирован в {filename}", "success")
            messagebox.showinfo("Успех", f"График успешно экспортирован в {filename}")
        except Exception as e:
            self.log(f"Ошибка экспорта графика: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось экспортировать график: {e}")

    def on_start_recording(self):
        """Отправляет команду начала регистрации на сервер"""
        if not self.udp_sock or not self.udp_registered:
            messagebox.showerror("Ошибка", "Сначала зарегистрируйтесь на UDP сервере")
            return

        try:
            channels = self.var_recording_channels.get().strip()
            sample_rate = int(self.var_sample_rate.get().strip())
            duration_str = self.var_recording_duration.get().strip()
            
            if sample_rate <= 0:
                raise ValueError("Частота должна быть > 0")
            
            cmd = f"START_RECORDING {channels} {sample_rate}"
            if duration_str:
                duration = float(duration_str)
                if duration <= 0:
                    raise ValueError("Длительность должна быть > 0")
                cmd += f" {duration}"
            
            udp_host = self.var_udp_host.get().strip()
            udp_port = int(self.var_udp_port.get().strip())
            server_addr = (udp_host, udp_port)
            
            self.udp_sock.sendto(cmd.encode('utf-8'), server_addr)
            self.log(f"Отправлена команда начала регистрации: {cmd} на {server_addr}", "info")
            
            # Ждем подтверждение от сервера (с таймаутом)
            try:
                self.udp_sock.settimeout(2.0)
                response, addr = self.udp_sock.recvfrom(1024)
                response_text = response.decode('utf-8').strip()
                if response_text == "RECORDING_STARTED":
                    self.log("✓ Сервер подтвердил начало регистрации", "success")
                else:
                    self.log(f"⚠ Неожиданный ответ от сервера: {response_text}", "warning")
            except socket.timeout:
                self.log("⚠ Таймаут ожидания подтверждения от сервера (возможно, сервер не запущен или не отвечает)", "warning")
            except Exception as e:
                self.log(f"⚠ Ошибка при получении подтверждения: {e}", "warning")
            finally:
                self.udp_sock.settimeout(1.0)  # Возвращаем таймаут для приема данных
            
            self.btn_start_recording.configure(state="disabled")
            self.btn_stop_recording.configure(state="normal")
            self.recording_packet_count = 0
            
            # ВАЖНО: сначала очищаем данные, потом устанавливаем флаг
            # Очищаем предыдущие данные
            self.recording_hex_data = []
            self.recording_graph_data = {}
            if hasattr(self, 'recording_start_time'):
                delattr(self, 'recording_start_time')
            
            # Устанавливаем флаг активной регистрации ПОСЛЕ очистки данных
            self.recording_active = True
            self.log(f"Регистрация начата. recording_active={self.recording_active}, hex_data очищен (размер: {len(self.recording_hex_data)})", "info")
            
            # Очищаем поле данных и график
            self.recording_data_text.configure(state="normal")
            self.recording_data_text.delete("1.0", tk.END)
            self.recording_data_text.insert("1.0", "Регистрация начата. Данные сохраняются...\n")
            self.recording_data_text.configure(state="disabled")
            
            if MATPLOTLIB_AVAILABLE:
                self.recording_ax.clear()
                self.recording_ax.set_xlabel('Время (с)', fontsize=10)
                self.recording_ax.set_ylabel('Напряжение, µВ', fontsize=10)
                self.recording_ax.set_title('Регистрация данных Intan RHS2116 (ожидание данных...)', fontsize=12, fontweight='bold')
                self.recording_ax.grid(True, alpha=0.3)
                self.recording_canvas.draw()
            
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверное значение параметра: {e}")
        except Exception as e:
            self.log(f"Ошибка отправки команды регистрации: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось начать регистрацию: {e}")

    def on_stop_recording(self):
        """Отправляет команду остановки регистрации на сервер"""
        if not self.udp_sock or not self.udp_registered:
            return

        try:
            udp_host = self.var_udp_host.get().strip()
            udp_port = int(self.var_udp_port.get().strip())
            server_addr = (udp_host, udp_port)
            
            self.udp_sock.sendto(b"STOP_RECORDING", server_addr)
            self.log("Отправлена команда остановки регистрации", "info")
            
            # Останавливаем запись данных
            self.recording_active = False
            
            self.btn_start_recording.configure(state="normal")
            self.btn_stop_recording.configure(state="disabled")
            
            # Парсим данные и строим график автоматически после остановки
            if len(self.recording_hex_data) > 0:
                self.log(f"Начинаем автоматическую обработку {len(self.recording_hex_data)} пакетов...", "info")
                self.recording_stats_label.config(
                    text=f"Обработка {len(self.recording_hex_data)} пакетов..."
                )
                # Запускаем парсинг в отдельном потоке, чтобы не блокировать GUI
                threading.Thread(target=self._parse_hex_data_thread, daemon=True).start()
            else:
                self.log("Нет данных для обработки", "warning")
                self.recording_stats_label.config(
                    text="Получено пакетов: 0 | Нет данных"
                )
        except Exception as e:
            self.log(f"Ошибка отправки команды остановки: {e}", "error")

    def _parse_hex_data_thread(self):
        """Запускает парсинг данных в отдельном потоке"""
        try:
            self._parse_hex_data()
            # Проверяем, что данные действительно распарсены
            data_count = len(self.recording_graph_data) if self.recording_graph_data else 0
            self.after(0, lambda dc=data_count: self.log(f"Парсинг завершен. Каналов в recording_graph_data: {dc}", "info"))
            
            # Обновляем текстовое поле и статистику после обработки
            # Важно: вызываем в главном потоке GUI после завершения парсинга
            def update_ui():
                try:
                    # Проверяем наличие данных перед обновлением
                    if self.recording_graph_data:
                        self.log(f"Обновление текстового поля: найдено {len(self.recording_graph_data)} каналов", "info")
                        self._update_recording_text_display()
                    else:
                        self.log("⚠ recording_graph_data пуст, текстовое поле не обновлено", "warning")
                        self.recording_data_text.configure(state="normal")
                        self.recording_data_text.delete("1.0", tk.END)
                        self.recording_data_text.insert("1.0", "Данные распарсены, но recording_graph_data пуст.\nПроверьте логи на наличие ошибок парсинга.")
                        self.recording_data_text.configure(state="disabled")
                    
                    self.recording_stats_label.config(
                        text=f"Обработано пакетов: {len(self.recording_hex_data)} | График построен"
                    )
                except Exception as e:
                    self.log(f"Ошибка обновления UI после парсинга: {e}", "error")
                    import traceback
                    self.log(f"Детали: {traceback.format_exc()}", "error")
            
            self.after(0, update_ui)
        except Exception as e:
            self.after(0, lambda: self.log(f"Ошибка парсинга данных: {e}", "error"))
            import traceback
            self.after(0, lambda: self.log(f"Детали ошибки: {traceback.format_exc()}", "error"))

    # ----------------- Измерения (температура и импеданс) -----------------

    def on_read_temperature(self):
        """Читает температуру с устройства"""
        def worker():
            try:
                cmd = {"cmd": "read_temperature"}
                resp = self.client.send_command(cmd)
                if resp.get("status") == "ok":
                    temp_value = resp.get("temperature", 0)
                    self.after(0, lambda: self.temp_value_label.config(
                        text=f"Температура: {temp_value} (ADC значение)"
                    ))
                    self.log(f"Температура: {temp_value}", "success")
                else:
                    raise Exception(resp.get("error", "Неизвестная ошибка"))
            except Exception as e:
                self.log(f"Ошибка чтения температуры: {e}", "error")
                messagebox.showerror("Ошибка", f"Не удалось прочитать температуру: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_measure_impedance(self):
        """Измерение импеданса. Идеи из jonnew/impedance (RHD2000): bestAmplitude 250 µV, factorOutParallelCapacitance."""
        try:
            ch = int(self.var_impedance_channel.get().strip())
            freq_hz = float(self.var_impedance_freq.get().strip())
            scale_str = self.var_impedance_scale.get().strip()
            if not (0 <= ch <= 15):
                messagebox.showerror("Ошибка", "Канал должен быть 0–15")
                return
            if freq_hz <= 0:
                messagebox.showerror("Ошибка", "Частота должна быть > 0")
                return
            scale_map = {"0.1 pF": (0, 0.1e-12, "0.1 pF"), "1 pF": (1, 1e-12, "1 pF"), "10 pF": (3, 10e-12, "10 pF")}
            if scale_str not in scale_map:
                messagebox.showerror("Ошибка", "Шкала: 0.1 pF, 1 pF или 10 pF")
                return
            scale_bits, C_farad, _ = scale_map[scale_str]
            num_averages = max(1, min(200, int(self.var_impedance_averages.get().strip() or 1)))
            auto_scale = bool(self.var_impedance_auto_scale.get())
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверные значения: {e}")
            return

        V_DAC_AMP = 0.6125
        BEST_AMPLITUDE_UV = 250.0
        C_PARASITIC = 10e-12

        def _do_single_measurement(sb, cf, scale_name):
            self.client.send_line("CLEAR")
            self.client.send_line("WRITE 2 0x0040 U=0 M=0")
            self.client.send_line("WRITE 3 0x0080 U=0 M=0")
            reg2 = (ch << 8) | (1 << 6) | (sb << 3) | 1
            self.client.send_line(f"WRITE 2 0x{reg2:04X} U=0 M=0")
            N = 64
            dac_vals = [max(0, min(255, int(128 + 127 * math.sin(2 * math.pi * i / N)))) for i in range(N)]
            adc_list = []
            for i in range(N):
                self.client.send_line(f"WRITE 3 0x{dac_vals[i]:04X} U=0 M=0")
                r = self.client.send_line(f"CONVERT {ch} U=0 M=0 D=0 H=0")
                m = re.search(r"0x([0-9A-Fa-f]+)", r)
                if m:
                    resp32 = int(m.group(1), 16)
                    adc_list.append((resp32 >> 16) & 0xFFFF)
            self.client.send_line(f"WRITE 2 0x{reg2 & 0xFFFE:04X} U=0 M=0")
            if len(adc_list) < N:
                raise RuntimeError("Недостаточно отсчётов ADC")
            values_uv = [rhs2116_ac_uV(adc) for adc in adc_list[:N]]
            mean_uv = sum(values_uv) / len(values_uv)
            values_ac = [x - mean_uv for x in values_uv]
            v_rms_uv = (sum(x * x for x in values_ac) / len(values_ac)) ** 0.5
            v_amp_uv = v_rms_uv * (2 ** 0.5) if v_rms_uv > 0 else 0
            I_amp = 2 * math.pi * freq_hz * cf * V_DAC_AMP
            z_ohm = (v_amp_uv * 1e-6 / I_amp) if I_amp > 0 else 0.0
            return z_ohm, v_amp_uv, scale_name

        def _factor_out_parallel_capacitance(z_mag, f_hz, c_par):
            """Коррекция паразитной ёмкости (упрощённо для R-электрода). RHD2000: factorOutParallelCapacitance."""
            if z_mag < 1000:
                return z_mag
            w = 2 * math.pi * f_hz
            denom = 1.0 - (w * c_par * z_mag) ** 2
            if denom <= 0.05:
                return z_mag
            return z_mag / math.sqrt(denom)

        def worker():
            try:
                scales_to_use = list(scale_map.items()) if auto_scale else [(scale_str, scale_map[scale_str])]
                if auto_scale:
                    self.after(0, lambda: self.log("Авто C: выбор шкалы по 250 µV (2 пробы на шкалу)...", "info"))
                    best_scale = None
                    best_dist = 1e99
                    AUTO_PROBES = 2  # несколько проб снижают случайный выбор шкалы из-за шума
                    for sstr, (sb, cf, sname) in scales_to_use:
                        v_probes = []
                        for _ in range(AUTO_PROBES):
                            _, v, _ = _do_single_measurement(sb, cf, sname)
                            v_probes.append(v)
                            time.sleep(0.2)
                        v_median = sorted(v_probes)[len(v_probes) // 2]
                        dist = abs(math.log(max(1, v_median) / BEST_AMPLITUDE_UV))
                        if dist < best_dist:
                            best_dist = dist
                            best_scale = (sb, cf, sname)
                        time.sleep(0.2)
                    scale_bits, C_farad, chosen_scale_str = best_scale
                    self.after(0, lambda: self.log(f"Выбрана шкала {chosen_scale_str} (V_amp≈{BEST_AMPLITUDE_UV} µV)", "info"))
                else:
                    scale_bits, C_farad, chosen_scale_str = scale_map[scale_str]

                self.after(0, lambda: self.log(f"Импеданс: {num_averages} измерений (C={chosen_scale_str})...", "info"))
                z_list, z_raw_list, v_list = [], [], []
                V_AMP_MIN = 5.0
                for i in range(num_averages):
                    z_ohm, v_amp_uv, _ = _do_single_measurement(scale_bits, C_farad, chosen_scale_str)
                    if v_amp_uv >= V_AMP_MIN:
                        z_corr = _factor_out_parallel_capacitance(z_ohm, freq_hz, C_PARASITIC)
                        z_list.append(z_corr)
                        z_raw_list.append(z_ohm)
                        v_list.append(v_amp_uv)
                    if i < num_averages - 1:
                        time.sleep(0.5)  # пауза между замерами (было 5 сек — избыточно для AC)
                if len(z_list) < 2:
                    raise RuntimeError("Недостаточно валидных измерений (V_amp < {} µV — шум?)".format(V_AMP_MIN))

                # Отсечение выбросов по MAD: |z - median| > k*MAD считаем выбросом
                MAD_K = 2.5
                z_sorted = sorted(z_list)
                n = len(z_sorted)
                med = z_sorted[n // 2]
                mad = sorted(abs(x - med) for x in z_list)[n // 2] if n > 0 else 0
                mad_scale = 1.4826 * mad if mad > 0 else 0
                threshold = MAD_K * mad_scale if mad_scale > 0 else float("inf")
                filtered = [(z, zr, v) for z, zr, v in zip(z_list, z_raw_list, v_list)
                            if abs(z - med) <= threshold]
                n_rejected = len(z_list) - len(filtered)
                if len(filtered) >= 2:
                    z_list = [x[0] for x in filtered]
                    z_raw_list = [x[1] for x in filtered]
                    v_list = [x[2] for x in filtered]
                    if n_rejected > 0:
                        self.after(0, lambda: self.log(f"Выбросы: отброшено {n_rejected} из {n_rejected + len(filtered)} измерений (MAD×{MAD_K})", "info"))

                z_sorted = sorted(z_list)
                n = len(z_sorted)
                z_ohm = z_sorted[n // 2]
                v_amp_uv = sum(v_list) / len(v_list) if v_list else 0
                mad = sorted(abs(x - z_ohm) for x in z_list)[n // 2] if n > 0 else 0
                std_z = 1.4826 * mad if n > 1 else 0
                n_valid = len(z_list)
                if z_ohm >= 1e6:
                    text = f"Импеданс канала {ch}: {z_ohm/1e6:.2f} ± {std_z/1e6:.2f} MΩ (n={n_valid}, C={chosen_scale_str})"
                elif z_ohm >= 1e3:
                    text = f"Импеданс канала {ch}: {z_ohm/1e3:.1f} ± {std_z/1e3:.1f} кΩ (n={n_valid}, C={chosen_scale_str})"
                else:
                    text = f"Импеданс канала {ch}: {z_ohm:.0f} ± {std_z:.0f} Ω (n={n_valid}, C={chosen_scale_str})"
                if std_z > 2 * max(z_ohm, 1):
                    text += "  ⚠ нестабильно"
                warn = []
                if v_amp_uv > 4000:
                    warn.append("⚠ Насыщение: V_amp>{:.1f} mV — меньшая C".format(v_amp_uv / 1000))
                z_par_ohm = 1.0 / (2 * math.pi * freq_hz * 10e-12)
                if z_ohm > 500e3 and z_ohm < z_par_ohm * 0.9:
                    warn.append("⚠ C~10 pF (Z_C≈{:.1f} MΩ)".format(z_par_ohm / 1e6))
                if warn:
                    text += "  " + " | ".join(warn)
                # Сохраняем данные для экспорта CSV
                self.last_impedance_data = {
                    "params": {"channel": ch, "frequency_hz": freq_hz, "scale": chosen_scale_str,
                               "num_averages": num_averages, "timestamp": datetime.now().isoformat()},
                    "raw_measurements": [{"nom": i + 1, "z_raw": z_raw_list[i], "z_corr": z_list[i],
                                         "V_amp": v_list[i]} for i in range(len(z_list))],
                    "statistics": {"z_median_ohm": z_ohm, "z_std_ohm": std_z, "n_valid": n_valid,
                                  "v_amp_mean_uv": v_amp_uv}
                }
                self.after(0, lambda: self.btn_export_impedance_csv.configure(state="normal"))
                self.after(0, lambda: self.impedance_value_label.config(text=text))
                self.after(0, lambda: self.log(text, "success"))
            except Exception as e:
                self.after(0, lambda err=e: self.log(f"Ошибка измерения импеданса: {err}", "error"))
                self.after(0, lambda err=e: self.impedance_value_label.config(text=f"Ошибка: {err}"))
                self.after(0, lambda err=e: messagebox.showerror("Ошибка", str(err)))
        threading.Thread(target=worker, daemon=True).start()

    def on_export_impedance_csv(self):
        """Выгружает данные последнего измерения импеданса в CSV файл."""
        if not self.last_impedance_data:
            messagebox.showinfo("Экспорт", "Нет данных для экспорта. Сначала выполните измерение импеданса.")
            return
        filename = filedialog.asksaveasfilename(
            title="Сохранить импеданс в CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")],
            initialfile=f"impedance_ch{self.last_impedance_data['params'].get('channel', 0)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        if not filename:
            return
        try:
            data = self.last_impedance_data
            with open(filename, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(["--- PARAMETRY IZMERENIYA ---"])
                for k, v in data["params"].items():
                    writer.writerow([k, v])
                writer.writerow([])
                writer.writerow(["--- KAZHDYY OTDELNYY ZAMER do usredneniya ---"])
                writer.writerow(["nom_zamera", "z_raw", "z_corr", "V_amp"])
                for m in data["raw_measurements"]:
                    writer.writerow([m["nom"], m["z_raw"], m["z_corr"], m["V_amp"]])
                writer.writerow([])
                writer.writerow(["--- STATISTIKA POSLE USREDNENIYA ---"])
                for k, v in data["statistics"].items():
                    writer.writerow([k, v])
                writer.writerow([])
                writer.writerow(["--- SPISOK VALIDNYH ZNAMENIY voshli v raschet ---"])
                writer.writerow(["nom", "z_corr"])
                for i, z in enumerate(data["raw_measurements"], 1):
                    writer.writerow([i, z["z_corr"]])
            self.log(f"Импеданс сохранён: {filename}", "success")
            messagebox.showinfo("Экспорт", f"Данные сохранены в\n{filename}")
        except Exception as e:
            self.log(f"Ошибка экспорта CSV: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось сохранить файл: {e}")

    def _get_adc_bias_from_table(self, adc_rate_ksps):
        """
        Возвращает рекомендуемые значения ADC buffer bias и MUX bias
        из таблицы даташита в зависимости от частоты дискретизации ADC.
        
        Args:
            adc_rate_ksps: частота дискретизации ADC в kS/s
            
        Returns:
            (adc_buffer_bias, mux_bias) или (None, None) если частота вне диапазона
        """
        if adc_rate_ksps <= 120:
            return (32, 40)
        elif adc_rate_ksps <= 140:
            return (16, 40)
        elif adc_rate_ksps <= 175:
            return (8, 40)
        elif adc_rate_ksps <= 220:
            return (8, 32)
        elif adc_rate_ksps <= 280:
            return (8, 26)
        elif adc_rate_ksps <= 350:
            return (4, 18)
        elif adc_rate_ksps <= 440:
            return (3, 16)
        else:  # >= 440
            return (3, 5)

    def _on_adc_rate_changed(self, event=None):
        """Автоматически обновляет значения bias при изменении частоты ADC"""
        try:
            adc_rate = float(self.var_adc_sampling_rate.get().strip())
            adc_buffer_bias, mux_bias = self._get_adc_bias_from_table(adc_rate)
            if adc_buffer_bias is not None:
                self.var_adc_buffer_bias.set(str(adc_buffer_bias))
                self.var_mux_bias.set(str(mux_bias))
        except (ValueError, AttributeError):
            pass  # Игнорируем ошибки парсинга

    def on_auto_adc_bias(self):
        """Автоматически устанавливает значения bias по частоте ADC"""
        try:
            adc_rate = float(self.var_adc_sampling_rate.get().strip())
            adc_buffer_bias, mux_bias = self._get_adc_bias_from_table(adc_rate)
            if adc_buffer_bias is not None:
                self.var_adc_buffer_bias.set(str(adc_buffer_bias))
                self.var_mux_bias.set(str(mux_bias))
                self.log(f"Автоматически установлены значения: ADC buffer bias={adc_buffer_bias}, MUX bias={mux_bias} (для {adc_rate} kS/s)", "info")
            else:
                messagebox.showwarning("Предупреждение", f"Частота {adc_rate} kS/s вне рекомендуемого диапазона")
        except ValueError:
            messagebox.showerror("Ошибка", "Неверное значение частоты ADC")

    def on_apply_adc_bias(self):
        """Применяет настройки ADC bias (Register 0) на сервере"""
        try:
            adc_buffer_bias = int(self.var_adc_buffer_bias.get().strip())
            mux_bias = int(self.var_mux_bias.get().strip())
            
            # Проверяем диапазоны
            if not (0 <= adc_buffer_bias <= 255):
                raise ValueError("ADC buffer bias должен быть в диапазоне 0-255")
            if not (0 <= mux_bias <= 255):
                raise ValueError("MUX bias должен быть в диапазоне 0-255")
            
            # Формируем Register 0
            # Согласно даташиту, Register 0 - это 16-битный регистр
            # Структура: биты [15:8] = ADC buffer bias, биты [7:0] = MUX bias
            reg0_value = (adc_buffer_bias << 8) | mux_bias
            
            def worker():
                try:
                    cmd = {
                        "cmd": "configure_adc",
                        "register_0": reg0_value,
                        "adc_buffer_bias": adc_buffer_bias,
                        "mux_bias": mux_bias
                    }
                    resp = self.client.send_command(cmd)
                    if resp.get("status") == "ok":
                        self.after(0, lambda: self.log(
                            f"Настройки ADC применены: Register 0 = 0x{reg0_value:04X} "
                            f"(ADC buffer bias={adc_buffer_bias}, MUX bias={mux_bias})", 
                            "success"
                        ))
                    else:
                        raise Exception(resp.get("error", "Неизвестная ошибка"))
                except Exception as e:
                    self.log(f"Ошибка применения настроек ADC: {e}", "error")
                    self.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось применить настройки ADC: {e}"))
            
            threading.Thread(target=worker, daemon=True).start()
        except ValueError as e:
            messagebox.showerror("Ошибка", str(e))
        except Exception as e:
            self.log(f"Ошибка применения настроек ADC: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось применить настройки ADC: {e}")

    def on_auto_filters_emg(self):
        """Автоматически устанавливает значения фильтров для ЭМГ"""
        try:
            # Рекомендуемые значения для ЭМГ
            self.var_fh_freq.set("500")
            self.var_reg4.set("0x015E")
            self.var_reg5.set("0x01AB")
            self.var_fl_freq.set("20")
            self.var_reg6.set("0x0036")
            self.var_reg7.set("0x000A")
            self.var_dsp_cutoff.set("9")
            self.var_reg1.set("0x951A")
            self.log("Автоматически установлены значения фильтров для ЭМГ", "info")
        except Exception as e:
            self.log(f"Ошибка установки фильтров: {e}", "error")

    def on_apply_filters(self):
        """Применяет настройки аппаратных фильтров на сервере"""
        try:
            # Парсим значения регистров
            reg4 = self._parse_int_value(self.var_reg4.get())
            reg5 = self._parse_int_value(self.var_reg5.get())
            reg6 = self._parse_int_value(self.var_reg6.get())
            reg7 = self._parse_int_value(self.var_reg7.get())
            reg1 = self._parse_int_value(self.var_reg1.get())
            
            # Проверяем диапазоны
            for reg_val, reg_name in [(reg4, "Register 4"), (reg5, "Register 5"), 
                                      (reg6, "Register 6"), (reg7, "Register 7"), 
                                      (reg1, "Register 1")]:
                if not (0 <= reg_val <= 0xFFFF):
                    raise ValueError(f"{reg_name} должен быть в диапазоне 0-0xFFFF")
            
            # Проверяем DSP cutoff (должен быть 0-15)
            dsp_cutoff = int(self.var_dsp_cutoff.get().strip())
            if not (0 <= dsp_cutoff <= 15):
                raise ValueError("DSP cutoff должен быть в диапазоне 0-15")
            
            # Проверяем, что Register 1 соответствует DSP cutoff
            # Register 1: биты [15:12] = DSP cutoff
            reg1_dsp_cutoff = (reg1 >> 12) & 0xF
            if reg1_dsp_cutoff != dsp_cutoff:
                # Обновляем Register 1 с правильным DSP cutoff
                reg1 = (reg1 & 0x0FFF) | (dsp_cutoff << 12)
                self.var_reg1.set(f"0x{reg1:04X}")
                self.log(f"Обновлен Register 1 для DSP cutoff={dsp_cutoff}: 0x{reg1:04X}", "info")
            
            def worker():
                try:
                    cmd = {
                        "cmd": "configure_filters",
                        "register_1": reg1,
                        "register_4": reg4,
                        "register_5": reg5,
                        "register_6": reg6,
                        "register_7": reg7,
                        "dsp_cutoff": dsp_cutoff,
                        "fh_freq": self.var_fh_freq.get(),
                        "fl_freq": self.var_fl_freq.get()
                    }
                    resp = self.client.send_command(cmd)
                    if resp.get("status") == "ok":
                        self.after(0, lambda: self.log(
                            f"Настройки фильтров применены: Reg1=0x{reg1:04X}, Reg4=0x{reg4:04X}, "
                            f"Reg5=0x{reg5:04X}, Reg6=0x{reg6:04X}, Reg7=0x{reg7:04X} "
                            f"(fH={self.var_fh_freq.get()} Hz, fL={self.var_fl_freq.get()} Hz, DSP cutoff={dsp_cutoff})", 
                            "success"
                        ))
                    else:
                        raise Exception(resp.get("error", "Неизвестная ошибка"))
                except Exception as e:
                    self.log(f"Ошибка применения фильтров: {e}", "error")
                    self.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось применить настройки фильтров: {e}"))
            
            threading.Thread(target=worker, daemon=True).start()
        except ValueError as e:
            messagebox.showerror("Ошибка", str(e))
        except Exception as e:
            self.log(f"Ошибка применения фильтров: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось применить настройки фильтров: {e}")

    def _parse_int_value(self, s: str) -> int:
        """
        Парсит строку как целое число.
        Поддерживает:
          - '123' (decimal)
          - '0x4F00' или '0X4F00' (hex)
          - '4F00' (hex без префикса, если содержит A-F)
        """
        s = s.strip()
        if not s:
            raise ValueError("Пустое значение")
        # Явный hex с префиксом
        if s.lower().startswith("0x"):
            return int(s, 16)
        # Если есть hex-символы A-F — считаем hex
        if any(c in "abcdefABCDEF" for c in s):
            return int(s, 16)
        # Иначе decimal
        return int(s, 10)

    def on_set_recovery_registers(self):
        """Применяет значения регистров 36 и 37 для схемы восстановления заряда."""
        try:
            reg36_str = self.var_recovery_reg36.get()
            reg37_str = self.var_recovery_reg37.get()

            val36 = self._parse_int_value(reg36_str)
            val37 = self._parse_int_value(reg37_str)

            # Ограничиваем до 16 бит
            val36 &= 0xFFFF
            val37 &= 0xFFFF

            commands = [
                f"WRITE 36 0x{val36:04X} U",
                f"WRITE 37 0x{val37:04X} U",
            ]

            cmd = {
                "cmd": "pattern",
                "commands": commands,
                "repeat_count": 1,
            }

            self._send_async(cmd, "Настройка recovery (регистры 36 и 37)")
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверное значение: {e}")
        except Exception as e:
            self.log(f"Ошибка настройки recovery: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось применить настройки recovery: {e}")


    def on_pattern_load_example(self):
        example = """# Пример паттерна стимуляции канала 0
# Настройка токов (можно использовать значение в µA: 20 или формат: 0x8014)
# Система автоматически преобразует значения < 0x8000 в формат 0x8000 + ток
# ВАЖНО: Настройка шага стимуляции и bias (обязательно!)
WRITE 34 0x00E2 U  # Шаг 1 µA (диапазон ±255 µA)
WRITE 35 0x00AA U  # PBIAS/NBIAS для шага 1 µA

# Настройка токов стимуляции (формат 0x80XX, где XX - значение тока в µA)
WRITE 64 0x8014 U  # Отрицательный ток 20 µA (канал 0)
WRITE 96 0x8014 U  # Положительный ток 20 µA (канал 0)

# Установка полярности (положительная для канала 0)
WRITE 44 0x0001 U
# Включение стимуляции канала 0
WRITE 42 0x0001 U
# Задержка (10 раз READ 255)
DELAY 10
# Выключение стимуляции
WRITE 42 0x0000 U
"""
        self.txt_pattern.delete("1.0", tk.END)
        self.txt_pattern.insert("1.0", example)
        self.on_pattern_text_change()

    # ========== МЕТОДЫ КОНСТРУКТОРА ПАТТЕРНОВ ==========
    
    def add_pattern_block(self, block_type):
        """Добавляет новый блок в паттерн"""
        block_id = len(self.pattern_blocks)
        block = {
            "id": block_id,
            "type": block_type,
            "data": {}
        }
        
        # Инициализация данных в зависимости от типа блока
        if block_type == "step_size":
            # Шаг стимуляции: 0x00E2 = 1 µA (по умолчанию)
            block["data"] = {"step_size_hex": "0x00E2", "step_size_ua": 1}
        elif block_type == "current":
            block["data"] = {"channel": 0, "neg_current": 0, "pos_current": 20}
        elif block_type == "polarity":
            block["data"] = {"channel": 0, "positive": True}
        elif block_type == "enable":
            block["data"] = {"channel": 0}
        elif block_type == "disable":
            block["data"] = {"channel": 0}
        elif block_type == "delay":
            block["data"] = {"count": 10}
        elif block_type == "comment":
            block["data"] = {"text": "Комментарий"}
        
        self.pattern_blocks.append(block)
        self.update_blocks_display()
        self.update_pattern_preview()
    
    def remove_pattern_block(self, block_id):
        """Удаляет блок из паттерна"""
        self.pattern_blocks = [b for b in self.pattern_blocks if b["id"] != block_id]
        # Перенумеровываем блоки
        for i, block in enumerate(self.pattern_blocks):
            block["id"] = i
        self.update_blocks_display()
        self.update_pattern_preview()
    
    def move_block_up(self, block_id):
        """Перемещает блок вверх"""
        idx = next((i for i, b in enumerate(self.pattern_blocks) if b["id"] == block_id), None)
        if idx is not None and idx > 0:
            self.pattern_blocks[idx], self.pattern_blocks[idx-1] = self.pattern_blocks[idx-1], self.pattern_blocks[idx]
            self.update_blocks_display()
            self.update_pattern_preview()
    
    def move_block_down(self, block_id):
        """Перемещает блок вниз"""
        idx = next((i for i, b in enumerate(self.pattern_blocks) if b["id"] == block_id), None)
        if idx is not None and idx < len(self.pattern_blocks) - 1:
            self.pattern_blocks[idx], self.pattern_blocks[idx+1] = self.pattern_blocks[idx+1], self.pattern_blocks[idx]
            self.update_blocks_display()
            self.update_pattern_preview()
    
    def update_blocks_display(self):
        """Обновляет отображение блоков"""
        # Очищаем контейнер
        for widget in self.blocks_container.winfo_children():
            widget.destroy()
        
        # Отображаем каждый блок
        for block in self.pattern_blocks:
            self.create_block_widget(block)
    
    def create_block_widget(self, block):
        """Создает виджет для блока"""
        block_frame = ttk.Frame(self.blocks_container, relief="raised", borderwidth=1)
        block_frame.pack(fill="x", padx=5, pady=3)
        
        # Заголовок блока
        header_frame = ttk.Frame(block_frame)
        header_frame.pack(fill="x", padx=5, pady=3)
        
        block_icons = {
            "current": "⚡",
            "polarity": "🔀",
            "enable": "▶",
            "disable": "⏹",
            "delay": "⏱",
            "comment": "💬"
        }
        
        block_names = {
            "current": "Настройка тока",
            "polarity": "Установка полярности",
            "enable": "Включить стимуляцию",
            "disable": "Выключить стимуляцию",
            "delay": "Задержка",
            "comment": "Комментарий"
        }
        
        icon = block_icons.get(block["type"], "📦")
        name = block_names.get(block["type"], "Блок")
        
        ttk.Label(header_frame, text=f"{icon} {name}", font=('Arial', 10, 'bold')).pack(side="left")
        
        # Кнопки управления
        btn_frame = ttk.Frame(header_frame)
        btn_frame.pack(side="right")
        
        ttk.Button(btn_frame, text="⬆", width=3, command=lambda: self.move_block_up(block["id"])).pack(side="left", padx=1)
        ttk.Button(btn_frame, text="⬇", width=3, command=lambda: self.move_block_down(block["id"])).pack(side="left", padx=1)
        ttk.Button(btn_frame, text="🗑", width=3, command=lambda: self.remove_pattern_block(block["id"])).pack(side="left", padx=1)
        
        # Содержимое блока
        content_frame = ttk.Frame(block_frame)
        content_frame.pack(fill="x", padx=5, pady=3)
        
        if block["type"] == "current":
            ttk.Label(content_frame, text="Канал:").grid(row=0, column=0, sticky="w", padx=2, pady=2)
            channel_var = tk.StringVar(value=str(block["data"]["channel"]))
            channel_entry = ttk.Entry(content_frame, textvariable=channel_var, width=5)
            channel_entry.grid(row=0, column=1, sticky="w", padx=2, pady=2)
            channel_var.trace('w', lambda *args, b=block, v=channel_var: self.update_block_data(b, "channel", v.get()))
            
            ttk.Label(content_frame, text="Отрицательный ток (µA):").grid(row=0, column=2, sticky="w", padx=2, pady=2)
            neg_var = tk.StringVar(value=str(block["data"]["neg_current"]))
            neg_entry = ttk.Entry(content_frame, textvariable=neg_var, width=8)
            neg_entry.grid(row=0, column=3, sticky="w", padx=2, pady=2)
            neg_var.trace('w', lambda *args, b=block, v=neg_var: self.update_block_data(b, "neg_current", v.get()))
            
            ttk.Label(content_frame, text="Положительный ток (µA):").grid(row=1, column=0, sticky="w", padx=2, pady=2)
            pos_var = tk.StringVar(value=str(block["data"]["pos_current"]))
            pos_entry = ttk.Entry(content_frame, textvariable=pos_var, width=8)
            pos_entry.grid(row=1, column=1, sticky="w", padx=2, pady=2)
            pos_var.trace('w', lambda *args, b=block, v=pos_var: self.update_block_data(b, "pos_current", v.get()))
            
        elif block["type"] == "polarity":
            ttk.Label(content_frame, text="Канал:").grid(row=0, column=0, sticky="w", padx=2, pady=2)
            channel_var = tk.StringVar(value=str(block["data"]["channel"]))
            channel_entry = ttk.Entry(content_frame, textvariable=channel_var, width=5)
            channel_entry.grid(row=0, column=1, sticky="w", padx=2, pady=2)
            channel_var.trace('w', lambda *args, b=block, v=channel_var: self.update_block_data(b, "channel", v.get()))
            
            ttk.Label(content_frame, text="Полярность:").grid(row=0, column=2, sticky="w", padx=2, pady=2)
            polarity_var = tk.BooleanVar(value=block["data"]["positive"])
            polarity_check = ttk.Checkbutton(content_frame, text="Положительная", variable=polarity_var)
            polarity_check.grid(row=0, column=3, sticky="w", padx=2, pady=2)
            polarity_var.trace('w', lambda *args, b=block, v=polarity_var: self.update_block_data(b, "positive", v.get()))
            
        elif block["type"] in ["enable", "disable"]:
            ttk.Label(content_frame, text="Канал:").grid(row=0, column=0, sticky="w", padx=2, pady=2)
            channel_var = tk.StringVar(value=str(block["data"]["channel"]))
            channel_entry = ttk.Entry(content_frame, textvariable=channel_var, width=5)
            channel_entry.grid(row=0, column=1, sticky="w", padx=2, pady=2)
            channel_var.trace('w', lambda *args, b=block, v=channel_var: self.update_block_data(b, "channel", v.get()))
            
        elif block["type"] == "delay":
            ttk.Label(content_frame, text="Количество циклов READ 255:").grid(row=0, column=0, sticky="w", padx=2, pady=2)
            count_var = tk.StringVar(value=str(block["data"]["count"]))
            count_entry = ttk.Entry(content_frame, textvariable=count_var, width=8)
            count_entry.grid(row=0, column=1, sticky="w", padx=2, pady=2)
            count_var.trace('w', lambda *args, b=block, v=count_var: self.update_block_data(b, "count", v.get()))
            
        elif block["type"] == "step_size":
            ttk.Label(content_frame, text="Шаг (µA):").grid(row=0, column=0, sticky="w", padx=2, pady=2)
            step_ua_var = tk.StringVar(value=str(block["data"].get("step_size_ua", 1)))
            step_ua_entry = ttk.Entry(content_frame, textvariable=step_ua_var, width=8)
            step_ua_entry.grid(row=0, column=1, sticky="w", padx=2, pady=2)
            
            # Таблица соответствия шага и значения Register 34
            # Согласно даташиту: 0x00E2 = 1 µA, другие значения можно добавить позже
            step_size_map = {
                1: "0x00E2",  # 1 µA шаг
            }
            
            def update_step_size(*args):
                try:
                    step_ua = int(step_ua_var.get() or 1)
                    if step_ua in step_size_map:
                        hex_value = step_size_map[step_ua]
                        block["data"]["step_size_ua"] = step_ua
                        block["data"]["step_size_hex"] = hex_value
                    else:
                        # По умолчанию используем 0x00E2 для шага 1 µA
                        block["data"]["step_size_ua"] = 1
                        block["data"]["step_size_hex"] = "0x00E2"
                    self.update_pattern_preview()
                except ValueError:
                    pass
            
            step_ua_var.trace('w', update_step_size)
            ttk.Label(content_frame, text="(Register 34)", font=("TkDefaultFont", 8)).grid(row=0, column=2, sticky="w", padx=2, pady=2)
            
        elif block["type"] == "comment":
            ttk.Label(content_frame, text="Текст:").grid(row=0, column=0, sticky="w", padx=2, pady=2)
            text_var = tk.StringVar(value=block["data"]["text"])
            text_entry = ttk.Entry(content_frame, textvariable=text_var, width=40)
            text_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=2, pady=2)
            text_var.trace('w', lambda *args, b=block, v=text_var: self.update_block_data(b, "text", v.get()))
    
    def update_block_data(self, block, key, value):
        """Обновляет данные блока"""
        try:
            if key in ["channel", "neg_current", "pos_current", "count"]:
                # Сохраняем старое значение для отслеживания изменений
                old_value = block["data"].get(key, 0)
                new_value = int(value) if value else 0
                block["data"][key] = new_value
                # Всегда обновляем визуализацию при изменении токов или канала
                # Это гарантирует, что график будет пересчитан с новыми значениями
                if old_value != new_value:
                    self.update_pattern_preview()
            elif key == "positive":
                old_value = block["data"].get(key, True)
                new_value = bool(value)
                block["data"][key] = new_value
                if old_value != new_value:
                    self.update_pattern_preview()
            else:
                block["data"][key] = value
            self.update_pattern_preview()
        except ValueError:
            pass  # Игнорируем ошибки преобразования

    def update_pattern_preview(self):
        """Обновляет предпросмотр паттерна"""
        commands = self.generate_pattern_commands()
        self.pattern_preview_text.configure(state="normal")
        self.pattern_preview_text.delete("1.0", tk.END)
        self.pattern_preview_text.insert("1.0", "\n".join(commands))
        self.pattern_preview_text.configure(state="disabled")
        # Обновляем также предварительный просмотр формы сигнала
        self.update_pattern_signal_preview()

    def _simulate_blocks_waveform(self, preview_channel: int = 0):
        """
        Простейшая симуляция формы тока по блокам конструктора для одного канала.
        Время задаётся в относительных единицах (шаги DELAY).
        """
        t = 0.0
        base_step = 1.0  # один шаг DELAY = 1 условная единица времени
        times = []
        currents = []

        neg_current = 0.0
        pos_current = 0.0
        polarity_positive = True
        enabled = False
        last_current = 0.0  # Последнее значение тока для отслеживания изменений
        
        # Начальная точка (t=0, ток=0)
        times.append(0.0)
        currents.append(0.0)

        def current_value():
            if not enabled:
                return 0.0
            return pos_current if polarity_positive else -neg_current
        
        def add_point_if_changed():
            """Добавляет точку, если ток изменился"""
            nonlocal last_current
            new_current = current_value()
            if abs(new_current - last_current) > 0.001:  # Учитываем небольшие изменения
                times.append(t)
                currents.append(new_current)
                last_current = new_current
        
        def add_point_force():
            """Принудительно добавляет точку с текущим значением тока"""
            nonlocal last_current
            new_current = current_value()
            times.append(t)
            currents.append(new_current)
            last_current = new_current

        for block in self.pattern_blocks:
            btype = block.get("type")
            data = block.get("data", {})

            if btype == "current":
                ch = int(data.get("channel", 0))
                if ch == preview_channel:
                    old_neg = neg_current
                    old_pos = pos_current
                    old_current_val = current_value()  # Сохраняем старое значение тока
                    neg_current = float(data.get("neg_current", 0) or 0)
                    pos_current = float(data.get("pos_current", 0) or 0)
                    new_current_val = current_value()  # Вычисляем новое значение тока
                    
                    # Фиксируем изменение тока
                    if old_neg != neg_current or old_pos != pos_current:
                        # Если стимуляция включена, сразу фиксируем изменение
                        if enabled:
                            # Добавляем точку с новым значением тока
                            times.append(t)
                            currents.append(new_current_val)
                            last_current = new_current_val
                        # Если стимуляция еще не включена, обновляем last_current
                        # чтобы при включении использовалось новое значение
                        else:
                            last_current = new_current_val

            elif btype == "polarity":
                ch = int(data.get("channel", 0))
                if ch == preview_channel:
                    old_polarity = polarity_positive
                    polarity_positive = bool(data.get("positive", True))
                    # Если полярность изменилась и стимуляция включена, фиксируем изменение
                    if enabled and old_polarity != polarity_positive:
                        add_point_if_changed()
                    elif not enabled:
                        # Обновляем last_current для правильного отображения при включении
                        last_current = current_value()

            elif btype == "enable":
                ch = int(data.get("channel", 0))
                if ch == preview_channel:
                    if not enabled:  # Включаем стимуляцию
                        enabled = True
                        # Принудительно добавляем точку с текущим значением тока
                        add_point_force()

            elif btype == "disable":
                ch = int(data.get("channel", 0))
                if ch == preview_channel:
                    if enabled:  # Выключаем стимуляцию
                        enabled = False
                        # Принудительно добавляем точку с нулевым током
                        add_point_force()

            elif btype == "delay":
                count = int(data.get("count", 10) or 0)
                if count <= 0:
                    continue
                dur = count * base_step
                # Добавляем точку в начале задержки (если еще не добавлена)
                if len(times) == 0 or abs(times[-1] - t) > 0.001:
                    add_point_force()
                # Добавляем точку в конце задержки с текущим значением тока
                t_end = t + dur
                t = t_end
                add_point_force()

        # Если время не изменилось, возвращаем пустые данные
        if len(times) < 2 or all(t == 0.0 for t in times):
            return [], []

        return times, currents

    def update_pattern_signal_preview(self):
        """Рисует приблизительную форму тока по блокам конструктора для выбранного канала."""
        if not hasattr(self, "pattern_signal_canvas"):
            return

        canvas = self.pattern_signal_canvas
        canvas.delete("all")

        # Получаем выбранный канал
        try:
            preview_channel = int(self.var_signal_channel.get() if hasattr(self, 'var_signal_channel') else "0")
            if preview_channel < 0 or preview_channel > 15:
                preview_channel = 0
        except:
            preview_channel = 0

        # Если блоков нет — показываем подсказку
        if not self.pattern_blocks:
            canvas.create_text(
                10,
                10,
                anchor="nw",
                text=f"Добавьте блоки в конструкторе,\nчтобы увидеть форму сигнала (канал {preview_channel}).",
                fill="#777777",
                font=("Arial", 9),
            )
            if hasattr(self, 'signal_metrics_label'):
                self.signal_metrics_label.config(text="")
            return

        times, currents = self._simulate_blocks_waveform(preview_channel=preview_channel)
        if not times or not currents:
            canvas.create_text(
                10,
                10,
                anchor="nw",
                text=f"Недостаточно данных для построения сигнала канала {preview_channel}.",
                fill="#777777",
                font=("Arial", 9),
            )
            if hasattr(self, 'signal_metrics_label'):
                self.signal_metrics_label.config(text="")
            return

        # Вычисляем метрики
        max_current = max(currents) if currents else 0
        min_current = min(currents) if currents else 0
        total_duration = max(times) - min(times) if len(times) > 1 else 0
        
        # Подсчитываем количество импульсов (переходы через ноль или изменения состояния)
        num_pulses = 0
        prev_enabled = False
        for i, cur in enumerate(currents):
            if i == 0:
                prev_enabled = (cur != 0)
            else:
                current_enabled = (cur != 0)
                if current_enabled != prev_enabled:
                    num_pulses += 1
                prev_enabled = current_enabled
        
        # Средний ток (только когда включен)
        enabled_currents = [c for c in currents if c != 0]
        avg_current = sum(enabled_currents) / len(enabled_currents) if enabled_currents else 0
        
        # Обновляем метрики
        if hasattr(self, 'signal_metrics_label'):
            metrics_text = f"Канал {preview_channel}: "
            metrics_text += f"Макс: {max_current:.1f} µA, "
            metrics_text += f"Мин: {min_current:.1f} µA, "
            metrics_text += f"Сред: {avg_current:.1f} µA, "
            metrics_text += f"Длит: {total_duration:.1f}, "
            metrics_text += f"Импульсов: {num_pulses // 2 if num_pulses > 0 else 0}"
            self.signal_metrics_label.config(text=metrics_text)

        # Получаем размеры canvas
        canvas.update_idletasks()
        width = max(int(canvas.winfo_width()), 200)
        height = max(int(canvas.winfo_height()), 80)

        # Нормируем по времени и амплитуде
        t_min, t_max = min(times), max(times)
        t_span = t_max - t_min if t_max > t_min else 1.0

        max_abs = max(abs(v) for v in currents) or 1.0

        # Увеличиваем отступы, чтобы текст не наслаивался на график
        left_pad = 60  # Место для подписей оси Y
        right_pad = 15  # Место для подписей оси X справа
        top_pad = 25   # Место для подписей оси Y сверху
        bottom_pad = 35  # Место для подписей оси X снизу

        # Область для построения графика (без текста)
        graph_left = left_pad
        graph_right = width - right_pad
        graph_top = top_pad
        graph_bottom = height - bottom_pad
        graph_width = graph_right - graph_left
        graph_height = graph_bottom - graph_top

        # Ось времени (горизонтальная середина области графика)
        mid_y = (graph_top + graph_bottom) / 2
        canvas.create_line(
            graph_left,
            mid_y,
            graph_right,
            mid_y,
            fill="#cccccc",
            dash=(2, 2),
            width=1,
        )

        # Подпись оси Y (ток) - слева от графика
        canvas.create_text(
            left_pad - 10,
            graph_top,
            anchor="n",
            text=f"{max_abs:.0f}",
            fill="#666666",
            font=("Arial", 8),
        )
        canvas.create_text(
            left_pad - 10,
            graph_bottom,
            anchor="s",
            text=f"{-max_abs:.0f}",
            fill="#666666",
            font=("Arial", 8),
        )
        canvas.create_text(
            left_pad - 30,
            mid_y,
            anchor="e",
            text="µA",
            fill="#666666",
            font=("Arial", 8),
        )
        
        # Подпись оси X (время) - снизу от графика
        canvas.create_text(
            graph_left,
            height - bottom_pad + 20,
            anchor="sw",
            text="0",
            fill="#666666",
            font=("Arial", 8),
        )
        canvas.create_text(
            graph_right,
            height - bottom_pad + 20,
            anchor="se",
            text=f"{total_duration:.1f}",
            fill="#666666",
            font=("Arial", 8),
        )
        
        # Подпись "Время" под осью X
        canvas.create_text(
            (graph_left + graph_right) / 2,
            height - 10,
            anchor="s",
            text="Время",
            fill="#666666",
            font=("Arial", 8),
        )

        # Строим ломаную в области графика
        points = []
        for t_val, cur in zip(times, currents):
            # Нормированное время [0..1]
            x_norm = (t_val - t_min) / t_span if t_span > 0 else 0
            x = graph_left + x_norm * graph_width

            # Нормированная амплитуда [-1..1]
            a_norm = cur / max_abs if max_abs > 0 else 0
            y = mid_y - a_norm * (graph_height / 2 - 5)

            points.extend([x, y])

        if len(points) >= 4:
            canvas.create_line(
                *points,
                fill="#4CAF50",
                width=2,
                smooth=False,
            )
    
    def generate_pattern_commands(self):
        """Генерирует список команд из блоков"""
        commands = []
        
        # ВАЖНО: Определяем шаг стимуляции из блоков
        # Ищем блок step_size для определения текущего шага
        step_size_ua = 1  # По умолчанию 1 µA
        step_size_hex = "0x00E2"  # По умолчанию 0x00E2
        has_step_size_block = False
        
        for block in self.pattern_blocks:
            if block.get("type") == "step_size":
                has_step_size_block = True
                step_size_ua = block["data"].get("step_size_ua", 1)
                step_size_hex = block["data"].get("step_size_hex", "0x00E2")
                break
        
        # Добавляем Register 34 (step size) в начало, если блока нет
        if not has_step_size_block:
            commands.append("WRITE 34 0x00E2 U  # Шаг 1 µA (по умолчанию)")
        else:
            commands.append(f"WRITE 34 {step_size_hex} U  # Шаг {step_size_ua} µA")
        
        # Register 35 (bias) - зависит от шага, но пока используем стандартное значение для шага 1 µA
        commands.append("WRITE 35 0x00AA U  # PBIAS/NBIAS для шага 1 µA")
        
        for block in self.pattern_blocks:
            if block["type"] == "step_size":
                # Блок step_size уже обработан выше, пропускаем
                continue
            elif block["type"] == "current":
                channel = block["data"].get("channel", 0)
                neg_current_ua = block["data"].get("neg_current", 0)  # Значение в микроамперах
                pos_current_ua = block["data"].get("pos_current", 0)  # Значение в микроамперах
                
                # КРИТИЧНО: Пересчитываем ток в зависимости от шага
                # Значение в регистре = ток_в_µA / шаг_в_µA
                # Например, при шаге 1 µA: ток 10 µA -> значение 10
                # При шаге 0.1 µA: ток 10 µA -> значение 100 (но это выходит за пределы 0-255)
                
                # Для шага 1 µA: значение = ток напрямую
                # Для других шагов нужно пересчитать
                if step_size_ua > 0:
                    neg_reg_value = int(neg_current_ua / step_size_ua) if neg_current_ua > 0 else 0
                    pos_reg_value = int(pos_current_ua / step_size_ua) if pos_current_ua > 0 else 0
                    
                    # Ограничиваем до 0-255
                    neg_reg_value = max(0, min(255, neg_reg_value))
                    pos_reg_value = max(0, min(255, pos_reg_value))
                else:
                    # Если шаг не определен, используем значение как есть (предполагаем шаг 1 µA)
                    neg_reg_value = max(0, min(255, int(neg_current_ua)))
                    pos_reg_value = max(0, min(255, int(pos_current_ua)))
                
                # Регистры: 64-79 для отрицательного, 96-111 для положительного
                # Формат регистров токов согласно даташиту:
                # - Биты [15:8] = positive/negative current trim [7:0] (0x80 = 128 = нормальное значение без подстройки)
                # - Биты [7:0] = positive/negative current magnitude [7:0] (величина тока)
                # Формат: 0x8000 | (reg_value & 0xFF) где 0x80 = trim = 128 (без подстройки)
                if neg_current_ua > 0:
                    reg_value = 0x8000 | (neg_reg_value & 0xFF)  # 0x80 = trim (128), биты [7:0] = ток
                    commands.append(f"WRITE {64 + channel} 0x{reg_value:04X} U  # {neg_current_ua} µA (шаг {step_size_ua} µA)")
                if pos_current_ua > 0:
                    reg_value = 0x8000 | (pos_reg_value & 0xFF)  # 0x80 = trim (128), биты [7:0] = ток
                    commands.append(f"WRITE {96 + channel} 0x{reg_value:04X} U  # {pos_current_ua} µA (шаг {step_size_ua} µA)")
            elif block["type"] == "polarity":
                channel = block["data"].get("channel", 0)
                positive = block["data"].get("positive", True)
                # Регистр 44: битовая маска, бит channel = 1 для положительной
                polarity_mask = (1 << channel) if positive else 0x0000
                commands.append(f"WRITE 44 0x{polarity_mask:04X} U")
            elif block["type"] == "enable":
                channel = block["data"].get("channel", 0)
                # Регистр 42: битовая маска, бит channel = 1 для включения
                enable_mask = 1 << channel
                commands.append(f"WRITE 42 0x{enable_mask:04X} U")
            elif block["type"] == "disable":
                channel = block["data"].get("channel", 0)
                # Регистр 42: сбрасываем бит channel
                commands.append(f"WRITE 42 0x0000 U")
            elif block["type"] == "delay":
                count = block["data"].get("count", 10)
                commands.append(f"DELAY {count}")
            elif block["type"] == "comment":
                text = block["data"].get("text", "")
                if text:
                    commands.append(f"# {text}")
        return commands
    
    def generate_pattern_from_blocks(self):
        """Генерирует паттерн из блоков и копирует в текстовый редактор"""
        commands = self.generate_pattern_commands()
        pattern_text = "\n".join(commands)
        self.txt_pattern.delete("1.0", tk.END)
        self.txt_pattern.insert("1.0", pattern_text)
        self.on_pattern_text_change()
        self.log("Паттерн сгенерирован из блоков и скопирован в текстовый редактор", "success")
    
    def copy_pattern_from_constructor(self):
        """Копирует паттерн из конструктора в текстовый редактор"""
        self.generate_pattern_from_blocks()
    
    def clear_all_blocks(self):
        """Очищает все блоки"""
        if messagebox.askyesno("Подтверждение", "Удалить все блоки?"):
            self.pattern_blocks = []
            self.update_blocks_display()
            self.update_pattern_preview()
    
    def load_example_blocks(self):
        """Загружает пример паттерна из блоков"""
        self.pattern_blocks = []
        # Блок 1: Настройка тока
        self.add_pattern_block("current")
        self.pattern_blocks[-1]["data"] = {"channel": 0, "neg_current": 0, "pos_current": 20}
        # Блок 2: Полярность
        self.add_pattern_block("polarity")
        self.pattern_blocks[-1]["data"] = {"channel": 0, "positive": True}
        # Блок 3: Включение
        self.add_pattern_block("enable")
        self.pattern_blocks[-1]["data"] = {"channel": 0}
        # Блок 4: Задержка
        self.add_pattern_block("delay")
        self.pattern_blocks[-1]["data"] = {"count": 10}
        # Блок 5: Выключение
        self.add_pattern_block("disable")
        self.pattern_blocks[-1]["data"] = {"channel": 0}
        
        self.update_blocks_display()
        self.update_pattern_preview()
        self.log("Загружен пример паттерна", "info")


def main():
    app = IntanGuiApp()
    app.mainloop()


if __name__ == "__main__":
    main()
