#!/usr/bin/env python3
"""
test_rig.py: checks that PWR ON and PWR OFF switch the drive on and off.

The drive counts as alive only if a sector can be read from it.
`lsblk`, the udev symlink and `sata_spd` keep showing the drive long after the power is cut.
The kernel may not notice a SATA unplug until it tries real I/O.
"""

import argparse, os, subprocess, sys, time


def link_spd():
    for p in sorted(os.listdir("/sys/class/ata_link")):
        try:
            v = open(f"/sys/class/ata_link/{p}/sata_spd").read().strip()
        except OSError:
            continue
        if v and v != "<unknown>":
            return f"{p}={v}"
    return "all <unknown>"


def drive_alive(dev, timeout=6):
    """True if a sector can be read from the drive."""
    if not os.path.exists(dev):
        return False
    try:
        r = subprocess.run(
            ["dd", f"if={dev}", "of=/dev/null", "bs=512", "count=1"], capture_output=True, timeout=timeout
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def wait_for(dev, state, sec=25):
    """Polls `drive_alive()` until it equals `state` and returns (reached, seconds_taken)."""
    t0 = time.time()
    while time.time() - t0 < sec:
        if drive_alive(dev) == state:
            return True, time.time() - t0
        time.sleep(0.5)
    return False, time.time() - t0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="/dev/pico-rig", help="Pico serial port")
    p.add_argument("--by-path", required=True, help="name from /dev/disk/by-path, e.g. pci-0000:00:17.0-ata-3")
    args = p.parse_args()
    dev = os.path.join("/dev/disk/by-path", args.by_path)

    import serial

    s = serial.Serial(args.port, 115200, timeout=2)
    time.sleep(0.6)
    s.reset_input_buffer()

    def cmd(c, w=1.0):
        s.write((c + "\r\n").encode())
        time.sleep(w)
        return s.read_all().decode(errors="replace").strip().replace("\r\n", " | ")

    errors = []
    st = cmd("STATE")
    print(f"resting state after reset: {st}")
    print(f"   drive alive={drive_alive(dev)}  link: {link_spd()}")
    if drive_alive(dev):
        errors.append("unsafe resting state: the drive is powered after a Pico reset")

    for step, expected in (("PWR ON", True), ("PWR OFF", False), ("PWR ON", True)):
        print(f"\n{step} -> {cmd(step)}")
        ok, took = wait_for(dev, expected)
        print(f"   drive alive={drive_alive(dev)} (expected {expected}) after {took:.1f} s, link: {link_spd()}")
        if not ok:
            errors.append(f"{step}: the drive did not reach state {expected} within 25 s")

    print(f"\nfinal STATE: {cmd('STATE')}")
    s.close()
    print()
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        return 1
    print("RIG: test passed: PWR ON powers the drive, PWR OFF cuts it, the resting state is safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
