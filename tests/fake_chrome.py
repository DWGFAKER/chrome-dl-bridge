#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fake_chrome.py —— 在**链路层**冒充"Chrome + 下载搬运工"的对手（仅测试用，v0.2 WS 版）

为什么需要它：真实 Chrome 要人手动"加载已解压的扩展"，无法无人值守。本文件用同一套
纸条协议 + 同一套 HMAC 握手连 Bridge 的 WS 口，收到 start 就去取字节、按 500ms 上报 progress、
支持 pause/resume/cancel。这样 Bridge 侧的行为可以被确定性地把玩。

诚实边界（必须写清，001-F8 的口径）：
  * 它能证明：Bridge 的协议、握手验签、账本、并发闸、重试退避、STALE、速度计算、
    RESTARTED 判定 —— 全对。
  * 它**不能**证明：真实 Chrome 的 MV3 行为（SW 被杀、alarms 实钳制、downloads 语义、
    LNA 档位）。那部分只能靠 WS-T0/T0.5/T0e 在真人 Chrome 上跑，本文件一律标 UNVERIFIED，
    不冒充实证；任何验收报告也不许引用本文件的结果主张"扩展已在 Chrome 验过"。
  * 它自己会发 HTTP 取字节 —— 这是"扮演 Chrome 网络栈"的定义所需；正因如此它**不属于交付代码**，
    放在 tests/，V4-a 静态审计的作用域明确排除本文件（审计对象是 bridge.py / client.py）。
  * 只允许连 127.0.0.1：本文件自带断言，非回环直接崩。

故障注入（真实扩展没有，专供造事故）：
  --die-at N          累计 N 字节后整个进程退出（模拟 Chrome 崩溃；V8′ 用）
  --drop-at N         累计 N 字节后**只断链路**，下载线程继续（模拟 SW 被杀，W6 用）
  --no-progress       连上但不报进度（模拟扩展装死）
  --send-blob         握手后立刻发一条 9000 字节的纸条（测服务端 8192 硬闸，W17）
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from ws_client import (WsClient, do_handshake, loopback_only)   # noqa: E402

MAX_FRAME = 8192


def default_state_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "chrome-dl-bridge")


class FakeChrome:
    def __init__(self, args):
        self.args = args
        self.state_dir = Path(args.state_dir)
        self.flow_path = self.state_dir / "fake_chrome_flow.jsonl"
        self.link_port = args.link_port
        self.root = Path(args.download_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, dict] = {}      # jobId -> 运行态
        self.final: dict[str, dict] = {}      # jobId -> 终态（链路死了也得留着，供对账）
        self._dlid: dict[str, int] = {}       # jobId -> 稳定的假 downloadId
        self._dl_seq = 0
        self.key: bytes | None = None
        if args.key:
            self.key = bytes.fromhex(args.key)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.cli: WsClient | None = None
        self.rx: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self.connected_once = False
        self.link_dropped_at = 0              # --drop-at 用：累计字节阈值
        self._dropped = False                 # --drop-at 只掐一次（真 SW 只死一次）
        self.welcome: dict | None = None

    # ---- 链路 ----
    def endpoint(self) -> dict:
        """endpoint.json 会在 Bridge 重启后换端口 → 每轮重读，不缓存。"""
        ep = self.state_dir / "endpoint.json"
        d = json.loads(ep.read_text(encoding="utf-8"))
        self.link_port = d.get("wsPort") or d.get("linkPort")
        return d

    def connect(self) -> bool:
        info = self.endpoint()
        cli = WsClient("127.0.0.1", self.link_port, info.get("wsPath", "/link"), timeout=8)
        cli.connect()
        meta = {"chromeVersion": "FAKE-CHROME/WS-0.2", "extensionVersion": "test",
                "port": self.link_port}
        h = do_handshake(cli, self.key, meta=meta)
        if not h["ok"]:
            print(f"[fake_chrome] 握手失败：{h.get('reason')}", file=sys.stderr)
            cli.close()
            return False
        self.key = h["key"]
        self.welcome = h["welcome"]
        self.cli = cli
        self.rx = threading.Thread(target=self._rx, daemon=True)
        self.rx.start()
        return True

    def send(self, obj: dict) -> None:
        obj.setdefault("ts", round(time.time(), 4))
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(raw) <= MAX_FRAME, f"纸条超长 {len(raw)}，违反 C7/V4-c（本夹具自己也不许超）"
        with self._send_lock:
            try:
                if self.cli and not self.cli.closed:
                    self.cli.send_obj(obj)
            except Exception:
                pass

    def _rx(self) -> None:
        while not self.stop.is_set():
            cli = self.cli                      # 本地快照：下载线程可能此刻把它置 None
            if cli is None or cli.closed:
                break
            msg = cli.recv_obj(timeout=1.0)
            if msg is None:
                # 关键区分：**空闲 1 秒没消息** ≠ **链路断了**。recv_obj 两种都返 None，
                # 但只有真断线才会把 cli.closed 置起来。当年没分这一刀，假 Chrome
                # 会把每一次正常静默都当成掉线，1 秒一条重连，把链路打成抖振。
                if cli.closed:
                    break
                continue
            if msg.get("_binary") is not None or msg.get("_badjson") is not None:
                continue
            t = msg.get("type")
            if t == "welcome":
                self.on_welcome(msg)
            elif t == "start":
                self.on_start(msg)
            elif t in ("pause", "resume", "cancel"):
                self.on_control(msg, t)
            elif t == "ping":
                self.send({"type": "pong"})
        # 链路断了：不重开下载，等 run() 的循环重连（等价 SW 被杀）
        with self._send_lock:
            self.cli = None

    # ---- 行为 ----
    def on_welcome(self, msg):
        """重连后对账 —— 这一步是 V8′/W6 能收敛的唯一原因。

        真 Chrome 的行为是：SW 死了下载**照跑**，SW 醒来后 sendSnapshot() 用
        chrome.downloads.search() 把**所有**任务（含已经跑完的）真实状态发回去。
        本夹具原来只报"内存里还在跑的"，链路断期间跑完的那一单就永远没人说 ——
        Bridge 只能干看着 DOWNLOADING + STALE。那是夹具的错，不是被测代码的错，
        所以这里补一份 final 台账（等价 storage 里留着的 tasks）。
        """
        with self.lock:
            items = []
            for jid, st in list(self.tasks.items()):
                items.append({"jobId": jid, "downloadId": st["downloadId"],
                              "state": "paused" if st["paused"] else "in_progress",
                              "bytesReceived": st["bytes"],
                              "totalBytes": st.get("total"), "canResume": True,
                              "restarted": st["restarted"]})
            for jid, fin in list(self.final.items()):
                it = dict(fin)
                it["jobId"] = jid
                items.append(it)
        if items:
            self.send({"type": "snapshot", "items": items})

    def on_start(self, msg):
        jid = msg["jobId"]
        with self.lock:
            self.final.pop(jid, None)             # Bridge 重新下单 → 旧终态作废
            if jid in self.tasks:                 # 幂等：同一任务不重开线程
                return
            did = self._dlid.get(jid)
            if did is None:
                self._dl_seq += 1
                did = 1000 + self._dl_seq
                self._dlid[jid] = did
            self.tasks[jid] = {"downloadId": did,
                               "url": msg["url"], "filename": msg["filename"],
                               "bytes": 0, "total": None, "paused": False,
                               "cancelled": False, "restarted": False,
                               "resumeRequested": bool(msg.get("resume"))}
        threading.Thread(target=self._download, args=(jid,), daemon=True).start()

    def on_control(self, msg, act):
        jid = msg.get("jobId")
        with self.lock:
            st = self.tasks.get(jid)
            if not st:
                return
            if act == "pause":
                st["paused"] = True
            elif act == "resume":
                st["paused"] = False
            elif act == "cancel":
                st["cancelled"] = True
        self.send({"type": "ack", "jobId": jid, "downloadId": st["downloadId"],
                   "state": {"pause": "paused", "resume": "in_progress",
                             "cancel": "cancelled"}[act]})

    # ---- "浏览器网络栈"：只有这里碰文件字节 ----
    def _fetch_resume(self, jid, st):
        """真取字节（扮演 Chrome 网络栈）。支持 Range 续传。

        故障由**服务端** abrupt 参数制造（服务器掉线），本夹具只负责检测
        "读到 EOF 但字节不够"，并如实上报 interrupted —— 这才是断网的样子。
        """
        u = re.match(r"^https?://([^/:]+)(?::(\d+))?(.*)$", st["url"])
        host, port, path = u.group(1), int(u.group(2) or 80), u.group(3)
        loopback_only(host, port)
        p = self.root / st["filename"]
        p.parent.mkdir(parents=True, exist_ok=True)
        have = p.stat().st_size if p.exists() else 0
        self._jlog({"ev": "open", "jid": jid, "have": have, "host": host, "port": port})
        headers = {}
        mode = "wb"
        if have:
            headers["Range"] = f"bytes={have}-"
            mode = "ab"
        try:
            c = http.client.HTTPConnection(host, port, timeout=10)
            c.request("GET", path, headers=headers)
            r = c.getresponse()
            if r.status >= 400:
                try:
                    r.read()
                except Exception:
                    pass
                c.close()
                self.send({"type": "progress", "jobId": jid, "downloadId": st["downloadId"],
                           "state": "interrupted", "bytesReceived": have,
                           "canResume": False, "error": f"HTTP_{r.status}"})
                with self.lock:
                    self.tasks.pop(jid, None)
                    self.final[jid] = {"downloadId": st["downloadId"], "state": "interrupted",
                                       "bytesReceived": have, "canResume": False,
                                       "error": f"HTTP_{r.status}"}
                return "error"
            resumable = r.status == 206
            if r.status == 200 and have:
                # 服务器不支持 Range → 只能重头下，如实标 restarted
                have = 0
                mode = "wb"
                st["restarted"] = True
            cl = r.getheader("Content-Length")
            total = (int(cl) + (have if resumable else 0)) if cl else st.get("total")
            st["total"] = total
            fh = open(p, mode)
        except Exception as e:
            self.send({"type": "progress", "jobId": jid, "downloadId": st["downloadId"],
                       "state": "interrupted", "bytesReceived": have, "canResume": True,
                       "error": f"FETCH_OPEN_{type(e).__name__}"})
            return "interrupted"

        got = have
        last_report = time.time()
        self._err = ""
        try:
            while not self.stop.is_set():
                with self.lock:
                    if st["cancelled"]:
                        break
                    paused = st["paused"]
                if paused:
                    self.send({"type": "progress", "jobId": jid, "downloadId": st["downloadId"],
                               "state": "paused", "bytesReceived": got, "totalBytes": total,
                               "canResume": True})
                    time.sleep(0.3)
                    continue
                chunk = r.read(32768)
                if not chunk:
                    break
                fh.write(chunk)
                fh.flush()
                got += len(chunk)
                st["bytes"] = got
                if self.args.die_at and got >= self.args.die_at:
                    fh.close(); c.close()
                    print("[fake_chrome] 模拟 Chrome 崩溃（V8′）", file=sys.stderr)
                    os._exit(9)
                if self.args.drop_at and got >= self.args.drop_at and not self._dropped:
                    # **一次性**掐链路，下载线程留着：等价 SW 被 Chrome 杀掉。
                    # 一次性是这里的关键：真 SW 只死一次，醒了就接回来。原来每个
                    # 32KB 分片都掐一次，链路被自己抖成"每秒重连"，测不出对账。
                    self._dropped = True
                    self._jlog({"ev": "drop_link", "jid": jid, "got": got})
                    cli = self.cli
                    self.cli = None                     # 先摘引用，send 从此静默丢弃
                    if cli:
                        try:
                            cli.close(1006, "sw killed")
                        except Exception:
                            pass
                    self.link_dropped_at = got
                if self.args.no_progress:
                    continue
                if time.time() - last_report >= 0.5:
                    last_report = time.time()
                    self.send({"type": "progress", "jobId": jid, "downloadId": st["downloadId"],
                               "state": "in_progress", "bytesReceived": got,
                               "totalBytes": total, "canResume": not st["restarted"],
                               "restarted": st["restarted"]})
        except (http.client.HTTPException, ConnectionError, OSError) as e:
            self._err = f"{type(e).__name__}:{e}"
        finally:
            try:
                fh.close()
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass

        with self.lock:
            if st["cancelled"]:
                self.send({"type": "progress", "jobId": jid, "downloadId": st["downloadId"],
                           "state": "cancelled", "bytesReceived": got, "totalBytes": total})
                self.tasks.pop(jid, None)
                self.final[jid] = {"downloadId": st["downloadId"], "state": "cancelled",
                                   "bytesReceived": got, "totalBytes": total}
                if p.exists():
                    try:
                        p.unlink()          # V7：取消后清掉半成品
                    except OSError:
                        pass
                return "cancelled"

        self._jlog({"ev": "finish", "jid": jid, "got": got, "total": total,
                    "status": r.status, "bytesOnDisk": p.stat().st_size if p.exists() else None})
        if total and got < total:
            # 连接提前结束 = 断网，不是完成（这里假装有 complete 就是造假）
            self.send({"type": "progress", "jobId": jid, "downloadId": st["downloadId"],
                       "state": "interrupted", "bytesReceived": got, "totalBytes": total,
                       "canResume": not st["restarted"], "restarted": st["restarted"],
                       "error": "CONNECTION_CLOSED_EARLY:" + (self._err or "eof")})
            with self.lock:
                self.tasks.pop(jid, None)
                self.final[jid] = {"downloadId": st["downloadId"], "state": "interrupted",
                                   "bytesReceived": got, "totalBytes": total,
                                   "canResume": not st["restarted"],
                                   "restarted": st["restarted"],
                                   "error": "CONNECTION_CLOSED_EARLY"}
            return "interrupted"

        complete = {"type": "progress", "jobId": jid, "downloadId": st["downloadId"],
                    "state": "complete", "bytesReceived": got,
                    "totalBytes": total or got, "restarted": st["restarted"]}
        self.send(complete)
        with self.lock:
            self.tasks.pop(jid, None)
            # 终态进台账：链路要是这会儿断了，重连后的 snapshot 还认得出这一单
            self.final[jid] = {k: v for k, v in complete.items() if k != "type"}
        return "complete"

    def _download(self, jid):
        with self.lock:
            st = self.tasks.get(jid)
        if not st:
            return
        try:
            self._fetch_resume(jid, st)
        except Exception as e:
            self.send({"type": "progress", "jobId": jid, "state": "interrupted",
                       "error": f"FAKE_CHROME_{type(e).__name__}:{e}", "canResume": False})
            with self.lock:
                self.tasks.pop(jid, None)

    def run(self) -> int:
        """循环重连：Bridge 重启/端口漂移后能自己接回来（等价扩展的 alarms + 退避重连）。"""
        first, announced = True, False
        while not self.stop.is_set():
            try:
                if self.connect():
                    if not announced:
                        print(json.dumps({"fake_chrome": {
                            "connected": True, "wsPort": self.link_port,
                            "bridgeId": (self.welcome or {}).get("bridgeId"),
                            "paired": self.key is not None,
                            "downloadRoot": str(self.root)}}))
                        announced = True
                    if self.args.send_blob:
                        # W17 反证：故意发一条超长纸条，服务端必须断链 1009
                        raw = json.dumps({"v": 2, "type": "progress", "jobId": "x" * 16,
                                          "blob": "y" * self.args.send_blob}).encode()
                        try:
                            with self._send_lock:
                                self.cli.send_raw(raw)
                        except Exception:
                            pass
                    first = False
                else:
                    time.sleep(0.4)
            except Exception as e:
                if first:
                    print(f"[fake_chrome] 首次连不上 bridge: {e}", file=sys.stderr)
                    return 1
                time.sleep(0.4)
            while not self.stop.is_set() and self.rx and self.rx.is_alive():
                time.sleep(0.2)
            if self.stop.is_set():
                break
            self.rx = None
        return 0

    def _jlog(self, rec) -> None:
        """流水账：这台"假浏览器"发了几次请求、各拿了多少字节。"""
        rec["ts"] = round(time.time(), 4)
        try:
            with open(self.flow_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="链路层假 Chrome（WS 版，仅测试）")
    p.add_argument("--state-dir", default=default_state_dir())
    p.add_argument("--link-port", type=int, default=None)
    p.add_argument("--download-root", default=None)
    p.add_argument("--key", default=None, help="已配对的 K（hex）；不给就自己 TOFU 一把")
    p.add_argument("--die-at", type=int, default=0)
    p.add_argument("--drop-at", type=int, default=0)
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--send-blob", type=int, default=0)
    a = p.parse_args(argv)
    if a.download_root is None:
        a.download_root = str(Path(a.state_dir) / "files")
    return FakeChrome(a).run()


if __name__ == "__main__":
    sys.exit(main())
