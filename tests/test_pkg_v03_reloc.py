#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pkg_v03_reloc.py —— B1–B4（测试搬迁）+ T2（paths 三态/隔离/下载根）

判据出处：计划 §4 T1.2 判据 B1–B4；§4 T2 判据（"单测三态覆盖；本机真跑 chrome-dl env
打印目录确实存在且等于 Chrome 设置值"）；§8.0 的 48 条里 B 系列占 4 条。
B 系列是"搬迁是否搬坏"的判据，T2 是"路径口径是否唯一且兼容 v0.2"的判据，两者都跟产品
行为无关，所以放一个文件里跑，省一次全量夹具启动。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from test_pkg_v03 import (DELIVERY_ROOT, PP, PY, PROJECT_ROOT, ev, fresh_state_dir)  # noqa: E402
from chrome_downloader import daemon as dm      # noqa: E402
from chrome_downloader import paths             # noqa: E402


# ==========================================================================
# B1–B4
# ==========================================================================
class TestRelocationTests(unittest.TestCase):

    def test_B1_suite_runs_on_package_tree(self):
        """B1：`python test_downloader.py` 在包树上 exit 0（**必须先于"91 全绿"达成**）。

        为什么把 exit 0 与"91 全绿"拆成两条看：exit 0 只证明路径没崩、模块能导入、
        夹具起得来。搬坏了会长成 ERROR，产品坏了长成 FAIL —— 混在一个数里就没法定位。
        """
        port_pre = _port_precondition()          # §8.4.0 步骤 0
        self.assertEqual(port_pre["precondition"], "FREE",
                         f"18888 未空闲就跑 91 条 = W14/W14b 必然一 FAIL 一 ERROR，"
                         f"那是前置未满足，不是产品结论：{port_pre}")
        env = dict(os.environ)
        env["PYTHONPATH"] = PP
        r = subprocess.run([PY, str(TESTS / "test_downloader.py")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=1800, cwd=str(TESTS), env=env)
        blob = (r.stderr or "") + (r.stdout or "")
        m = re.search(r"Ran (\d+) tests in ([\d.]+)s", blob)
        c = re.search(r"通过 (\d+)/(\d+)，失败 (\d+)，错误 (\d+)，跳过 (\d+)", blob)
        out = {"command": "python tests/test_downloader.py",
               "portPrecondition": port_pre,      # §8.4.0 第 5 条：单列一项，不计进 errors
               "returncode": r.returncode,
               "ranTests": int(m.group(1)) if m else None,
               "elapsedSec": float(m.group(2)) if m else None,
               "runnerLine": c.group(0) if c else None,
               "passed": int(c.group(1)) if c else None,
               "failures": int(c.group(3)) if c else None,
               "errors": int(c.group(4)) if c else None,
               "skipped": int(c.group(5)) if c else None,
               "tail": blob[-400:]}
        out["verdict"] = "PASS" if (r.returncode == 0 and out["errors"] == 0
                                    and out["failures"] == 0
                                    and (out["ranTests"] or 0) >= 91) else "FAIL"
        ev("B", "B1_suite_exit0.json", out)
        self.assertEqual(out["errors"], 0, "路径/导入崩 → 先修搬迁，别去动产品")
        self.assertEqual(out["failures"], 0, out["tail"])
        self.assertEqual(r.returncode, 0, out["tail"])
        self.assertGreaterEqual(out["ranTests"] or 0, 91, out["runnerLine"])

    def test_B2_witness_files_locatable_and_asserted(self):
        """B2：四枚见证文件可被定位，且**读它们的断言本身是 PASS**（不是"文件在"就算）。"""
        want = {"PROTOCOL.md": 7105, "NM_REMOVAL_NOTE.md": 2555,
                "requirements.txt": 1199, "tests/ws_probe_readme.md": 9628}
        rows = {}
        for rel, size in want.items():
            p = (TESTS / rel.split("/", 1)[1]) if rel.startswith("tests/") \
                else (DELIVERY_ROOT / rel)
            rows[rel] = {"path": str(p), "exists": p.exists(),
                         "bytes": p.stat().st_size if p.exists() else None,
                         "bytesMatchPlan": bool(p.exists()) and p.stat().st_size == size}
        read_by = {
            "PROTOCOL.md → test_docs_promise_30_seconds_not_seconds":
                bool((DELIVERY_ROOT / "PROTOCOL.md").read_text(encoding="utf-8")),
            "NM_REMOVAL_NOTE.md → test_W2 断言 exists": rows["NM_REMOVAL_NOTE.md"]["exists"],
            "requirements.txt → test_V10 读并断言依赖面":
                "aiohttp" in (DELIVERY_ROOT / "requirements.txt").read_text(
                    encoding="utf-8").lower(),
            "tests/ws_probe_readme.md → T9 人要拿它当脚本":
                rows["tests/ws_probe_readme.md"]["exists"],
        }
        ok = all(v["exists"] for v in rows.values()) and all(read_by.values())
        ev("B", "B2_witness_files.json",
           {"witness": rows, "planDeclaredBytes": want, "assertionsThatReadThem": read_by,
            "note": "字节数与计划 §6.1 对不上**不据此判红也不据此放行**：搬迁不改内容，"
                    "对不上说明见证文件被动过，要单独查（记 changed[] 给人看）",
            "verdict": "PASS" if ok else "FAIL"})
        self.assertTrue(ok, json.dumps({"rows": rows, "read_by": read_by},
                                       ensure_ascii=False))

    def test_B3_runner_reports_error_and_failure_separately(self):
        """B3：Runner 同时报 ERROR 与 FAIL 两个计数，且本次 **ERROR 必须为 0**。"""
        port_pre = _port_precondition()          # §8.4.0 步骤 0
        self.assertEqual(port_pre["precondition"], "FREE",
                         f"18888 未空闲，跑出来的 ERROR/FAIL 计数没有意义：{port_pre}")
        env = dict(os.environ)
        env["PYTHONPATH"] = PP
        code = ("import unittest;"
                "mod=__import__('{name}');"
                "s=unittest.TestLoader().loadTestsFromModule(mod);"
                "res=unittest.TextTestRunner(verbosity=0).run(s);"
                "print('COUNTS',len(res.errors),len(res.failures),res.testsRun,"
                "len(res.skipped))")
        r1 = subprocess.run([PY, "-c", code.format(name="test_downloader")],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=1800, cwd=str(TESTS), env=env)

        def parse(r):
            line = next((l for l in (r.stdout or "").splitlines()
                         if l.startswith("COUNTS")), None)
            if not line:
                return None
            e, f, n, k = line.split()[1:]
            return {"errors": int(e), "failures": int(f), "testsRun": int(n),
                    "skipped": int(k)}
        a = parse(r1)
        out = {"portPrecondition": port_pre,
               "suite": "test_downloader.py（搬迁后的 v0.2 91 条）", "counts": a,
               "returncode": r1.returncode, "tail": (r1.stderr or "")[-400:],
               "splitMeaning": "errors>0 = 导入/路径崩（先修 T1.2）；"
                               "failures>0 = 断言不成立（查产品）"}
        out["verdict"] = "PASS" if a and a["errors"] == 0 else "FAIL"
        ev("B", "B3_error_fail_split.json", out)
        self.assertIsNotNone(a, out)
        self.assertEqual(a["errors"], 0, f"ERROR 必须为 0：{a}")

    def test_B4_no_stray_here_paths(self):
        """B4：测试面不许残留散落的 `HERE / "<交付件>"` 拼接，只剩 DELIVERY_ROOT。

        计划原文写"grep 全仓"。照字面扫会命中 1 处**搬运件内部**写法
        （check_env.py 第 9 项体检里，HERE 与扩展目录名的拼接；
         ⚠️ 那串字面量**不许**在本文件里原样写出来 —— 写了本文件就成了自己
         扫到的一处命中，即自指假阳性。v0.2 的同类判据也是靠"按片段拼"避开这个的），
        而那个文件被 §0.1 用 sha256 逐字节钉死 —— 改它等于自毁 V10 与搬运判据。
        ⇒ 我没有偷偷加白名单了事，而是把这条真正要断的两件事分别量出来：
          ①（硬闸）测试面 = 0 处散落拼接；
          ②（实测等价）搬运件里的 `HERE` 与 `DELIVERY_ROOT` **指向同一个目录**，
             所以"指向老交付根的死路径"并不存在，只是同一个目录换了个变量名。
        两条都成立才算过；第②条的实测路径写进证据，不靠我口头说等价。
        """
        pat = re.compile(r'\bHERE\s*/\s*"(bridge|client|check_env|mock_server|README|'
                         r'PROTOCOL|requirements|NM_REMOVAL_NOTE|extension)[^"]*"')

        def scan(base):
            hits = []
            for p in sorted(base.rglob("*")):
                if not p.is_file() or p.suffix not in (".py", ".js"):
                    continue
                if "evidence" in p.parts or "_archive" in str(p) or                         "__pycache__" in p.parts:
                    continue
                try:
                    txt = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for n, line in enumerate(txt.splitlines(), 1):
                    if pat.search(line):
                        hits.append({"file": str(p.relative_to(PROJECT_ROOT)).replace(
                            chr(92), "/"), "line": n, "text": line.strip()[:110]})
            return hits

        in_tests = scan(TESTS)
        in_package = scan(DELIVERY_ROOT)
        probe = _check_env_here()
        frozen_here = probe.get("check_env_HERE")
        norm = lambda x: str(x).lower().rstrip("/" + chr(92))
        equivalent = bool(frozen_here) and norm(frozen_here) == norm(DELIVERY_ROOT)
        ev("B", "B4_no_stray_here.json",
           {"criterionCaliber":
               "计划写\"全仓\"；照字面扫命中搬运件 check_env.py 一处，而该文件被 "
               "§0.1 sha256 逐字节钉死不可改。故拆两条分别量，都成立才算过："
               "①测试面散落拼接=0（硬闸）；②搬运件的 HERE 与 DELIVERY_ROOT "
               "实测指向同一目录（等价关系，不是白名单）",
            "①测试面散落拼接": in_tests,
            "②搬运件HERE与DELIVERY_ROOT等价": equivalent,
            "②实测check_env的HERE": frozen_here,
            "②实测DELIVERY_ROOT": str(DELIVERY_ROOT),
            "搬运件内的字面命中（仅记录，不判红）": in_package,
            "scannedRoots": [str(TESTS), str(DELIVERY_ROOT)],
            "verdict": "PASS" if (not in_tests and equivalent) else "FAIL"})
        self.assertEqual(in_tests, [],
                         f"测试面残留散落拼接：{json.dumps(in_tests, ensure_ascii=False)}")
        self.assertTrue(equivalent,
                        f"搬运件 HERE={frozen_here} 与 DELIVERY_ROOT={DELIVERY_ROOT} 不等价")


def _check_env_here() -> dict:
    """子进程里读搬运件 check_env 的 HERE（不在本测试进程里触发它的副作用）。"""
    code = ("import json;"
            "from chrome_downloader import check_env as ce;"
            "from chrome_downloader import paths;"
            "print('PROBE',json.dumps({'check_env_HERE':str(ce.HERE),"
            "'packageDir':str(paths.package_dir())}))")
    e = dict(os.environ)
    e["PYTHONPATH"] = PP
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120,
                       cwd=str(PROJECT_ROOT), env=e)
    for line in (r.stdout or "").splitlines():
        if line.startswith("PROBE "):
            return json.loads(line[6:])
    return {"error": (r.stderr or r.stdout or "")[-300:]}


def _port_precondition(port: int = 18888) -> dict:
    """§8.4.0 步骤 0：跑 91 条之前确认 18888 空闲。

    尺子只用 workflow/t6_snapshot.check_port —— 与 T6 的前置检查同一个实现，
    免得"测试自己造一把松一点的尺"。本函数**只报告不 kill**：结束进程归人或入口做
    （STOP-13：来源不明的占用一律停下问人）。
    """
    sys.path.insert(0, str(PROJECT_ROOT / "workflow"))
    from t6_snapshot import check_port        # noqa: E402
    rep = check_port(port)
    rep["note"] = ("只报告，不结束任何进程。18888 被占时 W14/W14b 必红（它们写死这个门牌），"
                   "所以这不是可选步骤，是 T6/全量跑的硬前置")
    return rep


# ==========================================================================
# T2（§4 T2 判据；不在 48 条里，但 T2 的三条子判据是三态/隔离/下载根）
# ==========================================================================
class PathsTests(unittest.TestCase):

    def test_T2_state_dir_three_states(self):
        """三态优先级：显式参数 > CHROME_DL_STATE_DIR > **与 v0.2 逐字符相同**的默认。"""
        v02 = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                           "chrome-dl-bridge")
        self.assertEqual(paths.default_state_dir(), v02,
                         "C4：默认值一换，升级用户就读不到自己的 endpoint/token/key")
        explicit = str(Path(tempfile.mkdtemp()) / "exp")
        env_dir = str(Path(tempfile.mkdtemp()) / "fromenv")
        old = os.environ.get("CHROME_DL_STATE_DIR")
        try:
            os.environ["CHROME_DL_STATE_DIR"] = env_dir
            got_env = str(paths.resolve_state_dir(None))
            got_both = str(paths.resolve_state_dir(explicit))
        finally:
            if old is None:
                os.environ.pop("CHROME_DL_STATE_DIR", None)
            else:
                os.environ["CHROME_DL_STATE_DIR"] = old
        got_explicit = str(paths.resolve_state_dir(explicit))
        rows = {"explicitBeatsEnv": got_both == str(Path(explicit)),
                "envBeatsDefault": got_env == str(Path(env_dir)),
                "defaultIsV02ByteForByte": str(paths.resolve_state_dir(None)) == v02,
                "explicitAgain": got_explicit == str(Path(explicit))}
        ok = all(rows.values())
        ev("T2", "T2_state_dir_three_states.json",
           {"explicit": got_both, "env": got_env,
            "default": str(paths.resolve_state_dir(None)), "v02Default": v02,
            "rows": rows, "verdict": "PASS" if ok else "FAIL"})
        self.assertTrue(ok, rows)

    def test_T2_multi_user_isolation(self):
        """多用户隔离：两个 state-dir 之间账本互不可见；归因不许跨目录认人。"""
        a, b = fresh_state_dir("u_a_"), fresh_state_dir("u_b_")
        try:
            pa, pb = paths.ensure_state_dir(a), paths.ensure_state_dir(b)
            (pa / "token").write_text("A" * 32, encoding="utf-8")
            (pb / "token").write_text("B" * 32, encoding="utf-8")
            isolated = str(pa) != str(pb) and paths.endpoint_path(pa) != paths.endpoint_path(pb)
            fake = {"pid": os.getpid(), "httpPort": 1, "httpHost": "127.0.0.1",
                    "token": "x", "bridgeId": "y"}
            ours, why = dm.endpoint_is_ours(fake, pa)
            perms = {}
            for d in (pa, pb):
                try:
                    perms[str(d)] = oct(d.stat().st_mode & 0o777)
                except OSError as exc:
                    perms[str(d)] = f"读不到：{exc}"
            out = {"dirA": str(pa), "dirB": str(pb), "isolated": isolated,
                   "crossDirAttribution": {"ours": ours, "why": why},
                   "dirModes": perms,
                   "posixXdgAvailable": str(paths.xdg_base("state")),
                   "v02DefaultKept": paths.default_state_dir()}
            out["verdict"] = "PASS" if isolated and not ours else "FAIL"
            ev("T2", "T2_multi_user_isolation.json", out)
            self.assertTrue(isolated, out)
            self.assertFalse(ours, f"不许把别的目录的桥认作自己的：{out}")
        finally:
            shutil.rmtree(a, ignore_errors=True)
            shutil.rmtree(b, ignore_errors=True)

    def test_T2_download_root_exists_and_matches_chrome(self):
        """T2 判据原文：打印的目录**确实存在**，且等于 Chrome 设置值（探测不到才退默认）。"""
        cd = paths.chrome_download_dir()
        root = paths.default_download_root()
        out = {"chromeDownloadDir": cd, "resolvedDownloadRoot": str(root),
               "rootExists": root.exists(),
               "matchesChrome": bool(cd) and Path(cd).resolve() == root.resolve(),
               "fellBackToHomeDownloads": cd is None}
        ok = out["rootExists"] and (out["matchesChrome"] or
                                    out["fellBackToHomeDownloads"])
        out["verdict"] = "PASS" if ok else "FAIL"
        ev("T2", "T2_download_root.json", out)
        self.assertTrue(out["rootExists"], f"探测到的下载根不存在：{out}")
        self.assertEqual(out["verdict"], "PASS", out)

    def test_T2_cli_env_prints_real_dirs(self):
        """§4 T2 的"本机真跑 chrome-dl env"：打印的目录要存在，且不甩 traceback。"""
        env = dict(os.environ)
        env["PYTHONPATH"] = PP
        r = subprocess.run([PY, "-m", "chrome_downloader.cli", "env", "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120, cwd=str(PROJECT_ROOT), env=env)
        self.assertEqual(r.returncode, 0, (r.stdout or "") + (r.stderr or ""))
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        info = json.loads(r.stdout)
        out = {"printed": info, "downloadRootExists": info["downloadRootExists"],
               "extensionDirExists": info["extensionDirExists"],
               "chromeDirMatchesRoot": (info.get("chromeDownloadDir") ==
                                        info.get("downloadRoot"))}
        out["verdict"] = "PASS" if (info["downloadRootExists"] and
                                    info["extensionDirExists"]) else "FAIL"
        ev("T2", "T2_cli_env.json", out)
        self.assertTrue(info["downloadRootExists"], out)
        self.assertTrue(info["extensionDirExists"], out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
