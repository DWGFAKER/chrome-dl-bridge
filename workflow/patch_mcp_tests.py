#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修 M2 / M5 / M12 三处测试写法（一次性补丁脚本）。

三处的病根各不相同，都记进证据/报告，不留"改到绿为止"的糊涂账：
  M2  —— 我原来拿**整行**比逐字节相同，可信封里带着 JSON-RPC 的 id，
         两次请求的 id 天生不同 → 判据永远红。M2 的原意是"工具表本身确定序"，
         所以要比 result 的字节，不是比信封。
  M5  —— 我在 ensure_bridge 之前没挂链路层对手，L3（配对面）永远不就绪，
         桥按设计等到超时抛 NotReady。**这是产品正确行为**（三级就绪就是要配对），
         错在用例的顺序：起桥要放线程里，桥一露头就把假 Chrome 接上去。
  M12 —— 两点：① 我把"四个码都发出来"写成 {32700,…} ⊆ {-abs(c)}，正负号写反，
         自己把自己判红；② -32603（内部错误）没法从**外部输入**造出来，
         计划要求"逐条触发一次"，所以用受控注入（把工具实现临时换成会抛异常的桩）
         触发它，并在证据里明写"这是注入，不是产品路径"。
"""
import pathlib
import sys

P = pathlib.Path("D:/article4/scratch/chrome_downloader_pkg/tests/test_pkg_v03_mcp.py")
s = P.read_text(encoding="utf-8")


def rep(old, new, where, expect=1):
    global s
    c = s.count(old)
    if c != expect:
        print(f"[FAIL] {where}: 命中 {c}（期望 {expect}）")
        sys.exit(1)
    s = s.replace(old, new)


# ---------------- M2 ----------------
rep('''        r = self.session([req("tools/list", {}, "t1"), req("tools/list", {}, "t2")])
        self.assertEqual(len(r["lines"]), 2, r["stderr"][-300:])
        same = r["lines"][0] == r["lines"][1]
        body = json.loads(r["lines"][0])
        tools = body["result"]["tools"]
        names = [t["name"] for t in tools]
        ok = same and names == TOOL_NAMES and len(tools) == 8
        ev("M", "M2_tools_list_stable.json",
           {"byteIdentical": same, "toolCount": len(tools), "order": names,
            "expectedOrder": TOOL_NAMES, "sha256Line1":
                hashlib.sha256(r["lines"][0]).hexdigest()[:16],
            "sha256Line2": hashlib.sha256(r["lines"][1]).hexdigest()[:16],
            "verdict": "PASS" if ok else "FAIL"})
        self.assertTrue(same, "两次 tools/list 不逐字节相同 = 排序不确定")
        self.assertEqual(names, TOOL_NAMES)''',
    '''        # 同 id 两次请求：信封完全相同才是"逐字节相同"的本意。
        # （第一次写这判据时我比的是两条**不同 id** 的行，那永远不相等 ——
        #   判据自身缺参，不是产品不确定。这里改成：同 id 问两遍 + 比 result 字节。）
        r = self.session([req("tools/list", {}, "tA"), req("tools/list", {}, "tB")])
        self.assertEqual(len(r["lines"]), 2, r["stderr"][-300:])
        b1 = json.loads(r["lines"][0])
        b2 = json.loads(r["lines"][1])
        result_bytes_equal = mcp._dumps(b1["result"]) == mcp._dumps(b2["result"])
        same_line = r["lines"][0] == r["lines"][1]
        tools = b1["result"]["tools"]
        names = [t["name"] for t in tools]
        # 再问第三遍（中间隔一次别的调用），确认不是"缓存出来的假确定"
        r3 = self.session([req("tools/list", {}, "tC"),
                           req("server/discover", {}, "tD"),
                           req("tools/list", {}, "tE")])
        bodies3 = [json.loads(ln) for ln in r3["lines"]]
        third = [x for x in bodies3 if x["id"] == "tE"][0]["result"]
        stable_across_sessions = mcp._dumps(third) == mcp._dumps(b1["result"])
        ok = (result_bytes_equal and stable_across_sessions and names == TOOL_NAMES and
              len(tools) == 8)
        ev("M", "M2_tools_list_stable.json",
           {"resultBytesIdentical": result_bytes_equal,
            "identicalAcrossSessions": stable_across_sessions,
            "wholeLineIdentical_whenIdsDiffer": same_line,
            "criterionNote": "判据比的是 result 字节；整行含 JSON-RPC id，两次请求 id 必然不同",
            "toolCount": len(tools), "order": names, "expectedOrder": TOOL_NAMES,
            "sha256Result1": hashlib.sha256(mcp._dumps(b1["result"])).hexdigest()[:16],
            "sha256Result2": hashlib.sha256(mcp._dumps(b2["result"])).hexdigest()[:16],
            "sha256Result3": hashlib.sha256(mcp._dumps(third)).hexdigest()[:16],
            "verdict": "PASS" if ok else "FAIL"})
        self.assertTrue(result_bytes_equal, "同一进程内两次 result 不同 = 排序不确定")
        self.assertTrue(stable_across_sessions, "跨会话 result 不同 = 掺了会话态")
        self.assertEqual(names, TOOL_NAMES)''', "M2")

# ---------------- M5 ----------------
rep('''    def test_M5_real_download_via_mcp_completes(self):
        """M5：chrome_download 真下一次 → structuredContent.jobId + 中文 text。"""
        res = dm.ensure_bridge(self.state, port=H.pick_bridge_port(),
                               download_root=self.dl, timeout=25, quiet=True)
        self.pids.add(int(res["endpoint"]["pid"]))
        self.state.mkdir(parents=True, exist_ok=True)
        fake = H.start_fake_chrome(self.state, self.dl)
        try:
            t0 = time.time()
            while time.time() - t0 < 25:
                if dm.probe_levels(paths.read_endpoint(self.state),
                                   self.state)["L3"]:
                    break
                time.sleep(0.2)''',
    '''    def test_M5_real_download_via_mcp_completes(self):
        """M5：chrome_download 真下一次 → structuredContent.jobId + 中文 text。

        顺序要点：ensure_bridge 里"三级就绪"含 L3（配对面），而 K 是**扩展第一次连上来**
        时交出的（TOFU）。所以起桥必须放线程里跑、桥一露头就把链路层对手接上去；
        先阻塞式等就绪会永远等不到 —— 那是产品在如实报"没配对面"，不是 bug。
        """
        self.state.mkdir(parents=True, exist_ok=True)
        box: dict = {}
        th = threading.Thread(target=lambda: box.update(
            res=dm.ensure_bridge(self.state, port=H.pick_bridge_port(),
                                 download_root=self.dl, timeout=30, quiet=True)))
        th.start()
        fake = None
        try:
            t0 = time.time()
            info = None
            while time.time() - t0 < 20:
                info = paths.read_endpoint(self.state)
                if info:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(info, "桥没起来")
            fake = H.start_fake_chrome(self.state, self.dl)
            th.join(90)
            self.assertIsNotNone(box.get("res"), f"三级就绪没达成：{box.get('err')}")
            self.pids.add(int(box["res"]["endpoint"]["pid"]))''', "M5")

rep('''        finally:
            try:
                fake.terminate()
                fake.communicate(timeout=8)
            except Exception:
                fake.kill()''',
    '''        finally:
            if fake is not None:
                try:
                    fake.terminate()
                    fake.communicate(timeout=8)
                except Exception:
                    try:
                        fake.kill()
                    except Exception:
                        pass''', "M5 finally")

rep('''import shutil
import subprocess
import sys
import tempfile
import threading
import time''','''import shutil
import subprocess
import sys
import tempfile
import threading
import time''', "import threading exists")

# ---------------- M12 ----------------
rep('''        got = set(codes)
        expected = set(mcp.ALLOWED_ERROR_CODES)
        # -32603 不好在"外部输入"上造出来（内部异常），所以单独用直调 API 触发一次
        internal_probe = mcp.dispatch(b\'{"jsonrpc":"2.0","id":"i1","method":"tools/call",\'
                                      b\'"params":{"_meta":{"io.modelcontextprotocol/\'
                                      b\'protocolVersion":"2026-07-28",\'
                                      b\'"io.modelcontextprotocol/clientCapabilities":{}},\'
                                      b\'"name":"chrome_download_list","arguments":\'
                                      b\'{"stateDir":"\\\\\\\\..\\\\\\\\..\\\\\\\\nonexistent\\\\\\\\x"}}}\',
                                      sess)
        checks = {
            "emitted": sorted(got),
            "subsetOfWhitelist": got <= expected,
            "fourOfThemSeen": {32700, 32601, 32602, 32022} <= {-abs(c) for c in got},
            "noCustomCodeInRange": all(c in expected for c in got),
            "internalCodeReachable": isinstance(internal_probe, dict) and (
                "error" in internal_probe or "result" in internal_probe),
        }
        ok = checks["subsetOfWhitelist"] and checks["noCustomCodeInRange"] and \\
            checks["fourOfThemSeen"]''',
    '''        got = set(codes)
        expected = set(mcp.ALLOWED_ERROR_CODES)
        # -32603（内部错误）**没法从外部输入造出来**：能想到坏输入的地方都写成了
        # isError 或 -32601/-32602。计划要求"逐条触发一次"，所以这里用受控注入：
        # 把工具实现临时换成一定抛异常的桩，看服务端是否如实报 -32603（而不是
        # 吐 traceback、也不是憋着不回话）。证据里明写"这是注入，不是产品路径"。
        sess2 = mcp.Session()
        real = mcp.call_tool
        try:
            mcp.call_tool = lambda name, args: (_ for _ in ()).throw(
                RuntimeError("注入的内部故障：只为触发 -32603"))
            injected = mcp.dispatch(json.dumps(req(
                "tools/call", {"name": "chrome_download_status", "arguments": {}},
                "i1")).encode("utf-8"), sess2)
        finally:
            mcp.call_tool = real
        injected_code = (injected or {}).get("error", {}).get("code")
        seen = got | ({injected_code} if injected_code else set())
        checks = {
            "emittedFromWire": sorted(got),
            "injectedInternalCode": injected_code,
            "subsetOfWhitelist": seen <= expected,
            "allSixTriggered": seen == expected,
            "noCustomCodeInRange": all(c in expected for c in seen),
            "notSilent": injected_code is not None,
        }
        ok = checks["subsetOfWhitelist"] and checks["allSixTriggered"] and \\
            checks["noCustomCodeInRange"]''', "M12 body")

rep('''            "emittedCodes": sorted(got), "checks": checks,
            "note": "肯定断言 = 实际发出集合必须 ⊆ 白名单，且白名单里能在 stdio 触发的"
                    "四条（-32700/-32601/-32602/-32022）都要真发出来；"
                    "-32600/-32603 属实现允许但不易从外部输入触发的，记为 reachable",''',
    '''            "emittedCodes": sorted(got), "allSeen": sorted(seen), "checks": checks,
            "injectionIsNotAProductPath":
                "-32603 由测试内注入的异常触发（把 call_tool 换成会抛的桩），"
                "证的是服务端对意外异常的回话形状；产品里正常请求不该走到那条",
            "note": "肯定断言 = 实际发出的码必须 ⊆ 白名单，且白名单六条要逐条真发出来"
                    "（五条走外部输入，-32603 走受控注入）",''', "M12 note")

P.write_text(s, encoding="utf-8")
print("[OK] M2 / M5 / M12 三处判据写法已订正")
