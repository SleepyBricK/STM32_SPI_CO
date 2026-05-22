#!/usr/bin/env python3
"""
UDP-сервер для регистрации данных с Intan RHS2116.

Читает данные ADC со всех 16 каналов и отправляет их по UDP клиентам.
Использует CONVERT команды для получения данных с усилителей.

ОПТИМИЗАЦИИ ПРОИЗВОДИТЕЛЬНОСТИ:
- DMA используется автоматически на уровне драйвера SPI в ядре Linux
- Используется CONVERT(63) для автоматического переключения каналов (максимальная скорость)
- Оптимизировано формирование UDP пакетов: предварительное выделение памяти, упаковка несколькими полями одним вызовом
- Буферизация samples для упаковки нескольких точек в один пакет (оптимальный размер ~1400 байт)
- Убраны все логи в циклах регистрации для минимальных накладных расходов
- Предварительно вычисленные команды SPI для уменьшения вычислений в цикле

Запуск:
  python3 intan_udp_recorder.py --udp-port 9001 --channels 0-15 --sample-rate 40000
"""

import argparse
import socket
import struct
import threading
import time
import sys
import os
import math

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    # Fallback для систем без numpy (используем простые вычисления)
    NUMPY_AVAILABLE = False
    
    class np:
        @staticmethod
        def array(data):
            # Возвращаем список, но операции будут выполняться через встроенные функции
            return data if isinstance(data, list) else list(data)
        @staticmethod
        def sqrt(x):
            return math.sqrt(x)
        @staticmethod
        def mean(data):
            if isinstance(data, list):
                return sum(data) / len(data) if len(data) > 0 else 0
            return float(data)

from stimulate_channel0 import (
    GPIOController,
    GPIOError,
    SPIController,
    get_preferred_spi_device,
    read_intan_register,
    write_intan_register,
    clear_adc,
    clear_compliance_monitor,
)
from rhs2116_profiles import (
    rhs2116_recording_init_commands,
    rhs2116_register0_for_adc_rate,
    rhs2116_safe_impedance_commands,
    rhs2116_stimulation_init_commands,
    rhs2116_validate_channels,
    run_rhs2116_sequence,
)
from intan_shared_state import IntanSharedState


class IntanRecorder:
    """Класс для регистрации данных с Intan RHS2116"""

    def __init__(self, gpio_number=226, spi_device="/dev/spidev1.1", verbose=False, shared_state=None):
        self.gpio_number = gpio_number
        self.spi_device = get_preferred_spi_device(spi_device)
        self.verbose = verbose
        self.shared_state = shared_state or IntanSharedState()

        self.gpio = None
        self.spi = None
        self.initialized = False
        self.recording = False
        self.lock = threading.Lock()
        self.using_driver = self.spi_device == "/dev/intan"

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

    def ensure_initialized(self):
        """Инициализирует GPIO, SPI и чип Intan"""
        with self.lock:
            if self.initialized:
                return

            self._log("== Инициализация Intan для регистрации ==")

            try:
                # Инициализация GPIO нужна только для userspace spidev-пути.
                if self.using_driver:
                    self._log("Используется драйвер /dev/intan: GPIO питания управляется ядром")
                else:
                    self.gpio = GPIOController(self.gpio_number, raise_exceptions=True)
                    self.gpio.set_direction("out")
                    self.gpio.set_value(1)  # Включаем питание
                    time.sleep(0.1)

                # Инициализация SPI
                if not os.path.exists(self.spi_device):
                    raise FileNotFoundError(f"SPI устройство не найдено: {self.spi_device}")
                
                # Инициализация SPI: 10 МГц — xfer2 быстрее batch на этой скорости
                # DMA используется автоматически на уровне драйвера SPI в ядре Linux
                # для эффективной передачи данных без участия CPU
                self.spi = SPIController(
                    device=self.spi_device,
                    max_speed_hz=10000000,  # 10 МГц — оптимально для xfer2
                    mode=0,
                )
                self.spi.open()

                # Проверка чипа
                chip_id = read_intan_register(self.spi, 255, verbose=False)
                if chip_id != 32:
                    raise RuntimeError(f"Неверный Chip ID: {chip_id} (ожидалось 32)")

                # Инициализация чипа для регистрации (без стимуляции).
                self._log("Инициализация чипа для регистрации данных...")
                self._initialize_for_recording(verbose=self.verbose, adc_sampling_rate_ksps=480)

                self.initialized = True
                self._log("== Инициализация завершена ==")
            except Exception as e:
                self._log(f"❌ Ошибка инициализации: {e}")
                raise

    def _reinitialize_for_stimulation(self, verbose=False):
        """
        Полная переинициализация чипа для стимуляции после измерения импеданса.
        Аналогична _initialize_for_stimulation из IntanController.
        """
        if verbose:
            self._log("      Полная переинициализация для стимуляции...")

        self._run_rhs2116_sequence(rhs2116_stimulation_init_commands(adc_sampling_rate_ksps=480.0))
        
        if verbose:
            self._log("        ✓ Переинициализация для стимуляции завершена")

    def _initialize_for_recording(self, verbose=False, adc_sampling_rate_ksps=480):
        """
        Инициализация чипа для регистрации данных (без стимуляции).
        
        ВАЖНО: Регистры 32-33 остаются в 0x0000 для дифференциального режима усилителей.
        Это критично для правильной работы EMG регистрации.
        
        Согласно даташиту:
        - Register 0: ADC buffer bias и MUX bias зависят от частоты дискретизации
        - Register 1: DSP фильтр и auxiliary outputs
        - Register 2: Impedance testing DAC (опционально)
        - Register 3: Impedance check DAC (опционально)
        - Registers 4-7, 12: Настройка полосы пропускания усилителей
        - Register 8: High-Gain Amplifier Power
        - Register 9: Low-Gain Amplifier Power
        - Registers 32-33: ДОЛЖНЫ остаться в 0x0000 (не устанавливаем в 0xAAAA/0x00FF)
        
        Args:
            verbose: выводить отладочную информацию
            adc_sampling_rate_ksps: частота дискретизации ADC в kS/s (по умолчанию 480 kS/s)
        """
        if verbose:
            self._log(f"      WRITE 0 - настройка ADC (частота дискретизации: {adc_sampling_rate_ksps} kS/s)...")
            self._log(f"        Register 0: 0x{rhs2116_register0_for_adc_rate(adc_sampling_rate_ksps):04X}")
        self._run_rhs2116_sequence(rhs2116_recording_init_commands(adc_sampling_rate_ksps))

        if verbose:
            self._log("      ✓ Инициализация для регистрации завершена (регистры 32-33 остались в 0x0000)")

    def convert_channel(self, channel, amp_type='ac', h_flag=0):
        """
        Выполняет CONVERT команду для канала и возвращает ADC значение.
        
        Согласно даташиту RHS2116 (стр. 32):
        - Биты [31:30] = 00 (команда CONVERT)
        - Бит [29] = U (Update flag) = 0
        - Бит [28] = M (Monitor flag) = 0
        - Бит [27] = D (DC amplifier flag) = 1 для чтения DC, 0 для только AC
        - Бит [26] = H (High-pass filter flag) = 0 (обычно) или 1 (для сброса DSP HPF)
        - Биты [25:22] = 0000
        - Биты [21:16] = C[5:0] (номер канала, 6 бит, 0-15)
        - Биты [15:0] = 0x0000
        
        КРИТИЧНО: H flag = 1 мгновенно сбрасывает DSP high-pass filter.
        Это ОБЯЗАТЕЛЬНО после стимуляции для быстрого восстановления baseline.
        
        Результат:
        - Биты [31:16] = A[15:0] (AC high-gain amplifier, 16 бит)
        - Биты [15:10] = 000000
        - Биты [9:0] = W[9:0] (DC low-gain amplifier, 10 бит, только если D=1)
        
        Args:
            channel: номер канала (0-15)
            amp_type: тип усилителя ('ac' для high-gain, 'dc' для low-gain)
            h_flag: флаг сброса DSP HPF (0 = обычный режим, 1 = сброс фильтра)
        
        Returns:
            16-битное значение ADC (знаковое) для AC усилителя
        """
        if channel < 0 or channel > 15:
            raise ValueError(f"Номер канала должен быть 0-15, получено: {channel}")
        
        if hasattr(self.spi, "convert_channel"):
            return self.spi.convert_channel(channel, amp_type=amp_type, h_flag=h_flag)

        # Формируем команду CONVERT согласно даташиту
        # Биты [31:30] = 00
        # Бит [27] = D flag: 1 для чтения DC, 0 для только AC
        # Бит [26] = H flag: 1 для сброса DSP HPF, 0 для обычного режима
        d_flag = 1 if amp_type == 'dc' else 0
        
        # Команда CONVERT: биты [31:30] = 00, биты [21:16] = канал
        cmd_word = 0x00000000  # CONVERT команда
        cmd_word |= (channel << 16)  # номер канала в битах [21:16]
        if d_flag:
            cmd_word |= (1 << 27)  # D flag для чтения DC усилителя
        if h_flag:
            cmd_word |= (1 << 26)  # H flag для сброса DSP HPF
        
        # Преобразуем в байты (MSB first)
        cmd = [
            (cmd_word >> 24) & 0xFF,
            (cmd_word >> 16) & 0xFF,
            (cmd_word >> 8) & 0xFF,
            cmd_word & 0xFF
        ]
        
        # Отправляем команду CONVERT (3 фазы как в READ/WRITE)
        # Важно: каждый transfer поднимает CS, что нужно для Intan pipeline
        # Согласно даташиту, результат CONVERT приходит через два SPI цикла (N приходит в N+2)
        if not hasattr(self, '_dummy_cmd'):
            self._dummy_cmd = [0x00, 0x00, 0x00, 0x00]
        resp1 = self.spi.transfer(cmd)  # Отправка CONVERT команды, resp1 содержит результат команды N-2
        resp2 = self.spi.transfer(self._dummy_cmd)  # Dummy, resp2 содержит результат команды N-1
        resp3 = self.spi.transfer(self._dummy_cmd)  # Dummy, resp3 содержит результат команды N (текущей CONVERT)
        
        # Согласно даташиту, результат CONVERT:
        # - Биты [31:16] = A[15:0] (AC high-gain amplifier, 16 бит) - это то, что нам нужно
        # - Биты [15:10] = 000000
        # - Биты [9:0] = W[9:0] (DC low-gain amplifier, 10 бит, только если D=1)
        
        # Данные AC усилителя приходят в старших 16 битах результата (resp3[0:2])
        # resp3[0] и resp3[1] содержат старшие байты AC результата для команды, отправленной в resp1
        adc_value = (resp3[0] << 8) | resp3[1]
        return adc_value

    def reset_dsp_hpf(self, channel=0):
        """
        Сбрасывает DSP high-pass filter для указанного канала.
        
        КРИТИЧНО: После стимуляции DSP HPF может быть насыщен остаточными DC-смещениями.
        Сброс через H flag мгновенно восстанавливает baseline.
        
        Согласно даташиту: "Each channel's DSP high-pass filter can be instantly reset
        by setting the H flag of the CONVERT command to one."
        
        Args:
            channel: номер канала (0-15), по умолчанию 0
        
        Returns:
            16-битное значение ADC (беззнаковое, 0-65535)
        """
        return self.convert_channel(channel, amp_type='ac', h_flag=1)

    def convert_channel_auto(self):
        """
        Выполняет CONVERT(63) для автоматического переключения на следующий канал.
        Согласно даташиту, CONVERT(63) автоматически инкрементирует мультиплексор.
        Использует 3 транзакции (результат в pipeline N+2) — для одиночных вызовов.
        """
        if hasattr(self.spi, "convert_channel_auto"):
            return self.spi.convert_channel_auto()

        cmd = [0x00, 0x3F, 0x00, 0x00]  # CONVERT(63)
        if not hasattr(self, '_dummy_cmd'):
            self._dummy_cmd = [0x00, 0x00, 0x00, 0x00]
        resp1 = self.spi.transfer(cmd)
        resp2 = self.spi.transfer(self._dummy_cmd)
        resp3 = self.spi.transfer(self._dummy_cmd)
        adc_value = (resp3[0] << 8) | resp3[1]
        return adc_value

    def _convert_1tx(self, cmd_bytes):
        """
        Одна SPI-транзакция CONVERT. Результат приходит с задержкой 2 (pipeline).
        Ответ текущей транзакции = результат команды N-2.
        Используется в record_channels для максимальной скорости.
        """
        resp = self.spi.transfer(cmd_bytes)
        return (resp[0] << 8) | resp[1]

    def read_temperature(self):
        raise RuntimeError(
            "RHS2116 не предоставляет отдельную temperature-команду в текущем контракте. "
            "Register 3 зарезервирован под Zcheck DAC и не должен читаться как температура."
        )

    def measure_impedance(self, channel, test_current_nA=5, frequency=1000, num_samples=5000):
        """
        Измеряет импеданс электрода на указанном канале.
        
        Согласно даташиту RHS2116 (стр. 17-19, 36-37):
        - Zcheck использует ТОЛЬКО Register 2 и Register 3 (рег. 15-20 не существуют)
        - Register 2: Zcheck en, Zcheck select (канал), Zcheck scale (CS: 0.1/1/10 pF), Zcheck DAC power
        - Register 3: 8-бит DAC — ОБЯЗАТЕЛЬНО обновлять во времени для синусоиды!
        - I_peak = 2*pi*f*CS*VA; при 1 kHz: 0.1pF→0.38nA, 1pF→3.8nA, 10pF→38nA (макс. VA=0.6125V)
        - Z = V_peak / I_peak; AC усилители насыщаются при |Vin| > ±5 mV
        
        Args:
            channel: номер канала (0-15)
            test_current_nA: целевой пиковый ток в nA (по умолчанию 5 nA)
            frequency: частота тестового сигнала в Hz (по умолчанию 1000 Hz)
            num_samples: желаемое число samples (округлится до целых периодов)
        
        Returns:
            dict с impedance_ohm, adc_rms, voltage_rms, test_current_nA, frequency, channel
        """
        # ВАЖНО: Сохраняем состояние ВСЕХ регистров стимуляции ДО инициализации
        # потому что ensure_initialized() может вызвать _initialize_for_recording(),
        # которая установит регистры 32-33 в 0x0000
        # Регистры 64-79 и 96-111 являются triggered registers и требуют флага U для активации
        if self.spi is not None:
            try:
                reg32_before = read_intan_register(self.spi, 32, verbose=False)
                reg33_before = read_intan_register(self.spi, 33, verbose=False)
                reg42_before = read_intan_register(self.spi, 42, verbose=False)
                reg44_before = read_intan_register(self.spi, 44, verbose=False)
                reg2_before = read_intan_register(self.spi, 2, verbose=False)
                reg3_before = read_intan_register(self.spi, 3, verbose=False)
                reg34_before = read_intan_register(self.spi, 34, verbose=False)  # Step size
                reg35_before = read_intan_register(self.spi, 35, verbose=False)  # PBIAS/NBIAS
                
                # Сохраняем все регистры токов стимуляции (64-79 для отрицательных, 96-111 для положительных)
                regs_64_79_before = []  # Отрицательные токи (triggered registers)
                regs_96_111_before = []  # Положительные токи (triggered registers)
                for ch in range(16):
                    regs_64_79_before.append(read_intan_register(self.spi, 64 + ch, verbose=False))
                    regs_96_111_before.append(read_intan_register(self.spi, 96 + ch, verbose=False))
            except:
                # Если не удалось прочитать (чип не инициализирован), используем значения по умолчанию
                reg32_before = 0x0000
                reg33_before = 0x0000
                reg42_before = 0x0000
                reg44_before = 0x0000
                reg2_before = 0x0000
                reg3_before = 0x0000
                reg34_before = 0x0000
                reg35_before = 0x0000
                regs_64_79_before = [0x8000] * 16  # Значение по умолчанию для токов
                regs_96_111_before = [0x8000] * 16
        else:
            reg32_before = 0x0000
            reg33_before = 0x0000
            reg42_before = 0x0000
            reg44_before = 0x0000
            reg2_before = 0x0000
            reg3_before = 0x0000
            reg34_before = 0x0000
            reg35_before = 0x0000
            regs_64_79_before = [0x8000] * 16
            regs_96_111_before = [0x8000] * 16
        
        # Теперь инициализируем (это может изменить регистры 32-33)
        self.ensure_initialized()
        
        if channel < 0 or channel > 15:
            raise ValueError(f"Номер канала должен быть 0-15, получено: {channel}")
        
        if test_current_nA <= 0 or test_current_nA > 1000:
            raise ValueError(f"Тестовый ток должен быть в диапазоне 0-1000 nA, получено: {test_current_nA} nA")
        
        if self.verbose:
            self._log(f"Измерение импеданса: канал {channel}, ток {test_current_nA} nA, частота {frequency} Hz")
            self._log(f"      Сохранено состояние: Reg32=0x{reg32_before:04X}, Reg33=0x{reg33_before:04X}, Reg42=0x{reg42_before:04X}, Reg44=0x{reg44_before:04X}")
            self._log(f"      Reg2=0x{reg2_before:04X}, Reg3=0x{reg3_before:04X}, Reg34=0x{reg34_before:04X}, Reg35=0x{reg35_before:04X}")
            self._log(f"      Регистры токов стимуляции сохранены (64-79, 96-111)")

        self._run_rhs2116_sequence(rhs2116_safe_impedance_commands())
        
        # --- RHS2116 Zcheck: ТОЛЬКО Register 2 и 3 (рег. 15-20 не существуют) ---
        # Register 2: D[13:8]=channel, D[6]=DAC power, D[5]=load(0), D[4:3]=scale, D[0]=en
        # Zcheck scale: 00=0.1pF, 01=1pF, 11=10pF. Ток при 1kHz макс: 0.38/3.8/38 nA
        if test_current_nA <= 0.38:
            cs_scale = 0   # 0.1 pF
            cs_pF = 0.1
            i_max_nA = 0.38
        elif test_current_nA <= 3.8:
            cs_scale = 1   # 1 pF
            cs_pF = 1.0
            i_max_nA = 3.8
        else:
            cs_scale = 3   # 10 pF (биты 11)
            cs_pF = 10.0
            i_max_nA = 38.0
        amp_factor = min(1.0, test_current_nA / i_max_nA)  # 0..1
        dac_amplitude = max(1, int(127 * amp_factor))  # 1..127 от средней 128
        # Фактический ток: I_peak = 2*pi*f*CS*VA, VA=(dac_amp/127)*0.6125V
        va_volts = (dac_amplitude / 127.0) * 0.6125
        actual_current_nA = 2.0 * math.pi * frequency * cs_pF * 1e-12 * va_volts * 1e9
        
        reg2 = (channel << 8) | (1 << 6) | (1 << 0) | (cs_scale << 3)  # DAC power, Zcheck en, scale
        if self.verbose:
            self._log(f"      Register 2: канал {channel}, CS={cs_pF} pF, DAC amp={dac_amplitude}, I_peak≈{actual_current_nA:.2f} nA")
        write_intan_register(self.spi, 2, reg2, u_flag=0, m_flag=0, verbose=self.verbose)
        time.sleep(0.02)  # стабилизация DAC и переключателей
        
        # Сбор samples: КРИТИЧНО — обновлять Register 3 синусом на каждой итерации
        sample_rate_hz = 30000  # практическая частота CONVERT при одиночном канале
        samples_per_period = max(10, int(sample_rate_hz / frequency))
        num_periods = max(5, int(num_samples / samples_per_period))
        actual_samples = num_periods * samples_per_period
        
        if self.verbose:
            self._log(f"      Сбор {actual_samples} samples ({num_periods} периодов), обновление DAC по синусу...")
        
        adc_samples = []
        for i in range(actual_samples):
            phase = 2.0 * math.pi * (i % samples_per_period) / samples_per_period
            dac_val = 128 + int(dac_amplitude * math.sin(phase))
            dac_val = max(1, min(255, dac_val))
            write_intan_register(self.spi, 3, dac_val & 0xFF, u_flag=0, m_flag=0, verbose=False)
            adc_value = self.convert_channel(channel, amp_type='ac')
            adc_samples.append(adc_value)
            if i % 500 == 0 and self.verbose:
                self._log(f"        Sample {i}/{actual_samples}...")
        
        # Отключаем Zcheck
        write_intan_register(self.spi, 2, 0x0000, u_flag=0, m_flag=0, verbose=False)
        write_intan_register(self.spi, 3, 0x0080, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 8a. ВАЖНО: Восстанавливаем состояние регистров стимуляции после измерения
        # Это необходимо, чтобы стимуляция продолжала работать после измерения импеданса
        if self.verbose:
            self._log("      Восстановление состояния регистров стимуляции...")
        
        # Восстанавливаем регистры 32-33 в исходное состояние
        # Это критично для работы стимуляции после измерения импеданса
        write_intan_register(self.spi, 32, reg32_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(self.spi, 33, reg33_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # Проверяем, что регистры действительно восстановлены
        reg32_after = read_intan_register(self.spi, 32, verbose=False)
        reg33_after = read_intan_register(self.spi, 33, verbose=False)
        
        if self.verbose:
            if reg32_before == 0xAAAA and reg33_before == 0x00FF:
                if reg32_after == 0xAAAA and reg33_after == 0x00FF:
                    self._log("        Регистры 32-33 восстановлены в 0xAAAA/0x00FF для стимуляции ✓")
                else:
                    self._log(f"        ⚠ Регистры 32-33 не восстановлены! Ожидалось 0xAAAA/0x00FF, получено 0x{reg32_after:04X}/0x{reg33_after:04X}")
                    # Принудительно восстанавливаем
                    write_intan_register(self.spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=False)
                    time.sleep(0.001)
                    write_intan_register(self.spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=False)
                    time.sleep(0.001)
                    self._log("        Регистры 32-33 принудительно восстановлены в 0xAAAA/0x00FF")
            else:
                self._log(f"        Регистры 32-33 восстановлены в 0x{reg32_before:04X}/0x{reg33_before:04X}")
        
        # Восстанавливаем triggered-регистры через shadow write -> единый commit в Register 42.
        write_intan_register(self.spi, 44, reg44_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        for ch in range(16):
            write_intan_register(self.spi, 64 + ch, regs_64_79_before[ch], u_flag=0, m_flag=0, verbose=False)
            write_intan_register(self.spi, 96 + ch, regs_96_111_before[ch], u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(self.spi, 42, reg42_before, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # Проверяем, что регистры действительно восстановлены
        reg42_after = read_intan_register(self.spi, 42, verbose=False)
        reg44_after = read_intan_register(self.spi, 44, verbose=False)
        
        if self.verbose:
            if reg42_after == reg42_before and reg44_after == reg44_before:
                self._log(f"        Регистры 42/44 восстановлены: 0x{reg42_before:04X}/0x{reg44_before:04X} ✓")
            else:
                self._log(f"        ⚠ Регистры 42/44 не восстановлены! Ожидалось 0x{reg42_before:04X}/0x{reg44_before:04X}, получено 0x{reg42_after:04X}/0x{reg44_after:04X}")
                # Принудительно восстанавливаем через тот же commit policy.
                write_intan_register(self.spi, 44, reg44_before, u_flag=0, m_flag=0, verbose=False)
                time.sleep(0.001)
                for ch in range(16):
                    write_intan_register(self.spi, 64 + ch, regs_64_79_before[ch], u_flag=0, m_flag=0, verbose=False)
                    write_intan_register(self.spi, 96 + ch, regs_96_111_before[ch], u_flag=0, m_flag=0, verbose=False)
                time.sleep(0.001)
                write_intan_register(self.spi, 42, reg42_before, u_flag=1, m_flag=0, verbose=False)
                time.sleep(0.001)
                self._log(f"        Регистры 42/44 принудительно восстановлены в 0x{reg42_before:04X}/0x{reg44_before:04X}")
        
        # Восстанавливаем регистры 2 и 3 (impedance testing DAC)
        # Эти регистры могут быть изменены во время измерения импеданса
        write_intan_register(self.spi, 2, reg2_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(self.spi, 3, reg3_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        if self.verbose:
            self._log(f"        Регистры 2/3 восстановлены: 0x{reg2_before:04X}/0x{reg3_before:04X}")
        
        # Восстанавливаем регистры 34-35 (step size и PBIAS/NBIAS)
        # Эти регистры важны для правильной работы стимуляторов
        write_intan_register(self.spi, 34, reg34_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(self.spi, 35, reg35_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        if self.verbose:
            self._log(f"        Регистры 34/35 восстановлены: 0x{reg34_before:04X}/0x{reg35_before:04X}")
        
        # КРИТИЧНО: Если регистры были в состоянии для стимуляции, выполняем полную переинициализацию
        # Это гарантирует, что все регистры стимуляции будут правильно настроены
        # ВАЖНО: Переинициализация должна быть ДО восстановления регистров токов,
        # так как она устанавливает их в 0x8000, а потом мы восстановим нужные значения
        if reg32_before == 0xAAAA and reg33_before == 0x00FF:
            if self.verbose:
                self._log("        Выполняется полная переинициализация для стимуляции...")
            self._reinitialize_for_stimulation(verbose=self.verbose)
            
            # Дополнительно: убеждаемся, что регистры 32-33 установлены правильно
            reg32_final = read_intan_register(self.spi, 32, verbose=False)
            reg33_final = read_intan_register(self.spi, 33, verbose=False)
            if reg32_final != 0xAAAA or reg33_final != 0x00FF:
                if self.verbose:
                    self._log(f"        ⚠ Регистры 32-33 не установлены! Принудительно устанавливаем...")
                write_intan_register(self.spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=False)
                time.sleep(0.001)
                write_intan_register(self.spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=False)
                time.sleep(0.001)
                clear_compliance_monitor(self.spi, verbose=False)
                time.sleep(0.001)
                if self.verbose:
                    self._log("        ✓ Регистры 32-33 принудительно установлены в 0xAAAA/0x00FF")
            
            # Финальная проверка всех критических регистров
            if self.verbose:
                reg32_check = read_intan_register(self.spi, 32, verbose=False)
                reg33_check = read_intan_register(self.spi, 33, verbose=False)
                reg34_check = read_intan_register(self.spi, 34, verbose=False)
                reg35_check = read_intan_register(self.spi, 35, verbose=False)
                self._log(f"        Финальная проверка: Reg32=0x{reg32_check:04X}, Reg33=0x{reg33_check:04X}, Reg34=0x{reg34_check:04X}, Reg35=0x{reg35_check:04X}")
                if reg32_check == 0xAAAA and reg33_check == 0x00FF:
                    self._log("        ✓ Чип готов к стимуляции")
                else:
                    self._log("        ⚠ ВНИМАНИЕ: Регистры 32-33 не в состоянии для стимуляции!")
        else:
            # Если не было стимуляции, просто очищаем compliance monitor
            if self.verbose:
                self._log("        Очистка compliance monitor...")
            clear_compliance_monitor(self.spi, verbose=False)
            time.sleep(0.001)
        
        # 9. Обработка данных
        # Конвертируем ADC значения в знаковые (0-65535 -> -32768 to 32767)
        # Если значение >= 32768, это отрицательное число в дополнении до двух
        adc_signed_list = [(x - 65536) if x >= 32768 else x for x in adc_samples]
        
        # КРИТИЧНО: Вычитаем DC-смещение (среднее) перед расчётом RMS.
        # Без этого любое смещение (электрод, помеха 50/60 Hz) искажает результат
        # и может давать фиксированное ~2.4 МОм при измерении паразитной ёмкости.
        if NUMPY_AVAILABLE:
            adc_signed = np.array(adc_signed_list)
            dc_offset = np.mean(adc_signed)
            adc_ac = adc_signed - dc_offset
            adc_rms = np.sqrt(np.mean(adc_ac ** 2))
        else:
            dc_offset = sum(adc_signed_list) / len(adc_signed_list) if adc_signed_list else 0
            adc_ac = [x - dc_offset for x in adc_signed_list]
            adc_squared = [x * x for x in adc_ac]
            adc_mean_squared = sum(adc_squared) / len(adc_squared) if len(adc_squared) > 0 else 0
            adc_rms = math.sqrt(adc_mean_squared)
        
        # Конвертируем ADC RMS в напряжение (µV)
        # Согласно даташиту: Velec(AC) = 0.195 µV × (ADC result – 32768)
        # Для RMS: V_rms = 0.195 µV × ADC_rms
        voltage_rms_uv = 0.195 * adc_rms
        
        # Вычисляем пиковое напряжение (RMS × √2 для синусоидального сигнала)
        voltage_peak_uv = voltage_rms_uv * math.sqrt(2)
        
        # Конвертируем в вольты
        voltage_peak_v = voltage_peak_uv * 1e-6  # µV -> V
        
        # Используем фактический ток (из DAC amplitude и CS)
        current_peak_a = actual_current_nA * 1e-9  # nA -> A
        
        # Вычисляем импеданс: Z = V_peak / I_peak
        if current_peak_a > 0 and adc_rms > 5:
            impedance_ohm = voltage_peak_v / current_peak_a
            if impedance_ohm > 100e6:
                impedance_ohm = 100e6
        else:
            impedance_ohm = 0.0
        
        # КРИТИЧНО: Учитываем паразитную ёмкость согласно даташиту
        # Паразитная ёмкость 10 pF имеет импеданс ≈16 MΩ на 1 kHz
        # Это влияет на измерения для электродов с высоким импедансом (>1 MΩ)
        # Для более точных измерений можно вычесть эту ёмкость, но это опционально
        # parasitic_capacitance_impedance = 1 / (2 * math.pi * frequency * 10e-12)  # 10 pF
        # if impedance_ohm > parasitic_capacitance_impedance * 0.1:  # Если импеданс > 10% от паразитного
        #     # Корректируем импеданс (опционально, может быть неточным)
        #     pass
        
        dc_offset_val = float(dc_offset)
        if self.verbose:
            self._log(f"      Результаты измерения:")
            self._log(f"        DC offset (ADC): {dc_offset_val:.1f}")
            self._log(f"        ADC RMS (AC): {adc_rms:.2f}")
            self._log(f"        U_RMS: {voltage_rms_uv:.2f} µV")
            self._log(f"        U_peak: {voltage_peak_uv:.2f} µV")
            self._log(f"        I_peak: {actual_current_nA:.2f} nA")
            self._log(f"        Импеданс: {impedance_ohm:.2f} Ω ({impedance_ohm/1e6:.2f} MΩ)")
            if adc_rms < 20:
                self._log(f"        ⚠ Слабый сигнал (ADC RMS < 20) — проверьте подключение электрода")
        
        return {
            "impedance_ohm": float(impedance_ohm),
            "adc_rms": float(adc_rms),
            "dc_offset": float(dc_offset_val),
            "voltage_rms": float(voltage_rms_uv),  # в µV
            "voltage_peak": float(voltage_peak_uv),  # в µV
            "test_current_nA": float(actual_current_nA),
            "frequency": float(frequency),
            "channel": int(channel)
        }

    def record_channels(self, channels, sample_rate, duration=None):
        """
        Регистрирует данные с указанных каналов.
        1 транзакция на CONVERT — сырой поток. Смещение pipeline обрабатывается в GUI.
        
        Yields:
            (timestamp, raw_values, channels, pipeline_skip)
            raw_values — сырые uint16 как приходят из SPI (первые pipeline_skip — мусор)
            pipeline_skip — сколько значений пропустить в начале (Intan pipeline = 2 циклов)
        """
        self.ensure_initialized()
        
        if sample_rate <= 0:
            raise ValueError("Частота дискретизации должна быть > 0")
        
        start_time = time.time()
        conv_63 = [0x00, 0x3F, 0x00, 0x00]
        conv_ch = lambda ch: [0x00, ch & 0x3F, 0x00, 0x00]
        
        sequential = (len(channels) > 1 and channels == list(range(channels[0], channels[0] + len(channels))))
        nch = len(channels)
        first_ch = channels[0]
        # Pipeline Intan: ответ N = результат команды N-2. Первые 4 ответа — мусор.
        PIPELINE_SKIP = 4
        
        self._log(f"Начало регистрации: каналы {channels} (1 tx/convert, макс. скорость без sleep)")
        
        try:
            while True:
                if duration and (time.time() - start_time) >= duration:
                    break
                self.shared_state.wait_until_recording_allowed(timeout_s=0.05)
                if not self.shared_state.snapshot_recording_session().get("recording_active", False):
                    break

                sample_start = time.time()
                raw_values = []

                with self.shared_state.chip_lock:
                    if getattr(self.spi, "using_driver", False):
                        if sequential:
                            raw_values.append(self.convert_channel(first_ch))
                            for _ in range(nch - 1):
                                raw_values.append(self.convert_channel_auto())
                        else:
                            for ch in channels:
                                raw_values.append(self.convert_channel(ch))
                        yield (sample_start, raw_values, channels, 0)
                        continue

                    if sequential:
                        # 2 dummy + CONVERT(0) + 17×CONVERT(63) = 20 tx; ответы 1–4 мусор, 5–20 = ch0..ch15
                        for _ in range(2):
                            raw_values.append(self._convert_1tx(conv_63))
                        raw_values.append(self._convert_1tx(conv_ch(first_ch)))
                        for _ in range(17):
                            raw_values.append(self._convert_1tx(conv_63))
                    else:
                        for _ in range(2):
                            raw_values.append(self._convert_1tx(conv_63))
                        for ch in channels:
                            raw_values.append(self._convert_1tx(conv_ch(ch)))
                        for _ in range(nch + 2):
                            raw_values.append(self._convert_1tx(conv_63))
                
                yield (sample_start, raw_values, channels, PIPELINE_SKIP)
        except KeyboardInterrupt:
            self._log("Регистрация прервана пользователем")
        except Exception as e:
            self._log(f"Ошибка в record_channels: {e}")
            raise

    def close(self):
        """
        Безопасно освобождает ресурсы recorder.
        Метод нужен для корректной остановки из intan_server.py.
        """
        self.recording = False
        self.initialized = False

        # Освобождаем SPI, если он был открыт recorder'ом.
        try:
            if self.spi is not None:
                self.spi.close()
        except Exception:
            pass
        finally:
            self.spi = None

        # GPIO-питание не выключаем принудительно, чтобы не ломать общий сценарий
        # совместной работы TCP/UDP серверов.


class UDPRecorderServer:
    """UDP сервер для отправки данных регистрации клиентам"""

    def __init__(self, recorder, udp_port=9001, verbose=False, shared_state=None):
        self.recorder = recorder
        self.udp_port = udp_port
        self.verbose = verbose
        self.shared_state = shared_state or recorder.shared_state
        self.clients = set()  # Множество адресов клиентов (ip, port)
        self.lock = threading.Lock()
        self.running = False
        self.recording_thread = None
        self.recording_active = False  # Флаг активности регистрации
        self.current_effective_sample_rate_hz = None
        
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)  # 1 МБ буфер отправки
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)  # Low delay
        self.sock.bind(('0.0.0.0', self.udp_port))
        self.sock.settimeout(1.0)  # Таймаут для возможности проверки running
        self.shared_state.add_event_listener(self._handle_shared_event)

    def _log(self, msg):
        if self.verbose:
            print(msg)

    def _broadcast_text_message(self, message):
        encoded = message.encode("utf-8")
        with self.lock:
            clients_copy = list(self.clients)
        for client_addr in clients_copy:
            try:
                self.sock.sendto(encoded, client_addr)
            except (socket.error, OSError):
                continue

    def _handle_shared_event(self, payload):
        event = str(payload.get("event", "")).strip()
        if not event:
            return
        operation = str(payload.get("operation", "")).strip()
        suffix = f" operation={operation}" if operation else ""
        self._broadcast_text_message(f"STATUS {event}{suffix}")

    def start_listening(self):
        """Запускает поток для приема регистраций клиентов"""
        self.running = True
        listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        listener_thread.start()
        self._log(f"UDP сервер слушает на порту {self.udp_port}")

    def _listen_loop(self):
        """Цикл приема регистраций от клиентов"""
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1024)
                msg = data.decode('utf-8').strip()
                
                if msg == "REGISTER":
                    with self.lock:
                        self.clients.add(addr)
                    self.sock.sendto(b"REGISTERED", addr)
                    self._log(f"✓ Клиент зарегистрирован: {addr} (всего клиентов: {len(self.clients)})")
                elif msg == "UNREGISTER":
                    with self.lock:
                        self.clients.discard(addr)
                    self.sock.sendto(b"UNREGISTERED", addr)
                    self._log(f"Клиент отрегистрирован: {addr}")
                elif msg.startswith("START_RECORDING"):
                    # Формат: START_RECORDING channels sample_rate [duration]
                    parts = msg.split()
                    channels_str = parts[1] if len(parts) > 1 else "0-15"
                    sample_rate = int(parts[2]) if len(parts) > 2 else 40000
                    duration = float(parts[3]) if len(parts) > 3 else None
                    
                    self.start_recording(channels_str, sample_rate, duration)
                    self.sock.sendto(b"RECORDING_STARTED", addr)
                elif msg == "STOP_RECORDING":
                    self.stop_recording()
                    self.sock.sendto(b"RECORDING_STOPPED", addr)
            except socket.timeout:
                continue
            except Exception as e:
                self._log(f"Ошибка в listen_loop: {e}")

    def _use_driver_streaming(self):
        return bool(
            self.recorder
            and self.recorder.using_driver
            and self.recorder.spi is not None
            and hasattr(self.recorder.spi, "supports_streaming")
            and self.recorder.spi.supports_streaming()
        )

    def start_recording(self, channels_str="0-15", sample_rate=40000, duration=None):
        """Запускает регистрацию и отправку данных"""
        if self.recording_thread and self.recording_thread.is_alive():
            self._log("Регистрация уже запущена")
            return
        
        # Останавливаем предыдущую регистрацию, если есть
        self.stop_recording()
        
        # Проверяем, что recorder существует
        if not self.recorder:
            raise RuntimeError("recorder не инициализирован в UDPRecorderServer")
        
        # Парсим и валидируем каналы RHS2116.
        channels = []
        for part in channels_str.split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                start, end = map(int, part.split('-'))
                channels.extend(range(start, end + 1))
            else:
                channels.append(int(part))
        channels = rhs2116_validate_channels(channels)
        if not channels:
            raise ValueError("Нужно указать хотя бы один канал в диапазоне 0..15")
        
        # Intan RHS2116 поддерживает до 40 kSamples/s на канал (714 kSamples/s общая)
        max_rate_per_channel = 40000
        if sample_rate > max_rate_per_channel:
            self._log(f"⚠ Предупреждение: частота {sample_rate} Hz слишком высока, ограничиваем до {max_rate_per_channel} Hz на канал")
            sample_rate = max_rate_per_channel
        
        if len(channels) > 16:
            channels = channels[:16]
            self._log(f"⚠ Предупреждение: ограничиваем количество каналов до 16")
        
        # Вычисляем общую частоту ADC (частота на канал * количество каналов)
        # Это нужно для правильной настройки Register 0 (ADC buffer bias и MUX bias)
        num_channels = len(channels)
        total_adc_rate_ksps = (sample_rate * num_channels) / 1000.0  # в kS/s
        
        with self.shared_state.chip_lock:
            if self.recorder.initialized:
                self._log(f"Настройка ADC для частоты {total_adc_rate_ksps:.1f} kS/s (каналов: {num_channels}, частота на канал: {sample_rate} Hz)")
                self.recorder._initialize_for_recording(verbose=self.recorder.verbose, adc_sampling_rate_ksps=total_adc_rate_ksps)
            else:
                self.recorder.ensure_initialized()
                self.recorder._initialize_for_recording(verbose=self.recorder.verbose, adc_sampling_rate_ksps=total_adc_rate_ksps)
            if self._use_driver_streaming():
                self.recorder.spi.configure_stream(channels, sample_rate)
        
        self.current_effective_sample_rate_hz = sample_rate
        self.recording_active = True
        self.shared_state.set_recording_session(channels, sample_rate, duration, total_adc_rate_ksps)
        self.recording_thread = threading.Thread(
            target=self._recording_loop,
            args=(channels, sample_rate, duration),
            daemon=True
        )
        self.recording_thread.start()
        self._log(f"Регистрация запущена: каналы {channels}, частота {sample_rate} Hz на канал (общая ADC: {total_adc_rate_ksps:.1f} kS/s)")

    def _stream_recording_loop(self, channels, sample_rate, duration):
        """Быстрый путь: драйвер формирует packetized stream, userspace только шлет UDP."""
        start_time = time.time()
        completed_naturally = False
        packets_sent = 0
        clients_copy = []

        try:
            self._log(
                f"Запуск driver streaming loop: каналы={channels}, "
                f"частота={sample_rate} Hz, длительность={duration}"
            )
            self.recorder.spi.start_stream()

            while True:
                if duration and (time.time() - start_time) >= duration:
                    completed_naturally = True
                    break
                if not self.recording_active:
                    break

                packet = self.recorder.spi.read_stream_packet(timeout_ms=100)
                if packet is None:
                    continue

                if len(clients_copy) == 0:
                    with self.lock:
                        clients_copy = list(self.clients)
                elif len(self.clients) != len(clients_copy):
                    with self.lock:
                        clients_copy = list(self.clients)

                if not clients_copy:
                    continue

                failed_clients = []
                for client_addr in clients_copy:
                    try:
                        self.sock.sendto(packet["data"], client_addr)
                        packets_sent += 1
                    except (socket.error, OSError) as e:
                        failed_clients.append(client_addr)
                        if self.verbose:
                            self._log(f"⚠ Ошибка отправки stream packet клиенту {client_addr}: {e}")

                if failed_clients:
                    with self.lock:
                        for client_addr in failed_clients:
                            self.clients.discard(client_addr)
                        clients_copy = list(self.clients)

            if completed_naturally:
                if not clients_copy:
                    with self.lock:
                        clients_copy = list(self.clients)
                for client_addr in clients_copy:
                    try:
                        self.sock.sendto(b"RECORDING_STOPPED", client_addr)
                    except (socket.error, OSError):
                        pass

            self._log(f"✓ Driver streaming loop завершен. UDP packets sent: {packets_sent}")
        finally:
            try:
                self.recorder.spi.stop_stream()
            except Exception:
                pass

    def _recording_loop(self, channels, sample_rate, duration):
        """Цикл регистрации и отправки данных"""
        if self._use_driver_streaming():
            try:
                return self._stream_recording_loop(channels, sample_rate, duration)
            finally:
                self.recording_active = False
                self.shared_state.clear_recording_session()
                self._log("Driver streaming цикл завершен")

        try:
            self._log(f"Запуск _recording_loop: каналы={channels}, частота={sample_rate} Hz, длительность={duration}")
            
            # Проверяем, что recorder инициализирован
            if not self.recorder:
                raise RuntimeError("recorder не инициализирован")
            
            clients_copy = []
            
            # Формат v2: [ver=2][sample_count] per sample: timestamp(8)+pipeline_skip(2)+ch_count(2)+ch_list(ch_count)+raw_count(2)+raw
            MAX_PACKET_SIZE = 1400
            channel_count = len(channels)
            raw_per_sample = 4 + channel_count + 2
            sample_size = 8 + 2 + 2 + channel_count + 2 + raw_per_sample * 2
            max_samples_per_packet = (MAX_PACKET_SIZE - 5) // sample_size
            max_samples_per_packet = max(1, min(max_samples_per_packet, 100))
            samples_buffer = []
            samples_buffer_append = samples_buffer.append
            
            print(f"[SERVER] 📖 Начало чтения данных из record_channels...")
            self._log(f"Начало чтения данных из record_channels...")
            sample_iter = iter(self.recorder.record_channels(channels, sample_rate, duration))
            sample_count = 0
            completed_naturally = False
            
            while True:
                try:
                    timestamp, raw_values, ch_list, pipeline_skip = next(sample_iter)
                    sample_count += 1
                    
                    if sample_count == 1:
                        self._log(f"✓ Первый sample: timestamp={timestamp:.6f}, raw={len(raw_values)}, pipeline_skip={pipeline_skip}")
                    
                    if not self.recording_active:
                        self._log("Регистрация остановлена по запросу")
                        break
                    
                    if len(clients_copy) == 0:
                        with self.lock:
                            clients_copy = list(self.clients)
                    elif len(self.clients) != len(clients_copy):
                        with self.lock:
                            clients_copy = list(self.clients)
                    
                    if not clients_copy:
                        if sample_count <= 10:
                            self._log(f"⚠ Нет зарегистрированных клиентов (sample #{sample_count})")
                        continue
                    
                    samples_buffer_append((timestamp, raw_values, ch_list, pipeline_skip))
                    
                    if len(samples_buffer) >= max_samples_per_packet:
                        self._send_packet(samples_buffer, clients_copy)
                        samples_buffer = []
                        samples_buffer_append = samples_buffer.append
                        
                except StopIteration:
                    # Итератор завершился (нормальное завершение)
                    if self.recording_active:
                        completed_naturally = True
                    self._log(f"Итератор record_channels завершился. Всего samples: {sample_count}")
                    break
                except Exception as e:
                    import traceback
                    self._log(f"❌ Ошибка при чтении sample #{sample_count + 1}: {e}")
                    self._log(f"Детали: {traceback.format_exc()}")
                    raise
                
            # Отправляем оставшиеся samples
            if samples_buffer:
                self._send_packet(samples_buffer, clients_copy)

            if completed_naturally:
                if not clients_copy:
                    with self.lock:
                        clients_copy = list(self.clients)
                for client_addr in clients_copy:
                    try:
                        self.sock.sendto(b"RECORDING_STOPPED", client_addr)
                    except (socket.error, OSError):
                        pass
                
            self._log(f"✓ Регистрация завершена успешно. Всего samples: {sample_count}")
                
        except Exception as e:
            import traceback
            self._log(f"❌ Критическая ошибка в recording_loop: {e}")
            self._log(f"Детали: {traceback.format_exc()}")
        finally:
            self.recording_active = False
            self.shared_state.clear_recording_session()
            self._log("Цикл регистрации завершен")
    
    def _send_packet(self, samples_buffer, clients_copy):
        """Отправляет пакет: формат с pipeline (1 tx/convert), смещение обрабатывается в GUI"""
        if not samples_buffer or not clients_copy:
            return
        
        sample_count = len(samples_buffer)
        packet = bytearray(5)
        packet[0] = 2  # версия формата: 2 = pipeline (1 tx/convert), смещение в GUI
        struct.pack_into('<I', packet, 1, sample_count)
        
        for timestamp, raw_values, ch_list, pipeline_skip in samples_buffer:
            raw_count = len(raw_values)
            ch_count = len(ch_list)
            sample_block_size = 8 + 2 + 2 + ch_count + 2 + raw_count * 2
            sample_block = bytearray(sample_block_size)
            struct.pack_into('<dHH', sample_block, 0, timestamp, pipeline_skip, ch_count)
            for i, ch in enumerate(ch_list):
                sample_block[12 + i] = ch & 0xFF
            struct.pack_into('<H', sample_block, 12 + ch_count, raw_count)
            for i, v in enumerate(raw_values):
                struct.pack_into('<H', sample_block, 12 + ch_count + 2 + i * 2, v & 0xFFFF)
            packet += sample_block
        
        failed_clients = []
        packets_sent = 0
        for client_addr in clients_copy:
            try:
                self.sock.sendto(packet, client_addr)
                packets_sent += 1
            except (socket.error, OSError) as e:
                failed_clients.append(client_addr)
                if self.verbose:
                    self._log(f"⚠ Ошибка отправки пакета клиенту {client_addr}: {e}")
        
        if failed_clients:
            with self.lock:
                for client_addr in failed_clients:
                    self.clients.discard(client_addr)
                if failed_clients:
                    clients_copy[:] = list(self.clients)

    def stop_recording(self):
        """Останавливает регистрацию"""
        if self.recording_active:
            self._log("Остановка регистрации...")
            self.recording_active = False
            self.shared_state.clear_recording_session()
            try:
                if self._use_driver_streaming():
                    self.recorder.spi.stop_stream()
            except Exception:
                pass
            # Ждем завершения потока (максимум 2 секунды)
            if self.recording_thread and self.recording_thread.is_alive():
                self.recording_thread.join(timeout=2.0)
                if self.recording_thread.is_alive():
                    self._log("⚠ Поток регистрации не завершился в течение 2 секунд")

    def get_clients_count(self):
        """Возвращает количество зарегистрированных клиентов"""
        with self.lock:
            return len(self.clients)

    def stop(self):
        """Останавливает сервер"""
        self.running = False
        self.sock.close()


def main():
    parser = argparse.ArgumentParser(
        description="UDP-сервер для регистрации данных Intan RHS2116"
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=9001,
        help="UDP порт для отправки данных (по умолчанию: 9001)"
    )
    parser.add_argument(
        "-g", "--gpio",
        type=int,
        default=226,
        help="Номер GPIO для PH2 (по умолчанию: 226)"
    )
    parser.add_argument(
        "-d", "--device",
        default="/dev/spidev1.1",
        help="Путь к SPI устройству (по умолчанию: /dev/spidev1.1)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Подробный вывод"
    )

    args = parser.parse_args()

    recorder = IntanRecorder(
        gpio_number=args.gpio,
        spi_device=args.device,
        verbose=args.verbose
    )

    server = UDPRecorderServer(recorder, udp_port=args.udp_port, verbose=args.verbose)
    server.start_listening()

    try:
        print("=" * 60)
        print("UDP-сервер регистрации Intan RHS2116 запущен")
        print("=" * 60)
        print(f"UDP порт: {args.udp_port}")
        print(f"GPIO PH2: {args.gpio}")
        print(f"SPI устройство: {args.device}")
        print("\nКоманды клиентов:")
        print("  REGISTER - зарегистрироваться для получения данных")
        print("  UNREGISTER - отменить регистрацию")
        print("  START_RECORDING <channels> <sample_rate> [duration] - начать регистрацию")
        print("  STOP_RECORDING - остановить регистрацию")
        print("=" * 60)
        print("\nНажмите Ctrl+C для остановки\n")

        while True:
            time.sleep(1)
            clients_count = server.get_clients_count()
            if clients_count > 0:
                print(f"\rАктивных клиентов: {clients_count}", end='', flush=True)
    except KeyboardInterrupt:
        print("\n\nОстановка сервера...")
    finally:
        server.stop()
        print("Сервер остановлен")


if __name__ == "__main__":
    main()

