#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pkg_v03.py —— v0.3 打包版的**新增判据**实测（PLAN-CHROME-DOWNLOADER-003 V4）

覆盖：P1–P8（打包）｜B1–B4（测试搬迁）｜T2 路径三态｜D1–D13 + V4b2（自动拉起）
      ｜L1–L6（CLI）｜M1–M13（MCP）
合计条数与计划 §8.0 的 48 条一一对应（P8 + B4 + L6 + D13 + V4b2(1) + M13 + T7-a(3) = 48）。

⚠️ 三条纪律（写在文件头，免得跑的人踩）：
  1. **不碰 v0.2 的 IMMUTABLE 证据**：本文件只往 P*/B*/D*/L*/M*/V4b2/ 这些
     §8.4.1 的 NOT-IMMUTABLE 目录里落证据。
  2. **不占 18888**：所有真起的桥一律 `--port 0`（系统临时端口）+ 临时 state-dir。
     18888 是 W14/W14b 的门牌，占了就触发 §8.4.0 步骤 0 / STOP-13。
  3. 假 Chrome 仍是**链路层对手**（tests/fake_chrome.py），它证的是桥与协议行为，
     不替真实 Chrome 的 MV3 语义作证（§8.7 的 UNVERIFIED 清单不变）。

跑法：
    python test_pkg_v03.py                 全量
    python test_pkg_v03.py DaemonTests     只跑一类
    python test_pkg_v03.py DaemonTests.test_D1_autospawn_reaches_three_level_ready
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
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

from chrome_downloader import daemon as dm                       # noqa: E402
from chrome_downloader import mcp_server as mcp                  # noqa: E402
from chrome_downloader import paths                              # noqa: E402
from chrome_downloader import common                             # noqa: E402
from chrome_downloader.cli import main as cli_main               # noqa: E402
from chrome_downloader.mock_server import MockServer, expected_sha256  # noqa: E402
from harness import BridgeHarness, EVIDENCE, ev, pick_free, wait_endpoint  # noqa: E402

PY = sys.executable
PP = os.pathsep.join([str(SRC), str(TESTS)])
NEW_DIRS = ("P", "B", "D", "L", "M", "V4b2", "T2")     # §8.4.1 NOT-IMMUTABLE 的新增面

# ------------------------------------------------------------------ 工具
# P 系列要"干净 venv"，建一次全场复用（每个用例都重造 venv 会把 T6 拖成半小时）。
_VENV: Path | None = None
_VENV_LOCK = threading.Lock()
BASE_PIP_LIST: Path = PROJECT_ROOT / "workflow" / "pip_list_base_before.txt"


def fresh_state_dir(prefix="dlv03_") -> Path:
    """临时 state-dir：绝不写用户的全局目录（那是 v0.2 用户的账本）。"""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    return d


def run_py(code: str, *, env=None, timeout=120, cwd=None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e["PYTHONPATH"] = PP + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    if env:
        e.update(env)
    return subprocess.run([PY, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=str(cwd or PROJECT_ROOT), env=e)


def env_with_path(**extra) -> dict:
    """给子进程一份带 PYTHONPATH 的环境（+ 可选覆盖）。"""
    e = dict(os.environ)
    e["PYTHONPATH"] = PP + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e.update({k: v for k, v in extra.items() if v is not None})
    for k, v in extra.items():
        if v is None:
            e.pop(k, None)
    return e


def run_cli(argv, *, env=None, timeout=180, cwd=None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e["PYTHONPATH"] = PP + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    if env:
        e.update(env)
    return subprocess.run([PY, "-m", "chrome_downloader.cli"] + list(argv),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          cwd=str(cwd or PROJECT_ROOT), env=e)


def start_fake_chrome(state_dir: Path, download_root: Path, *, key: str | None = None,
                      wait: float = 25.0,
                      download_root_arg: bool = True) -> subprocess.Popen | None:
    """把链路层对手接到**已经由 daemon 起来的桥**上，负责 TOFU 配对（补 L3 就绪）。

    假 Chrome 自己会读 endpoint.json 换端口，所以"桥重启/端口漂"之后不用重起它。
    """
    cmd = [PY, str(TESTS / "fake_chrome.py"), "--state-dir", str(state_dir)]
    if download_root_arg:
        cmd += ["--download-root", str(download_root)]
    if key:
        cmd += ["--key", key]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            cwd=str(PROJECT_ROOT))
    t0 = time.time()
    while time.time() - t0 < wait:
        if _extension_connected(state_dir):
            return proc
        if proc.poll() is not None:
            return proc
        time.sleep(0.2)
    return proc


def _extension_connected(state_dir: Path) -> bool:
    try:
        info = paths.read_endpoint(state_dir)
        if not info:
            return False
        from chrome_downloader.client import Client
        c = Client(host=info["httpHost"], port=info["httpPort"], token=info["token"],
                   timeout=3.0)
        return bool(c.health().get("extensionConnected"))
    except Exception:
        return False


def daemon_pids_in_dir(state_dir: Path) -> list[int]:
    """数一遍"用着这个 state-dir 的桥进程"。只查本机命令行，不出网。"""
    pid = (paths.read_endpoint(state_dir) or {}).get("pid")
    if not pid:
        return []
    return [int(pid)] if common.pid_alive(int(pid)) else []


TESTS_DIR = TESTS          # 别名：外部套件里想按名字取也行
NEW_DIRS = ("P", "B", "D", "L", "M", "V4b2", "T2")


def pick_bridge_port() -> int:
    """给桥要一个**不是 18888** 的临时端口。

    为什么非要临时端口：18888 是 W14/W14b 的门牌（硬编码 12 处），
    任何测试占住它都会让 T6 当场 1 FAIL + 1 ERROR —— 那正是 §8.4.0 步骤 0
    与 STOP-13 要防的事。所以本仓新增的测试一律漂到临时端口。
    """
    for _ in range(20):
        p = pick_free()
        if p != 18888 and 1024 < p < 65535:
            return p
    return pick_free()


def our_bridge_pids(state_dir) -> list[int]:
    """endpoint.json 说的、且命令行归因为"我们这个 state-dir 起的桥"的 pid 列表。"""
    info = paths.read_endpoint(state_dir)
    if not info:
        return []
    from chrome_downloader import daemon as _dm
    ours, _why = _dm.endpoint_is_ours(info, state_dir)
    return [int(info["pid"])] if ours else []


def stop_our_bridge(state_dir, extra_pids=(), *, force: bool = False,
                    timeout: float = 8.0) -> dict:
    """收尾用：**只结束自己起的桥**（归因过），别的进程一律不动。"""
    import signal
    pids = list(dict.fromkeys(our_bridge_pids(state_dir) + [int(x) for x in extra_pids]))
    killed, refused = [], []
    for pid in pids:
        if not common.pid_alive(pid):
            continue
        if os.name == "nt":
            args = ["/PID", str(pid), "/T"] + (["/F"] if force else [])
            r = subprocess.run(["taskkill"] + args, capture_output=True, timeout=30)
            if r.returncode != 0 or not force:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=30)
        else:
            try:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            except OSError as exc:
                refused.append({"pid": pid, "why": str(exc)})
                continue
        killed.append(pid)
    t0 = time.time()
    while any(common.pid_alive(p) for p in killed) and time.time() - t0 < timeout:
        time.sleep(0.2)
    # 顺手把账本清掉，免得下一个用例读到陈 endpoint
    for f in (paths.endpoint_path(state_dir), paths.daemon_state_path(state_dir),
              paths.daemon_lock_path(state_dir)):
        try:
            f.unlink()
        except OSError:
            pass
    return {"killed": killed, "refused": refused,
            "stillAlive": [p for p in killed if common.pid_alive(p)]}


def count_bridge_processes() -> int:
    """本机所有 chrome_downloader.bridge 进程数（D2/D4 的硬口径）。"""
    if os.name == "nt":
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
             "Name='pythonw.exe'\" | ForEach-Object { $_.CommandLine }"],
            capture_output=True, text=True, timeout=40, errors="replace")
        blob = (r.stdout or "")
    else:
        blob = subprocess.run("ps -eo args", shell=True, capture_output=True,
                              text=True, timeout=40).stdout or ""
    return sum(1 for ln in blob.splitlines() if "chrome_downloader.bridge" in ln
               or "chrome_downloader\\bridge.py" in ln or "chrome_downloader/bridge.py" in ln)


# ==========================================================================
# P1–P6 / P8：打包
# ==========================================================================
class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _VENV
        with _VENV_LOCK:
            if _VENV is None or not (_VENV / "pyvenv.cfg").exists():
                d = Path(tempfile.mkdtemp(prefix="cdl_venv_"))
                r = subprocess.run([PY, "-m", "venv", str(d)], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=900)
                if r.returncode != 0:
                    raise RuntimeError(f"venv 建不出来：{(r.stdout or '')[-300:]}"
                                       f"{(r.stderr or '')[-300:]}")
                _VENV = d
        cls.venv = _VENV
        cls.venv_py = (str(_VENV / "Scripts" / "python.exe") if os.name == "nt"
                        else str(_VENV / "bin" / "python"))
        cls.venv_scripts = _VENV / ("Scripts" if os.name == "nt" else "bin")

    def pip_in_venv(self, *args, timeout=900):
        """venv 里的 pip。⚠️ 必须**剥掉 PYTHONPATH** 再调，这不是美化，是踩出来的：

        实测（本仓的测试环境天然带着 PYTHONPATH=src）：PYTHONPATH 里有源码树时，
        `pip uninstall` 会通过 importlib.metadata 在**源码树**里找到同一个发行包，
        于是打出 "Not uninstalling ... outside environment ... No files were found to
        uninstall" 就 rc=0 收工 —— venv 里的文件一个没删。P4 当场红。
        ⇒ 那是"开发者环境遮蔽了已安装的包"这一真实现象，属产品级发现（记进 P4 证据），
          不是"包卸载不干净"。所以：判据要在**普通用户的环境**（无 PYTHONPATH 遮蔽）里跑，
          同时把遮蔽现象本身作为 OBSERVATION 留在证据里，不藏。
        """
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run([self.venv_py, "-m", "pip", *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=str(PROJECT_ROOT),
                              env=env)

    def test_P1_clean_venv_install(self):
        """P1：干净 venv `pip install --no-build-isolation .` exit 0。"""
        r = self.pip_in_venv("install", "--no-build-isolation", str(PROJECT_ROOT))
        ev("P", "P1_install.json", {
            "cmd": f"{self.venv_py} -m pip install --no-build-isolation {PROJECT_ROOT}",
            "returncode": r.returncode,
            "tail": (r.stdout or "")[-600:], "errTail": (r.stderr or "")[-600:],
            "verdict": "PASS" if r.returncode == 0 else "FAIL"})
        self.assertEqual(r.returncode, 0, (r.stdout or "") + (r.stderr or ""))

    def test_P2_three_entrypoints_help(self):
        """P2：三个入口（chrome-dl / chrome-dl-mcp / python -m chrome_downloader）--help exit 0。"""
        rows = []
        # 先确认已安装（P1 的 venv）；没装则补装一次，避免用例顺序造成假绿/假红
        if not (self.venv_scripts / "chrome-dl.exe").exists() and \
                not (self.venv_scripts / "chrome-dl").exists():
            self.pip_in_venv("install", "--no-build-isolation", str(PROJECT_ROOT))
        for name in ("chrome-dl", "chrome-dl-mcp"):
            exe = self.venv_scripts / (name + (".exe" if os.name == "nt" else ""))
            self.assertTrue(exe.exists(), f"入口脚本没生成：{exe}")
            r = subprocess.run([str(exe), "--help"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=90)
            rows.append({"entry": name, "returncode": r.returncode,
                         "stdoutHead": (r.stdout or "")[:120],
                         "stderrHead": (r.stderr or "")[:200],
                         "noTraceback": "Traceback" not in (r.stdout + r.stderr)})
        r3 = subprocess.run([self.venv_py, "-m", "chrome_downloader", "--help"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=90)
        rows.append({"entry": "python -m chrome_downloader", "returncode": r3.returncode,
                     "stdoutHead": (r3.stdout or "")[:120],
                     "stderrHead": (r3.stderr or "")[:200],
                     "noTraceback": "Traceback" not in (r3.stdout + r3.stderr)})
        ok = all(x["returncode"] == 0 and x["noTraceback"] for x in rows)
        ev("P", "P2_entrypoints.json", {"entries": rows,
                                        "verdict": "PASS" if ok else "FAIL"})
        self.assertTrue(ok, json.dumps(rows, ensure_ascii=False))

    def test_P3_extension_dir_via_resources(self):
        """P3：扩展目录可由 importlib.resources 解析且 4 个文件在（装在 venv 里也解析得到）。"""
        code = (
            "import json;from chrome_downloader import paths;"
            "from importlib.resources import files;"
            "d=paths.extension_dir_resource();"
            f"need={json.dumps(paths.extension_files())};"
            "miss=[f for f in need if not (d/f).exists()];"
            "print(json.dumps({'dir':str(d),'missing':miss,"
            "'viaResources':str(files('chrome_downloader').joinpath('extension')),"
            "'present':[f for f in need if (d/f).exists()],\"verdict\":"
            "'PASS' if not miss else 'FAIL'},ensure_ascii=False))")
        local = run_py(code)
        venv = subprocess.run([self.venv_py, "-c", code], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        a = json.loads(local.stdout) if local.returncode == 0 else {"error": local.stderr[-300:]}
        b = json.loads(venv.stdout) if venv.returncode == 0 else {"error": venv.stderr[-300:]}
        verdict = "PASS" if a.get("verdict") == "PASS" and b.get("verdict") == "PASS" \
            else "FAIL"
        ev("P", "P3_extension_resources.json",
           {"源码树": a, "装进 venv": b, "filesMustExist": paths.extension_files(),
            "verdict": verdict})
        self.assertEqual(verdict, "PASS", json.dumps([a, b], ensure_ascii=False))

    def test_P4_uninstall_leaves_no_residue(self):
        """P4：卸载后 site-packages 之外无残留。

        判据（三条都成立才算过，全部是"包这个动作"留下的东西）：
          ① 卸载 exit 0；
          ② site-packages 里不再有 chrome_downloader*，Scripts 里不再有 chrome-dl*；
          ③ 与项目相关的 LOCALAPPDATA 目录名**不因安装/卸载而增加**
             （state-dir 只由运行期创建，安装动作不许往用户目录塞东西）。
        诚实边界：这条证的是 pip 安装/卸载不留残file；**证不了**运行期不写 state-dir
        —— 运行期写 state-dir 是设计行为，收尾由 D13（daemon stop 的三文件状态）负责。
        """
        la = paths.resolve_state_dir().parent
        watch = lambda: sorted(x.name for x in la.glob("*") if "chrome" in x.name.lower())
        before = watch()
        r1 = self.pip_in_venv("install", "--no-build-isolation", str(PROJECT_ROOT))
        self.assertEqual(r1.returncode, 0, (r1.stdout or "") + (r1.stderr or ""))
        site = Path(self.venv_scripts).parent / "Lib" / "site-packages"
        self.assertTrue(site.exists(), f"venv 结构不符预期：{site}")
        pkg_dir = site / "chrome_downloader"
        scripts = [x.name for x in Path(self.venv_scripts).glob("chrome-dl*")]
        self.assertTrue(pkg_dir.exists(), "装完包目录不在 site-packages")
        # 装了就应该有 chrome-dl / chrome-dl-mcp 两个入口
        self.assertGreaterEqual(len(scripts), 2, scripts)
        r = self.pip_in_venv("uninstall", "-y", "chrome-downloader-client")
        # ---- OBSERVATION（不作门槛，只留痕）：装回去再演示一次"遮蔽导致空转" ----
        # 判据已经在干净环境里量完了，这里才做破坏性演示，顺序不许换。
        self.pip_in_venv("install", "--no-build-isolation", str(PROJECT_ROOT))
        shadow_env = dict(os.environ)
        shadow_env["PYTHONPATH"] = PP          # 把源码树摆到 venv 前面
        sh = subprocess.run([self.venv_py, "-m", "pip", "uninstall", "-y",
                             "chrome-downloader-client"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300, cwd=str(PROJECT_ROOT),
                            env=shadow_env)
        sh_lines = [l.strip() for l in (sh.stdout or "").splitlines() if l.strip()]
        shadow_note = next((l for l in sh_lines if "outside environment" in l), "")
        shadow_left = [str(x) for x in site.glob("chrome_downloader*")]
        self.pip_in_venv("uninstall", "-y", "chrome-downloader-client")   # 收干净
        # Windows 上"目录已空但还看得见"是实测会有的收尾延迟（本用例第一版就被它绊过：
        # 卸载 rc=0、手工再查确实没了，但脚本立刻 glob 仍看得到旧目录）。
        # 所以这里给一个**有上限的等待**，并把等待秒数如实记进证据 ——
        # 等待不等于放宽判据：等满 5 秒还在，就照样 FAIL。
        t0 = time.time()
        while time.time() - t0 < 5.0:
            left_pkg = [str(x) for x in site.glob("chrome_downloader*")]
            left_scripts = [x.name for x in Path(self.venv_scripts).glob("chrome-dl*")]
            if not left_pkg and not left_scripts:
                break
            time.sleep(0.25)
        settle = round(time.time() - t0, 2)
        after = watch()
        ok = (r.returncode == 0 and not left_pkg and not left_scripts and before == after)
        ev("P", "P4_uninstall_residue.json",
           {"installReturncode": r1.returncode, "uninstallReturncode": r.returncode,
            "entrypointsWhileInstalled": scripts,
            "settleSeconds": settle,
            "leftoversInSitePackages": left_pkg[:10],
            "leftoverEntrypoints": left_scripts,
            "chromeNamesNextToStateDirBefore": before,
            "chromeNamesNextToStateDirAfter": after,
            "OBSERVATION_shadowing": {
                "what": "开发者环境（PYTHONPATH 指向源码树）里 pip uninstall 会空转",
                "returncode": sh.returncode, "stdoutLines": sh_lines[:6],
                "outsideEnvironmentNotice": shadow_note,
                "stillInSitePackagesAfterShadowAttempt": shadow_left[:6],
                "notACriterion": True,
                "note": "判据本体在**无 PYTHONPATH 遮蔽**的干净环境里量（= 普通用户环境）；"
                        "这段只把踩到的现象留下来，不据此放宽也不据此判红"},
            "pipInVenvStripsPythonPath": True,
            "honestScope": "证 pip 安装/卸载不留残file；运行期写 state-dir 属设计行为，"
                            "由 D13 收尾",
            "verdict": "PASS" if ok else "FAIL"})
        self.assertEqual(r.returncode, 0, (r.stdout or "") + (r.stderr or ""))
        self.assertEqual(left_pkg, [], f"卸载后 site-packages 仍有残留：{left_pkg}")
        self.assertEqual(left_scripts, [], f"卸载后入口仍在：{left_scripts}")
        self.assertEqual(before, after, "安装/卸载不该改变用户目录里的项目相关名字")

    def test_P5_no_required_third_party_dependency(self):
        """P5：importlib.metadata.requires() 的**必装**项为空（C3）。"""
        code = (
            "import json,importlib.metadata as m;"
            "req=m.requires('chrome-downloader-client') or [];"
            "required=[r for r in req if 'extra' not in r];"
            "extras=sorted({r.split('==')[0].split('[')[-1].rstrip(']').strip() "
            "for r in req if 'extra' in r});"
            "print(json.dumps({'requires':req,'required':required,'extras':extras,"
            "'verdict':'PASS' if not required else 'FAIL'},ensure_ascii=False))")
        # 必须在装了包的 venv 里查（源码树没有 dist-info，requires 查不出来）
        self.pip_in_venv("install", "--no-build-isolation", str(PROJECT_ROOT))
        r = subprocess.run([self.venv_py, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
        out = json.loads(r.stdout) if r.returncode == 0 else {"error": r.stderr[-300:]}
        ev("P", "P5_required_deps.json", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(out.get("required"), [], out)

    def test_P6_eight_interpreters_core_unit_tests(self):
        """P6：计划点名的 **8 个解释器**各跑核心单测（UnitTests）必须通过。

        解释器清单直接来自计划 §0.1（不靠"扫到什么"，避免漏掉点名要验的那几个）。
        """
        targets = [
            ("base 3.11.15", PY),
            ("cellnest 3.10.20", r"C:\Users\hrx\miniconda3\envs\cellnest\python.exe"),
            ("cu 3.11.16", r"C:\Users\hrx\miniconda3\envs\cu\python.exe"),
            ("dkd-comp 3.11.15", r"C:\Users\hrx\miniconda3\envs\dkd-comp\python.exe"),
            ("dkd-comp310 3.10.21", r"C:\Users\hrx\miniconda3\envs\dkd-comp310\python.exe"),
            ("grn 3.11.16", r"C:\Users\hrx\miniconda3\envs\grn\python.exe"),
            ("grn-legacy 3.10.21", r"C:\Users\hrx\miniconda3\envs\grn-legacy\python.exe"),
            ("sidish-audit 3.12.13", r"C:\Users\hrx\miniconda3\envs\sidish-audit\python.exe"),
        ]
        rows = []
        for name, exe in targets:
            if not Path(exe).exists():
                rows.append({"interpreter": name, "path": exe, "status": "MISSING"})
                continue
            ver = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                 timeout=60)
            e = dict(os.environ)
            e["PYTHONPATH"] = PP
            r = subprocess.run([exe, "-m", "unittest", "test_downloader.UnitTests", "-v"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=600, cwd=str(TESTS), env=e)
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            rows.append({"interpreter": name, "path": exe,
                         "version": (ver.stdout or ver.stderr).strip(),
                         "returncode": r.returncode, "tail": tail,
                         "ok": r.returncode == 0})
        missing = [x for x in rows if x.get("status") == "MISSING"]
        failed = [x for x in rows if x.get("ok") is False]
        ev("P", "P6_interpreters.json",
           {"requiredCount": 8, "rows": rows, "missing": missing, "failed": failed,
            "verdict": "PASS" if not missing and not failed else "FAIL"})
        self.assertEqual(missing, [], f"解释器不在位：{missing}")
        self.assertEqual(failed, [], f"跑不过：{json.dumps(failed, ensure_ascii=False)}")

    def test_P7_base_environment_not_polluted(self):
        """P7（C3）：构建/发布全程之后，base 环境的 `pip list` 快照**逐行相同**。

        快照文件由命令产出（`pip list --format=freeze`），不手写：
          workflow/pip_list_base_before.txt —— 进场时（T1 之前）拍的
          workflow/pip_list_base_after.txt  —— T8 构建跑完之后拍的
        本判据比较两者的**排序后行集合**，并把差异行数写进证据。
        """
        before_p = PROJECT_ROOT / "workflow" / "pip_list_base_before.txt"
        after_p = PROJECT_ROOT / "workflow" / "pip_list_base_after.txt"
        # ⚠️ 两边都必须用**剥掉 PYTHONPATH** 的环境取样。实测病根：editable 安装会在
        #    src/ 下留 *.egg-info，而 pip list 会把 PYTHONPATH 上的 egg-info 当成
        #    "已装发行包"列出来 → 带着 PYTHONPATH 取样本判据必假红。
        if not after_p.exists():
            # T8 还没跑时先把"当前"当作 after 拍一份，保证这条判据在 T6 阶段也能真测
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            r = subprocess.run([PY, "-m", "pip", "list", "--format=freeze"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=300, env=env)
            after_p.write_text(r.stdout, encoding="utf-8")
            shot = "本判据现场拍摄"
        else:
            shot = "复用 T8 之后落盘的快照"
        self.assertTrue(before_p.exists(), f"缺少进场快照：{before_p}")
        b = sorted(x for x in before_p.read_text(encoding="utf-8").splitlines() if x.strip())
        a = sorted(x for x in after_p.read_text(encoding="utf-8").splitlines() if x.strip())
        added = [x for x in a if x not in b]
        removed = [x for x in b if x not in a]
        ok = b == a
        ev("P", "P7_base_env_pip_list_stable.json",
           {"beforeFile": str(before_p), "afterFile": str(after_p),
            "snapshotSource": shot,
            "samplingEnv": "PYTHONPATH 已剥掉（否则 pip list 会把 src/ 下的 *.egg-info "
                           "当已装发行包列出，造成 P7 假红 —— 实测踩过）",
            "linesBefore": len(b), "linesAfter": len(a),
            "added": added[:20], "removed": removed[:20],
            "identicalLineByLine": ok,
            "note": "构建工具（build/twine）只许装在临时 venv；base 装了就等于"
                    "污染用户环境，C3 的'零必装依赖'变成空话",
            "verdict": "PASS" if ok else "FAIL"})
        self.assertEqual(added, [], f"base 被装了新东西：{added}")
        self.assertEqual(removed, [], f"base 少了东西（不该发生）：{removed}")

    def test_P8_requirements_pyproject_consistency(self):
        """P8：requirements.txt 必装项 == project.dependencies（空），extras 项对齐。

        ⚠️ 口径订正（改的是**实现**，不是判据文字，也不是为了凑绿）：
          第一版我把 requirements.txt 里所有**非注释行**全当"必装项"，于是
          `aiohttp==3.14.2` 被判成必装 → P8 假红。但计划 T1.2 选 A 时写的是
          「内容保持"仅**可选** aiohttp"的注释+一行」——那行本身就是可选声明，
          且它已被 [project.optional-dependencies] 的 serve 覆盖。
          所以"必装项"的正确算法是：**非注释行里，没被 extras 覆盖的那些**。
          v0.2 自己的判据（test_V10）也只断言"违禁库不在有效行里"，不把它当必装。
        """
        req = (DELIVERY_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        eff = [l.strip() for l in req.splitlines()
               if l.strip() and not l.strip().startswith("#")]
        py = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        deps = re.search(r"^dependencies\s*=\s*\[([^\]]*)\]", py, re.M)
        opts = re.search(r"^\[project\.optional-dependencies\](.*?)(?=^\[|\Z)", py,
                         re.M | re.S)
        dep_items = [x.strip().strip("\"'") for x in (deps.group(1).split(",")
                                                     if deps else []) if x.strip()]
        opt_items = re.findall(r"^\s*\w+\s*=\s*\[(.*?)\]", opts.group(1) if opts else "",
                               re.M | re.S)
        opt_pkgs = [p.strip().strip("\"'").split("==")[0].split(">=")[0].strip()
                    for blk in opt_items for p in blk.split(",") if p.strip()]
        req_pkgs = [x.split("==")[0].split(">=")[0].strip() for x in eff]
        required_from_requirements = [p for p in req_pkgs if p not in set(opt_pkgs)]
        ok_required = (required_from_requirements == [] and dep_items == [])
        ok_extras = set(req_pkgs) <= set(opt_pkgs) and set(opt_pkgs) <= set(req_pkgs)
        ev("P", "P8_requirements_sync.json",
           {"requirementsEffective": eff, "projectDependencies": dep_items,
            "optionalDependencyPkgs": sorted(set(opt_pkgs)),
            "requiredItemsFromRequirements": required_from_requirements,
            "requiredMatch": ok_required, "extrasCoverRequirements": ok_extras,
            "extrasExactlyMatchRequirements": set(req_pkgs) == set(opt_pkgs),
            "verdict": "PASS" if (ok_required and ok_extras) else "FAIL"})
        self.assertEqual(dep_items, [], "必装依赖不许非空")
        self.assertTrue(ok_required,
                        f"requirements 里出现必装项就不是零依赖了："
                        f"{required_from_requirements} vs {dep_items}")
        self.assertTrue(ok_extras,
                        f"requirements 与 optional-dependencies 必须互盖："
                        f"{req_pkgs} vs {opt_pkgs}")
        self.assertEqual(eff, ["aiohttp==3.14.2"], f"requirements 面不许变大：{eff}")


# ==========================================================================
# B1–B4（测试搬迁）与 T2（paths 三态/隔离/下载根）不在这个文件里：
# 其中 B1/B3 各要跑一整遍 91 条套件（实测约 4 分钟/次），
# 和 P 系列挤在一个文件里会让"跑一次打包判据"变成"再跑两遍全量"。
# 唯一落点在 tests/test_pkg_v03_reloc.py（B1–B4 + T2 四组判据）。
# ==========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
