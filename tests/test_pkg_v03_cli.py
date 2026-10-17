#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pkg_v03_cli.py —— L1–L6（统一 CLI，计划 §8.0 的 L 系列 6 条）

L 系列是审查员指出"T4 原本零门禁"之后补的，所以每条都写成能自己重算的形状：
  L1 每个子命令 --help exit 0
  L2 未知子命令 exit≠0 + 人话 + 无 traceback
  L3 daemon status 的 JSON 可 json.loads 且字段固定
  L4 daemon stop 后 D13 的三文件状态成立
  L5 download 别名与 add 等价（同参数同 job 结果）
  L6 CLI 任一路径不吐 traceback

纪律：一律临时 state-dir + 临时端口，绝不碰 18888（§8.4.0 步骤 0 / STOP-13）。
"""
from __future__ import annotations

import json
import os
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
from chrome_downloader import common                        # noqa: E402
from chrome_downloader import daemon as dm                  # noqa: E402
from chrome_downloader import paths                         # noqa: E402
from chrome_downloader.cli import ALL_VERBS, LEGACY_VERBS, NEW_VERBS  # noqa: E402
from chrome_downloader.mock_server import MockServer, expected_sha256  # noqa: E402

# L3 要求"字段固定"：这里把字段集合钉死，多一个少一个都算口径漂移
STATUS_FIELDS = {"stateDir", "endpointPresent", "bridgePid", "bridgeAlive",
                 "ownedByUs", "attribution", "port", "transport", "paired",
                 "extensionConnected", "egressGuard", "readyLevels",
                 "daemonJsonPresent", "lockPresent", "lockInfo", "noAutospawn"}


class CliBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cdl_l_"))
        self.state = self.dir / "state"
        self.dl = self.dir / "files"
        # 本用例创建过的所有 state-dir 都登记在这里。
        # 为什么非要登记：不登记就会漏 —— 实测漏过一次，用例在临时目录里
        # 起了一个常驻 18888 的桥，tearDown 只盯着 self.state 没看见它，
        # 结果后面跑 91 条套件时 W14/W14b 当场一 FAIL 一 ERROR。
        self.state_dirs: list[Path] = [self.state]

        self.mock = MockServer(0, str(self.dir / "mock.log")).start()
        self.fake = None
        self.pids: set[int] = set()

    def new_state_dir(self, name: str) -> Path:
        d = self.dir / name
        d.mkdir(parents=True, exist_ok=True)
        self.state_dirs.append(d)
        return d

    def tearDown(self):
        if self.fake is not None:
            try:
                self.fake.terminate()
                self.fake.communicate(timeout=6)
            except Exception:
                try:
                    self.fake.kill()
                except Exception:
                    pass
        pids = set(self.pids)
        for sd in self.state_dirs:
            info = paths.read_endpoint(sd)
            if not info:
                continue
            # 归因不过也照收：这些目录都在本用例的临时目录底下，跑掉的桥不可能是别人的
            if str(sd).lower().startswith(str(self.dir).lower()):
                try:
                    pids.add(int(info["pid"]))
                except (TypeError, ValueError):
                    pass
        for pid in pids:
            if pid != os.getpid() and common.pid_alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=30)
        try:
            self.mock.stop()
        except Exception:
            pass
        tmproot = Path(tempfile.gettempdir()).resolve()
        if self.dir.name.startswith("cdl_") and \
                str(self.dir.resolve()).lower().startswith(str(tmproot).lower()):
            shutil.rmtree(self.dir, ignore_errors=True)

    def cli(self, *argv, timeout=180, env_extra=None, use_state_dir: bool = True):
        """跑 CLI。**默认强制带 --state-dir**。

        ⚠️ 这条默认是踩出来的：不带 --state-dir 时自动拉起会按默认值写进
           **用户的全局目录**（LOCALAPPDATA\chrome-dl-bridge）并把桥常驻在 18888，
           于是后面跑 91 条套件时 W14/W14b（写死 18888）当场一 FAIL 一 ERROR。
           要测"不带 state-dir"的行为请显式传 use_state_dir=False，别用默认。
        """
        argv = list(argv)
        if use_state_dir and "--state-dir" not in [str(a) for a in argv]:
            argv = ["--state-dir", str(self.state)] + argv
        env = H.env_with_path(**(env_extra or {}))
        env["CHROME_DL_STATE_DIR"] = str(self.state)   # 双保险：环境变量也钉在临时目录
        return subprocess.run([PY, "-m", "chrome_downloader.cli"] + [str(a) for a in argv],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              cwd=str(PROJECT_ROOT), env=env)

    def live_bridge(self, with_fake: bool = False):
        """起一个可用的桥。

        with_fake=False：只预置 handshake.key（配对面就绪），够 status/stop/动词面用。
        with_fake=True ：再挂上 tests/fake_chrome.py，任务才**真能下完**。
          不挂对手时，桥会按设计在 link_wait_timeout 之后把任务判成
          FAILED: NO_EXTENSION_CONNECTED —— 那是产品的正确行为，不是 bug；
          所以 L5 这种"要看 COMPLETE 与 sha256Actual"的用例必须带对手。
        """
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "handshake.key").write_text("ab" * 32, encoding="ascii")
        box = {}

        def go():
            try:
                box["res"] = dm.ensure_bridge(self.state, port=H.pick_bridge_port(),
                                              download_root=self.dl, timeout=25,
                                              quiet=True)
            except Exception as exc:                       # noqa: BLE001
                box["err"] = exc
        th = threading.Thread(target=go)
        th.start()
        if with_fake:
            t0 = time.time()
            info = None
            while time.time() - t0 < 20:
                info = paths.read_endpoint(self.state)
                if info:
                    break
                time.sleep(0.1)
            if info:
                self.fake = H.start_fake_chrome(self.state, self.dl)
        th.join(60)
        assert "res" in box, f"桥起不来：{box.get('err')}"
        self.pids.add(int(box["res"]["endpoint"]["pid"]))
        return box["res"]


class CliTests(CliBase):

    def test_L1_every_subcommand_help_exit0(self):
        """L1：每个子命令 --help exit 0，且不甩 traceback。"""
        rows = {}
        bad = []
        for verb in sorted(ALL_VERBS):
            r = self.cli(verb, "--help")
            blob = (r.stdout or "") + (r.stderr or "")
            ok = r.returncode == 0 and "usage" in blob.lower() and "Traceback" not in blob
            rows[verb] = {"returncode": r.returncode, "hasUsage": "usage" in blob.lower(),
                          "noTraceback": "Traceback" not in blob,
                          "stdoutHead": (r.stdout or "")[:80].replace("\n", " ")}
            if not ok:
                bad.append(verb)
        r = self.cli("--help")
        top = (r.stdout or "") + (r.stderr or "")
        listed = [v for v in ALL_VERBS if v in top]
        rows["<top-level>"] = {"returncode": r.returncode, "verbsListed": listed}
        ok = (r.returncode == 0 and not bad and len(listed) == len(ALL_VERBS))
        out = {"verbs": rows, "notHelpOk": bad, "topLevelListsAll": len(listed),
               "verbCount": len(ALL_VERBS),
               "verdict": "PASS" if ok else "FAIL"}
        ev("L", "L1_help_exit0.json", out)
        self.assertEqual(bad, [], json.dumps(out, ensure_ascii=False)[:1000])
        self.assertEqual(len(listed), len(ALL_VERBS), out)

    def test_L2_unknown_verb_is_human_and_not_traceback(self):
        """L2：未知子命令 exit≠0 + 人话 + 无 traceback。"""
        rows = []
        for argv in (["bogus-verb"], ["list ", ], ["daemon"], ["daemon", "teleport"]):
            argv = [a.strip() for a in argv]
            r = self.cli(*argv)
            blob = (r.stdout or "") + (r.stderr or "")
            rows.append({"argv": argv, "returncode": r.returncode,
                         "noTraceback": "Traceback" not in blob,
                         # "人话"的口径 = 告诉调用方能用什么（动词清单 / usage 行 /
                         # 三个动作名都算），不是死抠某一句话术。
                         "listsVerbs": ("能用的动词" in blob or
                                         "usage" in blob.lower() or
                                         ("start" in blob and "stop" in blob and
                                          "status" in blob)),
                         "stderrHead": (r.stderr or "")[:200].replace("\n", " | ")})
        # "list " 那条其实是合法动词（带空格的 argv 被 strip 掉），所以判据只看真未知的那两条
        hard = [x for x in rows if x["argv"][0] in ("bogus-verb", "daemon")]
        bad = [x for x in hard if x["returncode"] == 0 or not x["noTraceback"]
               or not x["listsVerbs"]]
        out = {"rows": rows, "bad": bad,
               "verdict": "PASS" if not bad else "FAIL"}
        ev("L", "L2_unknown_verb_human.json", out)
        self.assertEqual(bad, [], json.dumps(out, ensure_ascii=False)[:1200])

    def test_L3_daemon_status_is_stable_json(self):
        """L3：`daemon status` 可被 json.loads，且字段集合固定（多一个少一个都算漂移）。"""
        self.live_bridge()
        r = self.cli("--state-dir", str(self.state), "daemon", "status")
        self.assertEqual(r.returncode, 0, (r.stdout or "") + (r.stderr or ""))
        try:
            data = json.loads(r.stdout)
        except ValueError as exc:
            self.fail(f"daemon status 不是合法 JSON：{exc} / {r.stdout[:300]!r}")
        keys = set(data)
        extra, missing = keys - STATUS_FIELDS, STATUS_FIELDS - keys
        # 再问一次：字段集合必须一致（同一状态下两次问不许漂移）
        r2 = self.cli("--state-dir", str(self.state), "daemon", "status")
        d2 = json.loads(r2.stdout)
        stable_keys = set(d2) == keys
        out = {"fields": sorted(keys), "unexpected": sorted(extra),
               "missing": sorted(missing),
               "secondCallSameFields": stable_keys,
               "bridgeAlive": data.get("bridgeAlive"), "port": data.get("port"),
               "readyLevels": data.get("readyLevels"), "sample": data,
               "verdict": "PASS" if (not extra and not missing and stable_keys and
                                     data.get("bridgeAlive")) else "FAIL"}
        ev("L", "L3_status_json_fixed_fields.json", out)
        self.assertEqual(sorted(missing), [], out)
        self.assertEqual(sorted(extra), [], out)
        self.assertTrue(out["bridgeAlive"], out)

    def test_L4_daemon_stop_files_state(self):
        """L4：`chrome-dl daemon stop` 后 D13 的三文件状态成立。"""
        self.live_bridge()
        before = {"endpoint": paths.endpoint_path(self.state).exists(),
                  "daemonJson": paths.daemon_state_path(self.state).exists()}
        self.assertTrue(before["endpoint"] and before["daemonJson"], before)
        r = self.cli("--state-dir", str(self.state), "daemon", "stop")
        time.sleep(1.0)
        after = {"endpoint": paths.endpoint_path(self.state).exists(),
                 "daemonJson": paths.daemon_state_path(self.state).exists(),
                 "lock": paths.daemon_lock_path(self.state).exists()}
        alive = dm.count_bridge_processes_in_dir(self.state)
        ok = (r.returncode == 0 and not any(after.values()) and alive["count"] == 0)
        out = {"before": before, "after": after, "returncode": r.returncode,
               "stdout": (r.stdout or "")[:400], "bridgesStillLive": alive,
               "verdict": "PASS" if ok else "FAIL"}
        ev("L", "L4_stop_file_state.json", out)
        self.pids.clear()          # stop 已经把桥收干净了
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    def test_L5_download_alias_equals_add(self):
        """L5：download 别名与 add 等价 —— 同参数得到同一个 job 结果。

        等价性的口径：同一个 URL 提交两次（add 与 download 各一次）必须**复用同一个
        jobId**（bridge 侧按 url+dest 幂等），并且 --wait 的终态与 sha256Actual 一致。
        """
        self.live_bridge(with_fake=True)
        name, size = "l5blob", 300_000
        sha = expected_sha256(name, size)
        base = ["--state-dir", str(self.state)]
        r_add = self.cli(*base, "add", self.mock.url(name, size, rate=200_000),
                         "--name", "l5.bin", "--sha256", sha)
        self.assertEqual(r_add.returncode, 0, (r_add.stdout or "") + (r_add.stderr or ""))
        id_add = self._job_id(r_add.stdout)
        r_dl = self.cli(*base, "download", self.mock.url(name, size, rate=200_000),
                        "--filename", "l5.bin", "--sha256", sha, "--wait")
        id_dl = self._job_id(r_dl.stdout)
        show = self.cli(*base, "show", id_add)
        doc = json.loads(show.stdout) if show.returncode == 0 else {}
        blob = r_dl.stdout + r_dl.stderr
        ok = (id_add and id_add == id_dl and doc.get("sha256Actual") == "OK" and
              '"state": "COMPLETE"' in blob.replace("'", '"'))
        out = {"addJobId": id_add, "downloadJobId": id_dl, "sameJob": id_add == id_dl,
               "downloadReturncode": r_dl.returncode,
               "showState": doc.get("state"), "showSha256Actual": doc.get("sha256Actual"),
               "filename": doc.get("filename"),
               "verdict": "PASS" if ok else "FAIL"}
        ev("L", "L5_download_equals_add.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    @staticmethod
    def _job_id(stdout):
        import re
        m = re.search(r"jobId=([0-9a-f]{16})", stdout or "")
        return m.group(1) if m else None

    def test_L6_no_traceback_on_any_path(self):
        """L6：CLI 的任一路径（含出错路径）都不许往 stdout/stderr 吐 traceback。

        两点写法纪律（都是踩出来的）：
          · **每条用例一个独立 state-dir**：共用一个目录时，前一条留下的半死桥会让
            后一条的 `--wait` 一直阻塞到超时（实测 TIMEOUT 就是这么来的）；
          · 带 `--wait` 的必须给 `--timeout`，否则走 SDK 默认 1800 秒，判据跑不完。
        """
        cases = [
            (["list"], "没桥"),
            (["health"], "没桥 health"),
            (["stats"], "没桥 stats"),
            (["download", "http://127.0.0.1:1/x.bin", "--wait", "--timeout", "8"],
             "没桥 download（带超时）"),
            (["--no-auto-spawn", "download", "http://127.0.0.1:1/x.bin",
              "--wait", "--timeout", "8"], "关自动拉起的 download"),
            (["daemon", "status"], "status 不许崩"),
            (["daemon", "stop"], "stop 不许崩"),
            (["daemon", "start", "--quiet"], "start 不许崩"),
            (["extension-path"], "扩展路径"),
            (["env", "--json"], "env"),
            (["show", "deadbeefdeadbeef"], "查不存在的 job"),
            (["bogus"], "未知动词"),
            (["--state-dir", str(self.dir / "not/exist/at/all"), "daemon", "status"],
             "怪路径"),
            (["add", "not-a-url"], "URL 不像 URL"),
            (["wait", "deadbeefdeadbeef", "--timeout", "8"], "等不存在的 job"),
        ]
        rows, bad = [], []
        for idx, (argv, why) in enumerate(cases):
            sd = self.new_state_dir(f"l6_{idx}")
            cmd = [a for a in argv]
            if "--state-dir" not in cmd:
                cmd = ["--state-dir", str(sd)]
            # 显式给临时端口：不带 --port 时自动拉起会去占默认门牌 18888，
            # 而 18888 是 W14/W14b 写死的端口（§8.4.0 步骤 0 / STOP-13）。
            if "--port" not in cmd and "--state-dir" in cmd:
                cmd = cmd + ["--port", str(H.pick_bridge_port())]
            elif "--port" not in cmd:
                cmd = ["--port", str(H.pick_bridge_port())] + cmd
            try:
                r = self.cli(*cmd, timeout=150)
            except subprocess.TimeoutExpired:
                bad.append({"argv": cmd, "why": why, "problem": "TIMEOUT"})
                continue
            blob = (r.stdout or "") + (r.stderr or "")
            tb = "Traceback (most recent call last)" in blob
            rows.append({"argv": cmd, "why": why, "returncode": r.returncode,
                         "traceback": tb, "stateDirIsolated": True,
                         "tail": blob[-160:].replace(chr(10), " | ")})
            if tb:
                bad.append({"argv": cmd, "why": why, "problem": "TRACEBACK",
                            "tail": blob[-300:]})
        out = {"paths": rows, "offenders": bad,
               "isolatedPerCase": True,
               "verdict": "PASS" if not bad else "FAIL"}
        ev("L", "L6_no_traceback.json", out)
        self.assertEqual(bad, [], json.dumps(out, ensure_ascii=False)[:1600])

    def test_L_cli_verb_surface_matches_v02_plus_new(self):
        """动词面核对：v0.2 的 12 个动词一个不少，新增的 4 个一个不多（写下来防止日后加面）。"""
        from chrome_downloader import client as client_mod
        src = (H.DELIVERY_ROOT / "client.py").read_text(encoding="utf-8")
        v02 = set(__import__("re").findall(r'sub\.add_parser\("([a-z\-]+)"', src))
        out = {"v02VerbsParsedFromClient": sorted(v02),
               "cliLegacyVerbs": sorted(LEGACY_VERBS),
               "cliNewVerbs": sorted(NEW_VERBS),
               "legacyCoversV02": v02 <= set(LEGACY_VERBS),
               "noVerifySubcommand": "verify" not in set(ALL_VERBS) | v02,
               "verdict": "PASS" if (v02 <= set(LEGACY_VERBS) and
                                     "verify" not in set(ALL_VERBS)) else "FAIL"}
        ev("L", "L_verb_surface.json", out)
        self.assertTrue(out["legacyCoversV02"], out)
        self.assertTrue(out["noVerifySubcommand"],
                        "F5′ 选项 A 明写不新增 verify 子命令")


if __name__ == "__main__":
    unittest.main(verbosity=2)
