#!/usr/bin/env python3
"""
Conveyor MQTT subscriber.
Subscribes to iot/conveyor/state and prints state changes to stdout.
Requires: mosquitto_sub (mosquitto-clients package)
"""
import argparse
import json
import subprocess
import sys
import time


STATE_ICON = {
    "RUNNING_OK":       "OK  ",
    "BLOCKAGE_ALERT":   "BLOK",
    "STRUCTURAL_FAULT": "STRC",
    "CRITICAL_FAULT":   "CRIT",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--topic", default="iot/conveyor/#")
    args = parser.parse_args()

    cmd = ["mosquitto_sub", "-h", args.broker, "-t", args.topic, "-v"]
    print(f"subscribing to {args.topic} on {args.broker}", flush=True)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print("mosquitto_sub not found — install mosquitto-clients", file=sys.stderr)
        return 1

    for raw in proc.stdout:
        raw = raw.strip()
        parts = raw.split(" ", 1)
        if len(parts) != 2:
            continue
        _, payload = parts
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            print(raw)
            continue

        state = ev.get("state", "?")
        icon = STATE_ICON.get(state, "??? ")
        tilt = ev.get("tilt", "?")
        blockage = ev.get("blockage", "?")
        dist = ev.get("distance_cm", "?")
        light = ev.get("light", "?")
        reason = ev.get("reason", 0)
        node = ev.get("node_id", "?")
        wall = time.strftime("%H:%M:%S", time.localtime(ev.get("created_at", 0)))

        print(
            f"[{wall}] [{icon}] {node:12s} "
            f"state={state:<18s} tilt={tilt:<7s} blockage={blockage:<8s} "
            f"dist={dist:>4}cm light={light:>5}  reason=0x{reason:02x}"
        )
        sys.stdout.flush()

    proc.wait()
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
