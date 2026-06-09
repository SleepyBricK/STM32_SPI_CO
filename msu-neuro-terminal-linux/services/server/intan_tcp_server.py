#!/usr/bin/env python3
"""
TCP-сервер для управления стимуляцией Intan RHS2116 из GUI/клиента.

Протокол: одна команда в строке, формат JSON, ответ тоже JSON одной строкой.

Примеры команд:
  {"cmd": "ping"}
  {"cmd": "pulse", "channels": "0-3", "neg": 0, "pos": 20}
  {"cmd": "sawtooth", "channels": "0,1", "pos": 50, "steps": 50, "duration": 0.001}
  {"cmd": "stop"}

Запуск сервера:
  python3 intan_tcp_server.py --port 9000
  
  (После настройки прав через setup_permissions.sh sudo не требуется)
"""

import argparse
import json
import math
import os
import socket
import socketserver
import subprocess
import threading
import time
import sys

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

from intan_impedance import rhs2116_safe_impedance_commands, run_rhs2116_sequence
from stimulate_channel0 import (
    GPIOController,
    GPIOError,
    SPIController,
    initialize_intan_chip,
    setup_stimulation_channels,
    clear_compliance_monitor,
    enable_stimulation_channels,
    set_stimulation_current,
    poll_register_until_ready,
    parse_channels,
    read_intan_register,
    write_intan_register,
    clear_adc,
    convert_intan,
    _is_usb_backend,
)
from intan_usb_transport import (
    encode_intan_write_raw_word,
    intan_spi_slots_to_us,
)


def _token_um_flag(parts, name, default=0):
    name = name.upper()
    for token in parts[2:]:
        t = token.strip().upper()
        if t == name:
            return 1
        if t.startswith(f"{name}="):
            try:
                return 1 if int(t.split("=", 1)[1], 0) else 0
            except Exception:
                return default
    return default


def parse_write_um_flags(parts):
    """WRITE reg value [U] [M] или WRITE reg value u m (0/1)."""
    if any(
        t.upper() in ("U", "M") or t.upper().startswith(("U=", "M="))
        for t in parts[3:]
    ):
        return _token_um_flag(parts, "U", 0), _token_um_flag(parts, "M", 0)
    if len(parts) >= 5:
        try:
            return int(parts[3], 0), int(parts[4], 0)
        except ValueError:
            pass
    return 0, 0


class IntanController:
    """
    Обертка над существующими функциями стимуляции для использования из TCP-сервера.
    """

    def __init__(
        self,
        gpio_number=226,
        spi_device="/dev/spidev1.1",
        verbose=False,
        transport=None,
        backend="spi",
    ):
        self.gpio_number = gpio_number
        self.spi_device = spi_device
        self.verbose = verbose
        self.transport = transport
        self.backend = backend

        self.gpio = None
        self.spi = None
        self.initialized = False
        
        # Память для хранения паттерна
        self.pattern_commands = []
        self.pattern_loaded = False
        
        # Автоматически экспортируем GPIO при инициализации
        self._ensure_gpio_exported()
        self.lock = threading.Lock()
    
    def _ensure_gpio_exported(self):
        """Убеждается, что GPIO экспортирован (через sudo если нужно)"""
        gpio_path = f"/sys/class/gpio/gpio{self.gpio_number}"
        if not os.path.exists(gpio_path):
            try:
                export_script = "/home/admin/export_gpio.sh"
                if os.path.exists(export_script):
                    result = subprocess.run(
                        ["sudo", export_script, str(self.gpio_number)],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                else:
                    result = subprocess.run(
                        ["sudo", "sh", "-c", f"echo {self.gpio_number} > /sys/class/gpio/export"],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                if result.returncode == 0 and self.verbose:
                    print(f"  GPIO {self.gpio_number} экспортирован через sudo")
            except Exception as e:
                if self.verbose:
                    print(f"  Предупреждение: не удалось автоматически экспортировать GPIO: {e}")
                # Не критично, попробуем позже при инициализации

    def _log(self, msg):
        if self.verbose:
            print(msg)

    def _run_rhs2116_sequence(self, commands):
        run_rhs2116_sequence(
            self.spi,
            commands,
            read_register=read_intan_register,
            write_register=write_intan_register,
            clear_adc=clear_adc,
            clear_compliance=clear_compliance_monitor,
            sleep_fn=time.sleep,
        )

    def ensure_chip_ready(self):
        """
        Быстрая проверка доступа к чипу (без полной инициализации стимуляции).
        Для read_register / ping GUI — не блокирует на десятки WRITE.
        """
        with self.lock:
            if self.backend == "usb":
                if self.transport is None:
                    raise RuntimeError("USB backend requires transport")
                self.spi = self.transport
                self.transport.verify_chip()
                return

            if self.initialized:
                return

            chip_id = read_intan_register(
                self._open_spi_for_probe(), 255, verbose=False
            )
            if chip_id != 32:
                raise RuntimeError(
                    f"Intan не отвечает: регистр 255 = {chip_id} (ожидалось 32)"
                )

    def _open_spi_for_probe(self):
        """Открывает SPI только для проверки chip ID (legacy backend)."""
        if self.spi is not None:
            return self.spi
        if not os.path.exists(self.spi_device):
            raise FileNotFoundError(f"SPI устройство не найдено: {self.spi_device}")
        self.gpio = GPIOController(self.gpio_number, raise_exceptions=True)
        self.gpio.set_direction("out")
        self.gpio.set_value(1)
        time.sleep(0.1)
        self.spi = SPIController(
            device=self.spi_device,
            max_speed_hz=10000000,
            mode=0,
        )
        self.spi.open()
        return self.spi

    def ensure_initialized(self):
        """
        Ленивая инициализация GPIO, SPI и чипа Intan.
        Вызывается перед любой командой, требующей доступа к чипу.
        """
        with self.lock:
            if self.initialized:
                return

            REG_ADDR = 255
            EXPECTED_VALUE = 32  # Chip ID для RHS2116

            self._log("== Инициализация Intan для TCP-сервера ==")

            try:
                if self.backend == "usb":
                    if self.transport is None:
                        raise RuntimeError("USB backend requires transport")
                    self.spi = self.transport
                    self._log("Backend: USB STM32 coprocessor")
                    self._log("[1/3] Проверка USB и chip ID...")
                    self.transport.verify_chip()
                    self._log("[2/3] Инициализация регистров для стимуляции...")
                    self._initialize_for_stimulation(verbose=self.verbose)
                    self._log("[3/3] Очистка compliance monitor...")
                    clear_compliance_monitor(self.spi, verbose=self.verbose)
                    self.initialized = True
                    self._log("== Инициализация завершена успешно ==")
                    return

                self._log(f"GPIO PH2: {self.gpio_number}")
                self._log(f"SPI устройство: {self.spi_device}")

                # Инициализация GPIO
                self._log("[1/6] Настройка GPIO...")
                self.gpio = GPIOController(self.gpio_number, raise_exceptions=True)
                self.gpio.set_direction("out")
                self._log(f"      GPIO {self.gpio_number} настроен как выход")

                # Включаем питание
                self._log("[2/6] Включение питания Intan (PH2 = 1)...")
                self.gpio.set_value(1)
                time.sleep(0.1)  # Даем время на включение питания
                gpio_value = self.gpio.get_value()
                self._log(f"      Питание включено, GPIO значение: {gpio_value}")

                # Инициализация SPI
                self._log("[3/6] Инициализация SPI...")
                if not os.path.exists(self.spi_device):
                    raise FileNotFoundError(f"SPI устройство не найдено: {self.spi_device}")
                
                self.spi = SPIController(
                    device=self.spi_device,
                    max_speed_hz=10000000,
                    mode=0,
                )
                self.spi.open()
                self._log(
                    f"      SPI настроен: скорость {self.spi.max_speed_hz/1e6:.1f} МГц, режим {self.spi.mode}"
                )

                # Проверка инициализации
                self._log("[4/6] Проверка инициализации Intan...")
                success, reg_value, attempts, elapsed = poll_register_until_ready(
                    spi=self.spi,
                    reg_addr=REG_ADDR,
                    expected_value=EXPECTED_VALUE,
                    timeout=10.0,  # Увеличиваем таймаут
                    poll_interval=0.01,
                    verbose=self.verbose,
                )
                if not success:
                    raise RuntimeError(
                        f"Intan не инициализирован: регистр {REG_ADDR} = {reg_value} (ожидалось {EXPECTED_VALUE})"
                    )
                self._log(f"      Intan обнаружен (регистр {REG_ADDR} = {reg_value})")

                # Инициализация регистров чипа для стимуляции
                # По аналогии с StimulationWithPreset.py: полная инициализация с установкой регистров 32-33
                self._log("[5/6] Инициализация регистров чипа для стимуляции...")
                self._initialize_for_stimulation(verbose=self.verbose)
                self._log("      Регистры чипа инициализированы (регистры 32-33 установлены в 0xAAAA/0x00FF)")

                # Очистка compliance monitor
                self._log("[6/6] Очистка compliance monitor...")
                clear_compliance_monitor(self.spi, verbose=self.verbose)
                self._log("      Compliance monitor очищен")

                self.initialized = True
                self._log("== Инициализация завершена успешно ==")
            except GPIOError as e:
                self._log(f"❌ Ошибка GPIO: {e}")
                try:
                    if self.spi is not None:
                        self.spi.close()
                except:
                    pass
                self.spi = None
                self.gpio = None
                self.initialized = False
                raise RuntimeError(f"Ошибка GPIO: {e}") from e
            except Exception as e:
                self._log(f"❌ Ошибка инициализации: {e}")
                import traceback
                if self.verbose:
                    self._log(traceback.format_exc())
                try:
                    if self.spi is not None:
                        self.spi.close()
                except:
                    pass
                self.spi = None
                self.gpio = None
                self.initialized = False
                raise  # Пробрасываем исключение дальше

    def _initialize_for_stimulation(self, verbose=False):
        """
        Инициализация чипа для стимуляции по аналогии с StimulationWithPreset.py.
        
        ВАЖНО: Согласно даташиту (May 2021), питание VSTIM должно быть не более ±7V
        (максимум 14V между VSTIM+ и VSTIM-). Использование ±9V может вызвать
        деградацию анодных генераторов тока после многих импульсов стимуляции.
        
        Последовательность:
        1. READ 255 (dummy команда)
        2. WRITE 32/33 0x0000 (отключить стимуляцию)
        3. Инициализация регистров 0-48
        4. Инициализация регистров 64-79 и 96-111 (токи стимуляции) в 0x8000
        5. WRITE 32 0xAAAA, WRITE 33 0x00FF (разрешить работу стимуляторов)
        6. READ 255 U=0 M=1 (очистка compliance monitor)
        
        Register 36 (Vrecov): 0x0080 = 0V (8-bit DAC, 128 = 0V, диапазон ±1.22V)
        Register 37 (Imax): 0x4F00 = 1 nA (sel1=0, sel2=30, sel3=2 согласно даташиту)
        """
        spi = self.spi
        
        # 1. READ 255 U=0 M=0 - dummy команда
        if verbose:
            self._log("      READ 255 (dummy команда)...")
        read_intan_register(spi, 255, verbose=False)
        time.sleep(0.001)
        
        # 2. WRITE 32/33 0x0000 - отключить стимуляцию
        if verbose:
            self._log("      WRITE 32 0x0000, WRITE 33 0x0000 - отключить стимуляцию...")
        write_intan_register(spi, 32, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(spi, 33, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 3. Инициализация основных регистров (как в StimulationWithPreset.py)
        if verbose:
            self._log("      Инициализация основных регистров...")
        
        # WRITE 38 0xFFFF - включить DC-coupled amplifiers
        write_intan_register(spi, 38, 0xFFFF, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # CLEAR - инициализация ADC
        clear_adc(spi, verbose=False)
        time.sleep(0.001)
        
        # WRITE 0 0x00C5 - настройка ADC (480 kS/s)
        write_intan_register(spi, 0, 0x00C5, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 1 - auxiliary outputs и DSP фильтр
        # В StimulationWithPreset используется 0x051A (DSP cutoff = 0)
        # Для стимуляции используем оригинальное значение из StimulationWithPreset
        write_intan_register(spi, 1, 0x051A, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # КРИТИЧНО: НЕ включаем impedance DAC при инициализации
        # Register 2 и 3 будут включены только при измерении импеданса
        # WRITE 2 0x0000 - отключить DAC для impedance testing (по умолчанию)
        write_intan_register(spi, 2, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 3 0x0000 - отключить impedance check DAC (по умолчанию)
        write_intan_register(spi, 3, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 4 0x0016 - верхняя частота среза (7.5 kHz)
        write_intan_register(spi, 4, 0x0016, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 5 0x0017 - нижняя частота среза (5 Hz)
        write_intan_register(spi, 5, 0x0017, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 6 0x00A8 - нижняя частота среза (5 Hz)
        write_intan_register(spi, 6, 0x00A8, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 7 0x000A - альтернативная нижняя частота среза (1000 Hz)
        write_intan_register(spi, 7, 0x000A, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 8 0xFFFF - включить AC-coupled amplifiers
        write_intan_register(spi, 8, 0xFFFF, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # ВАЖНО: Register 9 НЕ устанавливается в StimulationWithPreset.py
        # Не устанавливаем Register 9 для соответствия оригинальной инициализации
        
        # WRITE 10 0x0000 U=1 - отключить fast settle
        write_intan_register(spi, 10, 0x0000, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 12 0xFFFF U=1 - установить нижнюю частоту среза
        write_intan_register(spi, 12, 0xFFFF, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 34 0x00E2 - шаг стимуляции 1 µA
        write_intan_register(spi, 34, 0x00E2, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 35 0x00AA - напряжения смещения для шага 1 µA
        write_intan_register(spi, 35, 0x00AA, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 36 0x0080 - целевое напряжение charge recovery (0 V)
        write_intan_register(spi, 36, 0x0080, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # WRITE 37 0x4F00 - лимит тока charge recovery (1 nA)
        write_intan_register(spi, 37, 0x4F00, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # КРИТИЧНО: Записи в triggered регистры (44, 46, 48, 64-79, 96-111) БЕЗ U-флага
        # Все изменения накапливаются в shadow-RAM и применяются только при записи в Register 42 с U=1
        
        # WRITE 44 0x0000 U=0 - установить отрицательную полярность (накапливаем в shadow-RAM)
        write_intan_register(spi, 44, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)

        # WRITE 46/48 0x0000 U=0 - открыть charge recovery switch и отключить CL recovery.
        # На power-up эти triggered-регистры неопределенные; если их не сбросить, канал может шунтироваться.
        write_intan_register(spi, 46, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(spi, 48, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 4. Инициализация регистров токов стимуляции (64-79 и 96-111) в 0x8000:
        # magnitude=0, trim=128 (центральная калибровка по даташиту).
        # КРИТИЧНО: БЕЗ U-флага - накапливаем в shadow-RAM
        if verbose:
            self._log("      Инициализация регистров токов стимуляции (64-79, 96-111) в 0x8000 (без U-флага)...")
        for channel in range(16):
            write_intan_register(spi, 64 + channel, 0x8000, u_flag=0, m_flag=0, verbose=False)
            write_intan_register(spi, 96 + channel, 0x8000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # КРИТИЧНО: Применяем все накопленные изменения через Register 42 с U=1
        # Это единственное место, где используется U=1 для применения triggered регистров
        if verbose:
            self._log("      Применение всех накопленных изменений через Register 42 (U=1)...")
        write_intan_register(spi, 42, 0x0000, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 5. WRITE 32 0xAAAA, WRITE 33 0x00FF - разрешить работу стимуляторов
        if verbose:
            self._log("      WRITE 32 0xAAAA, WRITE 33 0x00FF - разрешить работу стимуляторов...")
        write_intan_register(spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 6. READ 255 U=0 M=1 - очистка compliance monitor
        if verbose:
            self._log("      READ 255 U=0 M=1 - очистка compliance monitor...")
        clear_compliance_monitor(spi, verbose=False)
        time.sleep(0.001)
        
        if verbose:
            self._log("      ✓ Инициализация для стимуляции завершена")

    # --- Высокоуровневые операции, вызываемые из TCP-обработчика ---

    def pulse(self, channels_str, neg_current, pos_current, pulse_duration=0.001, inter_pulse_delay=0.001, repeat_count=1):
        """
        Выполняет один или несколько коротких импульсов (биполярный или монополярный) для заданных каналов.
        
        Args:
            channels_str: строка с каналами (например, "0,1,2" или "0-3")
            neg_current: отрицательный ток в µA (0-255)
            pos_current: положительный ток в µA (0-255)
            pulse_duration: длительность одного импульса в секундах (по умолчанию 0.001 = 1 мс)
            inter_pulse_delay: задержка между импульсами в секундах (по умолчанию 0.001 = 1 мс)
            repeat_count: количество повторений (по умолчанию 1)
        """
        try:
            self.ensure_initialized()
        except Exception as e:
            raise RuntimeError(f"Ошибка инициализации: {e}")
        
        channels = parse_channels(channels_str)

        # Настраиваем токи
        setup_stimulation_channels(
            spi=self.spi,
            channels=channels,
            neg_current_magnitude=neg_current,
            pos_current_magnitude=pos_current,
            step_size_1ua=True,
            verbose=self.verbose,
        )

        for rep in range(repeat_count):
            # Биполярная или монополярная стимуляция
            if neg_current > 0 and pos_current > 0:
                # Сначала отрицательный импульс
                enable_stimulation_channels(
                    self.spi, channels, enable=True, negative_polarity=True, verbose=False
                )
                time.sleep(pulse_duration)
                enable_stimulation_channels(
                    self.spi, channels, enable=False, negative_polarity=True, verbose=False
                )
                time.sleep(inter_pulse_delay)

                # Затем положительный импульс
                enable_stimulation_channels(
                    self.spi, channels, enable=True, negative_polarity=False, verbose=False
                )
                time.sleep(pulse_duration)
                enable_stimulation_channels(
                    self.spi, channels, enable=False, negative_polarity=False, verbose=False
                )
            else:
                # Монополярная стимуляция
                polarity = neg_current > 0  # True для отрицательной
                current = neg_current if neg_current > 0 else pos_current
                enable_stimulation_channels(
                    self.spi, channels, enable=True, negative_polarity=polarity, verbose=self.verbose
                )
                time.sleep(pulse_duration)
                enable_stimulation_channels(
                    self.spi, channels, enable=False, negative_polarity=polarity, verbose=self.verbose
                )
            
            # Задержка между повторениями (кроме последнего)
            if rep < repeat_count - 1:
                time.sleep(inter_pulse_delay)
        
        # КРИТИЧНО: Сброс DSP HPF после стимуляции для быстрого восстановления baseline
        # Согласно даташиту: "Each channel's DSP high-pass filter can be instantly reset
        # by setting the H flag of the CONVERT command to one."
        if self.verbose:
            self._log("      Сброс DSP HPF после стимуляции...")
        self._reset_dsp_hpf(channels)

    def sawtooth(self, channels_str, pos_current, steps, duration):
        """
        Выполняет один цикл пилообразной стимуляции для заданных каналов.

        pos_current: максимальный ток (µA)
        steps: количество шагов
        duration: длительность одного цикла (сек)
        """
        try:
            self.ensure_initialized()
        except Exception as e:
            raise RuntimeError(f"Ошибка инициализации: {e}")
        
        channels = parse_channels(channels_str)

        if steps <= 0 or duration <= 0:
            raise ValueError("steps и duration должны быть > 0")

        # Настраиваем токи: отрицательный 0, положительный pos_current (будет изменяться)
        setup_stimulation_channels(
            spi=self.spi,
            channels=channels,
            neg_current_magnitude=0,
            pos_current_magnitude=0,
            step_size_1ua=True,
            verbose=self.verbose,
        )

        # Включаем стимуляторы с положительной полярностью
        enable_stimulation_channels(
            self.spi, channels, enable=True, negative_polarity=False, verbose=self.verbose
        )

        step_delay = duration / steps
        start = time.time()

        # Пила: от 0 до максимума
        # КРИТИЧНО: Изменения токов накапливаются в shadow-RAM без U-флага
        # Нужно применять их через Register 42 с U=1 после каждого изменения
        for step in range(steps + 1):
            current = int((pos_current * step) / steps)
            for ch in channels:
                set_stimulation_current(
                    self.spi, current_value=current, channel=ch, is_positive=True, verbose=False
                )
            # КРИТИЧНО: Применяем накопленные изменения токов через Register 42 с U=1
            # Читаем текущее значение Register 42 и записываем обратно с U=1
            current_reg42 = read_intan_register(self.spi, 42, verbose=False)
            write_intan_register(self.spi, 42, current_reg42, u_flag=1, m_flag=0, verbose=False)
            # При желании можно чуть выровнять по времени, но для максимальной скорости можно без задержки
            # time.sleep(step_delay)

        elapsed = time.time() - start
        self._log(
            f"TCP: sawtooth cycle completed, requested {duration*1000:.3f} ms, actual {elapsed*1000:.3f} ms"
        )

        # Отключаем стимуляторы
        enable_stimulation_channels(
            self.spi, channels, enable=False, negative_polarity=False, verbose=self.verbose
        )
        
        # КРИТИЧНО: Сброс DSP HPF после стимуляции для быстрого восстановления baseline
        if self.verbose:
            self._log("      Сброс DSP HPF после стимуляции...")
        self._reset_dsp_hpf(channels)

    def _reset_dsp_hpf(self, channels):
        """
        Сбрасывает DSP high-pass filter для указанных каналов.
        
        КРИТИЧНО: После стимуляции DSP HPF может быть насыщен остаточными DC-смещениями.
        Сброс через H flag мгновенно восстанавливает baseline.
        
        Согласно даташиту: "Each channel's DSP high-pass filter can be instantly reset
        by setting the H flag of the CONVERT command to one."
        
        Args:
            channels: список каналов для сброса DSP HPF
        """
        if not self.spi:
            return

        for channel in channels:
            convert_intan(self.spi, channel, amp_type="ac", h_flag=1, verbose=False)

        time.sleep(0.001)

    STM32_PATTERN_MAX_SLOTS = 1024

    def load_pattern(self, pattern_commands):
        """
        Загружает паттерн команд в память устройства без выполнения.
        Для USB/STM32 backend: intan_pattern.c (до 1024 слотов RAM).
        PATTERN_ADD_RAW = 1 SPI-слот; PATTERN_ADD_WRITE/READ = 3 слота.
        
        Args:
            pattern_commands: список команд в формате:
                - "PATTERN_ADD_RAW <word>" / "PATTERN_ADD_DELAY_US <us>"
                - "WRITE reg_addr value [U] [M]" — конвертируется в 1 RAW-слот
                - legacy: DELAY / DELAY_US / READ / CLEAR
        
        Returns:
            количество загруженных слотов (USB) или команд (legacy)
        """
        if self.backend != "usb" and len(pattern_commands) > 200:
            raise ValueError("Максимум 200 команд в паттерне")
        
        validated_commands = []
        for cmd_line in pattern_commands:
            cmd_line = cmd_line.strip()
            if not cmd_line or cmd_line.startswith('#'):
                continue  # Пропускаем пустые строки и комментарии
            
            # Базовая валидация синтаксиса
            parts = cmd_line.split()
            if not parts:
                continue
            
            cmd_type = parts[0].upper()
            if cmd_type not in [
                "WRITE", "READ", "CLEAR", "DELAY", "DELAY_US",
                "PATTERN_ADD_RAW", "PATTERN_ADD_WRITE", "PATTERN_ADD_READ",
                "PATTERN_ADD_CLEAR_ADC", "PATTERN_ADD_CLEAR_COMP",
                "PATTERN_ADD_DELAY_US", "PATTERN_ADD_DELAY_CYC",
                "PATTERN_CLEAR", "PATTERN_STATUS",
            ]:
                raise ValueError(f"Неизвестная команда: {cmd_type}")
            
            validated_commands.append(cmd_line)

        if self.backend == "usb":
            if self.spi is None:
                self.ensure_initialized()
            if self.spi is None or not hasattr(self.spi, "pattern_clear"):
                raise RuntimeError("USB STM32 firmware не поддерживает PATTERN_* команды")

            self._prepare_pattern_load_state()
            self.spi.pattern_clear()
            loaded_count = 0

            def _reserve_slots(needed: int):
                if loaded_count + needed > self.STM32_PATTERN_MAX_SLOTS:
                    raise ValueError(
                        f"Паттерн не помещается в RAM STM32: "
                        f"{loaded_count + needed} слотов (максимум {self.STM32_PATTERN_MAX_SLOTS})"
                    )

            for cmd_line in validated_commands:
                parts = cmd_line.split()
                cmd_type = parts[0].upper()

                if cmd_type in ("PATTERN_CLEAR", "PATTERN_STATUS"):
                    continue
                if cmd_type == "PATTERN_ADD_RAW":
                    if len(parts) < 2:
                        raise ValueError(f"PATTERN_ADD_RAW требует слово: {cmd_line}")
                    _reserve_slots(1)
                    self.spi.pattern_add_raw_word(int(parts[1], 0))
                    loaded_count += 1
                elif cmd_type == "PATTERN_ADD_DELAY_US":
                    if len(parts) < 2:
                        raise ValueError(f"PATTERN_ADD_DELAY_US требует µs: {cmd_line}")
                    delay_us = int(parts[1], 0)
                    if delay_us > 0:
                        _reserve_slots(1)
                        self.spi.pattern_add_delay_us(delay_us)
                        loaded_count += 1
                elif cmd_type == "PATTERN_ADD_DELAY_CYC":
                    if len(parts) < 2:
                        raise ValueError(f"PATTERN_ADD_DELAY_CYC требует cycles: {cmd_line}")
                    cycles = int(parts[1], 0)
                    if cycles > 0:
                        _reserve_slots(1)
                        self.spi.pattern_add_delay_cycles(cycles)
                        loaded_count += 1
                elif cmd_type == "PATTERN_ADD_WRITE":
                    if len(parts) < 3:
                        raise ValueError(f"PATTERN_ADD_WRITE требует reg value: {cmd_line}")
                    reg_addr = int(parts[1], 0)
                    value = int(parts[2], 0)
                    u_flag, m_flag = parse_write_um_flags(parts)
                    _reserve_slots(3)
                    self.spi.pattern_add_write(
                        reg_addr, value, u_flag=u_flag, m_flag=m_flag
                    )
                    loaded_count += 3
                elif cmd_type == "PATTERN_ADD_READ":
                    if len(parts) < 2:
                        raise ValueError(f"PATTERN_ADD_READ требует регистр: {cmd_line}")
                    _reserve_slots(3)
                    self.spi.pattern_add_read(int(parts[1], 0))
                    loaded_count += 3
                elif cmd_type == "PATTERN_ADD_CLEAR_ADC":
                    _reserve_slots(3)
                    self.spi.pattern_add_clear_adc()
                    loaded_count += 3
                elif cmd_type == "PATTERN_ADD_CLEAR_COMP":
                    _reserve_slots(3)
                    self.spi.pattern_add_clear_comp()
                    loaded_count += 3
                elif cmd_type == "WRITE":
                    if len(parts) < 3:
                        raise ValueError(f"WRITE требует регистр и значение: {cmd_line}")
                    reg_addr = int(parts[1], 0)
                    value = int(parts[2], 0)
                    u_flag, m_flag = parse_write_um_flags(parts)
                    raw_word = encode_intan_write_raw_word(
                        reg_addr, value, u_flag=u_flag, m_flag=m_flag
                    )
                    _reserve_slots(1)
                    self.spi.pattern_add_raw_word(raw_word)
                    loaded_count += 1
                elif cmd_type == "READ":
                    if len(parts) < 2:
                        raise ValueError(f"READ требует регистр: {cmd_line}")
                    _reserve_slots(3)
                    self.spi.pattern_add_read(int(parts[1], 0))
                    loaded_count += 3
                elif cmd_type == "CLEAR":
                    _reserve_slots(3)
                    self.spi.pattern_add_clear_adc()
                    loaded_count += 3
                elif cmd_type == "DELAY_US":
                    if len(parts) < 2:
                        raise ValueError(f"DELAY_US требует микросекунды: {cmd_line}")
                    delay_us = int(parts[1], 0)
                    if delay_us < 0:
                        raise ValueError(f"DELAY_US должен быть >= 0: {cmd_line}")
                    if delay_us > 0:
                        _reserve_slots(1)
                        self.spi.pattern_add_delay_us(delay_us)
                        loaded_count += 1
                elif cmd_type == "DELAY":
                    if len(parts) < 2:
                        raise ValueError(f"DELAY требует количество шагов: {cmd_line}")
                    delay_slots = int(parts[1], 0)
                    if delay_slots < 0:
                        raise ValueError(f"DELAY должен быть >= 0: {cmd_line}")
                    delay_us = intan_spi_slots_to_us(delay_slots)
                    if delay_us > 0:
                        _reserve_slots(1)
                        self.spi.pattern_add_delay_us(delay_us)
                        loaded_count += 1

            # Safety OFF: PATTERN_ADD_WRITE R42=0 U=1 (3 CS), если не задан в паттерне.
            if not self._pattern_ends_with_r42_safety_off(validated_commands):
                _reserve_slots(3)
                self.spi.pattern_add_write(42, 0x0000, u_flag=1, m_flag=0)
                loaded_count += 3

            self.pattern_commands = validated_commands
            self.pattern_loaded = True
            self._log(
                f"✓ Паттерн загружен в STM32: {loaded_count} команд, "
                f"status={self.spi.pattern_status()}"
            )
            return loaded_count
        
        self.pattern_commands = validated_commands
        self.pattern_loaded = True
        self._log(f"✓ Паттерн загружен в память: {len(validated_commands)} команд")
        
        return len(validated_commands)

    def _prepare_pattern_load_state(self):
        """§1 prep перед pattern_load (intan_pi_gui_guide): INIT_STIM, safe OFF, CLEAR_COMP."""
        if not self.spi:
            return
        if hasattr(self.spi, "stop_stream"):
            self.spi.stop_stream()
        if hasattr(self.spi, "init_stim"):
            self.spi.init_stim()
        else:
            write_intan_register(self.spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            write_intan_register(self.spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
        write_intan_register(self.spi, 42, 0x0000, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        clear_compliance_monitor(self.spi, verbose=False)
        time.sleep(0.001)

    @staticmethod
    def _pattern_ends_with_r42_safety_off(cmd_lines):
        """Паттерн уже содержит надёжный OFF R42 (PATTERN_ADD_WRITE 42 0 …)."""
        if not cmd_lines:
            return False
        parts = cmd_lines[-1].strip().split()
        if len(parts) < 4 or parts[0].upper() != "PATTERN_ADD_WRITE":
            return False
        try:
            reg = int(parts[1], 0)
            value = int(parts[2], 0)
        except ValueError:
            return False
        return reg == 42 and value == 0

    def _prepare_pattern_runtime_state(self):
        """Unlock стима перед PATTERN_RUN: R32/R33, clear_comp, R34/R35."""
        if not self.spi:
            return
        reg32 = read_intan_register(self.spi, 32, verbose=False)
        reg33 = read_intan_register(self.spi, 33, verbose=False)
        if reg32 != 0xAAAA or reg33 != 0x00FF:
            if self.verbose:
                self._log(
                    f"⚠ Регистры 32-33 не готовы для стима "
                    f"(0x{reg32:04X}/0x{reg33:04X}), unlock..."
                )
            write_intan_register(self.spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            write_intan_register(self.spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            clear_compliance_monitor(self.spi, verbose=False)
            time.sleep(0.001)

        reg34 = read_intan_register(self.spi, 34, verbose=False)
        if reg34 != 0x00E2:
            if self.verbose:
                self._log(f"⚠ Register 34 = 0x{reg34:04X}, устанавливаем 0x00E2 (1 µA step)...")
            write_intan_register(self.spi, 34, 0x00E2, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
        reg35 = read_intan_register(self.spi, 35, verbose=False)
        if reg35 != 0x00AA:
            if self.verbose:
                self._log(f"⚠ Register 35 = 0x{reg35:04X}, устанавливаем 0x00AA...")
            write_intan_register(self.spi, 35, 0x00AA, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)

    def run_pattern_from_memory(self, repeat_count=1):
        """
        Выполняет паттерн из памяти устройства.
        Паттерн должен быть предварительно загружен через load_pattern().
        
        Args:
            repeat_count: количество повторений паттерна (по умолчанию 1)
        
        Returns:
            список результатов выполнения команд (только для последнего повторения)
        """
        if not self.pattern_loaded or not self.pattern_commands:
            raise RuntimeError("Паттерн не загружен в память. Используйте pattern_load сначала.")

        if self.backend == "usb":
            if self.spi is None or not hasattr(self.spi, "pattern_run"):
                raise RuntimeError("USB STM32 firmware не поддерживает PATTERN_RUN")
            if self.spi is None:
                self.ensure_initialized()
            self._prepare_pattern_runtime_state()
            run_timeout_ms = max(60_000, int(repeat_count) * 500)
            self._log(
                f"▶ Запуск STM32 PATTERN_RUN: repeat_count={repeat_count}, "
                f"timeout_ms={run_timeout_ms}"
            )
            reply = self.spi.pattern_run(
                repeat_count=repeat_count, timeout_ms=run_timeout_ms
            )
            self._log(f"✓ STM32 PATTERN_RUN завершен: {reply}")
            try:
                write_intan_register(self.spi, 42, 0x0000, u_flag=1, m_flag=0, verbose=False)
            except Exception as exc:
                self._log(f"⚠ Не удалось выключить R42 после паттерна: {exc}")
            # Не вызываем INIT_STIM сразу после стима — на осциллографе это даёт
            # артефакт/удлинение последнего импульса; resync — при следующем READ/ID.
            return [{"cmd": "PATTERN_RUN", "repeat_count": repeat_count, "reply": reply, "status": "ok"}]
        
        return self.execute_pattern(self.pattern_commands, repeat_count=repeat_count)

    def stop_all(self):
        """
        Выключает все стимуляторы.
        """
        if not self.initialized or self.spi is None:
            return
        # Пустой список каналов => выключаем все (см. enable_stimulation_channels)
        enable_stimulation_channels(
            self.spi, [], enable=False, negative_polarity=False, verbose=self.verbose
        )

    def execute_pattern(self, pattern_commands, repeat_count=1):
        """
        Выполняет паттерн команд для стимуляции.
        
        Args:
            pattern_commands: список команд в формате:
                - "WRITE reg_addr value [U] [M]" - запись в регистр
                - "READ reg_addr" - чтение регистра
                - "CLEAR" - команда CLEAR
                - "DELAY X" - задержка (X раз READ 255)
            repeat_count: количество повторений паттерна (по умолчанию 1)
        
        Returns:
            список результатов выполнения команд (только для последнего повторения)
        """
        try:
            self.ensure_initialized()
        except Exception as e:
            raise RuntimeError(f"Ошибка инициализации: {e}")
        
        # КРИТИЧНО: Проверяем, что регистры 32-33 установлены для работы стимуляторов
        # Это необходимо для того, чтобы стимуляция работала
        reg32 = read_intan_register(self.spi, 32, verbose=False)
        reg33 = read_intan_register(self.spi, 33, verbose=False)
        if reg32 != 0xAAAA or reg33 != 0x00FF:
            if self.verbose:
                self._log(f"⚠ Регистры 32-33 не установлены для стимуляции (0x{reg32:04X}/0x{reg33:04X}), устанавливаем...")
            write_intan_register(self.spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            write_intan_register(self.spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            clear_compliance_monitor(self.spi, verbose=False)
            time.sleep(0.001)
        
        # КРИТИЧНО: Проверяем, что Register 34 (step size) установлен правильно
        # Согласно даташиту, Register 34 должен быть 0x00E2 для шага 1 µA
        # Без правильного step size токи будут неправильными!
        reg34 = read_intan_register(self.spi, 34, verbose=False)
        if reg34 != 0x00E2:
            if self.verbose:
                self._log(f"⚠ Register 34 (step size) не установлен правильно (0x{reg34:04X}), устанавливаем 0x00E2 (1 µA step)...")
            write_intan_register(self.spi, 34, 0x00E2, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
        
        # КРИТИЧНО: Проверяем, что Register 35 (bias) установлен правильно
        # Согласно даташиту, Register 35 должен быть 0x00AA для шага 1 µA
        reg35 = read_intan_register(self.spi, 35, verbose=False)
        if reg35 != 0x00AA:
            if self.verbose:
                self._log(f"⚠ Register 35 (bias) не установлен правильно (0x{reg35:04X}), устанавливаем 0x00AA (для шага 1 µA)...")
            write_intan_register(self.spi, 35, 0x00AA, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
        
        if repeat_count < 1:
            raise ValueError("repeat_count должен быть >= 1")
        
        results = []
        
        # Выполняем паттерн указанное количество раз
        for repeat_idx in range(repeat_count):
            for i, cmd_line in enumerate(pattern_commands):
                if i >= 200:  # Ограничение на 200 команд
                    break
                
                cmd_line = cmd_line.strip()
                if not cmd_line or cmd_line.startswith('#'):
                    continue  # Пропускаем пустые строки и комментарии
                
                try:
                    parts = cmd_line.split()
                    if not parts:
                        continue
                    
                    cmd_type = parts[0].upper()
                    
                    if cmd_type == "WRITE":
                        # Формат: WRITE reg_addr value [U] [M]
                        if len(parts) < 3:
                            raise ValueError(f"WRITE требует минимум 2 параметра: регистр и значение")
                        reg_addr = int(parts[1], 0)  # Поддержка hex (0x...) и десятичного формата
                        value = int(parts[2], 0)  # Поддержка hex (0x...) и десятичного формата
                        u_flag = 1 if "U" in parts else 0
                        m_flag = 1 if "M" in parts else 0
                        
                        # КРИТИЧНО: Автоматическое преобразование значений для регистров токов стимуляции
                        # Регистры 64-79 (отрицательные токи) и 96-111 (положительные токи)
                        # Согласно даташиту: формат регистров:
                        # - Биты [15:8] = current trim [7:0] (0x80 = 128 = нормальное значение без подстройки, диапазон ±28%)
                        # - Биты [7:0] = current magnitude [7:0] (величина тока, 0-255)
                        # Формат: 0x80XX, где 0x80 = trim (128), XX - значение тока (0-255)
                        # ВАЖНО: Значение в регистре - это МНОЖИТЕЛЬ шага (Register 34), а не прямой ток!
                        # При step size = 1 µA (Register 34 = 0x00E2), значение 1 = 1 µA, значение 10 = 10 µA
                        if (64 <= reg_addr <= 79) or (96 <= reg_addr <= 111):
                            # Проверяем, что Register 34 установлен правильно (0x00E2 для шага 1 µA)
                            reg34_check = read_intan_register(self.spi, 34, verbose=False)
                            if reg34_check != 0x00E2:
                                if self.verbose:
                                    self._log(f"⚠ ВНИМАНИЕ: Register 34 = 0x{reg34_check:04X} (ожидается 0x00E2 для шага 1 µA)!")
                                # Устанавливаем Register 34 в правильное значение
                                write_intan_register(self.spi, 34, 0x00E2, u_flag=0, m_flag=0, verbose=False)
                                time.sleep(0.001)
                                # Устанавливаем Register 35 в правильное значение
                                write_intan_register(self.spi, 35, 0x00AA, u_flag=0, m_flag=0, verbose=False)
                                time.sleep(0.001)
                            
                            # Если значение не в формате 0x80XX, преобразуем его
                            if value < 0x8000 or value > 0x80FF:
                                # Предполагаем, что значение - это ток в µA (0-255)
                                if 0 <= value <= 255:
                                    value = 0x8000 | (value & 0xFF)
                                    if self.verbose:
                                        self._log(f"  Преобразовано значение тока: {value & 0xFF} µA -> 0x{value:04X}")
                                else:
                                    raise ValueError(f"Значение тока должно быть в диапазоне 0-255 µA или в формате 0x80XX, получено: {value}")
                            else:
                                # Значение уже в формате 0x80XX, проверяем его
                                current_ua = value & 0xFF
                                if self.verbose:
                                    self._log(f"  Установка тока: регистр {reg_addr} = 0x{value:04X} (ток = {current_ua} µA при шаге 1 µA)")
                        
                        write_intan_register(self.spi, reg_addr, value, u_flag=u_flag, m_flag=m_flag, verbose=False)
                        
                        # ВАЖНО: Задержка после записи в triggered registers для их применения
                        # Регистры 42, 44, 64-79, 96-111 являются triggered registers и требуют времени на применение
                        if u_flag == 1 or reg_addr in [42, 44] or (64 <= reg_addr <= 79) or (96 <= reg_addr <= 111):
                            time.sleep(0.002)  # Увеличена задержка для гарантированного применения triggered register
                            # Проверяем, что регистр применился (для отладки)
                            if self.verbose and reg_addr in [42, 44]:
                                read_back = read_intan_register(self.spi, reg_addr, verbose=False)
                                if read_back != value:
                                    self._log(f"⚠ ВНИМАНИЕ: Регистр {reg_addr} не применился! Записано: 0x{value:04X}, прочитано: 0x{read_back:04X}")
                        
                        results.append({"cmd": "WRITE", "reg": reg_addr, "value": value, "status": "ok"})
                    
                    elif cmd_type == "READ":
                        # Формат: READ reg_addr
                        if len(parts) < 2:
                            raise ValueError(f"READ требует параметр: адрес регистра")
                        reg_addr = int(parts[1], 0)  # Поддержка hex (0x...) и десятичного формата
                        
                        reg_value = read_intan_register(self.spi, reg_addr, verbose=False)
                        results.append({"cmd": "READ", "reg": reg_addr, "value": reg_value, "status": "ok"})
                    
                    elif cmd_type == "CLEAR":
                        # Формат: CLEAR
                        clear_adc(self.spi, verbose=False)
                        results.append({"cmd": "CLEAR", "status": "ok"})
                    
                    elif cmd_type == "DELAY":
                        # Формат: DELAY X (X раз READ 255)
                        if len(parts) < 2:
                            raise ValueError(f"DELAY требует параметр: количество READ 255")
                        delay_count = int(parts[1])
                        
                        for j in range(delay_count):
                            read_intan_register(self.spi, 255, verbose=False)
                        results.append({"cmd": "DELAY", "count": delay_count, "status": "ok"})
                    
                    else:
                        raise ValueError(f"Неизвестная команда: {cmd_type}")
                        
                except Exception as e:
                    error_msg = f"Ошибка в команде [{i+1}] '{cmd_line}': {e}"
                    results.append({"cmd": cmd_line, "status": "error", "error": str(e)})
            
            # Сохраняем результаты только для последнего повторения
            if repeat_idx < repeat_count - 1:
                results = []  # Очищаем результаты для промежуточных повторений
        
        return results

    def close(self):
        """
        Закрывает SPI. Питание можно оставить включенным.
        """
        try:
            self.stop_all()
        except Exception:
            pass
        if self.backend == "usb":
            self.spi = self.transport
            self.initialized = False
            return
        try:
            if self.spi is not None:
                self.spi.close()
        except Exception:
            pass


class IntanTCPHandler(socketserver.StreamRequestHandler):
    """
    Обработчик TCP-подключений.
    Ожидает JSON-команды по строкам и отвечает JSON-объектами.
    """

    controller: IntanController = None  # будет установлен извне

    def handle(self):
        if not self._send({"status": "ok", "message": "Intan TCP server ready"}):
            return

        try:
            for line in self.rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    cmd_obj = json.loads(line.decode("utf-8"))
                except Exception as e:
                    resp = {"status": "error", "error": f"invalid_json: {e}"}
                    if not self._send(resp):
                        return
                    continue

                try:
                    resp = self.process_command(cmd_obj)
                except Exception as e:
                    resp = {"status": "error", "error": str(e)}
                if not self._send(resp):
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Клиент GUI может закрыть сокет без graceful shutdown.
            return

    def _send(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return False

    def _parse_flag(self, parts, name, default=0):
        """
        Парсит флаг из токенов команды.
        Поддержка форматов:
        - U / M / D / H
        - U=1 / M=0 / D=1 / H=0
        """
        name = name.upper()
        for token in parts[2:]:
            t = token.strip().upper()
            if t == name:
                return 1
            if t.startswith(f"{name}="):
                try:
                    return 1 if int(t.split("=", 1)[1], 0) else 0
                except Exception:
                    return default
        return default

    def _compute_impedance_metrics_from_accumulators(
        self, sin_accum, cos_accum, sample_count, freq_hz, c_farad, samples_per_period
    ):
        """Fixed-point projections from driver -> Z (ohm), v_amp_uv, phase_deg."""
        if sample_count <= 0:
            return 0.0, 0.0, 0.0
        if not samples_per_period or samples_per_period <= 0:
            samples_per_period = sample_count

        sine64 = (
            128, 140, 152, 164, 176, 187, 198, 209, 218, 227, 235, 242, 248, 253, 255, 255,
            255, 255, 253, 248, 242, 235, 227, 218, 209, 198, 187, 176, 164, 152, 140, 128,
            116, 104, 92, 80, 69, 58, 47, 38, 29, 21, 14, 8, 3, 1, 1, 1,
            1, 1, 3, 8, 14, 21, 29, 38, 47, 58, 69, 80, 92, 104, 116, 128,
        )
        phase_basis = []
        period_sum_s = 0.0
        period_sum_c = 0.0
        for phase_idx in range(samples_per_period):
            idx = (phase_idx * len(sine64)) // samples_per_period
            idx = min(idx, len(sine64) - 1)
            s = sine64[idx] - 128
            c = sine64[(idx + 16) & 63] - 128
            phase_basis.append((s, c))
            period_sum_s += s
            period_sum_c += c

        sum_s2 = 0.0
        sum_c2 = 0.0
        sum_sc = 0.0
        for i in range(sample_count):
            s_raw, c_raw = phase_basis[i % samples_per_period]
            s = s_raw * samples_per_period - period_sum_s
            c = c_raw * samples_per_period - period_sum_c
            sum_s2 += s * s
            sum_c2 += c * c
            sum_sc += s * c

        det = (sum_s2 * sum_c2) - (sum_sc * sum_sc)
        if abs(det) <= 1e-9:
            return 0.0, 0.0, 0.0

        rhs_s = float(sin_accum)
        rhs_c = float(cos_accum)
        sin_coeff_basis = ((rhs_s * sum_c2) - (rhs_c * sum_sc)) / det
        cos_coeff_basis = ((sum_s2 * rhs_c) - (sum_sc * rhs_s)) / det

        basis_scale = 127.0 * float(samples_per_period)
        sin_coeff_codes = basis_scale * sin_coeff_basis
        cos_coeff_codes = basis_scale * cos_coeff_basis
        v_amp_uv = 0.195 * (
            (sin_coeff_codes * sin_coeff_codes) + (cos_coeff_codes * cos_coeff_codes)
        ) ** 0.5
        v_dac = 0.6125
        i_amp = 2.0 * math.pi * freq_hz * c_farad * v_dac
        z_ohm = (v_amp_uv * 1e-6 / i_amp) if i_amp > 0 else 0.0
        phase_v_vs_dac_deg = math.degrees(math.atan2(cos_coeff_codes, sin_coeff_codes))
        phase_deg = phase_v_vs_dac_deg - 90.0
        while phase_deg <= -180.0:
            phase_deg += 360.0
        while phase_deg > 180.0:
            phase_deg -= 360.0
        return z_ohm, v_amp_uv, phase_deg

    def _process_measure_impedance_fast(self, cmd_obj):
        """Быстрый замер импеданса (USB или /dev/intan) одной командой GUI->сервер."""
        self.controller.ensure_initialized()
        if not hasattr(self.controller.spi, "measure_impedance_raw"):
            raise RuntimeError(
                "measure_impedance_fast требует backend с measure_impedance_raw (USB или /dev/intan)"
            )

        channel = int(cmd_obj.get("channel", 0))
        freq_hz = float(cmd_obj.get("frequency", 1000))
        scale_str = str(cmd_obj.get("scale", "1 pF")).strip()
        num_averages = max(1, min(1000, int(cmd_obj.get("num_averages", 10))))
        num_samples = max(16, min(2048, int(cmd_obj.get("num_samples", 64))))
        auto_scale = bool(cmd_obj.get("auto_scale", False))
        include_points = bool(cmd_obj.get("include_points", False))
        phase_safe = bool(cmd_obj.get("phase_safe", False))

        if not (0 <= channel <= 15):
            raise ValueError("channel должен быть 0-15")
        if freq_hz <= 0:
            raise ValueError("frequency должна быть > 0")

        scale_map = {"0.1 pF": (0, 0.1e-12), "1 pF": (1, 1e-12), "10 pF": (3, 10e-12)}
        if scale_str not in scale_map:
            raise ValueError("scale: 0.1 pF, 1 pF или 10 pF")

        best_amp_uv = 250.0
        v_amp_min = 5.0
        v_amp_floating_uv = 15.0
        c_parasitic = 10e-12
        reg_restore = None
        saved_state = {}
        phase_safe_info = {
            "enabled": False,
            "reg1_before": None,
            "reg1_applied": None,
            "dsp_enabled_before": None,
            "dsp_enabled_applied": None,
            "dsp_cutoff_before": None,
            "dsp_cutoff_applied": None,
            "absmode_before": None,
            "absmode_applied": None,
        }

        def _decode_reg1(reg1_value):
            return {
                "dsp_enabled": int((reg1_value >> 4) & 0x1),
                "dsp_cutoff": int(reg1_value & 0xF),
                "absmode": int((reg1_value >> 5) & 0x1),
                "twoscomp": int((reg1_value >> 6) & 0x1),
            }

        def _factor_out(z_mag, f_hz, c_par):
            if z_mag < 1000:
                return z_mag
            w = 2 * math.pi * f_hz
            denom = 1.0 - (w * c_par * z_mag) ** 2
            if denom <= 0.05:
                return z_mag
            return z_mag / math.sqrt(denom)

        try:
            for reg in (1, 2, 3, 32, 33, 42, 44, 46, 48):
                saved_state[reg] = read_intan_register(
                    self.controller.spi, reg, verbose=False
                )

            self.controller._run_rhs2116_sequence(rhs2116_safe_impedance_commands())

            if phase_safe:
                reg1_before = saved_state.get(1)
                reg1_applied = reg1_before & ~0x003F
                write_intan_register(
                    self.controller.spi, 1, reg1_applied, u_flag=0, m_flag=0, verbose=False
                )
                reg_restore = reg1_before
                before = _decode_reg1(reg1_before)
                applied = _decode_reg1(reg1_applied)
                phase_safe_info = {
                    "enabled": True,
                    "reg1_before": int(reg1_before),
                    "reg1_applied": int(reg1_applied),
                    "dsp_enabled_before": before["dsp_enabled"],
                    "dsp_enabled_applied": applied["dsp_enabled"],
                    "dsp_cutoff_before": before["dsp_cutoff"],
                    "dsp_cutoff_applied": applied["dsp_cutoff"],
                    "absmode_before": before["absmode"],
                    "absmode_applied": applied["absmode"],
                    "twoscomp_before": before["twoscomp"],
                    "twoscomp_applied": applied["twoscomp"],
                }

            if auto_scale:
                best_scale = None
                best_dist = 1e99
                for sstr, (sb, cf) in scale_map.items():
                    raw = self.controller.spi.measure_impedance_raw(
                        channel,
                        sb,
                        num_samples=num_samples,
                        frequency_hz=int(round(freq_hz)),
                        num_averages=1,
                    )
                    driver_points = raw.get("points", [])
                    if not driver_points:
                        continue
                    eff_freq_hz = float(raw.get("effective_frequency_hz", freq_hz))
                    actual_num_samples = int(raw.get("actual_num_samples", num_samples))
                    spp = int(raw.get("samples_per_period", actual_num_samples or 1))
                    _, v_amp, _ = self._compute_impedance_metrics_from_accumulators(
                        driver_points[0].get("sin_accum", 0),
                        driver_points[0].get("cos_accum", 0),
                        actual_num_samples,
                        eff_freq_hz,
                        cf,
                        samples_per_period=spp,
                    )
                    dist = abs(math.log(max(1.0, v_amp) / best_amp_uv))
                    if dist < best_dist:
                        best_dist = dist
                        best_scale = (sstr, sb, cf)
                if best_scale is None:
                    raise RuntimeError("Автовыбор шкалы не получил данных")
                scale_str, scale_bits, c_farad = best_scale
            else:
                scale_bits, c_farad = scale_map[scale_str]

            points_raw = []
            raw = self.controller.spi.measure_impedance_raw(
                channel,
                scale_bits,
                num_samples=num_samples,
                frequency_hz=int(round(freq_hz)),
                num_averages=num_averages,
            )
            eff_freq_hz = float(raw.get("effective_frequency_hz", freq_hz))
            sample_count = int(raw.get("actual_num_samples", num_samples))
            spp = int(raw.get("samples_per_period", sample_count or 1))
            for point in raw.get("points", []):
                z_ohm, v_amp_uv, phase_deg = self._compute_impedance_metrics_from_accumulators(
                    point.get("sin_accum", 0),
                    point.get("cos_accum", 0),
                    sample_count,
                    eff_freq_hz,
                    c_farad,
                    samples_per_period=spp,
                )
                if v_amp_uv >= v_amp_min:
                    z_corr = _factor_out(z_ohm, eff_freq_hz, c_parasitic)
                    points_raw.append((z_ohm, z_corr, v_amp_uv, phase_deg))
        finally:
            if reg_restore is not None:
                write_intan_register(
                    self.controller.spi, 1, reg_restore, u_flag=0, m_flag=0, verbose=False
                )
            if saved_state:
                write_intan_register(
                    self.controller.spi, 32, saved_state.get(32, 0x0000), u_flag=0, m_flag=0, verbose=False
                )
                write_intan_register(
                    self.controller.spi, 33, saved_state.get(33, 0x0000), u_flag=0, m_flag=0, verbose=False
                )
                write_intan_register(
                    self.controller.spi, 2, saved_state.get(2, 0x0000), u_flag=0, m_flag=0, verbose=False
                )
                write_intan_register(
                    self.controller.spi, 3, saved_state.get(3, 0x0080), u_flag=0, m_flag=0, verbose=False
                )
                write_intan_register(
                    self.controller.spi, 44, saved_state.get(44, 0x0000), u_flag=0, m_flag=0, verbose=False
                )
                write_intan_register(
                    self.controller.spi, 46, saved_state.get(46, 0x0000), u_flag=0, m_flag=0, verbose=False
                )
                write_intan_register(
                    self.controller.spi, 48, saved_state.get(48, 0x0000), u_flag=0, m_flag=0, verbose=False
                )
                write_intan_register(
                    self.controller.spi, 42, saved_state.get(42, 0x0000), u_flag=1, m_flag=0, verbose=False
                )

        if len(points_raw) < 2:
            raise RuntimeError(f"Недостаточно валидных измерений (V_amp < {v_amp_min} µV)")

        z_list = [p[1] for p in points_raw]
        v_list = [p[2] for p in points_raw]
        phase_list = [p[3] for p in points_raw]
        z_sorted = sorted(z_list)
        n = len(z_sorted)
        z_ohm = z_sorted[n // 2]
        v_amp_uv = sum(v_list) / len(v_list)
        phase_rad_avg = math.atan2(
            sum(math.sin(math.radians(p)) for p in phase_list),
            sum(math.cos(math.radians(p)) for p in phase_list),
        )
        phase_deg = math.degrees(phase_rad_avg)
        mad = sorted(abs(x - z_ohm) for x in z_list)[n // 2] if n > 0 else 0
        std_z = 1.4826 * mad if n > 1 else 0
        likely_floating = v_amp_uv < v_amp_floating_uv
        freq_error_hz = float(eff_freq_hz - freq_hz)
        freq_error_pct = float((freq_error_hz / freq_hz) * 100.0) if freq_hz else 0.0

        result = {
            "status": "ok",
            "cmd": "measure_impedance_fast",
            "impedance_ohm": float(z_ohm),
            "std_dev_ohm": float(std_z),
            "channel": channel,
            "frequency": float(freq_hz),
            "requested_frequency_hz": float(freq_hz),
            "effective_frequency_hz": float(eff_freq_hz),
            "frequency_error_hz": freq_error_hz,
            "frequency_error_pct": freq_error_pct,
            "scale": scale_str,
            "num_valid": len(z_list),
            "v_amp_uv": float(v_amp_uv),
            "phase_deg": float(phase_deg),
            "likely_floating": bool(likely_floating),
            "samples_per_period": int(spp),
            "actual_num_samples": int(sample_count),
            "phase_safe": phase_safe_info,
        }
        if include_points:
            result["points"] = [
                {
                    "nom": i + 1,
                    "z_raw": zr,
                    "z_corr": zc,
                    "V_amp": v,
                    "V_rms": v / (2 ** 0.5) if v else 0,
                    "phase_deg": ph,
                }
                for i, (zr, zc, v, ph) in enumerate(points_raw)
            ]
            result["valid_z"] = list(z_list)
        return result

    def _process_send_line(self, raw_line):
        """
        Выполняет одну «сырую» строковую команду RHS2116 и
        возвращает строковый ответ (совместимо с GUI regex по 0x...).
        """
        if not isinstance(raw_line, str):
            raise ValueError("line должен быть строкой")

        line = raw_line.strip()
        if not line:
            return ""
        if line.startswith("#"):
            return "COMMENT"

        parts = line.split()
        cmd = parts[0].upper()

        self.controller.ensure_initialized()

        if cmd == "CLEAR":
            clear_adc(self.controller.spi, verbose=False)
            return "CLEAR RESP: OK"

        if cmd == "CLEAR_COMP":
            if hasattr(self.controller.spi, "run_intan_command"):
                self.controller.spi.run_intan_command("CLEAR_COMP")
            else:
                clear_compliance_monitor(self.controller.spi, verbose=False)
            return "CLEAR_COMP RESP: OK"

        if cmd == "INIT_STIM":
            if hasattr(self.controller.spi, "run_intan_command"):
                reply = self.controller.spi.run_intan_command("INIT_STIM", timeout_ms=15000)
            else:
                self.controller.ensure_initialized()
                reply = "OK (ensure_initialized)"
            return f"INIT_STIM RESP: {reply}"

        if cmd == "PATTERN_STATUS":
            if hasattr(self.controller.spi, "pattern_status"):
                reply = self.controller.spi.pattern_status()
            else:
                reply = "N/A"
            return f"PATTERN_STATUS RESP: {reply}"

        if cmd == "WRITE":
            if len(parts) < 3:
                raise ValueError("WRITE требует формат: WRITE <reg> <value> [U] [M]")
            reg_addr = int(parts[1], 0)
            value = int(parts[2], 0)
            u_flag, m_flag = parse_write_um_flags(parts)
            write_intan_register(
                self.controller.spi,
                reg_addr,
                value,
                u_flag=u_flag,
                m_flag=m_flag,
                verbose=False,
            )
            return f"WRITE RESP [reg {reg_addr}]: 0x{value:04X}"

        if cmd == "READ":
            if len(parts) < 2:
                raise ValueError("READ требует формат: READ <reg>")
            reg_addr = int(parts[1], 0)
            value = read_intan_register(self.controller.spi, reg_addr, verbose=False)
            return f"READ RESP [reg {reg_addr}]: 0x{value:04X} ({value})"

        if cmd == "DELAY":
            if len(parts) < 2:
                raise ValueError("DELAY требует формат: DELAY <count>")
            delay_count = int(parts[1], 0)
            for _ in range(delay_count):
                read_intan_register(self.controller.spi, 255, verbose=False)
            return f"DELAY RESP: {delay_count}"

        if cmd == "CONVERT":
            if len(parts) < 2:
                raise ValueError("CONVERT требует формат: CONVERT <channel> [D=0/1] [H=0/1]")
            channel = int(parts[1], 0)
            if not (0 <= channel <= 63):
                raise ValueError("channel должен быть в диапазоне 0-63")
            d_flag = self._parse_flag(parts, "D", default=0)
            h_flag = self._parse_flag(parts, "H", default=0)

            amp_type = "dc" if d_flag else "ac"
            if channel == 63 and hasattr(self.controller.spi, "convert_channel_auto"):
                adc_value = self.controller.spi.convert_channel_auto()
            elif hasattr(self.controller.spi, "convert_channel"):
                adc_value = self.controller.spi.convert_channel(
                    channel, amp_type=amp_type, h_flag=h_flag
                )
            else:
                adc_value = convert_intan(
                    self.controller.spi,
                    channel,
                    amp_type=amp_type,
                    h_flag=h_flag,
                )

            resp32 = ((adc_value & 0xFFFF) << 16) & 0xFFFFFFFF
            return f"CONVERT RESP [ch {channel}]: 0x{resp32:08X}"

        raise ValueError(f"Неизвестная send_line команда: {cmd}")

    def process_command(self, cmd_obj):
        if not isinstance(cmd_obj, dict):
            raise ValueError("Команда должна быть JSON-объектом")

        cmd = cmd_obj.get("cmd")
        if not cmd:
            raise ValueError("Отсутствует поле 'cmd'")

        if cmd == "ping":
            return {"status": "ok", "reply": "pong"}

        if cmd == "pulse":
            channels_str = cmd_obj.get("channels", "0")
            neg = int(cmd_obj.get("neg", 0))
            pos = int(cmd_obj.get("pos", 10))
            pulse_duration = float(cmd_obj.get("pulse_duration", 0.001))
            inter_pulse_delay = float(cmd_obj.get("inter_pulse_delay", 0.001))
            repeat_count = int(cmd_obj.get("repeat_count", 1))
            if not (0 <= neg <= 255 and 0 <= pos <= 255):
                raise ValueError("neg и pos должны быть в диапазоне 0-255")
            if pulse_duration < 0 or inter_pulse_delay < 0:
                raise ValueError("pulse_duration и inter_pulse_delay должны быть >= 0")
            if repeat_count < 1:
                raise ValueError("repeat_count должен быть >= 1")
            self.controller.pulse(channels_str, neg, pos, pulse_duration, inter_pulse_delay, repeat_count)
            return {
                "status": "ok",
                "cmd": "pulse",
                "channels": channels_str,
                "neg": neg,
                "pos": pos,
                "pulse_duration": pulse_duration,
                "inter_pulse_delay": inter_pulse_delay,
                "repeat_count": repeat_count,
            }

        if cmd == "sawtooth":
            channels_str = cmd_obj.get("channels", "0")
            pos = int(cmd_obj.get("pos", 10))
            steps = int(cmd_obj.get("steps", 50))
            duration = float(cmd_obj.get("duration", 0.001))
            if not (0 <= pos <= 255):
                raise ValueError("pos должен быть в диапазоне 0-255")
            self.controller.sawtooth(channels_str, pos, steps, duration)
            return {
                "status": "ok",
                "cmd": "sawtooth",
                "channels": channels_str,
                "pos": pos,
                "steps": steps,
                "duration": duration,
            }

        if cmd == "stop":
            self.controller.stop_all()
            return {"status": "ok", "cmd": "stop"}

        if cmd == "init":
            # Принудительная инициализация (если нужно заранее)
            self.controller.ensure_initialized()
            return {"status": "ok", "cmd": "init"}

        if cmd == "close":
            self.controller.close()
            return {"status": "ok", "cmd": "close"}

        if cmd == "read_temperature":
            """Читает температуру из регистра 3"""
            self.controller.ensure_initialized()
            temp_value = read_intan_register(self.controller.spi, 3, verbose=self.controller.verbose)
            return {"status": "ok", "cmd": "read_temperature", "temperature": temp_value}

        if cmd == "read_register":
            """Читает значение регистра Intan"""
            address = int(cmd_obj.get("address", 255))
            if not (0 <= address <= 255):
                raise ValueError("address должен быть в диапазоне 0-255")

            self.controller.ensure_chip_ready()
            value = read_intan_register(
                self.controller.spi, address, verbose=self.controller.verbose
            )
            return {
                "status": "ok",
                "cmd": "read_register",
                "address": address,
                "value": value
            }

        if cmd == "send_line":
            raw_line = cmd_obj.get("line", "")
            response = self._process_send_line(raw_line)
            return {
                "status": "ok",
                "cmd": "send_line",
                "line": raw_line,
                "response": response,
            }

        if cmd == "measure_impedance_fast":
            return self._process_measure_impedance_fast(cmd_obj)

        if cmd == "measure_impedance":
            """Измеряет импеданс на указанном канале"""
            channel = int(cmd_obj.get("channel", 0))
            test_current_nA = float(cmd_obj.get("test_current_nA", 5.0))
            frequency = int(cmd_obj.get("frequency", 1000))
            num_samples = int(cmd_obj.get("num_samples", 1000))
            
            if not (0 <= channel <= 15):
                raise ValueError("channel должен быть в диапазоне 0-15")
            
            if not (0 < test_current_nA <= 1000):
                raise ValueError("test_current_nA должен быть в диапазоне 0-1000 nA")
            
            # КРИТИЧНО: Сохраняем состояние инициализации контроллера
            # Контроллер должен быть инициализирован для стимуляции
            controller_was_initialized = self.controller.initialized
            
            # Используем метод из IntanRecorder для измерения импеданса
            # НЕ вызываем ensure_initialized() для recorder, чтобы не сбросить регистры
            from intan_udp_recorder import IntanRecorder
            recorder = IntanRecorder(
                gpio_number=self.controller.gpio_number,
                spi_device=self.controller.spi_device,
                verbose=self.controller.verbose
            )
            # ВАЖНО: Используем SPI напрямую, не инициализируем recorder
            # Это предотвратит сброс регистров стимуляции
            if not recorder.spi:
                recorder.spi = self.controller.spi
            if not recorder.gpio:
                recorder.gpio = self.controller.gpio
            
            # КРИТИЧНО: Выполняем несколько измерений и усредняем для стабильности
            # Это уменьшает влияние шума и артефактов на результат
            num_measurements = int(cmd_obj.get("num_measurements", 3))  # По умолчанию 3 измерения
            measurements = []
            
            for i in range(num_measurements):
                result = recorder.measure_impedance(
                    channel=channel,
                    test_current_nA=test_current_nA,
                    frequency=frequency,
                    num_samples=num_samples
                )
                measurements.append(result)
                # Небольшая задержка между измерениями для стабилизации
                if i < num_measurements - 1:
                    time.sleep(0.1)
            
            # Усредняем результаты для стабильности
            if num_measurements > 1:
                avg_impedance = sum(m.get("impedance_ohm", 0) for m in measurements) / len(measurements)
                avg_adc_rms = sum(m.get("adc_rms", 0) for m in measurements) / len(measurements)
                avg_voltage_rms = sum(m.get("voltage_rms", 0) for m in measurements) / len(measurements)
                avg_voltage_peak = sum(m.get("voltage_peak", 0) for m in measurements) / len(measurements)
                
                # Вычисляем стандартное отклонение для оценки стабильности
                if NUMPY_AVAILABLE:
                    impedances = [m.get("impedance_ohm", 0) for m in measurements]
                    std_dev = np.std(impedances)
                else:
                    impedances = [m.get("impedance_ohm", 0) for m in measurements]
                    mean_imp = sum(impedances) / len(impedances)
                    variance = sum((x - mean_imp) ** 2 for x in impedances) / len(impedances)
                    std_dev = math.sqrt(variance)
                
                result = {
                    "impedance_ohm": float(avg_impedance),
                    "adc_rms": float(avg_adc_rms),
                    "voltage_rms": float(avg_voltage_rms),
                    "voltage_peak": float(avg_voltage_peak),
                    "test_current_nA": float(test_current_nA),
                    "frequency": float(frequency),
                    "channel": int(channel),
                    "num_measurements": num_measurements,
                    "std_dev_ohm": float(std_dev),  # Стандартное отклонение для оценки стабильности
                    "measurements": measurements  # Все отдельные измерения для анализа
                }
            else:
                result = measurements[0] if measurements else {}
            
            # КРИТИЧНО: После измерения импеданса переинициализируем контроллер для стимуляции
            # Это гарантирует, что все регистры стимуляции будут правильно настроены
            if controller_was_initialized:
                if self.controller.verbose:
                    self.controller._log("Переинициализация контроллера для стимуляции после измерения импеданса...")
                # Сбрасываем флаг инициализации и переинициализируем
                self.controller.initialized = False
                self.controller.ensure_initialized()
                if self.controller.verbose:
                    self.controller._log("✓ Контроллер переинициализирован для стимуляции")
            
            return {
                "status": "ok",
                "cmd": "measure_impedance",
                **result  # Распаковываем все поля из result
            }

        if cmd == "pattern_load":
            # Загрузка паттерна в память устройства
            pattern_commands = cmd_obj.get("commands", [])
            if not isinstance(pattern_commands, list):
                raise ValueError("commands должен быть списком строк")
            
            commands_count = self.controller.load_pattern(pattern_commands)
            return {
                "status": "ok",
                "cmd": "pattern_load",
                "commands_count": commands_count,
                "message": f"Паттерн загружен в память: {commands_count} команд",
            }

        if cmd == "pattern_run":
            # Запуск паттерна из памяти устройства
            repeat_count = int(cmd_obj.get("repeat_count", 1))
            if repeat_count < 1:
                raise ValueError("repeat_count должен быть >= 1")
            if repeat_count > 10000:
                raise ValueError("repeat_count не должен превышать 10000")
            
            results = self.controller.run_pattern_from_memory(repeat_count=repeat_count)
            return {
                "status": "ok",
                "cmd": "pattern_run",
                "repeat_count": repeat_count,
                "results": results,
            }

        if cmd == "configure_adc":
            """Настраивает Register 0 (ADC buffer bias и MUX bias)"""
            self.controller.ensure_initialized()
            
            # Получаем значение Register 0 из команды или формируем из параметров
            if "register_0" in cmd_obj:
                reg0_value = int(cmd_obj.get("register_0"))
            else:
                # Формируем Register 0 из отдельных параметров
                adc_buffer_bias = int(cmd_obj.get("adc_buffer_bias", 3))
                mux_bias = int(cmd_obj.get("mux_bias", 5))
                
                # Проверяем диапазоны
                if not (0 <= adc_buffer_bias <= 255):
                    raise ValueError("ADC buffer bias должен быть в диапазоне 0-255")
                if not (0 <= mux_bias <= 255):
                    raise ValueError("MUX bias должен быть в диапазоне 0-255")
                
                # Формируем Register 0: биты [15:8] = ADC buffer bias, биты [7:0] = MUX bias
                reg0_value = (adc_buffer_bias << 8) | mux_bias
            
            # Записываем Register 0
            write_intan_register(
                self.controller.spi, 
                0, 
                reg0_value, 
                u_flag=0, 
                m_flag=0, 
                verbose=self.controller.verbose
            )
            time.sleep(0.001)  # Небольшая задержка для применения
            
            return {
                "status": "ok",
                "cmd": "configure_adc",
                "register_0": reg0_value,
                "register_0_hex": f"0x{reg0_value:04X}",
                "adc_buffer_bias": (reg0_value >> 8) & 0xFF,
                "mux_bias": reg0_value & 0xFF,
            }

        if cmd == "configure_filters":
            """Настраивает аппаратные фильтры (Registers 1, 4, 5, 6, 7)"""
            self.controller.ensure_initialized()
            
            # Получаем значения регистров
            reg1 = int(cmd_obj.get("register_1", 0x951A))
            reg4 = int(cmd_obj.get("register_4", 0x015E))
            reg5 = int(cmd_obj.get("register_5", 0x01AB))
            reg6 = int(cmd_obj.get("register_6", 0x0036))
            reg7 = int(cmd_obj.get("register_7", 0x000A))
            
            # Проверяем диапазоны
            for reg_val, reg_name in [(reg1, "Register 1"), (reg4, "Register 4"), 
                                      (reg5, "Register 5"), (reg6, "Register 6"), 
                                      (reg7, "Register 7")]:
                if not (0 <= reg_val <= 0xFFFF):
                    raise ValueError(f"{reg_name} должен быть в диапазоне 0-0xFFFF")
            
            # Записываем регистры фильтров
            # Register 1: DSP HPF и auxiliary outputs
            write_intan_register(self.controller.spi, 1, reg1, u_flag=0, m_flag=0, verbose=self.controller.verbose)
            time.sleep(0.001)
            
            # Register 4: Верхняя частота среза (fH)
            write_intan_register(self.controller.spi, 4, reg4, u_flag=0, m_flag=0, verbose=self.controller.verbose)
            time.sleep(0.001)
            
            # Register 5: Параметр для верхней частоты среза (fH)
            write_intan_register(self.controller.spi, 5, reg5, u_flag=0, m_flag=0, verbose=self.controller.verbose)
            time.sleep(0.001)
            
            # Register 6: Нижняя частота среза (fL, версия A)
            write_intan_register(self.controller.spi, 6, reg6, u_flag=0, m_flag=0, verbose=self.controller.verbose)
            time.sleep(0.001)
            
            # Register 7: RL_B (быстрое восстановление, версия B)
            write_intan_register(self.controller.spi, 7, reg7, u_flag=0, m_flag=0, verbose=self.controller.verbose)
            time.sleep(0.001)
            
            # Извлекаем DSP cutoff из Register 1 для ответа
            dsp_cutoff = (reg1 >> 12) & 0xF
            
            return {
                "status": "ok",
                "cmd": "configure_filters",
                "register_1": reg1,
                "register_1_hex": f"0x{reg1:04X}",
                "register_4": reg4,
                "register_4_hex": f"0x{reg4:04X}",
                "register_5": reg5,
                "register_5_hex": f"0x{reg5:04X}",
                "register_6": reg6,
                "register_6_hex": f"0x{reg6:04X}",
                "register_7": reg7,
                "register_7_hex": f"0x{reg7:04X}",
                "dsp_cutoff": dsp_cutoff,
                "fh_freq": cmd_obj.get("fh_freq", ""),
                "fl_freq": cmd_obj.get("fl_freq", ""),
            }

        if cmd == "pattern":
            # Старый формат для обратной совместимости (загружает и сразу выполняет)
            pattern_commands = cmd_obj.get("commands", [])
            if not isinstance(pattern_commands, list):
                raise ValueError("commands должен быть списком строк")
            if len(pattern_commands) > 200:
                raise ValueError("Максимум 200 команд в паттерне")
            
            repeat_count = int(cmd_obj.get("repeat_count", 1))
            if repeat_count < 1:
                raise ValueError("repeat_count должен быть >= 1")
            if repeat_count > 10000:
                raise ValueError("repeat_count не должен превышать 10000")
            
            # Загружаем паттерн
            self.controller.load_pattern(pattern_commands)
            # Сразу выполняем
            results = self.controller.run_pattern_from_memory(repeat_count=repeat_count)
            return {
                "status": "ok",
                "cmd": "pattern",
                "commands_count": len(pattern_commands),
                "repeat_count": repeat_count,
                "results": results,
            }

        raise ValueError(f"Неизвестная команда: {cmd}")


def get_primary_ip() -> str:
    """
    Возвращает основной IP-адрес платы для исходящих подключений.
    Не делает реальных сетевых запросов, только локальное определение.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "не удалось определить"


def main():
    parser = argparse.ArgumentParser(
        description="TCP-сервер для управления стимуляцией Intan RHS2116",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="TCP-порт для прослушивания (по умолчанию: 9000)",
    )
    parser.add_argument(
        "-g",
        "--gpio",
        type=int,
        default=226,
        help="Номер GPIO для PH2 (по умолчанию: 226)",
    )
    parser.add_argument(
        "-d",
        "--device",
        default="/dev/spidev1.1",
        help="Путь к SPI устройству (по умолчанию: /dev/spidev1.1)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Подробный вывод",
    )

    args = parser.parse_args()

    controller = IntanController(
        gpio_number=args.gpio,
        spi_device=args.device,
        verbose=args.verbose,
    )

    IntanTCPHandler.controller = controller

    with socketserver.ThreadingTCPServer(("0.0.0.0", args.port), IntanTCPHandler) as server:
        ip = get_primary_ip()
        print("=" * 60)
        print("TCP-сервер Intan RHS2116 запущен")
        print("=" * 60)
        print(f"IP платы (основной): {ip}")
        print(f"Слушает на: 0.0.0.0:{args.port}")
        print(f"Порт: {args.port}")
        print(f"GPIO PH2: {args.gpio}")
        print(f"SPI устройство: {args.device}")
        print("Команды (JSON по TCP): ping, init, pulse, sawtooth, stop, close")
        print("=" * 60)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nОстановка сервера по Ctrl+C")
        finally:
            controller.close()
            server.server_close()


if __name__ == "__main__":
    main()

