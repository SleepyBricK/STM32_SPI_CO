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
import threading
import time
import sys

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

from stimulate_channel0 import (
    GPIOController,
    GPIOError,
    SPIController,
    get_preferred_spi_device,
    setup_stimulation_channels,
    clear_compliance_monitor,
    enable_stimulation_channels,
    set_stimulation_current,
    poll_register_until_ready,
    parse_channels,
    read_intan_register,
    write_intan_register,
    clear_adc,
)
from rhs2116_profiles import (
    RHS2116_STIM_BIAS_1UA,
    RHS2116_STIM_STEP_1UA,
    rhs2116_recording_init_commands,
    rhs2116_safe_impedance_commands,
    rhs2116_stimulation_init_commands,
    run_rhs2116_sequence,
)
from intan_shared_state import IntanSharedState

PATTERN_OP_WRITE_REG = 1
PATTERN_OP_READ_REG = 2
PATTERN_OP_CLEAR_ADC = 3
PATTERN_OP_DELAY = 4
PATTERN_OP_CLEAR_COMPLIANCE = 5


class IntanController:
    """
    Обертка над существующими функциями стимуляции для использования из TCP-сервера.
    """

    def __init__(self, gpio_number=226, spi_device="/dev/spidev1.1", verbose=False, shared_state=None):
        self.gpio_number = gpio_number
        self.spi_device = get_preferred_spi_device(spi_device)
        self.verbose = verbose
        self.shared_state = shared_state or IntanSharedState()

        self.gpio = None
        self.spi = None
        self.initialized = False
        self.using_driver = self.spi_device == "/dev/intan"
        self.current_mode = "unknown"
        
        # Память для хранения паттерна
        self.pattern_commands = []
        self.pattern_ops = []
        self.pattern_loaded = False
        self.lock = threading.Lock()

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

    def _restore_recording_after_stimulation(self, operation, recording_snapshot):
        if not recording_snapshot.get("recording_active"):
            return False

        adc_rate_ksps = float(recording_snapshot.get("adc_rate_ksps") or 480.0)
        self.shared_state.emit_event(
            "recording_reinit_started",
            operation=operation,
            adc_rate_ksps=adc_rate_ksps,
        )
        self._initialize_for_recording(
            verbose=self.verbose,
            adc_sampling_rate_ksps=adc_rate_ksps,
        )
        self.shared_state.emit_event(
            "recording_reinit_done",
            operation=operation,
            adc_rate_ksps=adc_rate_ksps,
        )
        return True

    def _run_coordinated_stimulation(self, operation, callback):
        recording_snapshot = self.shared_state.begin_stimulation(operation)
        restored_recording = False
        result = None
        pending_error = None

        with self.shared_state.chip_lock:
            try:
                result = callback()
            except Exception as exc:
                pending_error = exc
            finally:
                if recording_snapshot.get("recording_active"):
                    try:
                        restored_recording = self._restore_recording_after_stimulation(
                            operation,
                            recording_snapshot,
                        )
                    except Exception as restore_exc:
                        if pending_error is None:
                            pending_error = restore_exc
                        elif self.verbose:
                            self._log(
                                "Не удалось восстановить recording mode после ошибки "
                                f"'{operation}': {restore_exc}"
                            )

        self.shared_state.end_stimulation(
            operation,
            restored_recording=restored_recording,
        )
        if pending_error is not None:
            raise pending_error
        return result

    def ensure_initialized(self):
        """
        Ленивая безопасная инициализация GPIO, SPI и чипа Intan.
        По умолчанию переводит чип в recording-safe режим без unlock stimulation path.
        """
        with self.lock:
            if self.initialized:
                return

            REG_ADDR = 255
            EXPECTED_VALUE = 32  # Chip ID для RHS2116

            self._log("== Инициализация Intan для TCP-сервера ==")
            self._log(f"GPIO PH2: {self.gpio_number}")
            self._log(f"SPI устройство: {self.spi_device}")

            try:
                if self.using_driver:
                    self._log("[1/6] Используется драйвер /dev/intan: GPIO питания управляется ядром")
                    self._log("[2/6] Пропуск ручного включения питания через sysfs")
                else:
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
                if getattr(self.spi, "using_driver", False):
                    self._log("      SPI backend: /dev/intan (driver ioctl)")
                else:
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

                # Безопасная инициализация для чтения/регистрации без включения stimulation path
                self._log("[5/6] Инициализация регистров чипа для safe recording mode...")
                self._initialize_for_recording(verbose=self.verbose)
                self._log("      Чип переведен в safe recording mode (регистры 32-33 = 0x0000)")

                # Финальная очистка compliance monitor
                self._log("[6/6] Очистка compliance monitor...")
                clear_compliance_monitor(self.spi, verbose=self.verbose)
                self._log("      Compliance monitor очищен")

                self.initialized = True
                self.current_mode = "recording"
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
                self.current_mode = "unknown"
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
                self.current_mode = "unknown"
                raise  # Пробрасываем исключение дальше

    def _initialize_for_recording(self, verbose=False, adc_sampling_rate_ksps=480.0):
        """Переводит чип в безопасный recording mode без unlock stimulation path."""
        self._run_rhs2116_sequence(rhs2116_recording_init_commands(adc_sampling_rate_ksps))
        self.current_mode = "recording"
        if verbose:
            self._log("      ✓ Инициализация для записи завершена (stimulation path locked)")

    def _initialize_for_stimulation(self, verbose=False):
        """
        Инициализация чипа для стимуляции по аналогии с StimulationWithPreset.py.
        
        ВАЖНО: Согласно даташиту (May 2021), питание VSTIM должно быть не более ±7V
        (максимум 14V между VSTIM+ и VSTIM-). Использование ±9V может вызвать
        деградацию анодных генераторов тока после многих импульсов стимуляции.
        
        Последовательность:
        1. READ 255 (dummy команда)
        2. WRITE 32/33 0x0000 (отключить стимуляцию)
        3. Инициализация регистров 0-44 (включая Register 9 для Low-Gain Amplifiers)
        4. Инициализация регистров 64-79 и 96-111 (токи стимуляции) в 0x8000
        5. WRITE 32 0xAAAA, WRITE 33 0x00FF (разрешить работу стимуляторов)
        6. READ 255 U=0 M=1 (очистка compliance monitor)
        
        Register 36 (Vrecov): 0x0080 = 0V (8-bit DAC, 128 = 0V, диапазон ±1.22V)
        Register 37 (Imax): 0x4F00 = 1 nA (sel1=0, sel2=30, sel3=2 согласно даташиту)
        """
        self._run_rhs2116_sequence(rhs2116_stimulation_init_commands(adc_sampling_rate_ksps=480.0))
        self.current_mode = "stimulation"
        
        if verbose:
            self._log("      ✓ Инициализация для стимуляции завершена")

    def ensure_recording_ready(self, adc_sampling_rate_ksps=480.0):
        """Гарантирует безопасный режим записи без включенной стимуляции."""
        self.ensure_initialized()
        if self.current_mode != "recording":
            self._log("Переключение чипа в recording mode...")
            self._initialize_for_recording(verbose=self.verbose, adc_sampling_rate_ksps=adc_sampling_rate_ksps)

    def ensure_stimulation_ready(self):
        """Гарантирует режим стимуляции с полностью инициализированным stimulation path."""
        self.ensure_initialized()
        if self.current_mode != "stimulation":
            self._log("Переключение чипа в stimulation mode...")
            self._initialize_for_stimulation(verbose=self.verbose)

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
        def _run():
            try:
                self.ensure_stimulation_ready()
            except Exception as e:
                raise RuntimeError(f"Ошибка инициализации: {e}")

            channels = parse_channels(channels_str)

            setup_stimulation_channels(
                spi=self.spi,
                channels=channels,
                neg_current_magnitude=neg_current,
                pos_current_magnitude=pos_current,
                step_size_1ua=True,
                verbose=self.verbose,
                assume_step_size_configured=True,
            )

            for rep in range(repeat_count):
                if neg_current > 0 and pos_current > 0:
                    enable_stimulation_channels(
                        self.spi, channels, enable=True, negative_polarity=True, verbose=False
                    )
                    time.sleep(pulse_duration)
                    enable_stimulation_channels(
                        self.spi, channels, enable=False, negative_polarity=True, verbose=False
                    )
                    time.sleep(inter_pulse_delay)

                    enable_stimulation_channels(
                        self.spi, channels, enable=True, negative_polarity=False, verbose=False
                    )
                    time.sleep(pulse_duration)
                    enable_stimulation_channels(
                        self.spi, channels, enable=False, negative_polarity=False, verbose=False
                    )
                else:
                    polarity = neg_current > 0
                    enable_stimulation_channels(
                        self.spi, channels, enable=True, negative_polarity=polarity, verbose=self.verbose
                    )
                    time.sleep(pulse_duration)
                    enable_stimulation_channels(
                        self.spi, channels, enable=False, negative_polarity=polarity, verbose=self.verbose
                    )

                if rep < repeat_count - 1:
                    time.sleep(inter_pulse_delay)

            if self.verbose:
                self._log("      Сброс DSP HPF после стимуляции...")
            self._reset_dsp_hpf(channels)

        self._run_coordinated_stimulation("pulse", _run)

    def sawtooth(self, channels_str, pos_current, steps, duration):
        """
        Выполняет один цикл пилообразной стимуляции для заданных каналов.

        pos_current: максимальный ток (µA)
        steps: количество шагов
        duration: длительность одного цикла (сек)
        """
        def _run():
            try:
                self.ensure_stimulation_ready()
            except Exception as e:
                raise RuntimeError(f"Ошибка инициализации: {e}")

            channels = parse_channels(channels_str)
            if steps <= 0 or duration <= 0:
                raise ValueError("steps и duration должны быть > 0")

            setup_stimulation_channels(
                spi=self.spi,
                channels=channels,
                neg_current_magnitude=0,
                pos_current_magnitude=0,
                step_size_1ua=True,
                verbose=self.verbose,
                assume_step_size_configured=True,
            )

            enable_stimulation_channels(
                self.spi, channels, enable=True, negative_polarity=False, verbose=self.verbose
            )

            start = time.time()
            for step in range(steps + 1):
                current = int((pos_current * step) / steps)
                for ch in channels:
                    set_stimulation_current(
                        self.spi,
                        current_value=current,
                        channel=ch,
                        is_positive=True,
                        verbose=False,
                        assume_step_size_configured=True,
                    )
                current_reg42 = read_intan_register(self.spi, 42, verbose=False)
                write_intan_register(self.spi, 42, current_reg42, u_flag=1, m_flag=0, verbose=False)

            elapsed = time.time() - start
            self._log(
                f"TCP: sawtooth cycle completed, requested {duration*1000:.3f} ms, actual {elapsed*1000:.3f} ms"
            )
            enable_stimulation_channels(
                self.spi, channels, enable=False, negative_polarity=False, verbose=self.verbose
            )
            if self.verbose:
                self._log("      Сброс DSP HPF после стимуляции...")
            self._reset_dsp_hpf(channels)

        self._run_coordinated_stimulation("sawtooth", _run)

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
        
        # Для каждого канала выполняем CONVERT с H flag = 1
        for channel in channels:
            # Формируем команду CONVERT с H flag = 1
            # Биты [31:30] = 00 (CONVERT)
            # Бит [26] = H flag = 1 (сброс DSP HPF)
            # Биты [21:16] = номер канала
            cmd_word = 0x00000000
            cmd_word |= (channel << 16)  # номер канала
            cmd_word |= (1 << 26)  # H flag для сброса DSP HPF
            
            # Преобразуем в байты (MSB first)
            cmd = [
                (cmd_word >> 24) & 0xFF,
                (cmd_word >> 16) & 0xFF,
                (cmd_word >> 8) & 0xFF,
                cmd_word & 0xFF
            ]
            
            # Отправляем команду CONVERT (3 фазы для pipeline)
            dummy = [0x00, 0x00, 0x00, 0x00]
            self.spi.transfer(cmd)  # Отправка команды
            self.spi.transfer(dummy)  # Dummy для pipeline
            self.spi.transfer(dummy)  # Dummy для получения результата
        
        # Небольшая задержка для применения сброса
        time.sleep(0.001)

    def _make_pattern_write_op(self, reg_addr, value, u_flag=0, m_flag=0, visible=True):
        return {
            "opcode": PATTERN_OP_WRITE_REG,
            "reg": reg_addr,
            "flags": (u_flag & 1) | ((m_flag & 1) << 1),
            "value": value,
            "count": 0,
            "_visible_result": visible,
            "_result_cmd": "WRITE",
        }

    def _compile_pattern_commands(self, pattern_commands):
        if len(pattern_commands) > 200:
            raise ValueError("Максимум 200 команд в паттерне")

        validated_commands = []
        compiled_ops = []
        step_reg34_is_1ua = True
        step_reg35_is_1ua = True

        for cmd_line in pattern_commands:
            cmd_line = cmd_line.strip()
            if not cmd_line or cmd_line.startswith('#'):
                continue

            parts = cmd_line.split()
            if not parts:
                continue

            cmd_type = parts[0].upper()
            if cmd_type == "WRITE":
                if len(parts) < 3:
                    raise ValueError("WRITE требует минимум 2 параметра: регистр и значение")
                reg_addr = int(parts[1], 0)
                value = int(parts[2], 0)
                u_flag = 1 if "U" in parts else 0
                m_flag = 1 if "M" in parts else 0

                if (64 <= reg_addr <= 79) or (96 <= reg_addr <= 111):
                    if not (step_reg34_is_1ua and step_reg35_is_1ua):
                        compiled_ops.append(
                            self._make_pattern_write_op(
                                34,
                                RHS2116_STIM_STEP_1UA,
                                u_flag=0,
                                m_flag=0,
                                visible=False,
                            )
                        )
                        compiled_ops.append(
                            self._make_pattern_write_op(
                                35,
                                RHS2116_STIM_BIAS_1UA,
                                u_flag=0,
                                m_flag=0,
                                visible=False,
                            )
                        )
                        step_reg34_is_1ua = True
                        step_reg35_is_1ua = True

                    if value < 0x8000 or value > 0x80FF:
                        if 0 <= value <= 255:
                            value = 0x8000 | (value & 0xFF)
                            if self.verbose:
                                self._log(f"  Преобразовано значение тока: {value & 0xFF} µA -> 0x{value:04X}")
                        else:
                            raise ValueError(
                                "Значение тока должно быть в диапазоне 0-255 µA "
                                f"или в формате 0x80XX, получено: {value}"
                            )
                    elif self.verbose:
                        current_ua = value & 0xFF
                        self._log(
                            f"  Установка тока: регистр {reg_addr} = 0x{value:04X} "
                            f"(ток = {current_ua} µA при шаге 1 µA)"
                        )

                compiled_ops.append(
                    self._make_pattern_write_op(
                        reg_addr,
                        value,
                        u_flag=u_flag,
                        m_flag=m_flag,
                        visible=True,
                    )
                )
                if reg_addr == 34:
                    step_reg34_is_1ua = value == RHS2116_STIM_STEP_1UA
                elif reg_addr == 35:
                    step_reg35_is_1ua = value == RHS2116_STIM_BIAS_1UA

            elif cmd_type == "READ":
                if len(parts) < 2:
                    raise ValueError("READ требует параметр: адрес регистра")
                reg_addr = int(parts[1], 0)
                compiled_ops.append({
                    "opcode": PATTERN_OP_READ_REG,
                    "reg": reg_addr,
                    "flags": 0,
                    "value": 0,
                    "count": 0,
                    "_visible_result": True,
                    "_result_cmd": "READ",
                })

            elif cmd_type == "CLEAR":
                compiled_ops.append({
                    "opcode": PATTERN_OP_CLEAR_ADC,
                    "reg": 0,
                    "flags": 0,
                    "value": 0,
                    "count": 0,
                    "_visible_result": True,
                    "_result_cmd": "CLEAR",
                })

            elif cmd_type == "DELAY":
                if len(parts) < 2:
                    raise ValueError("DELAY требует параметр: количество SPI-шагов")
                delay_count = int(parts[1], 0)
                if delay_count < 0:
                    raise ValueError("DELAY не может быть отрицательным")
                compiled_ops.append({
                    "opcode": PATTERN_OP_DELAY,
                    "reg": 0,
                    "flags": 0,
                    "value": 0,
                    "count": delay_count,
                    "_visible_result": True,
                    "_result_cmd": "DELAY",
                })

            else:
                raise ValueError(f"Неизвестная команда: {cmd_type}")

            validated_commands.append(cmd_line)

        return validated_commands, compiled_ops

    def _supports_batch_pattern(self):
        return bool(self.spi) and hasattr(self.spi, "supports_batch_pattern") and self.spi.supports_batch_pattern()

    def _prepare_pattern_runtime_state(self):
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

        reg34 = read_intan_register(self.spi, 34, verbose=False)
        reg35 = read_intan_register(self.spi, 35, verbose=False)
        step_reg34_is_1ua = reg34 == RHS2116_STIM_STEP_1UA
        step_reg35_is_1ua = reg35 == RHS2116_STIM_BIAS_1UA
        if not (step_reg34_is_1ua and step_reg35_is_1ua):
            if self.verbose:
                self._log(
                    "⚠ Register 34/35 не установлены для шага 1 µA "
                    f"(0x{reg34:04X}/0x{reg35:04X}), восстанавливаем стандартные значения..."
                )
            write_intan_register(self.spi, 34, RHS2116_STIM_STEP_1UA, u_flag=0, m_flag=0, verbose=False)
            write_intan_register(self.spi, 35, RHS2116_STIM_BIAS_1UA, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)

    def _format_batch_results(self, compiled_ops, executed_ops, completed_ops):
        results = []
        for idx, (compiled_op, executed_op) in enumerate(zip(compiled_ops, executed_ops)):
            if idx >= completed_ops:
                break
            if not compiled_op.get("_visible_result", True):
                continue

            cmd_type = compiled_op.get("_result_cmd")
            if cmd_type == "WRITE":
                results.append({
                    "cmd": "WRITE",
                    "reg": executed_op["reg"],
                    "value": executed_op["value"],
                    "status": "ok",
                })
            elif cmd_type == "READ":
                results.append({
                    "cmd": "READ",
                    "reg": executed_op["reg"],
                    "value": executed_op["value"],
                    "status": "ok",
                })
            elif cmd_type == "CLEAR":
                results.append({"cmd": "CLEAR", "status": "ok"})
            elif cmd_type == "DELAY":
                results.append({
                    "cmd": "DELAY",
                    "count": executed_op["count"],
                    "status": "ok",
                })
        return results

    def _execute_pattern_batch(self, compiled_ops, repeat_count=1):
        results = []
        driver_ops = [
            {
                "opcode": op["opcode"],
                "reg": op.get("reg", 0),
                "flags": op.get("flags", 0),
                "value": op.get("value", 0),
                "count": op.get("count", 0),
            }
            for op in compiled_ops
        ]

        for repeat_idx in range(repeat_count):
            batch_result = self.spi.run_pattern(driver_ops)
            results = self._format_batch_results(
                compiled_ops,
                batch_result.get("ops", []),
                batch_result.get("completed_ops", len(compiled_ops)),
            )
            if repeat_idx < repeat_count - 1:
                results = []

        return results

    def load_pattern(self, pattern_commands):
        """
        Загружает паттерн команд в память устройства без выполнения.
        
        Args:
            pattern_commands: список команд в формате:
                - "WRITE reg_addr value [U] [M]" - запись в регистр
                - "READ reg_addr" - чтение регистра
                - "CLEAR" - команда CLEAR
                - "DELAY X" - задержка (X одношаговых SPI transfer)
        
        Returns:
            количество загруженных команд
        """
        validated_commands, compiled_ops = self._compile_pattern_commands(pattern_commands)
        self.pattern_commands = validated_commands
        self.pattern_ops = compiled_ops
        self.pattern_loaded = True
        self._log(
            f"✓ Паттерн загружен в память: {len(validated_commands)} команд, "
            f"{len(compiled_ops)} batch-операций"
        )
        
        return len(validated_commands)

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
        
        return self.execute_pattern(
            self.pattern_commands,
            repeat_count=repeat_count,
            compiled_ops=self.pattern_ops,
        )

    def stop_all(self):
        """
        Выключает все стимуляторы.
        """
        def _run():
            if not self.initialized or self.spi is None:
                return
            write_intan_register(self.spi, 44, 0x0000, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            write_intan_register(self.spi, 46, 0x0000, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            write_intan_register(self.spi, 48, 0x0000, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            write_intan_register(self.spi, 42, 0x0000, u_flag=1, m_flag=0, verbose=False)
            time.sleep(0.001)
            write_intan_register(self.spi, 32, 0x0000, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            write_intan_register(self.spi, 33, 0x0000, u_flag=0, m_flag=0, verbose=False)
            time.sleep(0.001)
            clear_compliance_monitor(self.spi, verbose=False)
            self.current_mode = "idle"

        self._run_coordinated_stimulation("stop", _run)

    def _execute_pattern_slow(self, compiled_ops, repeat_count=1):
        """
        Совместимый по-командный fallback, если batch ioctl недоступен.
        """
        results = []
        for repeat_idx in range(repeat_count):
            for i, op in enumerate(compiled_ops):
                try:
                    cmd_type = op.get("_result_cmd")
                    opcode = op["opcode"]

                    if opcode == PATTERN_OP_WRITE_REG:
                        reg_addr = op["reg"]
                        value = op["value"]
                        u_flag = op.get("flags", 0) & 0x1
                        m_flag = (op.get("flags", 0) >> 1) & 0x1

                        write_intan_register(self.spi, reg_addr, value, u_flag=u_flag, m_flag=m_flag, verbose=False)
                        if u_flag == 1 or reg_addr == 42:
                            time.sleep(0.002)
                            if self.verbose and reg_addr == 42:
                                read_back = read_intan_register(self.spi, reg_addr, verbose=False)
                                if read_back != value:
                                    self._log(
                                        f"⚠ ВНИМАНИЕ: Регистр {reg_addr} не применился! "
                                        f"Записано: 0x{value:04X}, прочитано: 0x{read_back:04X}"
                                    )

                        if op.get("_visible_result", True):
                            results.append({"cmd": "WRITE", "reg": reg_addr, "value": value, "status": "ok"})

                    elif opcode == PATTERN_OP_READ_REG:
                        reg_addr = op["reg"]
                        reg_value = read_intan_register(self.spi, reg_addr, verbose=False)
                        if op.get("_visible_result", True):
                            results.append({"cmd": "READ", "reg": reg_addr, "value": reg_value, "status": "ok"})

                    elif opcode == PATTERN_OP_CLEAR_ADC:
                        clear_adc(self.spi, verbose=False)
                        if op.get("_visible_result", True):
                            results.append({"cmd": "CLEAR", "status": "ok"})

                    elif opcode == PATTERN_OP_DELAY:
                        delay_count = op.get("count", 0)
                        for _ in range(delay_count):
                            self.spi.delay_step()
                        if op.get("_visible_result", True):
                            results.append({"cmd": "DELAY", "count": delay_count, "status": "ok"})

                    else:
                        raise ValueError(f"Неизвестная batch-операция: {opcode}")

                except Exception as e:
                    results.append({"cmd": cmd_type or opcode, "status": "error", "error": str(e)})

            if repeat_idx < repeat_count - 1:
                results = []

        return results

    def execute_pattern(self, pattern_commands, repeat_count=1, compiled_ops=None):
        """
        Выполняет паттерн команд для стимуляции.
        
        Args:
            pattern_commands: список команд в формате:
                - "WRITE reg_addr value [U] [M]" - запись в регистр
                - "READ reg_addr" - чтение регистра
                - "CLEAR" - команда CLEAR
                - "DELAY X" - задержка (X одношаговых SPI transfer)
            repeat_count: количество повторений паттерна (по умолчанию 1)
            compiled_ops: опциональный скомпилированный batch-представление паттерна
        
        Returns:
            список результатов выполнения команд (только для последнего повторения)
        """
        def _run():
            try:
                self.ensure_stimulation_ready()
            except Exception as e:
                raise RuntimeError(f"Ошибка инициализации: {e}")

            if repeat_count < 1:
                raise ValueError("repeat_count должен быть >= 1")

            self._prepare_pattern_runtime_state()

            effective_compiled_ops = compiled_ops
            if effective_compiled_ops is None:
                _, effective_compiled_ops = self._compile_pattern_commands(pattern_commands)

            if self._supports_batch_pattern() and effective_compiled_ops:
                try:
                    return self._execute_pattern_batch(effective_compiled_ops, repeat_count=repeat_count)
                except AttributeError:
                    pass
                except OSError as e:
                    if getattr(e, "errno", None) != 25:
                        raise
                    if self.verbose:
                        self._log("⚠ Batch ioctl недоступен в текущем модуле, откат на per-command path")

            return self._execute_pattern_slow(effective_compiled_ops, repeat_count=repeat_count)

        return self._run_coordinated_stimulation("pattern_run", _run)

    def close(self):
        """
        Закрывает SPI. Питание можно оставить включенным.
        """
        try:
            self.stop_all()
        except Exception:
            pass
        try:
            if self.spi is not None:
                self.spi.close()
        except Exception:
            pass
        self.current_mode = "unknown"


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

    def _compute_impedance_metrics_from_samples(self, samples, freq_hz, c_farad, samples_per_period=None):
        """ADC samples -> Z (ohm), v_amp_uv, phase_deg. Phase is Z phase relative to current."""
        if not samples:
            return 0.0, 0.0, 0.0
        values_uv = [0.195 * (int(adc) - 32768) for adc in samples]
        mean_uv = sum(values_uv) / len(values_uv)
        values_ac = [x - mean_uv for x in values_uv]
        n = len(values_ac)
        if not samples_per_period or samples_per_period <= 0:
            samples_per_period = n
        sin_proj = 0.0
        cos_proj = 0.0
        for i, x in enumerate(values_ac):
            angle = 2.0 * math.pi * (i % samples_per_period) / samples_per_period
            sin_proj += x * math.sin(angle)
            cos_proj += x * math.cos(angle)
        sin_coeff = (2.0 / n) * sin_proj
        cos_coeff = (2.0 / n) * cos_proj
        v_amp_uv = (sin_coeff * sin_coeff + cos_coeff * cos_coeff) ** 0.5
        v_dac = 0.6125
        i_amp = 2.0 * math.pi * freq_hz * c_farad * v_dac
        z_ohm = (v_amp_uv * 1e-6 / i_amp) if i_amp > 0 else 0.0
        phase_v_vs_dac_deg = math.degrees(math.atan2(cos_coeff, sin_coeff))
        phase_deg = phase_v_vs_dac_deg - 90.0
        while phase_deg <= -180.0:
            phase_deg += 360.0
        while phase_deg > 180.0:
            phase_deg -= 360.0
        return z_ohm, v_amp_uv, phase_deg

    def _compute_impedance_metrics_from_accumulators(self, sin_accum, cos_accum, sample_count, freq_hz, c_farad, samples_per_period):
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

        # The driver uses a zero-mean basis scaled by samples_per_period, so
        # convert the fitted basis coefficients back to ADC-code amplitudes.
        basis_scale = 127.0 * float(samples_per_period)
        sin_coeff_codes = basis_scale * sin_coeff_basis
        cos_coeff_codes = basis_scale * cos_coeff_basis
        v_amp_uv = 0.195 * ((sin_coeff_codes * sin_coeff_codes) + (cos_coeff_codes * cos_coeff_codes)) ** 0.5
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
        """Быстрый замер импеданса через /dev/intan с одной командой GUI->сервер."""
        self.controller.ensure_initialized()
        if not hasattr(self.controller.spi, "measure_impedance_raw"):
            raise RuntimeError("measure_impedance_fast требует backend /dev/intan")

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
                saved_state[reg] = read_intan_register(self.controller.spi, reg, verbose=False)

            self.controller._run_rhs2116_sequence(rhs2116_safe_impedance_commands())

            if phase_safe:
                reg1_before = saved_state.get(1)
                # RHS2116 Register 1 layout:
                # bit 5 = absmode, bit 4 = DSPen, bits 3:0 = DSP cutoff.
                # For true phase-safe mode we must clear all of them.
                reg1_applied = reg1_before & ~0x003F
                write_intan_register(self.controller.spi, 1, reg1_applied, u_flag=0, m_flag=0, verbose=False)
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
                    raise RuntimeError("Автовыбор шкалы не получил данных от драйвера")
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
                write_intan_register(self.controller.spi, 1, reg_restore, u_flag=0, m_flag=0, verbose=False)
            if saved_state:
                write_intan_register(self.controller.spi, 32, saved_state.get(32, 0x0000), u_flag=0, m_flag=0, verbose=False)
                write_intan_register(self.controller.spi, 33, saved_state.get(33, 0x0000), u_flag=0, m_flag=0, verbose=False)
                write_intan_register(self.controller.spi, 2, saved_state.get(2, 0x0000), u_flag=0, m_flag=0, verbose=False)
                write_intan_register(self.controller.spi, 3, saved_state.get(3, 0x0080), u_flag=0, m_flag=0, verbose=False)
                write_intan_register(self.controller.spi, 44, saved_state.get(44, 0x0000), u_flag=0, m_flag=0, verbose=False)
                write_intan_register(self.controller.spi, 46, saved_state.get(46, 0x0000), u_flag=0, m_flag=0, verbose=False)
                write_intan_register(self.controller.spi, 48, saved_state.get(48, 0x0000), u_flag=0, m_flag=0, verbose=False)
                write_intan_register(self.controller.spi, 42, saved_state.get(42, 0x0000), u_flag=1, m_flag=0, verbose=False)

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

        if cmd == "WRITE":
            if len(parts) < 3:
                raise ValueError("WRITE требует формат: WRITE <reg> <value> [U] [M]")
            reg_addr = int(parts[1], 0)
            value = int(parts[2], 0)
            u_flag = self._parse_flag(parts, "U", default=0)
            m_flag = self._parse_flag(parts, "M", default=0)
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
                self.controller.spi.delay_step()
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
                adc_value = self.controller.spi.convert_channel(channel, amp_type=amp_type, h_flag=h_flag)
            else:
                cmd_word = 0x00000000
                cmd_word |= (channel << 16)
                if d_flag:
                    cmd_word |= (1 << 27)
                if h_flag:
                    cmd_word |= (1 << 26)

                cmd_bytes = [
                    (cmd_word >> 24) & 0xFF,
                    (cmd_word >> 16) & 0xFF,
                    (cmd_word >> 8) & 0xFF,
                    cmd_word & 0xFF,
                ]
                dummy = [0x00, 0x00, 0x00, 0x00]
                self.controller.spi.transfer(cmd_bytes)
                self.controller.spi.transfer(dummy)
                resp3 = self.controller.spi.transfer(dummy)
                adc_value = (resp3[0] << 8) | resp3[1]

            # Совместимо с текущим GUI: он ищет 0x... и берет старшие 16 бит результата.
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
            with self.controller.shared_state.chip_lock:
                self.controller.ensure_recording_ready()
            return {"status": "ok", "cmd": "init"}

        if cmd == "close":
            with self.controller.shared_state.chip_lock:
                self.controller.close()
            return {"status": "ok", "cmd": "close"}

        if cmd == "read_temperature":
            raise RuntimeError(
                "RHS2116 не имеет отдельного temperature register в текущем контракте. "
                "Команда read_temperature отключена, потому что Register 3 используется для Zcheck DAC."
            )

        if cmd == "read_register":
            """Читает значение регистра Intan"""
            address = int(cmd_obj.get("address", 255))
            if not (0 <= address <= 255):
                raise ValueError("address должен быть в диапазоне 0-255")

            with self.controller.shared_state.chip_lock:
                self.controller.ensure_initialized()
                value = read_intan_register(self.controller.spi, address, verbose=self.controller.verbose)
            return {
                "status": "ok",
                "cmd": "read_register",
                "address": address,
                "value": value
            }

        if cmd == "send_line":
            raw_line = cmd_obj.get("line", "")
            with self.controller.shared_state.chip_lock:
                response = self._process_send_line(raw_line)
            return {
                "status": "ok",
                "cmd": "send_line",
                "line": raw_line,
                "response": response,
            }

        if cmd == "measure_impedance_fast":
            with self.controller.shared_state.chip_lock:
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

            with self.controller.shared_state.chip_lock:
                controller_was_initialized = self.controller.initialized
                controller_mode_before = self.controller.current_mode

                from intan_udp_recorder import IntanRecorder
                recorder = IntanRecorder(
                    gpio_number=self.controller.gpio_number,
                    spi_device=self.controller.spi_device,
                    verbose=self.controller.verbose,
                    shared_state=self.controller.shared_state,
                )
                if not recorder.spi:
                    recorder.spi = self.controller.spi
                if not recorder.gpio:
                    recorder.gpio = self.controller.gpio

                num_measurements = int(cmd_obj.get("num_measurements", 3))
                measurements = []
                for i in range(num_measurements):
                    result = recorder.measure_impedance(
                        channel=channel,
                        test_current_nA=test_current_nA,
                        frequency=frequency,
                        num_samples=num_samples
                    )
                    measurements.append(result)
                    if i < num_measurements - 1:
                        time.sleep(0.1)

                if num_measurements > 1:
                    avg_impedance = sum(m.get("impedance_ohm", 0) for m in measurements) / len(measurements)
                    avg_adc_rms = sum(m.get("adc_rms", 0) for m in measurements) / len(measurements)
                    avg_voltage_rms = sum(m.get("voltage_rms", 0) for m in measurements) / len(measurements)
                    avg_voltage_peak = sum(m.get("voltage_peak", 0) for m in measurements) / len(measurements)

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
                        "std_dev_ohm": float(std_dev),
                        "measurements": measurements
                    }
                else:
                    result = measurements[0] if measurements else {}

                if controller_was_initialized:
                    if controller_mode_before == "stimulation":
                        if self.controller.verbose:
                            self.controller._log("Восстановление stimulation mode после измерения импеданса...")
                        self.controller.ensure_stimulation_ready()
                    elif controller_mode_before == "recording":
                        if self.controller.verbose:
                            self.controller._log("Восстановление recording mode после измерения импеданса...")
                        self.controller.ensure_recording_ready()
                    else:
                        self.controller.current_mode = controller_mode_before
            
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
            with self.controller.shared_state.chip_lock:
                self.controller.ensure_recording_ready()
            
                # Получаем значение Register 0 из команды или формируем из параметров
                if "register_0" in cmd_obj:
                    reg0_value = int(cmd_obj.get("register_0"))
                else:
                    adc_buffer_bias = int(cmd_obj.get("adc_buffer_bias", 3))
                    mux_bias = int(cmd_obj.get("mux_bias", 5))

                    if not (0 <= adc_buffer_bias <= 255):
                        raise ValueError("ADC buffer bias должен быть в диапазоне 0-255")
                    if not (0 <= mux_bias <= 255):
                        raise ValueError("MUX bias должен быть в диапазоне 0-255")

                    reg0_value = (adc_buffer_bias << 8) | mux_bias

                write_intan_register(
                    self.controller.spi,
                    0,
                    reg0_value,
                    u_flag=0,
                    m_flag=0,
                    verbose=self.controller.verbose
                )
                time.sleep(0.001)
            
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
            with self.controller.shared_state.chip_lock:
                self.controller.ensure_recording_ready()

                reg1 = int(cmd_obj.get("register_1", 0x051A))
                reg4 = int(cmd_obj.get("register_4", 0x0016))
                reg5 = int(cmd_obj.get("register_5", 0x0017))
                reg6 = int(cmd_obj.get("register_6", 0x00A8))
                reg7 = int(cmd_obj.get("register_7", 0x000A))

                for reg_val, reg_name in [(reg1, "Register 1"), (reg4, "Register 4"),
                                          (reg5, "Register 5"), (reg6, "Register 6"),
                                          (reg7, "Register 7")]:
                    if not (0 <= reg_val <= 0xFFFF):
                        raise ValueError(f"{reg_name} должен быть в диапазоне 0-0xFFFF")

                write_intan_register(self.controller.spi, 1, reg1, u_flag=0, m_flag=0, verbose=self.controller.verbose)
                time.sleep(0.001)
                write_intan_register(self.controller.spi, 4, reg4, u_flag=0, m_flag=0, verbose=self.controller.verbose)
                time.sleep(0.001)
                write_intan_register(self.controller.spi, 5, reg5, u_flag=0, m_flag=0, verbose=self.controller.verbose)
                time.sleep(0.001)
                write_intan_register(self.controller.spi, 6, reg6, u_flag=0, m_flag=0, verbose=self.controller.verbose)
                time.sleep(0.001)
                write_intan_register(self.controller.spi, 7, reg7, u_flag=0, m_flag=0, verbose=self.controller.verbose)
                time.sleep(0.001)
            
            # RHS2116 Register 1: bit 4 = DSPen, bits 3:0 = DSP cutoff.
            dsp_enabled = (reg1 >> 4) & 0x1
            dsp_cutoff = reg1 & 0xF
            absmode = (reg1 >> 5) & 0x1
            
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
                "dsp_enabled": dsp_enabled,
                "dsp_cutoff": dsp_cutoff,
                "absmode": absmode,
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
        default=get_preferred_spi_device(),
        help="Путь к SPI устройству (по умолчанию: /dev/intan, если доступен, иначе /dev/spidev1.1)",
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

