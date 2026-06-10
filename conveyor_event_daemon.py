#!/usr/bin/env python3
"""
Conveyor kernel event → MQTT publisher.
Pure Python stdlib — no external packages, no mosquitto_pub needed.
Maintains a persistent TCP connection to the broker.

Usage:
    python3 conveyor_event_daemon.py [--broker 192.168.106.203] [--topic iot/conveyor/state]
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time


# ── minimal MQTT v3.1.1 client (publish-only) ─────────────────────────────────

def _enc_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack('!H', len(b)) + b


def _enc_remlen(n: int) -> bytes:
    out = []
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError('broker closed connection')
        buf += chunk
    return buf


class MqttPublisher:
    def __init__(self, host: str, port: int, client_id: str = 'conveyor-pub'):
        self.host = host
        self.port = port
        self.client_id = client_id
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        cid = _enc_str(self.client_id)
        proto = _enc_str('MQTT') + bytes([4, 2, 0, 60])   # v3.1.1, clean-session, keepalive=60s
        body = proto + cid
        pkt = bytes([0x10]) + _enc_remlen(len(body)) + body

        self._sock = socket.create_connection((self.host, self.port), timeout=10)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.sendall(pkt)
        ack = _recvn(self._sock, 4)
        if ack[0] != 0x20 or ack[3] != 0:
            raise RuntimeError(f'CONNACK refused (rc={ack[3]})')
        self._sock.settimeout(None)   # blocking mode for publish

    def publish(self, topic: str, payload: str) -> None:
        if self._sock is None:
            raise RuntimeError('not connected')
        body = _enc_str(topic) + payload.encode()
        pkt = bytes([0x30]) + _enc_remlen(len(body)) + body
        self._sock.sendall(pkt)

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.sendall(bytes([0xE0, 0x00]))
            except OSError:
                pass
            self._sock.close()
            self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()


# ── event parsing ──────────────────────────────────────────────────────────────

def _parse_value(v: str):
    if v.startswith('0x'):
        return int(v, 16)
    try:
        return int(v)
    except ValueError:
        return v


def parse_event(line: str) -> dict:
    event: dict = {}
    for token in line.strip().split():
        if '=' not in token:
            raise ValueError(f'unexpected token: {token!r}')
        k, v = token.split('=', 1)
        event[k] = _parse_value(v)
    event['source'] = 'conveyor_node'
    event['created_at'] = time.time()
    return event


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description='Conveyor kernel event MQTT publisher')
    parser.add_argument('--dev',     default='/dev/conveyor_node0')
    parser.add_argument('--broker',  default='192.168.106.203')
    parser.add_argument('--port',    default=1883, type=int)
    parser.add_argument('--topic',   default='iot/conveyor/state')
    parser.add_argument('--node-id', default='conveyor-1')
    args = parser.parse_args()

    print(f'publishing {args.dev} → mqtt://{args.broker}:{args.port}/{args.topic}', flush=True)

    while True:
        try:
            with MqttPublisher(args.broker, args.port) as pub:
                print('connected to broker', flush=True)
                with open(args.dev, 'r', encoding='utf-8') as dev:
                    for line in dev:
                        try:
                            ev = parse_event(line)
                            ev['node_id'] = args.node_id
                            payload = json.dumps(ev, separators=(',', ':'))
                            pub.publish(args.topic, payload)
                            print(payload, flush=True)
                        except Exception as exc:
                            print(f'parse/publish error: {exc}', file=sys.stderr, flush=True)
        except Exception as exc:
            print(f'broker error: {exc} — retry in 5s', file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == '__main__':
    raise SystemExit(main())
