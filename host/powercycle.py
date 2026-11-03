#!/usr/bin/env python3
"""
powercycle.py: host driver for the Powah power-cycle rig.

Requires: python3, pyserial, hdparm for identify, root privileges (writes to sysfs).

Usage:

    # a single power cycle
    sudo ./powercycle.py --by-path pci-0000:00:17.0-ata-3 cycle

    # boot into ROM mode (J2 shorted while power comes up)
    sudo ./powercycle.py --by-path pci-0000:00:17.0-ata-3 romboot

    # acceptance test: 50 cycles, each one verified
    sudo ./powercycle.py --by-path pci-0000:00:17.0-ata-3 selftest -n 50

    # model, serial, firmware and LBA count from hdparm -I
    sudo ./powercycle.py --by-path pci-0000:00:17.0-ata-3 identify
"""

import argparse
import glob
import os
import subprocess
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is missing: pip install pyserial")


BY_PATH_DIR = "/dev/disk/by-path"


# ---------------------------------------------------------------------------
# Host side: sysfs, hot-plug, identification
# ---------------------------------------------------------------------------


class SataPort:
    """Drive addressed by its SATA port through /dev/disk/by-path.

    In ROM mode a drive often reports a placeholder model and no serial number.
    That makes /dev/disk/by-id useless.
    """

    def __init__(self, by_path):
        self.by_path = by_path
        self.link = os.path.join(BY_PATH_DIR, by_path)

    # --- reading state ---

    def present(self):
        return os.path.exists(self.link)

    def device(self):
        """Returns e.g. /dev/sda, or None."""
        if not self.present():
            return None
        return os.path.realpath(self.link)

    def kname(self):
        dev = self.device()
        return os.path.basename(dev) if dev else None

    # --- control ---

    def detach(self):
        """Detaches the device from the kernel before the power is cut.

        Without this you get a flood of libata errors in dmesg.
        You also risk a link-reset timeout, which can hang the port for a dozen seconds or more.
        """
        kname = self.kname()
        if not kname:
            return False
        path = "/sys/block/%s/device/delete" % kname
        try:
            with open(path, "w") as f:
                f.write("1")
        except OSError as e:
            print("warning: detach failed (%s)" % e, file=sys.stderr)
            return False
        # wait for the symlink to go away
        for _ in range(50):
            if not self.present():
                return True
            time.sleep(0.05)
        return True

    @staticmethod
    def rescan_all():
        """Rescans all SCSI hosts, which is harmless and simpler than finding the host behind the port."""
        for host in glob.glob("/sys/class/scsi_host/host*/scan"):
            try:
                with open(host, "w") as f:
                    f.write("- - -")
            except OSError:
                pass

    def wait_present(self, timeout=20.0, poll=0.1):
        """Waits for the node to appear, re-running the rescan."""
        deadline = time.time() + timeout
        last_rescan = 0.0
        while time.time() < deadline:
            if self.present():
                return True
            now = time.time()
            if now - last_rescan > 1.0:
                self.rescan_all()
                last_rescan = now
            time.sleep(poll)
        return False

    def wait_ready(self, timeout=20.0):
        """Waits until the device can be opened for reading."""
        deadline = time.time() + timeout
        if not self.wait_present(timeout):
            return False
        while True:
            dev = self.device()
            if dev:
                try:
                    fd = os.open(dev, os.O_RDONLY)
                    os.close(fd)
                    return True
                except OSError:
                    pass
            if time.time() >= deadline:
                return False
            time.sleep(0.1)

    # --- identification ---

    def identify(self):
        """Returns a dict with the key fields of hdparm -I."""
        dev = self.device()
        if not dev:
            return None
        try:
            out = subprocess.run(
                ["hdparm", "-I", dev],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"error": str(e)}

        fields = {
            "model": "Model Number:",
            "serial": "Serial Number:",
            "firmware": "Firmware Revision:",
        }
        result = {}
        lba = {}
        for line in out.splitlines():
            s = line.strip()
            for key, prefix in fields.items():
                if s.startswith(prefix):
                    result[key] = s[len(prefix) :].strip()
            # hdparm pads the LBA labels with a varying number of spaces
            label, _, value = s.partition(":")
            words = label.split()
            if words[1:] == ["user", "addressable", "sectors"] and words[0] in ("LBA", "LBA48"):
                lba[words[0]] = value.strip()
        # the 28-bit count saturates at 268435455 on drives over 128 GiB
        if lba:
            result["lba"] = lba.get("LBA48", lba.get("LBA"))
        return result


# ---------------------------------------------------------------------------
# Rig side: Pico over USB CDC
# ---------------------------------------------------------------------------


class RigError(RuntimeError):
    pass


class Rig:
    def __init__(self, port="/dev/pico-rig", timeout=45.0):
        self.ser = serial.Serial(port, 115200, timeout=timeout)
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        if self.command("PING") != "PONG":
            raise RigError("the rig does not answer PING")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self.command("ROM OFF")
        except Exception:
            pass
        self.ser.close()

    def command(self, line):
        self.ser.write((line + "\n").encode())
        self.ser.flush()
        resp = self.ser.readline().decode(errors="replace").strip()
        if not resp:
            raise RigError("timeout on command %r" % line)
        if resp.startswith("ERR"):
            raise RigError("%s -> %s" % (line, resp))
        return resp

    # --- high-level operations ---

    def power_off(self):
        self.command("PWR OFF")

    def power_on(self):
        self.command("PWR ON")

    def state(self):
        return self.command("STATE")

    def _detached_command(self, port, line):
        """Detaches the drive and sends the command, rescanning the hosts if the command fails."""
        port.detach()
        try:
            self.command(line)
        except BaseException:
            SataPort.rescan_all()
            raise

    def cycle(self, port, off_ms=1500, wait=True, timeout=20.0):
        """Plain power cycle that returns the device path, or None."""
        self._detached_command(port, "CYCLE %d" % off_ms)
        if not wait:
            return None
        if not port.wait_ready(timeout):
            return None
        return port.device()

    def romboot(self, port, off_ms=1500, hold_ms=300, wait=True, timeout=20.0):
        """Like cycle(), but with J2 shorted while the power comes up."""
        self._detached_command(port, "ROMBOOT %d %d" % (off_ms, hold_ms))
        if not wait:
            return None
        if not port.wait_ready(timeout):
            return None
        return port.device()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_cycle(args, rig, port):
    dev = rig.cycle(port, off_ms=args.off_ms, timeout=args.timeout)
    if dev:
        print("OK: %s" % dev)
        return 0
    print("ERROR: the device did not come back within %.0f s" % args.timeout, file=sys.stderr)
    return 1


def cmd_romboot(args, rig, port):
    dev = rig.romboot(port, off_ms=args.off_ms, hold_ms=args.hold_ms, timeout=args.timeout)
    if not dev:
        print("ERROR: the device did not come back within %.0f s" % args.timeout, file=sys.stderr)
        return 1
    print("OK: %s" % dev)
    return 0


def cmd_identify(args, rig, port):
    if not port.present():
        print("ERROR: no device at %s" % port.link, file=sys.stderr)
        return 1
    info = port.identify() or {}
    if "error" in info or not info:
        reason = info.get("error")
        print("ERROR: identify failed%s" % (": %s" % reason if reason else ""), file=sys.stderr)
        return 1
    for k in ("model", "serial", "firmware", "lba"):
        print("%-10s %s" % (k + ":", info.get(k, "-")))
    return 0


def cmd_selftest(args, rig, port):
    """Runs N cycles in a row and passes only if every one succeeds."""
    fails = []
    times = []
    for i in range(1, args.n + 1):
        t0 = time.time()
        dev = rig.cycle(port, off_ms=args.off_ms, timeout=args.timeout)
        dt = time.time() - t0
        if dev:
            times.append(dt)
            print("  %3d/%d  OK    %s  %.2fs" % (i, args.n, dev, dt))
        else:
            fails.append(i)
            print("  %3d/%d  FAIL  no device after %.1fs" % (i, args.n, dt))
        time.sleep(args.settle)

    print()
    if times:
        print("return time: min %.2fs  avg %.2fs  max %.2fs" % (min(times), sum(times) / len(times), max(times)))
    if fails:
        print("FAILED: %d/%d misses (cycles: %s)" % (len(fails), args.n, ", ".join(map(str, fails))))
        print("increase --off-ms and repeat, and also check the bleeder and that 3.3 V is disconnected")
        return 1
    print("PASSED: %d/%d" % (args.n, args.n))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="/dev/pico-rig", help="Pico serial port")
    p.add_argument("--by-path", required=True, help="name from /dev/disk/by-path, e.g. pci-0000:00:17.0-ata-3")
    p.add_argument("--off-ms", type=int, default=1500, help="power-off gap")
    p.add_argument("--timeout", type=float, default=20.0, help="how long to wait for the device to come back")

    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("cycle").set_defaults(func=cmd_cycle)

    rb = sub.add_parser("romboot")
    rb.add_argument("--hold-ms", type=int, default=300)
    rb.set_defaults(func=cmd_romboot)

    sub.add_parser("identify").set_defaults(func=cmd_identify)

    st = sub.add_parser("selftest")
    st.add_argument("-n", type=int, default=50)
    st.add_argument("--settle", type=float, default=0.5)
    st.set_defaults(func=cmd_selftest)

    args = p.parse_args()

    if os.geteuid() != 0:
        print("warning: detach and rescan won't work without root", file=sys.stderr)

    if not hasattr(args, "hold_ms"):
        args.hold_ms = 300

    port = SataPort(args.by_path)
    try:
        with Rig(args.port) as rig:
            return args.func(args, rig, port)
    except RigError as e:
        print("rig ERROR: %s" % e, file=sys.stderr)
        return 2
    except serial.SerialException as e:
        print("serial port ERROR: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
