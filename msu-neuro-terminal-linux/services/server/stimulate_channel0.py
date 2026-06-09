#!/usr/bin/env python3
"""
Скрипт для стимуляции первого канала (канал 0) Intan RHS2116
Управляет питанием через GPIO PH2
Настраивает параметры стимуляции и запускает стимуляцию
"""

import os
import sys
import time
import argparse

# Импорт spidev для работы с SPI
try:
    import spidev
except ImportError:
    print("Ошибка: Модуль spidev не установлен.")
    print("Установите его командой: sudo apt-get install python3-spidev")
    sys.exit(1)


class GPIOError(Exception):
    """Исключение для ошибок GPIO"""
    pass


class GPIOController:
    """Класс для управления GPIO через sysfs"""
    
    def __init__(self, gpio_number, raise_exceptions=False):
        """
        Args:
            gpio_number: номер GPIO
            raise_exceptions: если True, выбрасывает исключения вместо sys.exit
        """
        self.gpio_number = gpio_number
        self.gpio_path = f"/sys/class/gpio/gpio{gpio_number}"
        self.exported = False
        self.raise_exceptions = raise_exceptions

    def _permission_error_message(self, action):
        setup_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_permissions.sh")
        return (
            f"Нет прав для операции '{action}' с GPIO {self.gpio_number}. "
            f"Один раз настройте права: sudo bash {setup_script} "
            f"(или sudo {setup_script}, если файл исполняемый), затем перелогиньтесь."
        )
    
    def export(self):
        """Экспортирует GPIO"""
        if not os.path.exists(self.gpio_path):
            try:
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(str(self.gpio_number))
                time.sleep(0.1)
                self.exported = True
            except PermissionError:
                error_msg = self._permission_error_message("export")
                if self.raise_exceptions:
                    raise GPIOError(error_msg)
                print(f"Ошибка: {error_msg}")
                sys.exit(1)
            except OSError as e:
                # Может случиться гонка, когда GPIO уже экспортирован другим процессом.
                if os.path.exists(self.gpio_path):
                    self.exported = True
                else:
                    error_msg = f"Ошибка при экспорте GPIO: {e}"
                    if self.raise_exceptions:
                        raise GPIOError(error_msg)
                    print(f"Ошибка: {error_msg}")
                    sys.exit(1)
            except Exception as e:
                error_msg = f"Ошибка при экспорте GPIO: {e}"
                if self.raise_exceptions:
                    raise GPIOError(error_msg)
                print(f"Ошибка: {error_msg}")
                sys.exit(1)
        else:
            self.exported = True
    
    def set_direction(self, direction):
        """Устанавливает направление: 'in' или 'out'"""
        if not self.exported:
            self.export()
        try:
            with open(f"{self.gpio_path}/direction", "w") as f:
                f.write(direction)
        except PermissionError as e:
            error_msg = self._permission_error_message("set_direction")
            if self.raise_exceptions:
                raise GPIOError(error_msg)
            print(f"Ошибка: {error_msg}")
            sys.exit(1)
        except Exception as e:
            error_msg = f"Ошибка при установке направления GPIO: {e}"
            if self.raise_exceptions:
                raise GPIOError(error_msg)
            print(f"Ошибка: {error_msg}")
            sys.exit(1)
    
    def set_value(self, value):
        """Устанавливает значение: 0 или 1"""
        if not self.exported:
            self.export()
        try:
            with open(f"{self.gpio_path}/value", "w") as f:
                f.write(str(value))
        except PermissionError as e:
            error_msg = self._permission_error_message("set_value")
            if self.raise_exceptions:
                raise GPIOError(error_msg)
            print(f"Ошибка: {error_msg}")
            sys.exit(1)
        except Exception as e:
            error_msg = f"Ошибка при установке значения GPIO: {e}"
            if self.raise_exceptions:
                raise GPIOError(error_msg)
            print(f"Ошибка: {error_msg}")
            sys.exit(1)
    
    def get_value(self):
        """Читает значение GPIO"""
        if not self.exported:
            self.export()
        try:
            with open(f"{self.gpio_path}/value", "r") as f:
                return int(f.read().strip())
        except Exception as e:
            error_msg = f"Ошибка при чтении значения GPIO: {e}"
            if self.raise_exceptions:
                raise GPIOError(error_msg)
            print(f"Ошибка: {error_msg}")
            sys.exit(1)


class SPIController:
    """Класс для работы с SPI через spidev"""
    
    def __init__(self, device="/dev/spidev1.1", max_speed_hz=10000000, mode=0):
        self.device = device
        self.max_speed_hz = max_speed_hz
        self.mode = mode
        self.spi = spidev.SpiDev()
    
    def open(self):
        """Открывает SPI устройство"""
        bus, device_num = self.device.split("spidev")[1].split(".")
        self.spi.open(int(bus), int(device_num))
        self.spi.max_speed_hz = self.max_speed_hz
        self.spi.mode = self.mode
        self.spi.bits_per_word = 8
    
    def transfer(self, data):
        """Отправляет данные и получает ответ"""
        return self.spi.xfer2(data)
    
    def close(self):
        """Закрывает SPI устройство"""
        if self.spi:
            self.spi.close()


def _is_usb_backend(hw):
    """True if hw is IntanUsbTransport (or compatible)."""
    return hasattr(hw, "read_register")


def convert_intan(spi, channel, amp_type="ac", h_flag=0, verbose=False):
    """CONVERT для SPI (pipeline N+2) или USB (одна команда CONVERT)."""
    if channel < 0 or channel > 63:
        raise ValueError(f"Номер канала должен быть 0-63, получено: {channel}")

    d_flag = 1 if amp_type == "dc" else 0
    if _is_usb_backend(spi):
        flags = (h_flag & 1) | ((d_flag & 1) << 1)
        val = spi.convert(channel, flags)
        if verbose:
            print(f"  CONVERT ch={channel} flags=0x{flags:X} = 0x{val:04X}")
        return val

    cmd_word = 0x00000000 | (channel << 16)
    if d_flag:
        cmd_word |= 1 << 27
    if h_flag:
        cmd_word |= 1 << 26
    cmd = [
        (cmd_word >> 24) & 0xFF,
        (cmd_word >> 16) & 0xFF,
        (cmd_word >> 8) & 0xFF,
        cmd_word & 0xFF,
    ]
    dummy = [0x00, 0x00, 0x00, 0x00]
    spi.transfer(cmd)
    spi.transfer(dummy)
    resp3 = spi.transfer(dummy)
    return (resp3[0] << 8) | resp3[1]


def read_intan_register(spi, reg_addr, verbose=False):
    """
    Читает регистр из Intan RHS2116
    
    Формат команды READ(R) - 32-битное слово (MSB первым):
    - Биты [31:30] = 11 (команда READ)
    - Бит [29] = U (Update) = 0
    - Бит [28] = M (Multiple) = 0  
    - Биты [27:24] = 0000
    - Биты [23:16] = адрес регистра R[7:0]
    - Биты [15:0] = 0x0000
    """
    if _is_usb_backend(spi):
        val = spi.read_register(reg_addr)
        if verbose:
            print(f"  READ reg {reg_addr} = 0x{val:04X}")
        return val

    cmd = [0xC0, reg_addr & 0xFF, 0x00, 0x00]
    
    resp1 = spi.transfer(cmd)
    resp2 = spi.transfer([0x00, 0x00, 0x00, 0x00])
    resp3 = spi.transfer([0x00, 0x00, 0x00, 0x00])
    
    reg_value = (resp3[2] << 8) | resp3[3]
    
    return reg_value


def write_intan_register(spi, reg_addr, value, u_flag=1, m_flag=0, verbose=False):
    """
    Записывает значение в регистр Intan RHS2116
    
    Формат команды WRITE(W) - 32-битное слово (MSB первым):
    - Биты [31:30] = 10 (команда WRITE)
    - Бит [29] = U (Update) - обновить triggered registers
    - Бит [28] = M (Multiple) - очистить compliance monitor
    - Биты [27:24] = 0000
    - Биты [23:16] = адрес регистра R[7:0]
    - Биты [15:0] = данные для записи D[15:0]
    
    Args:
        spi: Объект SPIController
        reg_addr: Адрес регистра (0-255)
        value: 16-битное значение для записи
        u_flag: U flag (1 для обновления triggered registers, 0 иначе)
        m_flag: M flag (1 для очистки compliance monitor, 0 иначе)
        verbose: Выводить отладочную информацию
    """
    if _is_usb_backend(spi):
        spi.write_register(reg_addr, value, u_flag, m_flag)
        if verbose:
            print(f"  WRITE reg {reg_addr} = 0x{value:04X} (u={u_flag}, m={m_flag})")
        return

    # Формируем команду WRITE: 32-битное слово
    # Байт 0 (MSB): 0b10000000 = 0x80 (10 + U=0 + M=0 + 0000)
    # Если U=1: 0b10100000 = 0xA0
    # Если M=1: 0b10010000 = 0x90
    # Если U=1 и M=1: 0b10110000 = 0xB0
    byte0 = 0x80 | (u_flag << 5) | (m_flag << 4)
    cmd = [byte0, reg_addr & 0xFF, (value >> 8) & 0xFF, value & 0xFF]
    
    if verbose:
        flags_str = []
    spi.transfer(cmd)


def clear_adc(spi, verbose=False):
    """
    Выполняет команду CLEAR для инициализации ADC
    
    Формат команды CLEAR: 0x6A000000
    - Биты [31:24] = 0x6A (01101010)
    - Биты [23:0] = 0x000000
    
    Args:
        spi: Объект SPIController
        verbose: Выводить отладочную информацию
    """
    if _is_usb_backend(spi):
        spi.clear_adc()
        if verbose:
            print("  CLEAR_ADC")
        return

    # Команда CLEAR: 0x6A000000 
    cmd = [0x6A, 0x00, 0x00, 0x00]
    spi.transfer(cmd)


def initialize_intan_chip(spi, verbose=False):
    """
    Инициализирует чип Intan RHS2116 согласно документации
    
    Выполняет последовательность команд инициализации перед использованием стимуляции.
    
    Args:
        spi: Объект SPIController
        verbose: Выводить отладочную информацию
    """
    if verbose:
        print("\nИнициализация Intan RHS2116...")
    
    # READ 255 U=0 M=0 - dummy команда после включения питания
    if verbose:
        print("  READ 255 (dummy команда)...")
    read_intan_register(spi, 255, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 32 0x0000 U=0 M=0 - отключить стимуляцию
    if verbose:
        print("  WRITE 32 0x0000 - отключить стимуляцию...")
    write_intan_register(spi, 32, 0x0000, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 33 0x0000 U=0 M=0 - отключить стимуляцию
    if verbose:
        print("  WRITE 33 0x0000 - отключить стимуляцию...")
    write_intan_register(spi, 33, 0x0000, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 38 0xFFFF U=0 M=0 - включить все DC-coupled low-gain amplifiers
    if verbose:
        print("  WRITE 38 0xFFFF - включить DC-coupled amplifiers...")
    write_intan_register(spi, 38, 0xFFFF, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # CLEAR - инициализация ADC
    clear_adc(spi, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 0 0x00C5 U=0 M=0 - настройка ADC и MUX для 480 kS/s
    if verbose:
        print("  WRITE 0 0x00C5 - настройка ADC (480 kS/s)...")
    write_intan_register(spi, 0, 0x00C5, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 1 0x051A U=0 M=0 - auxiliary outputs и DSP фильтр
    if verbose:
        print("  WRITE 1 0x051A - auxiliary outputs и DSP фильтр...")
    write_intan_register(spi, 1, 0x051A, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 2 0x0040 U=0 M=0 - включить DAC для impedance testing
    if verbose:
        print("  WRITE 2 0x0040 - включить impedance testing DAC...")
    write_intan_register(spi, 2, 0x0040, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 3 0x0080 U=0 M=0 - инициализация impedance check DAC
    if verbose:
        print("  WRITE 3 0x0080 - инициализация impedance DAC...")
    write_intan_register(spi, 3, 0x0080, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 4 0x0016 U=0 M=0 - верхняя частота среза AC-coupled amplifiers (7.5 kHz)
    if verbose:
        print("  WRITE 4 0x0016 - верхняя частота среза (7.5 kHz)...")
    write_intan_register(spi, 4, 0x0016, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 5 0x0017 U=0 M=0 - нижняя частота среза AC-coupled amplifiers (5 Hz)
    if verbose:
        print("  WRITE 5 0x0017 - нижняя частота среза (5 Hz)...")
    write_intan_register(spi, 5, 0x0017, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 6 0x00A8 U=0 M=0 - нижняя частота среза AC-coupled amplifiers (5 Hz)
    if verbose:
        print("  WRITE 6 0x00A8 - нижняя частота среза (5 Hz)...")
    write_intan_register(spi, 6, 0x00A8, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 7 0x000A U=0 M=0 - альтернативная нижняя частота среза (1000 Hz)
    if verbose:
        print("  WRITE 7 0x000A - альтернативная нижняя частота среза (1000 Hz)...")
    write_intan_register(spi, 7, 0x000A, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 8 0xFFFF U=0 M=0 - включить все AC-coupled high-gain amplifiers
    if verbose:
        print("  WRITE 8 0xFFFF - включить AC-coupled amplifiers...")
    write_intan_register(spi, 8, 0xFFFF, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 10 0x0000 U=1 M=0 - отключить fast settle (triggered register)
    if verbose:
        print("  WRITE 10 0x0000 U=1 - отключить fast settle...")
    write_intan_register(spi, 10, 0x0000, u_flag=1, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 12 0xFFFF U=1 M=0 - установить все amplifiers на нижнюю частоту среза (triggered register)
    if verbose:
        print("  WRITE 12 0xFFFF U=1 - установить нижнюю частоту среза...")
    write_intan_register(spi, 12, 0xFFFF, u_flag=1, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 34 0x00E2 U=0 M=0 - шаг стимуляции 1 µA
    if verbose:
        print("  WRITE 34 0x00E2 - шаг стимуляции 1 µA...")
    write_intan_register(spi, 34, 0x00E2, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 35 0x00AA U=0 M=0 - напряжения смещения для шага 1 µA
    if verbose:
        print("  WRITE 35 0x00AA - напряжения смещения...")
    write_intan_register(spi, 35, 0x00AA, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 36 0x0080 U=0 M=0 - целевое напряжение charge recovery (0 V)
    if verbose:
        print("  WRITE 36 0x0080 - целевое напряжение charge recovery (0 V)...")
    write_intan_register(spi, 36, 0x0080, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 37 0x4F00 U=0 M=0 - лимит тока charge recovery (1 nA)
    if verbose:
        print("  WRITE 37 0x4F00 - лимит тока charge recovery (1 nA)...")
    write_intan_register(spi, 37, 0x4F00, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 42 0x0000 U=1 M=0 - выключить все стимуляторы (triggered register)
    if verbose:
        print("  WRITE 42 0x0000 U=1 - выключить все стимуляторы...")
    write_intan_register(spi, 42, 0x0000, u_flag=1, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # WRITE 44 0x0000 U=1 M=0 - установить все стимуляторы на отрицательную полярность (triggered register)
    if verbose:
        print("  WRITE 44 0x0000 U=1 - установить отрицательную полярность...")
    write_intan_register(spi, 44, 0x0000, u_flag=1, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    # Разрешаем работу стимуляторов (регистры 32-33)
    # По аналогии с рабочим кодом: устанавливаем 0xAAAA и 0x00FF в конце инициализации
    if verbose:
        print("  WRITE 32 0xAAAA, WRITE 33 0x00FF - разрешить работу стимуляторов...")
    write_intan_register(spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    write_intan_register(spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=verbose)
    time.sleep(0.001)
    
    if verbose:
        print("  ✓ Инициализация чипа завершена")


def setup_stimulation_channels(spi, channels, neg_current_magnitude=0, pos_current_magnitude=0, 
                                step_size_1ua=True, verbose=False):
    """
    Настраивает токи стимуляции для указанных каналов
    
    Примечание: инициализация чипа (регистры 0-44) должна быть выполнена
    функцией initialize_intan_chip() перед вызовом этой функции.
    
    Args:
        spi: Объект SPIController
        channels: Список номеров каналов (0-15)
        neg_current_magnitude: Величина отрицательного тока стимуляции (0-255 для шага 1 µA)
        pos_current_magnitude: Величина положительного тока стимуляции (0-255 для шага 1 µA)
        step_size_1ua: Использовать шаг 1 µA (True) или другой шаг (False)
        verbose: Выводить отладочную информацию
    """
    if verbose:
        print(f"\nНастройка токов стимуляции для каналов {channels}:")
        print(f"  Отрицательный ток: {neg_current_magnitude} µA")
        print(f"  Положительный ток: {pos_current_magnitude} µA")
        print(f"  Шаг стимуляции: {'1 µA' if step_size_1ua else 'другой'}")
    
    # КРИТИЧНО: Проверяем, что Register 34 установлен правильно (0x00E2 для шага 1 µA)
    # Без правильного step size токи будут неправильными!
    reg34 = read_intan_register(spi, 34, verbose=False)
    if reg34 != 0x00E2:
        if verbose:
            print(f"  ⚠ ВНИМАНИЕ: Register 34 = 0x{reg34:04X} (ожидается 0x00E2 для шага 1 µA)!")
            print(f"  Устанавливаем Register 34 = 0x00E2 и Register 35 = 0x00AA...")
        write_intan_register(spi, 34, 0x00E2, u_flag=0, m_flag=0, verbose=verbose)
        time.sleep(0.001)
        write_intan_register(spi, 35, 0x00AA, u_flag=0, m_flag=0, verbose=verbose)
        time.sleep(0.001)
    
    # Устанавливаем токи для каждого канала
    # Согласно даташиту: формат регистров 64-79 и 96-111:
    # - Биты [15:8] = current trim [7:0] (0x80 = 128 = нормальное значение без подстройки, диапазон ±28%)
    # - Биты [7:0] = current magnitude [7:0] (величина тока, 0-255)
    # Формат: 0x8000 | (current_value & 0xFF) где 0x80 = trim = 128 (без подстройки)
    # ВАЖНО: Значение тока - это МНОЖИТЕЛЬ шага (Register 34), а не прямой ток!
    # При step size = 1 µA (Register 34 = 0x00E2), значение 1 = 1 µA, значение 10 = 10 µA
    for channel in channels:
        # Отрицательный ток (регистры 64-79)
        if neg_current_magnitude == 0:
            neg_value = 0x8000  # 0 µA (выключено), но с правильным форматом
        else:
            # Ограничиваем значение тока до 0-255
            current_val = min(max(int(neg_current_magnitude), 0), 255)
            # Формат: 0x8000 + значение тока
            neg_value = 0x8000 | (current_val & 0xFF)
        reg_neg = 64 + channel
        if verbose:
            print(f"  Установка отрицательного тока канала {channel} (регистр {reg_neg} = 0x{neg_value:04X}, ток = {neg_value & 0xFF} µA)...")
        # КРИТИЧНО: БЕЗ U-флага - накапливаем в shadow-RAM, применится при записи в Register 42
        write_intan_register(spi, reg_neg, neg_value, u_flag=0, verbose=verbose)
        time.sleep(0.001)  # Минимальная задержка
        
        # Положительный ток (регистры 96-111)
        if pos_current_magnitude == 0:
            pos_value = 0x8000  # 0 µA (выключено), но с правильным форматом
        else:
            # Ограничиваем значение тока до 0-255
            current_val = min(max(int(pos_current_magnitude), 0), 255)
            # Формат: 0x8000 + значение тока
            pos_value = 0x8000 | (current_val & 0xFF)
        reg_pos = 96 + channel
        if verbose:
            print(f"  Установка положительного тока канала {channel} (регистр {reg_pos} = 0x{pos_value:04X}, ток = {pos_value & 0xFF} µA)...")
        # КРИТИЧНО: БЕЗ U-флага - накапливаем в shadow-RAM, применится при записи в Register 42
        write_intan_register(spi, reg_pos, pos_value, u_flag=0, verbose=verbose)
        time.sleep(0.001)  # Минимальная задержка
    
    # Регистры 32-33 уже установлены в initialize_intan_chip (0xAAAA и 0x00FF)
    # Не нужно их устанавливать здесь
    
    if verbose:
        print("  ✓ Параметры стимуляции установлены")


def clear_compliance_monitor(spi, verbose=False):
    """
    Очищает compliance monitor (регистр 40) с помощью READ(255) с M flag = 1
    
    Args:
        spi: Объект SPIController
        verbose: Выводить отладочную информацию
    """
    # READ(255) с M flag = 1 для очистки compliance monitor
    # Формат: 0b11010000 = 0xD0 (11 + U=0 + M=1 + 0000)
    cmd = [0xD0, 255, 0x00, 0x00]
    
    if verbose:
        print("  Очистка compliance monitor (READ 255 с M flag)...")
    if _is_usb_backend(spi):
        spi.clear_comp()
        return
    # M=1 вызывает side-effect (очистку)
    spi.transfer(cmd)


def set_stimulation_current(spi, current_value, channel=0, is_positive=True, verbose=False):
    """
    Устанавливает ток стимуляции для указанного канала (быстрая функция)
    
    ВАЖНО: Значение тока - это МНОЖИТЕЛЬ шага (Register 34), а не прямой ток!
    При step size = 1 µA (Register 34 = 0x00E2), значение 1 = 1 µA, значение 10 = 10 µA
    
    Args:
        spi: Объект SPIController
        current_value: Значение тока в µA (0-255) - при условии, что Register 34 = 0x00E2 (шаг 1 µA)
        channel: Номер канала (0-15, по умолчанию: 0)
        is_positive: True для положительного тока, False для отрицательного
        verbose: Выводить отладочную информацию
    """
    # КРИТИЧНО: Проверяем, что Register 34 установлен правильно (0x00E2 для шага 1 µA)
    reg34 = read_intan_register(spi, 34, verbose=False)
    if reg34 != 0x00E2:
        if verbose:
            print(f"  ⚠ ВНИМАНИЕ: Register 34 = 0x{reg34:04X} (ожидается 0x00E2 для шага 1 µA)!")
            print(f"  Устанавливаем Register 34 = 0x00E2 и Register 35 = 0x00AA...")
        write_intan_register(spi, 34, 0x00E2, u_flag=0, m_flag=0, verbose=verbose)
        time.sleep(0.001)
        write_intan_register(spi, 35, 0x00AA, u_flag=0, m_flag=0, verbose=verbose)
        time.sleep(0.001)
    
    # Регистры: 64-79 для отрицательного тока (каналы 0-15), 96-111 для положительного (каналы 0-15)
    # Согласно даташиту: формат регистров:
    # - Биты [15:8] = current trim [7:0] (0x80 = 128 = нормальное значение без подстройки, диапазон ±28%)
    # - Биты [7:0] = current magnitude [7:0] (величина тока, 0-255)
    base_reg = 96 if is_positive else 64
    reg_addr = base_reg + channel
    
    # КРИТИЧНО: БЕЗ U-флага - накапливаем в shadow-RAM
    # U-флаг будет применен при следующей записи в Register 42 через enable_stimulation_channels
    
    # Ограничиваем значение тока до 0-255
    current_val = min(max(int(current_value), 0), 255)
    # Формат: 0x8000 | (current_val & 0xFF) где 0x80 = trim (128), биты [7:0] = ток
    value = 0x8000 | (current_val & 0xFF)
    
    if verbose:
        print(f"  Установка тока: канал {channel}, {'положительный' if is_positive else 'отрицательный'}, регистр {reg_addr} = 0x{value:04X} (ток = {current_val} µA при шаге 1 µA)")
    
    # КРИТИЧНО: БЕЗ U-флага - накапливаем в shadow-RAM
    # U-флаг будет применен при следующей записи в Register 42 через enable_stimulation_channels
    write_intan_register(spi, reg_addr, value, u_flag=0, verbose=verbose)
    time.sleep(0.001)  # Минимальная задержка


def enable_stimulation_channels(spi, channels, enable=True, negative_polarity=True, verbose=False):
    """
    Включает или выключает стимуляцию для указанных каналов
    
    По аналогии с рабочим кодом StimulationWithPreset.py:
    - Регистр 44: полярность (битовая маска: 1 << channel_num для каждого канала)
    - Регистр 42: включение/выключение (битовая маска: 1 << channel_num для каждого канала)
    
    Args:
        spi: Объект SPIController
        channels: Список номеров каналов (0-15)
        enable: True для включения, False для выключения
        negative_polarity: True для отрицательной полярности, False для положительной
        verbose: Выводить отладочную информацию
    """
    if enable:
        # Формируем битовую маску для полярности (регистр 44)
        # Каждый бит соответствует каналу: бит 0 = канал 0, бит 1 = канал 1, и т.д.
        polarity_mask = 0x0000
        for channel in channels:
            if not negative_polarity:
                # Положительная полярность: устанавливаем бит канала
                polarity_mask |= (1 << channel)
        
        # КРИТИЧНО: Register 44 БЕЗ U-флага - накапливаем в shadow-RAM
        write_intan_register(spi, 44, polarity_mask, u_flag=0, verbose=False)
        time.sleep(0.001)  # Минимальная задержка
        
        # Формируем битовую маску для включения стимуляторов (регистр 42)
        stim_enable_mask = 0x0000
        for channel in channels:
            stim_enable_mask |= (1 << channel)
        
        # КРИТИЧНО: Register 42 С U=1 - это финальная команда для применения всех накопленных изменений
        # Все изменения в triggered регистрах (44, 64-79, 96-111) применяются здесь
        write_intan_register(spi, 42, stim_enable_mask, u_flag=1, verbose=False)
        time.sleep(0.001)  # Задержка для применения всех triggered регистров
    else:
        # Выключаем стимуляторы (регистр 42)
        # Можно выключить все или только выбранные каналы
        if len(channels) == 0:
            # Выключить все
            stim_disable_value = 0x0000
        else:
            # Выключить только выбранные каналы (сбрасываем биты)
            # Читаем текущее значение регистра 42
            current_value = read_intan_register(spi, 42, verbose=False)
            stim_disable_value = current_value
            for channel in channels:
                stim_disable_value &= ~(1 << channel)  # Сбрасываем бит канала
        
        # КРИТИЧНО: Register 42 С U=1 - это финальная команда для применения всех накопленных изменений
        write_intan_register(spi, 42, stim_disable_value, u_flag=1, verbose=False)
        time.sleep(0.001)  # Задержка для применения всех triggered регистров


def poll_register_until_ready(spi, reg_addr, expected_value, timeout=5.0, poll_interval=0.01, verbose=True):
    """Опрашивает регистр до получения ожидаемого значения"""
    start_time = time.time()
    attempts = 0
    last_value = None
    
    if verbose:
        print(f"Ожидание инициализации Intan (регистр {reg_addr} должен стать {expected_value})...")
    
    while True:
        attempts += 1
        elapsed = time.time() - start_time
        
        if elapsed > timeout:
            if verbose:
                print(f"\n⚠ Таймаут ожидания инициализации ({timeout} сек)")
                if last_value is not None:
                    print(f"   Последнее значение регистра {reg_addr}: {last_value}")
            return (False, last_value, attempts, elapsed)
        
        try:
            value = read_intan_register(spi, reg_addr, verbose=False)
            last_value = value
            
            if verbose and attempts % 10 == 0:
                print(f"  Попытка {attempts}: регистр {reg_addr} = {value} (ожидается {expected_value})")
            
            if value == expected_value:
                elapsed = time.time() - start_time
                if verbose:
                    print(f"\n✓ Intan инициализирован успешно!")
                    print(f"  Регистр {reg_addr} = {value}, попыток: {attempts}, время: {elapsed:.3f} сек")
                return (True, value, attempts, elapsed)
            
            time.sleep(poll_interval)
            
        except Exception as e:
            if verbose:
                print(f"\n⚠ Ошибка при чтении регистра: {e}")
            time.sleep(poll_interval)


def parse_channels(channels_str):
    """
    Парсит строку каналов в список номеров каналов
    
    Поддерживаемые форматы:
    - "0,1,2" -> [0, 1, 2]
    - "0-3" -> [0, 1, 2, 3]
    - "0,2-4,7" -> [0, 2, 3, 4, 7]
    
    Args:
        channels_str: Строка с номерами каналов
    
    Returns:
        Список номеров каналов (0-15)
    """
    channels = []
    parts = channels_str.split(',')
    
    for part in parts:
        part = part.strip()
        if '-' in part:
            # Диапазон каналов
            start, end = part.split('-')
            start = int(start.strip())
            end = int(end.strip())
            if start < 0 or end > 15 or start > end:
                raise ValueError(f"Неверный диапазон каналов: {part}")
            channels.extend(range(start, end + 1))
        else:
            # Один канал
            channel = int(part)
            if channel < 0 or channel > 15:
                raise ValueError(f"Номер канала должен быть в диапазоне 0-15: {channel}")
            channels.append(channel)
    
    # Удаляем дубликаты и сортируем
    channels = sorted(list(set(channels)))
    return channels


def main():
    parser = argparse.ArgumentParser(
        description='Стимуляция каналов Intan RHS2116',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  %(prog)s                              # Стимуляция канала 0 с параметрами по умолчанию
  %(prog)s -c 0,1,2                     # Стимуляция каналов 0, 1, 2
  %(prog)s -c 0-3                       # Стимуляция каналов 0, 1, 2, 3
  %(prog)s -c 0,2-4,7 -n 0 -p 20        # Стимуляция каналов 0, 2, 3, 4, 7
  %(prog)s --once                        # Однократная стимуляция
  %(prog)s --duration 5                  # Стимуляция в течение 5 секунд
  %(prog)s --sawtooth -p 50              # Пилообразная стимуляция 0-50 µA за 1 мс
  %(prog)s --sawtooth -p 100 --sawtooth-steps 100 --sawtooth-duration 0.001  # Быстрая пила
        """
    )
    parser.add_argument('-c', '--channels', type=str, default='0',
                        help='Каналы для стимуляции (формат: "0,1,2" или "0-3" или "0,2-4,7", по умолчанию: "0")')
    parser.add_argument('-g', '--gpio', type=int, default=226,
                        help='Номер GPIO для PH2 (по умолчанию: 226)')
    parser.add_argument('-n', '--neg-current', type=int, default=0,
                        help='Отрицательный ток стимуляции в µA (0-255, по умолчанию: 0)')
    parser.add_argument('-p', '--pos-current', type=int, default=10,
                        help='Положительный ток стимуляции в µA (0-255, по умолчанию: 10)')
    parser.add_argument('--step-size', type=int, default=1, choices=[1],
                        help='Шаг стимуляции в µA (по умолчанию: 1, только 1 µA поддерживается)')
    parser.add_argument('--once', action='store_true',
                        help='Однократная стимуляция')
    parser.add_argument('--duration', type=float, default=None,
                        help='Длительность стимуляции в секундах (по умолчанию: бесконечно)')
    parser.add_argument('--interval', type=float, default=1.0,
                        help='Интервал между стимуляциями в секундах (по умолчанию: 1.0)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Подробный вывод')
    parser.add_argument('-d', '--device', default='/dev/spidev1.1',
                        help='Путь к SPI устройству (по умолчанию: /dev/spidev1.1)')
    parser.add_argument('--no-init-check', action='store_true',
                        help='Пропустить проверку инициализации (регистр 255)')
    parser.add_argument('--sawtooth', action='store_true',
                        help='Режим пилообразной стимуляции (быстрое изменение тока)')
    parser.add_argument('--sawtooth-steps', type=int, default=50,
                        help='Количество шагов в пилообразной стимуляции (по умолчанию: 50)')
    parser.add_argument('--sawtooth-duration', type=float, default=0.001,
                        help='Длительность одного цикла пилы в секундах (по умолчанию: 0.001 = 1 мс)')
    
    args = parser.parse_args()
    
    # Парсим каналы
    try:
        channels = parse_channels(args.channels)
    except ValueError as e:
        print(f"Ошибка: {e}")
        sys.exit(1)
    
    # Проверка диапазона значений тока
    if args.neg_current < 0 or args.neg_current > 255:
        print(f"Ошибка: отрицательный ток должен быть в диапазоне 0-255 µA")
        sys.exit(1)
    if args.pos_current < 0 or args.pos_current > 255:
        print(f"Ошибка: положительный ток должен быть в диапазоне 0-255 µA")
        sys.exit(1)
    
    # Проверка каналов
    if len(channels) == 0:
        print(f"Ошибка: не указаны каналы для стимуляции")
        sys.exit(1)
    
    GPIO_PH2 = args.gpio
    REG_ADDR = 255
    EXPECTED_VALUE = 32  # Chip ID для RHS2116
    
    print("=" * 60)
    print("Стимуляция каналов Intan RHS2116")
    print("=" * 60)
    print(f"Каналы: {channels}")
    print(f"GPIO PH2: {GPIO_PH2}")
    print(f"SPI устройство: {args.device}")
    print(f"Параметры стимуляции:")
    print(f"  Шаг стимуляции: {args.step_size} µA")
    print(f"  Отрицательный ток: {args.neg_current} µA")
    print(f"  Положительный ток: {args.pos_current} µA")
    if args.sawtooth:
        print(f"Режим: пилообразная стимуляция")
        print(f"  Шагов: {args.sawtooth_steps}, длительность цикла: {args.sawtooth_duration*1000:.3f} мс")
    elif args.once:
        print(f"Режим: однократная стимуляция")
    elif args.duration:
        print(f"Режим: стимуляция в течение {args.duration} сек")
    else:
        print(f"Режим: непрерывная стимуляция (интервал {args.interval} сек)")
    print("=" * 60)
    print()
    
    # Инициализируем GPIO
    gpio = GPIOController(GPIO_PH2)
    
    try:
        # Настраиваем GPIO как выход
        if args.verbose:
            print("[1/6] Настройка GPIO...")
        gpio.set_direction("out")
        
        # Включаем питание
        if args.verbose:
            print("[2/6] Включение питания Intan (PH2 = 1)...")
        gpio.set_value(1)
        time.sleep(0.01)  # Стабилизация питания
        
        # Инициализируем SPI
        if args.verbose:
            print("[3/6] Инициализация SPI...")
        spi = SPIController(
            device=args.device,
            max_speed_hz=10000000,
            mode=0
        )
        spi.open()
        
        if args.verbose:
            print(f"      SPI настроен: скорость {spi.max_speed_hz/1e6:.1f} МГц, режим {spi.mode}")
        
        # Проверяем инициализацию
        if not args.no_init_check:
            if args.verbose:
                print("[4/6] Проверка инициализации Intan...")
            success, reg_value, attempts, elapsed = poll_register_until_ready(
                spi=spi,
                reg_addr=REG_ADDR,
                expected_value=EXPECTED_VALUE,
                timeout=5.0,
                poll_interval=0.01,
                verbose=args.verbose
            )
            
            if not success:
                print(f"\n⚠ Предупреждение: Intan может быть не инициализирован")
                print(f"  Последнее значение регистра {REG_ADDR}: {reg_value}")
                response = input("  Продолжить? (y/n): ")
                if response.lower() != 'y':
                    sys.exit(1)
        else:
            if args.verbose:
                print("[4/6] Пропуск проверки инициализации")
        
        # Инициализируем чип (настройка регистров)
        if args.verbose:
            print("[5/7] Инициализация регистров чипа...")
        initialize_intan_chip(spi, verbose=args.verbose)
        
        # Настраиваем стимуляцию
        if args.verbose:
            print("[6/7] Настройка параметров стимуляции...")
        setup_stimulation_channels(
            spi=spi,
            channels=channels,
            neg_current_magnitude=args.neg_current,
            pos_current_magnitude=args.pos_current,
            step_size_1ua=(args.step_size == 1),
            verbose=args.verbose
        )
        
        # Запускаем стимуляцию
        if args.verbose:
            print("[7/7] Запуск стимуляции...")
        
        # Очищаем compliance monitor перед началом
        clear_compliance_monitor(spi, verbose=args.verbose)
        
        start_time = time.time()
        stimulation_count = 0
        
        try:
            if args.sawtooth:
                # Режим пилообразной стимуляции
                # Включаем стимулятор с положительной полярностью
                enable_stimulation_channels(spi, channels, enable=True, negative_polarity=False, verbose=False)
                
                step_delay = args.sawtooth_duration / args.sawtooth_steps
                
                while True:
                    cycle_start = time.time()
                    
                    # Пила: от 0 до максимума (без задержек для максимальной скорости)
                    for step in range(args.sawtooth_steps + 1):
                        current = int((args.pos_current * step) / args.sawtooth_steps)
                        # Устанавливаем ток для всех каналов
                        for channel in channels:
                            set_stimulation_current(spi, current, channel=channel, is_positive=True, verbose=False)
                        # Без задержки - максимальная скорость
                    
                    stimulation_count += 1
                    
                    # Проверяем длительность
                    if args.duration:
                        total_elapsed = time.time() - start_time
                        if total_elapsed >= args.duration:
                            enable_stimulation_channels(spi, channels, enable=False, verbose=False)
                            break
                    
                    # Без интервала между циклами для максимальной скорости
                    
            else:
                # Обычный режим стимуляции
                while True:
                    # Включаем стимуляцию
                    # Для биполярной стимуляции сначала отрицательный импульс, потом положительный
                    if args.neg_current > 0 and args.pos_current > 0:
                        # Биполярная стимуляция: сначала отрицательный импульс
                        enable_stimulation_channels(spi, channels, enable=True, negative_polarity=True, verbose=False)
                        # Без задержки для максимальной скорости
                        enable_stimulation_channels(spi, channels, enable=False, verbose=False)
                        
                        # Затем положительный импульс
                        enable_stimulation_channels(spi, channels, enable=True, negative_polarity=False, verbose=False)
                        # Без задержки для максимальной скорости
                        enable_stimulation_channels(spi, channels, enable=False, verbose=False)
                    else:
                        # Монополярная стимуляция
                        polarity = args.neg_current > 0  # True если отрицательный ток > 0
                        enable_stimulation_channels(spi, channels, enable=True, negative_polarity=polarity, verbose=False)
                    
                    stimulation_count += 1
                    
                    if args.once:
                        # Однократная стимуляция
                        enable_stimulation_channels(spi, channels, enable=False, verbose=False)
                        break
                    
                    # Проверяем длительность
                    if args.duration:
                        elapsed = time.time() - start_time
                        if elapsed >= args.duration:
                            enable_stimulation_channels(spi, channels, enable=False, verbose=False)
                            break
                    
                    # Без интервала для максимальной скорости
                
        except KeyboardInterrupt:
            print("\n\nПрервано пользователем")
            enable_stimulation_channels(spi, channels, enable=False, verbose=args.verbose)
        
        spi.close()
        
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем")
        sys.exit(130)
    except Exception as e:
        print(f"\n\nОШИБКА: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)
    finally:
        # Выключаем стимуляцию перед выходом
        try:
            if 'spi' in locals() and 'channels' in locals():
                enable_stimulation_channels(spi, channels, enable=False, verbose=False)
        except:
            pass
        # Питание можно оставить включенным
        # gpio.set_value(0)


if __name__ == "__main__":
    main()
