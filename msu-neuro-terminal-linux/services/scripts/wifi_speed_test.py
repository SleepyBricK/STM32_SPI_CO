#!/usr/bin/env python3
"""
Тест скорости Wi-Fi между ПК и Orange Pi Zero 2W.

Использует только стандартную библиотеку Python 3.

Примеры:
  # На Orange Pi (сервер):
  python3 wifi_speed_test.py server

  # На ПК (клиент), подставьте IP Pi:
  python3 wifi_speed_test.py client 192.168.31.191

  # Дольше и больше раундов:
  python3 wifi_speed_test.py client 192.168.31.191 -t 15 -n 5
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from typing import Callable

DEFAULT_PORT = 5201
CHUNK_SIZE = 1024 * 1024  # 1 MiB
SOCKET_BUF = 4 * 1024 * 1024  # 4 MiB
# Один буфер на процесс — не пересоздавать на каждый send (важно для ARM)
_SEND_CHUNK = b"\x00" * CHUNK_SIZE


def _fmt_mbps(bytes_count: int, seconds: float) -> str:
    if seconds <= 0:
        return "0.00 Mbit/s"
    mbits = bytes_count * 8 / 1_000_000
    return f"{mbits / seconds:.2f} Mbit/s"


def _fmt_mb_s(bytes_count: int, seconds: float) -> str:
    if seconds <= 0:
        return "0.00 MB/s"
    return f"{bytes_count / seconds / 1_000_000:.2f} MB/s"


def _tune_socket(sock: socket.socket) -> None:
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUF)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUF)
    except OSError:
        pass


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("Соединение закрыто до получения всех данных")
        buf.extend(chunk)
    return bytes(buf)


def _recv_for_duration(sock: socket.socket, duration: float) -> int:
    """Принимает поток данных ровно duration секунд (для download-теста)."""
    end = time.perf_counter() + duration
    total = 0
    sock.settimeout(1.0)
    while time.perf_counter() < end:
        try:
            data = sock.recv(CHUNK_SIZE)
        except socket.timeout:
            continue
        if not data:
            break
        total += len(data)
    return total


def _drain_until_idle(sock: socket.socket, idle_timeout: float = 0.5) -> int:
    """Дочитывает остаток потока после основного окна измерения."""
    extra = 0
    sock.settimeout(idle_timeout)
    while True:
        try:
            data = sock.recv(CHUNK_SIZE)
        except socket.timeout:
            break
        if not data:
            break
        extra += len(data)
    return extra


def _pump_send(sock: socket.socket, duration: float) -> int:
    """Отправляет поток данных в течение duration секунд (sendall — без повтора байт)."""
    end = time.perf_counter() + duration
    total = 0
    while time.perf_counter() < end:
        try:
            sock.sendall(_SEND_CHUNK)
            total += CHUNK_SIZE
        except BlockingIOError:
            continue
        except (BrokenPipeError, ConnectionResetError):
            break
    return total


def _measure_latency(sock: socket.socket, probes: int) -> list[float]:
    rtts: list[float] = []
    for _ in range(probes):
        t0 = time.perf_counter()
        sock.sendall(struct.pack("!B", 0x01))
        _recv_exact(sock, 1)
        rtts.append((time.perf_counter() - t0) * 1000)
    return rtts


class RoundStats:
    __slots__ = ("upload_mbps", "download_mbps", "rtt_avg_ms", "rtt_max_ms")

    def __init__(self) -> None:
        self.upload_mbps = 0.0
        self.download_mbps = 0.0
        self.rtt_avg_ms = 0.0
        self.rtt_max_ms = 0.0


def _run_once(
    host: str,
    port: int,
    duration: float,
    probes: int,
    label: str,
    connect_fn: Callable[[], socket.socket],
) -> RoundStats:
    print(f"\n--- {label} ---")
    stats = RoundStats()

    # Upload: клиент -> сервер
    sock = connect_fn()
    _tune_socket(sock)
    try:
        sock.sendall(f"UPLOAD {duration:.3f}\n".encode())
        sent = _pump_send(sock, duration)
        resp = _recv_exact(sock, 8)
        server_bytes = struct.unpack("!Q", resp)[0]
        used = min(sent, server_bytes) if server_bytes else sent
        stats.upload_mbps = used * 8 / duration / 1_000_000
        print(
            f"  ПК → Pi (upload):  {_fmt_mbps(used, duration)}  ({_fmt_mb_s(used, duration)})"
        )
        diff = abs(server_bytes - sent)
        if diff > CHUNK_SIZE:
            print(
                f"  Примечание: расхождение {diff} байт "
                f"(клиент {sent}, сервер {server_bytes}) — часто из‑за хвоста в буфере TCP"
            )
    finally:
        sock.close()

    # Download: сервер -> клиент
    sock = connect_fn()
    _tune_socket(sock)
    try:
        sock.sendall(f"DOWNLOAD {duration:.3f}\n".encode())
        received = _recv_for_duration(sock, duration)
        _drain_until_idle(sock, 0.3)
        stats.download_mbps = received * 8 / duration / 1_000_000
        print(
            f"  Pi → ПК (download): {_fmt_mbps(received, duration)}  "
            f"({_fmt_mb_s(received, duration)})"
        )
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    # RTT
    sock = connect_fn()
    _tune_socket(sock)
    try:
        sock.sendall(f"LATENCY {probes}\n".encode())
        rtts = _measure_latency(sock, probes)
    finally:
        sock.close()

    rtts.sort()
    stats.rtt_avg_ms = sum(rtts) / len(rtts)
    stats.rtt_max_ms = rtts[-1]
    print(
        f"  Задержка (TCP):     min {rtts[0]:.2f} ms | "
        f"avg {stats.rtt_avg_ms:.2f} ms | max {stats.rtt_max_ms:.2f} ms"
    )
    return stats


def client_main(args: argparse.Namespace) -> int:
    def connect() -> socket.socket:
        sock = socket.create_connection((args.host, args.port), timeout=10)
        return sock

    print(f"Тест Wi-Fi: клиент → {args.host}:{args.port}")
    print(f"Длительность потока: {args.duration} с, раундов: {args.rounds}")

    all_stats: list[RoundStats] = []
    for i in range(1, args.rounds + 1):
        all_stats.append(
            _run_once(
                args.host,
                args.port,
                args.duration,
                args.probes,
                f"Раунд {i}/{args.rounds}",
                connect,
            )
        )

    n = len(all_stats)
    avg_up = sum(s.upload_mbps for s in all_stats) / n
    avg_dn = sum(s.download_mbps for s in all_stats) / n
    avg_rtt = sum(s.rtt_avg_ms for s in all_stats) / n
    max_rtt = max(s.rtt_max_ms for s in all_stats)

    print("\n=== Итого (среднее по раундам) ===")
    print(f"  ПК → Pi:  {avg_up:.2f} Mbit/s")
    print(f"  Pi → ПК:  {avg_dn:.2f} Mbit/s")
    print(f"  Задержка: avg {avg_rtt:.2f} ms, max по раундам {max_rtt:.2f} ms")
    if avg_dn > 0 and avg_up / avg_dn > 3:
        print(
            "\n  Сильная асимметрия часто бывает на Pi при отправке из Python "
            "или при загрузке CPU. Перезапустите сервер на Pi и повторите тест."
        )
    print("\nГотово.")
    return 0


def _handle_session(conn: socket.socket, addr: tuple[str, int]) -> None:
    _tune_socket(conn)
    line = b""
    while not line.endswith(b"\n"):
        b = conn.recv(1)
        if not b:
            return
        line += b
    cmd = line.decode().strip().split()
    if not cmd:
        return

    op = cmd[0].upper()

    if op == "UPLOAD" and len(cmd) >= 2:
        duration = float(cmd[1])
        received = _recv_for_duration(conn, duration)
        received += _drain_until_idle(conn, 0.5)
        conn.sendall(struct.pack("!Q", received))
        print(f"  [{addr[0]}] upload: {_fmt_mbps(received, duration)} ({received} байт)")

    elif op == "DOWNLOAD" and len(cmd) >= 2:
        duration = float(cmd[1])
        try:
            sent = _pump_send(conn, duration)
            print(f"  [{addr[0]}] download: {_fmt_mbps(sent, duration)} ({sent} байт)")
        except (BrokenPipeError, ConnectionResetError):
            print(f"  [{addr[0]}] download: клиент закрыл соединение раньше")

    elif op == "LATENCY" and len(cmd) >= 2:
        probes = int(cmd[1])
        for _ in range(probes):
            data = _recv_exact(conn, 1)
            conn.sendall(data)
        print(f"  [{addr[0]}] latency: {probes} проб")

    else:
        conn.sendall(b"ERR unknown command\n")


def server_main(args: argparse.Namespace) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.bind, args.port))
    except OSError as e:
        print(f"Не удалось привязать {args.bind}:{args.port}: {e}", file=sys.stderr)
        return 1

    sock.listen(8)
    local_ips = _local_ipv4()
    print(f"Сервер слушает {args.bind}:{args.port}")
    print("IP Orange Pi в LAN:")
    for ip in local_ips:
        print(f"  {ip}")
    print("\nНа ПК запустите:")
    print(f"  python3 wifi_speed_test.py client <IP> -p {args.port}")
    print("Ctrl+C для остановки\n")

    try:
        while True:
            conn, addr = sock.accept()
            try:
                _handle_session(conn, addr)
            except (ConnectionError, OSError) as e:
                print(f"  [{addr[0]}] ошибка: {e}")
            finally:
                conn.close()
    except KeyboardInterrupt:
        print("\nОстановка сервера.")
    finally:
        sock.close()
    return 0


def _local_ipv4() -> list[str]:
    ips: list[str] = []
    try:
        import subprocess

        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=2)
        ips = [p for p in out.split() if "." in p and not p.startswith("127.")]
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.168.255.255", 1))
            ips = [s.getsockname()[0]]
            s.close()
        except OSError:
            ips = ["127.0.0.1"]
    return ips


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Тест скорости Wi-Fi между ПК и Orange Pi Zero 2W",
    )
    sub = p.add_subparsers(dest="mode", required=True)

    ps = sub.add_parser("server", help="Запуск на Orange Pi")
    ps.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    ps.add_argument(
        "-b",
        "--bind",
        default="0.0.0.0",
        help="Адрес привязки (по умолчанию все интерфейсы)",
    )

    pc = sub.add_parser("client", help="Запуск на ПК")
    pc.add_argument("host", help="IP Orange Pi в Wi-Fi сети")
    pc.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    pc.add_argument(
        "-t",
        "--duration",
        type=float,
        default=10.0,
        help="Длительность каждого потокового теста, сек (по умолчанию 10)",
    )
    pc.add_argument(
        "-n",
        "--rounds",
        type=int,
        default=3,
        help="Число раундов (по умолчанию 3)",
    )
    pc.add_argument(
        "--probes",
        type=int,
        default=20,
        help="Число измерений задержки (по умолчанию 20)",
    )

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.mode == "server":
        return server_main(args)
    return client_main(args)


if __name__ == "__main__":
    sys.exit(main())
