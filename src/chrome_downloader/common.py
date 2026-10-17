#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
common.py —— 新增面共用的小工具（daemon / cli / mcp_server 都用它）

三条设计约束（都来自计划的硬判据，不是风格偏好）：

1. **零第三方依赖**（C3 / P5）：只用标准库。Bridge 有零依赖降级实现，
   CLI/MCP 更不许装东西，否则 base 环境就被污染了（P7）。
2. **不许成为字节通道**（C1）：本模块里没有任何取字节的库（无标准库 http 客户端、
   无第三方请求库、无 socket 出站）。要连本机 Bridge 一律复用 client.py 的 Client，
   不另起第二条网络路径。
   （注：这里刻意**不写**那些库的名字 —— 写了本文件就成了静态扫描自己的一处命中。）
3. **MCP stdout 洁净**（C7 / M6 / M7）：所有日志走 stderr 或 daemon.log；
   本模块的 humanize() 只负责把异常变成两三行人话，不打印 traceback。

⚠️ C1 的取体静态扫描词表**不放在这里**：那些模式串一旦落在被扫描的文件里，
   本文件就成了自己的命中（自指假阳性）。它们住在 tests/ 侧（与 v0.2 的
   FORBIDDEN_STATIC 同一处置：test_downloader.py 自己不在 AUDIT_SCOPE 里）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# 交付根与证据目录（tests/ 与 src/ 是兄弟；证据目录在包树之外，同 v0.2 位置不变）
PACKAGE_ROOT = Path(__file__).resolve().parent


def delivery_root() -> Path:
    """包目录（搬运件所在处）。"""
    return PACKAGE_ROOT


def default_evidence_dir() -> Path:
    """证据目录：优先 CHROME_DL_EVIDENCE_DIR（测试会指到临时副本）。

    ⚠️ 这是"谁在写证据"的唯一入口。测试写的是它自己指定的目录，
    IMMUTABLE 那 37 个目录（§8.4.1）的原始位置不被动 —— §8.4 的基线靠这个成立。
    """
    v = os.environ.get("CHROME_DL_EVIDENCE_DIR")
    if v:
        return Path(v)
    return PACKAGE_ROOT.parent.parent / "tests" / "evidence"


# ==========================================================================
# 人话化（D5 / L6 / D10：报错必须是"下一步敲什么"，不许甩 traceback）
# ==========================================================================
def humanize(exc: BaseException, prefix: str = "") -> str:
    """把异常压成两三行中文人话。返回字符串，不打印。"""
    name = type(exc).__name__
    msg = str(exc).strip()
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10013:
        return (f"{prefix}这个门牌被别的软件占了（Windows 拒绝绑定）。"
                f"→ 换端口：chrome-dl daemon start --port 18889")
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (98, 48, 10048):
        return f"{prefix}端口已被占用。→ 先看看是谁：netstat -ano | findstr :18888"
    if name in ("ConnectionRefusedError",):
        return (f"{prefix}本机 Bridge 没在听。→ 启动它：chrome-dl daemon start"
                f"（或关掉自动拉起：set CHROME_DL_NO_AUTOSPAWN=1）")
    if not msg:
        return f"{prefix}{name}"
    first = msg.splitlines()[0]
    return f"{prefix}{name}: {first[:200]}"


def say(msg: str, *, err: bool = True) -> None:
    """新增面统一输出：人话走 stderr（MCP 的 stdout 必须只放 JSON-RPC）。"""
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


# ==========================================================================
# 证据落盘（新增判据 D*/M*/L*/B*/V4b2 用它；格式与 v0.2 的 ev() 一致）
# ==========================================================================
def ev(vid: str, name: str, obj, evidence_dir: Path | None = None):
    d = (evidence_dir or default_evidence_dir()) / vid
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    txt = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
    p.write_text(txt, encoding="utf-8")
    return p


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ==========================================================================
# 回环判定（D10：端口被占时不接管、不发送任何 URL/token）
# 复用 bridge.py 的口径，但**不 import bridge**（避免在 CLI 进程里装熔断/拖进 asyncio）。
# ==========================================================================
LOOPBACK_NAMES = {"127.0.0.1", "::1", "localhost", "localhost.", ""}


def is_loopback(target) -> bool:
    host = target[0] if isinstance(target, (tuple, list)) else str(target)
    host = str(host).strip()
    if host.lower() in LOOPBACK_NAMES or host == "::1":
        return True
    if host.startswith("127."):
        return True
    import re
    m = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})", host)
    return bool(m and int(m.group(1)) == 127)


def pid_alive(pid: int | None) -> bool:
    """pid 是否还活着（只问本机，不出网）。"""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                  # 存在但不是我们的进程
    except OSError:
        return False
    return True


def _win_pid_alive(pid: int) -> bool:
    """Windows：OpenProcess + GetExitCodeProcess（不弹黑框、不出网）。

    拿不到 ctypes（非 CPython）时退化为 tasklist 查询，同样只碰本地。
    """
    try:
        import ctypes                 # noqa: PLC0415
        from ctypes import wintypes   # noqa: PLC0415
    except ImportError:
        return _win_pid_alive_tasklist(pid)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        # 打不开有两种：进程不存在，或权限不足。用 CreateToolhelp32 快照再判一次，
        # 免得把"活着但我没权限看"误判成"死了"→ 那样会去抢别人的锁。
        return _win_pid_alive_tasklist(pid)
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return _win_pid_alive_tasklist(pid)
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _win_pid_alive_tasklist(pid: int) -> bool:
    import subprocess                 # noqa: PLC0415
    try:
        r = subprocess.run(["tasklist", "/fi", f"PID eq {pid}", "/nh", "/fo", "csv"],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False                 # 查不动就当活着（保守：不乱抢锁）
    txt = r.stdout.decode("gbk", "replace") if r.stdout else ""
    return f'"{pid}"' in txt or f" {pid} " in txt


# bootId 比较容差（秒）：Windows 上"开机那一秒"是由 GetTickCount64 反推的，
# 两个调用者各自采样可以有秒级差。真跨开机时差值在小时/天级，120s 不会漏。
BOOT_ID_TOLERANCE_SEC = 120


def boot_id() -> str | None:
    """启动标识（§3.2.1）。取不到返回 None，调用方退化为"只用 pid + 锁龄"。

    ⚠️ 实测量出的坑（D4 并发闸因此当场红）：计划写的 `GetTickCount64()` **不是常量**，
       它是"开机以来的滞秒数"，两次调用必不同。直接拿它当 bootId 会让
       每个调用者都把别人的锁判成 "bootid-mismatch → 陈旧" → 删锁重抢 →
       8 并发里冒出 5 个桥。另外 ctypes 默认 restype 是 c_int，不写 c_ulonglong
       会被截断（本机实测读数明显小于真实 uptime）。
       ⇒ 这里先换算成"开机发生在哪一秒"（同一开机周期内几乎不变），
         比较时再给 BOOT_ID_TOLERANCE_SEC 容差，不拿严格相等当判据。
    """
    if os.name == "nt":
        try:
            import ctypes             # noqa: PLC0415
            k32 = ctypes.windll.kernel32
            k32.GetTickCount64.restype = ctypes.c_ulonglong    # 不写会被当 c_int 截断
            ms = int(k32.GetTickCount64())
            return f"win-{int(time.time() - ms / 1000.0)}"
        except Exception:
            return None
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def boot_mismatch(stored: str | None, current: str | None) -> bool:
    """两个 bootId 算不算"来自不同开机"。

    Windows：换算值带秒级抖动 → 用容差；真跨开机远大于容差 → 仍能拓住。
    POSIX（/proc 里的 UUID）：本来就该逐字相等。
    取不到任何一侧 → False（不用它做陈旧判据，退化到 pid + 锁龄）。
    """
    if not stored or not current:
        return False
    if stored.startswith("win-") and current.startswith("win-"):
        try:
            a, b = int(stored[4:]), int(current[4:])
        except ValueError:
            return True
        return abs(a - b) > BOOT_ID_TOLERANCE_SEC
    return stored != current
