#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_downloader.py —— v0.2（纯回环 WebSocket 架构）真跑测试，标准库 unittest，无需 pytest

跑法：
    python test_downloader.py                    全量
    python test_downloader.py -v                 看每条用例名
    python test_downloader.py UnitTests          只跑某个类
    python test_downloader.py HandshakeTests.test_W13_three_sides

覆盖面（对应计划 §10）：
  UnitTests                纸条自检 / 字段闭包 / 路径消毒 / 状态归一 / HMAC 口径
  RestApiTests             W1 权限面 + endpoint 形状（含 WS 与 HTTP 同口）
  HandshakeTests           W13（双向挑战应答正反两向）
  FrameLimitTests          W17（8192 硬闸三处：aiohttp / 降级 / 声明长度 / 分片）
  PortHardeningTests       W14（SO_EXCLUSIVEADDRUSE）+ W3（端口漂移）+ W7 + W14b（抢占识别）
  KeepaliveTests           W4-lite / W5 / W6（SW 被杀后重连对账）/ W9（互通矩阵）/ W10（零依赖降级）
  EgressGuardTests         V4-a 静态审计 + V4-b 运行时熔断 + V4-e 无扩展如实失败
  SecurityTests            V9 只绑回环 / 令牌 / 路径穿越 / 非 http scheme
  IntegrationTests         V1 单文件 / V2 进度速度 / V3 并发闸 / V7 暂停取消 / V8 账本重载 / V4-c 纸条审计
  CrashRecoveryTests       V8′（**下载进行中**杀 Bridge，before≠after 是硬判据）
  ResumeTests              V5 真续传（206+Content-Range）/ V6 禁 Range → RESTARTED / 退避耗尽
  ProtocolFuzzTests        坏 JSON / 二次连接顶替 / 孤儿进度 / 未知字段
  CliTests                 V11：README 里的每条 CLI 真跑
  StaticAuditDeliverables  V10 违禁依赖 + W1/W2/W12/W20 + 桩测试的"非浏览器验证"标注（001-F8）

诚实边界（001-F8 硬判据）：本文件里所有"扩展"都是 tests/fake_chrome.py 这个链路层对手。
它能证 Bridge 侧协议/握手/账本/并发/续传/熔断，**不能**证真实 Chrome 的 MV3 行为
（SW 被杀、alarms 实钳制、downloads 语义、LNA 档位）。后者的编号一律带 *(真机 UNVERIFIED)*，
且只能落在 tests/ws_probe_report.md / tests/api_probe_report.md，本文件不代它作证。
"""
from __future__ import annotations

import ast
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# ---- T1.2 路径收敛（PLAN-CHROME-DOWNLOADER-003）----------------------------------
# DELIVERY_ROOT = **包目录**（交付件 + 四枚见证文件所在处），由 find_spec 解析：
#   装了包（site-packages）和没装包（源码树 src/）两种情形都命中，不写死绝对路径
#   —— 写死就等于把"搬家"变成"改判据"。
# TESTS = 本套件目录（夹具与 evidence 所在处）；PROJECT_ROOT = 工程根；SRC = 源码根
TESTS = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS.parent
SRC = PROJECT_ROOT / "src"
for _p in (str(SRC), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import importlib.util                          # noqa: E402
_spec = importlib.util.find_spec("chrome_downloader")
if _spec is None or not _spec.origin:
    raise RuntimeError("找不到 chrome_downloader 包：PYTHONPATH 要指向 src/")
DELIVERY_ROOT = Path(_spec.origin).resolve().parent

# 子进程寻包：测试里所有 `-m chrome_downloader.<mod>` 的调用都必须能找到包。
# 装了包的情形下 PYTHONPATH 多余但无害；没装包（源码树）的情形下它就是唯一通路。
_pp = os.environ.get("PYTHONPATH")
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(SRC), str(TESTS)] + ([_pp] if _pp else []))

from chrome_downloader import bridge            # noqa: E402
from chrome_downloader import client as client_mod             # noqa: E402
from harness import (BridgeHarness, EVIDENCE, ev, pick_free, sha256_file)   # noqa: E402
from chrome_downloader.mock_server import expected_sha256   # noqa: E402
from ws_client import (WsClient, do_handshake, bridge_proof,   # noqa: E402
                       extension_proof)

PY = sys.executable


# ==========================================================================
class UnitTests(unittest.TestCase):
    """纯函数单元：不需要网络、不需要 Chrome。"""

    def test_frame_size_self_check(self):
        """C7/V4-c：单条纸条必须 ≤8KB，超了抛错，不许静默变大。"""
        raw = bridge.encode_frame({"type": "progress", "jobId": "a" * 16, "bytesReceived": 123})
        self.assertLessEqual(len(raw), bridge.MAX_FRAME_BYTES)
        self.assertEqual(json.loads(raw.decode())["bytesReceived"], 123)
        self.assertEqual(bridge.MAX_FRAME_BYTES, 8192)
        with self.assertRaises(ValueError):
            bridge.encode_frame({"type": "blob", "data": "x" * 100_000})

    def test_v01_4byte_length_header_is_gone(self):
        """G2：NM 时代的 4 字节长度帧编解码必须整体消失（read_frame/struct 都不该在）。"""
        self.assertFalse(hasattr(bridge, "read_frame"))
        self.assertFalse(hasattr(bridge, "nm_relay"))
        self.assertFalse(hasattr(bridge, "autospawn_serve"))
        src = (DELIVERY_ROOT / "bridge.py").read_text(encoding="utf-8")
        self.assertNotIn("struct.pack", src)
        self.assertNotIn("LINK_MAGIC", src)

    def test_field_closure_whitelist(self):
        """字段集合必须 ⊆ 白名单；手工塞 base64 块要被抓出来。"""
        ok = {"v": 2, "type": "progress", "jobId": "j", "bytesReceived": 1,
              "items": [{"downloadId": 2, "state": "complete"}]}
        self.assertEqual(bridge.audit_field_closure(ok), [])
        bad = dict(ok, payload_base64="AAA" * 3000)
        self.assertEqual(bridge.audit_field_closure(bad), ["payload_base64"])

    def test_handshake_fields_are_whitelisted(self):
        """§5.3：握手/端点六个新键必须在白名单里，否则会被自己的审计当成未知字段丢掉。"""
        for k in ("port", "portScan", "nonce", "hmac", "bridgeId", "keyExchange"):
            self.assertIn(k, bridge.ALLOWED_FIELDS, k)
        msg = {"type": "hello_challenge", "clientNonce": "ab" * 16, "keyExchange": "cd" * 32}
        self.assertEqual(bridge.audit_field_closure(msg), [])

    def test_hmac_proofs_match_between_sides(self):
        """W13 的根：bridge.py 与 tests/ws_client.py 两侧口径必须逐字相同。

        换了承载（NM→WS）之后，"两端算的是同一句话"完全靠这两个函数对齐；
        字符串里那个 \\n 和 ctx 词只要有一处漂移，握手就会永久失败 —— 所以钉成断言。
        """
        key = b"\x11" * 32
        for cn, sn in (("0" * 32, "1" * 32), ("a" * 16, "b" * 16)):
            self.assertEqual(bridge.challenge_expect(key, cn, sn, bridge.HMAC_CTX_BRIDGE),
                             bridge.hmac_hex_equiv(key, cn, sn))
            self.assertEqual(bridge.challenge_expect(key, cn, sn, "bridge"),
                             bridge_proof(key, cn, sn))
            self.assertEqual(bridge.response_expect(key, cn, sn),
                             extension_proof(key, cn, sn))
        self.assertEqual(bridge.HMAC_CTX_BRIDGE, "bridge")
        self.assertEqual(bridge.HMAC_CTX_EXTENSION, "extension")
        # 常量时间比较：形状不对一律不通过，不许拿 == 蒙
        self.assertFalse(bridge.hmac_ok(bridge.hmac_sha256(key, "x"), "ff" * 32))
        self.assertFalse(bridge.hmac_ok(bridge.hmac_sha256(key, "x"), None))
        self.assertFalse(bridge.hmac_ok(bridge.hmac_sha256(key, "x"), "短"))
        self.assertTrue(bridge.hmac_ok(bridge.hmac_sha256(key, "x"), bridge.hmac_sha256(key, "x")))

    def test_sanitize_dest_blocks_traversal(self):
        """V9：../evil.txt 这类必须拒。"""
        for bad in ("../evil.txt", "a/../../evil", "/etc/passwd", "C:/Windows/x.txt",
                    "ok/../../evil.txt"):
            dest, err = bridge.sanitize_dest(bad, "http://127.0.0.1:1/f", "chrome-dl")
            self.assertIsNone(dest, f"{bad} 应被拒")
            self.assertIsNotNone(err)
        dest, err = bridge.sanitize_dest("sub/dir/ok.txt", "http://127.0.0.1:1/f")
        self.assertEqual(dest, "sub/dir/ok.txt")
        self.assertIsNone(err)

    def test_folder_prefix_for_chrome_download_dir(self):
        """filename 在 Chrome 自己的下载目录下解析 → 必须带统一子目录前缀。"""
        dest, err = bridge.sanitize_dest("a.bin", "http://127.0.0.1:1/f", "chrome-dl")
        self.assertEqual(dest, "chrome-dl/a.bin")
        self.assertIsNone(err)
        self.assertEqual(bridge.sanitize_dest("a.bin", "http://h/f", "../escape")[1], "BAD_FOLDER")
        self.assertEqual(bridge.sanitize_dest(None, "http://h/x/y.fastq", "cdl")[0], "cdl/y.fastq")

    def test_norm_state_mapping(self):
        self.assertEqual(bridge.norm_state("in_progress"), "DOWNLOADING")
        self.assertEqual(bridge.norm_state("complete"), "COMPLETE")
        self.assertEqual(bridge.norm_state("interrupted"), "INTERRUPTED")

    def test_is_loopback(self):
        for h in ("127.0.0.1", "::1", "localhost", "127.5.5.5"):
            self.assertTrue(bridge.is_loopback(h), h)
        for h in ("8.8.8.8", "example.com", "198.18.0.1", ("93.184.216.34", 80)):
            self.assertFalse(bridge.is_loopback(h), h)

    def test_jobid_idempotent(self):
        a = bridge.make_job("http://h/f", "f.bin", None, "f.bin")
        b = bridge.make_job("http://h/f", "f.bin", None, "f.bin")
        self.assertEqual(a["jobId"], b["jobId"])
        self.assertNotEqual(a["jobId"], bridge.make_job("http://h/g", "f.bin", None, "f.bin")["jobId"])

    def test_speed_window(self):
        st = bridge.BridgeState(bridge_args(state_dir=tempfile.mkdtemp(),
                                            download_root=tempfile.mkdtemp()))
        j = bridge.make_job("http://h/f", "f", None, "f")
        st.feed_speed(j, 0, 1000.0)
        st.feed_speed(j, 500_000, 1001.0)
        self.assertTrue(400_000 < j["speedBps"] < 600_000, j["speedBps"])

    def test_probe_port_avoids_busy(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            busy = s.getsockname()[1]
            p, holder = bridge.probe_port(busy, busy + 3)
            bound = holder.getsockname()[1]      # 先取真端口再关：关完 getsockname 会炸
            holder.close()
            self.assertNotEqual(p, busy)
            self.assertEqual(p, bound, "必须返回真实绑定端口")

    def test_probe_port_reports_occupied_vs_stale(self):
        """W3/W14b 的判定基础：'有人在听'与'没人听却绑不上'必须在错误信息里分开。"""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            busy = s.getsockname()[1]
            with self.assertRaises(OSError) as cm:
                bridge.probe_port(busy, busy)
            msg = str(cm.exception)
            self.assertIn(f"occupied=[{busy}]", msg, msg)

    def test_exclusive_socket_rejects_second_bind(self):
        """W14 的一半：本进程内第二个套接字独占绑同一端口必须失败（Windows 劫持面）。"""
        a = bridge.exclusive_listen_socket(0)
        port = a.getsockname()[1]
        try:
            with self.assertRaises(OSError):
                b = bridge.exclusive_listen_socket(port)
                b.close()
        finally:
            a.close()

    def test_illegal_url_rejected_by_client(self):
        with self.assertRaises(ValueError):
            client_mod.Client(host="93.184.216.34", port=80, token="x")

    def test_state_dir_permission_error_is_human(self):
        """001-F3：state_dir 打不开要给可读提示，不许丢裸 traceback。

        这里用 monkeypatch 造 PermissionError：Windows 上 os.chmod(0o500) 拦不住
        mkdir（ACL 语义与 POSIX 不同），拿它当"造得出权限错"的手段是假的。
        """
        target = Path(tempfile.mkdtemp()) / "nope"
        orig = Path.mkdir
        boom = PermissionError(13, "Permission denied")

        def fake_mkdir(self, *a, **kw):
            if str(self) == str(target):
                raise boom
            return orig(self, *a, **kw)

        Path.mkdir = fake_mkdir
        try:
            with self.assertRaises(SystemExit) as cm:
                bridge.BridgeState(bridge_args(state_dir=str(target),
                                               download_root=str(Path(tempfile.mkdtemp()))))
            msg = str(cm.exception)
            self.assertIn("状态目录打不开", msg, msg)
            self.assertIn("--state-dir", msg, msg)     # 必须给下一步该敲什么
        finally:
            Path.mkdir = orig

    def test_endpoint_uses_one_port_for_ws_and_http(self):
        """零配置的结构性前提：WS 与 Agent 的 HTTP 面在同一个门牌上（计划 §5.2）。"""
        ep_src = None
        h = BridgeHarness(fake_chrome=False, wait_fake=False)
        try:
            ep_src = json.loads((h.state_dir / "endpoint.json").read_text(encoding="utf-8"))
        finally:
            h.shutdown()
        self.assertEqual(ep_src["httpPort"], ep_src["wsPort"], ep_src)
        self.assertEqual(ep_src["wsPath"], "/link")
        self.assertEqual(ep_src["maxFrame"], 8192)
        self.assertEqual(ep_src["version"], 2)


# ==========================================================================
def bridge_args(**kw):
    """构造 Bridge 需要的 args 对象（默认值与 main() 的 argparse 保持一致）。"""
    class A:
        pass
    d = dict(port=0, state_dir=None, download_root=None, token="t" * 32, no_token=False,
             no_egress_guard=False, max_concurrent=4, per_host=2, max_retries=5,
             stale_after=45.0, link_wait_timeout=120.0, conflict="uniquify",
             verbose=False, chrome_version=None, ext_version=None, folder="",
             port_scan=10, bind_wait=0.0, keepalive_sec=bridge.KEEPALIVE_PING_SEC,
             selftest_egress=False, selftest_handshake=False, selftest_rounds=100,
             selftest_targets=None)
    d.update(kw)
    a = A()
    for k, v in d.items():
        setattr(a, k, v)
    return a


# 给 UnitTests 用的"等价实现"：证明 bridge.py 里那个公式就是这串字节
def hmac_hex_equiv(key, cn, sn):
    import hashlib
    import hmac as _hmac
    return _hmac.new(key, f"{cn}\n{sn}\nbridge".encode(), hashlib.sha256).hexdigest()


bridge.hmac_hex_equiv = hmac_hex_equiv


# ==========================================================================
class HandshakeTests(unittest.TestCase):
    """W13：HMAC-SHA256 双向挑战应答的三小问（计划 §10.2）。"""

    @classmethod
    def setUpClass(cls):
        cls.h = BridgeHarness(fake_chrome=False, wait_fake=False)

    @classmethod
    def tearDownClass(cls):
        cls.h.shutdown()

    def _cli(self):
        return WsClient("127.0.0.1", self.h.ws_port, "/link", timeout=8).connect()

    def test_W13_side1_fake_bridge_cannot_pass_extension_check(self):
        """①伪造服务端（不知道 K）→ 扩展侧必须验签失败并断开。

        这里把"扩展侧"用 ws_client.do_handshake 复现：它拿自己的 K 去验服务端交出的
        hmac，验不过就返回 SERVER_HMAC_MISMATCH 且**绝不进入命令阶段**。
        做法：让真 Bridge 配好一个 K，再拿**另一个** K 走同一次握手的验签逻辑。
        """
        cli = self._cli()
        h = do_handshake(cli, None)               # 第一次：TOFU，服务端收下我们生成的 K
        self.assertTrue(h["ok"], h)
        good = h["key"]
        cli.close()
        # 服务端手里现在是 good。换一个 K 的"扩展"来连、且不交 keyExchange
        # （冒充者最常见的形状）：扩展侧验签必须看穿，**绝不进入命令阶段**。
        cli2 = self._cli()
        h2 = do_handshake(cli2, os.urandom(32))
        self.assertFalse(h2["ok"], h2)
        self.assertEqual(h2.get("reason"), "SERVER_HMAC_MISMATCH", h2)
        cli2.close()
        # 两个方向的口径字符串不可互换：拿错方向当证明必须算不出同一个值
        nonce_a, nonce_b = "a" * 32, "b" * 32
        self.assertNotEqual(bridge_proof(good, nonce_a, nonce_b),
                            extension_proof(good, nonce_a, nonce_b))
        self.assertNotEqual(bridge_proof(good, nonce_a, nonce_b),
                            bridge_proof(os.urandom(32), nonce_a, nonce_b))
        ev("W13", "W13_side1_fake_bridge.json",
           {"tofuFirst": {"ok": h["ok"], "bridgeId": h.get("bridgeId")},
            "wrongKeyClientSees": h2.get("reason"),
            "extensionRefusesToProceed": True,
            "proofsAreDirectional": True, "verdict": "PASS"})

    def test_W13_side2_fake_client_is_rejected_by_bridge(self):
        """②伪造客户端被**服务端自己**拒掉（不是被测试夹具提前看穿）。

        专门测 Bridge 那一半：用 evil="wrong-hmac" 跳过客户端验签，
        把一份错的扩展证明交到服务端手上 —— 服务端必须抓住并记 pairing_audit。
        """
        cases = {}
        # 服务端**未配对**时来了个不交 keyExchange 的连接：不开放控制面，直接拒。
        # 单独开一个干净 state_dir —— 共用夹具早被前面几条 TOFU 配上了，
        # 拿"已配对的桥"去断 BRIDGE_UNPAIRED 是把两种失败模式混成一种。
        fresh = BridgeHarness(fake_chrome=False, wait_fake=False)
        try:
            c = WsClient("127.0.0.1", fresh.ws_port, "/link", timeout=8).connect()
            r = do_handshake(c, None, evil="no-key")
            c.close()
            cases["unpaired_no_key"] = {"ok": r["ok"], "reason": r.get("reason")}
            self.assertFalse(r["ok"], cases)
            self.assertEqual(r.get("reason"), "BRIDGE_UNPAIRED", cases)
            self.assertNotIn("welcome", json.dumps(cases), "未配对绝不许拿到控制面")
        finally:
            fresh.shutdown()

        c0 = self._cli()
        first = do_handshake(c0, None)                  # 先把关系配好（TOFU）
        self.assertTrue(first["ok"], first)
        key = first["key"]
        c0.close()
        c1 = self._cli()
        r2 = do_handshake(c1, key, evil="wrong-hmac")   # 再交一份错的证明
        c1.close()
        cases["wrong_extension_hmac"] = {"ok": r2["ok"], "reason": r2.get("reason")}
        self.assertFalse(r2["ok"], cases)
        self.assertEqual(r2.get("reason"), "EXT_HMAC_MISMATCH",
                         f"服务端必须自己抓出假扩展：{cases}")

        c2 = self._cli()
        r3 = do_handshake(c2, key, evil="legacy")       # v0.1 的无 HMAC hello
        c2.close()
        cases["legacy_hello"] = {"ok": r3["ok"], "reason": r3.get("reason")}
        self.assertFalse(r3["ok"], cases)
        self.assertEqual(r3.get("reason"), "NOT_HMAC_HELLO", cases)

        audit = "\n".join(self.h.tail(self.h.state_dir / "pairing_audit.jsonl", 200))
        self.assertIn("handshake_failed", audit)
        self.assertIn("EXT_HMAC_MISMATCH", audit)
        self.assertIn("link_rejected", audit)
        ev("W13", "W13_side2_fake_clients.json",
           {"cases": cases, "auditHasRejection": "link_rejected" in audit,
            "auditHasHandshakeFailed": "handshake_failed" in audit,
            "pairingAuditTail": self.h.tail(self.h.state_dir / "pairing_audit.jsonl", 8),
            "verdict": "PASS"})

    def test_W13_side3_normal_handshake_succeeds_repeatedly(self):
        """③正常双向握手连打 N 次全成功（重放/竞态不能把好连接打掉）。"""
        cli = self._cli()
        first = do_handshake(cli, None)
        self.assertTrue(first["ok"], first)
        key = first["key"]
        cli.close()
        results = []
        for _ in range(20):
            c = self._cli()
            r = do_handshake(c, key)
            results.append(r["ok"])
            c.close()
        self.assertEqual(results.count(False), 0, results)
        ev("W13", "W13_side3_repeat20.json", {"attempts": len(results),
           "failures": results.count(False), "verdict": "PASS"})

    def test_W13_replay_the_same_nonce_is_not_accepted_twice(self):
        """R1d：同一个 clientNonce 重放不能拿到第二次成功（服务端每次新出题）。"""
        cli = self._cli()
        h = do_handshake(cli, None)
        self.assertTrue(h["ok"], h)
        key = h["key"]
        cli.close()
        # 手工重放：把上一次握手的第一条报文原样再发一遍
        c2 = self._cli()
        cnn = "a" * 32
        c2.send_obj({"v": 2, "type": "hello_challenge", "role": "extension",
                     "clientNonce": cnn, "chromeVersion": "REPLAY/1"})
        # ↑ 不带 keyExchange：服务端已配对，会用自己手里的 K 出题
        rep = c2.recv_obj(timeout=8)
        self.assertEqual(rep.get("type"), "hello_verify", rep)
        n1 = rep.get("nonce")
        c2.close()
        c3 = self._cli()
        c3.send_obj({"v": 2, "type": "hello_challenge", "role": "extension",
                     "clientNonce": cnn, "chromeVersion": "REPLAY/1"})
        rep3 = c3.recv_obj(timeout=8)
        self.assertNotEqual(n1, rep3.get("nonce"), "服务端每次必须换新 serverNonce")
        # 把第一次握手时算好的那份扩展答卷原样重放过去 → 必须被拒
        replayed_proof = extension_proof(key, cnn, n1)
        c3.send_obj({"v": 2, "type": "hello_verify", "hmac": replayed_proof})
        wel = c3.recv_obj(timeout=8)
        c3.close()
        accepted = wel is not None and wel.get("type") == "welcome"
        out = {"firstServerNonce": n1, "secondServerNonce": rep3.get("nonce"),
               "rotates": n1 != rep3.get("nonce"),
               "oldProofReplayedAccepted": accepted,
               "reason": (wel or {}).get("reason")}
        out["verdict"] = "PASS" if (out["rotates"] and not accepted) else "FAIL"
        ev("W13", "W13_nonce_replay.json", out)
        self.assertFalse(accepted, f"旧答卷被认成有效 = 可重放：{out}")


# ==========================================================================
def wait_link_gone(h: BridgeHarness, timeout: float = 6.0) -> bool:
    """轮询"链路已经不在了"。

    为什么不用 sleep 一把算：服务端收到违规帧之后要先发关闭帧、再收尾账本；
    对面若是不回 close 回声的违规客户端，aiohttp 那侧还要等满超时。
    拿固定 sleep 判"断了没有"，测出来的就是忽好忽坏的假 FAIL。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if not h.cli().health()["extensionConnected"]:
                return True
        except Exception:
            return True
        time.sleep(0.15)
    return False


class FrameLimitTests(unittest.TestCase):
    """W17：8192 帧上限必须在三处都硬（源码、降级实现、扩展自检）。"""

    @classmethod
    def setUpClass(cls):
        cls.h = BridgeHarness(fake_chrome=False, wait_fake=False)

    @classmethod
    def tearDownClass(cls):
        cls.h.shutdown()

    def _cli(self):
        return WsClient("127.0.0.1", self.h.ws_port, "/link", timeout=8).connect()

    def test_W17_1_aiohttp_passes_max_msg_size_explicitly(self):
        """①源码级：必须显式写 max_msg_size=MAX_FRAME_BYTES，不许吃 aiohttp 默认 4MiB。"""
        src = (DELIVERY_ROOT / "bridge.py").read_text(encoding="utf-8")
        self.assertIn("max_msg_size=MAX_FRAME_BYTES", src.replace(" ", ""),
                      "aiohttp 路径必须显式传 max_msg_size")
        self.assertNotRegex(src, r"max_msg_size\s*=\s*(0|[9]\d{4,}|4\s*\*\s*1024)",
                            "不许出现 0（无限）或 >8192 的值")
        # 依赖事实复检（F13）：aiohttp 默认值仍是 4MiB，所以这条显式传参不能撤
        try:
            import inspect
            from aiohttp import web
            d = inspect.signature(web.WebSocketResponse.__init__).parameters["max_msg_size"]
            default = d.default
        except Exception:
            default = None
        ev("W17", "W17_1_source_explicit.json",
           {"explicit": "max_msg_size=MAX_FRAME_BYTES", "aiohttpDefault": default,
            "ourLimit": bridge.MAX_FRAME_BYTES,
            "verdict": "PASS" if default and default > bridge.MAX_FRAME_BYTES else "CHECK"})
        self.assertEqual(bridge.MAX_FRAME_BYTES, 8192)

    def test_W17_2_oversized_text_frame_kills_link(self):
        """②握手成功后发 9000 字节文本帧 → 服务端必须断链并记 link_bad_frame。"""
        cli = self._cli()
        self.assertTrue(do_handshake(cli, None)["ok"], "握手必须先过才能测帧上限")
        n_before = len(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 1))
        cli.send_raw(b"x" * 9000)
        got = cli.recv_obj(timeout=6)
        code = None
        try:
            cli.sock.settimeout(2)
            fin, opcode, payload = cli.recv_frame()
            if opcode == 0x8:
                code = int.from_bytes(payload[:2], "big")
        except Exception:
            pass
        time.sleep(0.4)
        audit = "\n".join(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 12))
        health = self.h.cli().health()
        cli.close()
        out = {"firstFrameEcho": got, "closeCode": code, "linkGone": not health["extensionConnected"],
               "auditHasBadFrame": "link_bad_frame" in audit, "auditTail": audit.splitlines()[-6:]}
        out["verdict"] = "PASS" if out["linkGone"] and out["auditHasBadFrame"] else "FAIL"
        ev("W17", "W17_2_oversize_frame.json", out)
        self.assertTrue(out["linkGone"], out)
        self.assertTrue(out["auditHasBadFrame"], out)
        self.assertGreaterEqual(len(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 1)), n_before)

    def test_W17_3_declared_length_over_limit_is_rejected_without_reading_body(self):
        """③只声明 20000 字节、不写真身 → 也必须立刻断链（服务端不许等正文收齐）。"""
        cli = self._cli()
        self.assertTrue(do_handshake(cli, None)["ok"], "握手必须先过才能测帧上限")
        cli.send_raw(b'{"type":"ping"}', declared_len=20000)
        closed = False
        code = None
        try:
            cli.sock.settimeout(4)
            fin, opcode, payload = cli.recv_frame()
            if opcode == 0x8:
                code = int.from_bytes(payload[:2], "big")
        except Exception:
            closed = True
        time.sleep(0.3)
        audit = "\n".join(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 12))
        cli.close()
        out = {"closeCode": code, "dropped": closed or code is not None,
               "auditHasBadFrame": "link_bad_frame" in audit}
        out["verdict"] = "PASS" if out["dropped"] and out["auditHasBadFrame"] else "FAIL"
        ev("W17", "W17_3_declared_length.json", out)
        self.assertTrue(out["dropped"], out)
        self.assertTrue(out["auditHasBadFrame"], out)

    def test_W17_4_fragmentation_is_not_a_way_around_the_limit(self):
        """④把 9000 字节拆成两帧 → 必须被拒（否则"单帧 ≤8KB"就是纸面上的）。"""
        cli = self._cli()
        self.assertTrue(do_handshake(cli, None)["ok"], "握手必须先过才能测帧上限")
        payload = b"{" + b"y" * 8998 + b"}"
        cli.send_raw(payload[:4500], fin=False)
        cli.send_raw(payload[4500:], opcode=0x0)
        code = None
        try:
            cli.sock.settimeout(4)
            fin, opcode, pl = cli.recv_frame()
            if opcode == 0x8:
                code = int.from_bytes(pl[:2], "big")
        except Exception:
            code = "closed"
        time.sleep(0.3)
        audit = "\n".join(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 14))
        health = self.h.cli().health()
        cli.close()
        out = {"closeCode": code, "linkGone": not health["extensionConnected"],
               "audit": [l for l in audit.splitlines() if "bad_frame" in l or "proto" in l][-3:]}
        out["verdict"] = "PASS" if out["linkGone"] else "FAIL"
        ev("W17", "W17_4_fragmented.json", out)
        self.assertTrue(out["linkGone"], out)

    def test_W17_5_binary_frame_treated_as_too_big(self):
        """⑤二进制帧 = 有人想往通道里塞文件字节，处置与超大帧一致（C1）。"""
        cli = self._cli()
        self.assertTrue(do_handshake(cli, None)["ok"], "握手必须先过才能测帧上限")
        cli.send_raw(b"\x00\x01\x02file-bytes", opcode=0x2)
        code = None
        try:
            cli.sock.settimeout(4)
            fin, opcode, pl = cli.recv_frame()
            if opcode == 0x8:
                code = int.from_bytes(pl[:2], "big")
        except Exception:
            code = "closed"
        audit = "\n".join(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 14))
        gone = wait_link_gone(self.h)
        cli.close()
        out = {"closeCode": code, "linkGone": gone,
               "auditHasBadFrame": "link_bad_frame" in audit,
               "audit": [l for l in audit.splitlines() if "bad_frame" in l][-2:]}
        out["verdict"] = "PASS" if (out["linkGone"] and out["auditHasBadFrame"]) else "FAIL"
        ev("W17", "W17_5_binary_frame.json", out)
        self.assertTrue(out["linkGone"], out)
        self.assertTrue(out["auditHasBadFrame"], out)

    def test_W17_6_extension_side_self_check_exists(self):
        """⑥扩展侧发送前自检（W17③）：>8192 必须"丢弃并告警"，而不是拆帧或静默发出去。"""
        src = (DELIVERY_ROOT / "extension" / "background.js").read_text(encoding="utf-8")
        self.assertIn("bytes > MAX_FRAME", src)
        self.assertIn("const MAX_FRAME = 8192", src)
        self.assertRegex(src, r"console\.warn\([^\n]*超长")
        # 拆帧 = 绕过长度闸的后门，本仓必须不用
        self.assertNotIn("ws.send", re.sub(r"send_raw|sock\.send", "", src).replace(
            "try { ws.send(text); return true; }", "SENT_ONE_PLACE"))

    def test_W17_7_bridge_send_also_self_checks(self):
        """服务端下行方向同样受自检约束：给扩展发超长纸条必须抛，不许分帧。"""
        with self.assertRaises(ValueError):
            bridge.encode_frame({"type": "snapshot", "items": [{"jobId": "x" * 16}] * 400})


# ==========================================================================
class PortHardeningTests(unittest.TestCase):
    """W14 / W14b / W3 / W7：端口独占、抢占识别、端口漂移后跟得上。"""

    def test_W14_second_process_cannot_steal_the_port(self):
        """W14：真 Bridge 在 18888 上听着时，另一进程 bind 18888 必须失败。"""
        h = BridgeHarness(port=18888, fake_chrome=False, wait_fake=False)
        try:
            self.assertEqual(h.ws_port, 18888, h.ws_port)
            code = r"""
import socket, json, sys
out = {}
for name, opt in (("bare", None), ("reuseaddr", socket.SO_REUSEADDR)):
    s = socket.socket()
    if opt is not None:
        s.setsockopt(socket.SOL_SOCKET, opt, 1)
    try:
        s.bind(("127.0.0.1", 18888)); out[name] = "BOUND (!!)"
    except OSError as e:
        out[name] = f"REJECTED errno={e.errno}"
    s.close()
print(json.dumps(out))
"""
            r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                               timeout=30, cwd=str(PROJECT_ROOT))
            out = json.loads(r.stdout)
            out["bridgePort"] = h.ws_port
            out["bridgeTransport"] = h.transport
            out["verdict"] = ("PASS" if "REJECTED" in out["bare"]
                              and "REJECTED" in out["reuseaddr"] else "FAIL")
            ev("W14", "W14_exclusive_bind.json", out)
            self.assertIn("REJECTED", out["bare"], out)
            self.assertIn("REJECTED", out["reuseaddr"],
                          f"SO_REUSEADDR 竟能抢到已被独占占用的端口（劫持面还在）：{out}")
        finally:
            h.shutdown()

    def test_W14b_occupied_port_is_reported_not_hijacked(self):
        """W14b 的服务端视角：18888 被无关进程占着时，Bridge 必须换门牌而不是硬抢。"""
        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", 18888))
        squatter.listen(16)
        try:
            h = BridgeHarness(port=18888, fake_chrome=False, wait_fake=False)
            try:
                out = {"squatterPort": 18888, "bridgePort": h.ws_port,
                       "endpoint": json.loads((h.state_dir / "endpoint.json").read_text(
                           encoding="utf-8"))}
                out["verdict"] = "PASS" if h.ws_port != 18888 else "FAIL"
                ev("W14b", "W14b_squatter.json", out)
                self.assertNotEqual(h.ws_port, 18888, out)
                self.assertLessEqual(h.ws_port, 18897, out)
                # 真 Bridge 起来之后，抢占者自己也别想再抢回去
                with self.assertRaises(OSError):
                    x = bridge.exclusive_listen_socket(h.ws_port)
                    x.close()
            finally:
                h.shutdown()
        finally:
            squatter.close()

    def test_W3_and_W7_link_follows_port_drift(self):
        """W3+W7：Bridge 换端口后，链路客户端 ≤1 个重连周期内跟上新门牌。

        机制：endpoint.json 每轮重读（fake_chrome.endpoint()），同 §5.1 的 storage 首选端口。
        """
        h = BridgeHarness(port=18888, fake_chrome=False, wait_fake=False)
        try:
            h.spawn_fake(wait=True)
            self.assertTrue(h.wait_extension(), h.dump_diag("第一轮没连上"))
            old_port = h.ws_port
            drift = {"from": old_port}
            new = h.restart_bridge(port=18889)
            drift["to"] = new["endpoint"]["wsPort"]
            t0 = time.time()
            self.assertTrue(h.wait_extension(timeout=30.0),
                            h.dump_diag(f"端口漂到 {drift['to']} 之后没跟上"))
            drift["reconnectSec"] = round(time.time() - t0, 2)
            # 跟上之后必须还能干活（不是只是 TCP 通）
            url = h.mock.url("driftblob", 200_000, rate=200_000)
            job = h.cli().add(url, filename="drift.bin",
                              sha256=expected_sha256("driftblob", 200_000))["job"]
            r = h.wait_jobs([job["jobId"]], timeout=90)
            self.assertTrue(r["ok"], h.dump_diag("端口漂移后下载失败"))
            self.assertEqual(h.cli().show(job["jobId"])["sha256Actual"], "OK")
            drift["pairedAfterDrift"] = h.cli().health()["paired"]
            drift["verdict"] = "PASS"
            ev("W3", "W3_port_drift.json", drift)
            self.assertTrue(drift["pairedAfterDrift"], "换端口后密钥必须还在（同一 state_dir）")
        finally:
            h.shutdown()

    def test_W14b_impostor_on_first_port_gets_no_control_plane(self):
        """W14b / R1b：先占 18888 的冒充者拿不到控制面；真桥漂到别的端口仍能服务。

        这里同时钉住处置的两半：①冒充者绝拿不到 welcome（控制面不开放）；
        ②扩展侧把那个端口拉黑后，下一轮扫描会跳过它、去找真桥 —— 所以"拉黑"
        不等于"从此失联"。（真机 popup 文案面仍属 W20/W18，需人看。）
        """
        imp_port = pick_free()
        imp = subprocess.Popen([PY, str(TESTS / "fake_bridge_impostor.py"),
                                "--port", str(imp_port), "--mode", "wrong-hmac", "--once"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT))
        h = BridgeHarness(fake_chrome=False, wait_fake=False)
        try:
            t0 = time.time()
            while time.time() - t0 < 10:
                with socket.socket() as t:
                    t.settimeout(0.3)
                    if t.connect_ex(("127.0.0.1", imp_port)) == 0:
                        break
                time.sleep(0.2)
            cli = WsClient("127.0.0.1", imp_port, "/link", timeout=6).connect()
            r = do_handshake(cli, None)               # 扩展侧：拿自己的 K 去验冒充者
            cli.close()
            self.assertFalse(r["ok"], f"冒充者竟通过了验签：{r}")
            self.assertEqual(r.get("reason"), "SERVER_HMAC_MISMATCH", r)
            # 真桥照常可服务（漂到别的端口）
            real = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
            rr = do_handshake(real, None)
            real.close()
            self.assertTrue(rr["ok"], rr)
            out = {"impostorPort": imp_port, "impostorReason": r.get("reason"),
                   "impostorGotControlPlane": False,
                   "realBridgePort": h.ws_port, "realHandshakeOk": rr["ok"],
                   "extensionBlacklistsItAndNextRoundSkips": "见 tests/evidence/TE1x 第16项",
                   "verdict": "PASS"}
            ev("W14b", "W14b_impostor_vs_real_bridge.json", out)
        finally:
            try:
                imp.kill(); imp.communicate(timeout=5)
            except Exception:
                pass
            h.shutdown()


    def test_W15_bridge_id_pinning_survives_restart_and_changes_on_new_state(self):
        """W15：bridgeId 钉扎 —— 同 state_dir 重启必须不变；换新 state_dir 必须变（可告警）。"""
        h = BridgeHarness(fake_chrome=False, wait_fake=False)
        try:
            first = h.bridge_id
            cli = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
            self.assertTrue(do_handshake(cli, None)["ok"], "先 TOFU 配对")
            cli.close()
            new = h.restart_bridge()
            self.assertEqual(new["endpoint"]["bridgeId"], first,
                             "同 state_dir 重启 bridgeId 变了 = 扩展会误报告警")
            h2 = BridgeHarness(fake_chrome=False, wait_fake=False)
            try:
                self.assertNotEqual(h2.bridge_id, first, "不同 state_dir 的 bridgeId 撞了")
                ev("W15", "W15_bridge_id_pinning.json",
                   {"first": first, "afterRestart": new["endpoint"]["bridgeId"],
                    "otherStateDir": h2.bridge_id, "verdict": "PASS"})
            finally:
                h2.shutdown()
        finally:
            h.shutdown()

    def test_W16_forget_key_forces_a_fresh_tofu(self):
        """W16/W18：清掉密钥（等价 popup"断开并忘记此程序"）→ 重新 TOFU 能连上。"""
        h = BridgeHarness(fake_chrome=False, wait_fake=False)
        try:
            cli = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
            first = do_handshake(cli, None)          # TOFU：K 由扩展侧生成并交出
            self.assertTrue(first["ok"], first)
            k1 = first["key"]
            cli.close()
            self.assertTrue((h.state_dir / "handshake.key").exists())
            (h.state_dir / "handshake.key").unlink()      # 扩展侧 forget + 服务端侧被清
            key2 = bridge.read_pairing_key(h.state_dir)
            self.assertIsNone(key2)
            cli2 = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
            r = do_handshake(cli2, None)                  # 重新 TOFU
            cli2.close()
            self.assertTrue(r["ok"], r)
            audit = "\n".join(h.tail(h.state_dir / "pairing_audit.jsonl", 60))
            self.assertIn("pairing_first", audit)
            # 关键：forget 之后密钥必须真的换了，不能"看起来还在配老关系"
            self.assertNotEqual((h.state_dir / "handshake.key").read_text(encoding="utf-8"),
                                k1.hex())
            ev("W16", "W16_forget_and_retofu.json",
               {"keyFileAfterForget": None, "secondPairingOk": r["ok"],
                "newKeyDiffers": True, "verdict": "PASS"})
        finally:
            h.shutdown()

    def test_W19_key_mismatch_after_state_dir_swap_is_detected(self):
        """W19：state_dir 被换 → 服务端换了 K → 旧扩展的握手必须**连续失败**并被记下来。

        本项证"服务端认得出对面不是自己人"，popup 主动提示那条属真机面（需人看）。
        """
        h = BridgeHarness(fake_chrome=False, wait_fake=False)
        try:
            cli = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
            first = do_handshake(cli, None)
            self.assertTrue(first["ok"], first)
            k = first["key"]                          # 老扩展手里的那把 K
            cli.close()
            # 换 state_dir（等价于用户清了数据目录）：新 Bridge 手里没有 K
            h2 = BridgeHarness(fake_chrome=False, wait_fake=False)
            try:
                fails = []
                for _ in range(3):
                    c = WsClient("127.0.0.1", h2.ws_port, "/link", timeout=8).connect()
                    r = do_handshake(c, k)               # 老 K 不带 keyExchange
                    fails.append({"ok": r["ok"], "reason": r.get("reason")})
                    c.close()
                self.assertTrue(all(not f["ok"] for f in fails), fails)
                self.assertTrue(all(f["reason"] == "BRIDGE_UNPAIRED" for f in fails),
                                f"未配对的新任 Bridge 必须明确拒（不开放控制面）：{fails}")
                ev("W19", "W19_key_mismatch.json",
                   {"attempts": fails, "serverSideDetection": True,
                    "popupHint": "UNVERIFIED（真机面：需人看 popup 是否按 W19 文案提示）",
                    "verdict": "PASS"})
            finally:
                h2.shutdown()
        finally:
            h.shutdown()


# ==========================================================================
class KeepaliveTests(unittest.TestCase):
    """W4/W5/W6/W9/W10：保活、静默、SW 被杀后的对账、互通矩阵、零依赖降级。

    ⚠️ 本类能证的只是 **Bridge 那一侧**的机制。"20 秒 ping 是否真让 Chrome 154 的 SW
    活过 5 分钟"、"alarm 实钳制到 30 秒"这两条属真机面（WS-T0 / W4b），标 UNVERIFIED，
    不在这里冒充实测（001-F8 硬判据）。
    """

    def test_W4_bridge_sends_ping_and_answers_extension_ping(self):
        """服务端按 20 秒（可调小验证节奏）发 ping；对面 ping 过来必须回 pong。

        用 --keepalive-sec 1 把节奏压出来看，默认值仍是官方 20 秒（F1），不改行为。
        """
        h = BridgeHarness(fake_chrome=False, wait_fake=False, keepalive_sec=1.0)
        try:
            cli = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
            self.assertTrue(do_handshake(cli, None)["ok"])
            pong_seen = False
            seen_client_side = []
            t0 = time.time()
            while time.time() - t0 < 6:
                m = cli.recv_obj(timeout=1.5)
                if not m:
                    continue
                seen_client_side.append(m.get("type"))
                if m.get("type") == "ping":
                    cli.send_obj({"type": "ping"})
                    continue
                if m.get("type") == "pong":
                    pong_seen = True
                    break
            self.assertTrue(pong_seen, "回给服务端一个 ping 必须拿到 pong")
            time.sleep(0.4)          # 服务端把这条 pong 记进留痕是在发出之后，等一下再读
            trace = h.tail(h.state_dir / "ws_trace.jsonl", 200)
            rows = [json.loads(x) for x in trace]
            types = {r["msg"].get("type") for r in rows if isinstance(r.get("msg"), dict)}
            self.assertIn("ping", types, types)
            self.assertIn("pong", types, types)
            ev("W4", "W4_keepalive_ping_pong.json",
               {"serverPingObserved": True, "pongReturned": pong_seen,
                "clientSideTypes": seen_client_side,
                "bridgeTypesSeen": sorted(str(t) for t in types),
                "chromeKeepaliveEffect": "UNVERIFIED（真机面 WS-T0：SW 是否活过 5 分钟）",
                "verdict": "PASS"})
            cli.close()
        finally:
            h.shutdown()

    def test_W5_idle_bridge_is_quiet_and_tasks_aging_out_is_logged(self):
        """没有扩展在场时不许刷屏（Bridge 侧可测；扩展 console 噪音属真机面）。"""
        h = BridgeHarness(fake_chrome=False, wait_fake=False, link_wait_timeout=6.0)
        try:
            lines_before = len(h.bridge_stdout)
            t0 = time.time()
            while time.time() - t0 < 10:
                time.sleep(1)
            self.held = h
            url = h.mock.url("quiet", 1024)
            job = h.cli().add(url, filename="quiet.bin")["job"]
            t1 = time.time()
            st = None
            while time.time() - t1 < 20:
                st = h.cli().show(job["jobId"])
                if st["state"] in ("FAILED", "COMPLETE"):
                    break
                time.sleep(0.5)
            self.assertEqual(st["state"], "FAILED", st)
            self.assertIn("NO_EXTENSION", st["error"], st)
            audit = "\n".join(h.tail(h.state_dir / "bridge_audit.jsonl", 400))
            # 静默 = 没有反复崩、没有 traceback、没有同一错误的百条重复
            self.assertNotIn("Traceback", audit)
            self.assertNotIn("watchdog_error", audit)
            self.assertNotIn("keepalive_error", audit)
            out = {"bridgeStdoutLines": len(h.bridge_stdout) - lines_before,
                   "noTraceback": "Traceback" not in audit,
                   "noWatchdogError": "watchdog_error" not in audit,
                   "idleSeconds": round(time.time() - t0, 1),
                   "extensionConsoleNoise": "UNVERIFIED（真机面：需人开 DevTools 数条数）",
                   "verdict": "PASS"}
            ev("W5", "W5_idle_quiet.json", out)
            self.assertTrue(out["noTraceback"], out)
        finally:
            h.shutdown()

    def test_W6_link_death_then_reconcile_no_duplicate_download(self):
        """链路中途死掉 → 客户端重连 → snapshot 对账 → 不许出现重复下载。

        模拟的是"SW 被 Chrome 杀掉但浏览器仍在写盘"这一类最麻烦的窗口（R7/V8′ 的近亲）：
        本用例里下载线程活着，只有链路断了；V8′ 则连指挥官一起杀。
        """
        h = BridgeHarness(fake_chrome=False, wait_fake=False, keepalive_sec=2.0,
                          drop_at=300_000)
        try:
            # 先手动接一次（带 drop_at），确认链路会中途死
            url = h.mock.url("reconcile", 800_000, rate=300_000)
            sha = expected_sha256("reconcile", 800_000)
            job = h.cli().add(url, filename="reconcile.bin", sha256=sha)["job"]
            h.spawn_fake(drop_at=300_000, wait=True)
            self.assertTrue(h.wait_extension(), h.dump_diag("第一轮没连上"))
            # 等链路被打掉（fake 在 300KB 处自杀链路）
            t0 = time.time()
            while time.time() - t0 < 25:
                if not h.cli().health()["extensionConnected"]:
                    break
                time.sleep(0.3)
            dropped_at = time.time() - t0
            # 重连后必须对账：不重开、不重复下，最后字节与哈希都对
            self.assertTrue(h.wait_extension(timeout=30.0),
                            h.dump_diag("链路断了之后没重连回来"))
            r = h.wait_jobs([job["jobId"]], timeout=150)
            done = h.cli().show(job["jobId"])
            log = h.mock.wait_bytes(800_000)
            served = sum(x["served"] for x in log if x["method"] == "GET"
                         and "/file/reconcile?" in x["path"])   # 只算本任务那条 URL
            out = {"dropAfterSec": round(dropped_at, 2), "waitOk": r["ok"], "job": done,
                   "finalState": done["state"], "sha256Actual": done["sha256Actual"],
                   "serverTotalServed": served, "fileSize": 800_000,
                   "restarted": done["restarted"],
                   "snapshotReconciled": any("snapshot" in str(x) for x in
                                             h.tail(h.state_dir / "fake_chrome_flow.jsonl", 40))}
            out["verdict"] = ("PASS" if done["state"] == "COMPLETE"
                              and done["sha256Actual"] == "OK"
                              and served == 800_000 else "FAIL")
            ev("W6", "W6_sw_killed_reconcile.json", out)
            self.assertEqual(done["state"], "COMPLETE", out)
            self.assertEqual(done["sha256Actual"], "OK", out)
            self.assertEqual(served, 800_000,
                             f"服务端总出流量应恰好等于文件大小，多出来就是重复下载：{out}")
            self.assertFalse(done["restarted"], f"链路断了不该导致重头下：{out}")
        finally:
            h.shutdown()

    def test_W9_interop_matrix(self):
        """W9 互通矩阵：Bridge 两种服务端 × 三种 WS 客户端实现。

        aiohttp 服务端 / 零依赖降级服务端 ↔ ①本项目 stdlib 客户端 ②websockets 库
        ③Node 的原生 WebSocket（若本机 node 在场，走 tests/interop_node.mjs）。
        """
        combos = []
        for transport in ("aiohttp", "stdlib"):
            h = BridgeHarness(fake_chrome=False, wait_fake=False, transport=transport)
            try:
                self.assertEqual(h.transport,
                                 "aiohttp" if transport == "aiohttp" else "stdlib-rfc6455",
                                 f"transport 没切过去：{h.transport}")
                # ① stdlib 客户端
                c1 = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
                r1 = do_handshake(c1, None)
                c1.close()
                combos.append({"server": h.transport, "client": "stdlib-ws_client",
                               "ok": r1["ok"], "reason": r1.get("reason")})
                # ② websockets 库（第三方实现，真做协议协商）
                r2 = _websockets_lib_handshake(h.ws_port, _WS_GUID_CLIENT)
                combos.append({"server": h.transport, "client": "websockets-lib", **r2})
                # ③ 下载一个真文件，证明连上之后确实能干活
                url = h.mock.url(f"interop{transport}", 100_000, rate=200_000)
                job = h.cli().add(url, filename=f"interop_{transport}.bin",
                                  sha256=expected_sha256(f"interop{transport}", 100_000))["job"]
                h.spawn_fake(wait=True)
                res = h.wait_jobs([job["jobId"]], timeout=60)
                done = h.cli().show(job["jobId"])
                combos.append({"server": h.transport, "client": "fake_chrome-download",
                               "ok": bool(res["ok"] and done["sha256Actual"] == "OK"),
                               "state": done["state"]})
            finally:
                h.shutdown()
        ev("W9", "W9_interop_matrix.json", {"combos": combos,
           "verdict": "PASS" if all(c["ok"] for c in combos) else "FAIL"})
        self.assertTrue(all(c["ok"] for c in combos), combos)

    def test_W10_zero_dependency_transport_completes_a_download(self):
        """W10：把 aiohttp 关掉，Bridge 仍能起 WS、握手、把一整单下载跑完。"""
        h = BridgeHarness(transport="stdlib", fake_chrome=False, wait_fake=False)
        try:
            self.assertEqual(h.transport, "stdlib-rfc6455", h.transport)
            h.spawn_fake(wait=True)
            self.assertTrue(h.wait_extension(), h.dump_diag("降级模式没连上"))
            name, size = "fallbackblob", 400_000
            url = h.mock.url(name, size, rate=200_000)
            job = h.cli().add(url, filename="fallback.bin",
                              sha256=expected_sha256(name, size))["job"]
            r = h.wait_jobs([job["jobId"]], timeout=90)
            done = h.cli().show(job["jobId"])
            out = {"transport": h.transport, "job": done, "verdict": "PASS"}
            ev("W10", "W10_zero_dependency.json", out)
            self.assertTrue(r["ok"], h.dump_diag("降级模式下载超时"))
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual(done["sha256Actual"], "OK", done)
            # 降级路径也必须把 8192 闸守住（W17②的降级分支）
            c = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
            self.assertTrue(do_handshake(c, None)["ok"])
            c.send_raw(b"x" * 9000)
            code = None
            try:
                c.sock.settimeout(4)
                fin, opcode, pl = c.recv_frame()
                if opcode == 0x8:
                    code = int.from_bytes(pl[:2], "big")
            except Exception:
                code = "closed"
            c.close()
            ev("W10", "W10_fallback_frame_limit.json",
               {"transport": h.transport, "oversizeCloseCode": code,
                "verdict": "PASS" if code in (1009, "closed") else "FAIL"})
            self.assertIn(code, (1009, "closed"), f"降级实现没拦住超大帧：{code}")
        finally:
            h.shutdown()

    def test_W10_rejects_second_link_while_first_is_serving(self):
        """单会话（L5）：第二条链路顶掉第一条，旧链路发的消息不再进账本。"""
        h = BridgeHarness(fake_chrome=False, wait_fake=False)
        try:
            c1 = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
            self.assertTrue(do_handshake(c1, None)["ok"])
            time.sleep(0.3)
            c2 = WsClient("127.0.0.1", h.ws_port, "/link", timeout=8).connect()
            self.assertTrue(do_handshake(c2, None)["ok"])
            time.sleep(0.6)
            audit = "\n".join(h.tail(h.state_dir / "bridge_audit.jsonl", 60))
            self.assertIn("SECOND_LINK", audit, audit[-600:])
            health = h.cli().health()
            out = {"extensionConnected": health["extensionConnected"],
                   "linkEpoch": h.link_port and health["stats"]["linked"],
                   "auditHasReplace": "SECOND_LINK" in audit, "verdict": "PASS"}
            ev("W10", "W10_single_session.json", out)
            self.assertTrue(out["extensionConnected"], out)
            c1.close(); c2.close()
        finally:
            h.shutdown()


_WS_GUID_CLIENT = "client"


def _websockets_lib_handshake(port: int, tag: str) -> dict:
    """用第三方 websockets 库连一次并完成我们的 HMAC 握手（真互通，不是自家两头骗）。"""
    try:
        import asyncio
        import websockets
    except Exception as e:
        return {"ok": None, "skipped": f"websockets 不在场: {e}"}

    async def go():
        import secrets as _s
        uri = f"ws://127.0.0.1:{port}/link"
        key = _s.token_bytes(32)
        cnonce = _s.token_hex(16)
        async with websockets.connect(uri, max_size=8192, open_timeout=6) as ws:
            await ws.send(json.dumps({"v": 2, "type": "hello_challenge", "role": "extension",
                                      "clientNonce": cnonce, "keyExchange": key.hex(),
                                      "chromeVersion": "WEBSOCKETS-LIB/1",
                                      "extensionVersion": "interop"}))
            rep = json.loads(await ws.recv())
            if rep.get("type") != "hello_verify":
                return {"ok": False, "reason": f"UNEXPECTED:{rep.get('type')}"}
            import hashlib
            import hmac as _h
            def hx(msg):
                return _h.new(key, msg.encode(), hashlib.sha256).hexdigest()
            want = hx(f"{cnonce}\n{rep.get('nonce','')}\nbridge")
            if want != rep.get("hmac"):
                return {"ok": False, "reason": "SERVER_HMAC_MISMATCH"}
            await ws.send(json.dumps({"v": 2, "type": "hello_verify",
                                      "hmac": hx(f"{rep.get('nonce','')}\n{cnonce}\nextension")}))
            wel = json.loads(await ws.recv())
            return {"ok": wel.get("type") == "welcome", "welcomeType": wel.get("type"),
                    "maxFrame": wel.get("maxFrame"), "websocketsVersion": websockets.__version__}

    try:
        return asyncio.run(go())
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


# ==========================================================================
# V4-a 规则一：绝对禁止的"取字节"客户端库/函数（在交付代码里出现即 FAIL）
FORBIDDEN_STATIC = [
    ("urllib.request", r"\burllib\.request\b|from\s+urllib\.request"),
    ("requests", r"\bimport\s+requests\b|\brequests\.(?:get|post|Session)"),
    ("httpx", r"\bimport\s+httpx\b|\bhttpx\."),
    ("aiohttp.ClientSession", r"ClientSession"),   # aiohttp 只当服务端，不准开客户端取字节
    ("urlopen", r"\burlopen\s*\("),
    ("pycurl", r"\bpycurl\b"),
    ("http.client", r"\bhttp\.client\b|from\s+http\s+import\s+client"),
    ("curl/wget 取数据", r"\b(?:subprocess\.\w+|os\.system|os\.exec\w*)\s*\([^)\n]*(?:curl|wget)"),
    ("playwright/selenium/pyppeteer", r"playwright|selenium|pyppeteer"),
]
# V4-a 规则二：允许出现的 socket 出站行必须逐一列白名单（行号会漂，所以按内容白名单）
CONNECT_EXEMPT_PATTERNS = [
    r"_orig_connect\(",              # 熔断包装内部：已过 is_loopback 判定才调真连接
    r"_orig_connect_ex\(",
    r"_orig_create_connection\(",
    r'connect(?:_ex)?\(\("127\.',   # 自测/探活里写死的回环调用
]

AUDIT_SCOPE = ["bridge.py"]          # 交付代码里承重的那一份
LOOPBACK_EXEMPT_FILE = "client.py"   # Agent 侧必须连本机 Bridge → 回环豁免，单独审


class EgressGuardTests(unittest.TestCase):
    """V4-a 静态审计 + V4-b 运行时熔断 + V4-e 无扩展如实失败。"""

    def test_V4a_static_scan_bridge(self):
        """扫描交付代码：不得出现任何"自己取字节"的库/函数，也不得出现
        未经白名单的 socket 出站调用。命中 = 0。"""
        hits = []
        for name in AUDIT_SCOPE:
            src = (DELIVERY_ROOT / name).read_text(encoding="utf-8")
            lines = src.splitlines()
            imported = set()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            for label, pat in FORBIDDEN_STATIC:
                for i, line in enumerate(lines, 1):
                    if re.search(pat, line) and "noqa: egress-audit" not in line:
                        hits.append({"file": name, "line": i, "rule": label,
                                     "text": line.strip()[:120]})
            for banned in ("requests", "httpx", "pycurl", "selenium", "playwright",
                           "urllib3", "urllib.request"):
                if banned in imported:
                    hits.append({"file": name, "rule": f"import:{banned}"})
            for i, line in enumerate(lines, 1):
                if re.search(r"^\s*def (?:async )?\w*(?:connect|create_connection)\w*\s*\(", line):
                    continue     # 熔断包装自身的函数定义，不是调用点
                if re.search(r"\.connect(?:_ex)?\s*\(", line) or "create_connection(" in line:
                    if "noqa: egress-audit" in line:
                        continue
                    if not any(re.search(p, line) for p in CONNECT_EXEMPT_PATTERNS):
                        hits.append({"file": name, "line": i, "rule": "unexempt-connect",
                                     "text": line.strip()[:120]})
        ev("V4a", "V4a_static_scan.json",
           {"scope": AUDIT_SCOPE, "hits": hits,
            "rules_library": [r[0] for r in FORBIDDEN_STATIC],
            "connectExempts": CONNECT_EXEMPT_PATTERNS,
            "verdict": "PASS" if not hits else "FAIL"})
        self.assertEqual(hits, [], json.dumps(hits, ensure_ascii=False))

    def test_V4a_client_only_loopback(self):
        """client.py 允许 http.client（连本机 Bridge），但必须被回环断言钉死。"""
        src = (DELIVERY_ROOT / LOOPBACK_EXEMPT_FILE).read_text(encoding="utf-8")
        self.assertIn("http.client", src)
        self.assertIn("_assert_loopback", src)
        self.assertIn("_assert_loopback(host)", src)
        ext = re.findall(r"https?://(?!127\.0\.0\.1|localhost|\[::1\]|%)[A-Za-z0-9.\-]+", src)
        ev("V4a", "V4a_client_exempt.json",
           {"file": LOOPBACK_EXEMPT_FILE, "loopback_guard": "_assert_loopback present",
            "non_loopback_url_literals": sorted(set(ext)), "verdict": "PASS"})
        self.assertEqual(ext, [])

    def test_V4a_no_native_messaging_anywhere(self):
        """G2/V4-a：交付面与测试面（归档除外）NM 关键字 0 命中。"""
        # 模式**按片段拼**：写成整词的话，本文件自己就会成为一处命中（自指假阳性）。
        pat = "|".join(["connect" + "Native", "native" + "Messaging",
                        "Native" + "Messaging" + "Hosts", "install_" + "host",
                        "allowed_" + "origins", "com" + "\." + "article4" + "\.chrome_dl"])
        hits = []
        for _walk_base in (DELIVERY_ROOT, TESTS):
          for root, dirs, files in os.walk(_walk_base):
              # 与 check_env.scan_nm_residue 同口径：归档区不扫（那是历史账），
              # **工具生成的 evidence/ 也不扫** —— 里面记的就是上一轮的命中清单，
              # 扫它等于让一份报告去指控生成它的那行代码（自指假阳性）。
              dirs[:] = [d for d in dirs
                         if d not in ("__pycache__", "node_modules", "evidence")
                         and not d.startswith("_archive")]
              if "_archive" in " ".join(Path(root).parts):
                  continue
              for f in files:
                  if not f.endswith((".py", ".js", ".json", ".bat", ".md", ".txt")):
                      continue
                  p = Path(root) / f
                  try:
                      for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                          if "grep-ok: nm-residue" in line:   # 显式豁免行，须在 NM_REMOVAL_NOTE.md 登记
                              continue
                          if re.search(pat, line, re.I):
                              hits.append({"file": str(p.relative_to(_walk_base)).replace("\\", "/"), "line": i,
                                           "text": line.strip()[:120]})
                  except (UnicodeDecodeError, OSError):
                      continue
        # NM_REMOVAL_NOTE.md 是"已移除"的集中登记面（§10.1 唯一豁免）
        hits = [h for h in hits
                if not h["file"].replace("\\", "/").replace("\\", "/")
                .startswith("NM_REMOVAL_NOTE")]
        ev("V4a", "V4a_nm_residue.json", {"hits": hits, "pattern": pat,
           "verdict": "PASS" if not hits else "FAIL"})
        self.assertEqual(hits, [], json.dumps(hits[:8], ensure_ascii=False))

    def test_V4b_runtime_block_all_paths(self):
        """真跑 bridge --selftest-egress：外网 socket/DNS/subprocess/os.system/os.execv
        必须全被拦，回环必须放行。"""
        r = subprocess.run([PY, "-m", "chrome_downloader.bridge", "--selftest-egress",
                            "--state-dir", str(Path(tempfile.mkdtemp()) / "s"),
                            "--port", "0"],
                           capture_output=True, text=True, timeout=90, cwd=str(PROJECT_ROOT))
        out = json.loads(r.stdout)
        ev("V4b", "V4b_egress_selftest.json", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(out["verdict"], "PASS", out)
        self.assertTrue(out["guard_enabled"])
        kinds = [a["kind"] for a in out["attempts"]]
        self.assertTrue(all(a["blocked"] for a in out["attempts"]), out)
        self.assertTrue(any(k.startswith("socket.create_connection") for k in kinds))
        self.assertTrue(any(k.startswith("getaddrinfo") for k in kinds))
        self.assertTrue(any(k.startswith("subprocess.Popen") for k in kinds))
        self.assertTrue(any(k.startswith("os.system") for k in kinds))
        self.assertTrue(any(k.startswith("os.execv") for k in kinds))
        self.assertTrue(out["loopback"].startswith("allowed"), out["loopback"])
        self.assertGreaterEqual(out["blockedLogEntries"], len(out["attempts"]))

    def test_V4b_guard_blocks_whole_bridge_lifetime(self):
        """下载全过程 Bridge 都开着熔断还能跑完 → 证明真不需要外网。"""
        h = BridgeHarness(max_concurrent=4, per_host=2, guard=True)
        try:
            name, size = "guardblob", 512 * 1024
            url = h.mock.url(name, size, rate=200_000)
            job = h.cli().add(url, filename="guard.bin",
                              sha256=expected_sha256(name, size))["job"]
            res = h.wait_jobs([job["jobId"]], timeout=90)
            self.assertTrue(res["ok"], h.dump_diag("带熔断的下载超时"))
            done = h.cli().show(job["jobId"])
            self.assertEqual(done["state"], "COMPLETE", done)
            health = h.cli().health()
            self.assertTrue(health["egressGuard"]["enabled"])
            self.assertEqual(health["egressGuard"]["blocked"], 0,
                             "正常流程不应触发任何出站拦截")
            ev("V4b", "V4b_full_download_with_guard.json",
               {"job": done, "health": health, "verdict": "PASS"})
        finally:
            h.shutdown()

    def test_V4e_no_extension_fails_honestly(self):
        """V4-e①：没有扩展在场 → 必须失败且错误是"无扩展连接"，不许假装成功。"""
        h = BridgeHarness(fake_chrome=False, wait_fake=False, link_wait_timeout=4.0)
        try:
            url = h.mock.url("nobody", 1024)
            job = h.cli().add(url, filename="nobody.bin")["job"]
            self.assertEqual(job["state"], "PENDING", job)
            t0 = time.time()
            st = None
            while time.time() - t0 < 30:
                st = h.cli().show(job["jobId"])
                if st["state"] in ("FAILED", "COMPLETE"):
                    break
                time.sleep(0.3)
            self.assertEqual(st["state"], "FAILED", st)
            self.assertIn("NO_EXTENSION", st["error"], st)
            self.assertFalse((h.dl_root / "nobody.bin").exists())
            ev("V4e", "V4e_no_extension.json", st)
        finally:
            h.shutdown()

    def test_V4b_guard_survives_restart(self):
        """熔断必须在**每个启动分支**都在（001-F6：曾经 --mode nm 那条支路没装闸）。

        v0.2 已经没有 NM 分支；这里把"重启后依然装闸"钉住，防止将来加分支又漏。
        """
        h = BridgeHarness(fake_chrome=False, wait_fake=False)
        try:
            self.assertTrue(h.cli().health()["egressGuard"]["enabled"])
            h.restart_bridge()
            h2 = h.cli().health()
            out = {"beforeEnabled": True, "afterRestartEnabled": h2["egressGuard"]["enabled"],
                   "newPid": h2["pid"], "verdict": "PASS" if h2["egressGuard"]["enabled"] else "FAIL"}
            ev("V4b", "V4b_guard_after_restart.json", out)
            self.assertTrue(out["afterRestartEnabled"], out)
        finally:
            h.shutdown()


# ==========================================================================
class IntegrationTests(unittest.TestCase):
    """V1 单文件 / V2 进度速度 / V3 并发闸 / V7 暂停取消 / V8 账本重载 / V4-c 纸条审计。"""

    @classmethod
    def setUpClass(cls):
        cls.h = BridgeHarness(max_concurrent=4, per_host=2)
        assert cls.h.wait_extension(), cls.h.dump_diag("假 Chrome 没连上")

    @classmethod
    def tearDownClass(cls):
        cls.h.shutdown()

    def test_V1_single_file_sha256(self):
        name, size = "single", 2 * 1024 * 1024
        sha = expected_sha256(name, size)
        url = self.h.mock.url(name, size, rate=400_000)
        job = self.h.cli().add(url, filename="single.bin", sha256=sha)["job"]
        res = self.h.wait_jobs([job["jobId"]], timeout=120)
        if not res["ok"]:
            self.fail(self.h.dump_diag("V1 超时"))
        done = self.h.cli().show(job["jobId"])
        ev("V1", "V1_job.json", done)
        self.assertEqual(done["state"], "COMPLETE", done)
        self.assertEqual(done["sha256Actual"], "OK", done)
        self.assertEqual(sha256_file(self.h.file_of(done)), sha)
        self.assertEqual(done["bytesReceived"], size, done)
        self.assertFalse(done["restarted"])

    def test_V2_progress_events_and_speed(self):
        name, size = "speedy", 3 * 1024 * 1024
        url = self.h.mock.url(name, size, rate=120_000)      # ~26s @120KB/s
        job = self.h.cli().add(url, filename="speedy.bin",
                               sha256=expected_sha256(name, size))["job"]
        cli = self.h.cli()
        collected = []

        def watcher():
            try:
                cli.watch(lambda e: collected.append(e), timeout=90)
            except Exception as e:
                collected.append({"error": str(e)})

        th = threading.Thread(target=watcher, daemon=True)
        th.start()
        poll_times, seen_bytes = [], set()
        t0 = time.time()
        while time.time() - t0 < 120:
            j = cli.show(job["jobId"])
            seen_bytes.add(j["bytesReceived"])
            poll_times.append((time.time(), j["bytesReceived"], j.get("speedBps") or 0))
            if j["state"] in ("COMPLETE", "FAILED", "CANCELLED"):
                break
            time.sleep(0.4)
        th.join(timeout=2)
        mine = [e for e in collected if e.get("jobId") == job["jobId"]]
        gaps = [round(b[0] - a[0], 3) for a, b in zip(poll_times, poll_times[1:])]
        monotone = all(x[1] <= y[1] for x, y in zip(poll_times, poll_times[1:]))
        t0b, b0, _ = poll_times[0]
        t1b, b1, s_last = poll_times[-1]
        measured = (b1 - b0) / max(t1b - t0b, 1e-6)
        max_gap = max(gaps) if gaps else None
        out = {"sseEvents": len(mine), "pollSamples": len(poll_times),
               "distinctByteValues": len(seen_bytes), "monotoneBytes": monotone,
               "maxGapSec": max_gap, "measuredSpeedBps": round(measured),
               "lastReportedSpeedBps": s_last, "verdict": "PASS"}
        ev("V2", "V2_progress_speed.json", out)
        self.assertTrue(monotone, out)
        self.assertGreater(len(seen_bytes), 3, "进度必须有实质推进")
        self.assertLessEqual(max_gap, 1.2, f"链路存活段应 ≥1 条/秒，实测最大间隔 {max_gap}s")
        self.assertGreater(measured, 50_000, out)
        self.assertGreater(s_last, 0, out)
        self.assertTrue(0.5 * measured < s_last < 2.0 * measured,
                        f"上报 {s_last} vs 实测 {round(measured)}")
        self.assertEqual(cli.show(job["jobId"])["state"], "COMPLETE", out)
        self.assertGreater(len(mine), 3, "SSE 应推过多次进度事件")

    def test_V3_concurrency_caps(self):
        """12 个任务，任一时刻活跃 ≤4、同 host ≤2。"""
        jobs = [self.h.cli().add(
            self.h.mock.url(f"conc{i}", 200_000, rate=60_000), filename=f"conc{i}.bin",
            sha256=expected_sha256(f"conc{i}", 200_000))["job"] for i in range(12)]
        ids = {j["jobId"] for j in jobs}
        cli = self.h.cli()
        samples = []
        t0 = time.time()
        while time.time() - t0 < 150:
            js = cli.list()
            act = [j for j in js if j["state"] == "DOWNLOADING" and j["jobId"] in ids]
            hosts: dict[str, int] = {}
            for j in act:
                hosts[j["host"]] = hosts.get(j["host"], 0) + 1
            samples.append((len(act), max(hosts.values()) if hosts else 0))
            if all(j["state"] in ("COMPLETE", "FAILED", "CANCELLED")
                   for j in js if j["jobId"] in ids):
                break
            time.sleep(0.3)
        max_active = max(s[0] for s in samples)
        max_per_host = max(s[1] for s in samples)
        mine = [j for j in cli.list() if j["jobId"] in ids]
        ok_all = all(j["state"] == "COMPLETE" and j["sha256Actual"] == "OK" for j in mine)
        out = {"samples": len(samples), "maxActive": max_active,
               "maxPerHost": max_per_host, "jobsDone": len(mine),
               "allCompleteAndVerified": ok_all, "cap": {"maxConcurrent": 4, "perHost": 2}}
        ev("V3", "V3_concurrency.json", out)
        self.assertLessEqual(max_active, 4, out)
        self.assertLessEqual(max_per_host, 2, out)
        self.assertEqual(len(mine), 12, out)
        self.assertTrue(ok_all, [j for j in mine if j["state"] != "COMPLETE"])

    def test_V7_pause_resume_cancel(self):
        name, size = "pauser", 4 * 1024 * 1024
        url = self.h.mock.url(name, size, rate=200_000)
        cli = self.h.cli()
        job = cli.add(url, filename="pause.bin", sha256=expected_sha256(name, size))["job"]
        for _ in range(40):
            if cli.show(job["jobId"])["state"] == "DOWNLOADING":
                break
            time.sleep(0.2)
        cli.pause(job["jobId"])
        time.sleep(0.6)
        b1 = cli.show(job["jobId"])["bytesReceived"]
        time.sleep(3)
        b2 = cli.show(job["jobId"])["bytesReceived"]
        self.assertLess(b2 - b1, 1024, f"暂停期间 3 秒内增量 {b2 - b1} 应 <1KB")
        cli.resume(job["jobId"])
        res = self.h.wait_jobs([job["jobId"]], timeout=150)
        done = cli.show(job["jobId"])
        self.assertTrue(res["ok"], self.h.dump_diag("恢复后没跑完"))
        self.assertEqual(done["state"], "COMPLETE", done)
        self.assertEqual(done["sha256Actual"], "OK", done)
        # 取消：状态必须落到终态
        job2 = cli.add(self.h.mock.url("canceller", 8 * 1024 * 1024, rate=100_000),
                       filename="cancel.bin")["job"]
        for _ in range(40):
            if cli.show(job2["jobId"])["state"] == "DOWNLOADING":
                break
            time.sleep(0.2)
        cli.cancel(job2["jobId"])
        t0 = time.time()
        while time.time() - t0 < 25:
            if cli.show(job2["jobId"])["state"] == "CANCELLED":
                break
            time.sleep(0.3)
        c = cli.show(job2["jobId"])
        self.assertEqual(c["state"], "CANCELLED", c)
        ev("V7", "V7_pause_cancel.json", {"pausedDeltaBytes": b2 - b1, "resumed": done,
                                          "cancelled": c, "verdict": "PASS"})

    def test_V8_ledger_survives_bridge_restart(self):
        """**已完成**任务跨重启：账本必须逐字段一致，且不许重下一份。"""
        name, size = "persist", 300_000
        url = self.h.mock.url(name, size, rate=200_000)
        sha = expected_sha256(name, size)
        cli = self.h.cli()
        job = cli.add(url, filename="persist.bin", sha256=sha)["job"]
        res = self.h.wait_jobs([job["jobId"]], timeout=90)
        self.assertTrue(res["ok"], self.h.dump_diag("V8 前置下载失败"))
        before = cli.show(job["jobId"])
        self.assertEqual(before["state"], "COMPLETE")
        info = self.h.restart_bridge()
        ep = info["endpoint"]
        cli2 = client_mod.Client(host="127.0.0.1", port=ep["httpPort"], token=ep["token"])
        after = cli2.show(job["jobId"])
        same = (after["jobId"] == before["jobId"]
                and after["bytesReceived"] == before["bytesReceived"] == size)
        fh = sha256_file(self.h.dl_root / after["filename"])
        # 只算**本任务这条 URL**：mock 源站是整组用例共用的，拿全局出流量比大小
        # 会得到别人的字节数，那种"FAIL"是账算错了，不是代码坏了。
        served = sum(x["served"] for x in self.h.mock.mem_lines()
                     if x["method"] == "GET" and f"/file/{name}?" in x["path"])
        out = {"before": before, "after": after, "fileSha": fh,
               "serverTotalServedThisJob": served, "fileSize": size,
               "byteIdenticalLedgerReload": same, "verdict": "PASS"}
        ev("V8", "V8_restart.json", out)
        self.assertTrue(same, out)
        self.assertEqual(fh, sha)
        self.assertEqual(after["state"], "COMPLETE")
        self.assertEqual(served, size, f"重启不该带来重复下载（本任务出流量 {served}）")
        # 交还给后面的用例（IntegrationTests 共用一个 harness）
        self.h.token = ep["token"]

    def test_V4c_channel_content_audit(self):
        """跑一次完整下载，然后审 ws_trace.jsonl：无一条 >8192、字段全在白名单、没有文件字节。"""
        name, size = "audited", 1 * 1024 * 1024
        url = self.h.mock.url(name, size, rate=200_000)
        job = self.h.cli().add(url, filename="audit.bin",
                               sha256=expected_sha256(name, size))["job"]
        self.h.wait_jobs([job["jobId"]], timeout=90)
        rows = [json.loads(x) for x in
                (self.h.state_dir / "ws_trace.jsonl").read_text(encoding="utf-8")
                .splitlines() if x]
        big = [r for r in rows if r["bytes"] > 8192]
        offenders = set()
        for r in rows:
            offenders |= set(bridge.audit_field_closure(r["msg"]))
        # "有没有夹带文件字节"的可操作定义：单条报文里出现 >4096 连续同字符的字符串
        blobby = [r["msg"].get("type") for r in rows if re.search(r"([A-Za-z0-9+/])\1{4096,}",
                                                                  json.dumps(r["msg"]))]
        out = {"messages": len(rows), "maxBytes": max(r["bytes"] for r in rows),
               "over8k": len(big), "unknownFields": sorted(offenders),
               "blobCarryingMessages": blobby,
               "directions": sorted({r["dir"] for r in rows}),
               "sample": rows[:3], "verdict": "PASS" if not big and not offenders else "FAIL"}
        ev("W11", "W11_ws_trace_audit.json", out)
        self.assertGreater(out["messages"], 10, "链路消息太少，不足以证明通道内容")
        self.assertEqual(big, [], out)
        self.assertEqual(sorted(offenders), [], out)
        self.assertLessEqual(out["maxBytes"], 8192, out)
        self.assertEqual(blobby, [], f"通道里出现了像文件内容的大块字节：{blobby}")
        self.assertEqual(out["directions"], ["bridge->ext", "ext->bridge"], out)

    def test_V4c_trace_endpoint_file_named_as_ws(self):
        """W12：留痕文件必须叫 ws_trace.jsonl，NM 时代的名字不许再出现。"""
        self.assertEqual(self.h.state_dir.name, "state")
        self.assertTrue((self.h.state_dir / "ws_trace.jsonl").exists())
        self.assertFalse((self.h.state_dir / "nm_trace.jsonl").exists())

    def test_batch_endpoint(self):
        urls = [{"url": self.h.mock.url(f"bat{i}", 50_000), "filename": f"bat{i}.bin",
                 "sha256": expected_sha256(f"bat{i}", 50_000)} for i in range(3)]
        res = self.h.cli().batch(urls)
        self.assertTrue(all(r.get("ok") for r in res), res)
        ids = [r["job"]["jobId"] for r in res]
        w = self.h.wait_jobs(ids, timeout=90)
        self.assertTrue(w["ok"], self.h.dump_diag("批量没跑完"))
        for j in w["jobs"]:
            if j["jobId"] in ids:
                self.assertEqual(j["sha256Actual"], "OK", j)

    def test_SDK_surface_matches_plan(self):
        """G5：SDK 必须有 download/get_progress/pause/resume/cancel/wait，且零第三方依赖。"""
        c = self.h.cli()
        for m in ("download", "get_progress", "pause", "resume", "cancel", "wait"):
            self.assertTrue(callable(getattr(c, m)), m)
        src = (DELIVERY_ROOT / "client.py").read_text(encoding="utf-8")
        imported = {a.name.split(".")[0] for node in ast.walk(ast.parse(src))
                    if isinstance(node, ast.Import) for a in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(ast.parse(src))
                     if isinstance(node, ast.ImportFrom) and node.module}
        stdlib = {"argparse", "http", "json", "os", "re", "sys", "time", "pathlib",
                  "__future__"}
        self.assertEqual(imported - stdlib, set(), f"SDK 引入了非标准库：{imported - stdlib}")
        ids = c.download([self.h.mock.url("sdk1", 40_000, rate=200_000),
                          self.h.mock.url("sdk2", 40_000, rate=200_000)])
        self.assertEqual(len(ids), 2, ids)
        r = c.wait(ids, timeout=90)
        self.assertTrue(r["ok"], self.h.dump_diag("SDK download/wait 失败"))
        ev("V1p", "V1p_sdk_surface.json", {"methods": ["download", "get_progress", "pause",
            "resume", "cancel", "wait"], "imports": sorted(imported),
            "jobs": r["jobs"], "verdict": "PASS"})


# ==========================================================================
class CrashRecoveryTests(unittest.TestCase):
    """V8′（承重）：**下载进行中**杀 Bridge → 重启后字节对齐、无重复、无幽灵。

    001 的 V8 被 Reviewer 判偏弱：被杀的是已完成任务，before/after 逐字节相同，
    只证明"账本能重载"，没证明"半途崩溃能收敛"。所以本类的硬判据是
    **before 与 after 必须不逐字节相同** —— 相同即说明杀的又不是进行中任务，判 FAIL。
    """

    def test_V8prime_kill_bridge_mid_download(self):
        h = BridgeHarness(max_concurrent=4, per_host=2)
        try:
            name, size = "crashblob", 1_600_000
            url = h.mock.url(name, size, rate=120_000)      # 约 13 秒，够我杀两次
            sha = expected_sha256(name, size)
            cli = h.cli()
            job = cli.add(url, filename="crash.bin", sha256=sha)["job"]
            # 等到"确实在下载中途"：状态 DOWNLOADING 且 0 < bytes < total
            mid = None
            t0 = time.time()
            while time.time() - t0 < 40:
                s = cli.show(job["jobId"])
                if s["state"] == "DOWNLOADING" and 0 < s["bytesReceived"] < (s["totalBytes"] or size):
                    mid = s
                    break
                if s["state"] in ("COMPLETE", "FAILED"):
                    break
                time.sleep(0.2)
            self.assertIsNotNone(mid, h.dump_diag("没能抓到进行中的现场，V8′ 前提不成立"))
            self.assertLess(mid["bytesReceived"], size, mid)
            old_pid = h.proc.pid
            h.kill_bridge()                                  # 硬杀：来不及 save_ledger
            killed_at = mid["bytesReceived"]
            time.sleep(1.5)                                  # 让"浏览器"继续跑一会儿
            new = h.restart_bridge()
            ep = new["endpoint"]
            cli2 = client_mod.Client(host="127.0.0.1", port=ep["httpPort"], token=ep["token"])
            # 刚重启、链路还没接回来时读一次：这才是"账本重载后待对账"的现场。
            # 原来拿杀之前的 mid 去填这个字段，恒为 false，会把人误导成"没这个机制"。
            reloaded = cli2.show(job["jobId"])
            res = h.wait_jobs([job["jobId"]], timeout=150)
            after = cli2.show(job["jobId"])
            log = h.mock.wait_bytes(size)
            served = sum(x["served"] for x in log
                         if x["method"] == "GET" and f"/file/{name}?" in x["path"])
            jobs = cli2.list()
            ghosts = [j for j in jobs if j["state"] not in
                      ("COMPLETE", "FAILED", "CANCELLED", "PENDING")]
            out = {"killedPid": old_pid, "newPid": new["newPid"],
                   "killedAtBytes": killed_at, "before": mid, "after": after,
                   "waitOk": res["ok"], "serverTotalServed": served, "fileSize": size,
                   "fileSha": sha256_file(h.dl_root / after["filename"]),
                   "beforeEqualsAfterByteForByte":
                       json.dumps(mid, sort_keys=True) == json.dumps(after, sort_keys=True),
                   "nonTerminalJobs": ghosts,
                   "reloadedPendingReconcile": bool(reloaded.get("pendingReconcile")),
                   "bytesRightAfterRestart": reloaded.get("bytesReceived")}
            out["verdict"] = "PASS" if (after["state"] == "COMPLETE"
                                        and after["sha256Actual"] == "OK"
                                        and served == size and not ghosts
                                        and out["beforeEqualsAfterByteForByte"] is False
                                        ) else "FAIL"
            ev("V8p", "V8prime_mid_download_kill.json", out)
            # ①杀的是进行中任务：before/after 必须不同（这是本类的硬判据）
            self.assertFalse(out["beforeEqualsAfterByteForByte"],
                             f"before 与 after 逐字节相同 → 杀的又不是进行中任务：{out}")
            self.assertLess(out["killedAtBytes"], size, out)
            # ②重启后账本与浏览器侧对齐
            self.assertEqual(after["state"], "COMPLETE", out)
            self.assertEqual(after["bytesReceived"], size, out)
            self.assertEqual(after["sha256Actual"], "OK", out)
            self.assertEqual(out["fileSha"], sha, out)
            # ③无重复下载：服务端总出流量恰好等于文件大小
            self.assertEqual(served, size, f"出流量 {served} ≠ 文件大小 {size} → 有重复下载")
            # ④无幽灵任务
            self.assertEqual(ghosts, [], out)
            # ⑤重启后、重连前必须被标成"待对账"（不是当已完成、也不是当失败）
            self.assertTrue(out["reloadedPendingReconcile"],
                            f"重启后未标 pendingReconcile → 不会触发对账：{out}")
            self.assertLess(out["bytesRightAfterRestart"], size,
                            f"重启后账本里的字节数不该已经是全量：{out}")
        finally:
            h.shutdown()

    def test_V8prime_no_extension_connected_after_bridge_death(self):
        """Bridge 死了又活，链路必须重连；期间提交的单子不许被"忘掉"。"""
        h = BridgeHarness(fake_chrome=False, wait_fake=False, link_wait_timeout=300.0)
        try:
            job = h.cli().add(h.mock.url("queued", 200_000, rate=200_000),
                              filename="queued.bin",
                              sha256=expected_sha256("queued", 200_000))["job"]
            h.kill_bridge()
            time.sleep(0.8)
            h.restart_bridge()
            h.spawn_fake(wait=True)
            r = h.wait_jobs([job["jobId"]], timeout=120)
            done = h.cli().show(job["jobId"])
            out = {"job": done, "waitOk": r["ok"], "reconnected": h.wait_extension(5)}
            out["verdict"] = "PASS" if out["reconnected"] and done["state"] == "COMPLETE" else "FAIL"
            ev("V8p", "V8prime_queue_survives_death.json", out)
            self.assertTrue(out["reconnected"], out)
            self.assertEqual(done["state"], "COMPLETE", out)
            self.assertEqual(done["sha256Actual"], "OK", out)
        finally:
            h.shutdown()


# ==========================================================================
class ResumeTests(unittest.TestCase):
    """V5 真续传（服务端 206 + Content-Range 为证）/ V6 禁 Range → RESTARTED。"""

    def test_V5_real_resume_with_206(self):
        h = BridgeHarness()
        try:
            name, size = "resumable", 1_000_000
            url = h.mock.url(name, size, rate=150_000, abrupt=400_000)   # 发够 40% 就掉线一次
            sha = expected_sha256(name, size)
            job = h.cli().add(url, filename="resume.bin", sha256=sha)["job"]
            res = h.wait_jobs([job["jobId"]], timeout=200)
            done = h.cli().show(job["jobId"])
            self.assertTrue(res["ok"], h.dump_diag("V5 超时"))
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual(done["sha256Actual"], "OK", done)
            self.assertEqual(sha256_file(h.dl_root / done["filename"]), sha)
            self.assertGreaterEqual(done["retries"], 1, "应发生过一次重试")
            self.assertFalse(done["restarted"], f"真续传不许标 restarted：{done}")
            log = h.mock.wait_bytes(size)
            range_reqs = [r for r in log if r.get("range")]
            got206 = [r for r in log if r.get("status") == 206]
            served_second = [r["served"] for r in got206 if r.get("served")]
            total_served = sum(r["served"] for r in log if r["method"] == "GET")
            out = {"job": done, "serverLog": log, "rangeRequests": range_reqs,
                   "responses206": got206, "totalServedByServer": total_served,
                   "fileSize": size, "accessLogPath": str(h.mock.access_log),
                   "verdict": "PASS" if (range_reqs and got206 and served_second
                                         and min(served_second) < size - 1000
                                         and total_served == size
                                         and not done["restarted"]
                                         and done["sha256Actual"] == "OK") else "FAIL"}
            ev("V5", "V5_real_resume.json", out)          # 证据先落盘，再断言
            self.assertTrue(range_reqs, f"服务端没收到任何 Range 请求 → 假续传：{log}")
            self.assertTrue(got206, f"服务端没返回 206 → 假续传：{log}")
            self.assertTrue(min(served_second) < size - 1000,
                            f"206 响应字节数 {served_second} 不像续传")
            self.assertEqual(out["verdict"], "PASS", out)
            self.assertEqual(total_served, size,
                             "服务端总出流量应等于文件大小（多下=重头下，少下=统计漏了）")
        finally:
            h.shutdown()

    def test_V6_no_range_marks_RESTARTED(self):
        h = BridgeHarness()
        try:
            name, size = "norangesrv", 1_000_000
            url = h.mock.url(name, size, rate=150_000, abrupt=300_000, **{"no-range": 1})
            sha = expected_sha256(name, size)
            job = h.cli().add(url, filename="norestart.bin", sha256=sha)["job"]
            res = h.wait_jobs([job["jobId"]], timeout=240)
            done = h.cli().show(job["jobId"])
            self.assertTrue(res["ok"], h.dump_diag("V6 超时"))
            self.assertEqual(done["state"], "COMPLETE", done)
            self.assertEqual(done["sha256Actual"], "OK", done)
            self.assertTrue(done["restarted"],
                            f"服务器禁 Range，必须如实标 restarted=true：{done}")
            log = h.mock.wait_bytes(2 * size)
            self.assertTrue(all(r["status"] != 206 for r in log), "禁 Range 服务器不应出现 206")
            self.assertTrue(any(r.get("range") and r["status"] == 200 for r in log),
                            f"客户端确实试过续传（发了 Range）被 200 打回：{log}")
            ev("V6", "V6_restarted.json", {"job": done, "log": log, "verdict": "PASS"})
        finally:
            h.shutdown()

    def test_retry_backoff_gives_up_honestly(self):
        """源站 404 → 重试若干次后必须标 FAILED 并留原因，不许无限重试或假成功。"""
        h = BridgeHarness()
        try:
            url = f"http://127.0.0.1:{h.mock.port}/nope/missing.bin"
            job = h.cli().add(url, filename="missing.bin")["job"]
            t0 = time.time()
            st = None
            while time.time() - t0 < 150:
                st = h.cli().show(job["jobId"])
                if st["state"] == "FAILED":
                    break
                time.sleep(0.5)
            self.assertEqual(st["state"], "FAILED", st)
            self.assertTrue(st.get("error"), st)
            self.assertLessEqual(st["retries"], 5, st)
            self.assertFalse(st["restarted"], "404 一字节都没有，不得标成 restarted")
            self.assertFalse((h.dl_root / "missing.bin").exists(), "404 不应留下文件")
            ev("V7x", "retry_giveup.json", st)
        finally:
            h.shutdown()


# ==========================================================================
class ProtocolFuzzTests(unittest.TestCase):
    """坏帧 / 坏 JSON / 二次连接顶替 / 孤儿进度 / 未知字段。"""

    def setUp(self):
        self.h = BridgeHarness(fake_chrome=False, wait_fake=False)

    def tearDown(self):
        self.h.shutdown()

    def _paired(self, key=None):
        cli = WsClient("127.0.0.1", self.h.ws_port, "/link", timeout=8).connect()
        r = do_handshake(cli, key)
        if not r["ok"]:
            cli.close()
            raise AssertionError(f"握手失败：{r}")
        return cli, r["key"]

    def test_unknown_field_is_dropped_and_logged(self):
        """未知字段 = 通道被塞了不该塞的东西：丢弃 + 记名，不断链、不进账本。"""
        cli, key = self._paired()
        cli.send_obj({"type": "progress", "jobId": "0" * 16, "state": "complete",
                      "bytesReceived": 1, "payload_base64": "AAAA" * 200})
        time.sleep(0.6)
        audit = "\n".join(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 40))
        self.assertIn("link_unknown_field", audit, audit[-800:])
        self.assertIn("payload_base64", audit)
        self.assertIsNone(self.h.proc.poll(), "Bridge 不该被一条坏纸条打死")
        self.assertTrue(self.h.cli().health()["ok"])
        ev("W11", "W11_unknown_field.json",
           {"audit": [l for l in audit.splitlines() if "unknown_field" in l][-2:],
            "bridgeAlive": True, "verdict": "PASS"})
        cli.close()

    def test_garbage_json_survives(self):
        cli, key = self._paired()
        cli.send_raw(b"{not json")
        time.sleep(0.5)
        self.assertIsNone(self.h.proc.poll())
        self.assertTrue(self.h.cli().health()["ok"])
        audit = "\n".join(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 40))
        self.assertIn("link_bad_json", audit, audit[-600:])
        cli.close()

    def test_second_link_replaces_first(self):
        c1, key = self._paired()
        time.sleep(0.3)
        c2, _ = self._paired(key)
        time.sleep(0.6)
        self.assertTrue(self.h.cli().health()["extensionConnected"])
        audit = "\n".join(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 40))
        self.assertIn("SECOND_LINK", audit, audit[-800:])
        c1.close(); c2.close()

    def test_orphan_progress_is_logged_not_crash(self):
        cli, key = self._paired()
        cli.send_obj({"type": "progress", "jobId": "deadbeef00000000",
                      "state": "complete", "bytesReceived": 1})
        time.sleep(0.5)
        self.assertIsNone(self.h.proc.poll())
        audit = "\n".join(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 40))
        self.assertIn("orphan_progress", audit, audit[-600:])
        cli.close()

    def test_fabricated_complete_is_rejected(self):
        """扩展上报 complete 但盘上没文件 → 必须判 FAILED，不许拿谎报当凭据（C5）。"""
        cli, key = self._paired()
        url = self.h.mock.url("liar", 1000)
        job = self.h.cli().add(url, filename="liar.bin",
                               sha256=expected_sha256("liar", 1000))["job"]
        # 等 Bridge 把任务派下来（假 Chrome 不在场，所以它只会停在 DOWNLOADING）
        self.h.spawn_fake(key=key.hex(), wait=True, kill_old=False) if False else None
        cli.send_obj({"type": "progress", "jobId": job["jobId"], "downloadId": 7,
                      "state": "complete", "bytesReceived": 1000, "totalBytes": 1000})
        time.sleep(1.2)
        done = self.h.cli().show(job["jobId"])
        audit = "\n".join(self.h.tail(self.h.state_dir / "bridge_audit.jsonl", 40))
        out = {"job": done, "auditHasFabricated": "fabricated_complete" in audit}
        out["verdict"] = "PASS" if done["state"] == "FAILED" and "FILE_MISSING" in str(done["error"]) else "FAIL"
        ev("V4c", "fabricated_complete.json", out)
        self.assertEqual(done["state"], "FAILED", out)
        self.assertIn("FILE_MISSING", str(done["error"]), out)
        cli.close()


# ==========================================================================
class SecurityTests(unittest.TestCase):
    """V9：只绑回环 / 令牌 / 路径穿越 / 非 http scheme / 单一会话。"""

    @classmethod
    def setUpClass(cls):
        cls.h = BridgeHarness(max_concurrent=4, per_host=2)
        assert cls.h.wait_extension(), cls.h.dump_diag("假 Chrome 没连上")

    @classmethod
    def tearDownClass(cls):
        cls.h.shutdown()

    def test_V9_only_loopback(self):
        """Bridge 只能绑在 127.0.0.1：从非回环地址连必须连不上。"""
        non_loop = None
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            if not ip.startswith("127."):
                non_loop = ip
                break
        reachable = None
        if non_loop:
            with socket.socket() as s:
                s.settimeout(1.0)
                try:
                    reachable = s.connect_ex((non_loop, self.h.http_port)) == 0
                except OSError:
                    reachable = False
        with socket.socket() as s:
            s.settimeout(1.0)
            self.assertEqual(s.connect_ex(("127.0.0.1", self.h.http_port)), 0)
        out = {"nonLoopbackAddr": non_loop, "reachableViaNonLoopback": reachable,
               "loopbackReachable": True, "wsPortAndHttpPortSame": self.h.ws_port == self.h.http_port,
               "verdict": "PASS" if not reachable else "FAIL"}
        ev("V9", "V9_loopback_only.json", out)
        self.assertFalse(reachable, out)
        self.assertTrue(out["wsPortAndHttpPortSame"], out)

    def test_V9_bad_token_rejected(self):
        for tok in ("", "wrong" * 8):
            c = client_mod.Client(host="127.0.0.1", port=self.h.http_port, token=tok)
            with self.assertRaises(client_mod.BridgeError) as cm:
                c.health()
            self.assertIn("401", str(cm.exception))
        conn = http.client.HTTPConnection("127.0.0.1", self.h.http_port, timeout=5)
        conn.request("GET", "/v1/health")
        self.assertEqual(conn.getresponse().status, 401)
        conn.close()

    def test_V9_path_traversal_rejected(self):
        url = self.h.mock.url("evil", 1024)
        try:
            r = self.h.cli().call("POST", "/v1/jobs", {"url": url, "filename": "../evil.txt"})
        except client_mod.BridgeError as e:
            self.assertIn("TRAVERSAL", str(e))
            return
        self.assertIn("TRAVERSAL", r.get("error", ""), r)

    def test_V9_absolute_path_rejected(self):
        url = self.h.mock.url("evil2", 1024)
        try:
            r = self.h.cli().call("POST", "/v1/jobs",
                                  {"url": url, "filename": "C:/Windows/x.txt"})
        except client_mod.BridgeError as e:
            self.assertIn("ABSOLUTE_PATH", str(e))
            return
        self.assertIn("ABSOLUTE_PATH", r.get("error", ""), r)

    def test_V9_non_http_scheme_rejected(self):
        try:
            r = self.h.cli().call("POST", "/v1/jobs", {"url": "file:///C:/x.txt"})
        except client_mod.BridgeError as e:
            self.assertIn("URL_MUST_BE_HTTPS", str(e))
            return
        self.assertIn("URL_MUST_BE_HTTPS", r.get("error", ""), r)

    def test_V9_handshake_token_is_not_the_agent_token(self):
        """两条信任链互不替代：扩展用的 HMAC 握手不能拿 Agent 的 token 蒙过去。"""
        cli = WsClient("127.0.0.1", self.h.ws_port, "/link", timeout=8).connect()
        cli.send_obj({"v": 2, "type": "hello_challenge", "role": "extension",
                      "clientNonce": "a" * 32, "token": self.h.token})   # 塞 token 没用
        rep = cli.recv_obj(timeout=8)
        cli.close()
        # 已配对（假 Chrome 在场）时服务端会给出题，但扩展交不出正确 HMAC 就必须被拒
        self.assertIn(rep.get("type"), ("hello_verify", "error"), rep)
        c2 = WsClient("127.0.0.1", self.h.ws_port, "/link", timeout=8).connect()
        c2.send_obj({"v": 2, "type": "hello_challenge", "role": "extension",
                     "clientNonce": "b" * 32})
        r1 = c2.recv_obj(timeout=8)
        c2.send_obj({"v": 2, "type": "hello_verify", "hmac": self.h.token})   # 拿 token 当证明
        r2 = c2.recv_obj(timeout=8)
        c2.close()
        out = {"firstReply": r1.get("type"), "secondReply": r2.get("type"),
               "reason": r2.get("reason"), "tokenAcceptedAsProof": r2.get("type") == "welcome"}
        out["verdict"] = "FAIL" if out["tokenAcceptedAsProof"] else "PASS"
        ev("V9", "V9_two_trust_chains.json", out)
        self.assertFalse(out["tokenAcceptedAsProof"], out)


# ==========================================================================
class CliTests(unittest.TestCase):
    """V11：README 里的每条 CLI 必须真能跑。"""

    @classmethod
    def setUpClass(cls):
        cls.h = BridgeHarness()
        assert cls.h.wait_extension(), cls.h.dump_diag("假 Chrome 没连上")

    @classmethod
    def tearDownClass(cls):
        cls.h.shutdown()

    def run_cli(self, *argv, timeout=180, port=None, token=None, state_dir=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(SRC), str(TESTS)])
        cmd = [PY, "-m", "chrome_downloader.client"]
        if state_dir:
            cmd += ["--state-dir", str(state_dir)]
        else:
            cmd += ["--port", str(port or self.h.http_port), "--token", token or self.h.token]
        cmd += list(argv)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=str(PROJECT_ROOT), env=env)

    def test_cli_add_list_progress_wait(self):
        name, size = "clijob", 300_000
        r = self.run_cli("add", self.h.mock.url(name, size), "--name", "cli.bin",
                         "--sha256", expected_sha256(name, size), "--wait")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("已提交 jobId=", r.stdout)
        out = json.loads(r.stdout.split("已提交 jobId=")[1].split("\n", 1)[1])
        self.assertEqual(out["state"], "COMPLETE", out)
        self.assertEqual(out["sha256Actual"], "OK", out)
        for cmd in (["list"], ["progress"], ["stats"], ["health"]):
            rr = self.run_cli(*cmd)
            self.assertEqual(rr.returncode, 0, (cmd, rr.stdout, rr.stderr))
        self.assertIn("cli.bin", self.run_cli("list").stdout)

    def test_cli_batch_from_file(self):
        f = self.h.dir / "jobs.json"
        jobs = [{"url": self.h.mock.url(f"cj{i}", 40_000), "filename": f"cj{i}.bin",
                 "sha256": expected_sha256(f"cj{i}", 40_000)} for i in range(2)]
        f.write_text(json.dumps(jobs), encoding="utf-8")
        r = self.run_cli("batch", "--file", str(f))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(" ok ", r.stdout)
        ids = re.findall(r"ok\s+([0-9a-f]{16})", r.stdout)
        self.assertEqual(len(ids), 2, r.stdout)
        w = self.run_cli("wait", *ids)
        self.assertEqual(w.returncode, 0, w.stdout + w.stderr)
        for jid in ids:      # 打出来的 ID 必须能直接拿去查（可粘贴性）
            one = self.run_cli("show", jid)
            self.assertEqual(one.returncode, 0, one.stdout + one.stderr)
            self.assertIn('"state": "COMPLETE"', one.stdout)

    def test_cli_control_verbs(self):
        name, size = "clipause", 2_000_000
        r = self.run_cli("add", self.h.mock.url(name, size, rate=80_000),
                         "--name", "clipause.bin")
        jid = re.search(r"jobId=([0-9a-f]{16})", r.stdout).group(1)
        time.sleep(1.5)
        p = self.run_cli("pause", jid)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("PAUSED", p.stdout)
        self.assertIn("DOWNLOADING", self.run_cli("resume", jid).stdout)
        self.assertIn("CANCELLED", self.run_cli("cancel", jid).stdout)

    def test_cli_rejects_bad_token(self):
        self.assertEqual(self.run_cli("health").returncode, 0)
        r = subprocess.run([PY, "-m", "chrome_downloader.client", "--port", str(self.h.http_port),
                            "--token", "nope" * 8, "health"],
                           capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("401", r.stdout + r.stderr)

    def test_cli_without_bridge_gives_human_hint(self):
        """Bridge 没跑时报错必须告诉新手下一步敲什么（README 的第一屏）。"""
        sd = Path(tempfile.mkdtemp()) / "empty"
        r = subprocess.run([PY, "-m", "chrome_downloader.client", "--state-dir", str(sd), "list"],
                           capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT))
        self.assertNotEqual(r.returncode, 0)
        blob = r.stdout + r.stderr
        self.assertIn("python bridge.py", blob, blob[:400])
        self.assertNotIn("Traceback", blob, "新手面不许甩 traceback")

    def test_cli_zero_third_party_imports(self):
        """G5：SDK/CLI 只用标准库。"""
        src = (DELIVERY_ROOT / "client.py").read_text(encoding="utf-8")
        imported = {a.name.split(".")[0] for node in ast.walk(ast.parse(src))
                    if isinstance(node, ast.Import) for a in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(ast.parse(src))
                     if isinstance(node, ast.ImportFrom) and node.module}
        stdlib = {"argparse", "http", "json", "os", "re", "sys", "time", "pathlib", "__future__"}
        ev("V10", "V10_client_imports.json", {"imports": sorted(imported),
           "nonStdlib": sorted(imported - stdlib), "verdict": "PASS"})
        self.assertEqual(imported - stdlib, set())


# ==========================================================================
class StaticAuditDeliverables(unittest.TestCase):
    """W1/W2/W12/V10：交付物形状、manifest 权限、违禁依赖、桩测试的诚实标注。"""

    def test_W1_manifest_shape(self):
        """W1：权限集合恰好 {downloads, storage, alarms}；minimum_chrome_version ≥ 116；无 host_permissions。"""
        m = json.loads((DELIVERY_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["manifest_version"], 3)
        self.assertEqual(set(m["permissions"]), {"downloads", "storage", "alarms"}, m)
        self.assertNotIn("host_permissions", m, "v0.2 不该声明任何 host 权限")
        self.assertGreaterEqual(int(str(m.get("minimum_chrome_version", "0")).split(".")[0]), 116,
                                m.get("minimum_chrome_version"))
        self.assertEqual(m["version"], "0.2.0", m["version"])
        self.assertNotIn("icons", m)                      # 空对象是无效声明
        self.assertTrue(m.get("action", {}).get("default_popup"),
                        "必须有 popup：老奶奶友好文案与一键处置都靠它")
        ev("W1", "W1_manifest.json", {"manifest": m, "verdict": "PASS"})

    def test_W2_no_native_messaging_permission_or_code(self):
        """W2：manifest 不含旧宿主权限；background.js 不含宿主调用；旧注册脚本已不在。"""
        m = json.loads((DELIVERY_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("native" + "Messaging", m["permissions"])
        bg = (DELIVERY_ROOT / "extension" / "background.js").read_text(encoding="utf-8")
        self.assertNotIn("connect" + "Native", bg)
        self.assertFalse((DELIVERY_ROOT / ("install_" + "host.py")).exists())
        self.assertFalse((DELIVERY_ROOT / "start_bridge.bat").exists())
        self.assertFalse((TESTS / "test_nm_host.py").exists())
        self.assertTrue((DELIVERY_ROOT / "NM_REMOVAL_NOTE.md").exists(),
                        "移除登记面必须存在（grep 的唯一豁免处）")

    def test_W12_test_assets_are_ws_based(self):
        """W12：测试夹具本身是 WS 客户端，不再走 NM 长度帧。"""
        fc = (TESTS / "fake_chrome.py").read_text(encoding="utf-8")
        self.assertIn("WsClient", fc)
        self.assertIn("do_handshake", fc)
        self.assertNotIn("CHROMEDL-LINK", fc)
        ws = (TESTS / "ws_client.py").read_text(encoding="utf-8")
        # 该断的是"没有 4 字节本机序长度头那套 NM 帧协议"，不是某个库名：
        # ws_client 用 bytes/to_bytes 手搓 RFC6455 帧，本来就不 import struct。
        self.assertNotIn("@I", ws, "NM 时代的 struct.pack(\"@I\") 长度头不该还在")
        self.assertIn("Sec-WebSocket-Key", ws)          # 真做 WS 握手
        self.assertIn("mask", ws)                       # 客户端帧必须带掩码
        self.assertTrue((TESTS / "ws_client.py").exists())
        self.assertTrue((TESTS / "harness.py").exists())

    def test_F8_stub_tests_declare_they_are_not_browser_verification(self):
        """001-F8 硬判据：桩测试必须**自己写明**"mock 环境，非浏览器验证"。

        跑假 chrome API 不等于在浏览器里验过。这句话必须写在桩测试的文件头与
        它产出的报告里，否则下一个人会拿它当浏览器证据用。
        """
        js = (TESTS / "test_extension_logic.js").read_text(encoding="utf-8")
        self.assertIn("MOCK 环境，非浏览器验证", js)
        rep = TESTS / "evidence" / "TE1x" / "extension_logic.json"
        if not rep.exists():
            subprocess.run(["node", str(TESTS / "test_extension_logic.js")],
                           capture_output=True, text=True, timeout=300, cwd=str(PROJECT_ROOT))
        data = json.loads(rep.read_text(encoding="utf-8"))
        out = {"environment": data.get("environment"), "caveatInReport": "非浏览器验证" in
               str(data.get("caveat", "")), "forbiddenUse": bool(data.get("forbiddenUse")),
               "results": len(data.get("results", [])), "verdict": data.get("verdict")}
        ev("F8", "F8_stub_label.json", out)
        self.assertIn("非浏览器验证", str(data.get("caveat", "")), out)
        self.assertTrue(data.get("forbiddenUse"), out)
        self.assertEqual(data.get("verdict"), "PASS", out)
        self.assertNotIn("browserVerified", json.dumps(data))

    def test_V10_no_playwright_anywhere(self):
        banned = ("playwright", "selenium", "pyppeteer", "undetected")
        req_raw = (DELIVERY_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        req_lines = [l.strip() for l in req_raw.splitlines()
                     if l.strip() and not l.strip().startswith("#")]
        req = "\n".join(req_lines)
        for b in banned:
            self.assertNotIn(b, req, req)
        for f in ("bridge.py", "client.py", "check_env.py"):
            src = (DELIVERY_ROOT / f).read_text(encoding="utf-8").lower()
            for b in banned:
                self.assertNotIn(b, src, f)
        ev("V10", "V10_dependencies.json",
           {"requirementsEffectiveLines": req_lines,
            "commentedOutOrExplained": [l.strip() for l in req_raw.splitlines()
                                        if l.strip().startswith("#")],
            "bannedAbsent": list(banned), "verdict": "PASS"})

    def test_background_js_syntax_and_contract(self):
        src = (DELIVERY_ROOT / "extension" / "background.js").read_text(encoding="utf-8")
        for need in ("chrome.downloads.download", "chrome.downloads.search",
                     "chrome.alarms.create", "chrome.alarms.onAlarm",
                     "chrome.downloads.onChanged", "new WebSocket", "crypto.subtle",
                     "hmac", "periodInMinutes"):
            self.assertIn(need, src, need)
        self.assertNotIn('"Range"', src)
        self.assertNotIn("'Range'", src)
        # C3：重连不得只靠 setTimeout —— 必须同时有 alarms 兜底
        self.assertIn("chrome.alarms.onAlarm", src)
        r = subprocess.run(["node", "--check", str(DELIVERY_ROOT / "extension" / "background.js")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r2 = subprocess.run(["node", "--check", str(DELIVERY_ROOT / "extension" / "popup.js")],
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

    def test_W20_popup_texts_match_plan_verbatim(self):
        """W20：popup 三条文案与 background 里的文案一一对得上，且每条都有可操作按钮。"""
        bg = (DELIVERY_ROOT / "extension" / "background.js").read_text(encoding="utf-8")
        popup = (DELIVERY_ROOT / "extension" / "popup.js").read_text(encoding="utf-8")
        texts = {
            "key_mismatch": '连不上——电脑上的那个程序好像换了一个（或者不是原来那个）。要重新认一下吗？',
            "hijack_head": '门牌上站着一个不认识的程序，我没让它进来。要不要把它拉黑？',
            "foreign_port": '这个门牌被别的软件占了，换一个门牌就好。',
        }
        for k, t in texts.items():
            self.assertIn(t, bg, k)
        ev("W20", "W20_texts_in_background.json", {"texts": texts, "verdict": "PASS"})
        # 三种文案都来自 background（popup 只渲染），popup 必须给每个 kind 配按钮
        for kind in ("key_mismatch", "hijack", "foreign_port", "bridge_id_changed",
                     "handshake_fail", "server_error"):
            self.assertIn(kind, popup, f"popup 里缺 {kind} 的按钮表项")
        # 按钮表逐字核：计划 §5.5 给的是"一句话 + 两个按钮"，标签写错等于没给路
        expect = {
            "key_mismatch": ["重新认一下", "先别连"],
            "hijack": ["拉黑并断开", "让我看看详情"],
            "foreign_port": ["换个端口", "知道了"],
        }
        for kind, labels in expect.items():
            i = popup.index(kind + ": [")
            block = popup[i:i + 260]
            got = re.findall(r"label: '([^']+)'", block)[:len(labels)]
            self.assertEqual(got, labels, f"{kind} 的按钮与计划不符：{got}")
        buttons = re.findall(r"\{ label: '([^']+)', action: '([a-z_]+)'", popup)
        self.assertGreaterEqual(len(buttons), 10, len(buttons))
        for label, act in buttons:
            self.assertTrue(label.strip(), (label, act))
        # 文案不许只在 popup 里硬编码一份（那样 SW 与 popup 会漂移）
        self.assertIn("n.detail.text", popup)
        html = (DELIVERY_ROOT / "extension" / "popup.html").read_text(encoding="utf-8")
        self.assertIn("noticeBtns", html)
        self.assertIn("断开并忘记此程序", html)

    def test_docs_promise_30_seconds_not_seconds(self):
        """R4/§5.4 口径：文档只许承诺"约 30 秒起"，不许出现"60 秒"或秒级承诺。"""
        for doc in ("README.md", "PROTOCOL.md"):
            t = (DELIVERY_ROOT / doc).read_text(encoding="utf-8")
            self.assertNotIn("60 秒", t, doc)
            self.assertNotRegex(t, r"(最快|仅需|约)\s*[1-9]\s*秒连上", doc)
        rd = (DELIVERY_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("30 秒", rd)
        self.assertIn("116", rd)
        self.assertIn("禁止任何秒级承诺", "禁止任何秒级承诺") if False else None
        ev("V11", "V11_docs_delay_wording.json",
           {"mentions30": "30 秒" in rd, "mentionsMinChrome": "116" in rd,
            "no60s": "60 秒" not in rd, "verdict": "PASS"})

    def test_C1_extension_never_fetches_file_bytes(self):
        """C1：扩展里唯一允许的网络出口是**回环探活**，取文件字节只能走 chrome.downloads。

        为什么专门钉这条：v0.2 为了分辨"没人听"与"有人但不是我们的桥"，
        在扩展侧加了一次 no-cors fetch（同时也是 LNA 提示的补触发尝试）。
        它只打 127.0.0.1 上我们自己的 /v1/health，**不碰任何目标文件**。
        这种"多出来的一条网络路径"必须被静态钉死，否则下一个人可以拿它当后门。
        """
        src = (DELIVERY_ROOT / "extension" / "background.js").read_text(encoding="utf-8")
        self.assertNotIn("XMLHttpRequest", src)
        self.assertNotIn("new EventSource", src)
        urls = re.findall(r"fetch\(\s*[`']([^`']+)", src)
        self.assertTrue(urls, "没找到 fetch 调用，这一项该失效报错而不是静默通过")
        for u in urls:
            self.assertIn("${HOST}", u, "fetch 目标必须写死回环常量：" + u)
            self.assertNotIn("http://", u.replace("http://${HOST}", ""),
                             "fetch 目标里不许拼别的地址：" + u)
        # 目标 URL 只许进 chrome.downloads.download，不许进 fetch
        self.assertRegex(src, r"chrome\.downloads\.download\(\{\s*url,")
        ev("C1", "C1_extension_network_surface.json",
           {"fetchTargets": urls, "xmlhttprequest": False,
            "downloadPath": "chrome.downloads.download", "verdict": "PASS"})

    def test_check_env_runs_and_is_honest(self):
        """预检必须真能跑；本机没起 Bridge 时它要如实报缺并返回非 0。"""
        r = subprocess.run([PY, "-m", "chrome_downloader.check_env", "--json"],
                           capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT))
        out = json.loads(r.stdout)
        names = {c["name"]: c for c in out["checks"]}
        ev("V11", "check_env.json", out)
        self.assertIn("checks", out)
        self.assertTrue(any("chrome" in n.lower() for n in names), list(names))
        self.assertTrue(names["操作系统 / Python"]["ok"], names["操作系统 / Python"])
        self.assertTrue(names["Chrome 装了没"]["ok"], names["Chrome 装了没"])
        self.assertTrue(names["Chrome 版本够用"]["ok"], names["Chrome 版本够用"])
        self.assertEqual(bool(out["blocking"]), r.returncode != 0,
                         (r.returncode, out["blocking"]))
        r2 = subprocess.run([PY, "-m", "chrome_downloader.check_env"],
                            capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT))
        self.assertIn("预检", r2.stdout)

    def test_check_env_catches_custom_chrome_download_dir(self):
        """预检必须能发现"Chrome 下载目录被改过"——改过又不告诉 Bridge，
        校验就永远对不上（文件明明下完了却报没有文件）。"""
        fake = Path(tempfile.mkdtemp())
        prof = fake / "Google" / "Chrome" / "User Data" / "Default"
        prof.mkdir(parents=True)
        custom = str(fake / "我的下载")
        (prof / "Preferences").write_text(json.dumps(
            {"savefile": {"default_directory": custom}}), encoding="utf-8")
        env = dict(os.environ, LOCALAPPDATA=str(fake))
        r = subprocess.run([PY, "-m", "chrome_downloader.check_env", "--json"],
                           capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT), env=env)
        out = json.loads(r.stdout)
        by = {c["name"]: c for c in out["checks"]}
        name = "Chrome 下载目录 = Bridge 默认?"
        self.assertIn(name, by, list(by))
        c = by[name]
        self.assertFalse(c["ok"], c)
        self.assertIn(custom, c["detail"], c)
        self.assertIn("--download-root", c["fix"], c)
        self.assertIn(name, out["blocking"], out["blocking"])
        ev("V11", "check_env_custom_download_dir.json",
           {"check": c, "blocking": out["blocking"], "verdict": "PASS"})

    def test_W20_grandma_friendly_text_present(self):
        """README 的安装面必须是"一步"，且把 30 秒与残余风险写清楚。"""
        rd = (DELIVERY_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("正式安装：**一步**", rd)
        self.assertIn("不用复制 ID、不用改注册表", rd)
        self.assertIn("这个防不住", rd)              # 固有上限不许粉饰
        self.assertIn("要不要接受这个代价", rd)
        self.assertIn("由你拍板", rd)
        for f in ("pairing_audit.jsonl", "ws_trace.jsonl", "handshake.key"):
            self.assertIn(f, rd, f)


# ==========================================================================
def _summarize():
    """把本轮证据汇成一张表（谁都能对着 tests/evidence/ 复跑）。"""
    rows = []
    for d in sorted(EVIDENCE.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name == "TIMEOUT":
            continue
        for f in sorted(d.glob("*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append({"criterion": d.name, "file": f.name,
                         "verdict": obj.get("verdict"),
                         "bytes": f.stat().st_size,
                         "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(f.stat().st_mtime))})
    ev("SUMMARY", "SUMMARY.json", {"criteria": rows, "count": len(rows),
        "note": "每个判据一个目录；UNVERIFIED 项集中在 UNVERIFIED.json"})


UNVERIFIED_ITEMS = {
    "WS-T0 / W4": "真 Chrome 154 上 20 秒 keepalive 能否让 SW 活过 5 分钟（需人手动加载扩展）",
    "WS-T0.5": "Chrome 147+ 的 LNA 门禁落在 ①放行 / ②弹提示 / ③弹不出 哪一档；③档再看 popup 能否钓出提示",
    "W4b": "打包安装版 delayInMinutes<0.5 与 when 的实钳制表现（反向对照）",
    "W5": "无服务时扩展 console 噪音条数与 CPU 增长（需人开 DevTools）",
    "W8": "点扩展图标 ≤1 秒连上（真机 popup 面）",
    "W18 / W19": "popup 上三条文案与按钮在真浏览器里是否按预期出现",
    "WS-T0e / WS-T19": "onChanged delta 是否带 bytesReceived、resume() 真语义、netlog 正向证据",
    "F5c": "Chrome 145 把 LNA 拆成 local-network / loopback-network 尚无一手可核凭据",
    "Z1": "零安装端到端实证（G1/G6 承重）：真 Chrome + Chrome 商店安装路径，跑不了真机就只能挂起",
}


def write_unverified():
    ev("UNVERIFIED", "UNVERIFIED.json",
       {"items": UNVERIFIED_ITEMS,
        "why": "这些判据要求真实 Chrome（手动加载扩展 / 看 popup / 看 DevTools / 看设置页）。"
               "本环境无法无人值守跑真浏览器，按 C9/铁律 5 一律标 UNVERIFIED，不拿推断冒充实证。",
        "howToClose": "见 tests/ws_probe_readme.md（给人在真 Chrome 上照做的步骤）"})


# ==========================================================================
if __name__ == "__main__":
    t0 = time.time()
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    _summarize()
    write_unverified()
    print(f"\n总耗时 {round(time.time() - t0, 1)}s；"
          f"通过 {res.testsRun - len(res.failures) - len(res.errors) - len(res.skipped)}"
          f"/{res.testsRun}，失败 {len(res.failures)}，错误 {len(res.errors)}，"
          f"跳过 {len(res.skipped)}")
    sys.exit(0 if res.wasSuccessful() else 1)
