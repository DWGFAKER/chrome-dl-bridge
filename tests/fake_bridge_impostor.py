#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/fake_bridge_impostor.py —— 冒充 Bridge 的**服务端**（仅测试用，127.0.0.1）

用途：造出计划 §5.5 "恢复路径一" 的第一类现场 ——
      "门牌上有个程序，但交不出正确的暗号"（疑似劫持/冒充）。
真机验证（tests/ws_probe_readme.md §7 的"疑似劫持"那一行）需要它先把 18888 占住，
人再看扩展 popup 有没有按 W20 文案弹出来、点〔拉黑并断开〕有没有真拉黑。

诚实边界：本文件**只**用来触发扩展侧的拒绝路径，它不做任何别的事，
也拿不到任何能力（扩展验签失败后会直接关连接并拉黑该端口）。
只监听 127.0.0.1；本文件属于 tests/，不是交付代码。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ws_client import WS_GUID, mask_unmask   # noqa: E402

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    n = len(payload)
    if n <= 125:
        head = bytes([0x80 | opcode, n])
    elif n <= 0xFFFF:
        head = bytes([0x80 | opcode, 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([0x80 | opcode, 127]) + n.to_bytes(8, "big")
    return head + payload            # 服务端帧不掩码


def read_client_frame(conn: socket.socket):
    def rd(n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf
    h = rd(2)
    opcode, ln = h[0] & 0x0F, h[1] & 0x7F
    if ln == 126:
        ln = int.from_bytes(rd(2), "big")
    elif ln == 127:
        ln = int.from_bytes(rd(8), "big")
    mask = rd(4) if (h[1] & 0x80) else b""
    payload = rd(ln)
    return opcode, (mask_unmask(payload, mask) if mask else payload)


def serve(port: int, mode: str, once: bool) -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", port))         # 故意**不**设 SO_EXCLUSIVEADDRUSE：冒充者抢端口就是这么抢的
    s.listen(8)
    print(json.dumps({"impostor": {"port": port, "mode": mode,
                                   "note": "只用于触发扩展的拒绝路径；不做任何别的事"}}),
          flush=True)
    while True:
        conn, peer = s.accept()
        if peer[0] not in LOOPBACK:
            conn.close()
            continue
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(2048)
                if not chunk:
                    break
                head += chunk
            key = re.search(rb"sec-websocket-key: *([A-Za-z0-9+/=]+)", head, re.I)
            if not key:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                continue
            accept = base64.b64encode(hashlib.sha1(
                (key.group(1).decode() + WS_GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            opcode, payload = read_client_frame(conn)
            hello = json.loads(payload.decode())
            print(json.dumps({"got": {"type": hello.get("type"),
                                      "hasKeyExchange": "keyExchange" in hello}},
                             ensure_ascii=False), flush=True)
            if mode == "wrong-hmac":
                # 交一份**算错**的证明：扩展必须识破并断开（W13①/W14b）
                body = {"v": 2, "type": "hello_verify", "ts": time.time(),
                        "nonce": secrets.token_hex(16),
                        "bridgeId": "f" * 32, "hmac": "0" * 64}
                conn.sendall(ws_frame(json.dumps(body).encode()))
            elif mode == "plain":
                # 完全不应答握手，只把连接挂着：等价"端口有人听但协议不像本桥"
                conn.sendall(ws_frame(b"<html>not a bridge</html>", opcode=0x2))
            elif mode == "legacy":
                conn.sendall(ws_frame(json.dumps({"v": 1, "type": "welcome",
                                                  "ok": True}).encode()))
            for _ in range(6):
                try:
                    conn.settimeout(2.0)
                    read_client_frame(conn)
                except Exception:
                    break
        except Exception as exc:
            print(json.dumps({"impostor_error": f"{type(exc).__name__}: {exc}"}), flush=True)
        finally:
            conn.close()
        if once:
            break
    s.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="冒充 Bridge 的回环服务端（仅测试）")
    p.add_argument("--port", type=int, default=18888)
    p.add_argument("--mode", default="wrong-hmac",
                   choices=["wrong-hmac", "plain", "legacy"])
    p.add_argument("--once", action="store_true", help="接一条连接就退（脚本化测试用）")
    a = p.parse_args(argv)
    return serve(a.port, a.mode, a.once)


if __name__ == "__main__":
    sys.exit(main())
