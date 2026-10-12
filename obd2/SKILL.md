---
name: obd2
description: Read data from a car through the USB OBD2 reader (ELM327 on /dev/ttyUSB0) plugged into this machine - trouble codes (DTCs), check-engine status, live sensor values (RPM, coolant temp, fuel trims, voltage), VIN, supported PIDs, or raw AT/OBD commands. Use when asked to scan the car, read or look up codes, check the OBD reader, log live engine data, or diagnose a 2013-2016 Dodge Dart.
---

# OBD2 reader

The reader is a MOCATEC ELM327 USB cable (sold on Amazon; it has an HS / MS
switch). It reports itself as ELM327 v1.5 and uses a CH340 serial chip, USB ID
`1a86:7523`.
It shows up as `/dev/ttyUSB0` (stable path:
`/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`) and talks 38400 baud 8N1.

Use the bundled `obd.py` (Python stdlib only, nothing to install). It finds the
port and baud rate itself.

```bash
OBD=~/.claude/skills/obd2/obd.py
python3 $OBD probe                    # adapter, baud, battery voltage, vehicle protocol
python3 $OBD dtc                      # check-engine lamp, stored + pending codes
python3 $OBD read rpm coolant stft1 ltft1 ecu_voltage
python3 $OBD supported                # mode 01 PIDs this vehicle answers
python3 $OBD vin
python3 $OBD monitor coolant rpm load --interval 1 > log.csv   # until Ctrl-C
python3 $OBD pids                     # names `read`/`monitor` can decode
python3 $OBD raw ATRV 0105 "ATDP"     # raw commands, raw replies
```

Always run `probe` first. It tells you which of these states you are in:

| probe output | meaning | what to do |
|---|---|---|
| `no serial adapter found` | reader not plugged into USB | check `lsusb` for `1a86:7523` |
| `permission denied` | user not in `dialout` | see Permissions below |
| `voltage 0.1V`, `UNABLE TO CONNECT` | reader is on USB but not in the car's OBD port | plug it into the car |
| `voltage ~12V`, `UNABLE TO CONNECT` | plugged in, ignition off | key to RUN (engine can be off) |
| `vehicle connected` + protocol | ready | read away |

`ATRV` voltage is the car battery as seen at the OBD port: about 12.2-12.8 V
engine off, 13.5-14.7 V running.

## Rules

- **Read-only by default.** Never send mode `04` (clears codes and readiness
  monitors) or any write/actuator command unless the user explicitly asks for
  it. Record the codes first.
- One process at a time on the port. Kill any running `monitor` before starting
  another command.
- The first command after opening the port is often lost; `obd.py` retries
  `ATI` to cope. By hand, send `ATI` until it answers. Do not send a bare
  carriage return: the ELM327 treats it as "repeat the last command".
- This is a "v1.5" clone: plain OBD2 modes 01/02/03/07/09 work, but
  manufacturer-specific (mode 22) requests and some AT commands may not.
- Decode codes with the knowledge base if one exists for the vehicle, else look
  them up; `P0xxx` are generic, `P1xxx`/`P2xxx`/`U`/`B`/`C` can be make-specific.

## Permissions

`/dev/ttyUSB0` is `root:dialout`. Add your user to `dialout`
(`sudo usermod -aG dialout $USER`), which applies to new login sessions. If a
shell still gets
"permission denied", either log out and in, or grant access for the current
plug-in (needs sudo, lost when the reader is unplugged):

```bash
sudo setfacl -m u:$USER:rw /dev/ttyUSB0
```

## Talking to it by hand

ELM327 protocol: send ASCII plus `\r`, read until the `>` prompt.

- `ATZ` reset, `ATI` version, `ATE0` echo off, `ATH1` show headers, `ATSP0`
  auto-detect protocol, `ATDP` show protocol, `ATRV` battery voltage.
- OBD request = mode byte + PID in hex, e.g. `010C`. Reply echoes mode+0x40:
  `41 0C 1A F8` means RPM = 0x1AF8 / 4 = 1726.
- Modes: `01` live data, `02` freeze frame, `03` stored codes, `07` pending
  codes, `09` vehicle info (`0902` = VIN), `0A` permanent codes.
- Error strings: `UNABLE TO CONNECT` (no ECU answered), `NO DATA` (PID not
  supported or ECU asleep), `SEARCHING...` (protocol auto-detect in progress,
  can take ~10 s on the first request), `?` (unknown command).

To add a decoded value, add a row to the `PIDS` table in `obd.py`.

## Notes from the first real connection (2016 Dart 2.0L, 2026-10-12)

- This reader has an **HS / MS slide switch**. On the 2016 Dart it only
  reaches the engine computer with the switch on **MS**; on HS every OBD
  request returns `CAN ERROR`. If `probe` shows battery voltage and the key is
  in RUN but the vehicle does not respond, have the user flip the switch.
- The Dart answers on ISO 15765-4 CAN 29-bit / 500 kbit (protocol 7).
- `ATRV` reads about 0.8 V low on this clone; trust the `ecu_voltage` PID.
- Freeze frame is read with raw mode 02 requests, frame 00:
  `obd.py raw 020200 020500 020C00 020400 021F00` (DTC, coolant, rpm, load,
  runtime). Decode like mode 01, skipping the frame byte after the PID.
- For logging in the background use `watch.py` (below), not `monitor`.

## Background logging and spoken wiggle test (`watch.py`)

Use this, not `monitor` in a background shell, for any log that runs while the
user works on the car. It detaches itself, returns at once, and always exits
cleanly, so nothing reports a failed command when it stops or times out.

```bash
W=~/.claude/skills/obd2/watch.py
python3 $W start coolant iat --say      # options: --interval 0.2 --duration 900 --log FILE
python3 $W status                       # running or not, plus a summary of the log
python3 $W stop                         # stop and print the summary
```

- `start` first checks the car answers (exit 1 with a reason if not), then
  logs CSV to `~/.cache/obd2/watch-<time>.csv` unless `--log` is given.
- The summary lists every dropout of the **first** PID: readings at or below
  `--drop-below` (default -20; an open temperature circuit reads -40 C) and
  moments the car gave no reply.
- `--say` speaks "ready", "drop", "back" and "no data" through `spd-say`, so
  the user hears a dropout while their hand is on the wire. On the PC this
  was written on (HP Compaq Elite 8300, no Bluetooth) the internal speaker needed
  `amixer -c0 sset Speaker 100% unmute` and
  `wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0` to be audible.
- Only one logger runs at a time; it holds the serial port, so `stop` it
  before using `obd.py`.
- `obd.py monitor` also exits 0 on SIGTERM now, for foreground use with
  `timeout`.
