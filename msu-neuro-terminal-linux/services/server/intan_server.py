#!/usr/bin/env python3
"""
Главный сервер для Intan RHS2116.
Запускает оба сервера: TCP (для стимуляции) и UDP (для регистрации данных).

Использование:
  python3 intan_server.py --backend usb
  python3 intan_server.py --backend spi --gpio 226 --device /dev/spidev1.1

Аргументы:
  --tcp-port: порт для TCP сервера (по умолчанию: 9000)
  --udp-port: порт для UDP сервера (по умолчанию: 9001)
  --backend: usb (STM32 coprocessor) или spi (legacy Orange Pi SPI)
  --gpio: номер GPIO для PH2 (только spi)
  --device: путь к SPI устройству (только spi)
  --verbose: подробный вывод
"""

import argparse
import socketserver
import subprocess
import threading
import time
import sys
import os

# Добавляем путь к модулям
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intan_tcp_server import IntanController, IntanTCPHandler
from intan_udp_recorder import IntanRecorder, UDPRecorderServer
from intan_usb_transport import IntanUsbTransport


class ReuseTCPServer(socketserver.ThreadingTCPServer):
    """SO_REUSEADDR + отдельный handler thread на TCP-клиента."""
    allow_reuse_address = True
    daemon_threads = True


class IntanServer:
    """Главный класс для запуска обоих серверов"""

    def __init__(
        self,
        tcp_port=9000,
        udp_port=9001,
        gpio_number=226,
        spi_device="/dev/spidev1.1",
        verbose=False,
        backend="usb",
        usb_vid=0x0483,
        usb_pid=0x5741,
        usb_reset=True,
    ):
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.gpio_number = gpio_number
        self.spi_device = spi_device
        self.verbose = verbose
        self.backend = backend
        self.usb_vid = usb_vid
        self.usb_pid = usb_pid
        self.usb_reset = usb_reset

        self.transport = None
        self.tcp_server = None
        self.udp_server = None
        self.controller = None
        self.recorder = None
        self.running = False

    @staticmethod
    def _usb_device_present(vid: int, pid: int) -> bool:
        try:
            result = subprocess.run(
                ["lsusb", "-d", f"{vid:04x}:{pid:04x}"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _wait_for_usb_device(self, timeout_s: float = 30.0, poll_s: float = 1.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._usb_device_present(self.usb_vid, self.usb_pid):
                time.sleep(2.0)
                return
            if self.verbose:
                print(
                    f"⏳ Ожидание USB {self.usb_vid:04x}:{self.usb_pid:04x} "
                    f"(осталось {max(0, int(deadline - time.monotonic()))} с)..."
                )
            time.sleep(poll_s)
        raise RuntimeError(
            f"device {self.usb_vid:04x}:{self.usb_pid:04x} not found within {timeout_s:.0f}s"
        )

    def _create_transport(self):
        if self.backend != "usb":
            return None
        self._wait_for_usb_device()
        last_exc = None
        reset_attempts = [self.usb_reset]
        if not self.usb_reset:
            reset_attempts.append(True)

        for use_reset in reset_attempts:
            transport = IntanUsbTransport(
                vid=self.usb_vid,
                pid=self.usb_pid,
                reset_on_open=use_reset,
                verbose=self.verbose,
            )
            try:
                transport.open()
                transport.firmware_version()
                self.transport = transport
                if use_reset and not self.usb_reset and self.verbose:
                    print("✓ USB ответил после bus reset (STM32 был в зависшем состоянии)")
                return self.transport
            except Exception as exc:
                last_exc = exc
                try:
                    transport.close()
                except Exception:
                    pass
                if use_reset is False and True in reset_attempts[1:]:
                    if self.verbose:
                        print(f"⚠ USB без reset: {exc}; повтор с bus reset...")
                    time.sleep(0.5)
                    continue
                break

        raise last_exc or RuntimeError("USB transport open failed")

    def start_tcp_server(self):
        """Запускает TCP сервер для стимуляции"""
        try:
            self.controller = IntanController(
                gpio_number=self.gpio_number,
                spi_device=self.spi_device,
                verbose=self.verbose,
                transport=self.transport,
                backend=self.backend,
            )

            IntanTCPHandler.controller = self.controller

            server_address = ("0.0.0.0", self.tcp_port)

            try:
                self.tcp_server = ReuseTCPServer(
                    server_address,
                    IntanTCPHandler,
                    bind_and_activate=True,
                )
            except OSError as e:
                if e.errno == 98:
                    print(f"⚠ Порт {self.tcp_port} уже занят!")
                    print(f"   Попробуйте выполнить:")
                    print(f"   sudo lsof -i :{self.tcp_port}  # найти процесс")
                    print(f"   sudo kill <PID>  # остановить процесс")
                    print(f"   или используйте другой порт: --tcp-port <другой_порт>")
                    raise
                else:
                    raise

            if self.verbose:
                print(f"[TCP] Сервер запущен на порту {self.tcp_port}")

            tcp_thread = threading.Thread(
                target=self.tcp_server.serve_forever, daemon=True
            )
            tcp_thread.start()

            return tcp_thread
        except Exception as e:
            print(f"❌ Ошибка запуска TCP сервера: {e}")
            raise

    def start_udp_server(self):
        """Запускает UDP сервер для регистрации данных"""
        try:
            self.recorder = IntanRecorder(
                gpio_number=self.gpio_number,
                spi_device=self.spi_device,
                verbose=self.verbose,
                transport=self.transport,
                backend=self.backend,
            )

            self.udp_server = UDPRecorderServer(
                recorder=self.recorder,
                udp_port=self.udp_port,
                verbose=self.verbose,
            )

            self.udp_server.start_listening()

            if self.verbose:
                print(f"[UDP] Сервер запущен на порту {self.udp_port}")

        except Exception as e:
            print(f"❌ Ошибка запуска UDP сервера: {e}")
            raise

    def start(self):
        """Запускает оба сервера"""
        self.running = True

        print("=" * 60)
        print("Запуск серверов Intan RHS2116")
        print("=" * 60)
        print(f"Backend: {self.backend}")
        print(f"TCP порт (стимуляция): {self.tcp_port}")
        print(f"UDP порт (регистрация): {self.udp_port}")
        if self.backend == "usb":
            print(f"USB устройство: {self.usb_vid:04x}:{self.usb_pid:04x}")
        else:
            print(f"GPIO PH2: {self.gpio_number}")
            print(f"SPI устройство: {self.spi_device}")
        print("=" * 60)

        if self.backend == "usb":
            try:
                self._create_transport()
                fw = self.transport.firmware_version()
                print(f"✓ USB transport открыт (прошивка {fw})")
            except Exception as e:
                print(f"❌ Не удалось открыть USB: {e}")
                self.stop()
                return False

        try:
            self.start_tcp_server()
            print("✓ TCP сервер запущен")
        except Exception as e:
            print(f"❌ Не удалось запустить TCP сервер: {e}")
            self.stop()
            return False

        try:
            self.start_udp_server()
            print("✓ UDP сервер запущен")
        except Exception as e:
            print(f"❌ Не удалось запустить UDP сервер: {e}")
            self.stop()
            return False

        print("\nСерверы работают. Нажмите Ctrl+C для остановки.\n")
        return True

    def stop(self):
        """Останавливает оба сервера"""
        self.running = False

        if self.udp_server:
            self.udp_server.stop()
            print("✓ UDP сервер остановлен")

        if self.tcp_server:
            self.tcp_server.shutdown()
            self.tcp_server.server_close()
            print("✓ TCP сервер остановлен")

        if self.controller:
            self.controller.close()

        if self.recorder:
            self.recorder.close()

        if self.transport:
            self.transport.close()
            self.transport = None
            print("✓ USB transport закрыт")

    def run(self):
        """Запускает серверы и ожидает завершения"""
        if not self.start():
            sys.exit(1)

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\nОстановка серверов по Ctrl+C...")
        finally:
            self.stop()
            print("\nСерверы остановлены.")


def get_primary_ip():
    """Получает основной IP адрес системы"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "не определен"


def main():
    parser = argparse.ArgumentParser(
        description="Главный сервер Intan RHS2116 (TCP + UDP)"
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=9000,
        help="TCP порт для стимуляции (по умолчанию: 9000)",
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=9001,
        help="UDP порт для регистрации данных (по умолчанию: 9001)",
    )
    parser.add_argument(
        "--backend",
        choices=("usb", "spi"),
        default="usb",
        help="Интерфейс к Intan: usb (STM32) или spi (legacy)",
    )
    parser.add_argument(
        "-g", "--gpio",
        type=int,
        default=226,
        help="Номер GPIO для PH2 (только --backend spi)",
    )
    parser.add_argument(
        "-d", "--device",
        default="/dev/spidev1.1",
        help="Путь к SPI устройству (только --backend spi)",
    )
    parser.add_argument(
        "--usb-vid",
        type=lambda x: int(x, 0),
        default=0x0483,
        help="USB VID STM32 (по умолчанию: 0x0483)",
    )
    parser.add_argument(
        "--usb-pid",
        type=lambda x: int(x, 0),
        default=0x5741,
        help="USB PID (по умолчанию: 0x5741)",
    )
    parser.add_argument(
        "--usb-reset",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="USB bus reset при открытии transport",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Подробный вывод",
    )

    args = parser.parse_args()

    server = IntanServer(
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
        gpio_number=args.gpio,
        spi_device=args.device,
        verbose=args.verbose,
        backend=args.backend,
        usb_vid=args.usb_vid,
        usb_pid=args.usb_pid,
        usb_reset=args.usb_reset,
    )

    server.run()


if __name__ == "__main__":
    main()
