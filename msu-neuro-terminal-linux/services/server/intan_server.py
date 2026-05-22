#!/usr/bin/env python3
"""
Главный сервер для Intan RHS2116.
Запускает оба сервера: TCP (для стимуляции) и UDP (для регистрации данных).

Использование:
  python3 intan_server.py --tcp-port 9000 --udp-port 9001 --gpio 226 --device /dev/spidev1.1

Аргументы:
  --tcp-port: порт для TCP сервера (по умолчанию: 9000)
  --udp-port: порт для UDP сервера (по умолчанию: 9001)
  --gpio: номер GPIO для PH2 (по умолчанию: 226)
  --device: путь к SPI устройству (по умолчанию: /dev/spidev1.1)
  --verbose: подробный вывод
"""

import argparse
import socketserver
import threading
import time
import sys
import os

# Добавляем путь к модулям
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intan_shared_state import IntanSharedState
from intan_tcp_server import IntanController, IntanTCPHandler
from intan_udp_recorder import IntanRecorder, UDPRecorderServer
from stimulate_channel0 import get_preferred_spi_device


class IntanServer:
    """Главный класс для запуска обоих серверов"""
    
    def __init__(self, tcp_port=9000, udp_port=9001, gpio_number=226, 
                 spi_device="/dev/spidev1.1", verbose=False):
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.gpio_number = gpio_number
        self.spi_device = get_preferred_spi_device(spi_device)
        self.verbose = verbose
        
        self.tcp_server = None
        self.udp_server = None
        self.controller = None
        self.recorder = None
        self.shared_state = IntanSharedState()
        self.running = False
    
    def start_tcp_server(self):
        """Запускает TCP сервер для стимуляции"""
        try:
            # Создаем контроллер
            self.controller = IntanController(
                gpio_number=self.gpio_number,
                spi_device=self.spi_device,
                verbose=self.verbose,
                shared_state=self.shared_state,
            )
            
            # Устанавливаем контроллер как класс-переменную для IntanTCPHandler
            IntanTCPHandler.controller = self.controller
            
            # Создаем TCP сервер
            server_address = ("0.0.0.0", self.tcp_port)
            
            try:
                self.tcp_server = socketserver.TCPServer(
                    server_address,
                    IntanTCPHandler,
                    bind_and_activate=True
                )
            except OSError as e:
                if e.errno == 98:
                    print(f"⚠ Порт {self.tcp_port} уже занят!")
                    print(f"   Попробуйте выполнить:")
                    print(f"   lsof -i :{self.tcp_port}  # найти процесс")
                    print(f"   kill <PID>  # остановить процесс")
                    print(f"   или используйте другой порт: --tcp-port <другой_порт>")
                    raise
                else:
                    raise
            
            if self.verbose:
                print(f"[TCP] Сервер запущен на порту {self.tcp_port}")
            
            # Запускаем сервер в отдельном потоке
            tcp_thread = threading.Thread(target=self.tcp_server.serve_forever, daemon=True)
            tcp_thread.start()
            
            return tcp_thread
        except Exception as e:
            print(f"❌ Ошибка запуска TCP сервера: {e}")
            raise
    
    def start_udp_server(self):
        """Запускает UDP сервер для регистрации данных"""
        try:
            # Создаем рекордер
            self.recorder = IntanRecorder(
                gpio_number=self.gpio_number,
                spi_device=self.spi_device,
                verbose=self.verbose,
                shared_state=self.shared_state,
            )
            
            # Создаем UDP сервер
            self.udp_server = UDPRecorderServer(
                recorder=self.recorder,
                udp_port=self.udp_port,
                verbose=self.verbose,
                shared_state=self.shared_state,
            )
            
            # Запускаем прослушивание
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
        print(f"TCP порт (стимуляция): {self.tcp_port}")
        print(f"UDP порт (регистрация): {self.udp_port}")
        print(f"GPIO PH2: {self.gpio_number}")
        print(f"SPI устройство: {self.spi_device}")
        print("=" * 60)
        
        # Запускаем TCP сервер
        try:
            self.start_tcp_server()
            print("✓ TCP сервер запущен")
        except Exception as e:
            print(f"❌ Не удалось запустить TCP сервер: {e}")
            return False
        
        # Запускаем UDP сервер
        try:
            self.start_udp_server()
            print("✓ UDP сервер запущен")
        except Exception as e:
            print(f"❌ Не удалось запустить UDP сервер: {e}")
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
    
    def run(self):
        """Запускает серверы и ожидает завершения"""
        if not self.start():
            return
        
        try:
            # Основной цикл - просто ждем
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
        # Подключаемся к внешнему адресу
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
        help="TCP порт для стимуляции (по умолчанию: 9000)"
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=9001,
        help="UDP порт для регистрации данных (по умолчанию: 9001)"
    )
    parser.add_argument(
        "-g", "--gpio",
        type=int,
        default=226,
        help="Номер GPIO для PH2 (по умолчанию: 226)"
    )
    parser.add_argument(
        "-d", "--device",
        default=get_preferred_spi_device(),
        help="Путь к SPI устройству (по умолчанию: /dev/intan, если доступен, иначе /dev/spidev1.1)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Подробный вывод"
    )
    
    args = parser.parse_args()
    
    # Создаем и запускаем сервер
    server = IntanServer(
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
        gpio_number=args.gpio,
        spi_device=args.device,
        verbose=args.verbose
    )
    
    server.run()


if __name__ == "__main__":
    main()
