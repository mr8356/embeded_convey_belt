#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import time


def parse_value(value):
    if value.startswith("0x"):
        return int(value, 16)
    try:
        return int(value)
    except ValueError:
        return value


def parse_event(line):
    event = {}
    for token in line.strip().split():
        if "=" not in token:
            raise ValueError(f"unexpected token: {token!r}")
        key, value = token.split("=", 1)
        event[key] = parse_value(value)

    event["source"] = "conveyor_node"
    event["node_id"] = "conveyor-1"
    event["created_at"] = time.time()
    return event


def publish_with_mosquitto(host, topic, payload):
    cmd = ["mosquitto_pub", "-h", host, "-t", topic, "-m", payload]
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="Read conveyor_node kernel events and optionally publish MQTT."
    )
    parser.add_argument("--dev", default="/dev/conveyor_node0")
    parser.add_argument("--broker", help="MQTT broker host; prints only if omitted")
    parser.add_argument("--topic", default="iot/conveyor/state")
    parser.add_argument("--node-id", default="conveyor-1")
    args = parser.parse_args()

    if args.broker and not shutil.which("mosquitto_pub"):
        print("mosquitto_pub not found; install mosquitto-clients or omit --broker", file=sys.stderr)
        return 2

    with open(args.dev, "r", encoding="utf-8") as dev:
        for line in dev:
            event = parse_event(line)
            event["node_id"] = args.node_id
            payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            print(payload, flush=True)
            if args.broker:
                publish_with_mosquitto(args.broker, args.topic, payload)


if __name__ == "__main__":
    raise SystemExit(main())
