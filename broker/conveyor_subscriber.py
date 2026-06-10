#!/usr/bin/env python3
"""
Conveyor MQTT subscriber.
Pure Python stdlib — no external packages, no mosquitto_sub needed.
Subscribes to the Mac broker and pretty-prints state changes.

Usage:
    python3 conveyor_subscriber.py [--broker 192.168.106.203] [--topic iot/conveyor/#]
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time


# ── minimal MQTT v3.1.1 subscribe ─────────────────────────────────────────────

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


def _read_remlen(sock: socket.socket) -> int:
    val = mul = 0
    for _ in range(4):
        b = _recvn(sock, 1)[0]
        val += (b & 0x7F) * (128 ** mul)
        mul += 1
        if not (b & 0x80):
            return val
    raise RuntimeError('malformed remaining-length')


def mqtt_messages(host: str, port: int, topic: str, client_id: str = 'conveyor-sub'):
    """Generator that yields (topic, payload) tuples, reconnecting on failure."""
    while True:
        try:
            sock = socket.create_connection((host, port), timeout=10)

            # CONNECT
            cid = _enc_str(client_id)
            proto = _enc_str('MQTT') + bytes([4, 2, 0, 60])
            body = proto + cid
            sock.sendall(bytes([0x10]) + _enc_remlen(len(body)) + body)

            ack = _recvn(sock, 4)
            if ack[0] != 0x20 or ack[3] != 0:
                raise RuntimeError(f'CONNACK refused (rc={ack[3]})')

            # SUBSCRIBE
            pkt_id = struct.pack('!H', 1)
            sub_payload = pkt_id + _enc_str(topic) + bytes([0])   # QoS 0
            sock.sendall(bytes([0x82]) + _enc_remlen(len(sub_payload)) + sub_payload)
            # SUBACK: fixed(1) + remlen(1) + pkt_id(2) + rc(1) = 5 bytes
            _recvn(sock, 5)

            print(f'connected to {host}:{port}, subscribed: {topic}', flush=True)

            sock.settimeout(60)
            while True:
                try:
                    pkt_type = _recvn(sock, 1)[0] & 0xF0
                    rem = _read_remlen(sock)
                    data = _recvn(sock, rem) if rem else b''

                    if pkt_type == 0x30:   # PUBLISH
                        tlen = struct.unpack('!H', data[:2])[0]
                        t = data[2:2 + tlen].decode()
                        p = data[2 + tlen:].decode()
                        yield t, p
                    # 0xD0 = PINGRESP — ignore
                except socket.timeout:
                    sock.sendall(bytes([0xC0, 0x00]))   # PINGREQ keepalive

        except Exception as exc:
            print(f'broker error: {exc} — reconnecting in 5s', file=sys.stderr, flush=True)
            time.sleep(5)


# ── display ────────────────────────────────────────────────────────────────────

STATE_ICON = {
    'RUNNING_OK':       'OK  ',
    'BLOCKAGE_ALERT':   'BLOK',
    'STRUCTURAL_FAULT': 'STRC',
    'CRITICAL_FAULT':   'CRIT',
}


def main() -> int:
    parser = argparse.ArgumentParser(description='Conveyor MQTT subscriber')
    parser.add_argument('--broker', default='192.168.106.203')
    parser.add_argument('--port',   default=1883, type=int)
    parser.add_argument('--topic',  default='iot/conveyor/#')
    args = parser.parse_args()

    for _, payload in mqtt_messages(args.broker, args.port, args.topic):
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            print(payload, flush=True)
            continue

        state    = ev.get('state', '?')
        icon     = STATE_ICON.get(state, '??? ')
        tilt     = ev.get('tilt', '?')
        blockage = ev.get('blockage', '?')
        dist     = ev.get('distance_cm', '?')
        light    = ev.get('light', '?')
        reason   = ev.get('reason', 0)
        node     = ev.get('node_id', '?')
        wall     = time.strftime('%H:%M:%S', time.localtime(ev.get('created_at', 0)))

        print(
            f'[{wall}] [{icon}] {node:12s} '
            f'state={state:<18s} tilt={tilt:<7s} blockage={blockage:<8s} '
            f'dist={dist:>4}cm light={light:>5}  reason=0x{reason:02x}',
            flush=True,
        )

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
