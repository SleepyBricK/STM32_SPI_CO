#!/usr/bin/env python3
"""Send one RHS1 USB bulk text command and print the reply."""

import argparse
import sys

from usb_intan_lib import PID, VID, close_device, open_device, run_text_command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="*", default=["PING"])
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument(
        "--reset",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--no-drain", action="store_true")
    args = parser.parse_args()

    command = " ".join(args.command).strip() or "PING"

    try:
        dev, ifn = open_device(args.vid, args.pid, reset=args.reset)
        reply = run_text_command(
            dev,
            command,
            timeout_ms=args.timeout_ms,
            drain_before=not args.no_drain,
        )
        print(reply)
        close_device(dev, ifn)
    except Exception as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
