# 13-16-dart-tools

Tools for working on 2013-2016 Dodge Darts from a Linux PC. So far that means
one thing: reading the car through a cheap USB OBD2 reader.

Built and tested on a 2016 Dart SE, 2.0L Tigershark, 6-speed manual.

## What's here

| Path | What it is |
|---|---|
| `obd2/obd.py` | Command-line client for USB ELM327 readers. Python standard library only. |
| `obd2/watch.py` | Background logger with a dropout summary and optional spoken alerts, for finding intermittent sensor faults. |
| `obd2/SKILL.md` | Instructions that let [Claude Code](https://claude.com/claude-code) drive the reader as a skill. Also the most complete usage notes. |

## Hardware

A USB ELM327 v1.5 clone with a CH340 serial chip (USB ID `1a86:7523`). It
appears as `/dev/ttyUSB0` at 38400 baud. A 10 ft passive USB extension works.

Your user needs to be in the `dialout` group:
`sudo usermod -aG dialout $USER`, then log out and back in.

## Quick start

```bash
python3 obd2/obd.py probe      # adapter, voltage, is the car answering
python3 obd2/obd.py dtc        # check-engine lamp, stored and pending codes
python3 obd2/obd.py vin
python3 obd2/obd.py read coolant iat rpm ecu_voltage
python3 obd2/obd.py pids       # every value name it can decode
python3 obd2/obd.py raw 020200 020500 020C00   # freeze frame: code, coolant, rpm
```

Key in RUN, engine on or off.

Background logging, for example while wiggling a connector:

```bash
python3 obd2/watch.py start coolant --say   # speaks "drop" when the reading falls out
python3 obd2/watch.py status
python3 obd2/watch.py stop                  # prints every dropout with its time
```

## What we learned about the Dart

- It answers on **ISO 15765-4 CAN, 29-bit IDs, 500 kbit** (ELM327 protocol 7).
  Auto-detect finds it.
- Readers with an **HS / MS switch**: ours only reached the engine computer
  with the switch on **MS**. On HS every request returned `CAN ERROR`.
- The clone's own voltage reading (`ATRV`) was about 0.8 V low. The
  `ecu_voltage` value from the car is the one to trust.
- Oil temperature (PID 5C) is not supported on the 2.0L.
- A coolant temperature reading of **-40 C** means the sensor circuit is open.
  On our car that was the sensor plug backing off after its locking clip
  broke, which sets P0118 / P0119 and makes the fans run and the temperature
  warning come on. `watch.py --say` found it in a couple of minutes.

## Safety

The tools only read. Nothing here clears codes or writes to the car.

## Using it as a Claude Code skill

Copy the `obd2` folder to `~/.claude/skills/obd2/`.
