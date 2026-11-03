#!/usr/bin/env python3
"""
test_led.py: rig liveness test that blinks the PWR channel.

Works without a drive and needs neither sysfs nor --by-path.
Sends PWR ON and PWR OFF to the Pico in turn and prints the replies.
Use it to check that:
  - the Pico answers PING,
  - the relay module reacts (input/coil LED),
  - the PWR_ACTIVE_HIGH polarity in the firmware is right.

PWR ON closes COM-NO and puts 5 V on the SATA extension.
The LED only shows the relay input, so check the contacts with an ohmmeter.

Usage:
    ./test_led.py                 # 5 blinks, 1 s each
    ./test_led.py -n 10 --on-ms 300 --off-ms 300
    ./test_led.py --rom           # blinks the J2 channel instead of PWR
"""

import argparse
import sys
import time
import serial


def command(ser, line):
    ser.write((line + "\n").encode())
    ser.flush()
    resp = ser.readline().decode(errors="replace").strip()
    if not resp:
        raise RuntimeError("timeout on command %r" % line)
    if resp.startswith("ERR"):
        raise RuntimeError("%s -> %s" % (line, resp))
    return resp


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="/dev/pico-rig")
    p.add_argument("-n", type=int, default=5, help="number of blinks (default 5)")
    p.add_argument("--on-ms", type=int, default=1000)
    p.add_argument("--off-ms", type=int, default=1000)
    p.add_argument("--rom", action="store_true", help="blink the ROM (J2) channel instead of PWR")
    args = p.parse_args()

    channel = "ROM" if args.rom else "PWR"

    with serial.Serial(args.port, 115200, timeout=5.0) as ser:
        time.sleep(0.3)
        ser.reset_input_buffer()

        print("PING     -> %s" % command(ser, "PING"))
        print("STATE    -> %s" % command(ser, "STATE"))
        print("channel: %s, %d blinks (%d ms ON / %d ms OFF)\n" % (channel, args.n, args.on_ms, args.off_ms))

        try:
            for i in range(1, args.n + 1):
                # ROM ON takes an ms argument (auto-release), PWR ON doesn't.
                on = "%s ON %d" % (channel, args.on_ms) if args.rom else "%s ON" % channel
                print("%2d/%d  %-12s -> %s   [%s]" % (i, args.n, on, command(ser, on), command(ser, "STATE")))
                time.sleep(args.on_ms / 1000.0)

                off = "%s OFF" % channel
                print("%2d/%d  %-12s -> %s   [%s]" % (i, args.n, off, command(ser, off), command(ser, "STATE")))
                time.sleep(args.off_ms / 1000.0)
        finally:
            # safe state no matter how we left the loop
            try:
                command(ser, "PWR OFF")
                command(ser, "ROM OFF")
                print("\nfinal state: %s" % command(ser, "STATE"))
            except Exception as e:
                print("\nWARNING: could not force the safe state: %s" % e, file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
