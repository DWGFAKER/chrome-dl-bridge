#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pkg_v03_mcp.py —— M1–M13（MCP stdio 双时代服务端，计划 §8.3；硬闸 M3/M4/M6/M11）

规范凭据：docs/mcp-spec-archive.md（STOP-12 —— 没有那份存档，M3/M4 只能靠信任）。
本文件里凡引规范处，都写"§存档 2.x"的锚，不回抄二手结论。

纪律：
  · 一律临时 state-dir + 临时端口（不碰 18888，见 §8.4.0 步骤 0 / STOP-13）。
  · 所有对 stdout 的判据都按**字节**读，不按文本行读 —— M6 的"无 \\r"用文本模式会被
    换行转译吃掉，那样测不到东西。
  · M8（真接本机 Claude Code 跑一次真下载）**不在这里**：那是"人在场"的活，
    按 §8.7 与 STOP-7 标 UNVERIFIED，我不拿自动化冒充实证。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import test_pkg_v03 as H                                    # noqa: E402
from test_pkg_v03 import PROJECT_ROOT, PY, ev               # noqa: E402
from chrome_downloader import daemon as dm                  # noqa: E402
from chrome_downloader import mcp_server as mcp             # noqa: E402
from chrome_downloader import paths                         # noqa: E402
from chrome_downloader.mock_server import MockServer, expected_sha256  # noqa: E402

MODERN = mcp.PROTOCOL_MODERN
LEGACY = mcp.PROTOCOL_LEGACY
TOOL_NAMES = [t["name"] for t in mcp.tool_defs()]


def meta(version: str = MODERN, **extra) -> dict:
    m = {mcp.META_VERSION_KEY: version,
         mcp.META_CLIENT_CAPS: {},
         mcp.META_CLIENT_INFO: {"name": "McpTestClient", "version": "0.0.1"}}
    m.update(extra)
    return m


def req(method, params=None, mid="r1", version=MODERN, with_meta=True):
    p = dict(params or {})
    if with_meta:
        p["_meta"] = meta(version)
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if with_meta or p:
        msg["params"] = p
    return msg


def notify(method, params=None):
    return {"jsonrpc": "2.0", "method": method, "params": params or {}}


class McpBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cdl_m_"))
        self.state = self.dir / "state"
        self.dl = self.dir / "files"
        self.mock = MockServer(0, str(self.dir / "mock.log")).start()
        self.pids: set[int] = set()

    def tearDown(self):
        for pid in list(self.pids):
            if pid != os.getpid() and dm.pid_alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=30)
        try:
            self.mock.stop()
        except Exception:
            pass
        tmproot = Path(tempfile.gettempdir()).resolve()
        if self.dir.name.startswith("cdl_m_") and \
                str(self.dir.resolve()).lower().startswith(str(tmproot).lower()):
            shutil.rmtree(self.dir, ignore_errors=True)

    # ---------- 起一条真 stdio 会话 ----------
    def session(self, msgs, *, env_extra=None, timeout=180):
        """把 msgs 逐行喂进 chrome-dl-mcp，按**字节**收回显行。

        返回 {"lines":[bytes], "parsed":[dict|None], "stdout":bytes, "stderr":str, "rc":int}
        """
        inp = b"".join(json.dumps(m, ensure_ascii=False).encode("utf-8") + b"\n"
                       for m in msgs)
        env = H.env_with_path(**(env_extra or {}))
        env.setdefault("CHROME_DL_STATE_DIR", str(self.state))
        p = subprocess.Popen([PY, "-m", "chrome_downloader.mcp_server"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=str(PROJECT_ROOT), env=env)
        out, err = p.communicate(inp, timeout=timeout)
        lines = out.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        parsed = []
        for ln in lines:
            try:
                parsed.append(json.loads(ln.decode("utf-8")))
            except Exception:
                parsed.append(None)
        return {"lines": lines, "parsed": parsed, "stdout": out, "stderr":
                err.decode("utf-8", "replace"), "rc": p.returncode,
                "rawLineCount": out.count(b"\n")}

    def live_bridge(self, *, with_pairing: bool = True) -> dict:
        """起桥（临时端口 + 临时 state-dir）。配对面按等价状态预置。"""
        self.state.mkdir(parents=True, exist_ok=True)
        if with_pairing:
            (self.state / "handshake.key").write_text("cd" * 32, encoding="ascii")
        res = dm.ensure_bridge(self.state, port=H.pick_bridge_port(),
                               download_root=self.dl, timeout=25, quiet=True)
        self.pids.add(int(res["endpoint"]["pid"]))
        return res


# ==========================================================================
class McpModernTests(McpBase):

    def test_M1_server_discover_shape(self):
        """M1：server/discover 含 2026-07-28 + resultType:"complete"（凭据 §存档 2.1/2.4/2.5）。"""
        r = self.session([req("server/discover", {}, "d1")])
        self.assertEqual(len(r["parsed"]), 1, r["stderr"][-400:])
        body = r["parsed"][0] or {}
        res = body.get("result") or {}
        self.assertEqual(r["rc"], 0, r["stderr"][-400:])
        need = {"resultType": "complete",
                "supportedVersions": [MODERN, LEGACY],
                "capabilities.tools": None, "ttlMs": None, "cacheScope": None}
        checks = {
            "jsonrpc2": body.get("jsonrpc") == "2.0",
            "idEchoed": body.get("id") == "d1",
            "resultTypeComplete": res.get("resultType") == "complete",
            "modernVersionListed": MODERN in (res.get("supportedVersions") or []),
            "legacyVersionListed": LEGACY in (res.get("supportedVersions") or []),
            "capabilitiesTools": isinstance(res.get("capabilities", {}).get("tools"), dict),
            "serverInfoInMeta": bool((res.get("_meta") or {}).get(mcp.META_SERVER_INFO)),
            "instructionsPresent": bool(res.get("instructions")),
            "ttlMsPresent": isinstance(res.get("ttlMs"), int),
            "cacheScopeValid": res.get("cacheScope") in ("public", "private"),
        }
        ok = all(checks.values())
        ev("M", "M1_discover.json",
           {"specAnchor": "docs/mcp-spec-archive.md §2.1 §2.4 §2.5（DiscoverResult 字段清单）",
            "response": res, "checks": checks,
            "verdict": "PASS" if ok else "FAIL"})
        self.assertEqual([k for k, v in checks.items() if not v], [], res)

    def test_M2_tools_list_is_byte_stable(self):
        """M2：tools/list 两次**逐字节相同**（确定性排序，不掺时间戳/随机 id）。"""
        # 判据口径：比 **result 的字节**，不比整行。
        # （第一版我拿两条不同 id 的整行比逐字节相同 —— 信封里带 JSON-RPC id，
        #   两次请求的 id 天生不同，那判据永远红，属"判据自身缺参"，不是产品不确定。）
        r = self.session([req("tools/list", {}, "tA"), req("tools/list", {}, "tB")])
        self.assertEqual(len(r["lines"]), 2, r["stderr"][-300:])
        b1 = json.loads(r["lines"][0]); b2 = json.loads(r["lines"][1])
        result_bytes_equal = mcp._dumps(b1["result"]) == mcp._dumps(b2["result"])
        # 再开一个**新会话**问第三遍：确认不是"同一进程内缓存出来的假确定"
        r3 = self.session([req("tools/list", {}, "tC"),
                           req("server/discover", {}, "tD"),
                           req("tools/list", {}, "tE")])
        bodies3 = [json.loads(ln) for ln in r3["lines"]]
        third = [x for x in bodies3 if x["id"] == "tE"][0]["result"]
        stable_across_sessions = mcp._dumps(third) == mcp._dumps(b1["result"])
        tools = b1["result"]["tools"]
        names = [t["name"] for t in tools]
        ok = result_bytes_equal and stable_across_sessions and names == TOOL_NAMES             and len(tools) == 8
        ev("M", "M2_tools_list_stable.json",
           {"resultBytesIdentical": result_bytes_equal,
            "identicalAcrossSessions": stable_across_sessions,
            "criterionCaliber": "比 result 字节；整行含 JSON-RPC id，两次请求 id 必然不同",
            "toolCount": len(tools), "order": names, "expectedOrder": TOOL_NAMES,
            "sha256Result1": hashlib.sha256(mcp._dumps(b1["result"])).hexdigest()[:16],
            "sha256Result2": hashlib.sha256(mcp._dumps(b2["result"])).hexdigest()[:16],
            "sha256Result3": hashlib.sha256(mcp._dumps(third)).hexdigest()[:16],
            "verdict": "PASS" if ok else "FAIL"})
        self.assertTrue(result_bytes_equal, "同一进程两次 result 不同 = 排序不确定")
        self.assertTrue(stable_across_sessions, "跨会话 result 不同 = 掺了会话态")
        self.assertEqual(names, TOOL_NAMES)

    def test_M3_unknown_version_is_32022_and_no_initialize_fallback(self):
        """M3（硬闸）：未知版本 → -32022 + data.supported 非空 + **不回退 initialize**。

        凭据 §存档 2.3（响应形状逐字照规范示例）与 §2.11（"Do **not** fall back to
        `initialize`"）。这条最容易被实现"好心"做成自动降级 —— 那是把可诊断的失败
        变成不可诊断的，规范明令禁止。
        """
        r = self.session([req("tools/list", {}, "v1", version="1900-01-01")])
        self.assertEqual(len(r["lines"]), 1, r["stdout"][:300])
        body = json.loads(r["lines"][0])
        err = body.get("error") or {}
        data = err.get("data") or {}
        blob = r["stdout"].decode("utf-8", "replace")
        checks = {
            "isErrorNotResult": "result" not in body,
            "code32022": err.get("code") == mcp.UNSUPPORTED_PROTOCOL_VERSION,
            "messageMatchesSpec": err.get("message") == "Unsupported protocol version",
            "supportedNonEmpty": bool(data.get("supported")),
            "supportedHasBoth": set(data.get("supported") or []) == {MODERN, LEGACY},
            "requestedEchoed": data.get("requested") == "1900-01-01",
            "noInitializeFallback": '"protocolVersion"' not in blob and
                                    "initialize" not in blob.lower(),
            "exactlyOneLine": r["rawLineCount"] == 1,
        }
        ok = all(checks.values())
        ev("M", "M3_unsupported_version.json",
           {"specAnchor": "docs/mcp-spec-archive.md §2.3（规范示例形状）+ §2.11（不回退）",
            "response": body, "checks": checks,
            "verdict": "PASS" if ok else "FAIL"})
        self.assertEqual([k for k, v in checks.items() if not v], [], body)

    def test_M6_stdout_is_line_json_and_has_no_cr(self):
        """M6（硬闸）：stdout 逐行 json.loads 全成功，且**一个 \\r 都没有**。

        \\r 是本判据的真正杀点：Windows 文本模式 print() 会写成 \\r\\n，
        规范说消息"不得含嵌入换行"，\\r 就是脏字节（§存档 2.8）。
        """
        msgs = [req("server/discover", {}, "a1"),
                req("tools/list", {}, "a2"),
                req("tools/call", {"name": "chrome_download_status", "arguments": {}}, "a3"),
                notify("notifications/initialized", {}),
                req("bogus/method", {}, "a4"),
                {"jsonrpc": "2.0", "id": "a5", "method": "tools/call",
                 "params": {"_meta": meta(), "name": "chrome_download",
                            "arguments": {"urls": ["file:///c:/Windows/notepad.exe"]}}}]
        r = self.session(msgs)
        raw = r["stdout"]
        lines = [ln for ln in raw.split(b"\n") if ln]
        parsed = [json.loads(ln.decode("utf-8")) for ln in lines] if all(
            _is_json(ln) for ln in lines) else []
        checks = {
            "noCarriageReturn": b"\r" not in raw,
            "everyLineParses": len(parsed) == len(lines),
            "lineCountMatchesRequests": len(lines) == 5,      # 通知不回话
            "noEmbeddedNewline": all(b"\n" not in ln for ln in lines),
            "returncodeZero": r["rc"] == 0,
        }
        ok = all(checks.values())
        ev("M", "M6_stdout_clean.json",
           {"specAnchor": "docs/mcp-spec-archive.md §2.8",
            "stdoutBytes": len(raw), "lineCount": len(lines),
            "crCount": raw.count(b"\r"), "checks": checks,
            "ids": [p.get("id") for p in parsed],
            "verdict": "PASS" if ok else "FAIL"})
        self.assertEqual(raw.count(b"\r"), 0, "stdout 里有 \\r —— Windows 文本模式泄漏")
        self.assertEqual(len(parsed), len(lines), "有行不是合法 JSON")
        self.assertEqual(len(lines), 5, f"通知不该回话/请求都该回话：{len(lines)} 行")

    def test_M7_logs_never_on_stdout(self):
        """M7：日志字样只出现在 stderr，stdout 上没有。"""
        # 故意制造一条内部错误（坏参 + 未知工具），再看 stdout 是否干净
        r = self.session([req("tools/call", {"name": "no_such_tool", "arguments": {}}, "e1"),
                          {"jsonrpc": "2.0", "id": "e2", "method": "tools/call",
                           "params": {"_meta": meta(), "name": "chrome_download",
                                      "arguments": {"urls": "不是数组"}}},
                          req("server/discover", {}, "e3")])
        stdout = r["stdout"].decode("utf-8", "replace")
        markers = ("[mcp]", "INFO", "WARNING", "ERROR:", "Traceback", "Exception")
        leaked = [m for m in markers if m in stdout]
        parsed_ok = all(_is_json(ln) for ln in r["lines"] if ln)
        checks = {"stdoutHasNoLogMarkers": not leaked,
                  "everyStdoutLineIsJson": parsed_ok,
                  "stderrMayLog": True}
        ev("M", "M7_logs_to_stderr.json",
           {"stdoutMarkersFound": leaked, "stderrHead": r["stderr"][:300],
            "checks": checks,
            "verdict": "PASS" if all(checks.values()) else "FAIL"})
        self.assertEqual(leaked, [], f"日志漏进 stdout：{leaked}")

    def test_M9_unknown_tool_and_bad_params(self):
        """M9：未知工具/坏参 —— 找不到工具属协议级错误 -32601；参数不对 -32602。"""
        r = self.session([
            req("tools/call", {"name": "definitely_not_a_tool", "arguments": {}}, "k1"),
            {"jsonrpc": "2.0", "id": "k2", "method": "tools/call",
             "params": {"_meta": meta(), "name": "chrome_download_progress",
                        "arguments": {}}},                       # 缺 jobId
            {"jsonrpc": "2.0", "id": "k3", "method": "tools/call",
             "params": {"_meta": meta()}},                       # 缺 name
        ])
        got = {json.loads(ln)["id"]: json.loads(ln) for ln in r["lines"] if ln}
        checks = {
            "unknownToolIsProtocolError": "error" in got["k1"] and
                                          got["k1"]["error"]["code"] == -32601,
            "missingRequiredArgIsToolError": "result" in got["k2"] and
                                             got["k2"]["result"].get("isError") is True,
            "missingToolNameIsProtocolError": "error" in got["k3"] and
                                              got["k3"]["error"]["code"] == -32602,
        }
        ok = all(checks.values())
        ev("M", "M9_unknown_tool_bad_params.json",
           {"specAnchor": "docs/mcp-spec-archive.md §2.9：工具内错误走 isError，"
                          "'找不到工具/不支持工具调用'走协议级错误",
            "responses": {k: (v.get("error") or v.get("result")) for k, v in got.items()},
            "checks": checks, "verdict": "PASS" if ok else "FAIL"})
        self.assertEqual([k for k, v in checks.items() if not v], [], got)

    def test_M10_file_scheme_no_job_created(self):
        """M10：file:// → isError（或协议错）且**未产生任务**。"""
        self.live_bridge()
        before = len(dm.status(self.state).get("port") and [])
        env = {"CHROME_DL_STATE_DIR": str(self.state)}
        r = self.session([
            req("tools/call", {"name": "chrome_download",
                               "arguments": {"urls": ["file:///c:/Windows/system32/cmd.exe"]}},
                "f1")], env_extra=env)
        body = json.loads(r["lines"][0])
        is_tool_error = "result" in body and body["result"].get("isError") is True
        is_protocol_error = "error" in body
        # 真查账本：不许悄悄留下一个 PENDING 任务
        try:
            jobs = dm.make_client(self.state, ensure=False).list()
        except Exception:
            jobs = []
        out = {"response": body, "isErrorPath": is_tool_error,
               "protocolErrorPath": is_protocol_error,
               "jobsInLedgerAfter": len(jobs),
               "verdict": "PASS" if ((is_tool_error or is_protocol_error) and
                                     len(jobs) == 0) else "FAIL"}
        ev("M", "M10_file_scheme_no_job.json", out)
        self.assertTrue(is_tool_error or is_protocol_error, body)
        self.assertEqual(len(jobs), 0, f"非 http(s) 也建了任务：{jobs}")
        self.assertFalse(bool(before))

    def test_M11_large_ledger_lines_stay_under_8192(self):
        """M11（硬闸）：≥500 条 job 的大账本，每次 tools/call 响应单行 ≤8192 字节；
        超限必须被截断并返回 truncated + totalCount + omitted。"""
        self.live_bridge()
        c = dm.make_client(self.state, ensure=False)
        seeded = 0
        for i in range(560):
            c.add(self.mock.url(f"big{i}", 1000), filename=f"big{i}.bin")
            seeded += 1
        env = {"CHROME_DL_STATE_DIR": str(self.state)}
        calls = [
            ("list 全部", req("tools/call", {"name": "chrome_download_list",
                                             "arguments": {}}, "b1")),
            ("list 只要进行中", req("tools/call", {"name": "chrome_download_list",
                                                  "arguments": {"includeTerminal": False}},
                                   "b2")),
            ("status", req("tools/call", {"name": "chrome_download_status",
                                          "arguments": {}}, "b3")),
            ("progress 单个", req("tools/call", {"name": "chrome_download_progress",
                                                "arguments": {"jobId": "deadbeef00000000"}},
                                 "b4")),
        ]
        r = self.session([m for _, m in calls], env_extra=env, timeout=300)
        rows, bad = [], []
        for ln in r["lines"]:
            body = json.loads(ln.decode("utf-8"))
            res = body.get("result") or {}
            too_big = len(ln) > mcp.MAX_LINE_BYTES
            truncated = bool(res.get("truncated"))
            has_meta = all(k in res for k in ("totalCount", "omitted")) if truncated else True
            rows.append({"id": body.get("id"), "bytes": len(ln),
                         "truncated": truncated,
                         "totalCount": res.get("totalCount"),
                         "omitted": res.get("omitted"),
                         "shownCount": (res.get("structuredContent") or {}).get("count"),
                         "hasTotalAndOmitted": has_meta})
            if too_big or not has_meta:
                bad.append(rows[-1])
        listed = [x for x in rows if x["truncated"]]
        checks = {
            "seededAtLeast500": seeded >= 500,
            "everyLineWithin8192": all(x["bytes"] <= mcp.MAX_LINE_BYTES for x in rows),
            "oversizeWasTruncated": bool(listed),
            "truncatedCarriesTotalAndOmitted": all(
                x["hasTotalAndOmitted"] for x in listed),
            "allFourRepliesPresent": len(rows) == 4,
        }
        ok = all(checks.values()) and not bad
        ev("M", "M11_byte_budget.json",
           {"budget": mcp.MAX_LINE_BYTES, "seededJobs": seeded, "rows": rows,
            "truncatedCount": len(listed), "checks": checks,
            "specAnchor": "计划 §3.3 响应字节预算 + C6 第三段（先算 structuredContent，"
                          "余额给 text；超限服务端截断而非发出）",
            "verdict": "PASS" if ok else "FAIL"})
        self.assertTrue(checks["everyLineWithin8192"], rows)
        self.assertTrue(checks["oversizeWasTruncated"],
                        "560 条账本还没触发截断 = 预算闸是空的")
        self.assertTrue(checks["truncatedCarriesTotalAndOmitted"], rows)

    def test_M12_error_code_whitelist_positive_assertion(self):
        """M12：穷举允许发出的码 {-32700,-32600,-32601,-32602,-32603,-32022}，
        逐条触发一次，断言**实际发出集合 == 白名单**（审查员要的肯定断言）。"""
        wire = [
            "这不是 JSON{{{".encode("utf-8"),           # -32700 坏 JSON
            json.dumps({"jsonrpc": "2.0", "id": "c0",  # -32600 缺 method
                        "params": {"_meta": meta()}},
                       ensure_ascii=False).encode("utf-8"),
            json.dumps(req("no/suchMethod", {}, "c1"),  # -32601 未知方法
                       ensure_ascii=False).encode("utf-8"),
            json.dumps({"jsonrpc": "2.0", "id": "c2",   # -32602 tools/call 缺 name
                        "method": "tools/call",
                        "params": {"_meta": meta()}},
                       ensure_ascii=False).encode("utf-8"),
            json.dumps(req("tools/list", {}, "c3",      # -32022 版本不认识
                           version="1999-12-31"),
                       ensure_ascii=False).encode("utf-8"),
        ]
        payload = b"\n".join(wire) + b"\n"
        env = H.env_with_path()
        env["CHROME_DL_STATE_DIR"] = str(self.state)
        p = subprocess.Popen([PY, "-m", "chrome_downloader.mcp_server"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=str(PROJECT_ROOT), env=env)
        out, err = p.communicate(payload, timeout=180)
        codes = []
        for ln in out.split(b"\n"):
            if not ln:
                continue
            body = json.loads(ln.decode("utf-8"))
            if "error" in body:
                codes.append(body["error"]["code"])
        got = set(codes)
        expected = set(mcp.ALLOWED_ERROR_CODES)
        # -32603（内部错误）**没法从外部输入造出来**：坏参的地方都写成了 isError 或
        # -32601/-32602。计划要求"逐条触发一次"，所以这里用受控注入：把工具实现
        # 临时换成一定抛异常的桩，看服务端是否如实回 -32603（而不是吐 traceback、
        # 也不是憋着不回话）。证据里明写"这是注入，不是产品路径"。
        sess = mcp.Session()
        real = mcp.call_tool
        try:
            def _boom(name, args):
                raise RuntimeError("注入的内部故障：只为触发 -32603")
            mcp.call_tool = _boom
            injected = mcp.dispatch(json.dumps(req(
                "tools/call", {"name": "chrome_download_status", "arguments": {}},
                "i1")).encode("utf-8"), sess)
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
        ok = (checks["subsetOfWhitelist"] and checks["allSixTriggered"] and
              checks["noCustomCodeInRange"])
        ev("M", "M12_error_code_whitelist.json",
           {"whitelist": sorted(expected),
            "specAnchor": "docs/mcp-spec-archive.md §2.10（-32020/-32021 归 HTTP/能力协商，"
                          "stdio 上不发；不许在 -32020..-32099 自造）",
            "emittedCodes": sorted(got), "checks": checks,
            "emittedCodes": sorted(got), "allSeen": sorted(seen), "checks": checks,
            "injectionIsNotAProductPath":
                "-32603 由测试内注入的异常触发（把 call_tool 换成会抛的桩），"
                "证的是服务端对意外异常的回话形状；产品里正常请求不该走到那条",
            "note": "肯定断言 = 实际发出的码必须 ⊆ 白名单，且白名单六条要逐条真发出来"
                    "（五条走外部输入，-32603 走受控注入）",
            "verdict": "PASS" if ok else "FAIL"})
        self.assertTrue(checks["subsetOfWhitelist"],
                        f"发出了白名单外的码：{sorted(seen - expected)}")
        self.assertTrue(checks["allSixTriggered"],
                        f"六条没发全：{sorted(seen)} 缺 {sorted(expected - seen)}")

    def test_M13_all_tools_carry_cold_start_truth(self):
        """M13：8 个工具的 description 全部含冷启动实话，命中必须 8/8。"""
        tools = mcp.tool_defs()
        needle = mcp.COLD_START_TRUTH
        hits = {t["name"]: (needle in (t.get("description") or "")) for t in tools}
        n_ok = sum(1 for v in hits.values() if v)
        out = {"needle": needle, "hits": hits, "hitCount": f"{n_ok}/{len(tools)}",
               "descriptions": {t["name"]: t["description"] for t in tools},
               "verdict": "PASS" if n_ok == len(tools) == 8 else "FAIL"}
        ev("M", "M13_cold_start_truth_in_descriptions.json", out)
        self.assertEqual(n_ok, 8, hits)
        self.assertEqual(len(tools), 8, "工具数不是 8")


class McpLegacyTests(McpBase):

    def test_M4_legacy_initialize_echoes_2025_11_25_and_no_result_type(self):
        """M4（硬闸）：旧时代 initialize → 回显 protocolVersion（钉 2025-11-25）+
        capabilities.tools，随后 tools/list / tools/call **不带 resultType**。

        凭据 §存档 2.6（legacy = 2025-11-25 and earlier）、§2.12（InitializeResult 形状）、
        §2.5（旧时代 Result 没有 resultType 字段）。
        """
        init = {"jsonrpc": "2.0", "id": "i1", "method": "initialize",
                "params": {"protocolVersion": LEGACY, "capabilities": {},
                           "clientInfo": {"name": "LegacyClient", "version": "1.0"}}}
        r = self.session([init,
                          {"jsonrpc": "2.0", "id": "i2", "method": "tools/list",
                           "params": {}},
                          {"jsonrpc": "2.0", "id": "i3", "method": "tools/call",
                           "params": {"name": "chrome_download_status",
                                      "arguments": {}}},
                          notify("notifications/initialized", {})])
        bodies = [json.loads(ln) for ln in r["lines"] if ln]
        by_id = {b["id"]: b for b in bodies}
        ir = by_id["i1"].get("result") or {}
        checks = {
            "initializeEchoesLegacyVersion": ir.get("protocolVersion") == LEGACY,
            "capabilitiesToolsPresent": isinstance(
                ir.get("capabilities", {}).get("tools"), dict),
            "serverInfoPresent": bool(ir.get("serverInfo", {}).get("name")),
            "toolsListHasNoResultType": "resultType" not in (by_id["i2"].get("result") or {}),
            "toolsListHasTools": bool((by_id["i2"].get("result") or {}).get("tools")),
            "toolsCallHasNoResultType": "resultType" not in (by_id["i3"].get("result") or {}),
            "toolsCallContentPresent": bool((by_id["i3"].get("result") or {}).get("content")),
            "notificationGotNoReply": len(bodies) == 3,
        }
        ok = all(checks.values())
        ev("M", "M4_legacy_initialize.json",
           {"specAnchor": "docs/mcp-spec-archive.md §2.5 §2.6 §2.12",
            "initializeResult": ir, "checks": checks,
            "verdict": "PASS" if ok else "FAIL"})
        self.assertEqual([k for k, v in checks.items() if not v], [], by_id)

    def test_M4b_dual_era_modern_after_legacy_without_handshake(self):
        """双时代核对：同一进程里旧 initialize 之后，现代请求仍按现代面答（带 resultType）。

        规范 §存档 2.7："A dual-era server **MAY** serve both eras concurrently"。
        """
        init = {"jsonrpc": "2.0", "id": "x1", "method": "initialize",
                "params": {"protocolVersion": LEGACY, "capabilities": {},
                           "clientInfo": {"name": "Dual", "version": "1"}}}
        r = self.session([init, req("tools/list", {}, "x2")])
        bodies = [json.loads(ln) for ln in r["lines"] if ln]
        by_id = {b["id"]: b for b in bodies}
        checks = {"legacyInitialize": by_id["x1"]["result"].get("protocolVersion") == LEGACY,
                  "modernToolsListHasResultType":
                      by_id["x2"]["result"].get("resultType") == "complete"}
        ev("M", "M4b_dual_era.json",
           {"checks": checks, "note": "这条不是新增判据，是把 M1/M4 放在同一进程里跑一遍，"
                                       "证明双时代不是两套代码",
            "verdict": "PASS" if all(checks.values()) else "FAIL"})
        self.assertTrue(all(checks.values()), by_id)


class McpRealDownloadTests(McpBase):

    def test_M5_real_download_via_mcp_completes(self):
        """M5：chrome_download 真下一次 → structuredContent.jobId + 中文 text。

        顺序要点：三级就绪里的 L3 是"配对面就绪"，而密钥 K 是**扩展第一次连上来**时
        交出的（TOFU）。所以起桥要放线程里跑、桥一露头就把链路层对手接上去；
        先阻塞式等"就绪"会永远等不到 —— 那是产品在如实报"还没有配对面"，不是 bug。
        """
        self.state.mkdir(parents=True, exist_ok=True)
        box: dict = {}
        th = threading.Thread(target=lambda: box.update(
            res=dm.ensure_bridge(self.state, port=H.pick_bridge_port(),
                                 download_root=self.dl, timeout=30, quiet=True)))
        th.start()
        info = None
        t0 = time.time()
        while time.time() - t0 < 20:
            info = paths.read_endpoint(self.state)
            if info:
                break
            time.sleep(0.1)
        self.assertIsNotNone(info, "桥没起来")
        fake = H.start_fake_chrome(self.state, self.dl)
        th.join(120)
        self.assertIsNotNone(box.get("res"), f"三级就绪没达成：{box.get('err')}")
        self.pids.add(int(box["res"]["endpoint"]["pid"]))
        try:
            name, size = "m5blob", 300_000
            url = self.mock.url(name, size, rate=200_000)
            sha = expected_sha256(name, size)
            env = H.env_with_path()
            env["CHROME_DL_STATE_DIR"] = str(self.state)
            r = self.session([
                req("tools/call", {"name": "chrome_download",
                                   "arguments": {"urls": [url], "filename": "m5.bin",
                                                 "sha256": sha, "wait": True,
                                                 "timeout_sec": 120}}, "dl1"),
            ], env_extra=env, timeout=300)
            body = json.loads(r["lines"][0])
            res_body = body.get("result") or {}
            sc = res_body.get("structuredContent") or {}
            jobs = sc.get("jobs") or []
            text = "".join(c.get("text", "") for c in res_body.get("content") or [])
            checks = {
                "jobIdPresent": bool(sc.get("jobId") or [j.get("jobId") for j in jobs]),
                "chineseTextPresent": bool(re.search(r"[\u4e00-\u9fff]", text)),
                "stateComplete": jobs and jobs[0].get("state") == "COMPLETE",
                "sha256Ok": jobs and jobs[0].get("sha256Actual") == "OK",
                "isErrorFalse": res_body.get("isError") is False,
                "lineWithinBudget": len(r["lines"][0]) <= mcp.MAX_LINE_BYTES,
            }
            ok = all(checks.values())
            ev("M", "M5_real_download.json",
               {"text": text, "structuredKeys": sorted(sc),
                "job": jobs[0] if jobs else None, "checks": checks,
                "ledgerFile": str(self.dl / "m5.bin"),
                "fileOnDisk": (self.dl / "m5.bin").exists(),
                "verdict": "PASS" if ok else "FAIL"})
            self.assertEqual([k for k, v in checks.items() if not v], [], res_body)
        finally:
            if fake is not None:
                try:
                    fake.terminate()
                    fake.communicate(timeout=8)
                except Exception:
                    try:
                        fake.kill()
                    except Exception:
                        pass


def _is_json(line: bytes) -> bool:
    try:
        json.loads(line.decode("utf-8"))
        return True
    except Exception:
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
