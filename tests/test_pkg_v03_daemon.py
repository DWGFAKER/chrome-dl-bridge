#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pkg_v03_daemon.py —— D1–D13 + V4b2（自动拉起，计划 §8.2；硬闸 D4/D6/D9/D12）

四条纪律（写在文件头，跑的人不必猜）：
  1. **一律临时 state-dir**：绝不写用户的全局目录（那里放着真用户的 token/key/账本）。
  2. **一律临时端口**：18888 是 W14/W14b 的门牌（硬编码 12 处），测试占住它就等于自己
     触发 §8.4.0 步骤 0 / STOP-13。
  3. **每个桥自己收干净**：只结束命令行归因为"我们这个 state-dir 起的桥"的 pid。
  4. 需要 L3（配对面就绪）的用例分两手法：
       (a) 接 tests/fake_chrome.py 走真 TOFU —— D1 / D12 / V4b2 用这条，
           因为它们要真把一单下载跑完；
       (b) 预置 handshake.key（= "上一个扩展已经配对过"的等价状态）——
           其余只看拉起/锁/文件状态的用例用这条，不必拖一个链路层对手进来。

诚实边界：假 Chrome 是**链路层对手**，它证桥侧行为，不替真实 Chrome 的 MV3 语义作证
（§8.7 的 UNVERIFIED 清单一条没缩）。D12  COMPLETE 说的是"协议与账本面跑通"，
不等于"真浏览器验过"（计划 §8.7 原话）。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
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
from test_pkg_v03 import (PROJECT_ROOT, PY, SRC, TESTS_DIR,  # noqa: E402
                          ev)
from chrome_downloader import bridge as bridge_mod          # noqa: E402
from chrome_downloader import common                        # noqa: E402
from chrome_downloader import daemon as dm                  # noqa: E402
from chrome_downloader import paths                         # noqa: E402
from chrome_downloader.client import Client                 # noqa: E402
from chrome_downloader.mock_server import MockServer, expected_sha256  # noqa: E402

READY_CAP = 10.0          # D1 / D9 的硬口径（§8.2 明文"≤10s"）


class DaemonBase(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cdl_d_"))
        self.state = self.dir / "state"
        self.dl = self.dir / "files"
        self.mock = MockServer(0, str(self.dir / "mock_access.log")).start()
        self.fake = None
        self.known_pids: set[int] = set()

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
        # 收尾只动"我起的桥"。⚠️ 两条硬护栏（都被本文件第一版踩过）：
        #   ① 绝不 kill os.getpid() —— D10 的冒充者账本里写的 pid 就是本进程自己，
        #      照着 endpoint.json 杀会把测试跑器一起带走（表现为"无输出 exit 1"）；
        #   ② known_pids 里可能混着冒充者/陈 pid，一律再归因一次才动手。
        info = paths.read_endpoint(self.state)
        ours, _why = (False, "no-endpoint")
        if info:
            ours, _why = dm.endpoint_is_ours(info, self.state)
        pids = set(self.known_pids)
        if ours and info.get("pid"):
            pids.add(int(info["pid"]))
        for pid in pids:
            if pid == os.getpid():
                continue
            if common.pid_alive(pid):
                _kill_pid(pid)
        for f in (paths.endpoint_path(self.state), paths.daemon_state_path(self.state),
                  paths.daemon_lock_path(self.state)):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            self.mock.stop()
        except Exception:
            pass
        tmproot = Path(tempfile.gettempdir()).resolve()
        real = self.dir.resolve()
        if self.dir.name.startswith("cdl_") and \
                str(real).lower().startswith(str(tmproot).lower()):
            shutil.rmtree(self.dir, ignore_errors=True)

    # ---------------- 舞台搭法 ----------------
    def seed_pairing(self) -> None:
        """预置 handshake.key：等价于"这台机器上的扩展以前配对过"。

        只给"看拉起/锁/文件状态"的用例用。需要真跑下载的用例走 fake_chrome 的 TOFU。
        """
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "handshake.key").write_text(secrets.token_hex(32), encoding="ascii")

    def remember(self, res_or_ep) -> int:
        ep = res_or_ep.get("endpoint") if isinstance(res_or_ep, dict) else res_or_ep
        pid = int(ep["pid"])
        self.known_pids.add(pid)
        return pid

    def spawn(self, *, port=None, timeout=25.0, with_fake=False, env=None,
              expect_error=False, extra_kwargs=None):
        p = port if port is not None else H.pick_bridge_port()
        box: dict = {}

        def go():
            try:
                box["res"] = dm.ensure_bridge(self.state, port=p, download_root=self.dl,
                                              timeout=timeout, quiet=True, env=env,
                                              **(extra_kwargs or {}))
            except Exception as exc:                       # noqa: BLE001
                box["err"] = exc
        t0 = time.time()
        th = threading.Thread(target=go)
        th.start()
        if with_fake:
            info = self._wait_endpoint(8.0)
            if info:
                self.fake = H.start_fake_chrome(self.state, self.dl)
        th.join(timeout + 40)
        el = round(time.time() - t0, 2)
        if box.get("err") is not None:
            if not expect_error:
                raise AssertionError(f"ensure_bridge 抛错（{el}s）：{box['err']!r}")
            raise box["err"]
        res = box.get("res")
        if expect_error:
            raise AssertionError(f"期望报错，却成功了：{res}")
        assert res is not None, "ensure_bridge 线程没留下结果"
        res["elapsedSec"] = el
        self.remember(res)
        return res

    def _wait_endpoint(self, timeout: float) -> dict | None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            info = paths.read_endpoint(self.state)
            if info:
                return info
            time.sleep(0.1)
        return None

    def client(self) -> Client:
        ep = paths.read_endpoint(self.state)
        self.assertIsNotNone(ep, "endpoint.json 不在")
        return Client(host=ep["httpHost"], port=ep["httpPort"], token=ep["token"],
                      timeout=20.0)

    def one_job(self, name="blob", size=250_000, filename="d.bin"):
        c = self.client()
        job = c.add(self.mock.url(name, size, rate=200_000), filename=filename,
                    sha256=expected_sha256(name, size))["job"]
        r = c.wait([job["jobId"]], poll=0.4, timeout=120)
        return c, r, job, c.show(job["jobId"])


def _kill_pid(pid: int) -> None:
    if pid == os.getpid():
        raise AssertionError("拒绝结束自己（测试跑器）—— 这类 pid 只能出现在冒充者场景里")
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=30)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# ==========================================================================
class DaemonTests(DaemonBase):

    def test_D1_autospawn_reaches_three_level_ready(self):
        """D1：无 endpoint → 自动拉起，≤10s 达**三级就绪**，且随后能立刻成功提交任务。"""
        self.assertFalse(paths.endpoint_path(self.state).exists())
        res = self.spawn(timeout=READY_CAP, with_fake=True)
        ep = res["endpoint"]
        lv = ep.get("readyLevels") or {}
        detail = lv.get("detail") or {}
        out = {"elapsedSec": res["elapsedSec"], "hardCapSeconds": READY_CAP,
               "L1_endpoint": lv.get("L1"), "L2_healthAndGuard": lv.get("L2"),
               "L3_pairingReady": lv.get("L3"), "detail": detail,
               "spawned": res["spawned"], "reason": res["reason"],
               "port": ep.get("httpPort"), "pid": ep.get("pid")}
        self.assertTrue(lv.get("L1") and lv.get("L2") and lv.get("L3"),
                        f"三级就绪没齐：{lv}")
        self.assertLessEqual(res["elapsedSec"], READY_CAP,
                             f"超过 10 秒硬口径：{out}")
        c, r, job, final = self.one_job("d1blob", 250_000, "d1.bin")
        out["submitAfterReady"] = {"state": final["state"],
                                   "sha256Actual": final["sha256Actual"],
                                   "waitOk": r["ok"]}
        out["verdict"] = "PASS" if (r["ok"] and final["state"] == "COMPLETE" and
                                    final["sha256Actual"] == "OK") else "FAIL"
        ev("D", "D1_autospawn_ready.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    def test_D2_live_bridge_is_reused_not_respawned(self):
        """D2：已有活桥 → 不新起（进程数不变、pid 不变）。"""
        self.seed_pairing()
        first = self.spawn()
        pid1 = first["endpoint"]["pid"]
        n1 = dm.count_bridge_processes_in_dir(self.state)          # dict：count/pids/method
        again = dm.ensure_bridge(self.state, port=H.pick_bridge_port(),
                                 download_root=self.dl, timeout=10.0, quiet=True)
        self.remember(again)
        pid2 = again["endpoint"]["pid"]
        n2 = dm.count_bridge_processes_in_dir(self.state)
        out = {"firstPid": pid1, "secondPid": pid2,
               "bridgesBefore": n1, "bridgesAfter": n2,
               "spawnedSecondTime": bool(again["spawned"]), "reason": again["reason"],
               "verdict": "PASS" if (pid1 == pid2 and n1["count"] == n2["count"] == 1 and
                                     not again["spawned"]) else "FAIL"}
        ev("D", "D2_reuse_no_respawn.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    def test_D3_stale_endpoint_cleaned_and_port_drift_followed(self):
        """D3：陈旧 endpoint（pid 死）→ 清理重起，端口漂移被跟随。"""
        self.seed_pairing()
        res = self.spawn()
        old_pid, old_port = int(res["endpoint"]["pid"]), res["endpoint"]["httpPort"]
        _kill_pid(old_pid)
        t0 = time.time()
        while common.pid_alive(old_pid) and time.time() - t0 < 8:
            time.sleep(0.2)
        self.assertFalse(common.pid_alive(old_pid), "桥没被杀掉，场景没造出来")
        stale = json.loads(paths.endpoint_path(self.state).read_text(encoding="utf-8"))
        stale["pid"] = 999_999                       # 一个不存在的 pid = 陈账本
        paths.endpoint_path(self.state).write_text(json.dumps(stale), encoding="utf-8")
        new_port = H.pick_bridge_port()
        res2 = self.spawn(port=new_port)
        info = paths.read_endpoint(self.state)
        h = self.client().health()
        out = {"oldPid": old_pid, "oldPort": old_port, "newPid": res2["endpoint"]["pid"],
               "newPort": info["httpPort"], "reason": res2["reason"],
               "spawned": res2["spawned"],
               "endpointFollowed": info["httpPort"] == new_port,
               "healthAgrees": h["httpPort"] == new_port,
               "drifted": info["httpPort"] != old_port,
               "bridgeIdPreserved": info.get("bridgeId") == res["endpoint"].get("bridgeId"),
               "verdict": "PASS" if (res2["endpoint"]["pid"] != old_pid and
                                     info["httpPort"] == new_port and
                                     h["httpPort"] == new_port and
                                     info["httpPort"] != old_port and
                                     info.get("bridgeId") ==
                                     res["endpoint"].get("bridgeId")) else "FAIL"}
        ev("D", "D3_stale_endpoint_recover.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    def test_D4_eight_concurrent_callers_spawn_exactly_one(self):
        """D4（硬闸）：8 进程并发首次调用 → **只起 1 个桥**。"""
        self.seed_pairing()
        port = H.pick_bridge_port()
        code = (
            "import json,os,sys;"
            "sys.path.insert(0,os.environ['CDL_SRC']);"
            "from chrome_downloader import daemon as dm;"
            f"r=dm.ensure_bridge({str(self.state)!r},port={port},"
            f"download_root={str(self.dl)!r},timeout=25,quiet=True);"
            "print('RESULT',json.dumps({'pid':r['endpoint'].get('pid'),"
            "'spawned':bool(r['spawned']),'reason':r['reason']}))")
        env = H.env_with_path(CDL_SRC=str(H.SRC))
        procs = [subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace",
                                  cwd=str(PROJECT_ROOT), env=env)
                 for _ in range(8)]
        rows, pids = [], set()
        for p in procs:
            o, _ = p.communicate(timeout=180)
            rec = None
            for line in (o or "").splitlines():
                if line.startswith("RESULT "):
                    rec = json.loads(line[7:])
                elif p.returncode != 0 and "Error" in line:
                    rows.append({"rawErrorTail": line[:160]})
            rows.append({"returncode": p.returncode, "parsed": rec})
            if rec:
                pids.add(int(rec["pid"]))
        live = dm.count_bridge_processes_in_dir(self.state)
        spawned_by = sum(1 for r in rows if (r.get("parsed") or {}).get("spawned"))
        for pid in pids:
            self.known_pids.add(pid)
        ok = (len(pids) == 1 and live["count"] == 1 and spawned_by == 1 and
              all(r["returncode"] == 0 for r in rows if "parsed" in r))
        out = {"callers": 8, "distinctPids": sorted(pids),
               "liveBridgesInDir": live,
               "spawnedBy": spawned_by,
               "reasons": sorted({(r["parsed"] or {}).get("reason", "")
                                  for r in rows if r.get("parsed")}),
               "rows": rows, "verdict": "PASS" if ok else "FAIL"}
        ev("D", "D4_eight_concurrent_single_spawn.json", out)
        self.assertEqual(out["verdict"], "PASS",
                         json.dumps(out, ensure_ascii=False)[:1500])

    def test_D5_failure_and_no_autospawn_are_human_readable(self):
        """D5：拉不起来 / NO_AUTOSPAWN=1 → 人话 + 退出码≠0 + **无 traceback**。"""
        sd = str(Path(tempfile.mkdtemp(prefix="cdl_d5_")) / "none")
        r = H.run_cli(["--state-dir", sd, "list"],
                      env=H.env_with_path(CHROME_DL_NO_AUTOSPAWN="1"))
        blob = (r.stdout or "") + (r.stderr or "")
        # 场景二：锁文件是坏 JSON（等价于"上一个持有者写到一半就死了"）
        bad = Path(tempfile.mkdtemp(prefix="cdl_d5b_")) / "ro"
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "daemon.lock").write_text("{坏了一半", encoding="utf-8")
        r2 = H.run_cli(["--state-dir", str(bad), "daemon", "start"], timeout=150)
        blob2 = (r2.stdout or "") + (r2.stderr or "")
        ep2 = paths.read_endpoint(bad)
        if ep2:
            self.known_pids.add(int(ep2["pid"]))
        ok = (r.returncode != 0 and "Traceback" not in blob and
              ("CHROME_DL_NO_AUTOSPAWN" in blob or "daemon start" in blob) and
              "Traceback" not in blob2)
        out = {"noAutospawn": {"returncode": r.returncode,
                               "stderr": (r.stderr or "")[:400],
                               "noTraceback": "Traceback" not in blob},
               "corruptLock": {"returncode": r2.returncode,
                               "stdoutTail": (r2.stdout or "")[-240:],
                               "stderrTail": (r2.stderr or "")[-240:],
                               "noTraceback": "Traceback" not in blob2,
                               "recovered": bool(ep2)},
               "verdict": "PASS" if ok else "FAIL"}
        ev("D", "D5_human_errors.json", out)
        shutil.rmtree(bad.parent, ignore_errors=True)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    def test_D6_parent_death_child_survives(self):
        """D6（硬闸）：父死子活 —— 发起拉起的进程退出后，桥还活着且能答。"""
        self.seed_pairing()
        code = (
            "import json,os,sys;"
            "sys.path.insert(0,os.environ['CDL_SRC']);"
            "from chrome_downloader import daemon as dm;"
            f"r=dm.ensure_bridge({str(self.state)!r},port={H.pick_bridge_port()},"
            f"download_root={str(self.dl)!r},timeout=25,quiet=True);"
            "print('RESULT',json.dumps({'pid':r['endpoint']['pid'],"
            "'port':r['endpoint']['httpPort']}))")
        env = H.env_with_path(CDL_SRC=str(H.SRC))
        self.assertFalse(paths.endpoint_path(self.state).exists())
        r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           cwd=str(PROJECT_ROOT), env=env)
        self.assertEqual(r.returncode, 0, (r.stdout or "") + (r.stderr or ""))
        info = json.loads([l for l in r.stdout.splitlines()
                           if l.startswith("RESULT ")][0][7:])
        pid = int(info["pid"])
        self.known_pids.add(pid)
        time.sleep(3.0)                       # 父进程已被 subprocess.run 回收
        alive = common.pid_alive(pid)
        ep = paths.read_endpoint(self.state)
        try:
            h = Client(host=ep["httpHost"], port=ep["httpPort"], token=ep["token"],
                       timeout=8.0).health()
        except Exception as exc:
            h = {"error": f"{type(exc).__name__}: {exc}"}
        cmd = dm.process_cmdline(pid) or ""
        out = {"parentReturncode": r.returncode, "childPid": pid,
               "parentIsNowGone": True, "aliveAfterParentExit": alive,
               "childCmdlineLooksDetached": "-m chrome_downloader.bridge" in
               cmd.replace("\\", "/").lower() or "bridge" in cmd.lower(),
               "health": {"ok": h.get("ok"), "port": h.get("httpPort"),
                          "egressGuard": h.get("egressGuard"), "error": h.get("error")},
               "detachedMechanism": ("DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP"
                                     if os.name == "nt" else "start_new_session=True"),
               "verdict": "PASS" if alive and h.get("ok") else "FAIL"}
        ev("D", "D6_parent_death_child_alive.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    def test_D7_bridge_stdout_goes_to_log_not_caller(self):
        """D7：桥的 stdout 进 <state>/bridge.log，不污染调用方的 stdout。"""
        self.seed_pairing()
        code = (
            "import os,sys;"
            "sys.path.insert(0,os.environ['CDL_SRC']);"
            "from chrome_downloader import daemon as dm;"
            f"dm.ensure_bridge({str(self.state)!r},port={H.pick_bridge_port()},"
            f"download_root={str(self.dl)!r},timeout=25,quiet=True);"
            "print('CALLER-ONLY-LINE')")
        env = H.env_with_path(CDL_SRC=str(H.SRC))
        r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           cwd=str(PROJECT_ROOT), env=env)
        ep = paths.read_endpoint(self.state)
        if ep:
            self.known_pids.add(int(ep["pid"]))
        logp = paths.bridge_log_path(self.state)
        txt = logp.read_text(encoding="utf-8", errors="replace") if logp.exists() else ""
        leaked = "bridge_ready" in (r.stdout or "")
        out = {"callerStdoutLines": (r.stdout or "").splitlines()[:6],
               "callerStderrTail": (r.stderr or "")[-200:],
               "bridgeLogExists": logp.exists(),
               "bridgeLogHasReadyEvent": "bridge_ready" in txt,
               "bridgeLogBytes": len(txt),
               "nothingLeakedToCallerStdout": not leaked,
               "verdict": "PASS" if (r.returncode == 0 and not leaked and
                                     "bridge_ready" in txt) else "FAIL"}
        ev("D", "D7_stdout_to_log.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    def test_D8_no_auto_spawn_flag_matches_v02(self):
        """D8：`--no-auto-spawn` 行为等同 v0.2 —— 报缺、给"python bridge.py"、非 0。"""
        sd = str(Path(tempfile.mkdtemp(prefix="cdl_d8_")) / "empty")
        r = H.run_cli(["--no-auto-spawn", "--state-dir", sd, "list"])
        blob = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode != 0 and "python bridge.py" in blob and "Traceback" not in blob
        out = {"returncode": r.returncode, "stderr": (r.stderr or "")[:400],
               "mentionsBridgeCommand": "python bridge.py" in blob,
               "noTraceback": "Traceback" not in blob,
               "v02Parity": "这句话原封不动来自搬运件 client.py 的 BridgeError，"
                            "没有另写一套话术",
               "verdict": "PASS" if ok else "FAIL"}
        ev("D", "D8_no_auto_spawn_v02_parity.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    def test_D9_killed_lock_holder_replaced_within_10s(self):
        """D9（硬闸）：抢锁进程在"已抢到、未就绪"时被 kill → 下次调用 ≤10s 仍拉起成功，
        且 daemon.log 里有**接管审计行**（F1 的病根：锁毒化会永久废掉 G1）。"""
        self.assertFalse(paths.endpoint_path(self.state).exists())
        port = H.pick_bridge_port()
        holder = subprocess.Popen(
            [PY, "-c",
             "import os,sys,time;"
             "sys.path.insert(0,os.environ['CDL_SRC']);"
             "from chrome_downloader import daemon as dm;"
             f"h=dm.acquire_lock({str(self.state)!r},argv_hint='D9-holder');"
             "print('LOCKED', h.payload['pid'] if h else 'NONE', flush=True);"
             "time.sleep(300)"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT),
            env=H.env_with_path(CDL_SRC=str(H.SRC)))
        try:
            first = ""
            t0 = time.time()
            while time.time() - t0 < 40:
                first = (holder.stdout.readline() or "").strip()
                if first:
                    break
            self.assertTrue(first.startswith("LOCKED"), f"抢锁没成功：{first!r}")
            self.assertTrue(paths.daemon_lock_path(self.state).exists(),
                            "锁文件不在，场景没造出来")
            holder.kill()
            holder.communicate(timeout=15)
            self.seed_pairing()        # 接管者要能判定"就绪"，配对面按等价状态预置
            t1 = time.time()
            res = dm.ensure_bridge(self.state, port=port, download_root=self.dl,
                                   timeout=READY_CAP, quiet=True)
            el = round(time.time() - t1, 2)
            self.remember(res)
        finally:
            try:
                holder.kill()
            except Exception:
                pass
        logp = paths.daemon_log_path(self.state)
        lines = logp.read_text(encoding="utf-8", errors="replace").splitlines() \
            if logp.exists() else []
        takeover = [json.loads(l) for l in lines if '"lock_takeover"' in l]
        out = {"elapsedSec": el, "hardCapSeconds": READY_CAP,
               "spawned": res["spawned"], "reason": res["reason"],
               "pid": res["endpoint"]["pid"], "lockNow": str(
                   paths.daemon_lock_path(self.state).exists()),
               "takeoverAuditLines": takeover,
               "daemonLogExists": logp.exists(),
               "verdict": "PASS" if (el <= READY_CAP and takeover and
                                     takeover[0].get("reason") == "holder-pid-dead")
               else "FAIL"}
        ev("D", "D9_stale_lock_takeover.json", out)
        self.assertEqual(out["verdict"], "PASS",
                         json.dumps(out, ensure_ascii=False)[:1200])

    def test_D10_foreign_port_owner_is_not_hijacked(self):
        """D10：门牌被冒充者占着 → 不接管、人话、退出码≠0，且**不向对方发任何 URL/token**。"""
        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(8)
        sport = squatter.getsockname()[1]
        received: list[bytes] = []
        stop = threading.Event()

        def serve():
            squatter.settimeout(0.4)
            while not stop.is_set():
                try:
                    conn, _ = squatter.accept()
                except OSError:
                    continue
                conn.settimeout(1.0)
                try:
                    received.append(conn.recv(4096) or b"")
                except OSError:
                    received.append(b"")
                conn.close()
        th = threading.Thread(target=serve, daemon=True)
        th.start()
        secret = "SECRET-TOKEN-MUST-NOT-LEAVE-" + secrets.token_hex(4)
        self.state.mkdir(parents=True, exist_ok=True)
        paths.endpoint_path(self.state).write_text(json.dumps({
            "version": 2, "token": secret, "pid": os.getpid(),
            "httpHost": "127.0.0.1", "httpPort": sport, "wsPort": sport,
            "wsPath": "/link", "transport": "aiohttp", "bridgeId": "not-really",
            "paired": True, "egressGuard": True, "stateDir": str(self.state)}),
            encoding="utf-8")
        err = None
        try:
            dm.ensure_bridge(self.state, port=sport, download_root=self.dl,
                             timeout=8.0, quiet=True)
        except dm.DaemonError as exc:
            err = exc
        stop.set()
        th.join(timeout=4)
        squatter.close()
        sent = b"".join(received)
        ok = (isinstance(err, dm.PortTaken) and not sent)
        out = {"errorClass": type(err).__name__, "errorText": str(err or "")[:400],
               "squatterReceivedBytes": len(sent),
               "tokenLeaked": secret.encode() in sent,
               "urlLeaked": b"/file/" in sent or b"GET " in sent,
               "humanReadable": bool(err and ("确认" in str(err) or "端口" in str(err))),
               "verdict": "PASS" if ok else "FAIL"}
        ev("D", "D10_foreign_port_no_hijack.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False)[:900])

    def test_D11_bridge_id_mismatch_refuses_reuse(self):
        """D11：endpoint 的 bridgeId 与预期不符 → 拒绝复用并给处置引导（不递凭证）。"""
        self.seed_pairing()
        res = self.spawn()
        real_id = res["endpoint"].get("bridgeId")
        err = None
        try:
            dm.ensure_bridge(self.state, port=res["endpoint"]["httpPort"],
                             download_root=self.dl, expected_bridge_id="0" * 32,
                             timeout=8.0, quiet=True)
        except dm.DaemonError as exc:
            err = exc
        out = {"expected": "0" * 32, "actual": real_id,
               "errorClass": type(err).__name__ if err else None,
               "errorText": str(err or "")[:400],
               "givesRemedy": bool(err and "daemon stop" in str(err)),
               "stillReused": err is None,
               "verdict": "PASS" if (isinstance(err, dm.PortTaken) and
                                     "bridgeId" in str(err) and
                                     "daemon stop" in str(err)) else "FAIL"}
        ev("D", "D11_bridge_id_mismatch.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False)[:900])

    def test_D12_cold_start_sdk_to_complete_download(self):
        """D12（硬闸）：无桥出发 → Client() → 自动拉起 → 提交 → COMPLETE + sha256 OK。"""
        self.assertFalse(paths.endpoint_path(self.state).exists())
        port = H.pick_bridge_port()
        box: dict = {}
        th = threading.Thread(target=lambda: box.update(
            res=dm.ensure_bridge(self.state, port=port, download_root=self.dl,
                                 timeout=25.0, quiet=True)))
        th.start()
        if self._wait_endpoint(8.0):
            self.fake = H.start_fake_chrome(self.state, self.dl)
        th.join(90)
        self.assertIsNotNone(box.get("res"), f"自动拉起没成功：{box}")
        self.remember(box["res"])
        c, r, job, final = self.one_job("d12blob", 500_000, "d12.bin")
        ok = (r["ok"] and final["state"] == "COMPLETE" and
              final["sha256Actual"] == "OK")
        out = {"startedFromNoBridge": True, "jobId": job["jobId"], "waitOk": r["ok"],
               "state": final["state"], "sha256Actual": final["sha256Actual"],
               "bytesReceived": final.get("bytesReceived"),
               "totalBytes": final.get("totalBytes"),
               "transport": final.get("host"),
               "verdict": "PASS" if ok else "FAIL"}
        ev("D", "D12_cold_start_full_download.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))

    def test_D13_daemon_stop_leaves_defined_file_state(self):
        """D13：daemon stop 后 —— endpoint.json 删、daemon.json 删、daemon.lock 必不在。"""
        self.seed_pairing()
        res = self.spawn()
        pid = int(res["endpoint"]["pid"])
        before = {"endpoint": paths.endpoint_path(self.state).exists(),
                  "daemonJson": paths.daemon_state_path(self.state).exists(),
                  "lock": paths.daemon_lock_path(self.state).exists()}
        self.assertTrue(before["endpoint"] and before["daemonJson"],
                        f"前置状态不对：{before}")
        out_stop = dm.stop_bridge(self.state)
        time.sleep(1.0)
        after = {"endpoint": paths.endpoint_path(self.state).exists(),
                 "daemonJson": paths.daemon_state_path(self.state).exists(),
                 "lock": paths.daemon_lock_path(self.state).exists()}
        alive = common.pid_alive(pid)
        rec = {"before": before, "after": after, "killed": out_stop.get("killed"),
               "refused": out_stop.get("refused"), "pidWas": pid,
               "pidAliveAfter": alive,
               "lockNeverAppearedOrRemoved": not after["lock"],
               "verdict": "PASS" if (not any(after.values()) and not alive and
                                     out_stop.get("refused") is None) else "FAIL"}
        ev("D", "D13_stop_file_state.json", rec)
        self.assertEqual(rec["verdict"], "PASS", json.dumps(rec, ensure_ascii=False))
        self.known_pids.discard(pid)


# ==========================================================================
class EgressGuardChildTests(DaemonBase):

    def test_V4b2_spawned_child_has_egress_guard(self):
        """V4b2（C2）：daemon 拉起出的子进程里 egressGuard=true，且自测 PASS。"""
        res = self.spawn(with_fake=True)
        ep = paths.read_endpoint(self.state)
        h = self.client().health()
        guard = h.get("egressGuard") or {}
        r = subprocess.run([PY, "-m", "chrome_downloader.bridge", "--selftest-egress",
                            "--state-dir", str(Path(tempfile.mkdtemp()) / "s"),
                            "--port", "0"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=150, cwd=str(PROJECT_ROOT),
                           env=H.env_with_path())
        st = json.loads(r.stdout) if r.returncode == 0 else {"error": r.stderr[-300:]}
        ok = (guard.get("enabled") is True and r.returncode == 0 and
              st.get("verdict") == "PASS" and st.get("guard_enabled") is True)
        out = {"spawnReason": res["reason"], "pid": ep.get("pid"),
               "endpointEgressGuard": ep.get("egressGuard"),
               "healthEgressGuard": guard,
               "selftestReturncode": r.returncode,
               "selftestVerdict": st.get("verdict"),
               "selftestGuardEnabled": st.get("guard_enabled"),
               "selftestBlockedEntries": st.get("blockedLogEntries"),
               "selftestLoopback": st.get("loopback"),
               "verdict": "PASS" if ok else "FAIL"}
        ev("V4b2", "V4b2_spawned_egress_guard.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))


class Redline1Tests(unittest.TestCase):
    """红线 1（F8-①）的反例证据：拉起代码只跑在没装熔断的进程里。"""

    def test_redline1_interpreter_exception_is_a_regex_edge(self):
        """同一份 bridge.py：argv 里多一段带 `http` 字样的路径，判定立刻从放行翻成拦。

        这不是产品缺陷，是把"靠正则边界碰巧能跑"钉成可复查的证据 ——
        所以拉起永远放在 CLI/SDK/MCP（没装熔断的进程），Bridge 不自己 fork 自己。
        """
        cases = {
            "普通下载根": [PY, "-m", "chrome_downloader.bridge",
                           "--download-root", "C:\\downloads_cache"],
            "路径里带 http 字样": [PY, "-m", "chrome_downloader.bridge",
                                   "--download-root", "C:\\http_cache"],
            "改名含 http": [PY, "-m", "chrome_http_dl.bridge"],
            "带真 URL": [PY, "-m", "chrome_downloader.bridge",
                         "http://example.com/x"],
        }
        rows = {k: {"argv": v, "looksEgress": list(bridge_mod.exec_looks_like_egress(v))}
                for k, v in cases.items()}
        flipped = (rows["普通下载根"]["looksEgress"][0] is False and
                   rows["路径里带 http 字样"]["looksEgress"][0] is True and
                   rows["改名含 http"]["looksEgress"][0] is True and
                   rows["带真 URL"]["looksEgress"][0] is True)
        out = {"rows": rows, "翻脸成立": flipped,
               "结论": "解释器例外分支是正则边界，不可依赖；熔断 raise EgressBlocked 而非阻塞，"
                       "最坏是 D5 的人话报错，不会死锁",
               "verdict": "PASS" if flipped else "FAIL"}
        ev("D", "D_redline1_regex_edge.json", out)
        self.assertEqual(out["verdict"], "PASS", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
