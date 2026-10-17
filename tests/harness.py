#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/harness.py —— v0.2 WS 架构的测试舞台（被 test_downloader.py 与各专项测试共用）

为什么单独成文件：Bridge 子进程 + mock 源站 + 假 Chrome 三件套的启动、等待、取证、收尾
在每个用例里都要用；写歪一次就会造成"证据上写着跑过了，其实连的是上一任的死桥"。

诚实边界（001-F8 口径）：这里的假 Chrome 是**链路层**对手，能证 Bridge 侧的行为，
不能证真实 Chrome 的 MV3 语义（SW 生命周期 / alarms 实钳制 / downloads 语义 / LNA 档位）。
那些只能由人在真浏览器里跑 WS-T0/T0.5/T0e，本文件不代它作证。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---- T1.2 路径收敛 ----
TESTS = Path(__file__).resolve().parent      # 夹具与 evidence 所在处
PROJECT_ROOT = TESTS.parent                  # 工程根（src/ 与 tests/ 的父目录）
SRC = PROJECT_ROOT / "src"
import importlib.util as _ilu                 # noqa: E402
_spec = _ilu.find_spec("chrome_downloader")
if _spec is None or not _spec.origin:
    raise RuntimeError("找不到 chrome_downloader 包：PYTHONPATH 要指向 src/")
DELIVERY_ROOT = Path(_spec.origin).resolve().parent   # 交付代码根 = 包目录
HERE = DELIVERY_ROOT        # 兼容位：旧引用里的 HERE 指交付根，不指 tests/
PY = sys.executable
EVIDENCE = TESTS / "evidence"

# 子进程寻包：测试里所有 `-m chrome_downloader.<mod>` 的调用都必须能找到包。
# 装了包的情形下 PYTHONPATH 多余但无害；没装包（源码树）的情形下它就是唯一通路。
_pp = os.environ.get("PYTHONPATH")
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(SRC), str(TESTS)] + ([_pp] if _pp else []))

for _p in (str(SRC), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from chrome_downloader.mock_server import MockServer   # noqa: E402


def pick_free() -> int:
    """要一个"此刻没人用"的临时端口（用来制造占用场景，不是用来给 Bridge 定端口的）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_endpoint(state_dir, timeout: float = 30.0, not_pid: int | None = None,
                  want_pid: int | None = None) -> dict:
    """读 endpoint.json。

    重启场景必须给 want_pid 或 not_pid：上一个进程留下的陈文件会把测试带连到死桥上，
    那种失败看起来像"代码坏了"，其实是"账本读错了"。
    """
    ep = Path(state_dir) / "endpoint.json"
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ep.exists():
            try:
                d = json.loads(ep.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(0.1)
                continue
            if not d.get("httpPort"):
                pass
            elif want_pid is not None and d.get("pid") != want_pid:
                pass
            elif not_pid is not None and d.get("pid") == not_pid:
                pass
            else:
                return d
        time.sleep(0.1)
    raise RuntimeError(f"bridge 未就绪（endpoint.json 超时 not_pid={not_pid} want_pid={want_pid}）")


class BridgeHarness:
    """起真 bridge 子进程 + mock 源站 + 假 Chrome（WS 版），给集成测试当舞台。

    port=0（默认）→ 系统分临时端口：测试之间不抢真用户的 18888。
    port=18888 → 钉死门牌，只有 W3/W14/W14b 这类"端口本身"的实验才需要。
    """

    def __init__(self, max_concurrent: int = 4, per_host: int = 2, no_token: bool = False,
                 stale_after: float = 45.0, guard: bool = True, fake_chrome: bool = True,
                 key: str | None = None, port: int = 0, transport: str | None = None,
                 die_at: int = 0, drop_at: int = 0, send_blob: int = 0,
                 link_wait_timeout: float | None = None, keepalive_sec: float | None = None,
                 extra_bridge_args=None, wait_fake: bool = True, token: str | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="cdl_"))
        self.state_dir = self.dir / "state"
        self.dl_root = self.dir / "files"
        self.mock = MockServer(0, str(self.dir / "mock_access.log")).start()
        cmd = [PY, "-m", "chrome_downloader.bridge", "--port", str(port),
               "--state-dir", str(self.state_dir), "--download-root", str(self.dl_root),
               "--max-concurrent", str(max_concurrent), "--per-host", str(per_host),
               "--stale-after", str(stale_after)]
        if link_wait_timeout is not None:
            cmd += ["--link-wait-timeout", str(link_wait_timeout)]
        if keepalive_sec is not None:
            cmd += ["--keepalive-sec", str(keepalive_sec)]
        if token is not None:
            cmd += ["--token", token]
        if no_token:
            cmd.append("--no-token")
        if not guard:
            cmd.append("--no-egress-guard")
        if extra_bridge_args:
            cmd += list(extra_bridge_args)
        env = dict(os.environ)
        if transport:
            env["CDL_TRANSPORT"] = transport      # W10：不拆环境也能验零依赖降级
        self.bridge_cmd = cmd
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     cwd=str(PROJECT_ROOT), env=env)
        ep = wait_endpoint(self.state_dir, want_pid=self.proc.pid)
        self.token = ep["token"]
        self.http_port = ep["httpPort"]
        self.ws_port = ep["wsPort"]
        self.link_port = ep["wsPort"]            # v0.2：WS 与 HTTP 同口
        self.bridge_id = ep["bridgeId"]
        self.transport = ep["transport"]
        self.bridge_stdout: list[str] = []
        self.fake: subprocess.Popen | None = None
        self.fake_stdout: list[str] = []
        if fake_chrome:
            self.spawn_fake(key=key, die_at=die_at, drop_at=drop_at,
                            send_blob=send_blob, wait=wait_fake)
        elif wait_fake:
            pass

    # ---- 单独拉起/重拉假 Chrome（W6/W7/V8′ 会用到）----
    def spawn_fake(self, key: str | None = None, die_at: int = 0, drop_at: int = 0,
                   send_blob: int = 0, wait: bool = True, kill_old: bool = True) -> subprocess.Popen:
        if kill_old:
            self.kill_fake()
        fc = [PY, str(TESTS / "fake_chrome.py"), "--state-dir", str(self.state_dir),
              "--download-root", str(self.dl_root)]
        if key:
            fc += ["--key", key]
        if die_at:
            fc += ["--die-at", str(die_at)]
        if drop_at:
            fc += ["--drop-at", str(drop_at)]
        if send_blob:
            fc += ["--send-blob", str(send_blob)]
        self.fake = subprocess.Popen(fc, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     cwd=str(PROJECT_ROOT))
        if wait:
            assert self.wait_extension(timeout=25.0), self.dump_diag("假 Chrome 没连上（握手可能失败）")
        return self.fake

    def kill_fake(self) -> None:
        if not self.fake:
            return
        self.fake.terminate()
        try:
            out, _ = self.fake.communicate(timeout=6)
            self.fake_stdout.append(out or "")
        except Exception:
            self.fake.kill()
            try:
                self.fake.communicate(timeout=4)
            except Exception:
                pass
        self.fake = None

    # ---- 常用包装 ----
    def cli(self):
        from chrome_downloader import client as client_mod
        return client_mod.Client(host="127.0.0.1", port=self.http_port, token=self.token)

    def wait_extension(self, timeout: float = 20.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.cli().health()["extensionConnected"]:
                    return True
            except Exception:
                pass
            time.sleep(0.15)
        return False

    def wait_jobs(self, ids, timeout: float = 90.0) -> dict:
        """等到终态。超时时把现场 dump 到证据目录，不然只能靠重跑碰运气。"""
        r = self.cli().wait(list(ids), poll=0.4, timeout=timeout)
        if not r["ok"]:
            d = EVIDENCE / "TIMEOUT"
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"wait_timeout_{int(time.time() * 1000)}.json"
            f.write_text(json.dumps(self.diag(list(ids)), ensure_ascii=False, indent=1),
                         encoding="utf-8")
            r["diagFile"] = str(f)
        return r

    def diag(self, ids=None) -> dict:
        out: dict = {}
        try:
            out["health"] = self.cli().health()
        except Exception as e:
            out["health"] = f"<读不到: {e}>"
        out["auditTail"] = self.tail(self.state_dir / "bridge_audit.jsonl", 30)
        out["pairingTail"] = self.tail(self.state_dir / "pairing_audit.jsonl", 20)
        out["fakeFlowTail"] = self.tail(self.state_dir / "fake_chrome_flow.jsonl", 20)
        try:
            out["serverLogTail"] = self.mock.lines()[-15:]
        except Exception as e:
            out["serverLogTail"] = f"<读不到: {e}>"
        if ids:
            try:
                out["jobs"] = [j for j in self.cli().list() if j["jobId"] in set(ids)]
            except Exception as e:
                out["jobs"] = f"<读不到: {e}>"
        return out

    @staticmethod
    def tail(path, n: int) -> list:
        p = Path(path)
        if not p.exists():
            return []
        rows = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return rows[-n:]

    def dump_diag(self, why: str) -> str:
        return f"{why}\n现场：{json.dumps(self.diag(), ensure_ascii=False)[:2500]}"

    def file_of(self, job) -> Path:
        return self.dl_root / job["filename"]

    # ---- 生命周期 ----
    def kill_bridge(self) -> int:
        """硬杀（V8′ 要的正是"来不及收尾"这种死法）。"""
        pid = self.proc.pid
        self.proc.kill()
        try:
            out, _ = self.proc.communicate(timeout=8)
            self.bridge_stdout.append(out or "")
        except Exception:
            pass
        return pid

    def stop_bridge(self) -> int:
        pid = self.proc.pid
        self.proc.terminate()
        try:
            out, _ = self.proc.communicate(timeout=8)
            self.bridge_stdout.append(out or "")
        except Exception:
            self.proc.kill()
        return pid

    def restart_bridge(self, port: int | None = None, keepalive_sec: float | None = None,
                       extra=None) -> dict:
        """在**同一个 state_dir** 上重启：密钥/账本/bridgeId 都在盘上，重启后必须还在。"""
        old_pid = self.proc.pid
        self.kill_bridge()
        time.sleep(0.4)
        cmd = [PY, "-m", "chrome_downloader.bridge", "--port", str(port if port is not None else 0),
               "--state-dir", str(self.state_dir), "--download-root", str(self.dl_root)]
        if keepalive_sec is not None:
            cmd += ["--keepalive-sec", str(keepalive_sec)]
        if extra:
            cmd += list(extra)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     cwd=str(PROJECT_ROOT))
        ep = wait_endpoint(self.state_dir, timeout=40, want_pid=self.proc.pid)
        self.token = ep["token"]
        self.http_port = ep["httpPort"]
        self.ws_port = ep["wsPort"]
        self.link_port = ep["wsPort"]
        self.bridge_id = ep["bridgeId"]
        self.transport = ep["transport"]
        return {"oldPid": old_pid, "newPid": self.proc.pid, "endpoint": ep}

    def shutdown(self, kill_fake: bool = True, kill_bridge: bool = True) -> None:
        if kill_fake:
            self.kill_fake()
        if self.proc.poll() is None:
            self.kill_bridge() if kill_bridge else self.stop_bridge()
        try:
            self.mock.stop()
        except Exception:
            pass
        # 001-F3：rmtree 之前必须确认目标真的是本 harness 自己建的临时目录。
        # 早年间这里误删过用户文件 —— 校验不过就宁可留一堆垃圾也坚决不删。
        d = self.dir
        tmproot = Path(tempfile.gettempdir()).resolve()
        real = d.resolve()
        safe = (d.name.startswith("cdl_") and str(real).lower().startswith(str(tmproot).lower()))
        if safe:
            shutil.rmtree(d, ignore_errors=True)
        else:
            print(f"[harness] 拒绝删除可疑目录 {d} → {real}（只删 {tmproot}/cdl_*）",
                  file=sys.stderr)


def sha256_file(p) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def ev(vid: str, name: str, obj):
    """把证据落到 tests/evidence/<判据>/，返回路径。先落盘再断言，FAIL 也留得下现场。"""
    d = EVIDENCE / vid
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    txt = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
    p.write_text(txt, encoding="utf-8")
    return p
