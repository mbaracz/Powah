"""
pico_rig.py: firmware for the Powah power-cycle rig.

Target: Raspberry Pi Pico (RP2040), MicroPython 1.20+
Install: copy it to the board as main.py

Protocol: text lines over USB CDC, one command per line, terminated with \n.
Every command gets exactly one line in reply.

  PING                       -> PONG
  STATE                      -> OK PWR=<0|1> ROM=<0|1>
  PWR ON|OFF                 -> OK
  ROM ON [ms] | ROM OFF      -> OK          (ON without ms = auto-release after ROM_MAX_ON_MS)
  CYCLE [off_ms]             -> OK          (cut power, wait, restore it)
  ROMBOOT [off_ms] [hold_ms] -> OK          (cut power, wait, short J2, restore power, hold, release J2)
  error                      -> ERR <reason>

Safety:
  - after a reset both channels are off
  - J2 is released after ROM_MAX_ON_MS, so a crashed host script can't leave it shorted for good
"""

from machine import Pin
import select
import sys
import time

# --- configuration ---

PIN_PWR = 15  # relay module input (SSD +5 V supply)
PIN_ROM = 14  # gate of the MOSFET that shorts J2 to ground

PWR_ACTIVE_HIGH = False  # the relay module is active-low

ROM_ACTIVE_HIGH = True  # leave True for an N-MOSFET

DEFAULT_OFF_MS = 1500  # default power-off gap
DEFAULT_HOLD_MS = 300  # default J2 hold time after power is restored

MAX_OFF_MS = 30000
MAX_HOLD_MS = 10000
ROM_MAX_ON_MS = 15000  # hard limit for the J2 short

# --- state ---

# Each pin starts at its inactive level, because the output latch resets to 0.
_pwr = Pin(PIN_PWR, Pin.OUT, value=0 if PWR_ACTIVE_HIGH else 1)
_rom = Pin(PIN_ROM, Pin.OUT, value=0 if ROM_ACTIVE_HIGH else 1)

_pwr_state = False
_rom_state = False
_rom_deadline = None  # ticks_ms after which J2 gets released


def _drive(pin, active_high, state):
    pin.value(1 if (state == active_high) else 0)


def set_pwr(state):
    global _pwr_state
    _drive(_pwr, PWR_ACTIVE_HIGH, state)
    _pwr_state = state


def set_rom(state, timeout_ms=ROM_MAX_ON_MS):
    global _rom_state, _rom_deadline
    _drive(_rom, ROM_ACTIVE_HIGH, state)
    _rom_state = state
    if state:
        _rom_deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    else:
        _rom_deadline = None


def safety_tick():
    """Releases the J2 short once the time limit has passed."""
    if _rom_state and _rom_deadline is not None:
        if time.ticks_diff(_rom_deadline, time.ticks_ms()) <= 0:
            set_rom(False)


def wait_ms(ms):
    """Sleeps for ms milliseconds and releases J2 at its deadline if that falls inside the wait."""
    if _rom_state and _rom_deadline is not None:
        left = time.ticks_diff(_rom_deadline, time.ticks_ms())
        if left < ms:
            if left > 0:
                time.sleep_ms(left)
                ms -= left
            set_rom(False)
    time.sleep_ms(ms)


# --- I/O ---

_poller = select.poll()
_poller.register(sys.stdin, select.POLLIN)


def readline():
    """Reads one line and runs safety_tick() while it waits."""
    buf = ""
    while True:
        safety_tick()
        if _poller.poll(100):
            ch = sys.stdin.read(1)
            if ch is None:
                continue
            if ch in ("\n", "\r"):
                if buf:
                    return buf
                continue
            buf += ch
            if len(buf) > 64:
                return buf


def reply(line):
    sys.stdout.write(line + "\n")


# --- commands ---


def _arg_int(parts, idx, default, lo, hi):
    if len(parts) <= idx:
        return default
    try:
        v = int(parts[idx])
    except ValueError:
        raise ValueError("bad argument")
    if not (lo <= v <= hi):
        raise ValueError("argument out of range")
    return v


def do_cycle(off_ms):
    set_pwr(False)
    wait_ms(off_ms)
    set_pwr(True)


def do_romboot(off_ms, hold_ms):
    set_pwr(False)
    wait_ms(off_ms)
    set_rom(True, hold_ms + 2000)
    time.sleep_ms(20)  # the short must be stable before power comes up
    set_pwr(True)
    wait_ms(hold_ms)
    set_rom(False)


def handle(line):
    parts = line.strip().upper().split()
    if not parts:
        return "ERR empty"

    cmd = parts[0]

    if cmd == "PING":
        return "PONG"

    if cmd == "STATE":
        return "OK PWR=%d ROM=%d" % (int(_pwr_state), int(_rom_state))

    if cmd == "PWR":
        if len(parts) < 2 or parts[1] not in ("ON", "OFF"):
            return "ERR syntax: PWR ON|OFF"
        set_pwr(parts[1] == "ON")
        return "OK"

    if cmd == "ROM":
        if len(parts) < 2 or parts[1] not in ("ON", "OFF"):
            return "ERR syntax: ROM ON [ms] | ROM OFF"
        if parts[1] == "OFF":
            set_rom(False)
            return "OK"
        ms = _arg_int(parts, 2, ROM_MAX_ON_MS, 1, ROM_MAX_ON_MS)
        set_rom(True, ms)
        return "OK"

    if cmd == "CYCLE":
        off = _arg_int(parts, 1, DEFAULT_OFF_MS, 50, MAX_OFF_MS)
        do_cycle(off)
        return "OK"

    if cmd == "ROMBOOT":
        off = _arg_int(parts, 1, DEFAULT_OFF_MS, 50, MAX_OFF_MS)
        hold = _arg_int(parts, 2, DEFAULT_HOLD_MS, 1, MAX_HOLD_MS)
        do_romboot(off, hold)
        return "OK"

    return "ERR unknown command"


# --- main ---


def main():
    set_pwr(False)
    set_rom(False)
    while True:
        line = readline()
        try:
            reply(handle(line))
        except ValueError as e:
            reply("ERR " + str(e))
        except Exception as e:  # keep the main loop running
            set_rom(False)
            reply("ERR exception: " + str(e))


main()
