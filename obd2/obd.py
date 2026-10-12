#!/usr/bin/env python3
"""Talk to an ELM327-style OBD2 adapter over a serial port. Stdlib only.

  obd.py probe                 find the adapter, baud rate, and vehicle protocol
  obd.py raw ATRV 0100 ...     send raw AT / OBD commands, print raw replies
  obd.py read rpm speed ...    read decoded live values (see `obd.py pids`)
  obd.py supported             list the mode 01 PIDs the vehicle supports
  obd.py dtc                   read stored and pending trouble codes
  obd.py vin                   read the VIN
  obd.py monitor rpm speed     poll values until Ctrl-C (CSV on stdout)
  obd.py pids                  list the PID names this tool can decode

Read-only: it never sends mode 04 (clear codes) unless you do so via `raw`.
"""
import argparse
import glob
import os
import select
import signal
import sys
import termios
import time

BAUDS = [38400, 9600, 115200, 57600, 19200, 230400, 500000]
NO_DATA = ("NO DATA", "UNABLE TO CONNECT", "CAN ERROR", "BUS INIT", "ERROR",
           "STOPPED", "BUS BUSY", "FB ERROR", "DATA ERROR", "?")

# name: (pid, bytes, unit, decode(list of data bytes))
PIDS = {
    "load":          (0x04, 1, "%",     lambda d: d[0] * 100 / 255),
    "coolant":       (0x05, 1, "C",     lambda d: d[0] - 40),
    "stft1":         (0x06, 1, "%",     lambda d: (d[0] - 128) * 100 / 128),
    "ltft1":         (0x07, 1, "%",     lambda d: (d[0] - 128) * 100 / 128),
    "stft2":         (0x08, 1, "%",     lambda d: (d[0] - 128) * 100 / 128),
    "ltft2":         (0x09, 1, "%",     lambda d: (d[0] - 128) * 100 / 128),
    "fuel_pressure": (0x0A, 1, "kPa",   lambda d: d[0] * 3),
    "map":           (0x0B, 1, "kPa",   lambda d: d[0]),
    "rpm":           (0x0C, 2, "rpm",   lambda d: (d[0] * 256 + d[1]) / 4),
    "speed":         (0x0D, 1, "km/h",  lambda d: d[0]),
    "timing":        (0x0E, 1, "deg",   lambda d: d[0] / 2 - 64),
    "iat":           (0x0F, 1, "C",     lambda d: d[0] - 40),
    "maf":           (0x10, 2, "g/s",   lambda d: (d[0] * 256 + d[1]) / 100),
    "throttle":      (0x11, 1, "%",     lambda d: d[0] * 100 / 255),
    "runtime":       (0x1F, 2, "s",     lambda d: d[0] * 256 + d[1]),
    "mil_distance":  (0x21, 2, "km",    lambda d: d[0] * 256 + d[1]),
    "fuel_level":    (0x2F, 1, "%",     lambda d: d[0] * 100 / 255),
    "baro":          (0x33, 1, "kPa",   lambda d: d[0]),
    "ecu_voltage":   (0x42, 2, "V",     lambda d: (d[0] * 256 + d[1]) / 1000),
    "ambient":       (0x46, 1, "C",     lambda d: d[0] - 40),
    "oil_temp":      (0x5C, 1, "C",     lambda d: d[0] - 40),
    "fuel_rate":     (0x5E, 2, "L/h",   lambda d: (d[0] * 256 + d[1]) / 20),
}


def find_port():
    ports = sorted(glob.glob("/dev/serial/by-id/*")) or sorted(
        glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not ports:
        sys.exit("no serial adapter found (check lsusb / dmesg)")
    return ports[0]


class Elm:
    def __init__(self, port, baud):
        try:
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            sys.exit(f"permission denied on {port}: user must be in the 'dialout' "
                     "group (log out/in after `sudo usermod -aG dialout $USER`)")
        a = termios.tcgetattr(self.fd)
        speed = getattr(termios, f"B{baud}")
        a[0] = 0                                             # iflag: raw
        a[1] = 0                                             # oflag: raw
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # 8N1, no flow ctl
        a[3] = 0                                             # lflag: raw
        a[4] = a[5] = speed
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def close(self):
        os.close(self.fd)

    def cmd(self, text, timeout=5.0):
        """Send one command, return reply lines (without echo or prompt)."""
        termios.tcflush(self.fd, termios.TCIFLUSH)
        os.write(self.fd, text.encode("ascii") + b"\r")
        buf = b""
        end = time.monotonic() + timeout
        while b">" not in buf:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                break
            buf += os.read(self.fd, 4096)
        if b">" not in buf:
            raise TimeoutError(f"no prompt after {text!r}: got {buf!r}")
        out = buf.replace(b"\x00", b"").decode("ascii", "replace").replace(">", "")
        lines = [l.strip() for l in out.replace("\n", "\r").split("\r") if l.strip()]
        if lines and lines[0].replace(" ", "").upper() == text.replace(" ", "").upper():
            lines = lines[1:]                                # drop echo
        return lines

    def init(self):
        """Reset to a known state: echo off, no linefeeds, headers off, auto protocol."""
        self.cmd("ATZ", 6)
        for c in ("ATE0", "ATL0", "ATS1", "ATH0", "ATAT1", "ATSP0"):
            self.cmd(c)

    def obd(self, mode, pid=None, timeout=12.0):
        """Send an OBD request; return one list of data bytes per responding ECU."""
        req = f"{mode:02X}" + ("" if pid is None else f"{pid:02X}")
        lines = self.cmd(req, timeout)
        lines = [l for l in lines if not l.upper().startswith("SEARCHING")]
        bad = [l for l in lines if any(l.upper().startswith(e) for e in NO_DATA)]
        if bad or not lines:
            raise LookupError(f"{req}: {bad[0] if bad else 'empty reply'}")
        want = [mode + 0x40] + ([] if pid is None else [pid])
        frames, multi = [], {}
        for l in lines:
            if ":" in l:                                     # CAN multi-frame "0: 49 02 ..."
                idx, rest = l.split(":", 1)
                multi[int(idx, 16)] = [int(x, 16) for x in rest.split()]
                continue
            try:
                b = [int(x, 16) for x in l.split()]
            except ValueError:
                continue
            if b[:len(want)] == want:
                frames.append(b[len(want):])
        if multi:
            b = [x for k in sorted(multi) for x in multi[k]]
            if b[:len(want)] == want:
                frames.append(b[len(want):])
        if not frames:
            raise LookupError(f"{req}: unparsed reply {lines}")
        return frames


def connect(args, init=True):
    port = args.port or find_port()
    bauds = [args.baud] if args.baud else BAUDS
    for baud in bauds:
        elm = Elm(port, baud)
        time.sleep(0.2)
        # The first command after open is often lost, or lands while the adapter
        # is still busy with a previous request, so ask more than once. Never
        # send a bare CR here: the ELM327 treats it as "repeat last command".
        for _ in range(3):
            try:
                ident = elm.cmd("ATI", 2)
            except TimeoutError:
                continue
            if any("ELM" in l.upper() or "STN" in l.upper() for l in ident):
                elm.port, elm.baud = port, baud
                if init:
                    elm.init()
                return elm
        elm.close()
    sys.exit(f"no ELM327 reply on {port} at {bauds} baud "
             "(is it powered? it draws power from the car's OBD port)")


def supported_pids(elm):
    found = []
    for base in range(0x00, 0xE0, 0x20):
        try:
            d = elm.obd(0x01, base)[0]
        except LookupError:
            break
        bits = int.from_bytes(bytes(d[:4]), "big")
        found += [base + i + 1 for i in range(32) if bits >> (31 - i) & 1]
        if not bits & 1:
            break
    return found


def decode_dtcs(data):
    codes = []
    for i in range(0, len(data) - 1, 2):
        a, b = data[i], data[i + 1]
        if a or b:
            codes.append("PCBU"[a >> 6] + f"{(a >> 4) & 3}{a & 15:X}{b:02X}")
    return codes


def read_dtcs(elm, mode):
    codes = []
    try:
        frames = elm.obd(mode)
    except LookupError:
        return codes
    for f in frames:
        # CAN replies start with a count byte (odd length); legacy ones don't
        codes += decode_dtcs(f[1:] if len(f) % 2 else f)
    return codes


def read_pid(elm, name):
    pid, n, unit, fn = PIDS[name]
    d = elm.obd(0x01, pid)[0]
    if len(d) < n:
        raise LookupError(f"{name}: short reply {d}")
    return fn(d), unit


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="serial device (default: first USB serial adapter)")
    p.add_argument("--baud", type=int, help="baud rate (default: autodetect)")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("probe")
    sub.add_parser("supported")
    sub.add_parser("dtc")
    sub.add_parser("vin")
    sub.add_parser("pids")
    s = sub.add_parser("raw"); s.add_argument("commands", nargs="+")
    s = sub.add_parser("read"); s.add_argument("names", nargs="+", choices=sorted(PIDS))
    s = sub.add_parser("monitor"); s.add_argument("names", nargs="+", choices=sorted(PIDS))
    s.add_argument("--interval", type=float, default=0.5)
    args = p.parse_args()

    if args.action == "pids":
        for name, (pid, _, unit, _) in sorted(PIDS.items(), key=lambda kv: kv[1][0]):
            print(f"01{pid:02X}  {name:14} {unit}")
        return

    if args.action == "raw":
        elm = connect(args, init=False)
        for c in args.commands:
            try:
                print(f"{c} -> {' | '.join(elm.cmd(c, 12))}")
            except TimeoutError as e:
                print(f"{c} -> TIMEOUT ({e})")
        return

    elm = connect(args)
    if args.action == "probe":
        print(f"port      {elm.port}")
        print(f"baud      {elm.baud}")
        for label, c in (("adapter", "ATI"), ("device", "AT@1"), ("voltage", "ATRV")):
            print(f"{label:9} {' '.join(elm.cmd(c))}")
        try:
            elm.obd(0x01, 0x00)
            print(f"vehicle   connected")
            print(f"protocol  {' '.join(elm.cmd('ATDP'))}")
        except LookupError as e:
            print(f"vehicle   NOT RESPONDING ({e})")
            print("          under ~6V: reader is not plugged into the car's OBD port; "
                  "~12V: turn the ignition to RUN")
    elif args.action == "supported":
        names = {v[0]: k for k, v in PIDS.items()}
        for pid in supported_pids(elm):
            print(f"01{pid:02X}  {names.get(pid, '')}")
    elif args.action == "dtc":
        d = elm.obd(0x01, 0x01)[0]
        print(f"MIL {'ON' if d[0] & 0x80 else 'off'}, {d[0] & 0x7F} stored code(s)")
        print("stored :", " ".join(read_dtcs(elm, 0x03)) or "none")
        print("pending:", " ".join(read_dtcs(elm, 0x07)) or "none")
    elif args.action == "vin":
        d = elm.obd(0x09, 0x02)[0]
        print(bytes(x for x in d if 0x20 <= x < 0x7F).decode())
    elif args.action == "read":
        for name in args.names:
            try:
                v, unit = read_pid(elm, name)
                print(f"{name:14} {v:.2f} {unit}")
            except LookupError as e:
                print(f"{name:14} n/a ({e})")
    elif args.action == "monitor":
        print("time," + ",".join(args.names), flush=True)

        def _term(*_):                                       # `timeout`/kill: stop cleanly
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)
        try:
            while True:
                row = []
                for name in args.names:
                    try:
                        row.append(f"{read_pid(elm, name)[0]:.2f}")
                    except LookupError:
                        row.append("")
                print(f"{time.time():.2f}," + ",".join(row), flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        main()
    except LookupError as e:
        sys.exit(f"vehicle did not answer ({e}). Run `obd.py probe` to see why.")
