#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/ws_client.py —— 测试用的回环 WebSocket **客户端**（纯标准库，含 HMAC 握手）

为什么要有它：v0.2 的链路层是 WS，测试必须有一个真按 RFC6455 说话、真做双向挑战应答的
对手，否则"握手/帧上限/端口抢占"这些判据只能写在纸上。它扮演的是**扩展那一侧**：
连 ws://127.0.0.1:<port>/link，走完 hello_challenge → hello_verify → hello_verify → welcome。

诚实边界（001-F8 的口径，不许越线）：
  * 它能证明：Bridge 服务端的握手验签、帧上限、并发闸、账本、重试、STALE、速度计算 —— 全对。
  * 它**不能**证明：真实 Chrome 的 MV3 行为（SW 被杀、alarms 实钳制、downloads 语义、
    LNA 档位）。那些只能由人在真浏览器里跑 WS-T0/T0.5/T0e，见 tests/ws_probe_report.md。
  * 只允许连 127.0.0.1：本文件自带断言，非回环直接崩。
  * 本文件属于 tests/，不是交付代码；V4-a 静态审计的作用域不含它（同 fake_chrome.py）。

自带一个"故意不守规矩"的模式，用于安全测试：
  --evil no-key      不带 keyExchange、也不认识 K  → 服务端必须拒
  --evil wrong-hmac  走完整流程但最后一步交错的 hmac → 服务端必须拒
  --evil legacy      只发旧版 {type:hello}          → 服务端必须拒
  --evil big-frame   握手成功后发一个 9000 字节文本帧 → 服务端必须断链 1009
  --evil jumbo-len   声明一个 20000 字节的帧头（不写真身）→ 必须断链 1009
  --evil unmasked    发一个未掩码的客户端帧          → 协议错，必须被拒
  --evil fragment    把 9000 字节拆成两帧发          → 必须被拒（不许靠分片绕过长度闸）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

MAX_FRAME = 8192
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOOPBACK = ("127.0.0.1", "::1", "localhost")

# 与 bridge.py / background.js 三处逐字一致的口径
CTX_BRIDGE = "bridge"
CTX_EXTENSION = "extension"


def loopback_only(host: str, port: int) -> None:
    if host not in LOOPBACK and not str(host).startswith("127."):
        raise AssertionError(f"[ws_client] 拒绝非回环目标 {host}:{port}")


# --------------------------------------------------------------------------
# 帧编解码（客户端 → 服务端必须带掩码，RFC6455 硬性要求）
# --------------------------------------------------------------------------
def mask_unmask(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return b""
    pad = (mask * (len(payload) // 4 + 1))[:len(payload)]
    return bytes(a ^ b for a, b in zip(payload, pad))


def build_frame(payload: bytes, opcode: int = 0x1, fin: bool = True, mask_it: bool = True,
                declared_len: int | None = None) -> bytes:
    n = len(payload)
    ln = declared_len if declared_len is not None else n
    b0 = (0x80 if fin else 0x00) | opcode
    if ln <= 125:
        head = bytes([b0, (0x80 if mask_it else 0) | ln])
    elif ln <= 0xFFFF:
        head = bytes([b0, (0x80 if mask_it else 0) | 126]) + ln.to_bytes(2, "big")
    else:
        head = bytes([b0, (0x80 if mask_it else 0) | 127]) + ln.to_bytes(8, "big")
    mask = secrets.token_bytes(4) if mask_it else b""
    return head + mask + (mask_unmask(payload, mask) if mask_it else payload)


class WsClient:
    """最小可用的 RFC6455 客户端 + 本项目握手。给 fake_chrome 与专项安全测试共用。"""

    def __init__(self, host: str, port: int, path: str = "/link", timeout: float = 10.0):
        loopback_only(host, port)
        self.host, self.port, self.path, self.timeout = host, port, path, timeout
        self.sock: socket.socket | None = None
        self.rbuf = b""
        self.lock = threading.Lock()
        self.closed = False
        self.frames_sent = 0
        self.max_frame_sent = 0

    # ---- 底层 ----
    def connect(self) -> "WsClient":
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)  # noqa: egress-audit
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("服务端在 101 之前就把连接关了")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
        if "101" not in status:
            raise ConnectionError(f"不是 WS 应答：{status}")
        accept = re.search(rb"sec-websocket-accept: *([A-Za-z0-9+/=]+)", head, re.I)
        want = base64.b64encode(hashlib.sha1(f"{key}{WS_GUID}".encode()).digest()).decode()
        if not accept or accept.group(1).decode() != want:
            raise ConnectionError("Sec-WebSocket-Accept 不对，不是合规的 WS 服务端")
        self.sock = s
        return self

    def send_obj(self, obj: dict, fin: bool = True) -> None:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_raw(raw, fin=fin)

    def send_raw(self, raw: bytes, opcode: int = 0x1, fin: bool = True,
                 declared_len: int | None = None, mask_it: bool = True) -> None:
        with self.lock:
            self.frames_sent += 1
            self.max_frame_sent = max(self.max_frame_sent, len(raw))
            self.sock.sendall(build_frame(raw, opcode, fin, mask_it, declared_len))

    def _read(self, n: int) -> bytes:
        while len(self.rbuf) < n:
            if self.closed:
                raise ConnectionError("closed")
            chunk = self.sock.recv(65536)
            if not chunk:
                self.closed = True
                raise ConnectionError("对端关闭")
            self.rbuf += chunk
        out, self.rbuf = self.rbuf[:n], self.rbuf[n:]
        return out

    def recv_frame(self):
        h = self._read(2)
        fin, opcode = bool(h[0] & 0x80), h[0] & 0x0F
        masked, ln = bool(h[1] & 0x80), h[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(self._read(2), "big")
        elif ln == 127:
            ln = int.from_bytes(self._read(8), "big")
        mask = self._read(4) if masked else b""
        payload = mask_unmask(self._read(ln), mask) if masked else self._read(ln)
        return fin, opcode, payload

    def recv_obj(self, timeout: float | None = None) -> dict | None:
        """收一条文本帧并 JSON 化；ping 自动回 pong。

        **必须分清两种 None**（这是 W6/V8′ 能不能收敛的关键）：
          · 只是这一拍没消息（socket.timeout）→ 返回 None，连接仍算活着
          · 对端真死了（EOF / RST / OSError）→ 置 closed=True 再返回 None
        原来两者混成一个，fake_chrome 就以为"还在连"，于是指挥官重启之后
        它一次都不重连 —— 现场看起来像"Bridge 不会重新接受扩展"，其实是夹具没 noticing。
        """
        deadline = time.time() + (timeout or self.timeout)
        while True:
            self.sock.settimeout(max(0.2, deadline - time.time()))
            try:
                fin, opcode, payload = self.recv_frame()
            except socket.timeout:
                return None
            except (OSError, ConnectionError):
                self.closed = True
                return None
            if opcode == 0x9:                       # ping → pong
                self.send_raw(payload, opcode=0xA)
                continue
            if opcode == 0xA:                       # pong
                continue
            if opcode == 0x8:
                self.closed = True
                return None
            if opcode == 0x2:
                return {"_binary": len(payload)}
            try:
                return json.loads(payload.decode("utf-8"))
            except Exception:
                return {"_badjson": payload[:80].decode("utf-8", "replace")}

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        try:
            self.send_raw(int(code).to_bytes(2, "big") + reason.encode()[:120], opcode=0x8)
        except Exception:
            pass
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 握手（扩展那一侧的实现，逐字对齐 background.js 与 bridge.py）
# --------------------------------------------------------------------------
def hmac_hex(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def bridge_proof(key: bytes, client_nonce: str, server_nonce: str) -> str:
    return hmac_hex(key, f"{client_nonce}\n{server_nonce}\n{CTX_BRIDGE}")


def extension_proof(key: bytes, client_nonce: str, server_nonce: str) -> str:
    return hmac_hex(key, f"{server_nonce}\n{client_nonce}\n{CTX_EXTENSION}")


def do_handshake(cli: WsClient, key: bytes | None, evil: str = "",
                 meta: dict | None = None) -> dict:
    """返回 {"ok":bool,"reason":str,"welcome":dict,"key":bytes}

    三种身份，分别对应 W13 的三小问（**语义写死在这里，测试不许靠注释猜**）：
      正常 + key=None   → 真扩展首次 TOFU：自带新 K 并交 keyExchange
      正常 + key=K      → 已配对扩展：不交 keyExchange，只交双向证明
      evil="no-key"     → 声称已配对但**没有** K，且不交 keyExchange（冒充者最常见形状）
      evil="wrong-hmac" → **故意跳过服务端验签**，交一份错的 hmac；
                          这一档把“服务端自己能不能拓出假扩展”单独量出来
                          （否则客户端一发现不对就跑了，根本测不到服务端那一半）
      evil="legacy"     → 只发 v0.1 的 {type:hello}，没有 HMAC
    """
    meta = meta or {}
    cnonce = secrets.token_hex(16)
    use_key = key if key is not None else secrets.token_bytes(32)
    hello = {"v": 2, "type": "hello_challenge", "role": "extension", "clientNonce": cnonce,
             "chromeVersion": meta.get("chromeVersion", "WS-CLIENT/1"),
             "extensionVersion": meta.get("extensionVersion", "test"),
             "port": meta.get("port", 0)}
    if key is None and evil == "":
        hello["keyExchange"] = use_key.hex()
    if evil == "legacy":
        hello = {"v": 1, "type": "hello", "role": "extension"}
    cli.send_obj(hello)
    rep = cli.recv_obj(timeout=12)
    if rep is None:
        return {"ok": False, "reason": "NO_REPLY", "key": use_key}
    if rep.get("type") == "error":
        return {"ok": False, "reason": rep.get("reason"), "key": use_key}
    if rep.get("type") != "hello_verify":
        return {"ok": False, "reason": f"UNEXPECTED:{rep.get('type')}", "key": use_key}
    want = bridge_proof(use_key, cnonce, rep.get("nonce", ""))
    if want != rep.get("hmac") and evil != "wrong-hmac":
        # 服务端交不出正确证明 = 它不知道 K = 冒充 Bridge（本客户端在此**必须**断开）
        return {"ok": False, "reason": "SERVER_HMAC_MISMATCH", "bridgeId": rep.get("bridgeId"),
                "key": use_key, "serverSaid": rep}
    if evil == "wrong-hmac":
        cli.send_obj({"v": 2, "type": "hello_verify", "hmac": "00" * 32})
    else:
        cli.send_obj({"v": 2, "type": "hello_verify",
                      "hmac": extension_proof(use_key, cnonce, rep.get("nonce", ""))})
    wel = cli.recv_obj(timeout=12)
    if wel is not None and wel.get("type") == "error":
        # 服务端验签没过：把它的判词原样报上去，别糊成"没收到 welcome"
        return {"ok": False, "reason": wel.get("reason"), "key": use_key,
                "bridgeId": rep.get("bridgeId"), "rejectedByServer": True}
    if wel is None or wel.get("type") != "welcome":
        return {"ok": False, "reason": f"NO_WELCOME:{wel}", "key": use_key,
                "bridgeId": rep.get("bridgeId")}
    return {"ok": True, "welcome": wel, "key": use_key, "bridgeId": rep.get("bridgeId")}


def default_state_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "chrome-dl-bridge")


def main(argv=None) -> int:
    """单独跑就是"一次握手打靶"，把结果打成 JSON，供 W13 系列取证。"""
    p = argparse.ArgumentParser(description="测试用 WS 客户端（扮演扩展那一侧）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--path", default="/link")
    p.add_argument("--state-dir", default=default_state_dir())
    p.add_argument("--key", default=None, help="已有 K（hex）；不给就走 TOFU")
    p.add_argument("--evil", default="", choices=["", "no-key", "wrong-hmac", "legacy",
                                                  "big-frame", "jumbo-len", "unmasked",
                                                  "fragment"])
    p.add_argument("--rounds", type=int, default=1, help="正常握手重复次数（W13③）")
    p.add_argument("--size", type=int, default=9000, help="big-frame/fragment 用的载荷字节数")
    a = p.parse_args(argv)

    port = a.port
    if port is None:
        ep = Path(a.state_dir) / "endpoint.json"
        if not ep.exists():
            print(json.dumps({"error": "ENDPOINT_NOT_FOUND", "path": str(ep)}))
            return 2
        port = json.loads(ep.read_text(encoding="utf-8"))["wsPort"]
    key = bytes.fromhex(a.key) if a.key else None

    if a.evil in ("big-frame", "jumbo-len", "unmasked", "fragment"):
        # 这些要先完成握手才能测"通道里的帧上限"
        cli = WsClient(a.host, port, a.path).connect()
        h = do_handshake(cli, key)
        if not h["ok"]:
            print(json.dumps({"evil": a.evil, "ok": False, "reason": "HANDSHAKE_FAILED",
                              "detail": h.get("reason")}))
            return 1
        key = h["key"]
        if a.evil == "big-frame":
            cli.send_raw(b"x" * a.size)
        elif a.evil == "jumbo-len":
            cli.send_raw(b"{\"a\":1}", declared_len=20000)
        elif a.evil == "unmasked":
            cli.send_raw(b'{"type":"ping"}', mask_it=False)
        elif a.evil == "fragment":
            half = a.size // 2
            cli.send_raw(b"{" + b"y" * (half - 1), fin=False)
            cli.send_raw(b"y" * (a.size - half) + b"}", opcode=0x0)
        got = cli.recv_obj(timeout=6)
        closed = got is None
        code = None
        if not closed and got.get("_binary") is None:
            pass
        # 服务端处置：关连接码 1009 会先被 recv_frame 看到（close 帧）→ 这里再探一次
        try:
            cli.sock.settimeout(2)
            fin, opcode, payload = cli.recv_frame()
            if opcode == 0x8:
                code = int.from_bytes(payload[:2], "big")
        except Exception:
            closed = True
        out = {"evil": a.evil, "linkDropped": bool(closed or code is not None),
               "closeCode": code, "echo": got}
        out["verdict"] = "PASS" if out["linkDropped"] else "FAIL"
        print(json.dumps(out, ensure_ascii=False))
        cli.close()
        return 0 if out["verdict"] == "PASS" else 1

    results = []
    for i in range(a.rounds):
        cli = WsClient(a.host, port, a.path).connect()
        h = do_handshake(cli, key, evil=a.evil)
        results.append({"round": i + 1, "ok": h["ok"], "reason": h.get("reason"),
                        "bridgeId": h.get("bridgeId"),
                        "welcomePort": (h.get("welcome") or {}).get("port"),
                        "welcomeMaxFrame": (h.get("welcome") or {}).get("maxFrame"),
                        "peerPid": (h.get("welcome") or {}).get("peerPid")})
        if key is None and h["ok"]:
            key = h["key"]              # TOFU 之后复用同一把 K
        cli.close()
    ok_all = all(r["ok"] for r in results)
    out = {"evil": a.evil, "rounds": len(results), "results": results,
           "verdict": ("PASS" if ok_all else "FAIL") if a.evil == ""
           else ("PASS" if all(not r["ok"] for r in results) else "FAIL"),
           "expect": "全部握手成功" if a.evil == "" else "全部握手被拒"}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
