#!/usr/bin/env python3
"""Background OBD logger with optional spoken dropout alerts.

  watch.py start coolant [iat ...] [--say] [--interval 0.2] [--duration 900]
  watch.py status
  watch.py stop

`start` checks the car answers, then detaches and returns at once. The logger
writes CSV until `stop` or until --duration seconds pass, and always exits
cleanly. `stop` and `status` print a summary of the log, including every
dropout of the first PID (value at or below --drop-below, or no reply).

With --say the PC speaks "ready", "drop", "back" and "no data" as they happen.
"""
import argparse, json, os, signal, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import obd  # noqa: E402

STATE_DIR = os.path.expanduser("~/.cache/obd2")
STATE = os.path.join(STATE_DIR, "watch.json")


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def running(st):
    if not st:
        return False
    try:
        with open(f"/proc/{st['pid']}/cmdline", "rb") as f:
            return b"watch.py" in f.read()
    except OSError:
        return False


def say(text):
    try:
        subprocess.Popen(["spd-say", "-i", "100", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def summarize(st):
    """Print what the log holds: span, range of the first PID, each dropout."""
    try:
        with open(st["log"]) as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        print(f"log      {st['log']} (missing)")
        return
    rows = []
    for l in lines:
        p = l.split(",")
        try:
            rows.append((float(p[0]), float(p[1]) if len(p) > 1 and p[1] else None))
        except ValueError:
            continue
    print(f"log      {st['log']}")
    if not rows:
        print("samples  0")
        return
    t0 = rows[0][0]
    vals = [v for _, v in rows if v is not None]
    print(f"samples  {len(rows)} over {rows[-1][0] - t0:.0f} s")
    if vals:
        print(f"{st['names'][0]:8} min {min(vals):g}  max {max(vals):g}  last {vals[-1]:g}")
    drops, start, kind = [], None, None
    for t, v in rows:
        k = "no data" if v is None else ("drop" if v <= st["drop_below"] else None)
        if k != kind:
            if kind:
                drops.append((start - t0, t - start, kind))
            start, kind = t, k
    if kind:
        drops.append((start - t0, rows[-1][0] - start, kind))
    if not drops:
        print("dropouts none")
    for at, dur, k in drops:
        print(f"dropout  at {at:.1f} s for {dur:.1f} s ({k})")


def loop(elm, a, log):
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    end = time.monotonic() + a.duration
    state = None
    with open(log, "w", buffering=1) as f:
        f.write("time," + ",".join(a.names) + "\n")
        while not stop and time.monotonic() < end:
            row = []
            for name in a.names:
                try:
                    row.append(obd.read_pid(elm, name)[0])
                except (LookupError, TimeoutError, OSError):
                    row.append(None)
            f.write(f"{time.time():.2f}," +
                    ",".join("" if v is None else f"{v:.2f}" for v in row) + "\n")
            v = row[0]
            new = "lost" if v is None else ("drop" if v <= a.drop_below else "ok")
            if a.say and new != state:
                if state is None and new == "ok":
                    say("ready")
                elif new == "drop":
                    say("drop")
                elif new == "lost":
                    say("no data")
                else:
                    say("back")
            state = new
            time.sleep(a.interval)
    try:
        elm.close()
    except OSError:
        pass


def start(a):
    st = load_state()
    if running(st):
        print(f"already running (pid {st['pid']}); `watch.py stop` first")
        return
    elm = obd.connect(a)
    try:
        v, unit = obd.read_pid(elm, a.names[0])
    except LookupError as e:
        elm.close()
        print(f"not started: vehicle did not answer ({e}). Key in RUN? Reader switch on MS?")
        sys.exit(1)
    os.makedirs(STATE_DIR, exist_ok=True)
    log = a.log or os.path.join(STATE_DIR, time.strftime("watch-%Y%m%d-%H%M%S.csv"))
    log = os.path.abspath(log)
    sys.stdout.flush()
    if os.fork():                       # parent: report and return
        time.sleep(0.3)
        print(f"started  {a.names[0]} = {v:g} {unit}; logging {','.join(a.names)} "
              f"every {a.interval:g} s for up to {a.duration:g} s"
              f"{', speaking dropouts' if a.say else ''}")
        print(f"log      {log}")
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
    nul = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(nul, fd)
    with open(STATE, "w") as f:
        json.dump({"pid": os.getpid(), "log": log, "names": a.names,
                   "drop_below": a.drop_below, "started": time.time()}, f)
    try:
        loop(elm, a, log)
    finally:
        os._exit(0)


def stop(_):
    st = load_state()
    if not st:
        print("not running; no log on record")
        return
    if running(st):
        os.kill(st["pid"], signal.SIGTERM)
        for _ in range(100):
            if not running(st):
                break
            time.sleep(0.1)
        print("stopped" if not running(st) else f"pid {st['pid']} did not exit")
    else:
        print("not running (already finished)")
    summarize(st)


def status(_):
    st = load_state()
    if not st:
        print("not running; no log on record")
        return
    print("running" if running(st) else "not running (finished)")
    summarize(st)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port")
    p.add_argument("--baud", type=int)
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("start")
    s.add_argument("names", nargs="+", choices=sorted(obd.PIDS))
    s.add_argument("--interval", type=float, default=0.2)
    s.add_argument("--duration", type=float, default=900)
    s.add_argument("--say", action="store_true")
    s.add_argument("--drop-below", type=float, default=-20)
    s.add_argument("--log")
    sub.add_parser("stop")
    sub.add_parser("status")
    a = p.parse_args()
    {"start": start, "stop": stop, "status": status}[a.action](a)


if __name__ == "__main__":
    main()
