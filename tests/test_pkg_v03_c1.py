#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pkg_v03_c1.py —— C1 不变式在**新增面**上的静态核（计划 §2 C1 后半句）

C1 原文要求：「100% 文件字节仍由浏览器 chrome.downloads 发起；Bridge/Client/CLI/MCP/Daemon
五方都不得成为字节通道」，并点名「新增：daemon.py 与 mcp_server.py 两个新模块的静态扫描
**0 命中**取体类调用」。

为什么不把新模块塞进 test_downloader.py 的 AUDIT_SCOPE：
  AUDIT_SCOPE 的值会被写进 `tests/evidence/V4a/V4a_static_scan.json` 的 `scope` 字段。
  V4a 属 §8.4.1 的 IMMUTABLE 目录，而 D 口径**保留数组长度**（§8.4.2.1 第 2 条），
  改长度必然改规范化 sha256 → 直接撞 (b2)/STOP-5。计划 §8.4.3 给的出路是
  "scope 扩容属预期新增面，记入 changed[] 并说明原因"，但那要求我**改**IMMUTABLE 证据，
  比另开一个判据目录更脏。所以：
    · 搬运件（bridge.py / client.py）的扫描留在 v0.2 的原判据里，一字不动；
    · 新增模块（daemon/cli/mcp_server/paths/common/__main__/壳）在这里扫，
      证据落 `C1x/`（§8.4.1 NOT-IMMUTABLE 的新增面）。
  两条合起来覆盖"五方"，这才是 C1 的原意。

取体特征词表**写在本文件里**，而本文件不在被扫描的范围内 ——
这与 v0.2 的 FORBIDDEN_STATIC 同一处置（test_downloader.py 自己也不在 AUDIT_SCOPE 里）。
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
PROJECT_ROOT = TESTS.parent
SRC = PROJECT_ROOT / "src"

import importlib.util                          # noqa: E402
_spec = importlib.util.find_spec("chrome_downloader")
DELIVERY_ROOT = Path(_spec.origin).resolve().parent

from test_pkg_v03 import ev                    # noqa: E402

# 新增模块清单 = 包目录里**不是搬运件**的那些 .py
CARRIED = {"bridge.py", "client.py", "check_env.py", "mock_server.py"}


def new_modules() -> list[str]:
    return sorted(p.name for p in DELIVERY_ROOT.glob("*.py")
                  if p.name not in CARRIED)


# C1 的"取体类"特征（与 v0.2 的 FORBIDDEN_STATIC 同口径，另加 socket 出站）
FORBIDDEN = [
    ("urllib.request", r"\burllib\.request\b|from\s+urllib\.request"),
    ("requests", r"\bimport\s+requests\b|\brequests\.(?:get|post|Session)"),
    ("httpx", r"\bimport\s+httpx\b|\bhttpx\."),
    ("aiohttp.ClientSession", r"\bClientSession\b"),
    ("urlopen", r"\burlopen\s*\("),
    ("pycurl", r"\bpycurl\b"),
    ("http.client（直接建连）", r"\bhttp\.client\b|from\s+http\s+import\s+client"),
    ("socket 出站", r"\bsocket\.(?:socket|create_connection)\b"),
    ("curl/wget 取数据", r"\b(?:subprocess\.\w+|os\.system|os\.exec\w*)\s*\([^)\n]*"
                        r"(?:curl|wget)"),
    ("playwright/selenium/pyppeteer", r"playwright|selenium|pyppeteer"),
]
BANNED_IMPORTS = ("requests", "httpx", "pycurl", "selenium", "playwright",
                  "urllib3", "aiohttp", "pyppeteer")


class C1NewModulesTests(unittest.TestCase):

    def test_C1_new_modules_are_byte_channel_free(self):
        """C1：新增的 5 个模块（daemon/cli/mcp_server/paths/common）静态扫描 0 命中。"""
        mods = new_modules()
        hits: list[dict] = []
        for name in mods:
            p = DELIVERY_ROOT / name
            src = p.read_text(encoding="utf-8")
            lines = src.splitlines()
            imported: set[str] = set()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            for label, pat in FORBIDDEN:
                for i, line in enumerate(lines, 1):
                    # 注释/文档字符串里提到词不算取体调用，但为了不做"自己判自己宽"，
                    # 这里只放行显式豁免标记 noqa: byte-channel
                    if re.search(pat, line) and "noqa: byte-channel" not in line:
                        hits.append({"file": name, "line": i, "rule": label,
                                     "text": line.strip()[:120]})
            for banned in BANNED_IMPORTS:
                if banned in imported:
                    hits.append({"file": name, "rule": f"import:{banned}", "line": 0,
                                 "text": ""})
        verdict = "PASS" if not hits else "FAIL"
        ev("C1x", "C1x_new_modules_static_scan.json",
           {"scope": mods, "carriedModulesExcluded": sorted(CARRIED),
            "hits": hits, "rules_library": [r[0] for r in FORBIDDEN],
            "bannedImports": list(BANNED_IMPORTS),
            "whyAnotherEvidenceDir":
                "V4a/V4a_static_scan.json 属 §8.4.1 IMMUTABLE，且 D 口径保留数组长度；"
                "把新模块塞进 AUDIT_SCOPE 会改那文件的规范化 sha256 → 撞 (b2)/STOP-5。"
                "故搬运件扫描留在 v0.2 原判据，新模块扫描落 C1x（新增面），"
                "两条合起来才覆盖 C1 说的'五方'",
            "byteChannelPathNote": "新增模块要连本机桥时一律复用搬运件 client.Client"
                                   "（内部有 _assert_loopback），不另起网络路径",
            "verdict": verdict})
        self.assertEqual(hits, [], json.dumps(hits, ensure_ascii=False)[:900])

    def test_C1_only_client_touches_loopback_http(self):
        """C1 的结构面：新增模块里不许出现任何 HTTP/socket 客户端，只许"借用"搬运件。"""
        mods = new_modules()
        offenders = []
        for name in mods:
            src = (DELIVERY_ROOT / name).read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mod = getattr(node, "module", None) or ""
                    for a in getattr(node, "names", []):
                        full = mod or a.name
                        if full.split(".")[0] in ("http", "socket", "urllib", "aiohttp",
                                                  "ssl"):
                            offenders.append({"file": name, "import": full})
        # 允许：daemon.py 归因/进程扫描要用 subprocess（不是网络），不含 http/socket
        ev("C1x", "C1x_new_module_import_graph.json",
           {"modules": mods, "networkImportsFound": offenders,
           "assertion": "新增模块不 import 任何 http/socket/urllib/aiohttp —— "
                        "与桥的对话全部经搬运件 client.Client",
            "verdict": "PASS" if not offenders else "FAIL"})
        self.assertEqual(offenders, [], json.dumps(offenders, ensure_ascii=False))

    def test_C1_extension_unchanged_and_bytes_still_browser_only(self):
        """C1 的另一半：搬运进包的扩展**逐字节没变**，且仍只走 chrome.downloads。"""
        import hashlib
        orig = Path("D:/article4/scratch/chrome_downloader/extension")
        new = DELIVERY_ROOT / "extension"
        rows = {}
        for f in sorted(p.name for p in new.glob("*")):
            a = (orig / f).read_bytes()
            b = (new / f).read_bytes()
            rows[f] = {"sha256Orig": hashlib.sha256(a).hexdigest()[:16],
                       "sha256Pkg": hashlib.sha256(b).hexdigest()[:16],
                       "byteIdentical": a == b}
        bg = (new / "background.js").read_text(encoding="utf-8")
        static_ok = ("XMLHttpRequest" not in bg and "new EventSource" not in bg and
                     re.search(r"chrome\.downloads\.download\(\{\s*url,", bg))
        manifest = json.loads((new / "manifest.json").read_text(encoding="utf-8"))
        perms_ok = (set(manifest["permissions"]) == {"downloads", "alarms", "storage"} and
                    "host_permissions" not in manifest and
                    int(str(manifest["minimum_chrome_version"]).split(".")[0]) >= 116)
        all_identical = all(v["byteIdentical"] for v in rows.values())
        out = {"extensionFiles": rows, "allByteIdentical": all_identical,
               "backgroundStaticOk": bool(static_ok), "manifestPermsOk": perms_ok,
               "note": "W1/W17 仍由 v0.2 原判据从包内 manifest 读并断言（C5）；"
                       "本条只核『搬运没改字节』这一半",
               "verdict": "PASS" if (all_identical and static_ok and perms_ok)
               else "FAIL"}
        ev("C1x", "C1x_extension_bytes_unchanged.json", out)
        self.assertTrue(all_identical, out)
        self.assertTrue(static_ok, out)
        self.assertTrue(perms_ok, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
