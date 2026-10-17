#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_server.py —— 本地假站点（**只监听 127.0.0.1**），给测试当"被测服务器"。

为什么必须有它：断点续传是不是真的，只有服务端说了算。
Reviewer 第 7 条明确要求：mock 必须真返 **206 + Content-Range**，
否则 "resume 成功但其实重头下" 会被记成 restarted=false —— 假续传。

能力：
  GET /file/<name>?size=BYTES&rate=BYTES_PER_SEC[&no-range=1]
    - 支持 Range: bytes=start-[end]  → 206 + Content-Range + Accept-Ranges
    - ?no-range=1                   → 忽略 Range，永远 200 全量（用于 V6）
    - 无 end 的 open-ended 请求会**在发完一个 chunk 后主动掐断连接**
      （模拟断网，用于 V5 制造 interrupted）
    - HEAD 支持（探长度）
  访问日志：每行一条 JSON（method/path/range/status/served/total/keepAlive），
    这是 V5「服务端日志见 Range 请求」的直接证据。
  POST /admin/quit  —— 关掉自己（测试收尾用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")
CHUNK = 64 * 1024


def block_tile(name: str, blk: int) -> bytes:
    """一个 4096 字节的确定性块。文件第 i 个字节 = tile(i//4096)[i%4096]。

    这样无论服务端从哪个偏移量开始响应 Range，产出的字节都与全文件一致，
    所以“断点后拼接”的结果能被完整校验（否则会造成假 CHECKSUM_MISMATCH）。
    """
    seed = hashlib.sha256(f"{name}:{blk}".encode()).digest()
    return (seed * (4096 // len(seed) + 1))[:4096]


def build_block(name: str, start: int, length: int) -> bytes:
    out = bytearray()
    off = start
    remain = length
    while remain > 0:
        tile = block_tile(name, off // 4096)
        base = off % 4096
        n = min(remain, 4096 - base)
        out += tile[base:base + n]
        off += n
        remain -= n
    return bytes(out)


def expected_sha256(name: str, size: int, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    for off in range(0, size, chunk):
        h.update(build_block(name, off, min(chunk, size - off)))
    return h.hexdigest()


class Handler(BaseHTTPRequestHandler):
    server_version = "MockDL/1"
    protocol_version = "HTTP/1.1"

    # ---- 日志 ----
    def log_message(self, fmt, *a):
        pass


    def _log(self, **kw):
        rec = {"ts": round(time.time(), 4), "peer": "%s:%s" % self.client_address[:2]}
        rec.update(kw)
        path = getattr(self.server, "access_log", None)
        if path:
            # 内存里也留一份，避免写文件失败时“流量发生了但没记账”
            mem = getattr(self.server, "mem_log", None)
            if mem is not None:
                mem.append(rec)
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass

    # ---- 路由 ----
    def do_HEAD(self):
        if not self.path.startswith("/file/"):
            self.send_error(404)
            return
        q, name, size, no_range = self._parse()
        self._log(method="HEAD", path=self.path, range=self.headers.get("Range"),
                  status=200, served=0, total=size)
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Type", "application/octet-stream")
        if not no_range:
            self.send_header("Accept-Ranges", "bytes")
        else:
            self.send_header("Accept-Ranges", "none")
        self.send_header("ETag", f'"{name}-{size}"')
        self.end_headers()

    def do_GET(self):
        if not self.path.startswith("/file/"):
            self._log(method="GET", path=self.path, status=404, served=0, total=0,
                      note="NOT_FOUND")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        q, name, size, no_range = self._parse()
        rng = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if rng and not no_range:
            m = RANGE_RE.match(rng.strip())
            if not m:
                self._log(method="GET", path=self.path, range=rng, status=416, served=0,
                          total=size, note="bad-range")
                self.send_error(416, "Range Not Satisfiable")
                return
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
            open_ended = m.group(2) == ""   # noqa: F841  (保留：注释掉的 end 语义供读代码的人对照)
            if start >= size:
                self._log(method="GET", path=self.path, range=rng, status=416, served=0,
                          total=size)
                self.send_error(416, "Range Not Satisfiable")
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "none" if no_range else "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("ETag", f'"{name}-{size}"')
        self.send_header("Last-Modified", self.date_time_string(int(time.time())))
        self.end_headers()

        rate = self._rate(q)
        served = 0
        abrupt = self._abrupt(q)          # 发够这个字节数就掐线（造 interrupted）
        # 同一资源只掉一次线：否则“重试”永远碰上掉线，测不出收敛。
        if abrupt is not None and self._already_aborted(name):
            abrupt = None
        try:
            off = start
            remaining = length
            while remaining > 0:
                n = min(CHUNK, remaining)
                if abrupt is not None and served + n > abrupt:
                    n = max(1, abrupt - served)
                data = build_block(name, off, n)
                self.wfile.write(data)
                self.wfile.flush()
                off += n
                served += n
                remaining -= n
                if rate:
                    time.sleep(n / max(rate, 1))
                if abrupt is not None and served >= abrupt:
                    self._mark_aborted(name)
                    self._log(method="GET", path=self.path, range=rng, status=status,
                              served=served, total=size, note="ABRUPT_HANGUP",
                              keepAlive=False)
                    # 不拆 RST、不用 SO_LINGER：已 flush 的字节必须能到达客户端，
                    # 否则“部分成功 + 掉线”会变成“一个字节也没收到”，测不出真续传。
                    self.close_connection = True
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._log(method="GET", path=self.path, range=rng, status=status,
                      served=served, total=size, note="CLIENT_ABORT")
            return
        self._log(method="GET", path=self.path, range=rng, status=status, served=served,
                  total=size, keepAlive=True)

    def do_POST(self):
        if urlparse(self.path).path == "/admin/quit":
            self.send_response(204)
            self.end_headers()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self.send_error(404)

    # ---- 解析 ----
    def _already_aborted(self, name: str) -> bool:
        return name in getattr(self.server, "aborted", set())

    def _mark_aborted(self, name: str) -> None:
        if not hasattr(self.server, "aborted"):
            self.server.aborted = set()
        self.server.aborted.add(name)

    def _parse(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        name = u.path.rsplit("/", 1)[-1] or "blob"
        size = int((q.get("size") or ["1048576"])[0])
        no_range = (q.get("no-range") or ["0"])[0] in ("1", "true", "yes")
        return q, name, size, no_range

    def _rate(self, q) -> float:
        return float((q.get("rate") or ["0"])[0] or 0)

    def _abrupt(self, q):
        v = (q.get("abrupt") or [""])[0]
        return int(v) if v.isdigit() else None


class MockHTTPD(ThreadingHTTPServer):
    """客户端中途取消（V7 的 cancel 就是这个动作）会抛 ConnectionReset ——属预期。

    socketserver 默认把这类异常整屏 traceback 打出来，看着像源站坏了。
    这里只咽掉"客户端自己掐线"那一类，其余原样抛，别把真故障也一起咽了。
    """

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            mem = getattr(self, "mem_log", None)
            if mem is not None:
                mem.append({"ts": round(time.time(), 4), "peer": "%s:%s" % client_address[:2],
                            "method": "ERR", "status": 0, "served": 0,
                            "note": type(exc).__name__ + " (client abort)"})
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


class MockServer:
    def __init__(self, port: int = 0, access_log: str | None = None):
        self.httpd = MockHTTPD(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.access_log = access_log or f"mock_access_{self.port}.log"
        self.httpd.access_log = self.access_log
        self.httpd.aborted = set()          # 已掉过线的资源名（只掉一次）
        self.httpd.mem_log = []              # 内存里的完整访问流水
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "MockServer":
        self.thread.start()
        return self

    def url(self, name: str, size: int, **kw) -> str:
        q = "&".join([f"{k}={v}" for k, v in kw.items()])
        return f"http://127.0.0.1:{self.port}/file/{name}?size={size}" + (f"&{q}" if q else "")

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass

    def reset_log(self):
        open(self.access_log, "w", encoding="utf-8").close()

    def lines(self) -> list[dict]:
        try:
            with open(self.access_log, encoding="utf-8") as fh:
                return [json.loads(x) for x in fh if x.strip()]
        except OSError:
            return list(getattr(self.httpd, "mem_log", []))

    def mem_lines(self) -> list[dict]:
        return list(getattr(self.httpd, "mem_log", []))

    def wait_bytes(self, total: int, timeout: float = 15.0) -> list[dict]:
        """等服务端累计发出的字节数达到 total。

        为什么需要：HTTP/1.1 下客户端只要读够 Content-Length 就会上报完成，
        而服务端处理线程还差最后一行日志没写。不等就会出现
        “下载真成功了，但服务端流水看不到”的假不一致（实测约 25% 概率）。
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            rows = self.mem_lines()
            if sum(r.get("served") or 0 for r in rows) >= total:
                return rows
            time.sleep(0.1)
        return self.mem_lines()


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=18099)
    p.add_argument("--access-log", default=None)
    a = p.parse_args(argv)
    s = MockServer(a.port, a.access_log)
    s.start()
    print(json.dumps({"mock_server": {"port": s.port, "accessLog": s.access_log}}))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.stop()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
