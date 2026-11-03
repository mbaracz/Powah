# Powah

Powah cuts and restores the power of a 2.5" SATA SSD from a script.
A Raspberry Pi Pico switches the drive's 5 V through a relay.
A second channel shorts a pad on the drive to ground while the power comes up, which is how many SSD controllers are put
into ROM mode.
It is meant for long unattended runs where a drive has to be restarted hundreds of times a day, such as data recovery
from a controller that stops answering.

## Repository layout

```
firmware/pico_rig.py     MicroPython firmware for the Pico
host/powercycle.py       host driver, as a command line tool and a Python library
host/test_led.py         switches one channel on and off, no drive needed
host/test_rig.py         checks that PWR ON and PWR OFF switch the drive
udev/99-pico-rig.rules   /dev/pico-rig symlink and access for your user
hardware/                build guide, schematic and parts list
hardware/pcb/            gerbers and checks for the J2 switch board
```

## Requirements

- the rig, built as described in the [build guide](hardware/README.md)
- a Raspberry Pi Pico with [MicroPython](https://micropython.org/download/RPI_PICO/) 1.20 or newer
- Linux with Python 3, pyserial, and hdparm for `identify`
- a SATA port with hot plug enabled in the BIOS, otherwise the kernel doesn't pick the drive up again when the power
  comes back
- root for the scripts that touch the drive, `powercycle.py` and `test_rig.py`

## Installation

### Firmware

Copy the firmware to the Pico as `main.py`, so it starts on power-up.
With [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html):

```bash
mpremote cp firmware/pico_rig.py :main.py
mpremote reset
```

The pins and the relay polarity are set at the top of the file, see [Firmware settings](#firmware-settings).

### udev rule

The rule gives your user access to the Pico and creates the `/dev/pico-rig` symlink, which all scripts use by default.

```bash
sudo cp udev/99-pico-rig.rules /etc/udev/rules.d/
sudo sed -i "s/OWNER=\"username\"/OWNER=\"$USER\"/" /etc/udev/rules.d/99-pico-rig.rules
sudo udevadm control --reload-rules
```

Then unplug the Pico and plug it back in.
The rule matches any Pico running MicroPython.
With more than one connected, add `ATTRS{serial}=="<serial>"` to the rule, taking the value from
`udevadm info -a -n /dev/ttyACM0 | grep serial`.

### Host packages

On Debian and Ubuntu:

```bash
sudo apt install python3-serial hdparm
```

The scripts that touch the drive run under `sudo`, so install pyserial system-wide rather than with
`pip install --user`.

## Drive port

The host scripts find the drive by its SATA port, through `/dev/disk/by-path`.
In ROM mode a drive often reports a placeholder model and no serial number, so `/dev/disk/by-id` can't be used.
Look up the port name once while the drive has power, for example from a plain SATA power cable:

```bash
ls -l /dev/disk/by-path/
# pci-0000:00:17.0-ata-3 -> ../../sda
```

`<port>` in the commands below stands for that name.

## Bring-up

Run these checks in order on a new rig.

1. Before plugging in the drive, check that the Pico answers and the relay follows the commands:

   ```bash
   ./host/test_led.py -n 5 --on-ms 3000 --off-ms 3000
   ```

   While it runs, confirm the relay polarity with an ohmmeter as described
   in [Which terminal is COM, NO and NC](hardware/README.md#which-terminal-is-com-no-and-nc).
   With `--rom` it switches the J2 channel instead.

2. With the drive connected, check that it is unpowered at rest and that `PWR ON` and `PWR OFF` switch it:

   ```bash
   sudo ./host/test_rig.py --by-path <port>
   ```

   Run it right after plugging in the Pico, because it takes the starting state as the state after a reset.

3. Run the acceptance test:

   ```bash
   sudo ./host/powercycle.py --by-path <port> selftest -n 50
   ```

   All 50 cycles have to pass: after each one the drive comes back under the same port and opens for reading.
   If any cycle fails, lengthen `--off-ms`, check the bleeder and that 3.3 V is disconnected, and run it again.

## Usage

### Command line

```bash
sudo ./host/powercycle.py --by-path <port> cycle      # cut the power and restore it
sudo ./host/powercycle.py --by-path <port> romboot    # the same, with J2 shorted while the power comes up
sudo ./host/powercycle.py --by-path <port> identify   # model, serial, firmware and LBA count
sudo ./host/powercycle.py --by-path <port> selftest   # acceptance test
```

`cycle` and `romboot` print the device node once the drive can be read again.

| Option      | Default         | Meaning                                                                  |
|-------------|-----------------|--------------------------------------------------------------------------|
| `--port`    | `/dev/pico-rig` | serial port of the Pico                                                  |
| `--by-path` | required        | port name from `/dev/disk/by-path`                                       |
| `--off-ms`  | 1500            | how long the power stays off, in ms                                      |
| `--timeout` | 20              | how long to wait for the drive to come back, in seconds                  |
| `--hold-ms` | 300             | `romboot` only: how long J2 stays shorted after the power returns, in ms |
| `-n`        | 50              | `selftest` only: number of cycles                                        |
| `--settle`  | 0.5             | `selftest` only: pause between cycles, in seconds                        |

`--hold-ms`, `-n` and `--settle` go after the subcommand, the other options before it.
The exit code is 0 on success, 1 when the drive is missing, doesn't come back, fails the self-test or can't be identified, and 2 when the rig or the serial port fails.

### Python

With `host/` on `PYTHONPATH`:

```python
from powercycle import Rig, SataPort

port = SataPort("pci-0000:00:17.0-ata-3")
with Rig("/dev/pico-rig") as rig:
    dev = rig.romboot(port, off_ms=2000, hold_ms=300)
    if dev is None:
        raise SystemExit("the drive did not come back")
    print(dev, port.identify())
```

`cycle()` and `romboot()` return the device node, or `None` when the drive doesn't come back within `timeout` seconds.
`identify()` returns the main `hdparm -I` fields as a dict, or `None` when the drive is missing.
Check these fields for your drive's ROM-mode signature.
Leaving the `with` block sends `ROM OFF` and closes the serial port.

## How a cycle runs

1. The host detaches the drive through `/sys/block/sdX/device/delete`, which avoids a flood of libata errors and
   link-reset timeouts.
2. The Pico cuts the 5 V and waits `off_ms`.
3. For `romboot` it shorts J2.
4. It restores the 5 V.
5. For `romboot` it holds J2 for `hold_ms`, then releases it.
6. The host rescans all SCSI hosts through `/sys/class/scsi_host/host*/scan` until `/dev/disk/by-path/<port>` appears.
7. The host waits until the device opens for reading.

## Serial protocol

The firmware reads text lines over USB CDC, one command per line, and answers each with exactly one line.
After a reset both channels are off.

| Command                      | Reply                      | Action                                                                                      |
|------------------------------|----------------------------|---------------------------------------------------------------------------------------------|
| `PING`                       | `PONG`                     | liveness check                                                                              |
| `STATE`                      | `OK PWR=<0\|1> ROM=<0\|1>` | state of both channels                                                                      |
| `PWR ON`, `PWR OFF`          | `OK`                       | switch the drive's power                                                                    |
| `ROM ON [ms]`, `ROM OFF`     | `OK`                       | short J2 for `ms` (default `ROM_MAX_ON_MS`) or release it                                   |
| `CYCLE [off_ms]`             | `OK`                       | cut the power, wait, restore it                                                             |
| `ROMBOOT [off_ms] [hold_ms]` | `OK`                       | `CYCLE` with J2 shorted while the power comes up, see [How a cycle runs](#how-a-cycle-runs) |

Commands are case-insensitive and times are in ms.
`CYCLE` and `ROMBOOT` reply once the whole sequence has finished.
Errors and out-of-range arguments come back as `ERR <reason>`.

## Firmware settings

Set at the top of [firmware/pico_rig.py](firmware/pico_rig.py):

| Setting           | Default | Meaning                                                                                                   |
|-------------------|---------|-----------------------------------------------------------------------------------------------------------|
| `PIN_PWR`         | 15      | GPIO driving the relay module input                                                                       |
| `PIN_ROM`         | 14      | GPIO driving the gate of the J2 switch MOSFET                                                             |
| `PWR_ACTIVE_HIGH` | `False` | relay input polarity, checked as in the [build guide](hardware/README.md#which-terminal-is-com-no-and-nc) |
| `ROM_ACTIVE_HIGH` | `True`  | leave `True` for the N-MOSFET board                                                                       |
| `DEFAULT_OFF_MS`  | 1500    | off time when the command gives none                                                                      |
| `DEFAULT_HOLD_MS` | 300     | J2 hold time when the command gives none                                                                  |
| `MAX_OFF_MS`      | 30000   | upper limit for `off_ms`, the lower one is 50                                                             |
| `MAX_HOLD_MS`     | 10000   | upper limit for `hold_ms`                                                                                 |
| `ROM_MAX_ON_MS`   | 15000   | J2 is released after this time even if the host never sends `ROM OFF`                                     |

## License

Released under Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0).
See [`LICENSE`](LICENSE).
