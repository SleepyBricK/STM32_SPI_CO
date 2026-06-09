#!/usr/bin/env python3
"""
Расширенный GUI‑клиент для управления Intan RHS2116 через Orange Pi.

Архитектура (актуальная):
  ПК (этот GUI) --TCP 9000/UDP 9001--> Orange Pi (intan_server.py --backend usb)
       --USB HS 0483:5741--> STM32H743 (STM32_SPI_CO) --SPI--> Intan RHS2116

GUI не обращается к SPI/GPIO Orange Pi напрямую — только JSON по TCP и UDP.

На плате должен работать: python3 intan_server.py --backend usb
"""

from __future__ import annotations

import json
import math
import os
import queue
import socket
import struct
import threading
import time
from typing import Optional

# Таймауты TCP (сек): USB-сопроцессор медленнее локального spidev
TCP_TIMEOUT_DEFAULT = 10.0
TCP_TIMEOUT_READ_REG = 15.0
TCP_TIMEOUT_INIT = 120.0
TCP_TIMEOUT_PULSE = 30.0
TCP_TIMEOUT_SEND_LINE = 15.0
TCP_TIMEOUT_IMPEDANCE = 180.0
INTAN_CHIP_ID_REG = 255
INTAN_CHIP_ID_EXPECTED = 32

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


RECORDING_TEXT_PREVIEW_LIMIT = 2000
# Новые USB/UDP batch-форматы уже приходят после компенсации двухслотовой Intan pipeline.
USB_RECORDING_PIPELINE_SKIP_FRAMES = 0
RECORDING_PLOT_MAX_POINTS_PER_CHANNEL = 5000
RECORDING_SPECTRUM_MIN_POINTS = 32

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

    @staticmethod
    def timeout_for_command(cmd: dict) -> float:
        """Подбирает таймаут под команду (USB/STM32 на стороне сервера)."""
        name = (cmd.get("cmd") or "").lower()
        if name == "init":
            return TCP_TIMEOUT_INIT
        if name == "read_register":
            return TCP_TIMEOUT_READ_REG
        if name in ("pulse", "sawtooth", "pattern_run", "pattern_load", "stop"):
            return TCP_TIMEOUT_PULSE
        if name == "send_line":
            return TCP_TIMEOUT_SEND_LINE
        if name in ("measure_impedance", "measure_impedance_fast"):
            return TCP_TIMEOUT_IMPEDANCE
        if name == "configure_adc":
            return TCP_TIMEOUT_INIT
        return TCP_TIMEOUT_DEFAULT

    def send_command(self, cmd: dict, timeout: Optional[float] = None) -> dict:
        """
        Отправляет JSON‑команду и возвращает JSON‑ответ.
        """
        if timeout is None:
            timeout = self.timeout_for_command(cmd)
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

    @staticmethod
    def require_ok(resp: dict, context: str = "команда") -> dict:
        """Проверяет status==ok в ответе TCP-сервера, иначе RuntimeError."""
        if resp.get("status") == "ok":
            return resp
        err = resp.get("error") or resp.get("message") or json.dumps(resp, ensure_ascii=False)
        raise RuntimeError(f"{context}: {err}")

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


class RecordingGraphWindow(tk.Toplevel):
    """Отдельное окно для просмотра графика регистрации и спектра."""

    def __init__(self, master, app: "IntanGuiApp"):
        super().__init__(master)
        self.app = app
        self.title("График регистрации Intan RHS2116")
        self.geometry("1280x860")
        self.minsize(900, 600)

        self._visible_channels = set(range(16))
        self._show_grid = tk.BooleanVar(value=True)
        self._show_legend = tk.BooleanVar(value=True)
        self._auto_fit_time = tk.BooleanVar(value=True)
        self._auto_fit_spectrum = tk.BooleanVar(value=True)
        self._channel_vars: dict[int, tk.BooleanVar] = {}
        self._time_data_limits = None
        self._spectrum_data_limits = None

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._create_widgets()

    def _on_close(self):
        self.app.recording_graph_window = None
        self.destroy()

    def show_or_raise(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _create_widgets(self):
        pad = 6
        controls = ttk.Frame(self, padding=pad)
        controls.pack(fill="x")

        ttk.Button(
            controls, text="🔄 Обновить", command=self.refresh_from_app, width=14
        ).pack(side="left", padx=(0, pad))

        ttk.Button(
            controls, text="↺ Сброс масштаба", command=self.reset_all_zoom, width=16
        ).pack(side="left", padx=(0, pad))

        ttk.Checkbutton(
            controls, text="Авто-масштаб", variable=self._auto_fit_time,
            command=self.refresh_from_app,
        ).pack(side="left", padx=(0, pad))

        ttk.Checkbutton(
            controls, text="Сетка", variable=self._show_grid,
            command=self.refresh_from_app,
        ).pack(side="left", padx=(0, pad))

        ttk.Checkbutton(
            controls, text="Легенда", variable=self._show_legend,
            command=self.refresh_from_app,
        ).pack(side="left", padx=(0, pad))

        ttk.Button(
            controls, text="Все каналы", command=self._select_all_channels, width=12
        ).pack(side="left", padx=(pad, 0))

        ttk.Button(
            controls, text="Снять все", command=self._deselect_all_channels, width=12
        ).pack(side="left", padx=(pad, 0))

        ttk.Button(
            controls, text="📊 Экспорт", command=self.export_current_plot, width=12
        ).pack(side="right")

        channels_frame = ttk.LabelFrame(self, text="Видимые каналы", padding=pad)
        channels_frame.pack(fill="x", padx=pad, pady=(0, pad))

        for channel in range(16):
            var = tk.BooleanVar(value=True)
            self._channel_vars[channel] = var
            ttk.Checkbutton(
                channels_frame,
                text=f"Ch{channel}",
                variable=var,
                command=self._on_channel_visibility_changed,
                width=6,
            ).pack(side="left", padx=2, pady=2)

        self.status_label = ttk.Label(
            self, text="Откройте окно и нажмите «Обновить» или «Построить график» в главном окне.",
            style="Status.TLabel",
        )
        self.status_label.pack(fill="x", padx=pad, pady=(0, pad))

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        self.plot_notebook = notebook

        time_tab = ttk.Frame(notebook)
        spectrum_tab = ttk.Frame(notebook)
        notebook.add(time_tab, text="📊 Временная область")
        notebook.add(spectrum_tab, text="📈 Спектр")

        if not MATPLOTLIB_AVAILABLE:
            ttk.Label(
                time_tab,
                text="Для графиков установите matplotlib:\npip install matplotlib",
                justify="center",
            ).pack(expand=True)
            return

        self.recording_figure = Figure(figsize=(12, 7), dpi=100)
        self.recording_ax = self.recording_figure.add_subplot(111)
        self._setup_time_axes("Регистрация данных Intan RHS2116")

        self.recording_canvas = FigureCanvasTkAgg(self.recording_figure, time_tab)
        self.recording_canvas.draw()
        self.recording_canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

        time_toolbar_frame = ttk.Frame(time_tab)
        time_toolbar_frame.pack(side="top", fill="x")
        self.recording_toolbar = NavigationToolbar2Tk(self.recording_canvas, time_toolbar_frame)
        self.recording_toolbar.update()
        self.recording_canvas.mpl_connect(
            "button_release_event", self._on_time_canvas_interaction
        )

        self.recording_spectrum_figure = Figure(figsize=(12, 7), dpi=100)
        self.recording_spectrum_ax = self.recording_spectrum_figure.add_subplot(111)
        self._setup_spectrum_axes("Спектральное разложение данных Intan RHS2116")

        self.recording_spectrum_canvas = FigureCanvasTkAgg(
            self.recording_spectrum_figure, spectrum_tab
        )
        self.recording_spectrum_canvas.draw()
        self.recording_spectrum_canvas.get_tk_widget().pack(
            side="top", fill="both", expand=True
        )

        spectrum_toolbar_frame = ttk.Frame(spectrum_tab)
        spectrum_toolbar_frame.pack(side="top", fill="x")
        self.recording_spectrum_toolbar = NavigationToolbar2Tk(
            self.recording_spectrum_canvas, spectrum_toolbar_frame
        )
        self.recording_spectrum_toolbar.update()
        self.recording_spectrum_canvas.mpl_connect(
            "button_release_event", self._on_spectrum_canvas_interaction
        )

    def _setup_time_axes(self, title: str):
        self.recording_ax.set_xlabel("Время (с)", fontsize=10)
        self.recording_ax.set_ylabel("Напряжение, µВ", fontsize=10)
        self.recording_ax.set_title(title, fontsize=12, fontweight="bold")
        self.recording_ax.grid(self._show_grid.get(), alpha=0.3)

    def _setup_spectrum_axes(self, title: str):
        self.recording_spectrum_ax.set_xlabel("Частота (Гц)", fontsize=10)
        self.recording_spectrum_ax.set_ylabel("Амплитуда, µВ", fontsize=10)
        self.recording_spectrum_ax.set_title(title, fontsize=12, fontweight="bold")
        self.recording_spectrum_ax.grid(self._show_grid.get(), alpha=0.3)

    def _on_time_canvas_interaction(self, _event):
        if not self._auto_fit_time.get():
            return
        self._auto_fit_time.set(False)

    def _on_spectrum_canvas_interaction(self, _event):
        if not self._auto_fit_spectrum.get():
            return
        self._auto_fit_spectrum.set(False)

    def _select_all_channels(self):
        for var in self._channel_vars.values():
            var.set(True)
        self._sync_visible_channels()
        self.redraw_time(self.app.recording_graph_data)
        self.redraw_spectrum(self.app.recording_spectrum_data)

    def _deselect_all_channels(self):
        for var in self._channel_vars.values():
            var.set(False)
        self._sync_visible_channels()
        self.redraw_time(self.app.recording_graph_data)
        self.redraw_spectrum(self.app.recording_spectrum_data)

    def _on_channel_visibility_changed(self):
        self._sync_visible_channels()
        self.redraw_time(self.app.recording_graph_data)
        self.redraw_spectrum(self.app.recording_spectrum_data)

    def _sync_visible_channels(self):
        self._visible_channels = {
            channel for channel, var in self._channel_vars.items() if var.get()
        }

    def refresh_from_app(self):
        self.redraw_time(self.app.recording_graph_data)
        self.redraw_spectrum(self.app.recording_spectrum_data)

    def reset_all_zoom(self):
        self._auto_fit_time.set(True)
        self._auto_fit_spectrum.set(True)
        self.refresh_from_app()

    def reset_time_zoom(self):
        if self._time_data_limits:
            x_min, x_max, y_min, y_max = self._time_data_limits
            y_range = y_max - y_min
            x_range = x_max - x_min
            if y_range > 0:
                y_margin = y_range * 0.1
                self.recording_ax.set_ylim(y_min - y_margin, y_max + y_margin)
            else:
                self.recording_ax.set_ylim(y_min - 100, y_max + 100)
            if x_range > 0:
                x_margin = x_range * 0.02
                self.recording_ax.set_xlim(x_min - x_margin, x_max + x_margin)
            else:
                self.recording_ax.set_xlim(x_min - 0.1, x_max + 0.1)
            self.recording_canvas.draw()

    def clear(self):
        if not MATPLOTLIB_AVAILABLE or not hasattr(self, "recording_ax"):
            return
        self._time_data_limits = None
        self._spectrum_data_limits = None
        self.recording_ax.clear()
        self._setup_time_axes("Регистрация данных Intan RHS2116 (масштаб: µВ)")
        self.recording_canvas.draw()

        self.recording_spectrum_ax.clear()
        self._setup_spectrum_axes("Спектральное разложение данных Intan RHS2116")
        self.recording_spectrum_canvas.draw()
        self.status_label.config(text="График очищен")

    def _filter_valid_channels(self, graph_data: dict) -> dict:
        valid_channels = {}
        for channel, data in graph_data.items():
            if isinstance(channel, int) and 0 <= channel <= 15:
                if channel in self._visible_channels:
                    valid_channels[channel] = data
        return valid_channels

    def redraw_time(self, graph_data: dict):
        if not MATPLOTLIB_AVAILABLE or not hasattr(self, "recording_ax"):
            return

        valid_channels = self._filter_valid_channels(graph_data)
        if not valid_channels:
            self.recording_ax.clear()
            self._setup_time_axes("Регистрация данных Intan RHS2116")
            if not graph_data:
                message = "Нет данных. Выполните регистрацию и постройте график."
            elif not self._visible_channels:
                message = "Не выбран ни один канал для отображения."
            else:
                message = "Нет валидных каналов для отображения."
            self.recording_ax.text(
                0.5, 0.5, message,
                transform=self.recording_ax.transAxes,
                ha="center", va="center", fontsize=11, color="gray",
            )
            self.recording_canvas.draw()
            self.status_label.config(text=message)
            return

        total_points = sum(len(data["time"]) for data in valid_channels.values())
        self.recording_ax.clear()
        self.recording_ax.set_xlabel("Время (с)", fontsize=10)
        self.recording_ax.set_ylabel("Напряжение, µВ", fontsize=10)
        self.recording_ax.set_title(
            f"Регистрация данных Intan RHS2116 "
            f"(каналов: {len(valid_channels)}, точек: {total_points}, масштаб: µВ)",
            fontsize=12, fontweight="bold",
        )
        self.recording_ax.grid(self._show_grid.get(), alpha=0.3)

        colors = plt.cm.tab20(range(16))
        plotted_channels = 0
        plotted_points = 0
        sampled_points = 0
        y_min = y_max = x_min = x_max = None

        for channel in sorted(valid_channels.keys()):
            data = valid_channels[channel]
            plot_times = data.get("point_time", data["time"])
            plot_values = data["values_uv"]
            point_count = len(plot_times)
            if point_count <= 0:
                continue

            if point_count > RECORDING_PLOT_MAX_POINTS_PER_CHANNEL:
                sample_indices = np.linspace(
                    0, point_count - 1, RECORDING_PLOT_MAX_POINTS_PER_CHANNEL, dtype=int,
                )
                plot_times_sampled = [plot_times[i] for i in sample_indices]
                plot_values_sampled = [plot_values[i] for i in sample_indices]
            else:
                plot_times_sampled = plot_times
                plot_values_sampled = plot_values

            local_y_min = min(plot_values)
            local_y_max = max(plot_values)
            local_x_min = plot_times[0]
            local_x_max = plot_times[-1]
            y_min = local_y_min if y_min is None else min(y_min, local_y_min)
            y_max = local_y_max if y_max is None else max(y_max, local_y_max)
            x_min = local_x_min if x_min is None else min(x_min, local_x_min)
            x_max = local_x_max if x_max is None else max(x_max, local_x_max)

            self.recording_ax.plot(
                plot_times_sampled,
                plot_values_sampled,
                label=f"Ch{channel} ({point_count} точек)",
                color=colors[channel % len(colors)],
                linewidth=0.5,
                alpha=0.7,
            )
            plotted_channels += 1
            plotted_points += point_count
            sampled_points += len(plot_times_sampled)

        if y_min is not None and y_max is not None:
            self._time_data_limits = (x_min, x_max, y_min, y_max)
            if self._auto_fit_time.get():
                self.reset_time_zoom()

        if self._show_legend.get() and plotted_channels <= 8:
            self.recording_ax.legend(loc="upper right", fontsize=8, ncol=2)

        self.recording_canvas.draw()
        status = (
            f"Временная область: {plotted_channels} каналов, "
            f"{sampled_points}/{plotted_points} точек"
        )
        if y_min is not None and y_max is not None:
            status += f" | Y: [{y_min:.2f}, {y_max:.2f}] µV, X: [{x_min:.2f}, {x_max:.2f}] с"
        self.status_label.config(text=status)

    def redraw_spectrum(self, spectrum_data: dict):
        if not MATPLOTLIB_AVAILABLE or not hasattr(self, "recording_spectrum_ax"):
            return

        self.recording_spectrum_ax.clear()
        self.recording_spectrum_ax.set_xlabel("Частота (Гц)", fontsize=10)
        self.recording_spectrum_ax.set_ylabel("Амплитуда, µВ", fontsize=10)
        self.recording_spectrum_ax.set_title(
            "Спектральное разложение данных Intan RHS2116",
            fontsize=12, fontweight="bold",
        )
        self.recording_spectrum_ax.grid(self._show_grid.get(), alpha=0.3)

        visible_spectrum = {
            channel: data for channel, data in spectrum_data.items()
            if channel in self._visible_channels
        }

        if not visible_spectrum:
            message = (
                "Недостаточно данных для спектрального разложения"
                if spectrum_data else "Спектр будет доступен после построения графика"
            )
            self.recording_spectrum_ax.text(
                0.5, 0.5, message,
                transform=self.recording_spectrum_ax.transAxes,
                ha="center", va="center", fontsize=11, color="gray",
            )
            self.recording_spectrum_canvas.draw()
            return

        colors = plt.cm.tab20(range(16))
        plotted_channels = 0
        sampled_points = 0
        max_freq = 0.0
        max_amp = 0.0

        for channel in sorted(visible_spectrum.keys()):
            spectrum = visible_spectrum[channel]
            freqs = spectrum["freqs_hz"]
            amplitudes = spectrum["amplitude_uv"]
            point_count = len(freqs)
            if point_count < 2:
                continue

            if point_count > RECORDING_PLOT_MAX_POINTS_PER_CHANNEL:
                sample_indices = np.linspace(
                    0, point_count - 1, RECORDING_PLOT_MAX_POINTS_PER_CHANNEL, dtype=int,
                )
                freqs_to_plot = freqs[sample_indices]
                amplitudes_to_plot = amplitudes[sample_indices]
            else:
                freqs_to_plot = freqs
                amplitudes_to_plot = amplitudes

            self.recording_spectrum_ax.plot(
                freqs_to_plot,
                amplitudes_to_plot,
                label=(
                    f"Ch{channel} "
                    f"(Fs={spectrum['sample_rate_hz']:.1f} Гц, N={spectrum['point_count']})"
                ),
                color=colors[channel % len(colors)],
                linewidth=0.8,
                alpha=0.8,
            )
            plotted_channels += 1
            sampled_points += len(freqs_to_plot)
            max_freq = max(max_freq, float(freqs[-1]))
            max_amp = max(max_amp, float(np.max(amplitudes)))

        if plotted_channels == 0:
            self.recording_spectrum_ax.text(
                0.5, 0.5, "Недостаточно данных для спектрального разложения",
                transform=self.recording_spectrum_ax.transAxes,
                ha="center", va="center", fontsize=11, color="gray",
            )
        else:
            self._spectrum_data_limits = (0.0, max_freq, 0.0, max_amp * 1.1)
            if self._auto_fit_spectrum.get():
                self.recording_spectrum_ax.set_xlim(0, max_freq if max_freq > 0 else 1)
                self.recording_spectrum_ax.set_ylim(0, max_amp * 1.1 if max_amp > 0 else 1)
            if self._show_legend.get() and plotted_channels <= 8:
                self.recording_spectrum_ax.legend(loc="upper right", fontsize=8, ncol=1)

        self.recording_spectrum_canvas.draw()

    def export_current_plot(self):
        if not MATPLOTLIB_AVAILABLE:
            messagebox.showwarning("Предупреждение", "Matplotlib не установлен.")
            return

        filename = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".png",
            filetypes=[
                ("PNG files", "*.png"),
                ("PDF files", "*.pdf"),
                ("SVG files", "*.svg"),
                ("All files", "*.*"),
            ],
            title="Экспорт графика",
        )
        if not filename:
            return

        try:
            selected_tab = self.plot_notebook.index(self.plot_notebook.select())
            figure = (
                self.recording_spectrum_figure
                if selected_tab == 1
                else self.recording_figure
            )
            figure.savefig(filename, dpi=300, bbox_inches="tight")
            self.app.log(f"График экспортирован в {filename}", "success")
            messagebox.showinfo("Успех", f"График успешно экспортирован в {filename}", parent=self)
        except Exception as exc:
            self.app.log(f"Ошибка экспорта графика: {exc}", "error")
            messagebox.showerror("Ошибка", f"Не удалось экспортировать график: {exc}", parent=self)

    def set_waiting_state(self):
        if not MATPLOTLIB_AVAILABLE or not hasattr(self, "recording_ax"):
            return
        self.recording_ax.clear()
        self._setup_time_axes("Регистрация данных Intan RHS2116 (ожидание данных...)")
        self.recording_canvas.draw()

        self.recording_spectrum_ax.clear()
        self._setup_spectrum_axes("Спектральное разложение данных Intan RHS2116 (ожидание данных...)")
        self.recording_spectrum_canvas.draw()
        self.status_label.config(text="Ожидание данных регистрации...")


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

        # UDP регистрация - инициализация переменных
        self.udp_sock = None
        self.udp_registered = False
        self.udp_listening = False
        self.udp_listen_thread = None
        self.udp_control_messages = queue.Queue()
        self.recording_packet_count = 0
        self.recording_values_received = 0
        self.recording_samples_received = 0
        self.recording_receive_started_at = None
        self.recording_graph_data = {}  # Данные для графика
        self.recording_spectrum_data = {}  # Данные спектрального разложения
        self.recording_graph_window = None  # Отдельное окно графика
        self.recording_hex_data = []  # Список срезов (offset, size) в recording_blob
        self.recording_blob = bytearray()
        self.recording_channel_list = list(range(16))
        self.recording_active = False  # Флаг активной регистрации
        self.recording_stop_processed = False  # Защита от двойной финализации остановки
        self.recording_sample_rate = None  # Частота регистрации для вычисления point_time
        self.recording_gap_events = []
        self.recording_runtime_status = tk.StringVar(
            value="Данные: Orange Pi → USB → STM32 → Intan. При стимуляции/reinit возможны короткие паузы STREAM."
        )
        
        # Конструктор паттернов
        self.pattern_blocks = []  # Список блоков паттерна

        # Данные последнего измерения импеданса (для экспорта точек)
        self._last_impedance_data = None
        self.phase_test_results = []

        self._create_widgets()

    def _clear_udp_control_messages(self):
        try:
            while True:
                self.udp_control_messages.get_nowait()
        except queue.Empty:
            return

    def _push_udp_control_message(self, message):
        try:
            self.udp_control_messages.put_nowait(message)
        except Exception:
            pass

    def _wait_for_udp_control_message(self, expected_message, timeout_s=2.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            try:
                message = self.udp_control_messages.get(timeout=remaining)
            except queue.Empty:
                return None
            if message == expected_message:
                return message
        return None

    def _note_recording_gap_event(self, event_name, operation=""):
        ts = time.time()
        self.recording_gap_events.append((ts, event_name, operation))
        operation_suffix = f" ({operation})" if operation else ""
        if event_name == "stim_started":
            self.recording_runtime_status.set(
                "Идет стимуляция во время регистрации: возможна краткая деградация/пауза данных."
            )
            self.log(f"Статус Intan: началась стимуляция{operation_suffix}. Возможна краткая пауза регистрации.", "warning")
        elif event_name == "recording_reinit_started":
            self.recording_runtime_status.set(
                "Intan возвращается в recording mode после стимуляции. Возможны пропуски данных."
            )
            self.log(f"Статус Intan: начато восстановление recording mode{operation_suffix}.", "warning")
        elif event_name == "recording_reinit_done":
            self.recording_runtime_status.set(
                "Recording mode восстановлен после стимуляции."
            )
            self.log(f"Статус Intan: recording mode восстановлен{operation_suffix}.", "success")
        elif event_name == "stim_finished":
            self.recording_runtime_status.set(
                "Стимуляция завершена; поток регистрации продолжает работу."
            )
            self.log(f"Статус Intan: стимуляция завершена{operation_suffix}.", "info")

    def _parse_recording_channels(self, channels_str):
        channels = []
        for part in channels_str.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = map(int, part.split("-", 1))
                channels.extend(range(start, end + 1))
            else:
                channels.append(int(part))
        return sorted(set(channels))

    def _count_values_in_packet(self, binary_data):
        total_values = 0
        total_samples = 0

        if len(binary_data) < 4:
            return total_values, total_samples

        # Формат v4 (компактный, каналы из recording_channel_list):
        # magic:u32 + seq:u32 + channel_count:u16 + sample_count:u16 + reserved:u32 + values[]
        if len(binary_data) >= 16:
            try:
                magic, _, channel_count, sample_count = struct.unpack_from("<IIHH", binary_data, 0)
            except struct.error:
                magic = 0
                channel_count = 0
                sample_count = 0
            if magic == 0x344E5449 and len(binary_data) >= 16:
                value_count = channel_count * sample_count
                if len(binary_data) >= 16 + value_count * 2:
                    return value_count, sample_count

        # Формат v3:
        # magic:u32 + version:u16 + header_size:u16 + seq:u32 + timestamp_ns:u64
        # + channel_count:u16 + sample_count:u16 + flags:u16 + reserved:u16
        # + channels[16] + values[sample_count * channel_count]:u16
        if len(binary_data) >= 44:
            try:
                magic, version, header_size, _, _, channel_count, sample_count, _, _ = struct.unpack_from(
                    '<IHHIQHHHH',
                    binary_data,
                    0,
                )
            except struct.error:
                magic = version = 0
                header_size = 0
                channel_count = 0
                sample_count = 0
            if magic == 0x334E5449 and version == 3:
                if header_size < 44 or len(binary_data) < header_size:
                    return 0, 0
                value_count = channel_count * sample_count
                if len(binary_data) < header_size + (value_count * 2):
                    return 0, 0
                return value_count, sample_count

        # Формат v2:
        # [ver=2][sample_count:u32] + samples
        # sample = timestamp:f64 + pipeline_skip:u16 + ch_count:u16 + ch_list:u8[ch_count] + raw_count:u16 + raw:u16[raw_count]
        if len(binary_data) >= 5 and binary_data[0] == 2:
            try:
                sample_count = struct.unpack_from('<I', binary_data, 1)[0]
            except struct.error:
                return 0, 0

            offset = 5
            for _ in range(sample_count):
                if offset + 12 > len(binary_data):
                    break
                try:
                    _, pipeline_skip, channel_count = struct.unpack_from('<dHH', binary_data, offset)
                except struct.error:
                    break
                offset += 12

                if offset + channel_count > len(binary_data):
                    break
                offset += channel_count

                if offset + 2 > len(binary_data):
                    break
                try:
                    raw_count = struct.unpack_from('<H', binary_data, offset)[0]
                except struct.error:
                    break
                offset += 2

                raw_bytes_len = raw_count * 2
                if offset + raw_bytes_len > len(binary_data):
                    break
                offset += raw_bytes_len

                total_values += min(channel_count, max(0, raw_count - pipeline_skip))
                total_samples += 1

            return total_values, total_samples

        try:
            sample_count = struct.unpack('I', binary_data[0:4])[0]
        except struct.error:
            return 0, 0

        # Старый формат: несколько samples в одном пакете.
        if 0 < sample_count <= 100:
            offset = 4
            for _ in range(sample_count):
                if offset + 12 > len(binary_data):
                    break
                try:
                    _timestamp = struct.unpack('d', binary_data[offset:offset+8])[0]
                    offset += 8
                    channel_count = struct.unpack('I', binary_data[offset:offset+4])[0]
                    offset += 4
                except struct.error:
                    break

                if offset + channel_count * 6 > len(binary_data):
                    break
                offset += channel_count * 6
                total_values += channel_count
                total_samples += 1

            return total_values, total_samples

        # Очень старый формат: один sample в пакете.
        if len(binary_data) >= 12:
            try:
                channel_count = struct.unpack('I', binary_data[8:12])[0]
            except struct.error:
                return 0, 0
            if 0 < channel_count <= 16:
                return channel_count, 1

        return 0, 0

    def _finalize_recording_stop(self, source="server"):
        if self.recording_stop_processed:
            return

        self.recording_stop_processed = True
        self.recording_active = False
        self.btn_start_recording.configure(state="normal" if self.udp_registered else "disabled")
        self.btn_stop_recording.configure(state="disabled")

        elapsed = 0.0
        if self.recording_receive_started_at is not None:
            elapsed = max(0.0, time.perf_counter() - self.recording_receive_started_at)
        values_per_second = (self.recording_values_received / elapsed) if elapsed > 0 else 0.0

        if len(self.recording_hex_data) > 0:
            self.log(
                f"Регистрация завершена ({source}). Начинаем автоматическую обработку {len(self.recording_hex_data)} пакетов...",
                "info",
            )
            self.recording_stats_label.config(
                text=(
                    f"Обработка {len(self.recording_hex_data)} пакетов | "
                    f"Значений: {self.recording_values_received} | "
                    f"Значений/с: {values_per_second:.1f}"
                )
            )
            if self.recording_gap_events:
                self.recording_runtime_status.set(
                    f"Регистрация завершена; отмечено окон переключения режима: {len(self.recording_gap_events)}."
                )
            else:
                self.recording_runtime_status.set("Регистрация завершена без отмеченных окон переключения режима.")
            threading.Thread(target=self._parse_hex_data_thread, daemon=True).start()
        else:
            self.log(f"Регистрация завершена ({source}), данных для обработки нет", "warning")
            self.recording_stats_label.config(
                text="Получено пакетов: 0 | Нет данных"
            )
            self.recording_runtime_status.set("Регистрация завершена без сохраненных данных.")

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

        ttk.Label(
            conn_inner,
            text="Сервер на Pi: intan_server.py --backend usb  |  STM32 0483:5741",
            style="Status.TLabel",
        ).grid(row=1, column=0, columnspan=6, sticky="w", padx=pad_small, pady=(0, pad_small))

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

        # Вкладка 2: Паттерн команд (STM32 PATTERN_ADD_RAW / DELAY_US)
        tab_pattern = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_pattern, text="🎯 Паттерн")

        # Вкладка 5: Справка по регистрам
        tab_registers = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_registers, text="📚 Справка")

        # Вкладка 6: Регистрация данных
        tab_recording = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_recording, text="📊 Регистрация")

        # Вкладка 6: Измерения (импеданс)
        tab_measurements = ttk.Frame(notebook, padding=pad)
        notebook.add(tab_measurements, text="⚡ Измерения")

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

        # ========== ВКЛАДКА 2: ПАТТЕРН ==========
        self.var_pattern_example_channel = tk.StringVar(value="0")
        self.var_pattern_example_current = tk.StringVar(value="180")

        example_params_frame = ttk.LabelFrame(tab_pattern, text="📄 Параметры примера", padding=pad)
        example_params_frame.pack(fill="x", pady=(0, pad))

        ttk.Label(example_params_frame, text="Канал (0–15):").grid(
            row=0, column=0, sticky="w", padx=pad_small, pady=pad_small
        )
        tk.Spinbox(
            example_params_frame, textvariable=self.var_pattern_example_channel,
            from_=0, to=15, width=6,
        ).grid(row=0, column=1, sticky="w", padx=pad_small, pady=pad_small)

        ttk.Label(example_params_frame, text="Ток (µA):").grid(
            row=0, column=2, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Entry(example_params_frame, textvariable=self.var_pattern_example_current, width=8).grid(
            row=0, column=3, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(
            example_params_frame,
            text="(для кнопок «Пример» в конструкторе и текстовом редакторе)",
            font=('Arial', 8), foreground='gray',
        ).grid(row=0, column=4, sticky="w", padx=pad_small, pady=pad_small)

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
        
        self.btn_add_pulse_duration = ttk.Button(
            palette_buttons_frame, text="⏱ Длительность импульса",
            command=lambda: self.add_pattern_block("pulse_duration"), width=22
        )
        self.btn_add_pulse_duration.pack(fill="x", padx=pad_small, pady=pad_small)
        
        self.btn_add_inter_pulse_delay = ttk.Button(
            palette_buttons_frame, text="⏸ Пауза между импульсами",
            command=lambda: self.add_pattern_block("inter_pulse_delay"), width=22
        )
        self.btn_add_inter_pulse_delay.pack(fill="x", padx=pad_small, pady=pad_small)
        
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

        help_text = """STM32 intan_pattern.c: ON → длит. импульса → OFF → пауза.
Импульс: Включить → Длительность (µs) → Выключить → Пауза (µs).
Паузу после последнего OFF не добавляйте. Блоки → Сгенерировать → Загрузить → Запустить."""
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

        self.txt_pattern.insert("1.0", "# Используйте «Пример» или соберите паттерн в конструкторе блоков\n")

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

        
        # Создаем панель с поиском и информацией о регистрах
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
        filter_frame.columnconfigure(3, weight=1)

        _fh = (
            "Подпись для вас и для лога: ориентировочная верхняя частота среза аналогового сглаживающего (нижний частотный) фильтра, "
            "которую вы хотите получить при текущих R4/R5. Важно: TCP‑сервер и чип не пересчитывают fH из этого поля — в RHS2116 "
            "записываются только Register 4 и 5. Если меняете R4/R5 по даташиту, обновите fH здесь вручную, чтобы значения в интерфейсе "
            "и в ответе сервера совпадали с реальной настройкой."
        )
        _r4 = (
            "Register 4 (16 бит): часть слова настройки аналогового LPF (верхняя граница полосы вместе с R5). Именно это значение "
            "уходит в SPI как регистр 4. Смысл полей битов — в даташите RHS2116; GUI не выводит частоту из hex автоматически."
        )
        _r5 = (
            "Register 5 (16 бит): вторая часть настройки того же аналогового LPF (пара к R4). Записывается в чип как регистр 5."
        )
        _fl = (
            "Подпись для вас и для лога: ориентировочная нижняя частота среза аналогового ВЧ‑фильтра при текущих R6/R7. Сервер не "
            "пересчитывает fL в регистры — в чип пишутся только Register 6 и 7. Обновляйте fL вручную при смене R6/R7 по даташиту."
        )
        _r6 = (
            "Register 6 (16 бит): часть настройки аналогового HPF (нижняя граница полосы вместе с R7). Уходит в SPI как регистр 6."
        )
        _r7 = (
            "Register 7 (16 бит): вторая часть настройки аналогового HPF (пара к R6). Уходит в SPI как регистр 7."
        )
        _dsp = (
            "Индекс цифрового сглаживающего ВЧ‑фильтра (DSP HPF) RHS2116, допустимые значения 0…15 (см. даташит). В этом клиенте "
            "поле связано с Register 1: при «Применить фильтры», если младшие 4 бита R1 не совпадают с этим числом, клиент "
            "включает DSPen (bit 4), записывает cutoff в bits 3:0 и не меняет биты R1 выше 4. Если младшие 4 бита уже совпадают "
            "с полем, содержимое R1 (в том числе DSPen) не изменяется автоматически."
        )
        _r1 = (
            "Register 1 (16 бит): полное значение, отправляемое в чип. Младшие биты относятся к DSP (bit 4 — включение DSP HPF, "
            "bits 3:0 — выбранный cutoff; также есть другие функции R1, например absmode в bit 5 — по даташиту). Остальные биты "
            "должны соответствовать вашему профилю записи; при рассогласовании с полем DSP cutoff клиент поправит только маску 0x001F."
        )

        def _filter_help(text: str) -> ttk.Label:
            return ttk.Label(
                filter_frame,
                text=text,
                wraplength=720,
                justify="left",
                font=("Arial", 8),
                foreground="gray",
            )

        row = 0
        ttk.Label(filter_frame, text="fH (справочно, Hz):").grid(row=row, column=0, sticky="nw", padx=pad_small, pady=pad_small)
        self.var_fh_freq = tk.StringVar(value="7500")
        ttk.Entry(filter_frame, textvariable=self.var_fh_freq, width=12).grid(
            row=row, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(filter_frame, text="Hz").grid(row=row, column=2, sticky="nw", padx=(0, pad_small), pady=pad_small)
        _filter_help(_fh).grid(row=row, column=3, sticky="nw", padx=pad_small, pady=pad_small)
        row += 1

        ttk.Label(filter_frame, text="Register 4:").grid(row=row, column=0, sticky="nw", padx=pad_small, pady=pad_small)
        self.var_reg4 = tk.StringVar(value="0x0016")
        ttk.Entry(filter_frame, textvariable=self.var_reg4, width=12).grid(
            row=row, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(filter_frame, text="hex").grid(row=row, column=2, sticky="nw", padx=(0, pad_small), pady=pad_small)
        _filter_help(_r4).grid(row=row, column=3, sticky="nw", padx=pad_small, pady=pad_small)
        row += 1

        ttk.Label(filter_frame, text="Register 5:").grid(row=row, column=0, sticky="nw", padx=pad_small, pady=pad_small)
        self.var_reg5 = tk.StringVar(value="0x0017")
        ttk.Entry(filter_frame, textvariable=self.var_reg5, width=12).grid(
            row=row, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(filter_frame, text="hex").grid(row=row, column=2, sticky="nw", padx=(0, pad_small), pady=pad_small)
        _filter_help(_r5).grid(row=row, column=3, sticky="nw", padx=pad_small, pady=pad_small)
        row += 1

        ttk.Label(filter_frame, text="fL (справочно, Hz):").grid(row=row, column=0, sticky="nw", padx=pad_small, pady=pad_small)
        self.var_fl_freq = tk.StringVar(value="5")
        ttk.Entry(filter_frame, textvariable=self.var_fl_freq, width=12).grid(
            row=row, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(filter_frame, text="Hz").grid(row=row, column=2, sticky="nw", padx=(0, pad_small), pady=pad_small)
        _filter_help(_fl).grid(row=row, column=3, sticky="nw", padx=pad_small, pady=pad_small)
        row += 1

        ttk.Label(filter_frame, text="Register 6:").grid(row=row, column=0, sticky="nw", padx=pad_small, pady=pad_small)
        self.var_reg6 = tk.StringVar(value="0x00A8")
        ttk.Entry(filter_frame, textvariable=self.var_reg6, width=12).grid(
            row=row, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(filter_frame, text="hex").grid(row=row, column=2, sticky="nw", padx=(0, pad_small), pady=pad_small)
        _filter_help(_r6).grid(row=row, column=3, sticky="nw", padx=pad_small, pady=pad_small)
        row += 1

        ttk.Label(filter_frame, text="Register 7:").grid(row=row, column=0, sticky="nw", padx=pad_small, pady=pad_small)
        self.var_reg7 = tk.StringVar(value="0x000A")
        ttk.Entry(filter_frame, textvariable=self.var_reg7, width=12).grid(
            row=row, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(filter_frame, text="hex").grid(row=row, column=2, sticky="nw", padx=(0, pad_small), pady=pad_small)
        _filter_help(_r7).grid(row=row, column=3, sticky="nw", padx=pad_small, pady=pad_small)
        row += 1

        ttk.Label(filter_frame, text="DSP HPF cutoff:").grid(row=row, column=0, sticky="nw", padx=pad_small, pady=pad_small)
        self.var_dsp_cutoff = tk.StringVar(value="0")
        ttk.Entry(filter_frame, textvariable=self.var_dsp_cutoff, width=12).grid(
            row=row, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(filter_frame, text="0…15").grid(row=row, column=2, sticky="nw", padx=(0, pad_small), pady=pad_small)
        _filter_help(_dsp).grid(row=row, column=3, sticky="nw", padx=pad_small, pady=pad_small)
        row += 1

        ttk.Label(filter_frame, text="Register 1:").grid(row=row, column=0, sticky="nw", padx=pad_small, pady=pad_small)
        self.var_reg1 = tk.StringVar(value="0x051A")
        ttk.Entry(filter_frame, textvariable=self.var_reg1, width=12).grid(
            row=row, column=1, sticky="w", padx=pad_small, pady=pad_small
        )
        ttk.Label(filter_frame, text="hex").grid(row=row, column=2, sticky="nw", padx=(0, pad_small), pady=pad_small)
        _filter_help(_r1).grid(row=row, column=3, sticky="nw", padx=pad_small, pady=pad_small)
        row += 1

        btn_row = ttk.Frame(filter_frame)
        btn_row.grid(row=row, column=0, columnspan=4, sticky="w", padx=pad_small, pady=pad_small)
        self.btn_auto_filters_wideband = ttk.Button(
            btn_row, text="Авто (широкополосно)", command=self.on_auto_filters_wideband, width=22
        )
        self.btn_auto_filters_wideband.pack(side="left", padx=(0, pad_small))
        self.btn_apply_filters = ttk.Button(
            btn_row,
            text="Применить фильтры",
            command=self.on_apply_filters,
            style="Primary.TButton",
            width=22,
        )
        self.btn_apply_filters.pack(side="left", padx=pad_small)

        filter_hint = ttk.Label(
            filter_frame,
            text="Пресет «Авто»: fH=7500 Hz, fL=5 Hz, R4–R7 и R1 как в типовой широкополосной записи; fH/fL не записываются в чип отдельно.",
            font=("Arial", 8),
            foreground="gray",
            wraplength=720,
            justify="left",
        )
        filter_hint.grid(row=row + 1, column=0, columnspan=4, sticky="w", padx=pad_small, pady=(0, pad_small))

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

        self.recording_runtime_label = ttk.Label(
            recording_data_frame,
            textvariable=self.recording_runtime_status,
            style='Status.TLabel',
            wraplength=900,
            justify="left",
        )
        self.recording_runtime_label.pack(fill="x", pady=(0, pad_small))

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

        self.btn_open_graph_window = ttk.Button(
            graph_btn_frame, text="📊 Открыть график",
            command=self.open_recording_graph_window, width=18
        )
        self.btn_open_graph_window.pack(side="left", padx=pad_small)
        if not MATPLOTLIB_AVAILABLE:
            self.btn_open_graph_window.configure(state="disabled")

        graph_info_text = (
            "График и спектр открываются в отдельном окне — кнопка «📊 Открыть график» "
            "или автоматически после «🔍 Построить график»."
            if MATPLOTLIB_AVAILABLE
            else "Для графиков установите matplotlib: pip install matplotlib"
        )
        graph_info = ttk.Label(
            recording_data_frame,
            text=graph_info_text,
            style='Status.TLabel',
            wraplength=900,
            justify="left",
        )
        graph_info.pack(fill="x", pady=(0, pad_small))

        # Текстовое поле для отображения данных
        self.recording_data_text = scrolledtext.ScrolledText(
            recording_data_frame, height=15, state="disabled", wrap=tk.WORD,
            font=('Consolas', 9), bg='#f8f8f8', fg='#333333'
        )
        self.recording_data_text.pack(fill="both", expand=True)

        # ========== ВКЛАДКА 6: ИЗМЕРЕНИЯ (ИМПЕДАНС) ==========
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
        tk.Spinbox(impedance_frame, textvariable=self.var_impedance_averages, from_=1, to=1000, width=6).grid(
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
        self.btn_measure_all_impedance = ttk.Button(
            impedance_frame, text="📚 Измерить все каналы",
            command=self.on_measure_all_impedances, state="disabled", width=20
        )
        self.btn_measure_all_impedance.grid(row=1, column=6, columnspan=2, sticky="w", padx=pad_small, pady=pad_small)
        self.btn_export_impedance = ttk.Button(
            impedance_frame, text="💾 Экспорт точек",
            command=self.on_export_impedance_points, state="disabled", width=16
        )
        self.btn_export_impedance.grid(row=1, column=4, columnspan=2, sticky="w", padx=pad_small, pady=pad_small)

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
        self.btn_stop.configure(state=state)
        self.btn_pattern_load.configure(state=state)
        self.btn_pattern_run.configure(state=state)
        self.btn_measure_impedance.configure(state=state)
        self.btn_measure_all_impedance.configure(state=state)
        self.btn_export_impedance.configure(state=state)
        self.btn_check_intan.configure(state=state)
        self.btn_apply_adc_bias.configure(state=state)
        self.btn_apply_filters.configure(state=state)
        self.btn_auto_filters_wideband.configure(state=state)
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

    def _send_async(self, cmd: dict, description: str, timeout: Optional[float] = None):
        def worker():
            try:
                tout = timeout if timeout is not None else IntanTcpClient.timeout_for_command(cmd)
                self.log(
                    f"Отправка ({tout:.0f}s max): {json.dumps(cmd, ensure_ascii=False)}",
                    "info",
                )
                resp = self.client.send_command(cmd, timeout=tout)
                self.log(f"Ответ: {json.dumps(resp, indent=2, ensure_ascii=False)}", "success")
            except Exception as e:
                self.log(f"Ошибка команды '{description}': {e}", "error")
                messagebox.showerror("Ошибка", f"{description}: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_ping(self):
        self._send_async({"cmd": "ping"}, "ping")

    def on_init(self):
        self.log(
            "Полная инициализация стимуляции на STM32 (может занять до 2 мин)...",
            "info",
        )
        self._send_async({"cmd": "init"}, "init", timeout=TCP_TIMEOUT_INIT)

    def on_check_intan(self):
        """Проверяет Intan по chip ID (reg 255) через USB-сопроцессор на сервере."""
        def worker():
            try:
                self.log(
                    "Проверка Intan: read_register 255 (USB STM32, без полного init)...",
                    "info",
                )
                max_attempts = 5
                success = False
                last_exc = ""
                cmd = {"cmd": "read_register", "address": INTAN_CHIP_ID_REG}
                tout = TCP_TIMEOUT_READ_REG

                for attempt in range(1, max_attempts + 1):
                    try:
                        resp = self.client.send_command(cmd, timeout=tout)
                        last_exc = ""

                        if resp.get("status") == "ok":
                            value = resp.get("value", 0)
                            if isinstance(value, str):
                                if value.startswith(("0x", "0X")):
                                    value = int(value, 16)
                                else:
                                    value = int(value)

                            self.log(
                                f"Попытка {attempt}/{max_attempts}: reg {INTAN_CHIP_ID_REG} = "
                                f"{value} (0x{value:04X})",
                                "info",
                            )

                            if value == INTAN_CHIP_ID_EXPECTED:
                                success = True
                                self.after(
                                    0,
                                    lambda v=value, a=attempt: messagebox.showinfo(
                                        "Успех",
                                        f"Intan RHS2116 (через STM32 USB)\n"
                                        f"Регистр {INTAN_CHIP_ID_REG} = {v} (0x{v:04X})\n"
                                        f"Попытка {a}/{max_attempts}",
                                    ),
                                )
                                self.after(
                                    0,
                                    lambda v=value: self.log(
                                        f"✓ Intan обнаружен (chip ID 0x{v:04X})", "success"
                                    ),
                                )
                                break
                            if value == 65535:
                                self.log(
                                    "65535 — нет ответа SPI; проверьте intan_server --backend usb "
                                    "и кабель USB3300",
                                    "warning",
                                )
                        else:
                            error_msg = resp.get("error", "Неизвестная ошибка")
                            self.log(
                                f"Попытка {attempt}/{max_attempts}: {error_msg}",
                                "warning",
                            )

                        if attempt < max_attempts:
                            time.sleep(1.0 if "timed out" in last_exc.lower() else 0.5)

                    except Exception as e:
                        last_exc = str(e)
                        self.log(
                            f"Попытка {attempt}/{max_attempts}: {e}",
                            "warning",
                        )
                        if "timed out" in last_exc.lower():
                            self.log(
                                "Таймаут: возможно идёт UDP STREAM — остановите запись и повторите",
                                "info",
                            )
                        if attempt < max_attempts:
                            time.sleep(1.5)

                if not success:
                    self.after(
                        0,
                        lambda: messagebox.showerror(
                            "Ошибка",
                            f"Intan не обнаружен за {max_attempts} попыток.\n"
                            f"Ожидается reg {INTAN_CHIP_ID_REG} = {INTAN_CHIP_ID_EXPECTED} (0x0020).\n\n"
                            "Проверьте:\n"
                            "• systemctl status intan-server (должен быть --backend usb)\n"
                            "• lsusb | grep 0483:5741\n"
                            "• кабель USB3300 к Orange Pi",
                        ),
                    )
                    self.after(
                        0,
                        lambda: self.log("✗ Проверка Intan не удалась", "error"),
                    )
                    
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
        if self.recording_active:
            self.log(
                "Стимуляция запущена во время активной регистрации. Сервер временно переключит Intan в stimulation mode, затем вернет recording mode.",
                "warning",
            )
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

    STM32_PATTERN_MAX_SLOTS = 1024

    @classmethod
    def _pattern_lines_for_load(cls, prep_lines, pattern_cmd_lines):
        """Команды для pattern_load (прошивка STM32, без meta-команд)."""
        skip = {
            "INIT_STIM", "CLEAR_COMP", "PATTERN_RUN", "READ",
            "PATTERN_CLEAR", "PATTERN_STATUS",
        }
        out = []
        for ln in prep_lines + pattern_cmd_lines:
            cmd = ln.split("#", 1)[0].strip()
            if not cmd:
                continue
            parts = cmd.split()
            cmd_type = parts[0].upper()
            if cmd_type in skip:
                continue
            if cmd_type == "DELAY_US" and len(parts) >= 2:
                out.append(f"PATTERN_ADD_DELAY_US {parts[1]}")
                continue
            if cmd_type == "WRITE" and len(parts) >= 3:
                out.append(cmd)
                continue
            if cmd_type in (
                "PATTERN_ADD_RAW", "PATTERN_ADD_DELAY_US", "PATTERN_ADD_DELAY_CYC",
                "DELAY",
            ):
                out.append(cmd)
        return out

    @classmethod
    def estimate_stm32_pattern_slots(cls, load_lines, include_server_safety_off=True):
        """Оценка слотов RAM по правилам intan_pattern.c."""
        slots = spi = delays = 0
        triple_spi = {
            "PATTERN_ADD_WRITE", "PATTERN_ADD_READ", "PATTERN_ADD_CONVERT",
            "PATTERN_ADD_CLEAR_ADC", "PATTERN_ADD_CLEAR_COMP", "READ", "CLEAR",
        }
        for ln in load_lines:
            parts = ln.split("#", 1)[0].strip().split()
            if not parts:
                continue
            cmd_type = parts[0].upper()
            if cmd_type in ("PATTERN_ADD_RAW", "WRITE"):
                slots += 1
                spi += 1
            elif cmd_type in ("PATTERN_ADD_DELAY_US", "DELAY_US", "DELAY", "PATTERN_ADD_DELAY_CYC"):
                slots += 1
                delays += 1
            elif cmd_type in triple_spi:
                slots += 3
                spi += 3
        if include_server_safety_off:
            slots += 3
            spi += 3
        return slots, spi, delays

    def _pattern_load_sync(self, prep_lines, pattern_cmd_lines):
        """Загрузка паттерна в STM32 (pattern_load), без send_line INIT_STIM."""
        pattern_load_lines = self._pattern_lines_for_load(prep_lines, pattern_cmd_lines)
        if not pattern_load_lines:
            raise ValueError("Нет команд для pattern_load")
        resp = self.client.send_command({"cmd": "pattern_load", "commands": pattern_load_lines})
        IntanTcpClient.require_ok(resp, "pattern_load")
        count = resp.get("commands_count")
        if count is None:
            raise RuntimeError("pattern_load: сервер не вернул commands_count")
        return count

    def _pattern_run_sync(self, repeat_count):
        """PATTERN_RUN — unlock R32/R33 и safe OFF выполняет сервер."""
        resp = self.client.send_command(
            {"cmd": "pattern_run", "repeat_count": repeat_count},
            timeout=max(30.0, repeat_count * 0.5),
        )
        return IntanTcpClient.require_ok(resp, "pattern_run")

    def _get_pattern_lines_from_editor(self):
        """Строки паттерна из текстового редактора (или из блоков конструктора)."""
        pattern_text = self.txt_pattern.get("1.0", tk.END)
        pattern_lines = [line.strip() for line in pattern_text.split("\n") if line.strip()]
        if not pattern_lines and self.pattern_blocks:
            self.generate_pattern_from_blocks()
            pattern_text = self.txt_pattern.get("1.0", tk.END)
            pattern_lines = [line.strip() for line in pattern_text.split("\n") if line.strip()]
        return pattern_lines

    def on_pattern_load(self):
        """Загрузка паттерна в STM32 (pattern_load): токи + PATTERN_ADD_*."""
        pattern_lines = self._get_pattern_lines_from_editor()
        if not pattern_lines:
            messagebox.showerror("Ошибка", "Паттерн пуст. Добавьте блоки или введите команды.")
            return

        prep_lines, pattern_cmd_lines, _post = self._parse_pattern_script(pattern_lines)
        load_lines = self._pattern_lines_for_load(prep_lines, pattern_cmd_lines)
        if not load_lines and self.pattern_blocks:
            self.generate_pattern_from_blocks()
            pattern_lines = self._get_pattern_lines_from_editor()
            prep_lines, pattern_cmd_lines, _post = self._parse_pattern_script(pattern_lines)
            load_lines = self._pattern_lines_for_load(prep_lines, pattern_cmd_lines)
        if not load_lines:
            messagebox.showerror(
                "Ошибка",
                "Нет команд для pattern_load (PATTERN_ADD_RAW / PATTERN_ADD_DELAY_US). "
                "Соберите паттерн в конструкторе и нажмите «Сгенерировать».",
            )
            return
        slot_est, spi_est, delay_est = self.estimate_stm32_pattern_slots(load_lines)
        if slot_est > self.STM32_PATTERN_MAX_SLOTS:
            messagebox.showerror(
                "Ошибка",
                f"Паттерн не помещается в RAM STM32: ~{slot_est} слотов "
                f"(максимум {self.STM32_PATTERN_MAX_SLOTS}).",
            )
            return

        def worker():
            try:
                self.log("Загрузка паттерна в STM32 (pattern_load)...", "info")
                count = self._pattern_load_sync(prep_lines, pattern_cmd_lines)
                self.log(
                    f"✓ Паттерн загружен: {count} слотов в STM32 "
                    f"(оценка ~{slot_est}: spi≈{spi_est}, delays≈{delay_est})",
                    "success",
                )
                self.pattern_status_label.config(
                    text=f"● Паттерн загружен (~{slot_est} слотов)",
                    style="Success.TLabel",
                )
                self.btn_pattern_run.configure(state="normal")
            except Exception as e:
                self.log(f"Ошибка загрузки паттерна: {e}", "error")
                self.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось загрузить паттерн: {e}"))
                self.pattern_status_label.config(text="● Ошибка загрузки", style="Error.TLabel")

        threading.Thread(target=worker, daemon=True).start()

    def on_pattern_run(self):
        """PATTERN_RUN — unlock стима и safe OFF на сервере."""
        try:
            repeat_count = int(self.var_pattern_repeat.get().strip())
        except ValueError:
            messagebox.showerror("Ошибка", "Количество повторений должно быть числом")
            return
        if repeat_count < 1 or repeat_count > 10000:
            messagebox.showerror("Ошибка", "Количество повторений должно быть от 1 до 10000")
            return

        if self.recording_active:
            self.log(
                "Паттерн запускается во время регистрации. Возможны короткие окна потери данных.",
                "warning",
            )

        def worker():
            try:
                self.log(f"PATTERN_RUN x{repeat_count}...", "info")
                resp = self._pattern_run_sync(repeat_count)
                self.log(f"✓ PATTERN_RUN x{repeat_count}: ok", "success")
            except Exception as e:
                self.log(f"Ошибка запуска паттерна: {e}", "error")
                self.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось запустить паттерн: {e}"))

        threading.Thread(target=worker, daemon=True).start()

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
        """Импортирует паттерн из текстового файла в редактор."""
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
        if not hasattr(self, "txt_pattern"):
            return []
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
            elif cmd_type == "DELAY_US":
                if len(parts) >= 2:
                    try:
                        delay_us = int(parts[1], 0)
                        commands.append({"type": "DELAY_US", "line": i+1, "delay_us": delay_us})
                    except:
                        commands.append({"type": "error", "line": i+1, "text": line})
            elif cmd_type == "DELAY":
                if len(parts) >= 2:
                    try:
                        count = int(parts[1], 0)
                        commands.append({"type": "DELAY", "line": i+1, "count": count, "legacy": True})
                    except:
                        commands.append({"type": "error", "line": i+1, "text": line})
            elif cmd_type == "PATTERN_ADD_RAW":
                if len(parts) >= 2:
                    try:
                        word = int(parts[1], 0)
                        commands.append({"type": "PATTERN_ADD_RAW", "line": i+1, "word": word})
                    except:
                        commands.append({"type": "error", "line": i+1, "text": line})
            elif cmd_type == "PATTERN_ADD_DELAY_US":
                if len(parts) >= 2:
                    try:
                        delay_us = int(parts[1], 0)
                        commands.append({"type": "PATTERN_ADD_DELAY_US", "line": i+1, "delay_us": delay_us})
                    except:
                        commands.append({"type": "error", "line": i+1, "text": line})
            elif cmd_type in ("PATTERN_CLEAR", "PATTERN_STATUS", "PATTERN_RUN", "INIT_STIM", "CLEAR_COMP"):
                commands.append({"type": "meta", "line": i+1, "cmd": cmd_type})
            else:
                commands.append({"type": "error", "line": i+1, "text": line})
        
        return commands

    def update_pattern_visualization(self):
        """Обновляет визуализацию паттерна"""
        if not hasattr(self, "pattern_viz_canvas"):
            return
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
            "PATTERN_ADD_RAW": "#2E7D32",
            "PATTERN_ADD_DELAY_US": "#9C27B0",
            "READ": "#2196F3",
            "CLEAR": "#FF9800",
            "DELAY": "#9C27B0",
            "DELAY_US": "#9C27B0",
            "meta": "#607D8B",
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
            elif cmd_type == "DELAY_US":
                text = f"DELAY_US {cmd['delay_us']} µs"
            elif cmd_type == "DELAY":
                text = f"DELAY {cmd['count']} (устар., ~{cmd['count'] * 1280 // 1000} µs)"
            elif cmd_type == "PATTERN_ADD_RAW":
                text = f"RAW 0x{cmd['word']:08X}"
            elif cmd_type == "PATTERN_ADD_DELAY_US":
                text = f"DELAY_US {cmd['delay_us']} µs"
            elif cmd_type == "meta":
                text = cmd.get("cmd", "meta")
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
        if not hasattr(self, "hints_text"):
            return
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

• Импульс: ON → длительность (µs) → OFF → пауза (µs)

• PATTERN_ADD_RAW — 1 SPI-слот; PATTERN_ADD_DELAY_US — пауза

• Паузу после последнего OFF не добавляйте (intan_stim_pattern_guide.md)

• pattern_load → pattern_run (repeat 1–10000, max 1024 слотов)

Начните вводить паттерн, и здесь появятся подсказки!"""
        else:
            hints = "💡 Анализ паттерна:\n\n"
            
            # Статистика
            write_count = sum(1 for c in commands if c.get("type") == "WRITE")
            read_count = sum(1 for c in commands if c.get("type") == "READ")
            clear_count = sum(1 for c in commands if c.get("type") == "CLEAR")
            delay_us_count = sum(1 for c in commands if c.get("type") == "DELAY_US")
            delay_legacy_count = sum(1 for c in commands if c.get("type") == "DELAY")
            error_count = sum(1 for c in commands if c.get("type") == "error")
            comment_count = sum(1 for c in commands if c.get("type") == "comment")
            raw_count = sum(1 for c in commands if c.get("type") == "PATTERN_ADD_RAW")
            delay_raw_count = sum(1 for c in commands if c.get("type") == "PATTERN_ADD_DELAY_US")
            load_lines = []
            for c in commands:
                if c.get("type") == "PATTERN_ADD_RAW":
                    load_lines.append(f"PATTERN_ADD_RAW 0x{c['word']:08X}")
                elif c.get("type") == "PATTERN_ADD_DELAY_US":
                    load_lines.append(f"PATTERN_ADD_DELAY_US {c['delay_us']}")
                elif c.get("type") == "WRITE":
                    u = " U" if c.get("u") else ""
                    m = " M" if c.get("m") else ""
                    load_lines.append(f"WRITE {c['reg']} 0x{c['value']:04X}{u}{m}")
                elif c.get("type") == "DELAY_US":
                    load_lines.append(f"PATTERN_ADD_DELAY_US {c['delay_us']}")
            slots_est, spi_est, delays_est = self.estimate_stm32_pattern_slots(load_lines)
            raw_42_on = sum(
                1 for c in commands
                if c.get("type") == "PATTERN_ADD_RAW"
                and (c.get("word", 0) & 0xFFFF0000) == 0xA02A0000
                and (c.get("word", 0) & 0xFFFF) != 0
            )
            
            hints += f"📊 Статистика (STM32 intan_pattern.c):\n"
            hints += f"  • Команд в редакторе: {len(commands)}\n"
            if raw_count > 0:
                hints += f"  • PATTERN_ADD_RAW: {raw_count}\n"
            if delay_raw_count > 0:
                hints += f"  • PATTERN_ADD_DELAY_US: {delay_raw_count}\n"
            if write_count > 0:
                hints += f"  • WRITE (→ RAW на сервере): {write_count}\n"
            if delay_us_count > 0:
                hints += f"  • DELAY_US (legacy): {delay_us_count}\n"
            if delay_legacy_count > 0:
                hints += f"  • DELAY (устар.): {delay_legacy_count}\n"
            if raw_42_on > 0:
                hints += f"  • Импульсов (оценка): {raw_42_on}\n"
            hints += f"  • Слотов≈{slots_est} (spi≈{spi_est}, delays≈{delays_est}, max {self.STM32_PATTERN_MAX_SLOTS})\n"
            if slots_est > self.STM32_PATTERN_MAX_SLOTS:
                hints += f"\n⚠️ Превышен лимит {self.STM32_PATTERN_MAX_SLOTS} слотов!\n"
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
            "name": "DSP / Aux Configuration",
            "type": "read-write",
            "bits": 16,
            "description": "Регистр режима тракта: DSP cutoff, auxiliary outputs, absmode/twoscomp и связанные флаги.",
            "usage": "В recording/stimulation профиль задает Register 1 целиком. Для phase-safe нужно явно очищать bit 5 (absmode), bit 4 (DSPen) и bits 3:0 (DSP cutoff), а не трогать старшие биты."
        },
        2: {
            "name": "Zcheck Control",
            "type": "read-write",
            "bits": 16,
            "description": "Управление impedance check. Содержит Zcheck enable, Zcheck DAC power, выбор канала и scale (0.1/1/10 pF).",
            "usage": "Используется только внутри impedance/phase режима и после измерения должен быть возвращен в 0x0000."
        },
        3: {
            "name": "Zcheck DAC",
            "type": "read-write",
            "bits": 16,
            "description": "8-битный DAC для возбуждения при impedance/phase measurement.",
            "usage": "При измерении обновляется равномерно во времени для формирования синусоиды; вне Zcheck должен быть в нейтральном состоянии 0x0080."
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
            "name": "Reserved / Do Not Touch",
            "type": "read-write",
            "bits": 16,
            "description": "В текущем проектном контракте RHS2116 этот регистр не используется, так как управление питанием усилителей идет через R8 и R38.",
            "usage": "Не менять без прямой ссылки на даташит RHS2116."
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
            "name": "Stimulator Global Unlock A",
            "type": "read-write",
            "bits": 16,
            "format": "Ожидаемое значение для разрешения стимуляторов: 0xAAAA",
            "description": "Глобальный unlock stimulation path. Это не per-channel bitmask.",
            "usage": "Для recording/impedance держится в 0x0000. Для stimulation включается вместе с R33=0x00FF.",
            "example": [
                "WRITE 32 0x0000   # recording / safe-state",
                "WRITE 32 0xAAAA   # unlock stimulation"
            ],
            "notes": "Не помечать как triggered. Канальные triggered-регистры коммитятся отдельно через U=1.",
            "related": [33, 42, 44]
        },
        33: {
            "name": "Stimulator Global Unlock B",
            "type": "read-write",
            "bits": 16,
            "format": "Ожидаемое значение для разрешения стимуляторов: 0x00FF",
            "description": "Вторая половина глобального unlock stimulation path. Это не per-channel bitmask.",
            "usage": "Для recording/impedance держится в 0x0000. Для stimulation включается вместе с R32=0xAAAA.",
            "example": [
                "WRITE 33 0x0000   # recording / safe-state",
                "WRITE 33 0x00FF   # unlock stimulation"
            ],
            "notes": "Не помечать как triggered. Канальные triggered-регистры коммитятся отдельно через U=1.",
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
            "type": "read-write",
            "bits": 16,
            "format": "Для проекта всегда 0xFFFF",
            "description": "Питание DC-coupled amplifier blocks. В проекте по инварианту всегда держится включенным.",
            "usage": "Используйте 0xFFFF во всех режимах; отключение может дать нежелательное энергопотребление и расхождения между режимами.",
            "example": [
                "WRITE 38 0xFFFF"
            ],
            "notes": "Не использовать R9 как замену этому регистру.",
            "related": [8]
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
            "description": "Идентификатор чипа (chip ID). Только для чтения. Ожидается 32 (0x0020). Проверка в GUI идёт через Orange Pi → USB → STM32 → SPI.",
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
                try:
                    self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
                except OSError:
                    try:
                        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
                    except OSError:
                        pass
                self.udp_sock.bind(('0.0.0.0', listen_port))
                self.udp_sock.settimeout(1.0)
                self.start_udp_listening()

                # Отправляем регистрацию
                server_addr = (udp_host, udp_port)
                self._clear_udp_control_messages()
                self.udp_sock.sendto(b"REGISTER", server_addr)
                self.log("Отправлена регистрация на UDP сервер", "info")

                message = self._wait_for_udp_control_message("REGISTERED", timeout_s=2.0)
                if message != "REGISTERED":
                    raise Exception("Таймаут ожидания подтверждения регистрации")

                self.udp_registered = True
                self.udp_status_label.config(
                    text=f"● Зарегистрирован на {udp_host}:{udp_port}", 
                    style='Success.TLabel'
                )
                self.btn_udp_register.configure(state="disabled")
                self.btn_udp_unregister.configure(state="normal")
                self.btn_start_recording.configure(state="normal")
                self.log(f"Успешно зарегистрирован на UDP сервере {server_addr}", "success")
            except Exception as e:
                self.log(f"Ошибка регистрации на UDP сервере: {e}", "error")
                messagebox.showerror("Ошибка", f"Не удалось зарегистрироваться: {e}")
                if self.udp_sock:
                    self.udp_listening = False
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
            self._clear_udp_control_messages()
            
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
                data, addr = self.udp_sock.recvfrom(65507)
                
                # Проверяем, является ли это текстовым ответом от сервера
                try:
                    text_response = data.decode('utf-8').strip()
                    if text_response.startswith("STATUS "):
                        payload = text_response[len("STATUS "):].strip()
                        event_name = payload
                        operation = ""
                        if " operation=" in payload:
                            event_name, operation = payload.split(" operation=", 1)
                            event_name = event_name.strip()
                            operation = operation.strip()
                        self.after(0, lambda e=event_name, o=operation: self._note_recording_gap_event(e, o))
                        continue
                    if text_response in ["REGISTERED", "UNREGISTERED", "RECORDING_STARTED", "RECORDING_STOPPED"]:
                        self._push_udp_control_message(text_response)
                        # Это текстовый ответ, не бинарные данные
                        if text_response == "RECORDING_STARTED":
                            self.after(0, lambda: self.log("✓ Сервер подтвердил начало регистрации", "success"))
                        elif text_response == "RECORDING_STOPPED":
                            self.after(0, lambda: self.log("✓ Сервер подтвердил остановку регистрации", "info"))
                            self.after(0, lambda: self._finalize_recording_stop("сервер"))
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
                        off = len(self.recording_blob)
                        self.recording_blob.extend(data)
                        self.recording_hex_data.append((off, len(data)))
                        self.recording_packet_count += 1
                        values_in_packet, samples_in_packet = self._count_values_in_packet(data)
                        self.recording_values_received += values_in_packet
                        self.recording_samples_received += samples_in_packet
                        
                        if self.recording_packet_count <= 5:
                            self.after(0, lambda p=self.recording_packet_count, s=len(data): 
                                self.log(f"📦 Получен пакет #{p}, размер: {s} байт", "info"))
                        
                        if self.recording_packet_count % 1000 == 0:
                            self.after(0, self._update_recording_stats_only)
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
            elapsed = 0.0
            if self.recording_receive_started_at is not None:
                elapsed = max(0.0, time.perf_counter() - self.recording_receive_started_at)
            values_per_second = (self.recording_values_received / elapsed) if elapsed > 0 else 0.0
            self.recording_stats_label.config(
                text=(
                    f"Пакетов: {self.recording_packet_count} | "
                    f"Сохранено: {saved_count} | "
                    f"Значений: {self.recording_values_received} | "
                    f"Значений/с: {values_per_second:.1f}"
                )
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

            parsed_count = 0
            skipped_count = 0
            total_samples = 0
            total_graph_points = 0
            invalid_channels_logged = set()
            first_adc_values = []
            first_uv_values = []
            recording_start_time = getattr(self, 'recording_start_time', None)
            sample_rate = self.recording_sample_rate if self.recording_sample_rate and self.recording_sample_rate > 0 else None
            channel_times = [[] for _ in range(16)]
            channel_point_times = [[] for _ in range(16)]
            channel_values = [[] for _ in range(16)]
            raw_struct_cache = {}

            ch_list = list(getattr(self, "recording_channel_list", list(range(16))))
            global_frame_idx = 0

            for packet_entry in self.recording_hex_data:
                if isinstance(packet_entry, tuple):
                    off, ln = packet_entry
                    binary_data = self.recording_blob[off : off + ln]
                else:
                    binary_data = packet_entry

                if len(binary_data) >= 16:
                    try:
                        magic, _, channel_count, sample_count = struct.unpack_from(
                            "<IIHH", binary_data, 0
                        )
                    except struct.error:
                        magic = 0
                        channel_count = 0
                        sample_count = 0
                    if magic == 0x344E5449 and sample_count > 0:
                        if channel_count <= 0 or channel_count > 16:
                            channel_count = len(ch_list)
                        value_count = channel_count * sample_count
                        values_offset = 16
                        if len(binary_data) < values_offset + value_count * 2:
                            skipped_count += 1
                            continue
                        raw_struct = raw_struct_cache.get(value_count)
                        if raw_struct is None:
                            raw_struct = struct.Struct(f"<{value_count}H")
                            raw_struct_cache[value_count] = raw_struct
                        try:
                            raw_values = raw_struct.unpack_from(binary_data, values_offset)
                        except struct.error:
                            skipped_count += 1
                            continue

                        if recording_start_time is None:
                            recording_start_time = time.time()
                        sample_dt = (1.0 / sample_rate) if sample_rate and sample_rate > 0 else 0.0
                        packet_ch_list = ch_list[:channel_count]
                        base_frame_idx = struct.unpack_from("<I", binary_data, 12)[0]

                        for sample_idx in range(sample_count):
                            candidate = base_frame_idx + sample_idx
                            if candidate >= global_frame_idx:
                                frame_idx = candidate
                            else:
                                frame_idx = global_frame_idx + sample_idx
                            global_frame_idx = frame_idx + 1
                            if frame_idx < USB_RECORDING_PIPELINE_SKIP_FRAMES:
                                continue

                            relative_time = frame_idx * sample_dt
                            value_offset = sample_idx * channel_count
                            for ch_idx, channel_num in enumerate(packet_ch_list):
                                if channel_num < 0 or channel_num > 15:
                                    continue
                                adc_value_unsigned = raw_values[value_offset + ch_idx]
                                value_uv = rhs2116_ac_uV(adc_value_unsigned)
                                channel_times[channel_num].append(relative_time)
                                channel_point_times[channel_num].append(relative_time)
                                channel_values[channel_num].append(value_uv)
                                total_graph_points += 1
                                if len(first_adc_values) < 10:
                                    first_adc_values.append((channel_num, adc_value_unsigned))
                                if len(first_uv_values) < 10:
                                    first_uv_values.append((channel_num, value_uv))
                            total_samples += 1

                        parsed_count += 1
                        continue

                if len(binary_data) >= 44:
                    try:
                        magic, version, header_size, sequence, timestamp_ns, channel_count, sample_count, flags, _ = struct.unpack_from(
                            '<IHHIQHHHH',
                            binary_data,
                            0,
                        )
                    except struct.error:
                        magic = version = 0
                        header_size = 0
                    if magic == 0x334E5449 and version == 3:
                        if header_size < 44 or len(binary_data) < header_size:
                            skipped_count += 1
                            continue

                        packet_ch_list = list(binary_data[28:44][:channel_count])
                        value_count = channel_count * sample_count
                        values_offset = header_size
                        values_bytes_len = value_count * 2
                        if values_offset + values_bytes_len > len(binary_data):
                            skipped_count += 1
                            continue

                        raw_struct = raw_struct_cache.get(value_count)
                        if raw_struct is None:
                            raw_struct = struct.Struct(f'<{value_count}H')
                            raw_struct_cache[value_count] = raw_struct
                        try:
                            raw_values = raw_struct.unpack_from(binary_data, values_offset)
                        except struct.error:
                            skipped_count += 1
                            continue

                        timestamp = timestamp_ns / 1_000_000_000.0
                        if not (0 <= timestamp <= 4102444800):
                            skipped_count += 1
                            continue

                        if recording_start_time is None:
                            recording_start_time = timestamp
                        base_relative_time = timestamp - recording_start_time

                        if parsed_count == 0 and total_samples == 0 and channel_count > 0:
                            self.log(
                                f"✓ Первый sample v3: seq={sequence}, channels={channel_count}, samples={sample_count}, flags=0x{flags:04X}",
                                "info",
                            )

                        sample_dt = (1.0 / sample_rate) if sample_rate and sample_rate > 0 else 0.0
                        for sample_idx in range(sample_count):
                            frame_idx = global_frame_idx
                            global_frame_idx += 1
                            if frame_idx < USB_RECORDING_PIPELINE_SKIP_FRAMES:
                                continue
                            relative_time = frame_idx * sample_dt
                            value_offset = sample_idx * channel_count
                            for ch_idx, channel_num in enumerate(packet_ch_list):
                                adc_value_unsigned = raw_values[value_offset + ch_idx]
                                if len(first_adc_values) < 10:
                                    first_adc_values.append((channel_num, adc_value_unsigned))
                                if 0 <= channel_num <= 15:
                                    value_uv = rhs2116_ac_uV(adc_value_unsigned)
                                    channel_times[channel_num].append(relative_time)
                                    channel_point_times[channel_num].append(relative_time)
                                    channel_values[channel_num].append(value_uv)
                                    total_graph_points += 1
                                    if len(first_uv_values) < 10:
                                        first_uv_values.append((channel_num, value_uv))
                                elif channel_num not in invalid_channels_logged and len(invalid_channels_logged) < 5:
                                    self.log(f"⚠ Пропущен невалидный номер канала: {channel_num} (ожидается 0-15)", "warning")
                                    invalid_channels_logged.add(channel_num)
                            total_samples += 1

                        parsed_count += 1
                        continue

                if len(binary_data) < 5 or binary_data[0] != 2:
                    skipped_count += 1
                    continue

                try:
                    sample_count = struct.unpack_from('<I', binary_data, 1)[0]
                except struct.error:
                    skipped_count += 1
                    continue

                offset = 5
                packet_samples_parsed = 0

                for _ in range(sample_count):
                    if offset + 12 > len(binary_data):
                        break

                    try:
                        timestamp, pipeline_skip, channel_count = struct.unpack_from('<dHH', binary_data, offset)
                    except struct.error:
                        break
                    offset += 12

                    if offset + channel_count > len(binary_data):
                        break
                    ch_list = binary_data[offset:offset + channel_count]
                    offset += channel_count

                    if offset + 2 > len(binary_data):
                        break
                    try:
                        raw_count = struct.unpack_from('<H', binary_data, offset)[0]
                    except struct.error:
                        break
                    offset += 2

                    raw_bytes_len = raw_count * 2
                    if offset + raw_bytes_len > len(binary_data):
                        break

                    raw_struct = raw_struct_cache.get(raw_count)
                    if raw_struct is None:
                        raw_struct = struct.Struct(f'<{raw_count}H')
                        raw_struct_cache[raw_count] = raw_struct

                    try:
                        raw_values = raw_struct.unpack_from(binary_data, offset)
                    except struct.error:
                        break
                    offset += raw_bytes_len

                    if not (0 <= timestamp <= 4102444800):
                        continue

                    if recording_start_time is None:
                        recording_start_time = timestamp
                    relative_time = timestamp - recording_start_time

                    usable_count = min(channel_count, max(0, raw_count - pipeline_skip))
                    if usable_count <= 0:
                        continue

                    point_dt = (1.0 / (sample_rate * usable_count)) if sample_rate else 0.0
                    first_sample_channels = 0

                    for i in range(usable_count):
                        channel_num = ch_list[i]
                        adc_value_unsigned = raw_values[pipeline_skip + i]

                        if len(first_adc_values) < 10:
                            first_adc_values.append((channel_num, adc_value_unsigned))

                        if 0 <= channel_num <= 15:
                            value_uv = float((adc_value_unsigned - 32768) * 0.195)
                            channel_times[channel_num].append(relative_time)
                            channel_point_times[channel_num].append(relative_time + i * point_dt)
                            channel_values[channel_num].append(value_uv)
                            total_graph_points += 1
                            first_sample_channels += 1

                            if len(first_uv_values) < 10:
                                first_uv_values.append((channel_num, value_uv))
                        elif channel_num not in invalid_channels_logged and len(invalid_channels_logged) < 5:
                            self.log(f"⚠ Пропущен невалидный номер канала: {channel_num} (ожидается 0-15)", "warning")
                            invalid_channels_logged.add(channel_num)

                    if parsed_count == 0 and packet_samples_parsed == 0 and first_sample_channels > 0:
                        self.log(
                            f"✓ Первый sample v2: сохранено {first_sample_channels} каналов, "
                            f"pipeline_skip={pipeline_skip}, raw_count={raw_count}",
                            "info",
                        )

                    packet_samples_parsed += 1
                    total_samples += 1

                if packet_samples_parsed > 0:
                    parsed_count += 1
                else:
                    skipped_count += 1

                if total_samples and total_samples % 10000 == 0:
                    self.log(f"Обработано {total_samples} сэмплов из {parsed_count} пакетов, точек: {total_graph_points}", "info")

            self.recording_graph_data = {
                channel: {
                    'time': channel_times[channel],
                    'point_time': channel_point_times[channel],
                    'values_uv': channel_values[channel],
                }
                for channel in range(16)
                if channel_times[channel]
            }
            if recording_start_time is not None:
                self.recording_start_time = recording_start_time

            if first_adc_values:
                self.log("=== Sanity-check: Первые 10 raw ADC значений ===", "info")
                for ch, adc_val in first_adc_values[:10]:
                    self.log(f"  Ch{ch}: ADC={adc_val} (0x{adc_val:04X})", "info")
                    if adc_val == 0 or adc_val == 65535:
                        self.log(f"  ⚠ ВНИМАНИЕ: Ch{ch} показывает клиппинг (0 или 65535)!", "warning")

            if first_uv_values:
                self.log("=== Sanity-check: Первые 10 значений после конвертации (µV) ===", "info")
                for ch, uv_val in first_uv_values[:10]:
                    self.log(f"  Ch{ch}: {uv_val:8.2f} µV", "info")

            self.log(f"Парсинг завершен. Обработано {parsed_count} из {total_packets} пакетов ({total_samples} samples, пропущено: {skipped_count})", "success")
            self.log(f"Всего точек в графике: {total_graph_points} (каналов: {len(self.recording_graph_data)})", "info")

            if parsed_count < total_packets - skipped_count:
                self.log(f"⚠ Предупреждение: не все пакеты были обработаны ({parsed_count}/{total_packets}, пропущено: {skipped_count})", "warning")

            if total_graph_points == 0:
                self.log("⚠ ОШИБКА: после парсинга нет точек для графика!", "error")
                return

            channel_lengths = {
                ch: len(channel_values[ch]) for ch in range(16) if channel_values[ch]
            }
            if channel_lengths:
                min_len = min(channel_lengths.values())
                max_len = max(channel_lengths.values())
                if max_len - min_len > 1:
                    self.log(
                        f"⚠ Разная длина каналов после парсинга: min={min_len}, max={max_len} "
                        f"({channel_lengths})",
                        "warning",
                    )

            self._compute_recording_spectrum_data()

        except Exception as e:
            self.log(f"Ошибка парсинга данных: {e}", "error")
            import traceback
            self.log(f"Детали ошибки: {traceback.format_exc()}", "error")
            messagebox.showerror("Ошибка", f"Не удалось обработать данные: {e}")

    def _estimate_channel_sample_rate(self, channel_data):
        """Оценивает эффективную частоту дискретизации конкретного канала."""
        point_times = np.asarray(channel_data.get('point_time', channel_data.get('time', [])), dtype=float)
        if point_times.size >= 2:
            diffs = np.diff(point_times)
            positive_diffs = diffs[diffs > 0]
            if positive_diffs.size > 0:
                median_dt = float(np.median(positive_diffs))
                if median_dt > 0:
                    return 1.0 / median_dt

        if self.recording_sample_rate and self.recording_sample_rate > 0:
            return float(self.recording_sample_rate)

        return None

    def _compute_recording_spectrum_data(self):
        """Строит амплитудный спектр FFT для распарсенных каналов."""
        self.recording_spectrum_data = {}

        valid_channels = [
            ch for ch in sorted(self.recording_graph_data.keys(), key=lambda x: (isinstance(x, str), x))
            if isinstance(ch, int) and 0 <= ch <= 15
        ]

        for channel in valid_channels:
            channel_data = self.recording_graph_data[channel]
            values = np.asarray(channel_data.get('values_uv', []), dtype=float)
            if values.size < RECORDING_SPECTRUM_MIN_POINTS:
                continue

            sample_rate = self._estimate_channel_sample_rate(channel_data)
            if not sample_rate or sample_rate <= 0:
                continue

            centered_values = values - np.mean(values)
            window = np.hanning(centered_values.size)
            window_sum = float(np.sum(window))
            if window_sum <= 0:
                continue

            spectrum = np.fft.rfft(centered_values * window)
            freqs = np.fft.rfftfreq(centered_values.size, d=1.0 / sample_rate)
            amplitude_uv = np.abs(spectrum) * (2.0 / window_sum)

            if freqs.size < 2:
                continue

            self.recording_spectrum_data[channel] = {
                'freqs_hz': freqs,
                'amplitude_uv': amplitude_uv,
                'sample_rate_hz': sample_rate,
                'point_count': int(centered_values.size),
            }

        if self.recording_spectrum_data:
            self.log(
                f"Спектральное разложение рассчитано для {len(self.recording_spectrum_data)} каналов",
                "info",
            )
        else:
            self.log(
                "Недостаточно данных для спектрального разложения. Нужно больше точек после регистрации.",
                "warning",
            )
    
    def _perform_calibration_check(self):
        """
        Выполняет проверку baseline/покоя на первых 2 секундах данных.
        """
        try:
            self.log("=== Calibration/Rest Check ===", "info")
            
            # Выбираем первый доступный канал для проверки.
            check_channel = None
            if self.recording_graph_data:
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

            valid_channels = [
                ch for ch in sorted(self.recording_graph_data.keys(), key=lambda x: (isinstance(x, str), x))
                if isinstance(ch, int) and 0 <= ch <= 15
            ]
            if not valid_channels:
                self.recording_data_text.insert(
                    "1.0",
                    "Данные распарсены, но валидные каналы 0-15 отсутствуют.\n"
                    f"Каналов в recording_graph_data: {len(self.recording_graph_data)}\n"
                    "Проверьте логи на наличие ошибок парсинга.\n"
                )
                self.recording_data_text.configure(state="disabled")
                self.log("⚠ Нет валидных каналов для отображения в текстовом виде", "warning")
                return

            preview_lengths = [
                len(self.recording_graph_data[ch]['values_uv']) for ch in valid_channels
            ]
            preview_n = min(RECORDING_TEXT_PREVIEW_LIMIT, *preview_lengths) if preview_lengths else 0
            if preview_n <= 0:
                self.recording_data_text.insert(
                    "1.0",
                    "Данные распарсены, но значения отсутствуют.\n"
                    f"Каналов в recording_graph_data: {len(self.recording_graph_data)}\n"
                    "Проверьте логи на наличие ошибок парсинга.\n"
                )
                self.recording_data_text.configure(state="disabled")
                self.log("⚠ Нет значений для отображения", "warning")
                return

            reference_channel = valid_channels[0]
            reference_times = self.recording_graph_data[reference_channel]['time']
            start_time = getattr(self, 'recording_start_time', 0)
            lines = []

            for i in range(preview_n):
                rel_time = reference_times[i] if i < len(reference_times) else (i / float(self.recording_sample_rate or 1))
                abs_time = start_time + rel_time
                time_str = datetime.fromtimestamp(abs_time).strftime("%H:%M:%S.%f")[:-3]
                row_parts = [f"[{time_str}]"]

                for channel in valid_channels:
                    channel_data = self.recording_graph_data[channel]
                    values_uv = channel_data['values_uv']
                    if i < len(values_uv):
                        row_parts.append(f"Ch{channel}:{values_uv[i]:8.2f} µV")
                    else:
                        row_parts.append(f"Ch{channel}:        -")

                lines.append(" ".join(row_parts))

            output_chunks = ["\n".join(lines)]
            if preview_lengths and max(preview_lengths) > RECORDING_TEXT_PREVIEW_LIMIT:
                output_chunks.append(
                    f"\n\nПоказаны первые {RECORDING_TEXT_PREVIEW_LIMIT} кадров из {max(preview_lengths)}. "
                    "Полные данные доступны на графике и при экспорте в CSV."
                )

            valid_channels_count = len(valid_channels)
            total_channels = len(self.recording_graph_data)
            total_points = max(preview_lengths) if preview_lengths else 0
            output_chunks.append("\n\n=== Статистика ===")
            output_chunks.append(f"Валидных каналов (0-15): {valid_channels_count}")
            if total_channels != valid_channels_count:
                output_chunks.append(f"Всего каналов (включая невалидные): {total_channels}")
            output_chunks.append(f"Временных точек опорного канала: {total_points}")
            output_chunks.append(f"Пакетов обработано: {len(self.recording_hex_data)}")
            output_chunks.append("\n=== Статистика по каналам ===")

            for channel in sorted(self.recording_graph_data.keys(), key=lambda x: (isinstance(x, str), x)):
                if isinstance(channel, int) and 0 <= channel <= 15:
                    channel_data = self.recording_graph_data[channel]
                    if len(channel_data['values_uv']) > 0:
                        values = np.array(channel_data['values_uv'])
                        mean_val = np.mean(values)
                        std_val = np.std(values)
                        min_val = np.min(values)
                        max_val = np.max(values)
                        channel_name = f"Ch{channel}"
                        output_chunks.append(
                            f"{channel_name}: mean={mean_val:8.2f} µV, std={std_val:8.2f} µV, "
                            f"range=[{min_val:8.2f}, {max_val:8.2f}] µV"
                        )

            if self.recording_spectrum_data:
                output_chunks.append("\n=== Доминирующие частоты ===")
                for channel in sorted(self.recording_spectrum_data.keys()):
                    spectrum_data = self.recording_spectrum_data[channel]
                    freqs = spectrum_data['freqs_hz']
                    amplitudes = spectrum_data['amplitude_uv']
                    if len(freqs) < 2 or len(amplitudes) < 2:
                        continue

                    dominant_idx = 1 + int(np.argmax(amplitudes[1:]))
                    output_chunks.append(
                        f"Ch{channel}: {freqs[dominant_idx]:8.2f} Гц, амплитуда {amplitudes[dominant_idx]:8.2f} µV"
                    )

            self.recording_data_text.insert("1.0", "\n".join(output_chunks))
            self.recording_data_text.see("1.0")
            self.recording_data_text.configure(state="disabled")

            self.log(f"Текстовое поле обновлено: показано {len(timestamps_to_show)} точек", "info")
        except Exception as e:
            self.log(f"Ошибка обновления текстового поля: {e}", "error")
            import traceback
            self.log(f"Детали ошибки: {traceback.format_exc()}", "error")


    def open_recording_graph_window(self, auto_redraw: bool = True):
        """Открывает или поднимает отдельное окно графика."""
        if not MATPLOTLIB_AVAILABLE:
            messagebox.showwarning(
                "Предупреждение",
                "Matplotlib не установлен. Установите: pip install matplotlib",
            )
            return None

        if self.recording_graph_window is None or not self.recording_graph_window.winfo_exists():
            self.recording_graph_window = RecordingGraphWindow(self, self)

        self.recording_graph_window.show_or_raise()
        if auto_redraw:
            self.recording_graph_window.refresh_from_app()
        return self.recording_graph_window

    def _redraw_recording_graph(self):
        """Перерисовывает график регистрации в отдельном окне."""
        if not MATPLOTLIB_AVAILABLE:
            return
        if self.recording_graph_window is None or not self.recording_graph_window.winfo_exists():
            return
        try:
            self.recording_graph_window.redraw_time(self.recording_graph_data)
        except Exception:
            pass

    def _redraw_recording_spectrum(self):
        """Перерисовывает спектр регистрации в отдельном окне."""
        if not MATPLOTLIB_AVAILABLE:
            return
        if self.recording_graph_window is None or not self.recording_graph_window.winfo_exists():
            return
        try:
            self.recording_graph_window.redraw_spectrum(self.recording_spectrum_data)
        except Exception:
            pass

    def on_clear_graph(self):
        """Очищает график и данные"""
        self.recording_graph_data = {}
        self.recording_spectrum_data = {}
        if hasattr(self, 'recording_start_time'):
            delattr(self, 'recording_start_time')

        if MATPLOTLIB_AVAILABLE:
            if self.recording_graph_window is not None and self.recording_graph_window.winfo_exists():
                self.recording_graph_window.clear()
        
        # Очищаем текстовое поле
        self.recording_data_text.configure(state="normal")
        self.recording_data_text.delete("1.0", tk.END)
        self.recording_data_text.configure(state="disabled")
        
        # Очищаем все данные
        self.recording_packet_count = 0
        self.recording_values_received = 0
        self.recording_samples_received = 0
        self.recording_receive_started_at = None
        self.recording_hex_data = []
        self.recording_blob = bytearray()
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
                
                # ВАЖНО: у точек теперь может быть свой point_time для каждого канала,
                # поэтому выгружаем время и значение отдельно по каждому каналу.
                channels = sorted(self.recording_graph_data.keys(), key=lambda x: (isinstance(x, str), x))
                header = ['Индекс точки']
                for ch in channels:
                    channel_name = f'Канал {ch}' if isinstance(ch, int) else str(ch)
                    header.extend([f'{channel_name} время (с)', f'{channel_name} (µВ)'])
                writer.writerow(header)
                
                # Находим максимальную длину данных
                max_len = max(len(self.recording_graph_data[ch]['time']) for ch in channels) if channels else 0
                
                # Записываем данные
                for i in range(max_len):
                    row = [i]
                    
                    for ch in channels:
                        channel_data = self.recording_graph_data[ch]
                        point_times = channel_data.get('point_time', channel_data['time'])
                        if i < len(channel_data['values_uv']):
                            time_value = point_times[i] if i < len(point_times) else ''
                            row.append(time_value)
                            row.append(channel_data['values_uv'][i])
                        else:
                            row.append('')
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
            if MATPLOTLIB_AVAILABLE and self.recording_graph_data:
                graph_window = self.open_recording_graph_window(auto_redraw=True)
                if graph_window:
                    self.log("График открыт в отдельном окне", "info")
            self.recording_stats_label.config(
                text=f"Обработано пакетов: {len(self.recording_hex_data)} | График и спектр построены"
            )
            
            messagebox.showinfo(
                "Успех",
                f"График и спектр открыты в отдельном окне.\n"
                f"Обработано пакетов: {len(self.recording_hex_data)}",
            )
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

        graph_window = self.open_recording_graph_window(auto_redraw=True)
        if graph_window:
            graph_window.export_current_plot()

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

            self.btn_start_recording.configure(state="disabled")
            self.btn_stop_recording.configure(state="normal")
            self.recording_packet_count = 0
            self.recording_values_received = 0
            self.recording_samples_received = 0
            self.recording_receive_started_at = time.perf_counter()

            # Очищаем предыдущие данные ДО старта, чтобы не смешивать с новым потоком.
            self.recording_blob = bytearray()
            self.recording_hex_data = []
            self.recording_channel_list = self._parse_recording_channels(channels)
            self.recording_graph_data = {}
            self.recording_spectrum_data = {}
            self.recording_gap_events = []
            self.recording_sample_rate = sample_rate
            self.recording_runtime_status.set(
                "Регистрация активна. Стимуляция разрешена, но во время переключения режима Intan возможны краткие окна потери данных."
            )
            if hasattr(self, 'recording_start_time'):
                delattr(self, 'recording_start_time')

            self._clear_udp_control_messages()
            self.recording_stop_processed = False
            self.recording_active = True
            self.udp_sock.sendto(cmd.encode('utf-8'), server_addr)
            self.log(f"Отправлена команда начала регистрации: {cmd} на {server_addr}", "info")

            ack = self._wait_for_udp_control_message("RECORDING_STARTED", timeout_s=2.0)
            if ack == "RECORDING_STARTED":
                self.log("✓ Сервер подтвердил начало регистрации", "success")
            else:
                self.log(
                    "⚠ Таймаут ожидания подтверждения от сервера; оставляю прием данных активным, "
                    "так как поток мог стартовать раньше ответа.",
                    "warning",
                )

            self.log(f"Регистрация начата. recording_active={self.recording_active}, hex_data очищен (размер: {len(self.recording_hex_data)})", "info")
            
            # Очищаем поле данных и график
            self.recording_data_text.configure(state="normal")
            self.recording_data_text.delete("1.0", tk.END)
            self.recording_data_text.insert("1.0", "Регистрация начата. Данные сохраняются...\n")
            self.recording_data_text.configure(state="disabled")
            
            if MATPLOTLIB_AVAILABLE:
                if self.recording_graph_window is not None and self.recording_graph_window.winfo_exists():
                    self.recording_graph_window.set_waiting_state()
            
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
            self._finalize_recording_stop("локальная команда stop")
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
                        if MATPLOTLIB_AVAILABLE:
                            self.open_recording_graph_window(auto_redraw=True)
                            self.log("График и спектр построены в отдельном окне", "success")
                        else:
                            self.log("Matplotlib недоступен, график не построен", "warning")
                    else:
                        self.log("⚠ recording_graph_data пуст, текстовое поле не обновлено", "warning")
                        self.recording_data_text.configure(state="normal")
                        self.recording_data_text.delete("1.0", tk.END)
                        self.recording_data_text.insert("1.0", "Данные распарсены, но recording_graph_data пуст.\nПроверьте логи на наличие ошибок парсинга.")
                        self.recording_data_text.configure(state="disabled")
                    
                    self.recording_stats_label.config(
                        text=f"Обработано пакетов: {len(self.recording_hex_data)} | График и спектр построены"
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
            scale_map = {"0.1 pF", "1 pF", "10 pF"}
            if scale_str not in scale_map:
                messagebox.showerror("Ошибка", "Шкала: 0.1 pF, 1 pF или 10 pF")
                return
            num_averages = max(1, min(1000, int(self.var_impedance_averages.get().strip() or 1)))
            auto_scale = bool(self.var_impedance_auto_scale.get())
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверные значения: {e}")
            return

        def _format_result(z_ohm, std_z, n_valid, scale_str, v_amp_uv, phase_deg=0.0, likely_floating=False):
            if likely_floating:
                text = f"Импеданс канала {ch}: канал в воздухе (V_amp={v_amp_uv:.1f} µV < 15 µV, не считаем Z)"
            elif z_ohm >= 1e6:
                text = f"Импеданс канала {ch}: {z_ohm/1e6:.2f} ± {std_z/1e6:.2f} MΩ, фаза {phase_deg:.1f}° (n={n_valid}, C={scale_str})"
            elif z_ohm >= 1e3:
                text = f"Импеданс канала {ch}: {z_ohm/1e3:.1f} ± {std_z/1e3:.1f} кΩ, фаза {phase_deg:.1f}° (n={n_valid}, C={scale_str})"
            else:
                text = f"Импеданс канала {ch}: {z_ohm:.0f} ± {std_z:.0f} Ω, фаза {phase_deg:.1f}° (n={n_valid}, C={scale_str})"
            if std_z > 2 * max(z_ohm, 1) and not likely_floating:
                text += "  ⚠ нестабильно"
            warn = []
            if v_amp_uv > 4000:
                warn.append("⚠ Насыщение: V_amp>{:.1f} mV — меньшая C".format(v_amp_uv / 1000))
            z_par_ohm = 1.0 / (2 * math.pi * freq_hz * 10e-12)
            if z_ohm > 500e3 and z_ohm < z_par_ohm * 0.9:
                warn.append("⚠ C~10 pF (Z_C≈{:.1f} MΩ)".format(z_par_ohm / 1e6))
            if warn:
                text += "  " + " | ".join(warn)
            return text

        def worker():
            try:
                timeout_s = max(30.0, min(180.0, 10.0 + num_averages * 0.12))
                self.after(0, lambda: self.log("Быстрый замер (batched driver)...", "info"))
                resp = self.client.send_command({
                    "cmd": "measure_impedance_fast",
                    "channel": ch,
                    "frequency": freq_hz,
                    "scale": scale_str,
                    "num_averages": num_averages,
                    "num_samples": 64,
                    "auto_scale": auto_scale,
                    "include_points": True,
                }, timeout=timeout_s)
                if resp.get("status") != "ok" or "impedance_ohm" not in resp:
                    raise RuntimeError(resp.get("error", "measure_impedance_fast не вернул результат"))

                z_ohm = resp["impedance_ohm"]
                std_z = resp.get("std_dev_ohm", 0)
                n_valid = resp.get("num_valid", 0)
                scale_str_res = resp.get("scale", scale_str)
                v_amp_uv = resp.get("v_amp_uv", 0)
                phase_deg = resp.get("phase_deg", 0.0)
                likely_floating = resp.get("likely_floating", False)
                self._last_impedance_data = {
                    "channel": ch, "frequency": freq_hz, "scale": scale_str_res,
                    "impedance_ohm": z_ohm, "std_dev_ohm": std_z, "num_valid": n_valid, "v_amp_uv": v_amp_uv, "phase_deg": phase_deg,
                    "points": resp.get("points", []), "valid_z": resp.get("valid_z", []),
                }
                text = _format_result(z_ohm, std_z, n_valid, scale_str_res, v_amp_uv, phase_deg, likely_floating)
                self.after(0, lambda: self.impedance_value_label.config(text=text))
                self.after(0, lambda: self.log(text, "success"))
            except Exception as e:
                self.after(0, lambda err=e: self.log(f"Ошибка измерения импеданса: {err}", "error"))
                self.after(0, lambda err=e: self.impedance_value_label.config(text=f"Ошибка: {err}"))
                self.after(0, lambda err=e: messagebox.showerror("Ошибка", str(err)))
        threading.Thread(target=worker, daemon=True).start()

    def on_measure_all_impedances(self):
        """Последовательно измеряет импеданс на всех каналах 0-15 через быстрый путь сервера."""
        try:
            freq_hz = float(self.var_impedance_freq.get().strip())
            scale_str = self.var_impedance_scale.get().strip()
            if freq_hz <= 0:
                messagebox.showerror("Ошибка", "Частота должна быть > 0")
                return
            scale_map = {"0.1 pF": (0, 0.1e-12, "0.1 pF"), "1 pF": (1, 1e-12, "1 pF"), "10 pF": (3, 10e-12, "10 pF")}
            if scale_str not in scale_map:
                messagebox.showerror("Ошибка", "Шкала: 0.1 pF, 1 pF или 10 pF")
                return
            num_averages = max(1, min(1000, int(self.var_impedance_averages.get().strip() or 1)))
            auto_scale = bool(self.var_impedance_auto_scale.get())
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверные значения: {e}")
            return

        def _format_short(ch, resp):
            if resp.get("likely_floating", False):
                return f"{ch}: воздух"
            z_ohm = resp.get("impedance_ohm", 0.0)
            phase_deg = resp.get("phase_deg", 0.0)
            if z_ohm >= 1e6:
                return f"{ch}: {z_ohm/1e6:.2f} MΩ / {phase_deg:.1f}°"
            if z_ohm >= 1e3:
                return f"{ch}: {z_ohm/1e3:.1f} кΩ / {phase_deg:.1f}°"
            return f"{ch}: {z_ohm:.0f} Ω / {phase_deg:.1f}°"

        def worker():
            results = []
            try:
                timeout_s = max(30.0, min(180.0, 10.0 + num_averages * 0.12))
                self.after(0, lambda: self.log("Запуск последовательного измерения импеданса по каналам 0-15...", "info"))
                for ch in range(16):
                    self.after(0, lambda ch=ch: self.impedance_value_label.config(text=f"Измерение импеданса: канал {ch}..."))
                    resp = self.client.send_command({
                        "cmd": "measure_impedance_fast",
                        "channel": ch,
                        "frequency": freq_hz,
                        "scale": scale_str,
                        "num_averages": num_averages,
                        "num_samples": 64,
                        "auto_scale": auto_scale,
                        "include_points": True,
                    }, timeout=timeout_s)

                    if resp.get("status") == "ok" and "impedance_ohm" in resp:
                        self._last_impedance_data = {
                            "channel": ch,
                            "frequency": freq_hz,
                            "scale": resp.get("scale", scale_str),
                            "impedance_ohm": resp.get("impedance_ohm", 0),
                            "std_dev_ohm": resp.get("std_dev_ohm", 0),
                            "num_valid": resp.get("num_valid", 0),
                            "v_amp_uv": resp.get("v_amp_uv", 0),
                            "phase_deg": resp.get("phase_deg", 0.0),
                            "points": resp.get("points", []),
                            "valid_z": resp.get("valid_z", []),
                        }
                        short = _format_short(ch, resp)
                        results.append(short)
                        self.after(0, lambda s=short: self.log(f"Импеданс {s}", "success"))
                    else:
                        err = resp.get("error", "неизвестная ошибка")
                        results.append(f"{ch}: ошибка")
                        self.after(0, lambda ch=ch, err=err: self.log(f"Канал {ch}: ошибка измерения импеданса: {err}", "error"))

                summary = " | ".join(results)
                self.after(0, lambda: self.impedance_value_label.config(text=f"Все каналы: {summary}"))
                self.after(0, lambda: self.log("Последовательное измерение всех каналов завершено.", "success"))
            except Exception as e:
                self.after(0, lambda err=e: self.log(f"Ошибка измерения импеданса по всем каналам: {err}", "error"))
                self.after(0, lambda err=e: self.impedance_value_label.config(text=f"Ошибка: {err}"))
                self.after(0, lambda err=e: messagebox.showerror("Ошибка", str(err)))

        threading.Thread(target=worker, daemon=True).start()

    def _phase_append_result_text(self, text):
        self.phase_results_text.configure(state="normal")
        self.phase_results_text.insert("end", text + "\n")
        self.phase_results_text.see("end")
        self.phase_results_text.configure(state="disabled")

    def _phase_clear_result_text(self):
        self.phase_results_text.configure(state="normal")
        self.phase_results_text.delete("1.0", tk.END)
        self.phase_results_text.configure(state="disabled")

    def _phase_set_preset(self, *, freq_hz=None, freq_list=None, repeats=None, samples=None, num_averages=None,
                          auto_scale=None, phase_safe=None):
        if freq_hz is not None:
            self.var_phase_freq.set(str(freq_hz))
        if freq_list is not None:
            self.var_phase_freq_list.set(freq_list)
        if repeats is not None:
            self.var_phase_repeats.set(str(repeats))
        if samples is not None:
            self.var_phase_samples.set(str(samples))
        if num_averages is not None:
            self.var_phase_averages.set(str(num_averages))
        if auto_scale is not None:
            self.var_phase_auto_scale.set(bool(auto_scale))
        if phase_safe is not None:
            self.var_phase_safe.set(bool(phase_safe))

    def on_phase_apply_recommended_preset(self):
        """Рекомендуемые настройки для устойчивого инженерного прогона фазы."""
        self._phase_set_preset(
            freq_hz=1000,
            freq_list="100,300,1000,2000",
            repeats=5,
            samples=128,
            num_averages=10,
            auto_scale=False,
            phase_safe=True,
        )
        self.phase_status_label.config(text="Фазовый тест: применены рекомендованные настройки")
        self.phase_interpretation_label.config(
            text="Интерпретация: базовый протокол установлен. Phase-safe включён, для строгой проверки увеличьте повторы до 10."
        )

    def on_phase_run_1khz_repeat_test(self):
        """Быстрый тест устойчивости на 1 кГц с 10 повторами."""
        self._phase_set_preset(
            freq_hz=1000,
            freq_list="1000",
            repeats=10,
            samples=128,
            num_averages=10,
            auto_scale=False,
            phase_safe=True,
        )
        self.on_phase_frequency_sweep()

    def _phase_interpretation_text(self, phase_mean_deg, phase_std_deg, z_mean_ohm=None):
        if phase_std_deg >= 20.0:
            stability = "Фаза сильно гуляет: методика ещё нестабильна."
        elif phase_std_deg >= 10.0:
            stability = "Фаза измеряется, но разброс ещё великоват для уверенного инженерного вывода."
        else:
            stability = "Фаза выглядит устойчивой."

        if phase_mean_deg > -20.0:
            nature = "Нагрузка преимущественно резистивная."
        elif phase_mean_deg > -60.0:
            nature = "Нагрузка смешанная RC, заметный резистивный вклад."
        elif phase_mean_deg > -85.0:
            nature = "Нагрузка преимущественно ёмкостная."
        else:
            nature = "Нагрузка близка к идеальной ёмкостной."

        magnitude = ""
        if z_mean_ohm is not None:
            if z_mean_ohm >= 1e6:
                magnitude = f" | |Z|≈{z_mean_ohm/1e6:.2f} MΩ."
            elif z_mean_ohm >= 1e3:
                magnitude = f" | |Z|≈{z_mean_ohm/1e3:.1f} кΩ."
            else:
                magnitude = f" | |Z|≈{z_mean_ohm:.0f} Ω."

        return f"{stability} {nature}{magnitude}"

    def _phase_update_summary_and_plot(self):
        if not self.phase_test_results:
            self.phase_summary_label.config(text="Сводка: нет данных фазового теста.")
            self.phase_interpretation_label.config(text="Интерпретация: --")
            if self.phase_canvas:
                self.phase_ax_z.clear()
                self.phase_ax_phase.clear()
                self.phase_ax_z.set_title("|Z|(f)")
                self.phase_ax_phase.set_title("phase(f)")
                self.phase_canvas.draw()
            return

        if len(self.phase_test_results) == 1 and self.phase_test_results[0].get("repeat_count", 0) == 1:
            block = self.phase_test_results[0]
            summary = block.get("summary", {})
            run0 = block.get("runs", [{}])[0] if block.get("runs") else {}
            self.phase_summary_label.config(
                text=(
                    f"Сводка: канал {block.get('channel', 0)}, {block.get('frequency', 0):.0f} Гц, "
                    f"|Z|={summary.get('z_mean_ohm', 0):.2f} Ω, "
                    f"phase raw/corrected={summary.get('phase_raw_mean_deg', 0):.2f}°/"
                    f"{summary.get('phase_mean_deg', 0):.2f}°. "
                    f"{self._phase_frequency_diag_text(run0)}"
                )
            )
            self.phase_interpretation_label.config(
                text=f"Интерпретация: {self._phase_interpretation_text(summary.get('phase_mean_deg', 0.0), summary.get('phase_std_deg', 0.0), summary.get('z_mean_ohm', 0.0))}"
            )
        else:
            phase_stds = [block.get("summary", {}).get("phase_std_deg", 0.0) for block in self.phase_test_results]
            freq_errors_pct = []
            for block in self.phase_test_results:
                for run in block.get("runs", []):
                    freq_errors_pct.append(abs(float(run.get("frequency_error_pct", 0.0) or 0.0)))
            worst_phase_std = max(phase_stds) if phase_stds else 0.0
            worst_freq_error_pct = max(freq_errors_pct) if freq_errors_pct else 0.0
            stability = "устойчива" if worst_phase_std < 10.0 else "неустойчива"
            one_khz_block = next((b for b in self.phase_test_results if abs(b.get("frequency", 0.0) - 1000.0) < 1e-9), None)
            ref_block = one_khz_block or self.phase_test_results[len(self.phase_test_results) // 2]
            ref_summary = ref_block.get("summary", {})
            self.phase_summary_label.config(
                text=(
                    f"Сводка: частот {len(self.phase_test_results)}, максимальный разброс фазы {worst_phase_std:.2f}°, "
                    f"макс. |Δf|={worst_freq_error_pct:.2f}%. Оценка: фаза {stability}. "
                    f"Для инженерного результата лучше держать разброс < 10°."
                )
            )
            self.phase_interpretation_label.config(
                text=(
                    f"Интерпретация: опорная частота {ref_block.get('frequency', 0):.0f} Гц. "
                    f"{self._phase_interpretation_text(ref_summary.get('phase_mean_deg', 0.0), ref_summary.get('phase_std_deg', 0.0), ref_summary.get('z_mean_ohm', 0.0))}"
                )
            )

        if not self.phase_canvas:
            return

        self.phase_ax_z.clear()
        self.phase_ax_phase.clear()

        freqs = [block.get("frequency", 0.0) for block in self.phase_test_results]
        z_means = [block.get("summary", {}).get("z_mean_ohm", 0.0) for block in self.phase_test_results]
        z_stds = [block.get("summary", {}).get("z_std_ohm", 0.0) for block in self.phase_test_results]
        phase_means = [block.get("summary", {}).get("phase_mean_deg", 0.0) for block in self.phase_test_results]
        phase_stds = [block.get("summary", {}).get("phase_std_deg", 0.0) for block in self.phase_test_results]

        self.phase_ax_z.errorbar(freqs, z_means, yerr=z_stds, fmt='o-', capsize=4, color='tab:blue', label='|Z| mean ± std')
        self.phase_ax_z.set_title("|Z|(f)")
        self.phase_ax_z.set_xlabel("Частота (Hz)")
        self.phase_ax_z.set_ylabel("|Z| (Ω)")
        self.phase_ax_z.grid(True, alpha=0.3)
        if any(z > 0 for z in z_means):
            self.phase_ax_z.set_xscale('log')
        self.phase_ax_z.legend(loc='best', fontsize=8)

        self.phase_ax_phase.errorbar(freqs, phase_means, yerr=phase_stds, fmt='o-', capsize=4, color='tab:orange', label='phase corrected mean ± std')
        self.phase_ax_phase.axhline(0.0, color='gray', linestyle='--', linewidth=1, alpha=0.8, label='0°')
        self.phase_ax_phase.axhline(-90.0, color='tab:green', linestyle=':', linewidth=1, alpha=0.8, label='-90°')
        self.phase_ax_phase.set_title("phase(f)")
        self.phase_ax_phase.set_xlabel("Частота (Hz)")
        self.phase_ax_phase.set_ylabel("Фаза (°)")
        self.phase_ax_phase.grid(True, alpha=0.3)
        if any(f > 0 for f in freqs):
            self.phase_ax_phase.set_xscale('log')
        self.phase_ax_phase.legend(loc='best', fontsize=8)

        self.phase_figure.tight_layout()
        self.phase_canvas.draw()

    def _phase_frequency_diag_text(self, run):
        requested = float(run.get("requested_frequency_hz", run.get("frequency", 0.0)) or 0.0)
        effective = float(run.get("effective_frequency_hz", requested) or 0.0)
        error_hz = float(run.get("frequency_error_hz", effective - requested))
        if requested:
            error_pct = float(run.get("frequency_error_pct", (error_hz / requested) * 100.0))
        else:
            error_pct = 0.0
        ratio = (effective / requested) if requested else 0.0
        return (
            f"f_req={requested:.2f} Hz, f_eff={effective:.2f} Hz, "
            f"Δf={error_hz:+.2f} Hz ({error_pct:+.2f}%), ratio={ratio:.4f}"
        )

    def _phase_sampling_diag_text(self, run):
        samples_per_period = run.get("samples_per_period", "")
        actual_num_samples = run.get("actual_num_samples", "")
        return f"samples_per_period={samples_per_period}, actual_num_samples={actual_num_samples}"

    def _normalize_phase_deg(self, phase_deg):
        phase = float(phase_deg)
        while phase <= -180.0:
            phase += 360.0
        while phase > 180.0:
            phase -= 360.0
        return phase

    def _phase_apply_delay_correction(self, run, tau_corr_us):
        raw_phase = float(run.get("phase_deg", 0.0) or 0.0)
        effective = float(run.get("effective_frequency_hz", run.get("requested_frequency_hz", run.get("frequency", 0.0))) or 0.0)
        tau_us = float(tau_corr_us or 0.0)
        correction_deg = 360.0 * effective * (tau_us * 1e-6)
        requested = float(run.get("requested_frequency_hz", run.get("frequency", 0.0)) or 0.0)
        freq_map = run.get("phase_frequency_offsets_deg_map", {}) or {}
        freq_key = int(round(requested)) if requested else None
        freq_correction_deg = float(freq_map.get(freq_key, 0.0)) if freq_key is not None else 0.0
        total_correction_deg = correction_deg + freq_correction_deg
        corrected = self._normalize_phase_deg(raw_phase + total_correction_deg)
        return {
            "phase_raw_deg": raw_phase,
            "phase_delay_correction_us": tau_us,
            "phase_delay_correction_deg": correction_deg,
            "phase_frequency_correction_deg": freq_correction_deg,
            "phase_total_correction_deg": total_correction_deg,
            "phase_corrected_deg": corrected,
        }

    def _parse_phase_frequency_offsets_deg(self):
        raw = (self.var_phase_frequency_offsets_deg.get() or "").strip()
        if not raw:
            return {}
        result = {}
        for chunk in raw.replace(";", ",").split(","):
            item = chunk.strip()
            if not item:
                continue
            if ":" not in item:
                raise ValueError(f"Неверный элемент таблицы поправок: {item}")
            freq_text, deg_text = item.split(":", 1)
            freq = int(round(float(freq_text.strip())))
            result[freq] = float(deg_text.strip())
        return result

    def _phase_build_summary(self, runs, tau_corr_us):
        if not runs:
            return {
                "z_mean_ohm": 0.0,
                "z_std_ohm": 0.0,
                "phase_mean_deg": 0.0,
                "phase_std_deg": 0.0,
                "phase_raw_mean_deg": 0.0,
                "phase_raw_std_deg": 0.0,
                "freq_requested_hz": 0.0,
                "freq_effective_hz": 0.0,
                "freq_error_hz": 0.0,
                "freq_error_pct": 0.0,
                "samples_per_period_mean": 0.0,
                "actual_num_samples_mean": 0.0,
                "phase_delay_correction_us": float(tau_corr_us or 0.0),
                "phase_frequency_correction_deg": 0.0,
                "phase_total_correction_deg": 0.0,
            }

        z_values = [r.get("impedance_ohm", 0.0) for r in runs]
        phase_values = [r.get("phase_corrected_deg", r.get("phase_deg", 0.0)) for r in runs]
        phase_raw_values = [r.get("phase_raw_deg", r.get("phase_deg", 0.0)) for r in runs]
        z_mean = sum(z_values) / len(z_values)
        z_std = (sum((z - z_mean) ** 2 for z in z_values) / len(z_values)) ** 0.5
        phase_mean, phase_std = self._phase_circular_stats(phase_values)
        phase_raw_mean, phase_raw_std = self._phase_circular_stats(phase_raw_values)
        return {
            "z_mean_ohm": z_mean,
            "z_std_ohm": z_std,
            "phase_mean_deg": phase_mean,
            "phase_std_deg": phase_std,
            "phase_raw_mean_deg": phase_raw_mean,
            "phase_raw_std_deg": phase_raw_std,
            "freq_requested_hz": sum(r.get("requested_frequency_hz", r.get("frequency", 0.0)) for r in runs) / len(runs),
            "freq_effective_hz": sum(r.get("effective_frequency_hz", r.get("frequency", 0.0)) for r in runs) / len(runs),
            "freq_error_hz": sum(r.get("frequency_error_hz", 0.0) for r in runs) / len(runs),
            "freq_error_pct": sum(r.get("frequency_error_pct", 0.0) for r in runs) / len(runs),
            "samples_per_period_mean": sum(r.get("samples_per_period", 0.0) for r in runs) / len(runs),
            "actual_num_samples_mean": sum(r.get("actual_num_samples", 0.0) for r in runs) / len(runs),
            "phase_delay_correction_us": float(tau_corr_us or 0.0),
            "phase_frequency_correction_deg": sum(r.get("phase_frequency_correction_deg", 0.0) for r in runs) / len(runs),
            "phase_total_correction_deg": sum(r.get("phase_total_correction_deg", 0.0) for r in runs) / len(runs),
        }

    def _phase_recompute_results_with_tau(self, tau_corr_us):
        for block in self.phase_test_results:
            runs = block.get("runs", [])
            for run in runs:
                run.update(self._phase_apply_delay_correction(run, tau_corr_us))
            block["summary"] = self._phase_build_summary(runs, tau_corr_us)

    def on_phase_recalibrate_delay(self):
        if not self.phase_test_results:
            messagebox.showinfo("Нет данных", "Сначала выполните фазовый sweep или одиночный замер.")
            return
        try:
            target_phase_deg = float(self.var_phase_cal_target_deg.get().strip() or 0.0)
            calib_freq_hz = float(self.var_phase_cal_frequency_hz.get().strip() or 0.0)
            if calib_freq_hz <= 0:
                raise ValueError("Частота калибровки должна быть > 0")
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверные параметры калибровки: {e}")
            return

        block = min(self.phase_test_results, key=lambda item: abs(float(item.get("frequency", 0.0)) - calib_freq_hz))
        summary = block.get("summary", {})
        raw_phase_deg = float(summary.get("phase_raw_mean_deg", summary.get("phase_mean_deg", 0.0)) or 0.0)
        effective_freq_hz = float(summary.get("freq_effective_hz", block.get("frequency", 0.0)) or 0.0)
        if effective_freq_hz <= 0.0:
            messagebox.showerror("Ошибка", "Для выбранной точки effective frequency не определена.")
            return

        delta_phase_deg = self._normalize_phase_deg(target_phase_deg - raw_phase_deg)
        tau_corr_us = (delta_phase_deg / (360.0 * effective_freq_hz)) * 1e6
        self.var_phase_delay_correction_us.set(f"{tau_corr_us:.2f}")
        self._phase_recompute_results_with_tau(tau_corr_us)
        self._phase_append_result_text(
            f"\nτcorr перекалиброван по точке {block.get('frequency', 0.0):.0f} Hz "
            f"(f_eff={effective_freq_hz:.2f} Hz, φ_raw={raw_phase_deg:.2f}°, φ_эталон={target_phase_deg:.2f}°) "
            f"-> τcorr={tau_corr_us:.2f} µs"
        )
        self.phase_status_label.config(text=f"Фазовый тест: τcorr перекалиброван на {tau_corr_us:.2f} µs")
        self._phase_update_summary_and_plot()

    def _parse_phase_settings(self, require_sweep=False):
        try:
            channel = int(self.var_phase_channel.get().strip())
            freq_hz = float(self.var_phase_freq.get().strip())
            scale_str = self.var_phase_scale.get().strip()
            num_averages = max(1, min(1000, int(self.var_phase_averages.get().strip() or 1)))
            num_samples = max(16, min(128, int(self.var_phase_samples.get().strip() or 128)))
            repeats = max(1, min(20, int(self.var_phase_repeats.get().strip() or 1)))
            auto_scale = bool(self.var_phase_auto_scale.get())
            phase_safe = bool(self.var_phase_safe.get())
            phase_delay_correction_us = float(self.var_phase_delay_correction_us.get().strip() or 0.0)
            phase_frequency_offsets_deg = self._parse_phase_frequency_offsets_deg()
            if not (0 <= channel <= 15):
                raise ValueError("Канал должен быть 0–15")
            if freq_hz <= 0:
                raise ValueError("Частота должна быть > 0")
            if scale_str not in ("0.1 pF", "1 pF", "10 pF"):
                raise ValueError("Шкала: 0.1 pF, 1 pF или 10 pF")
            freq_list = [freq_hz]
            if require_sweep:
                raw = self.var_phase_freq_list.get().replace(";", ",")
                freq_list = [float(x.strip()) for x in raw.split(",") if x.strip()]
                if not freq_list:
                    raise ValueError("Укажите хотя бы одну частоту")
                for f in freq_list:
                    if f <= 0:
                        raise ValueError("Все частоты должны быть > 0")
            return {
                "channel": channel,
                "freq_hz": freq_hz,
                "scale": scale_str,
                "num_averages": num_averages,
                "num_samples": num_samples,
                "repeats": repeats,
                "auto_scale": auto_scale,
                "phase_safe": phase_safe,
                "phase_delay_correction_us": phase_delay_correction_us,
                "phase_frequency_offsets_deg": phase_frequency_offsets_deg,
                "freq_list": freq_list,
            }
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверные параметры фазового теста: {e}")
            return None

    def _phase_measurement_timeout_s(self, frequency, num_averages, num_samples):
        freq_hz = max(float(frequency or 0.0), 1.0)
        # Keep GUI timeout aligned with calibrated driver profiles so paced
        # low-frequency measurements do not fail client-side before the
        # ioctl returns.
        spp_hint = {
            100.0: 8.0,
            300.0: 32.0,
            1000.0: 11.0,
            2000.0: 5.0,
        }.get(float(freq_hz))
        if spp_hint is None:
            spp_hint = max(6.0, round(11500.0 / freq_hz))
        expected_s = (max(1, int(num_averages)) * max(1, int(num_samples))) / (freq_hz * spp_hint)
        return max(30.0, min(180.0, 12.0 + expected_s * 1.8))

    def _phase_send_measurement(self, channel, frequency, scale, num_averages, num_samples, auto_scale, phase_safe):
        return self.client.send_command({
            "cmd": "measure_impedance_fast",
            "channel": channel,
            "frequency": frequency,
            "scale": scale,
            "num_averages": num_averages,
            "num_samples": num_samples,
            "auto_scale": auto_scale,
            "phase_safe": phase_safe,
            "include_points": True,
        }, timeout=self._phase_measurement_timeout_s(frequency, num_averages, num_samples))

    def _phase_circular_stats(self, angles_deg):
        if not angles_deg:
            return 0.0, 0.0
        mean_rad = math.atan2(
            sum(math.sin(math.radians(a)) for a in angles_deg),
            sum(math.cos(math.radians(a)) for a in angles_deg),
        )
        mean_deg = math.degrees(mean_rad)
        diffs = [((a - mean_deg + 180.0) % 360.0) - 180.0 for a in angles_deg]
        std_deg = (sum(d * d for d in diffs) / len(diffs)) ** 0.5
        return mean_deg, std_deg

    def on_phase_single_measurement(self):
        """Один детальный фазовый замер на выбранной частоте."""
        settings = self._parse_phase_settings(require_sweep=False)
        if not settings:
            return

        def worker():
            try:
                self.after(0, self._phase_clear_result_text)
                self.after(0, lambda: self.phase_status_label.config(text="Фазовый тест: выполняется одиночный замер..."))
                resp = self._phase_send_measurement(
                    settings["channel"],
                    settings["freq_hz"],
                    settings["scale"],
                    settings["num_averages"],
                    settings["num_samples"],
                    settings["auto_scale"],
                    settings["phase_safe"],
                )
                if resp.get("status") != "ok" or "impedance_ohm" not in resp:
                    raise RuntimeError(resp.get("error", "Не удалось выполнить фазовый замер"))
                resp["phase_frequency_offsets_deg_map"] = settings["phase_frequency_offsets_deg"]
                resp.update(self._phase_apply_delay_correction(resp, settings["phase_delay_correction_us"]))

                self.phase_test_results = [{
                    "channel": settings["channel"],
                    "frequency": settings["freq_hz"],
                    "repeat_count": 1,
                    "runs": [resp],
                    "summary": self._phase_build_summary([resp], settings["phase_delay_correction_us"]),
                }]

                lines = [
                    f"Канал: {settings['channel']}",
                    f"Частота: {settings['freq_hz']} Hz",
                    f"Шкала: {resp.get('scale', settings['scale'])}",
                    f"Phase-safe: {'ON' if resp.get('phase_safe', {}).get('enabled') else 'OFF'}",
                    f"Диагностика частоты: {self._phase_frequency_diag_text(resp)}",
                    f"Диагностика семплирования: {self._phase_sampling_diag_text(resp)}",
                    f"|Z|: {resp.get('impedance_ohm', 0.0):.2f} Ω",
                    f"Фаза raw/corrected: {resp.get('phase_raw_deg', resp.get('phase_deg', 0.0)):.2f}° / {resp.get('phase_corrected_deg', resp.get('phase_deg', 0.0)):.2f}°",
                    f"Фазовая поправка: τ={resp.get('phase_delay_correction_us', settings['phase_delay_correction_us']):.2f} µs, "
                    f"Δφτ={resp.get('phase_delay_correction_deg', 0.0):+.2f}°, "
                    f"Δφf={resp.get('phase_frequency_correction_deg', 0.0):+.2f}°, "
                    f"ΔφΣ={resp.get('phase_total_correction_deg', 0.0):+.2f}°",
                    f"V_amp: {resp.get('v_amp_uv', 0.0):.2f} µV",
                    f"Валидных повторов внутри замера: {resp.get('num_valid', 0)}",
                ]
                if resp.get("phase_safe", {}).get("enabled"):
                    phase_safe_info = resp.get("phase_safe", {})
                    lines.append(
                        "Register 1: "
                        f"0x{phase_safe_info.get('reg1_before', 0):04X} -> "
                        f"0x{phase_safe_info.get('reg1_applied', 0):04X} "
                        f"(DSPen {phase_safe_info.get('dsp_enabled_before', 0)} -> "
                        f"{phase_safe_info.get('dsp_enabled_applied', 0)}, "
                        f"absmode {phase_safe_info.get('absmode_before', 0)} -> "
                        f"{phase_safe_info.get('absmode_applied', 0)}, "
                        f"DSP cutoff {phase_safe_info.get('dsp_cutoff_before', 0)} -> "
                        f"{phase_safe_info.get('dsp_cutoff_applied', 0)})"
                    )
                if resp.get("likely_floating", False):
                    lines.append("Предупреждение: канал выглядит как висящий в воздухе.")
                lines.append(
                    "Интерпретация: " + self._phase_interpretation_text(
                        resp.get('phase_corrected_deg', resp.get('phase_deg', 0.0)), 0.0, resp.get('impedance_ohm', 0.0)
                    )
                )
                self.after(0, lambda: self.phase_status_label.config(text="Фазовый тест: одиночный замер завершён"))
                self.after(0, lambda: self._phase_append_result_text("\n".join(lines)))
                self.after(0, self._phase_update_summary_and_plot)
            except Exception as e:
                self.after(0, lambda err=e: self.phase_status_label.config(text=f"Фазовый тест: ошибка - {err}"))
                self.after(0, lambda err=e: self._phase_append_result_text(f"Ошибка одиночного фазового замера: {err}"))
                self.after(0, lambda err=e: messagebox.showerror("Ошибка", str(err)))

        threading.Thread(target=worker, daemon=True).start()

    def on_phase_frequency_sweep(self):
        """Прогон по нескольким частотам с повторными измерениями для оценки стабильности фазы."""
        settings = self._parse_phase_settings(require_sweep=True)
        if not settings:
            return

        def worker():
            collected = []
            try:
                self.after(0, self._phase_clear_result_text)
                self.after(0, lambda: self.phase_status_label.config(text="Фазовый тест: идёт прогон по частотам..."))
                self.after(0, lambda: self._phase_append_result_text(
                    f"Канал {settings['channel']}, шкала {settings['scale']}, samples={settings['num_samples']}, "
                    f"усреднений={settings['num_averages']}, повторов={settings['repeats']}, "
                    f"phase-safe={'ON' if settings['phase_safe'] else 'OFF'}, "
                    f"τcorr={settings['phase_delay_correction_us']:.2f} µs"
                ))
                for freq in settings["freq_list"]:
                    runs = []
                    self.after(0, lambda freq=freq: self._phase_append_result_text(f"\n=== Частота {freq:.0f} Hz ==="))
                    for rep in range(settings["repeats"]):
                        self.after(0, lambda freq=freq, rep=rep: self.phase_status_label.config(
                            text=f"Фазовый тест: {freq:.0f} Hz, повтор {rep + 1}/{settings['repeats']}"
                        ))
                        resp = self._phase_send_measurement(
                            settings["channel"],
                            freq,
                            settings["scale"],
                            settings["num_averages"],
                            settings["num_samples"],
                            settings["auto_scale"],
                            settings["phase_safe"],
                        )
                        if resp.get("status") != "ok" or "impedance_ohm" not in resp:
                            raise RuntimeError(f"{freq:.0f} Hz: {resp.get('error', 'ошибка измерения')}")
                        resp["phase_frequency_offsets_deg_map"] = settings["phase_frequency_offsets_deg"]
                        resp.update(self._phase_apply_delay_correction(resp, settings["phase_delay_correction_us"]))
                        runs.append(resp)
                        self.after(0, lambda resp=resp, rep=rep: self._phase_append_result_text(
                            f"Повтор {rep + 1}: |Z|={resp.get('impedance_ohm', 0.0):.2f} Ω, "
                            f"фаза raw/corrected={resp.get('phase_raw_deg', resp.get('phase_deg', 0.0)):.2f}°/"
                            f"{resp.get('phase_corrected_deg', resp.get('phase_deg', 0.0)):.2f}°, "
                            f"V_amp={resp.get('v_amp_uv', 0.0):.2f} µV, "
                            f"phase-safe={'ON' if resp.get('phase_safe', {}).get('enabled') else 'OFF'}, "
                            f"τcorr={resp.get('phase_delay_correction_us', settings['phase_delay_correction_us']):.2f} µs, "
                            f"Δφτ={resp.get('phase_delay_correction_deg', 0.0):+.2f}°, "
                            f"Δφf={resp.get('phase_frequency_correction_deg', 0.0):+.2f}°, "
                            f"ΔφΣ={resp.get('phase_total_correction_deg', 0.0):+.2f}°, "
                            f"{self._phase_frequency_diag_text(resp)}, "
                            f"{self._phase_sampling_diag_text(resp)}"
                        ))

                    summary = self._phase_build_summary(runs, settings["phase_delay_correction_us"])
                    collected.append({
                        "channel": settings["channel"],
                        "frequency": freq,
                        "repeat_count": settings["repeats"],
                        "runs": runs,
                        "summary": summary,
                    })
                    self.after(0, lambda summary=summary: self._phase_append_result_text(
                        f"Итог: |Z|={summary['z_mean_ohm']:.2f} ± {summary['z_std_ohm']:.2f} Ω, "
                        f"фаза raw/corrected={summary['phase_raw_mean_deg']:.2f} ± {summary['phase_raw_std_deg']:.2f}° / "
                        f"{summary['phase_mean_deg']:.2f} ± {summary['phase_std_deg']:.2f}°, "
                        f"f_req={summary['freq_requested_hz']:.2f} Hz, "
                        f"f_eff={summary['freq_effective_hz']:.2f} Hz, "
                        f"Δf={summary['freq_error_hz']:+.2f} Hz ({summary['freq_error_pct']:+.2f}%), "
                        f"spp≈{summary['samples_per_period_mean']:.2f}, "
                        f"N≈{summary['actual_num_samples_mean']:.2f}, "
                        f"τcorr={summary['phase_delay_correction_us']:.2f} µs, "
                        f"Δφf≈{summary['phase_frequency_correction_deg']:+.2f}°, "
                        f"ΔφΣ≈{summary['phase_total_correction_deg']:+.2f}°"
                    ))
                    self.after(0, lambda summary=summary: self._phase_append_result_text(
                        "Интерпретация: " + self._phase_interpretation_text(
                            summary['phase_mean_deg'], summary['phase_std_deg'], summary['z_mean_ohm']
                        )
                    ))
                    if summary["phase_std_deg"] >= 10.0:
                        self.after(0, lambda freq=freq, summary=summary: self._phase_append_result_text(
                            f"⚠ {freq:.0f} Hz: разброс фазы {summary['phase_std_deg']:.2f}° — результат пока нестабилен."
                        ))
                    else:
                        self.after(0, lambda freq=freq, summary=summary: self._phase_append_result_text(
                            f"✓ {freq:.0f} Hz: разброс фазы {summary['phase_std_deg']:.2f}° — результат выглядит устойчивым."
                        ))

                self.phase_test_results = collected
                self.after(0, lambda: self.phase_status_label.config(text="Фазовый тест: прогон завершён"))
                self.after(0, lambda: self._phase_append_result_text(
                    "\nРекомендация: для инженерной опоры отдельно смотрите 1 кГц и добивайтесь малого разброса между повторами."
                ))
                self.after(0, self._phase_update_summary_and_plot)
            except Exception as e:
                self.after(0, lambda err=e: self.phase_status_label.config(text=f"Фазовый тест: ошибка - {err}"))
                self.after(0, lambda err=e: self._phase_append_result_text(f"\nОшибка фазового прогона: {err}"))
                self.after(0, lambda err=e: messagebox.showerror("Ошибка", str(err)))

        threading.Thread(target=worker, daemon=True).start()

    def on_export_phase_results(self):
        """Экспортирует результаты фазового теста в CSV."""
        if not self.phase_test_results:
            messagebox.showinfo("", "Сначала выполните фазовый тест.")
            return

        path = filedialog.asksaveasfilename(
            title="Экспорт результатов фазового теста",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")],
            initialfile="phase_test_results.csv",
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow([
                    "channel", "frequency_hz", "repeat_idx", "scale", "impedance_ohm",
                    "std_dev_ohm", "phase_raw_deg", "phase_corrected_deg", "phase_delay_correction_us", "phase_delay_correction_deg",
                    "phase_frequency_correction_deg", "phase_total_correction_deg",
                    "v_amp_uv", "num_valid", "likely_floating",
                    "requested_frequency_hz", "effective_frequency_hz", "frequency_error_hz", "frequency_error_pct",
                    "samples_per_period", "actual_num_samples",
                    "phase_safe", "reg1_before", "reg1_applied", "dsp_cutoff_before", "dsp_cutoff_applied"
                ])
                for block in self.phase_test_results:
                    for idx, run in enumerate(block.get("runs", []), 1):
                        phase_safe_info = run.get("phase_safe", {})
                        writer.writerow([
                            block.get("channel", 0),
                            block.get("frequency", 0),
                            idx,
                            run.get("scale", ""),
                            run.get("impedance_ohm", 0),
                            run.get("std_dev_ohm", 0),
                            run.get("phase_raw_deg", run.get("phase_deg", 0)),
                            run.get("phase_corrected_deg", run.get("phase_deg", 0)),
                            run.get("phase_delay_correction_us", ""),
                            run.get("phase_delay_correction_deg", ""),
                            run.get("phase_frequency_correction_deg", ""),
                            run.get("phase_total_correction_deg", ""),
                            run.get("v_amp_uv", 0),
                            run.get("num_valid", 0),
                            int(bool(run.get("likely_floating", False))),
                            run.get("requested_frequency_hz", run.get("frequency", 0)),
                            run.get("effective_frequency_hz", ""),
                            run.get("frequency_error_hz", ""),
                            run.get("frequency_error_pct", ""),
                            run.get("samples_per_period", ""),
                            run.get("actual_num_samples", ""),
                            int(bool(phase_safe_info.get("enabled", False))),
                            phase_safe_info.get("reg1_before", ""),
                            phase_safe_info.get("reg1_applied", ""),
                            phase_safe_info.get("dsp_cutoff_before", ""),
                            phase_safe_info.get("dsp_cutoff_applied", ""),
                        ])
                    summary = block.get("summary", {})
                    writer.writerow([
                        block.get("channel", 0),
                        block.get("frequency", 0),
                        "summary",
                        "",
                        summary.get("z_mean_ohm", 0),
                        summary.get("z_std_ohm", 0),
                        summary.get("phase_raw_mean_deg", 0),
                        summary.get("phase_mean_deg", 0),
                        summary.get("phase_delay_correction_us", ""),
                        "",
                        summary.get("phase_frequency_correction_deg", ""),
                        summary.get("phase_total_correction_deg", ""),
                        "",
                        "",
                        "",
                        summary.get("freq_requested_hz", ""),
                        summary.get("freq_effective_hz", ""),
                        summary.get("freq_error_hz", ""),
                        summary.get("freq_error_pct", ""),
                        summary.get("samples_per_period_mean", ""),
                        summary.get("actual_num_samples_mean", ""),
                        "",
                        "",
                        "",
                        "",
                        "",
                    ])
            self.log(f"Результаты фазового теста экспортированы: {path}", "success")
            messagebox.showinfo("Успех", f"Результаты фазового теста экспортированы в:\n{path}")
        except Exception as e:
            self.log(f"Ошибка экспорта фазового теста: {e}", "error")
            messagebox.showerror("Ошибка", str(e))

    def on_export_impedance_points(self):
        """Экспортирует точки последнего измерения импеданса в CSV для просмотра в impedance_csv_viewer."""
        if not self._last_impedance_data:
            messagebox.showinfo("", "Сначала выполните измерение импеданса.")
            return
        d = self._last_impedance_data
        path = filedialog.asksaveasfilename(
            title="Экспорт точек импеданса",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")],
            initialfile=f"impedance_ch{d.get('channel', 0)}_{d.get('scale', '1pF').replace(' ', '')}.csv",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = lambda s: f.write(s + "\n")
                w("# Экспорт точек импеданса RHS2116 (совместимо с impedance_csv_viewer)")
                w("")
                w("--- PARAMETRY IZMERENIYA ---")
                w("parametr;znachenie")
                w(f"channel;{d.get('channel', 0)}")
                w(f"frequency;{d.get('frequency', 1000)}")
                w(f"scale;{d.get('scale', '1 pF')}")
                w(f"num_averages;{d.get('num_valid', 0)}")
                w("")
                w("--- KAZHDYY OTDELNYY ZAMER do usredneniya ---")
                w("nom_zamera;z_raw;z_corr;V_amp;V_rms;phase_deg")
                for p in d.get("points", []):
                    nom = p.get("nom", 0)
                    zr = p.get("z_raw", 0)
                    zc = p.get("z_corr", 0)
                    vamp = p.get("V_amp", 0)
                    vrms = p.get("V_rms", vamp / (2 ** 0.5) if vamp else 0)
                    phase_deg = p.get("phase_deg", 0)
                    w(f"{nom};{zr};{zc};{vamp};{vrms};{phase_deg}")
                w("")
                w("--- STATISTIKA POSLE USREDNENIYA ---")
                w("pokazatel;znachenie")
                w(f"impedance_ohm;{d.get('impedance_ohm', 0)}")
                w(f"std_dev_ohm;{d.get('std_dev_ohm', 0)}")
                w(f"v_amp_uv;{d.get('v_amp_uv', 0)}")
                w(f"phase_deg;{d.get('phase_deg', 0)}")
                w("")
                w("--- SPISOK VALIDNYH ZNAMENIY voshli v raschet ---")
                w("nom;z_ohm")
                for i, z in enumerate(d.get("valid_z", []), 1):
                    w(f"{i};{z}")
            self.log(f"Точки экспортированы: {path}", "success")
            messagebox.showinfo("Успех", f"Экспортировано {len(d.get('points', []))} точек в:\n{path}")
        except Exception as e:
            self.log(f"Ошибка экспорта: {e}", "error")
            messagebox.showerror("Ошибка", str(e))

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

    def on_auto_filters_wideband(self):
        """Автоматически устанавливает значения фильтров для широкополосной записи"""
        try:
            self.var_fh_freq.set("7500")
            self.var_reg4.set("0x0016")
            self.var_reg5.set("0x0017")
            self.var_fl_freq.set("5")
            self.var_reg6.set("0x00A8")
            self.var_reg7.set("0x000A")
            self.var_dsp_cutoff.set("0")
            self.var_reg1.set("0x051A")
            self.log("Автоматически установлены значения фильтров для широкополосной записи", "info")
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
            
            # RHS2116 Register 1: bit 4 = DSPen, bits 3:0 = DSP cutoff.
            reg1_dsp_cutoff = reg1 & 0xF
            if reg1_dsp_cutoff != dsp_cutoff:
                # Если пользователь задает DSP cutoff вручную, включаем DSP и
                # обновляем только младшие биты RHS2116 Register 1.
                reg1 = (reg1 & ~0x001F) | 0x0010 | (dsp_cutoff & 0xF)
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
                            f"(справочно в лог: fH={self.var_fh_freq.get()} Hz, fL={self.var_fl_freq.get()} Hz; "
                            f"в чип пишутся только регистры; DSP cutoff={dsp_cutoff})",
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


    def _parse_pattern_example_params(self):
        """Читает канал и ток из полей «Параметры примера»."""
        channel = int(self.var_pattern_example_channel.get().strip())
        current_ua = int(float(self.var_pattern_example_current.get().strip()))
        if not 0 <= channel <= 15:
            raise ValueError("Канал должен быть в диапазоне 0–15")
        if not 0 <= current_ua <= 255:
            raise ValueError("Ток должен быть в диапазоне 0–255 µA (шаг 1 µA)")
        return channel, current_ua

    def format_guide_pattern_text(
        self,
        channel,
        current_ua,
        pre_setup_lines=None,
        pattern_lines=None,
        repeat_count=1,
    ):
        """Форматирует 3-этапный скрипт по intan_stim_pattern_guide.md."""
        if pre_setup_lines is None or pattern_lines is None:
            pre_setup_lines, pattern_lines = self.generate_pattern_commands()
        load_preview = self._pattern_lines_for_load(pre_setup_lines, pattern_lines)
        slot_est, spi_est, delay_est = self.estimate_stm32_pattern_slots(load_preview)
        lines = [
            "# STM32 intan_pattern.c — очередь слотов в RAM",
            f"# Оценка: ~{slot_est} слотов (spi≈{spi_est}, delays≈{delay_est}, max 1024)",
            "# pattern_load: PATTERN_ADD_RAW + PATTERN_ADD_DELAY_US",
            "",
            "# Токи (PATTERN_ADD_RAW, R64/R96)",
        ]
        lines.extend(pre_setup_lines)
        lines.extend(["", "# Импульсы: ON → длительность → OFF → пауза (intan_stim_pattern_guide.md)"])
        lines.extend(pattern_lines)
        lines.extend([
            "",
            f"# Запуск: PATTERN_RUN {int(repeat_count)} (кнопка «Запустить», 1–10000)",
            "# Пауза после OFF включается при repeat>1 или если в блоках есть следующий импульс",
            "# Сервер перед run: unlock R32/R33; в конце load: safety R42 OFF",
        ])
        return "\n".join(lines) + "\n"

    @staticmethod
    def _stim_current_hex(current_ua: int) -> int:
        """0x8000 | current_uA — формат magnitude из intan_stim_pattern_guide.md."""
        return 0x8000 | (int(current_ua) & 0xFF)

    @staticmethod
    def _intan_raw_word(reg: int, value: int, u_flag: bool = False, m_flag: bool = False) -> int:
        """word = (header<<24)|(reg<<16)|value; header = 0x80|(U<<5)|(M<<4)."""
        if (64 <= reg <= 79 or 96 <= reg <= 111) and value < 0x8000:
            value = 0x8000 | (value & 0xFF)
        header = 0x80 | ((1 if u_flag else 0) << 5) | ((1 if m_flag else 0) << 4)
        return ((header & 0xFF) << 24) | ((reg & 0xFF) << 16) | (value & 0xFFFF)

    def _format_pattern_add_raw(self, raw_word: int, comment: str = "") -> str:
        line = f"PATTERN_ADD_RAW 0x{int(raw_word) & 0xFFFFFFFF:08X}"
        if comment:
            line += f"    # {comment}"
        return line

    def build_stim_current_writes(self, channel, current_ua=180):
        """Токи R64/R96 как PATTERN_ADD_RAW (1 слот = 1 CS, intan_pattern.c)."""
        channel = int(channel)
        current_ua = int(current_ua)
        current_hex = self._stim_current_hex(current_ua)
        reg_neg = 64 + channel
        reg_pos = 96 + channel
        raw_neg = self._intan_raw_word(reg_neg, current_hex, u_flag=False)
        raw_pos = self._intan_raw_word(reg_pos, current_hex, u_flag=False)
        return [
            self._format_pattern_add_raw(raw_neg, f"R{reg_neg} ch{channel} neg {current_ua} µA"),
            self._format_pattern_add_raw(raw_pos, f"R{reg_pos} ch{channel} pos {current_ua} µA"),
        ]

    def build_stim_pattern_body(self, channel, pulse_us=100, pause_us=100, positive=True):
        """Один импульс: 4× PATTERN_ADD_RAW + 2× PATTERN_ADD_DELAY_US (прошивка STM32)."""
        channel = int(channel)
        pol_mask = (1 << channel) if positive else 0
        en_mask = 1 << channel
        raw_pol = self._intan_raw_word(44, pol_mask, u_flag=False)
        raw_on = self._intan_raw_word(42, en_mask, u_flag=True)
        raw_off = self._intan_raw_word(42, 0x0000, u_flag=True)
        return [
            self._format_pattern_add_raw(raw_pol, f"R44 polarity ch{channel}"),
            self._format_pattern_add_raw(raw_on, f"R42 ON ch{channel} U=1"),
            f"PATTERN_ADD_DELAY_US {int(pulse_us)}",
            self._format_pattern_add_raw(raw_off, "R42 OFF all U=1"),
            f"PATTERN_ADD_DELAY_US {int(pause_us)}",
        ]

    def build_stim_postrun_commands(self, repeat_count=1):
        """§3 Запуск и безопасное выключение."""
        return [
            f"PATTERN_RUN {int(repeat_count)}",
            "WRITE 42 0 1 0",
            "READ 42",
            "READ 40",
            "READ 50",
        ]

    def _execute_stim_script_line(self, line: str, timeout: float = 10.0) -> str:
        """Выполняет одну строку скрипта (INIT_STIM / WRITE / READ / CLEAR_COMP / …)."""
        cmd = line.split("#", 1)[0].strip()
        if not cmd:
            return ""
        return self.client.send_line(cmd, timeout=timeout)

    def _parse_pattern_script(self, pattern_lines):
        """
        Делит скрипт на 3 этапа: prep, pattern, post.
        Поддерживает заголовки §1/§2/§3 и legacy # PRESETUP:.
        """
        prep, pattern, post = [], [], []
        section = "prep"

        for line in pattern_lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                lower = stripped.lower()
                upper = stripped.upper()
                if upper.startswith("# PRESETUP:"):
                    legacy = stripped.split(":", 1)[1].strip()
                    if legacy:
                        prep.append(legacy)
                    continue
                if "1." in stripped and "подготов" in lower:
                    section = "prep"
                    continue
                if "2." in stripped and ("сборк" in lower or "pattern" in lower):
                    section = "pattern"
                    continue
                if "3." in stripped and ("запуск" in lower or "выключ" in lower):
                    section = "post"
                    continue
                continue

            cmd = stripped.split("#", 1)[0].strip()
            if not cmd:
                continue
            parts = cmd.split()
            cmd_type = parts[0].upper()

            if cmd_type in ("INIT_STIM", "CLEAR_COMP"):
                prep.append(cmd)
            elif cmd_type == "WRITE":
                try:
                    reg = int(parts[1], 0)
                except (ValueError, IndexError):
                    reg = -1
                if section == "post":
                    post.append(cmd)
                elif reg in (34, 35) or (64 <= reg <= 111):
                    prep.append(cmd)
                elif section == "prep":
                    prep.append(cmd)
                else:
                    pattern.append(cmd)
            elif cmd_type in ("PATTERN_ADD_RAW", "PATTERN_ADD_DELAY_US", "DELAY_US", "DELAY"):
                pattern.append(cmd)
            elif cmd_type in ("PATTERN_CLEAR", "PATTERN_STATUS"):
                pattern.append(cmd)
            elif cmd_type in ("PATTERN_RUN", "READ"):
                post.append(cmd)
            elif section == "prep":
                prep.append(cmd)
            elif section == "post":
                post.append(cmd)
            else:
                pattern.append(cmd)

        return prep, pattern, post

    def _split_pattern_lines(self, pattern_lines):
        """Совместимость: prep + pattern без post."""
        prep, pattern, _post = self._parse_pattern_script(pattern_lines)
        return prep, pattern

    def on_pattern_load_example(self):
        """Загружает пример 3-этапного скрипта (канал/ток — из «Параметры примера»)."""
        try:
            channel, current_ua = self._parse_pattern_example_params()
        except ValueError as e:
            messagebox.showerror("Ошибка", str(e))
            return
        try:
            repeat_count = int(self.var_pattern_repeat.get().strip())
        except ValueError:
            repeat_count = 1

        prep = self.build_stim_current_writes(channel, current_ua)
        pattern = self.build_stim_pattern_body(channel, 100, 100, positive=True)
        example = self.format_guide_pattern_text(
            channel,
            current_ua,
            pre_setup_lines=prep,
            pattern_lines=pattern,
            repeat_count=repeat_count,
        )
        self.txt_pattern.delete("1.0", tk.END)
        self.txt_pattern.insert("1.0", example)
        self.on_pattern_text_change()
        self.log(
            f"Загружен пример: канал {channel}, {current_ua} µA, 100/100 µs (гайд STM32)",
            "info",
        )

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
            block["data"] = {"channel": 0, "neg_current": 0, "pos_current": 180}
        elif block_type == "polarity":
            block["data"] = {"channel": 0, "positive": True}
        elif block_type == "enable":
            block["data"] = {"channel": 0}
        elif block_type == "disable":
            block["data"] = {"channel": 0}
        elif block_type in ("delay", "pulse_duration", "inter_pulse_delay"):
            block["data"] = {"delay_us": 100}
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
            "pulse_duration": "⏱",
            "inter_pulse_delay": "⏸",
            "delay": "⏱",
            "comment": "💬"
        }
        
        block_names = {
            "current": "Настройка тока",
            "polarity": "Установка полярности",
            "enable": "Включить стимуляцию",
            "disable": "Выключить стимуляцию",
            "pulse_duration": "Длительность импульса (ON)",
            "inter_pulse_delay": "Пауза между импульсами (OFF)",
            "delay": "Задержка (legacy)",
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
            
        elif block["type"] in ("pulse_duration", "inter_pulse_delay", "delay"):
            if block["type"] == "pulse_duration":
                label = "Длительность импульса (µs):"
                hint = "после ON, до OFF"
            elif block["type"] == "inter_pulse_delay":
                label = "Пауза между импульсами (µs):"
                hint = "после OFF, до следующего ON"
            else:
                label = "Задержка (µs):"
                hint = "legacy: после enable=длит., после disable=пауза"
            ttk.Label(content_frame, text=label).grid(row=0, column=0, sticky="w", padx=2, pady=2)
            count_var = tk.StringVar(value=str(block["data"].get("delay_us", block["data"].get("count", 100))))
            count_entry = ttk.Entry(content_frame, textvariable=count_var, width=8)
            count_entry.grid(row=0, column=1, sticky="w", padx=2, pady=2)
            count_var.trace('w', lambda *args, b=block, v=count_var: self.update_block_data(b, "delay_us", v.get()))
            ttk.Label(content_frame, text=hint, font=("TkDefaultFont", 8), foreground="#666").grid(
                row=0, column=2, sticky="w", padx=2, pady=2
            )
            
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

    def _format_blocks_preview_text(self):
        """Текст предпросмотра: PRE-SETUP + PATTERN из блоков конструктора."""
        pre_setup, pattern = self.generate_pattern_commands()
        try:
            channel, current_ua = self._parse_pattern_example_params()
        except ValueError:
            channel, current_ua = 0, 180
        return self.format_guide_pattern_text(
            channel, current_ua, pre_setup_lines=pre_setup, pattern_lines=pattern
        )

    def update_pattern_preview(self):
        """Обновляет предпросмотр паттерна"""
        if not hasattr(self, "pattern_preview_text"):
            self.update_pattern_signal_preview()
            return
        preview_text = self._format_blocks_preview_text()
        self.pattern_preview_text.configure(state="normal")
        self.pattern_preview_text.delete("1.0", tk.END)
        self.pattern_preview_text.insert("1.0", preview_text)
        self.pattern_preview_text.configure(state="disabled")
        # Обновляем также предварительный просмотр формы сигнала
        self.update_pattern_signal_preview()

    def _simulate_blocks_waveform(self, preview_channel: int = 0):
        """
        Простейшая симуляция формы тока по блокам конструктора для одного канала.
        Время задаётся в микросекундах (DELAY_US).
        """
        t = 0.0
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

        for idx, block in enumerate(self.pattern_blocks):
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
                if ch != preview_channel:
                    continue
                pulse_us = 100
                pause_us = 100
                scan = idx + 1
                blocks = self.pattern_blocks
                if scan < len(blocks) and blocks[scan].get("type") == "pulse_duration":
                    pulse_us = self._block_delay_us(blocks[scan])
                    scan += 1
                elif scan < len(blocks) and blocks[scan].get("type") == "delay":
                    pulse_us = self._block_delay_us(blocks[scan])
                    scan += 1
                if scan < len(blocks) and blocks[scan].get("type") == "disable":
                    scan += 1
                if scan < len(blocks) and blocks[scan].get("type") == "inter_pulse_delay":
                    pause_us = self._block_delay_us(blocks[scan])
                elif scan < len(blocks) and blocks[scan].get("type") == "delay":
                    pause_us = self._block_delay_us(blocks[scan])

                enabled = True
                add_point_force()
                if pulse_us > 0:
                    t += float(pulse_us)
                    add_point_force()
                enabled = False
                add_point_force()
                if self._should_add_inter_pulse_pause(blocks, scan + 1, pause_us):
                    t += float(pause_us)
                    add_point_force()
                continue

            elif btype in ("disable", "pulse_duration", "inter_pulse_delay", "delay"):
                continue

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

        try:
            preview_channel = int(self.var_signal_channel.get() if hasattr(self, "var_signal_channel") else "0")
            if preview_channel < 0 or preview_channel > 15:
                preview_channel = 0
        except (ValueError, AttributeError):
            preview_channel = 0

        if not self.pattern_blocks:
            canvas.create_text(
                10,
                10,
                anchor="nw",
                text=f"Добавьте блоки в конструкторе,\nчтобы увидеть форму сигнала (канал {preview_channel}).",
                fill="#777777",
                font=("Arial", 9),
            )
            if hasattr(self, "signal_metrics_label"):
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
            metrics_text += f"Длит: {total_duration:.1f} µs, "
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
    
    @staticmethod
    def _block_delay_us(block) -> int:
        data = block.get("data", {})
        return int(data.get("delay_us", data.get("count", 100)) or 0)

    @staticmethod
    def _has_following_enable(blocks, start_idx: int) -> bool:
        for block in blocks[start_idx:]:
            if block.get("type") == "enable":
                return True
        return False

    def _get_pattern_repeat_count(self) -> int:
        try:
            return max(1, int(self.var_pattern_repeat.get().strip()))
        except (ValueError, AttributeError):
            return 1

    def _should_add_inter_pulse_pause(self, blocks, after_idx: int, pause_us: int) -> bool:
        """
        Пауза после OFF: между импульсами в паттерне или между повторами PATTERN_RUN.
        Без паузы только у последнего импульса при repeat_count=1 (гайд STM32).
        """
        if pause_us <= 0:
            return False
        if self._has_following_enable(blocks, after_idx):
            return True
        return self._get_pattern_repeat_count() > 1

    def _append_pulse_cycle(self, pattern, channel: int, pulse_us: int, pause_us: int, add_pause: bool):
        """ON → DELAY(pulse) → OFF → DELAY(pause) по intan_stim_pattern_guide.md."""
        enable_mask = 1 << int(channel)
        raw_on = self._intan_raw_word(42, enable_mask, u_flag=True)
        raw_off = self._intan_raw_word(42, 0x0000, u_flag=True)
        pattern.append(self._format_pattern_add_raw(raw_on, f"R42 ON ch{channel}"))
        if pulse_us > 0:
            pattern.append(f"PATTERN_ADD_DELAY_US {int(pulse_us)}")
        pattern.append(self._format_pattern_add_raw(raw_off, "R42 OFF"))
        if add_pause and pause_us > 0:
            pattern.append(f"PATTERN_ADD_DELAY_US {int(pause_us)}")

    def _generate_pattern_body_from_blocks(self, channel_default: int = 0):
        """Собирает импульсы: enable + длительность + disable + пауза (гайд STM32)."""
        pattern = []
        has_polarity = False
        body_blocks = [
            b for b in self.pattern_blocks
            if b.get("type") not in ("step_size", "current", "comment")
        ]

        i = 0
        while i < len(body_blocks):
            block = body_blocks[i]
            btype = block.get("type")

            if btype == "polarity":
                has_polarity = True
                ch = int(block["data"].get("channel", channel_default))
                polarity_mask = (1 << ch) if block["data"].get("positive", True) else 0x0000
                raw_pol = self._intan_raw_word(44, polarity_mask, u_flag=False)
                pattern.append(self._format_pattern_add_raw(raw_pol, f"R44 ch{ch}"))
                i += 1
                continue

            if btype == "enable":
                ch = int(block["data"].get("channel", channel_default))
                pulse_us = 100
                pause_us = 100
                i += 1

                if i < len(body_blocks) and body_blocks[i].get("type") == "pulse_duration":
                    pulse_us = self._block_delay_us(body_blocks[i])
                    i += 1
                elif i < len(body_blocks) and body_blocks[i].get("type") == "delay":
                    pulse_us = self._block_delay_us(body_blocks[i])
                    i += 1

                if i < len(body_blocks) and body_blocks[i].get("type") == "disable":
                    i += 1

                if i < len(body_blocks) and body_blocks[i].get("type") == "inter_pulse_delay":
                    pause_us = self._block_delay_us(body_blocks[i])
                    i += 1
                elif i < len(body_blocks) and body_blocks[i].get("type") == "delay":
                    pause_us = self._block_delay_us(body_blocks[i])
                    i += 1

                add_pause = self._should_add_inter_pulse_pause(body_blocks, i, pause_us)
                self._append_pulse_cycle(pattern, ch, pulse_us, pause_us, add_pause)
                continue

            if btype == "disable":
                pattern.append(
                    self._format_pattern_add_raw(
                        self._intan_raw_word(42, 0x0000, u_flag=True), "R42 OFF"
                    )
                )
                i += 1
                continue

            if btype in ("pulse_duration", "inter_pulse_delay", "delay"):
                i += 1
                continue

            i += 1

        return pattern, has_polarity

    def generate_pattern_commands(self):
        """Генерирует слоты для STM32: PATTERN_ADD_RAW + PATTERN_ADD_DELAY_US."""
        pre_setup = []
        pattern = []
        step_size_ua = 1
        channel = 0
        current_ua = 180
        has_current = False
        has_polarity = False

        for block in self.pattern_blocks:
            if block.get("type") == "step_size":
                step_size_ua = block["data"].get("step_size_ua", 1)
                step_val = int(block["data"].get("step_size_hex", "0x00E2"), 0)
                pre_setup.append(
                    self._format_pattern_add_raw(
                        self._intan_raw_word(34, step_val, u_flag=False),
                        f"R34 step {step_size_ua} µA",
                    )
                )
                pre_setup.append(
                    self._format_pattern_add_raw(
                        self._intan_raw_word(35, 0x00AA, u_flag=False), "R35 bias"
                    )
                )
            elif block["type"] == "current":
                has_current = True
                channel = int(block["data"].get("channel", 0))
                pos_current_ua = float(block["data"].get("pos_current", 0) or 0)
                neg_current_ua = float(block["data"].get("neg_current", 0) or 0)
                current_ua = int(max(pos_current_ua, neg_current_ua))
                if step_size_ua > 0:
                    pos_reg = max(0, min(255, int(pos_current_ua / step_size_ua))) if pos_current_ua > 0 else 0
                    neg_reg = max(0, min(255, int(neg_current_ua / step_size_ua))) if neg_current_ua > 0 else 0
                else:
                    pos_reg = max(0, min(255, int(pos_current_ua)))
                    neg_reg = max(0, min(255, int(neg_current_ua)))
                if neg_current_ua > 0:
                    pre_setup.append(
                        self._format_pattern_add_raw(
                            self._intan_raw_word(64 + channel, 0x8000 | neg_reg, u_flag=False),
                            f"R{64 + channel} neg {neg_current_ua} µA",
                        )
                    )
                if pos_current_ua > 0:
                    pre_setup.append(
                        self._format_pattern_add_raw(
                            self._intan_raw_word(96 + channel, 0x8000 | pos_reg, u_flag=False),
                            f"R{96 + channel} pos {pos_current_ua} µA",
                        )
                    )

        if not has_current:
            try:
                channel, current_ua = self._parse_pattern_example_params()
            except ValueError:
                channel, current_ua = 0, 180
            pre_setup.extend(self.build_stim_current_writes(channel, current_ua))

        body_pattern, body_has_polarity = self._generate_pattern_body_from_blocks(channel)
        pattern.extend(body_pattern)
        has_polarity = has_polarity or body_has_polarity

        for block in self.pattern_blocks:
            if block.get("type") == "comment":
                text = block["data"].get("text", "")
                if text:
                    pattern.append(f"# {text}")

        if pattern and not has_polarity:
            pol_mask = 1 << channel
            raw_pol = self._intan_raw_word(44, pol_mask, u_flag=False)
            pattern.insert(0, self._format_pattern_add_raw(raw_pol, f"R44 ch{channel} (auto)"))

        return pre_setup, pattern

    def generate_pattern_from_blocks(self):
        """Генерирует паттерн из блоков и копирует в текстовый редактор"""
        pre_setup, pattern = self.generate_pattern_commands()
        try:
            channel, current_ua = self._parse_pattern_example_params()
        except ValueError:
            channel, current_ua = 0, 180
        pattern_text = self.format_guide_pattern_text(
            channel, current_ua, pre_setup_lines=pre_setup, pattern_lines=pattern
        )
        if hasattr(self, "txt_pattern"):
            self.txt_pattern.delete("1.0", tk.END)
            self.txt_pattern.insert("1.0", pattern_text)
            self.on_pattern_text_change()
        self.log("Паттерн сгенерирован из блоков", "success")
    
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
        """Загружает пример паттерна из блоков (PATTERN по гайду; ток — в PRESETUP)."""
        try:
            channel, current_ua = self._parse_pattern_example_params()
        except ValueError as e:
            messagebox.showerror("Ошибка", str(e))
            return

        self.pattern_blocks = []
        self.add_pattern_block("polarity")
        self.pattern_blocks[-1]["data"] = {"channel": channel, "positive": True}
        self.add_pattern_block("enable")
        self.pattern_blocks[-1]["data"] = {"channel": channel}
        self.add_pattern_block("pulse_duration")
        self.pattern_blocks[-1]["data"] = {"delay_us": 100}
        self.add_pattern_block("disable")
        self.pattern_blocks[-1]["data"] = {"channel": channel}
        self.add_pattern_block("inter_pulse_delay")
        self.pattern_blocks[-1]["data"] = {"delay_us": 100}
        
        self.update_blocks_display()
        self.update_pattern_preview()
        self.generate_pattern_from_blocks()
        self.log(
            f"Загружен пример: канал {channel}, {current_ua} µA (скопирован в текстовый редактор)",
            "info",
        )


def main():
    app = IntanGuiApp()
    app.mainloop()


if __name__ == "__main__":
    main()
