#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daemon.py —— 后台无感知自动拉起 + 锁协议（PLAN-CHROME-DOWNLOADER-003 · T3，§3.2）

职责：调用方（CLI / SDK / MCP）发现"本机没有能用的 Bridge"时，把 Bridge 拉起来并等它
三级就绪，全程不需要人敲 `python bridge.py`。**本模块自己不联网、不取字节**（C1）：
所有和 Bridge 的对话都复用搬运件 client.py 的 Client（它自带 _assert_loopback）。

四条红线（§3.2.3）的实现位置，逐条对上：
  红线 1（F8-①）拉起代码只跑在**没装熔断**的进程里（CLI/SDK/MCP）。
        反例照抄进注释，不许删：bridge.py 的 exec_looks_like_egress（:142-172）对
        `python/python3/py/node` 有一条"命令里不含 URL 且不含 -m pip/requests/http/
        urllib/socket 就放行"的例外分支。所以 `python -m chrome_downloader.bridge`
        **碰巧**能过 —— 但那是**正则边界**：同一条 argv 里只要多出一段带 `http` 字样的
        路径（例如 `--download-root C:\\http_cache`），或将来改名带上 `chrome_http_dl`，
        同一份代码立刻从"能跑"翻成 EgressBlocked。
        ⇒ 结论：拉起永远由未装闸的父进程发起，Bridge 自己绝不 fork 自己。
        （熔断是 raise EgressBlocked，不是阻塞，所以最坏是 D5 的人话报错，不会死锁。）
  红线 2 "活着"以**三级就绪**为准（wait_ready / probe_levels），pid 只用于陈旧判断。
  红线 3 端口被他人占用时**不接管**（D10）：先做归因，归因不过一个字节都不发过去
        （不发 token、不发 URL）。TOFU 的固有代价不因拉起而改变（§3.2.3 红线 3 前半句）。
  红线 4 父死子活：Windows DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP，
        POSIX start_new_session=True（D6）。

锁协议（§3.2.1）见 acquire_lock：六条分支 + "限 1 次"语义 + try/finally + atexit 释放。
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import paths
from .common import (boot_id, boot_mismatch, humanize, is_loopback, pid_alive, say)

READINESS_TIMEOUT = 10.0          # D1 / D9 的硬口径：≤10 秒
POLL = 0.12
LOCK_RETRY_LIMIT = 1              # §3.2.1「限 1 次」
LOCK_WRITE_GRACE_SEC = 1.5        # 持锁者"建名→写完 payload"的窗口（实测 0.05s 内完成，
                                  # 这里给到 1.5s 是余量；超过就当真坏了）

# 有意不 wait 的 detached 子进程登记处（D6 父死子活）。
# 留引用的目的是**诚实**：一方面避免 Popen 析构时刷 ResourceWarning，
# 另一方面给 daemon status / 测试收尾多一条"这是我起的子进程"的佐证。
_DETACHED_CHILDREN: dict[int, dict] = {}


class DaemonError(RuntimeError):
    """人话错误。CLI/MCP 捕获它，统一转成两三行提示 + 退出码≠0（D5 / L6）。"""


class NotReady(DaemonError):
    pass


class PortTaken(DaemonError):
    """D10：门牌上站着一个不是我们的进程 —— 不接管、不递凭证。"""


# ==========================================================================
# 锁（§3.2.1）
# ==========================================================================
def _now_ms() -> int:
    return int(time.time() * 1000)


def read_lock(state_dir=None) -> dict | None:
    """读锁内容。

    语义分三种，acquire_lock 里走不同分支（N4）：
      * 文件不存在        → **向上抛 FileNotFoundError**（= 持有者刚刚释放）
      * 读不动 / JSON 坏 / 非 dict → None（= 内容不可信，判陈旧）
      * 正常              → dict
    """
    p = paths.daemon_lock_path(state_dir)
    raw = p.read_text(encoding="utf-8")        # FileNotFoundError 有意不吞
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def lock_age_ms(lock: dict) -> float:
    try:
        return _now_ms() - float(lock.get("spawnedAtMs") or 0)
    except (TypeError, ValueError):
        return float("inf")


def is_stale(lock: dict | None, state_dir=None) -> tuple[bool, str]:
    """判陈旧：读不出 / 字段缺 / pid 不在 / bootId 不符 / 锁龄超阈值。

    ⚠️ "读不出"不等于"陈旧"：持锁者刚用 O_EXCL 建好名字、还没写完 payload 的那一瞬间，
    别人读到的就是空文件。那类窗口的处置写在 acquire_lock 里（等满宽限期），
    不在这里——这里只负责"拿到内容之后"的判断。
    """
    if not lock:
        return True, "lock-unreadable-or-corrupt"
    for k in ("pid", "spawnedAtMs"):
        if k not in lock:
            return True, f"lock-missing-{k}"
    if not pid_alive(lock.get("pid")):
        return True, "holder-pid-dead"
    want_boot, got_boot = boot_id(), lock.get("bootId")
    if want_boot and got_boot and boot_mismatch(got_boot, want_boot):
        return True, "bootid-mismatch"
    if lock_age_ms(lock) > paths.lock_stale_sec() * 1000.0:
        return True, f"lock-age>{paths.lock_stale_sec():.0f}s"
    return False, ""


def lock_file_age_ms(state_dir=None) -> float:
    """锁文件的 mtime 年龄（payload 读不出时唯一的可用线索）。"""
    try:
        return _now_ms() - os.path.getmtime(paths.daemon_lock_path(state_dir)) * 1000.0
    except OSError:
        return float("inf")


def read_lock_until_parseable(state_dir=None,
                              grace: float = LOCK_WRITE_GRACE_SEC) -> tuple[dict | None, str]:
    """在**写锁宽限期**内反复读锁，把"别人正在写"与"锁真的坏了"分开。

    返回 (lock, note)：
      * 读到可用内容        → (dict, "")  （坏 JSON 返 None，交上层判陈旧）
      * 文件不见了           → 向上抛 FileNotFoundError（N4 分支 (5)）
      * 宽限期过还是读不出  → (None, "grace-expired")
    """
    t0 = time.time()
    while True:
        try:
            lock = read_lock(state_dir)
        except FileNotFoundError:
            raise                       # 持有者已释放（N4 分支 (5)）
        if lock:
            return lock, ""
        if lock_file_age_ms(state_dir) > grace * 1000.0 or time.time() - t0 > grace:
            return None, "grace-expired"
        time.sleep(0.05)


def audit(state_dir, event: str, **kw) -> None:
    """§3.2.1 审计：每次接管/释放都在 daemon.log 留一行 JSON（D9 靠它断言）。"""
    rec = {"ts": round(time.time(), 3), "event": event, **kw}
    try:
        paths.resolve_state_dir(state_dir).mkdir(parents=True, exist_ok=True)
        with open(paths.daemon_log_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass                                    # 审计写不动不能反过来搞死拉起


class LockHandle:
    """持有者句柄：try/finally + atexit 双保险释放（F1 的四条退出路径）。

    释放时**先验内容再删**：只删属于自己 pid 的锁。实测踩过的那个坑：
    A 被判陈旧 → B 接管 → A 的 finally 照样把 B 的锁删了 → C 又能抢。
    """

    def __init__(self, path: Path, payload: dict):
        self.path, self.payload = path, payload
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            cur = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(cur, dict) and int(cur.get("pid") or -1) != os.getpid():
                return                    # 锁已易主，我不是持有者 → 不许删
        except (OSError, ValueError):
            pass                          # 读不出/已不在：按原语义继续删
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def acquire_lock(state_dir=None, *, argv_hint: str = "",
                 retries: int = LOCK_RETRY_LIMIT) -> "LockHandle | None":
    """抢锁。成功 → LockHandle；抢不到 → None（调用方转为"等对方就绪"）。

    §3.2.1 分支表（逐条实现）：
      (成功) os.open(O_CREAT|O_EXCL) → 立刻写 {pid, spawnedAtMs, bootId, argv}
      (5)   读锁抛 FileNotFoundError → 视为持有者刚释放 → 重试抢锁（**限 1 次**）
      (1)   JSON 坏/字段缺          → 记审计 → 删 → 重抢（**限 1 次**）
      (2)   持有者 pid 不存在        → 同上
      (3)   bootId 不符              → 同上
      (4)   锁龄 > STALE_SEC         → 同上
      (6)   活着且新鲜               → 不抢，返回 None

    "限 1 次"：接管类（1)-(4) 整轮只允许删+重抢一次；N4 的 FileNotFound 重试也只一次。
    循环上限固定 4 轮，第二次抢不到就交回上层等人处置 —— 不进无限循环（防活锁）。
    """
    sd = paths.resolve_state_dir(state_dir)
    sd.mkdir(parents=True, exist_ok=True)
    lp = paths.daemon_lock_path(sd)
    n4_left = True            # 分支 (5) 的那一次
    tookover_left = True      # 分支 (1)-(4) 的那一次
    for _round in range(4):
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                lock = read_lock(sd)
            except FileNotFoundError:
                # 分支 (5)：O_EXCL 失败与读内容之间，持有者刚好 try/finally 释放了
                if n4_left:
                    n4_left = False
                    continue
                return None
            if lock is None:
                # payload 还没写完（空文件/半截 JSON），**这不是陈旧**。
                # 实测病根：照原写法直接删锁重抢，会让 8 并发里冒出 3 个持有者，
                # 于是 D4（硬闸）破防 —— 所以先等满写锁宽限期，再判要不要接管。
                try:
                    lock, note = read_lock_until_parseable(sd)
                except FileNotFoundError:
                    if n4_left:
                        n4_left = False
                        continue
                    return None
                if lock is None and note == "grace-expired":
                    audit(sd, "lock_unreadable_after_grace",
                          ageMs=int(lock_file_age_ms(sd)),
                          graceSec=LOCK_WRITE_GRACE_SEC)
            stale, reason = is_stale(lock, sd)
            if not stale:
                return None                      # 分支 (6)：别人正在干，去等就绪
            if not tookover_left:
                return None                      # 接管过一次还抢不到 → 交回上层
            tookover_left = False
            audit(sd, "lock_takeover", reason=reason,
                  oldPid=(lock or {}).get("pid"),
                  oldAgeMs=int(lock_age_ms(lock)) if lock else None)
            try:
                lp.unlink()
            except OSError:
                pass
            continue
        except OSError as exc:
            raise DaemonError(f"锁文件打不开：{lp}\n→ {humanize(exc)}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            payload = {"pid": os.getpid(), "spawnedAtMs": _now_ms(),
                       "bootId": boot_id(), "argv": argv_hint[:200]}
            fh.write(json.dumps(payload, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())          # 尽快让内容可读：缩小别人的空窗
        h = LockHandle(lp, payload)
        atexit.register(h.release)
        return h
    return None


# ==========================================================================
# 归因：这台 18888 上的进程是不是我们自己的 Bridge？（红线 3 / D10 / D11）
# ==========================================================================
def process_cmdline(pid: int) -> str | None:
    """只读本机进程表，不联网。读不到一律返回 None（保守：不接管、不删）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
                capture_output=True, timeout=15, text=True, errors="replace")
        except (OSError, subprocess.SubprocessError):
            return None
        return (r.stdout or "").strip() or None
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ") \
            .decode("utf-8", "replace").strip()
    except OSError:
        return None


def endpoint_is_ours(info: dict, state_dir=None) -> tuple[bool, str]:
    """endpoint.json 说的进程，是不是"用我们这个 state-dir 起起来的桥"。

    三条判据全用本机信息，**不向对方发送任何 token/URL**：
      1) pid 活着；
      2) 命令行里有本项目的桥（`chrome_downloader.bridge` 或 `bridge.py`）；
      3) 命令行里有我们这个 state-dir（全路径或目录名）。
    任何一条不成立 → False。False 不代表"坏了"，代表"我不认识它"→ 调用方走 D10。
    """
    pid = info.get("pid")
    if not pid_alive(pid):
        return False, "pid-dead"
    cmd = process_cmdline(int(pid))
    if not cmd:
        return False, "cmdline-unreadable"
    low = cmd.lower().replace("\\", "/")
    looks_bridge = ("chrome_downloader/bridge" in low) or ("bridge.py" in low) \
        or ("-m chrome_downloader.bridge" in low)
    if not looks_bridge:
        return False, "cmdline-not-bridge"
    want = str(paths.resolve_state_dir(state_dir)).lower().replace("\\", "/")
    if want and want not in low and Path(want).name.lower() not in low:
        return False, "cmdline-state-dir-mismatch"
    return True, "ours"


def bridge_id_ok(info: dict, expected: str | None) -> tuple[bool, str]:
    """D11：endpoint 里的 bridgeId 与预期不符 → 拒绝复用（不换身份、不递凭证）。"""
    if not expected:
        return True, ""
    got = info.get("bridgeId")
    return (got == expected,
            "" if got == expected else f"bridgeId 不符：实际 {got!r}，预期 {expected!r}")


# ==========================================================================
# 三级就绪（§3.2.2 / F7）
# ==========================================================================
def probe_levels(info: dict, state_dir=None) -> dict:
    """L1 endpoint 可用 / L2 health 通且 egressGuard=true / L3 配对面就绪。"""
    out = {"L1": False, "L2": False, "L3": False, "detail": {}}
    if not isinstance(info, dict) or not info.get("httpPort"):
        return out
    out["L1"] = True
    host = info.get("httpHost", "127.0.0.1")
    if not is_loopback(host):
        out["detail"]["host"] = f"非回环地址，拒绝连接：{host}"
        return out
    try:
        from .client import Client              # 搬运件：内部 _assert_loopback 再钉一次
        c = Client(host=host, port=info["httpPort"], token=info.get("token"),
                   timeout=4.0)
        h = c.health()
    except Exception as exc:
        out["detail"]["health"] = humanize(exc)
        return out
    guard = h.get("egressGuard") or {}
    out["L2"] = bool(h.get("ok")) and bool(guard.get("enabled"))
    explicit = h.get("pairingReady")
    out["L3"] = bool(paths.pairing_key_path(state_dir).exists()) or bool(explicit) \
        or bool(h.get("paired"))
    out["detail"].update({"paired": h.get("paired"),
                          "extensionConnected": h.get("extensionConnected"),
                          "transport": h.get("transport"), "port": h.get("httpPort"),
                          "bridgeId": h.get("bridgeId"),
                          "egressGuard": {"enabled": guard.get("enabled"),
                                          "blocked": guard.get("blocked")}})
    return out


def ready(info: dict, state_dir=None) -> bool:
    lv = probe_levels(info, state_dir)
    return lv["L1"] and lv["L2"] and lv["L3"]


def _not_loopback(info: dict, lv: dict) -> bool:
    return bool((lv.get("detail") or {}).get("host"))


def wait_ready(state_dir=None, timeout: float = READINESS_TIMEOUT) -> dict:
    """轮询到三级就绪。返回 endpoint dict（附 readyLevels）；超时抛 NotReady。"""
    sd = paths.resolve_state_dir(state_dir)
    t0 = time.time()
    last: dict = {}
    while time.time() - t0 < timeout:
        info = paths.read_endpoint(sd)
        if info:
            lv = probe_levels(info, sd)
            last = lv
            if lv["L1"] and lv["L2"] and lv["L3"]:
                out = dict(info)
                out["readyLevels"] = lv
                return out
            if _not_loopback(info, lv):
                # 记账本上写着非回环地址：这不是"还没好"，是"不是我们的桥"，别白等
                raise PortTaken(
                    f"门牌 {info.get('httpPort')} 的记账本写着非回环地址"
                    f"（{lv['detail']['host']}）。我没有向它发送任何 token/URL。")
    raise NotReady(
        "Bridge 没在 {t:.0f} 秒内三级就绪。现场：{lv}\n"
        "→ 看日志：{log}\n"
        "→ 手动起一次看它报什么：python -m chrome_downloader.bridge --state-dir \"{sd}\"\n"
        "→ 想关掉自动拉起：set CHROME_DL_NO_AUTOSPAWN=1".format(
            t=timeout, lv=json.dumps(last, ensure_ascii=False)[:300],
            log=paths.daemon_log_path(sd), sd=sd))


# ==========================================================================
# 拉起
# ==========================================================================
def count_bridge_processes_in_dir(state_dir=None) -> dict | int:
    """数一数“用着这个 state-dir 的活桥”有几个（D2 / D4 的硬口径）。

    ⚠️ 不能只读 endpoint.json 的 pid：那个文件只记**最后一个写它的**桥。
       实测拿它当计数会把"起了 3 个"读成"起了 1 个" —— 那 D4 就成了假绿。
       所以这里扫本机进程表，按命令行里的 --state-dir 归因；扫不动才退化回
       endpoint 归因，并如实带 `method` 字段说清用的是哪一种。
    """
    sd = str(paths.resolve_state_dir(state_dir)).lower().replace("\\", "/")
    procs = _list_python_cmdlines()
    if procs is not None:
        hits = []
        for pid, cmd in procs:
            low = (cmd or "").lower().replace("\\", "/")
            looks_bridge = ("chrome_downloader.bridge" in low) or ("bridge.py" in low)
            # 必须比**完整的 --state-dir 参数值**：只比目录名会抹到所有跑次（它们都叫
            # "state"）。实测就是这个错把别的跑次里残留的桥计了进来，D2 虚报 count=2。
            if looks_bridge and (f"--state-dir {sd}" in low or
                                 f'--state-dir "{sd}"' in low or
                                 f"--state-dir={sd}" in low):
                hits.append(pid)
        return {"count": len(set(hits)), "pids": sorted(set(hits)),
                "method": "cmdline-scan-by-full-state-dir-path",
                "matchedOn": f"--state-dir {sd}"}
    info = paths.read_endpoint(state_dir)
    if not info:
        return {"count": 0, "pids": [], "method": "endpoint-only(scan-unavailable)"}
    ours, _why = endpoint_is_ours(info, state_dir)
    return {"count": 1 if (ours and pid_alive(info.get("pid"))) else 0,
            "pids": [int(info["pid"])] if (ours and pid_alive(info.get("pid"))) else [],
            "method": "endpoint-only(scan-unavailable)"}


def _list_python_cmdlines() -> list[tuple[int, str]] | None:
    """本机 python 进程的 (pid, cmdline)。只读本机进程表，不联网。"""
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
                 "Name='pythonw.exe'\" | ForEach-Object { [string]$_.ProcessId + "
                 "'@|@' + $_.CommandLine }"],
                capture_output=True, timeout=40)
        except (OSError, subprocess.SubprocessError):
            return None
        txt = (r.stdout or b"").decode("gbk", "replace")      # 中文 Windows 输出是 GBK
        if r.returncode != 0 or "@|@" not in txt:
            return None
        out: list[tuple[int, str]] = []
        for line in txt.splitlines():
            pid, _, cmd = line.partition("@|@")
            try:
                out.append((int(pid.strip()), cmd.strip()))
            except ValueError:
                continue
        return out
    try:
        rows: list[tuple[int, str]] = []
        for p in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                cmd = p.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                rows.append((int(p.parent.name), cmd.strip()))
            except OSError:
                continue
        return rows
    except OSError:
        return None


def build_bridge_argv(state_dir=None, port: int | None = None,
                      download_root=None, extra=None) -> list[str]:
    sd = paths.resolve_state_dir(state_dir)
    root = paths.download_root(download_root)
    cmd = [sys.executable, "-m", "chrome_downloader.bridge",
           "--port", str(paths.BRIDGE_PORT if port is None else port),
           "--state-dir", str(sd), "--download-root", str(root)]
    if extra:
        cmd += list(extra)
    return cmd


def spawn_bridge(state_dir=None, port: int | None = None, download_root=None,
                 extra=None, env: dict | None = None) -> tuple:
    """父死子活地拉起 Bridge（红线 4 / D6）。stdout+stderr 一律进 <state>/bridge.log（D7）。

    ⚠️ 不用 shell、不拼字符串、参数里没有任何 URL/token：
       命令里唯一的本机信息是 state-dir 与 download-root 路径，它们只进本机子进程。
    """
    sd = paths.ensure_state_dir(state_dir)
    cmd = build_bridge_argv(sd, port, download_root, extra)
    child_env = dict(os.environ if env is None else env)
    pkg_parent = str(paths.package_dir().parent)          # src/ —— 让 -m 找得到包
    pp = child_env.get("PYTHONPATH")
    child_env["PYTHONPATH"] = os.pathsep.join(
        [pkg_parent] + ([pp] if pp else []))
    child_env.pop("CDL_TRANSPORT", None)                  # 别让调用方的降级开关传染子进程
    try:
        fh = open(paths.bridge_log_path(sd), "ab")
    except OSError as exc:
        raise DaemonError(f"日志写不了：{paths.bridge_log_path(sd)}\n→ {humanize(exc)}") \
            from exc
    kwargs: dict = dict(stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        cwd=str(paths.package_dir()), env=child_env, close_fds=True)
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        fh.close()
        raise DaemonError(f"桥起不来：{humanize(exc)}\n→ 手动起："
                          f"python -m chrome_downloader.bridge --state-dir \"{sd}\"") from exc
    fh.close()
    state = {"pid": proc.pid, "cmdline": cmd, "stateDir": str(sd),
             "spawnedAt": round(time.time(), 3), "bootId": boot_id()}
    try:
        paths.daemon_state_path(sd).write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    _DETACHED_CHILDREN[proc.pid] = {"proc": proc, "state": state}
    audit(sd, "bridge_spawned", pid=proc.pid, port=cmd[cmd.index("--port") + 1])
    return proc, state


def ensure_bridge(state_dir=None, *, port: int | None = None, download_root=None,
                  timeout: float = READINESS_TIMEOUT, spawn: bool = True,
                  expected_bridge_id: str | None = None, quiet: bool = False,
                  env: dict | None = None) -> dict:
    """"给我一个能用的桥"。返回 {endpoint, spawned, reused, pid, tookLock, reason}。

    每一步对应判据：
      1) 读 endpoint → 归因 → 三级就绪？ 直接复用（**D2：不新起，进程数/pid 不变**）
      2) 归因不过但 pid 活着 → **不动它、不递凭证**，PortTaken（**D10**）
      3) bridgeId 与预期不符 → 拒绝复用 + 处置引导（**D11**）
      4) pid 死了/账本没有 → 清陈账本（**D3**）→ 抢锁 → 抢到的人拉起，
         没抢到的人等对方就绪（**D4**）
      5) 等不到就绪 → NotReady（人话 + 退出码≠0，**D5**）
    """
    sd = paths.resolve_state_dir(state_dir)
    if paths.no_autospawn():
        raise DaemonError(
            "自动拉起已被关掉（CHROME_DL_NO_AUTOSPAWN=1）。\n"
            f"→ 自己起一次：python -m chrome_downloader.bridge --state-dir \"{sd}\"")
    info = paths.read_endpoint(sd)
    if info:
        ours, why = endpoint_is_ours(info, sd)
        if ours:
            ok_id, id_why = bridge_id_ok(info, expected_bridge_id)
            if not ok_id:
                raise PortTaken(
                    f"{id_why}\n→ 这台机器上的桥换过身份（换了 state-dir，或重装过）。"
                    "处置：chrome-dl daemon stop，然后在 Chrome 扩展里点"
                    "「断开并忘记此程序」，再重试。")
            if ready(info, sd):
                return {"endpoint": info, "spawned": False, "reused": True,
                        "pid": info.get("pid"), "tookLock": False,
                        "reason": "already-ready"}
            # 是我们的桥、身份也对，但还没就绪 → 多半正在启动。等 3 秒，别抢。
            try:
                got = wait_ready(sd, timeout=min(timeout, 3.0))
                return {"endpoint": got, "spawned": False, "reused": True,
                        "pid": got.get("pid"), "tookLock": False,
                        "reason": "waited-for-existing"}
            except PortTaken:
                raise
            except NotReady:
                raise NotReady(
                    f"桥进程 {info.get('pid')} 活着但问不通（归因={why}）。\n"
                    f"→ 重启它：chrome-dl daemon restart") from None
        elif pid_alive(info.get("pid")):
            raise PortTaken(
                f"门牌 {info.get('httpPort')} 上有一个我不认识的进程"
                f"（pid={info.get('pid')}，归因={why}）。\n"
                "→ 我没有向它发送 token/URL。请先确认它是什么程序；"
                f"要换端口段起：python -m chrome_downloader.bridge --port "
                f"{paths.BRIDGE_PORT + 1}")
        else:
            # D3：pid 已死 → 陈旧账本，清掉重起（端口漂了就跟随新门牌）
            audit(sd, "stale_endpoint_removed", pid=info.get("pid"),
                  port=info.get("httpPort"))
            try:
                paths.endpoint_path(sd).unlink()
            except OSError:
                pass
    if not spawn:
        raise DaemonError(f"本机没有可用的桥（spawn=False）。→ 先跑 "
                          f"python -m chrome_downloader.bridge --state-dir \"{sd}\"")
    h = acquire_lock(sd, argv_hint="ensure_bridge")
    if h is None:
        got = wait_ready(sd, timeout=timeout)             # 分支 (6)：等对方就绪
        return {"endpoint": got, "spawned": False, "reused": True,
                "pid": got.get("pid"), "tookLock": False, "reason": "peer-spawning"}
    try:
        info = paths.read_endpoint(sd)                    # 抢到锁后再看一眼
        if info:
            ours, _w = endpoint_is_ours(info, sd)
            if ours and ready(info, sd):
                return {"endpoint": info, "spawned": False, "reused": True,
                        "pid": info.get("pid"), "tookLock": True,
                        "reason": "ready-inside-lock"}
        proc, state = spawn_bridge(sd, port=port, download_root=download_root,
                                   env=env)
        got = wait_ready(sd, timeout=timeout)
        if not quiet:
            say(f"已在后台启动下载桥（pid={proc.pid}，门牌 {got.get('httpPort')}）",
                err=False)
        audit(sd, "bridge_ready", pid=proc.pid, port=got.get("httpPort"),
              levels=got.get("readyLevels", {}).get("detail", {}))
        return {"endpoint": got, "spawned": True, "reused": False, "pid": proc.pid,
                "tookLock": True, "reason": "spawned", "daemon": state}
    finally:
        h.release()                                       # 成功/失败/异常/超时 四条路径都释放


def stop_bridge(state_dir=None, *, timeout: float = 8.0) -> dict:
    """chrome-dl daemon stop：只结束**归因为"我们的桥"**的进程，并落到 D13 的三文件状态。

    归因不过 → 一律不动（AGENTS.md §8 无人值守禁令 / STOP-13 同一口径），
    在返回值的 refused 里写清楚为什么没动。
    """
    sd = paths.resolve_state_dir(state_dir)
    out = {"stateDir": str(sd), "killed": None, "refused": None,
           "endpointRemoved": False, "daemonJsonRemoved": False, "lockRemoved": None}
    info = paths.read_endpoint(sd)
    if info:
        ours, why = endpoint_is_ours(info, sd)
        if ours and pid_alive(info.get("pid")):
            pid = int(info["pid"])
            if os.name == "nt":
                import subprocess as _sp
                # taskkill 的中文输出在中文 Windows 上是 GBK，用 utf-8 解会爆（实测踩过）
                def _tk(args):
                    r = _sp.run(["taskkill", "/PID", str(pid)] + args,
                                capture_output=True, timeout=20)
                    txt = (r.stdout or b"") + (r.stderr or b"")
                    return r.returncode, txt.decode("gbk", "replace").strip()
                rc, note = _tk(["/T"])                    # 先温和地整树结束
                if rc == 0:
                    t0 = time.time()
                    while pid_alive(pid) and time.time() - t0 < min(timeout, 4.0):
                        time.sleep(0.2)                   # 温和结束生效需要一点时间
                if rc != 0 or pid_alive(pid):
                    # 实测：DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP 起的桥，
                    # 不带 /F 时 taskkill 会拒（"只能强行终止"），必须升级。
                    rc2, note2 = _tk(["/T", "/F"])
                    note = (f"第一次（不带 /F）rc={rc}：{note[:120]} "
                            f"→ 升级 /F rc={rc2}：{note2[:160]}")
                    rc = rc2
                out["killed"] = {"pid": pid, "returncode": rc, "note": note[:400],
                                 "aliveAfterKillAttempt": pid_alive(pid)}
            else:
                import signal
                try:
                    os.kill(pid, signal.SIGTERM)
                    out["killed"] = {"pid": pid, "returncode": 0, "note": "SIGTERM"}
                except OSError as exc:
                    out["killed"] = {"pid": pid, "returncode": 1, "note": str(exc)}
            t0 = time.time()
            while pid_alive(pid) and time.time() - t0 < timeout:
                time.sleep(0.2)
            audit(sd, "bridge_stopped_by_daemon", pid=pid, aliveAfter=pid_alive(pid))
        elif pid_alive(info.get("pid")):
            out["refused"] = (f"门牌 {info.get('httpPort')} 上的进程归因不过"
                              f"（{why}，pid={info.get('pid')}），我没有动它")
    for key, p in (("endpointRemoved", paths.endpoint_path(sd)),
                   ("daemonJsonRemoved", paths.daemon_state_path(sd))):
        try:
            p.unlink()
            out[key] = True
        except FileNotFoundError:
            out[key] = False
        except OSError as exc:
            out[key] = f"删除失败：{humanize(exc)}"
    lp = paths.daemon_lock_path(sd)
    try:
        lp.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        out["lockRemoved"] = f"删除失败：{humanize(exc)}"
    if out["lockRemoved"] is None:
        out["lockRemoved"] = not lp.exists()
    audit(sd, "daemon_stop", **{k: out[k] for k in
                                ("endpointRemoved", "daemonJsonRemoved", "lockRemoved")})
    return out


def status(state_dir=None) -> dict:
    """chrome-dl daemon status 的数据面（**L3**：JSON 可解析 + 字段固定）。

    固定 15 个字段，一个不多一个不少 —— 少字段等于机器读不到，多字段等于口径漂移。
    """
    sd = paths.resolve_state_dir(state_dir)
    info = paths.read_endpoint(sd)
    lv = probe_levels(info, sd) if info else {"L1": False, "L2": False, "L3": False,
                                              "detail": {}}
    owned, why = ((False, "no-endpoint") if not info else endpoint_is_ours(info, sd))
    try:
        lock = read_lock(sd)
    except FileNotFoundError:
        lock = None
    d = lv.get("detail") or {}
    return {
        "stateDir": str(sd),
        "endpointPresent": bool(info),
        "bridgePid": info.get("pid") if info else None,
        "bridgeAlive": pid_alive(info.get("pid")) if info else False,
        "ownedByUs": bool(owned),
        "attribution": why,
        "port": d.get("port") or (info or {}).get("httpPort"),
        "transport": d.get("transport"),
        "paired": d.get("paired"),
        "extensionConnected": d.get("extensionConnected"),
        "egressGuard": d.get("egressGuard"),
        "readyLevels": {"L1": bool(lv["L1"]), "L2": bool(lv["L2"]),
                        "L3": bool(lv["L3"]),
                        "ready": bool(lv["L1"] and lv["L2"] and lv["L3"])},
        "daemonJsonPresent": paths.daemon_state_path(sd).exists(),
        "lockPresent": paths.daemon_lock_path(sd).exists(),
        "lockInfo": lock,
        "noAutospawn": paths.no_autospawn(),
    }


# ==========================================================================
# 给 CLI / MCP / SDK 用的门面
# ==========================================================================
def make_client(state_dir=None, *, port: int | None = None, download_root=None,
                ensure: bool = True, timeout: float = READINESS_TIMEOUT):
    """返回连好的搬运件 Client；ensure=True 时先确保三级就绪（G1 的落点）。"""
    from .client import Client, BridgeError
    if ensure:
        try:
            info = ensure_bridge(state_dir, port=port, download_root=download_root,
                                 timeout=timeout)["endpoint"]
        except DaemonError as exc:
            raise BridgeError(str(exc)) from exc
    else:
        info = paths.read_endpoint(state_dir)
        if not info:
            raise BridgeError(
                f"找不到 {paths.endpoint_path(state_dir)}\n"
                "→ 桥没在跑。先：chrome-dl daemon start"
                "（或 python -m chrome_downloader.bridge）")
    return Client(host=info.get("httpHost"), port=info.get("httpPort"),
                  token=info.get("token"), timeout=30.0)


class AutospawnClient:
    """**D12** 的口径：从"无桥"出发，`Client()` → 自动拉起 → 提交 → COMPLETE。

    为什么不改 client.py 的 __init__：它是搬运件，§0.1 用 sha256 逐字节钉着
    （f7daca7d…），V10 的两份证据（imports / CLI 面）也直接读它的源文件。
    在这里加自动拉起 = 改搬运件 = 判据红。所以给一个**同签名薄壳**：
      · 构造时先 ensure_bridge（三级就绪），再委托真 Client；
      · 方法全部透传，SDK 面与 v0.2 一致；
      · 包外用法：`from chrome_downloader import Client` 拿到的就是这个壳。
    """

    def __init__(self, host=None, port=None, token=None, state_dir=None,
                 timeout=30.0, transport="sse", ensure: bool = True):
        if host is not None or token is not None:
            # 调用方显式给了地址/凭证 → 说明它自己知道在连谁，不越权替它拉起
            from .client import Client
            self._inner = Client(host=host, port=port, token=token,
                                 state_dir=str(state_dir) if state_dir else None,
                                 timeout=timeout, transport=transport)
        else:
            self._inner = make_client(state_dir, port=port, ensure=ensure,
                                      timeout=timeout)

    @property
    def inner(self):
        return self._inner

    def __getattr__(self, name):
        return getattr(self.__dict__["_inner"], name)

    def __repr__(self):
        i = self.__dict__["_inner"]
        return f"<AutospawnClient {i.host}:{i.port}>"
