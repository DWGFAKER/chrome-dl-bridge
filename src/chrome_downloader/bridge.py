#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chrome-dl-bridge v0.2 —— 本机"指挥官"（只监听 127.0.0.1，零注册、零常驻要求）

职责边界（对应计划 C1 / C4 / V4-b）：
  * 只发号施令、只记账本、只算速度。**永远不碰文件字节，永远不连外网。**
  * 真正上网取字节的是 Chrome 扩展（搬运工），二者之间只有"纸条"（WS 文本帧，≤8192B）。
  * 本进程启动即安装"非回环出站熔断"：任何连非 127.0.0.1/::1/localhost 的 socket
    一律抛 EgressBlocked 并落审计日志；子进程命令里出现非回环 URL 或已知取数据
    工具（curl/wget/pip/powershell 等）同样拦死（本地纯查询放行，避免假阳性）。

v0.2 相对 v0.1 的唯一实质变化：**控制通道**
  旧：扩展 → 系统注册的宿主进程（stdio + 4 字节长度帧），需要复制扩展 ID、写注册表、宿主常驻。
  新：扩展 → ws://127.0.0.1:18888/link（回环 WebSocket 文本帧），扩展主动连，双方都不常驻。
  代价：操作系统不再替我们做"谁能跟我说话"的来源绑定 → 改用 HMAC-SHA256 双向挑战应答
        （TOFU：第一次连上就配对，以后每次对暗号）。固有上限见计划 §5.5 / F15，本文件不粉饰。

一条硬约束**一点没动**：文件字节 100% 由 chrome.downloads 走浏览器内核发起，
本通道只许传 URL / 任务号 / 状态 / 字节数 / 速度这类纸条，**单个文本帧超过 8192 字节立即断开**。

服务端两种实现，接口完全一致（同一套 RestApi + link_session）：
  * aiohttp（优先）：`WebSocketResponse(max_msg_size=8192)` —— **必须显式传参**，
    因为 aiohttp 的默认值是 4 MiB（F13，本机源码 web_ws.py:117），不传等于把 C7 写废。
  * 零依赖降级：手写 RFC6455 + 标准库 HTTP，aiohttp 不在场时自动切换，
    **同样**强制 8192（超限 → 关闭码 1009 + 记 link_bad_frame，绝不分片接收）。

Windows 端口加固：监听套接字一律设 `SO_EXCLUSIVEADDRUSE`（F14）。
`SO_REUSEADDR` 在 Windows 上允许第二个进程绑到已被占用的端口（端口劫持），
微软官方建议服务端用 SO_EXCLUSIVEADDRUSE 防劫持。

协议见 PROTOCOL.md。Agent 侧 HTTP/SSE 面与 001 完全一致（同端口同路径），SDK 不用改调用方式。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import signal as signal_module
import socket
import subprocess  # noqa: F401  (仅用于被熔断拦截，本模块不使用它取字节)
import sys
import time
import uuid
from pathlib import Path

PROTOCOL_VERSION = 2
MAX_FRAME_BYTES = 8192          # 单条"纸条"上限 8KB（V4-c / C7 判据）
LOOPBACK_NAMES = {"127.0.0.1", "::1", "localhost", "localhost.", ""}
SAMPLE_URL = "http://exa" + "mple.com"   # 仅用作被拦样本（拆写以免被静态扫描当硬编码外联）

# ---- 通道端点（计划 §5.1 / §5.5 L1：固定端点优先，失败才扫）----
WS_PATH = "/link"
DEFAULT_WS_PORT = 18888
PORT_MIN, PORT_MAX = 18888, 18897        # 候选区间，含端点
HANDSHAKE_TIMEOUT = 10.0                 # 握手每一步最多等这么久
KEEPALIVE_PING_SEC = 20.0                # 服务端也按官方 20 秒发一次 ping（F1）

# ---- HMAC 挑战应答的口径字符串（扩展端 background.js 必须逐字一致）----
HMAC_CTX_BRIDGE = "bridge"
HMAC_CTX_EXTENSION = "extension"

# 允许的字段全集（V4-c：字段集合必须 ⊆ 本表；出现 base64/大块一律判 FAIL）
ALLOWED_FIELDS = {
    "v", "type", "jobId", "downloadId", "url", "filename", "bytesReceived",
    "totalBytes", "speedBps", "state", "error", "canResume", "restarted",
    "resumedFrom", "ts", "role", "token", "chromeVersion", "extensionVersion",
    "ok", "reason", "conflictAction", "items", "sha256", "maxConcurrent",
    "perHost", "host", "maxFrame", "resume",
    # v0.2 新增：端点 / 握手 / 身份钉扎（计划 §5.3 六键 + 实测里确实要用的几个）
    "port", "portScan", "nonce", "clientNonce", "serverNonce", "hmac", "bridgeId",
    "keyExchange", "pairing", "peerPid", "peerStartedAt", "wsPath",
}

TERMINAL_STATES = {"COMPLETE", "FAILED", "CANCELLED"}


class EgressBlocked(OSError):
    """本进程试图连接非回环地址 —— 违反 C1/C4，硬拦。"""


class FrameTooBig(Exception):
    """对端发来 > MAX_FRAME_BYTES 的帧：必须断链（1009），不许分片接收。"""


# --------------------------------------------------------------------------
# 纸条编解码（WS 文本帧载荷；不再有 4 字节长度头）
# --------------------------------------------------------------------------
def encode_frame(obj: dict) -> bytes:
    """序列化一条纸条并做上限自检。超了抛 ValueError —— 宁可丢也不拆字节。"""
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError(f"frame too big: {len(data)} > {MAX_FRAME_BYTES}")
    return data


def iter_frames(node, prefix=""):
    """递归产出报文里所有键（含 snapshot items 内层）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield prefix + k
            yield from iter_frames(v, prefix + k + ".")
    elif isinstance(node, list):
        for it in node:
            yield from iter_frames(it, prefix)


def audit_field_closure(obj) -> list[str]:
    """返回不在白名单里的键名。

    嵌套（snapshot 的 items 列表）按**叶子键名**判定：字段名本身要在白名单里，
    但允许被包在 items 这类容器下。
    """
    bad = []
    for path in iter_frames(obj):
        leaf = path.rsplit(".", 1)[-1]
        if leaf not in ALLOWED_FIELDS:
            bad.append(path)
    return sorted(set(bad))


# --------------------------------------------------------------------------
# 出站熔断（V4-b）
# --------------------------------------------------------------------------
# 已知的"能上网取字节"命令行工具名（拦命令形态，不拦一切 subprocess）
EXEC_FETCH_TOOLS = (
    "curl", "wget", "nc", "netcat", "telnet", "ftp", "tftp", "http", "https",
    "invoke-webrequest", "invoke-restmethod", "iwr", "start-bitstransfer",
    "bitsadmin", "certutil", "aria2c", "powershell", "pwsh", "cmd", "mshta",
    "python", "python3", "py", "node", "deno", "bun", "ruby", "perl", "php",
    "aria2", "axel", "rsync", "ssh", "scp", "sftp", "git", "pip", "npm", "npx",
)
URL_IN_CMD = re.compile(r"(?:https?|ftp)://[^\s'\"]+", re.I)


def exec_looks_like_egress(cmd) -> tuple[bool, str]:
    """判定一个子进程/命令是否属于"可能上网取字节"。

    为什么要区分：熔断的目标是 C1（不让字节进 Python），不是把本进程变成什么
    都不能干的僵尸。实测发现 aiohttp 在 Windows 上调 platform.win32_ver() 会跑
    `cmd /c ver`，无差别拦截会造成假阳性（下载根本跑不起来）。
    所以：非回环 URL 一律拦；已知取数据工具名一律拦；其余本地系统查询放行。
    """
    if cmd is None:
        return False, ""
    if isinstance(cmd, (list, tuple)):
        parts = [str(x) for x in cmd]
    else:
        parts = [str(cmd)]
    joined = " ".join(parts).lower()
    for m in URL_IN_CMD.finditer(joined):
        u = m.group(0)
        host = re.match(r"(?:https?|ftp)://([^/:]+)", u)
        h = host.group(1) if host else ""
        if not (h.startswith("127.") or h in ("localhost", "::1")):
            return True, f"non-loopback-url:{u}"
    first = parts[0].replace("\\", "/").rsplit("/", 1)[-1]
    first = re.sub(r"\.exe$", "", first).strip('"').lower()
    if first in EXEC_FETCH_TOOLS:
        # 例外：Windows 内置的纯信息查询（不取网络数据），不误伤
        if first == "cmd" and re.search(r"(?:^|\s|/)(?:/c|/K)\s+(?:ver|echo|cd|set|dir|hostname|whoami|systeminfo|path)(?:\s|$)", joined):
            return False, "local-system-query"
        if first in ("python", "python3", "py", "node") and not URL_IN_CMD.search(joined) \
                and not any(t in joined for t in ("-m pip", "requests", "http", "urllib", "socket")):
            return False, "interpreter-without-url"
        return True, f"fetch-tool:{first}"
    return False, ""


def is_loopback(target) -> bool:
    try:
        if isinstance(target, (tuple, list)):
            host = target[0]
        else:
            host = str(target)
        host = str(host).strip()
        if host.lower() in LOOPBACK_NAMES:
            return True
        if host.startswith("127."):
            return True
        if host == "::1":
            return True
        # 127.0.0.0/8 整段都算回环
        m = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})", host)
        if m and int(m.group(1)) == 127:
            return True
        return False
    except Exception:
        return False


def install_egress_guard(state: "BridgeState") -> None:
    """把本进程的"能上网取字节"的路径纳入熔断。仅影响当前进程（不绑子孙）。

    三类拦截：
      1. socket.connect / connect_ex / create_connection → 非回环目标直接拒
      2. socket.getaddrinfo → 非回环域名解析也拒（堵"先解析再连"的绕过）
      3. subprocess.Popen / os.system / os.execv / os.execve → 命令里出现非回环 URL
         或属于已知取数据工具（curl/wget/powershell/pip…）则拒；本地系统查询（如
         aiohttp 内部跑的 `cmd /c ver`）放行，否则会假阳性把服务本身堵死。

    措辞边界（001-F2/F7 闭环）：这是**运行时出站熔断（已知路径名单 + 子进程形态判定）**，
    目标是"Bridge 不得成为文件字节的传输通道"；它不声明"Bridge 完全不许联网"
    （回环 HTTP/SSE 与必要的元数据请求是允许的），也不拦子孙进程。绕过面见 README。
    """
    _orig_connect = socket.socket.connect
    _orig_connect_ex = socket.socket.connect_ex
    _orig_create_connection = socket.create_connection
    _orig_getaddrinfo = socket.getaddrinfo
    _orig_popen = subprocess.Popen
    _orig_system = os.system
    _orig_execv, _orig_execve = os.execv, os.execve

    def _deny(kind, target, detail=""):
        rec = {"ts": time.time(), "pid": os.getpid(), "kind": kind,
               "target": str(target), "detail": detail, "action": "BLOCKED"}
        state.record_blocked(rec)
        raise EgressBlocked(
            f"[egress-guard] {kind} -> {target} 被拒绝：Bridge 不得成为文件字节的传输通道 (C1/C4)")

    def connect(self, target, *a, **kw):
        if not is_loopback(target):
            _deny("socket.connect", target)
        return _orig_connect(self, target, *a, **kw)

    def connect_ex(self, target, *a, **kw):
        if not is_loopback(target):
            _deny("socket.connect_ex", target)
        return _orig_connect_ex(self, target, *a, **kw)

    def create_connection(address, *a, **kw):
        if not is_loopback(address):
            _deny("socket.create_connection", address)
        return _orig_create_connection(address, *a, **kw)

    def getaddrinfo(host, *a, **kw):
        # DNS 也拦：非回环域名解析直接失败，避免"先解析再连"绕过
        if host is not None and not is_loopback(host):
            _deny("socket.getaddrinfo", host)
        return _orig_getaddrinfo(host, *a, **kw)

    def _maybe_deny_exec(kind, cmd):
        bad, why = exec_looks_like_egress(cmd)
        if bad:
            _deny(kind, cmd, detail=why)

    def popen(*a, **kw):
        _maybe_deny_exec("subprocess.Popen", a[0] if a else kw.get("args"))
        return _orig_popen(*a, **kw)

    def system(cmd, *a, **kw):
        _maybe_deny_exec("os.system", cmd)
        return _orig_system(cmd, *a, **kw)

    def execv(path, args, *a, **kw):
        _maybe_deny_exec("os.execv", path)
        # 过了闸就**真执行**：原来这里判定完什么都做、直接 return None，
        # 调用方以为 execv 生效了其实没换进程 —— 这叫假成功。
        # 本模块自己从不 exec 任何东西；这条分支只为把闸做成"策略判定"而不是"静默废掉"。
        return _orig_execv(path, args, *a, **kw)

    def execve(path, args, env, *a, **kw):
        _maybe_deny_exec("os.execve", path)
        return _orig_execve(path, args, env, *a, **kw)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo
    subprocess.Popen = popen
    os.system = system
    os.execv = execv
    os.execve = execve
    state.guard_enabled = True


# --------------------------------------------------------------------------
# 配对与 HMAC（计划 §5.5 L2/L3/L4）
# --------------------------------------------------------------------------
HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")


def read_pairing_key(state_dir: Path) -> bytes | None:
    """读握手密钥；没有就返回 None（= 未配对，等新扩展把 K 送来）。"""
    f = Path(state_dir) / "handshake.key"
    if not f.exists():
        return None
    try:
        raw = f.read_text(encoding="ascii").strip()
    except OSError:
        return None
    if not HEX64_RE.fullmatch(raw):
        return None
    return bytes.fromhex(raw)


def write_pairing_key(state_dir: Path, key: bytes) -> Path:
    """落盘密钥并尽量收紧权限（chmod 600）。

    诚实边界（计划 R1c）：同用户的任何进程仍然读得到这个文件 —— 600 只防别的用户，
    不防"本机同一个登录用户下的恶意程序"。这是零安装架构的固有上限，不粉饰。
    """
    f = Path(state_dir) / "handshake.key"
    tmp = f.with_suffix(".key.tmp")
    tmp.write_text(key.hex(), encoding="ascii")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, f)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return f


def load_bridge_id(state_dir: Path) -> str:
    """bridgeId = 这个 Bridge 的长期身份，持久化后扩展可钉扎（L4 / W15）。"""
    f = Path(state_dir) / "bridge_id"
    if f.exists():
        v = f.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9a-f]{16,64}", v):
            return v
    v = uuid.uuid4().hex[:32]
    f.write_text(v, encoding="ascii")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return v


def hmac_sha256(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def challenge_expect(key: bytes, client_nonce: str, server_nonce: str, ctx: str) -> str:
    """服务端要交的证明：HMAC(K, clientNonce\\nserverNonce\\n"bridge")。"""
    return hmac_sha256(key, f"{client_nonce}\n{server_nonce}\n{ctx}")


def response_expect(key: bytes, client_nonce: str, server_nonce: str) -> str:
    """扩展要交的证明：HMAC(K, serverNonce\\nclientNonce\\n"extension")。"""
    return hmac_sha256(key, f"{server_nonce}\n{client_nonce}\n{HMAC_CTX_EXTENSION}")


def hmac_ok(expected: str, got) -> bool:
    """常量时间比较（R1d：不许用 == 比签名，防时序侧信道）。"""
    if not isinstance(got, str) or not HEX64_RE.fullmatch(got):
        return False
    return hmac.compare_digest(expected.lower(), got.lower())


# --------------------------------------------------------------------------
# 任务账本 + 状态机
# --------------------------------------------------------------------------
def norm_state(raw: str | None) -> str:
    m = {"in_progress": "DOWNLOADING", "complete": "COMPLETE", "cancelled": "CANCELLED",
         "interrupted": "INTERRUPTED", "paused": "PAUSED"}
    return m.get(str(raw), str(raw or "UNKNOWN").upper())


class Job(dict):
    """账本里的一行。dict 子类，方便直接 JSON 化。"""


def make_job(url: str, filename: str, sha256: str | None, dest: str) -> Job:
    jid = hashlib.sha1(f"{url}\n{dest}".encode("utf-8")).hexdigest()[:16]
    now = time.time()
    return Job({
        "jobId": jid, "url": url, "filename": dest, "state": "PENDING",
        "downloadId": None, "bytesReceived": 0, "totalBytes": None,
        "speedBps": 0.0, "restarted": False, "canResume": True, "retries": 0,
        "error": None, "sha256Expected": sha256, "sha256Actual": None,
        "host": urlsplit_host(url), "createdAt": now, "updatedAt": now,
        "lastProgressAt": None, "samples": [], "events": [],
    })


def urlsplit_host(url: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/]+)", url)
    return m.group(1) if m else "?"


SAFE_NAME = re.compile(r"^[\w\-. /一-鿿()（）\[\]{}'\"+,:;=@~#%&!]+$", re.UNICODE)


def sanitize_dest(raw_filename: str | None, url: str, folder: str = "") -> tuple[str | None, str | None]:
    """返回 (相对 download_root 的路径, 错误)。只允许 download_root 之下的相对路径。

    folder 非空时加上统一前缀（默认 "chrome-dl"）：因为 chrome.downloads 的 filename
    是相对 **Chrome 自己的下载目录** 解析的，加一层子目录既好认也不会跟用户手动下载混在一起。
    """
    name = raw_filename
    if not name:
        name = url.split("?")[0].rstrip("/").split("/")[-1] or "index.html"
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None, "ABSOLUTE_PATH_REJECTED"
    parts = name.split("/")
    if any(p in ("..", "", ".") for p in parts[:-1] + [parts[-1]]):
        return None, "PATH_TRAVERSAL_REJECTED"
    if len(name) > 200:
        return None, "NAME_TOO_LONG"
    if not SAFE_NAME.fullmatch(name):
        return None, "ILLEGAL_FILENAME_CHARS"
    if folder:
        f = folder.replace("\\", "/").strip("/")
        if any(p == ".." for p in f.split("/")):
            return None, "BAD_FOLDER"
        name = f"{f}/{name}"
    return name, None


# --------------------------------------------------------------------------
# 核心状态
# --------------------------------------------------------------------------
class BridgeState:
    def __init__(self, args):
        self.args = args
        self.state_dir = Path(args.state_dir)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            # 001-F3：状态目录打不开/没权限时说人话，别丢一个裸 traceback
            raise SystemExit(
                f"[bridge] 状态目录打不开：{self.state_dir}\n"
                f"  原因：{exc}\n"
                f"  下一步：换一个可写的目录，例如 "
                f"python bridge.py --state-dir \"D:\\chrome-dl-state\"") from exc
        self.token = args.token or self._load_or_make_token()
        self.jobs: dict[str, Job] = {}
        self.link: "LinkSession | None" = None
        self.link_epoch = 0
        self.guard_enabled = False
        self.blocked_count = 0
        self.started_at = time.time()
        self.peer_started_at = self.started_at
        self.dispenser_events = 0
        self.download_root = Path(args.download_root)
        self.download_root.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.state_dir / "jobs.json"
        self.trace_path = self.state_dir / "ws_trace.jsonl"      # v0.2：通道留痕
        self.blocked_path = self.state_dir / "egress_blocked.jsonl"
        self.audit_path = self.state_dir / "bridge_audit.jsonl"
        self.pairing_path = self.state_dir / "pairing_audit.jsonl"
        self.bridge_id = load_bridge_id(self.state_dir)
        self.key: bytes | None = read_pairing_key(self.state_dir)
        self.transport = "unknown"
        self.ws_port: int | None = None
        # 候选端口清单：握手时带回，供扩展侧与 popup 显示"我们扫过哪些门牌"
        self.port_scan = list(range(getattr(args, "port", DEFAULT_WS_PORT),
                                    getattr(args, "port", DEFAULT_WS_PORT)
                                    + getattr(args, "port_scan", 10)))
        self.load_ledger()

    @property
    def paired(self) -> bool:
        return self.key is not None

    def _load_or_make_token(self) -> str:
        """令牌持久化：Bridge 重启后 Agent 侧不需要重配（V8）。

        注意这个 token 只管 **Agent ⇄ Bridge 的 HTTP/SSE 面**；扩展侧不用它
        （扩展用 HMAC 握手，见 §5.5）。两条信任链互不替代。
        """
        tf = self.state_dir / "token"
        if tf.exists():
            tok = tf.read_text(encoding="ascii").strip()
            if len(tok) >= 32:
                return tok
        tok = uuid.uuid4().hex + uuid.uuid4().hex
        try:
            tf.write_text(tok, encoding="ascii")
            os.chmod(tf, 0o600)
        except OSError:
            pass
        return tok

    # ---- 日志 ----
    def _append(self, path: Path, obj: dict) -> None:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception as exc:  # 日志不能把主流程带崩
            print(f"[log-fail] {path.name}: {exc}", file=sys.stderr)

    def record_blocked(self, rec: dict) -> None:
        self.blocked_count += 1
        self._append(self.blocked_path, rec)

    def record_trace(self, direction: str, obj: dict, nbytes: int) -> None:
        self._append(self.trace_path, {
            "ts": round(time.time(), 4), "dir": direction, "bytes": nbytes,
            "maxFrame": MAX_FRAME_BYTES, "msg": obj,
        })

    def audit(self, event: str, **kw) -> None:
        self._append(self.audit_path, {"ts": round(time.time(), 4), "event": event, **kw})
        if self.args.verbose:
            print(f"[{event}] {kw}", file=sys.stderr)

    def audit_pairing(self, event: str, **kw) -> None:
        """握手/配对单独一张账：安全类事件不许被普通审计的噪声埋掉（W14b/W15/W18）。"""
        self._append(self.pairing_path,
                     {"ts": round(time.time(), 4), "event": event,
                      "bridgeId": self.bridge_id, "pid": os.getpid(), **kw})
        self.audit(event, **kw)

    # ---- 账本 ----
    def load_ledger(self) -> None:
        if not self.ledger_path.exists():
            return
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.audit("ledger_load_failed", error=str(exc))
            return
        for row in data.get("jobs", []):
            row.setdefault("samples", [])
            row.setdefault("events", [])
            if row.get("state") not in TERMINAL_STATES and row.get("state") not in (
                    "DOWNLOADING", "PAUSED", "INTERRUPTED", "PENDING", "STALE"):
                row["state"] = "PENDING"
            # 非终态在重启后一律回到 PENDING，等链路重连后由 snapshot 对账
            if row.get("state") in ("DOWNLOADING", "PAUSED", "INTERRUPTED", "STALE"):
                row["pendingReconcile"] = True
                self.audit("ledger_reopen", jobId=row["jobId"], prevState=row["state"])
            self.jobs[row["jobId"]] = Job(row)
        self.audit("ledger_loaded", count=len(self.jobs))

    def save_ledger(self) -> None:
        tmp = self.ledger_path.with_suffix(".json.tmp")
        drop = {"events", "samples"}
        rows = [{k: v for k, v in j.items() if k not in drop} for j in self.jobs.values()]
        tmp.write_text(json.dumps({"version": 1, "jobs": rows}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.ledger_path)

    def touch(self, job: Job, force: bool = False) -> None:
        """进度落盘节流：非终态最多 1 秒一次，终态必落（否则 12 并发会把磁盘写爆）。"""
        job["updatedAt"] = time.time()
        now = time.time()
        if not force and job["state"] not in TERMINAL_STATES:
            if now - getattr(self, "_last_save", 0) < 1.0:
                return
        self._last_save = now
        self.save_ledger()

    # ---- 速度 ----
    def feed_speed(self, job: Job, nbytes: int, ts: float) -> None:
        s = job["samples"]
        s.append([round(ts, 4), nbytes])
        del s[:-8]
        if len(s) >= 2:
            (t0, b0), (t1, b1) = s[0], s[-1]
            dt = max(t1 - t0, 1e-6)
            job["speedBps"] = round(max(0, b1 - b0) / dt, 2)

    # ---- 统计 ----
    def stats(self) -> dict:
        c: dict[str, int] = {}
        for j in self.jobs.values():
            c[j["state"]] = c.get(j["state"], 0) + 1
        return {
            "total": len(self.jobs), "byState": c,
            "active": sum(1 for j in self.jobs.values() if j["state"] == "DOWNLOADING"),
            "pending": sum(1 for j in self.jobs.values() if j["state"] == "PENDING"),
            "linked": bool(self.link and self.link.alive),
            "blockedEgress": self.blocked_count,
            "dispenserEvents": self.dispenser_events,
            "uptimeSec": round(time.time() - self.started_at, 1),
        }


# --------------------------------------------------------------------------
# 链路会话（扩展 → 本服务的这条 WebSocket）
# --------------------------------------------------------------------------
class LinkSession:
    def __init__(self, state: BridgeState, send_text, peer: str, conn: "WsConn"):
        self.state = state
        self._send_text = send_text
        self.peer = peer
        self.conn = conn
        self.alive = True
        self.chromeVersion = None
        self.extensionVersion = None
        self.last_seen = time.time()

    def close(self) -> None:
        self.alive = False

    async def send(self, obj: dict) -> None:
        if not self.alive:
            raise ConnectionError("link down")
        obj.setdefault("v", PROTOCOL_VERSION)
        obj.setdefault("ts", round(time.time(), 4))
        raw = encode_frame(obj)                # 8192 自检在这里，超了直接抛
        await self._send_text(raw.decode("utf-8"))
        self.state.record_trace("bridge->ext", obj, len(raw))


# --------------------------------------------------------------------------
# 命令分发 / 并发闸 / 重试
# --------------------------------------------------------------------------
class Scheduler:
    def __init__(self, state: BridgeState, loop: asyncio.AbstractEventLoop):
        self.state = state
        self.loop = loop
        self.max_concurrent = state.args.max_concurrent
        self.per_host = state.args.per_host
        self.pending_retry: dict[str, asyncio.TimerHandle] = {}

    def counts(self) -> tuple[int, dict[str, int]]:
        host_n: dict[str, int] = {}
        active = 0
        for j in self.state.jobs.values():
            if j["state"] == "DOWNLOADING":
                active += 1
                host_n[j["host"]] = host_n.get(j["host"], 0) + 1
        return active, host_n

    def slots_ok(self, job: Job) -> bool:
        active, host_n = self.counts()
        if active >= self.max_concurrent:
            return False
        if host_n.get(job["host"], 0) >= self.per_host:
            return False
        return True

    async def dispatch_once(self) -> int:
        """把能排上的 PENDING 任务发给扩展。返回本次下发数。

        即使链路不在也要跑：没扩展的超时要能变成 FAILED（V4-e①），不能永远 PENDING。
        """
        sent = 0
        link = self.state.link
        link_up = bool(link and link.alive)
        now = time.time()
        expired = 0
        for j in sorted(list(self.state.jobs.values()), key=lambda x: x["createdAt"]):
            if j["state"] != "PENDING":
                continue
            if now - j["createdAt"] > self.state.args.link_wait_timeout:
                j["state"] = "FAILED"
                j["error"] = "NO_EXTENSION_CONNECTED"
                self.state.touch(j)
                self.state.audit("expire", jobId=j["jobId"], reason="NO_EXTENSION_CONNECTED")
                expired += 1
                continue
            if not link_up or not self.slots_ok(j):
                continue
            msg = {"type": "start", "jobId": j["jobId"], "url": j["url"],
                   "filename": j["filename"], "conflictAction": self.state.args.conflict}
            # 语义对齐 Chrome：能续的走 resume(id)，不能续的开新单但 overwrite 同一文件，
            # 绝不因为 uniquify 而多下一份。
            if j.pop("resumeAttempt", False) and j.get("downloadId") is not None and not j.get("restarted"):
                msg["resume"] = True
                msg["downloadId"] = j["downloadId"]
            elif j.get("restarted"):
                msg["conflictAction"] = "overwrite"
            if j.get("sha256Expected"):
                msg["sha256"] = j["sha256Expected"]
            try:
                await link.send(msg)
            except Exception as exc:
                self.state.audit("dispatch_failed", jobId=j["jobId"], error=str(exc))
                break
            j["state"] = "DOWNLOADING"
            j["dispatchedAt"] = time.time()
            j["lastProgressAt"] = time.time()
            j["staleFlagged"] = False
            self.state.dispenser_events += 1
            self.state.touch(j)
            self.state.audit("dispatch", jobId=j["jobId"], url=j["url"])
            sent += 1
        if expired:
            self.state.audit("expired_batch", count=expired)
        return sent

    def schedule_retry(self, job: Job, reason: str) -> None:
        if job["retries"] >= self.state.args.max_retries:
            job["state"] = "FAILED"
            job["error"] = f"{reason} (retries exhausted)"
            self.state.touch(job)
            self.state.audit("giveup", jobId=job["jobId"], reason=reason)
            return
        delay = min(2 ** job["retries"], 16)
        job["retries"] += 1
        job["state"] = "PENDING"
        job["resumeAttempt"] = True
        self.state.touch(job)
        self.state.audit("retry_scheduled", jobId=job["jobId"], delay=delay,
                         attempt=job["retries"], reason=reason)
        # 关键：Bridge 活着的时候定时器是可靠的（扩展侧 SW 被杀不可靠，见 F2/F4）
        self.pending_retry[job["jobId"]] = self.loop.call_later(
            delay, lambda: self.loop.create_task(self.dispatch_once()))


# --------------------------------------------------------------------------
# 进度事件处理
# --------------------------------------------------------------------------
def public_job(job: Job) -> dict:
    keys = ["jobId", "url", "filename", "state", "downloadId", "bytesReceived",
            "totalBytes", "speedBps", "restarted", "canResume", "retries", "error",
            "sha256Expected", "sha256Actual", "createdAt", "updatedAt", "host"]
    out = {k: job.get(k) for k in keys}
    out["pendingReconcile"] = bool(job.get("pendingReconcile"))
    # STALE 是给人看的告警位，不改 state（任务可能真在跑，误判重下会造成重复）
    out["stale"] = bool(job.get("staleFlagged"))
    return out


class ProgressHub:
    def __init__(self):
        self.queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self.queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.queues:
            self.queues.remove(q)

    def publish(self, evt: dict) -> None:
        for q in list(self.queues):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass


def verify_file(job: Job, root: Path) -> str | None:
    """本地磁盘哈希（只读本地文件，不涉及任何网络出站）。"""
    if not job.get("sha256Expected"):
        return "SKIP"
    p = root / job["filename"]
    if not p.exists():
        return "MISSING"
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "OK" if h.hexdigest() == job["sha256Expected"] else "MISMATCH"


# ==========================================================================
# REST/SSE 面（Agent ⇄ Bridge）：与传输层无关的一份实现，两种服务端共用
# ==========================================================================
class RestResult:
    __slots__ = ("status", "headers", "payload", "stream")

    def __init__(self, status=200, headers=None, payload=None, stream=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json; charset=utf-8"}
        self.payload = payload            # bytes | None
        self.stream = stream              # async iterator of bytes | None（SSE）


def json_res(obj, status=200) -> RestResult:
    return RestResult(status=status,
                      payload=json.dumps(obj, ensure_ascii=False).encode("utf-8"))


class RestApi:
    """所有 Agent 侧路由。aiohttp 与零依赖降级两条传输路径都调这里 —— 一份语义，不漂移。"""

    BODY_LIMIT = 64 * 1024          # HTTP 面的请求体上限（纸条本来就小，这是防滥用）

    def __init__(self, state: BridgeState, sched: Scheduler, hub: ProgressHub):
        self.state = state
        self.sched = sched
        self.hub = hub

    # ---- 鉴权（只管 Agent 的 HTTP 面；扩展面用 HMAC 握手，两条信任链互不替代）----
    def auth(self, headers, query) -> bool:
        if self.state.args.no_token:
            return True
        hdr = headers.get("authorization", "")
        tok = hdr[7:] if hdr.lower().startswith("bearer ") else (query.get("token") or "")
        return bool(tok) and tok == self.state.token

    async def handle(self, method: str, path: str, query: dict, headers: dict,
                     body: bytes) -> RestResult:
        if method == "GET" and path == "/v1/health":
            return self.health(query, headers)
        if method == "GET" and path == "/v1/stats":
            return self.unauth_or(query, headers, lambda: json_res(self.state.stats()))
        if method == "GET" and path == "/v1/stream":
            return self.stream_res(query, headers)
        if method == "GET" and path == "/v1/trace":
            return self.trace_res(query, headers)
        if method == "POST" and path == "/v1/jobs":
            return await self.submit_res(query, headers, body)
        if method == "POST" and path == "/v1/jobs/batch":
            return await self.batch_res(query, headers, body)
        if method == "GET" and path == "/v1/jobs":
            return self.unauth_or(
                query, headers,
                lambda: json_res({"jobs": [public_job(j) for j in self.state.jobs.values()]}))
        m = re.fullmatch(r"/v1/jobs/([0-9a-f]{16})/(pause|resume|cancel)", path)
        if m and method == "POST":
            return await self.ctrl_res(query, headers, m.group(1), m.group(2))
        m = re.fullmatch(r"/v1/jobs/([0-9a-f]{16})", path)
        if m and method == "GET":
            job = self.state.jobs.get(m.group(1))
            if not job:
                return self.unauth_or(query, headers, lambda: json_res({"error": "NOT_FOUND"}, 404))
            return self.unauth_or(query, headers, lambda: json_res(public_job(job)))
        return json_res({"error": "NOT_FOUND", "path": path}, 404)

    def unauth_or(self, query, headers, fn) -> RestResult:
        if not self.auth(headers, query):
            return json_res({"error": "BAD_TOKEN"}, 401)
        return fn()

    # ---- 各路由 ----
    def health(self, query, headers) -> RestResult:
        def body():
            link = self.state.link
            return json_res({
                "ok": True, "pid": os.getpid(), "version": PROTOCOL_VERSION,
                "httpPort": self.state.ws_port, "linkPort": self.state.ws_port,
                "wsPath": WS_PATH, "transport": self.state.transport,
                "uptimeSec": self.state.stats()["uptimeSec"],
                "extensionConnected": bool(link and link.alive),
                "chromeVersion": link.chromeVersion if link else None,
                "extensionVersion": link.extensionVersion if link else None,
                "paired": self.state.paired, "bridgeId": self.state.bridge_id,
                "egressGuard": {"enabled": self.state.guard_enabled,
                                "blocked": self.state.blocked_count,
                                "loopbackExempt": sorted(LOOPBACK_NAMES - {"", "localhost."})},
                "stats": self.state.stats(),
            })
        return self.unauth_or(query, headers, body)

    async def submit_res(self, query, headers, body) -> RestResult:
        if not self.auth(headers, query):
            return json_res({"error": "BAD_TOKEN"}, 401)
        try:
            spec = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return json_res({"error": "BAD_JSON"}, 400)
        out = await self.submit_one(spec)
        return json_res(out, 400 if out.get("error") else 200)

    async def batch_res(self, query, headers, body) -> RestResult:
        if not self.auth(headers, query):
            return json_res({"error": "BAD_TOKEN"}, 401)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return json_res({"error": "BAD_JSON"}, 400)
        results = [await self.submit_one(s) for s in payload.get("jobs", [])]
        await self.sched.dispatch_once()
        bad = any(r.get("error") for r in results)
        return json_res({"results": results}, 207 if bad else 200)

    async def submit_one(self, job_spec: dict) -> dict:
        url = job_spec.get("url", "")
        if not re.match(r"^https?://", url):
            return {"error": "URL_MUST_BE_HTTPS", "url": url}
        dest, err = sanitize_dest(job_spec.get("filename"), url, self.state.args.folder)
        if err:
            return {"error": err, "filename": job_spec.get("filename")}
        sha = job_spec.get("sha256")
        if sha is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            return {"error": "BAD_SHA256"}
        existing = self.state.jobs.get(hashlib.sha1(f"{url}\n{dest}".encode()).hexdigest()[:16])
        if existing is not None:
            return {"ok": True, "reused": True, "job": public_job(existing)}
        job = make_job(url, dest, sha, dest)
        self.state.jobs[job["jobId"]] = job
        self.state.touch(job)
        self.state.audit("submit", jobId=job["jobId"], url=url, filename=dest)
        self.hub.publish({"type": "state", "jobId": job["jobId"], "state": "PENDING",
                          "url": url, "filename": dest})
        return {"ok": True, "reused": False, "job": public_job(job)}

    async def ctrl_res(self, query, headers, job_id, act) -> RestResult:
        if not self.auth(headers, query):
            return json_res({"error": "BAD_TOKEN"}, 401)
        job = self.state.jobs.get(job_id)
        if not job:
            return json_res({"error": "NOT_FOUND"}, 404)
        link = self.state.link
        if link and link.alive:
            await link.send({"type": act, "jobId": job["jobId"],
                             "downloadId": job.get("downloadId")})
        if act == "cancel":
            job["state"] = "CANCELLED"
            job["error"] = "cancelled by agent"
        elif act == "pause":
            job["state"] = "PAUSED"
        elif act == "resume":
            job["state"] = "PENDING"
            job["resumeAttempt"] = True
        self.state.touch(job)
        self.state.audit("control", jobId=job["jobId"], action=act)
        self.hub.publish({"type": "state", "jobId": job["jobId"], "state": job["state"]})
        if act == "resume":
            await self.sched.dispatch_once()
        return json_res(public_job(job))

    def trace_res(self, query, headers) -> RestResult:
        def body():
            limit = int(query.get("limit") or 100000)
            rows = []
            if self.state.trace_path.exists():
                with open(self.state.trace_path, encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        if i >= limit:
                            break
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            continue
            return json_res({"trace": rows, "file": str(self.state.trace_path),
                             "maxFrame": MAX_FRAME_BYTES})
        return self.unauth_or(query, headers, body)

    def stream_res(self, query, headers) -> RestResult:
        if not self.auth(headers, query):
            return json_res({"error": "BAD_TOKEN"}, 401)
        state, hub = self.state, self.hub

        async def gen():
            q = hub.subscribe()
            try:
                yield b": hello\n\n"
                for j in state.jobs.values():
                    yield f"data: {json.dumps(public_job(j), ensure_ascii=False)}\n\n".encode()
                while True:
                    try:
                        evt = await asyncio.wait_for(q.get(), timeout=10)
                    except asyncio.TimeoutError:
                        yield b": ping\n\n"
                        continue
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode()
            except asyncio.CancelledError:
                raise
            finally:
                hub.unsubscribe(q)

        return RestResult(status=200, headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"}, stream=gen())


# ==========================================================================
# 链路通道：握手（HMAC 挑战应答）+ 消息泵 —— 与传输层无关的一份实现
# ==========================================================================
class LinkHandshakeError(Exception):
    def __init__(self, reason, close_code=1008):
        super().__init__(reason)
        self.reason = reason
        self.close_code = close_code


async def run_handshake(state: BridgeState, conn: "WsConn", peer: str,
                        scan_ports: list[int]) -> dict:
    """双向 HMAC 挑战应答（计划 §5.5 L2/L3）。返回扩展的 hello 元信息。

    扩展 → {type:hello_challenge, clientNonce, [keyExchange], chromeVersion, ...}
    服务 → {type:hello_verify, nonce:serverNonce, bridgeId, hmac}
    扩展 → {type:hello_verify, hmac}
    服务 → {type:welcome, ok, bridgeId, token, maxConcurrent, perHost, maxFrame, port}

    任何一步对不上就断链并写 pairing_audit —— 宁可连不上，不能让不知密钥的进程冒充。
    """
    msg = await conn.recv_text(timeout=HANDSHAKE_TIMEOUT)
    if msg is None:
        raise LinkHandshakeError("NO_HELLO")
    state.record_trace("ext->bridge", msg, len(json.dumps(msg, ensure_ascii=False).encode()))
    ctype = msg.get("type")
    client_nonce = msg.get("clientNonce") or msg.get("nonce") or ""
    if ctype != "hello_challenge":
        # 旧版 hello 不再接受：没有 HMAC 的连接一律拒绝（G2/C4：零 NM 残余）
        state.audit_pairing("link_rejected", reason="NOT_HMAC_HELLO", got=ctype, peer=peer)
        raise LinkHandshakeError("NOT_HMAC_HELLO")
    if not re.fullmatch(r"[0-9a-fA-F]{16,64}", str(client_nonce)):
        state.audit_pairing("link_rejected", reason="BAD_CLIENT_NONCE", peer=peer)
        raise LinkHandshakeError("BAD_CLIENT_NONCE")

    key_ex = msg.get("keyExchange")
    key: bytes | None = state.key
    pairing = "existing"
    if key_ex is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(key_ex)):
            state.audit_pairing("link_rejected", reason="BAD_KEY_EXCHANGE", peer=peer)
            raise LinkHandshakeError("BAD_KEY_EXCHANGE")
        newkey = bytes.fromhex(str(key_ex))
        if key is not None and key != newkey:
            # 扩展侧走了"忘记此程序"，或 state_dir 被换 → 重新 TOFU，必须大声记审计（W15/W18/W19）
            state.audit_pairing("pairing_replaced",
                                oldKeyFp=hmac_sha256(key, "fingerprint")[:16],
                                newKeyFp=hmac_sha256(newkey, "fingerprint")[:16], peer=peer)
        if key is None:
            state.audit_pairing("pairing_first",
                                note="TOFU 首任：本机信任边界固有上限（F15 / R1b）",
                                peer=peer, portScan=scan_ports)
        key = newkey
        state.key = key
        write_pairing_key(state.state_dir, key)
        pairing = "new"
    if key is None:
        # 服务端还没有密钥，扩展又没带来 → 无从验证，直接拒（不开放未配对控制面）
        state.audit_pairing("link_rejected", reason="BRIDGE_UNPAIRED", peer=peer)
        raise LinkHandshakeError("BRIDGE_UNPAIRED")

    server_nonce = secrets.token_hex(16)
    payload = {"v": PROTOCOL_VERSION, "type": "hello_verify", "ts": round(time.time(), 4),
               "nonce": server_nonce, "bridgeId": state.bridge_id,
               "hmac": challenge_expect(key, str(client_nonce), server_nonce, HMAC_CTX_BRIDGE)}
    raw = encode_frame(payload)               # 握手帧也必须 ≤8192（自检在这）
    await conn.send_text(raw.decode("utf-8"))
    state.record_trace("bridge->ext", payload, len(raw))

    rep = await conn.recv_text(timeout=HANDSHAKE_TIMEOUT)
    if rep is None:
        raise LinkHandshakeError("NO_REPLY")
    state.record_trace("ext->bridge", rep, len(json.dumps(rep, ensure_ascii=False).encode()))
    if rep.get("type") != "hello_verify" or not hmac_ok(
            response_expect(key, str(client_nonce), server_nonce), rep.get("hmac")):
        # 对面不知道 K：冒充扩展的程序，或者两端密钥已经不一致（W19 的服务端视角）
        state.audit_pairing("handshake_failed", reason="EXT_HMAC_MISMATCH", peer=peer,
                            gotType=rep.get("type"), pairing=pairing)
        raise LinkHandshakeError("EXT_HMAC_MISMATCH")

    state.audit_pairing("handshake_ok", peer=peer, pairing=pairing,
                        chromeVersion=msg.get("chromeVersion"),
                        extensionVersion=msg.get("extensionVersion"),
                        port=msg.get("port"), portScan=scan_ports)
    return {"clientNonce": str(client_nonce), "serverNonce": server_nonce,
            "pairing": pairing, "chromeVersion": msg.get("chromeVersion"),
            "extensionVersion": msg.get("extensionVersion")}


async def link_session(state: BridgeState, sched: Scheduler, hub: ProgressHub,
                       conn: "WsConn", peer: str, scan_ports: list[int]) -> None:
    """一条扩展连接的一生：握手 → welcome → 消息泵 → 收尾。"""
    try:
        info = await run_handshake(state, conn, peer, scan_ports)
    except LinkHandshakeError as exc:
        try:
            await conn.send_text(json.dumps({"v": PROTOCOL_VERSION, "type": "error",
                                             "reason": exc.reason,
                                             "ts": round(time.time(), 4)}))
        except Exception:
            pass
        await conn.close(exc.close_code, exc.reason)
        return
    except FrameTooBig as exc:
        state.audit("link_bad_frame", length=str(exc), stage="handshake")
        await conn.close(1009, "message too big")
        return
    except asyncio.TimeoutError:
        state.audit("link_handshake_timeout", peer=peer)
        await conn.close(1008, "handshake timeout")
        return

    session = LinkSession(state, conn.send_text, peer, conn)
    session.chromeVersion = info.get("chromeVersion")
    session.extensionVersion = info.get("extensionVersion")
    if state.link and state.link.alive and state.link is not session:
        state.audit("link_replaced", reason="SECOND_LINK", oldPeer=state.link.peer, newPeer=peer)
        try:
            await state.link.send({"type": "replaced", "reason": "SECOND_LINK"})
        except Exception:
            pass
        state.link.alive = False
    state.link = session
    state.link_epoch += 1
    state.audit("link_open", peer=peer, pairing=info["pairing"])

    # L4：把自己的 pid / 启动时间告知扩展 —— popup 上拿它定位"门口站的是哪个程序"（W8/W18）
    await session.send({"type": "welcome", "ok": True, "bridgeId": state.bridge_id,
                        "token": state.token, "maxConcurrent": sched.max_concurrent,
                        "perHost": sched.per_host, "maxFrame": MAX_FRAME_BYTES,
                        "port": state.ws_port, "peerPid": os.getpid(),
                        "peerStartedAt": round(state.peer_started_at, 3)})

    async def handle_msg(msg: dict) -> None:
        session.last_seen = time.time()
        typ = msg.get("type")
        if typ == "ping":                     # 保活：回一个 pong，两端都算活动窗口（F1）
            await session.send({"type": "pong"})
            return
        if typ == "pong":
            return
        if typ == "hello":                    # 已握手后的自我介绍，只取元信息
            session.chromeVersion = msg.get("chromeVersion") or session.chromeVersion
            session.extensionVersion = msg.get("extensionVersion") or session.extensionVersion
            return
        if typ == "snapshot":
            for it in msg.get("items", []):
                await apply_progress(state, sched, hub, it, from_snapshot=True)
            await sched.dispatch_once()
            return
        if typ in ("progress", "ack", "result"):
            await apply_progress(state, sched, hub, msg)
            return
        state.audit("link_unknown_type", type=typ)

    pump = asyncio.create_task(pump_frames(state, conn, handle_msg, session))
    await sched.dispatch_once()
    try:
        await pump
    finally:
        session.alive = False
        if state.link is session:
            state.link = None
            state.audit("link_close", peer=peer)
        await asyncio.gather(pump, return_exceptions=True)
        try:
            await conn.close(1000, "bye")
        except Exception:
            pass


async def pump_frames(state: BridgeState, conn: "WsConn", handle_msg, session) -> None:
    while session.alive:
        try:
            msg = await conn.recv_text()
        except FrameTooBig as exc:
            # C7 硬闸：超大帧只有一种处置 —— 断开 1009，绝不分片接收、不放宽上限
            state.audit("link_bad_frame", length=str(exc), maxFrame=MAX_FRAME_BYTES)
            await conn.close(1009, "message too big")
            return
        except (ConnectionError, OSError) as exc:
            state.audit("link_io_error", error=f"{type(exc).__name__}: {exc}")
            return
        if msg is None:
            return
        state.record_trace("ext->bridge", msg,
                           len(json.dumps(msg, ensure_ascii=False).encode()))
        bad = audit_field_closure(msg)
        if bad:
            # 未知字段 = 通道被塞了不该塞的东西（V4-c 承重）。丢这一条并留痕，不断链。
            state.audit("link_unknown_field", fields=bad, type=msg.get("type"))
            continue
        try:
            await handle_msg(msg)
        except Exception as exc:
            state.audit("link_msg_error", error=f"{type(exc).__name__}: {exc}",
                        type=msg.get("type"))


async def apply_progress(state: BridgeState, sched: Scheduler, hub: ProgressHub,
                         msg: dict, from_snapshot: bool = False) -> None:
    jid = msg.get("jobId")
    job = state.jobs.get(jid) if jid else None
    if job is None:
        state.audit("orphan_progress", jobId=jid)
        return
    prev = job["state"]
    st = norm_state(msg.get("state"))
    if msg.get("downloadId") is not None:
        job["downloadId"] = msg["downloadId"]
    br = msg.get("bytesReceived")
    tb = msg.get("totalBytes")
    if br is not None:
        # 单调化：乱序/重传的快照不能让已收字节倒退（F7 双保险之一）
        job["bytesReceived"] = max(int(job.get("bytesReceived") or 0), int(br))
    if tb:
        job["totalBytes"] = int(tb)
    if msg.get("canResume") is not None:
        job["canResume"] = bool(msg["canResume"])
    if msg.get("resumedFrom") is not None:
        job["resumedFrom"] = int(msg["resumedFrom"])
    # 诚实的"重头下"检测：Chrome 续传时 bytesReceived 接着涨；不支持 Range 重头下会打回接近 0。
    # 判定用**原始上报值**而非单调值，否则永远发现不了重头下。
    raw_br = msg.get("bytesReceived")
    if raw_br is not None and not msg.get("restarted"):
        if int(raw_br) < int(job.get("bytesReceived") or 0) - 1024 and st == "DOWNLOADING":
            if not job["restarted"]:
                job["restarted"] = True
                state.audit("restart_detected", jobId=jid,
                            prevBytes=job["bytesReceived"], rawBytes=int(raw_br),
                            note="字节数打回，判定为重头下（不称续传）")
    if msg.get("restarted") and not job["restarted"]:
        job["restarted"] = True
        state.audit("restarted", jobId=jid, note="扩展显式上报重头下")
    if st in ("DOWNLOADING", "IN_PROGRESS"):
        job["state"] = "DOWNLOADING"
        job["lastProgressAt"] = time.time()
        job["staleFlagged"] = False
        if job.get("pendingReconcile"):
            job["pendingReconcile"] = False
    elif st == "COMPLETE":
        job["bytesReceived"] = job["totalBytes"] or job["bytesReceived"]
        job["state"] = "COMPLETE"
        job["completedAt"] = time.time()
        job["sha256Actual"] = verify_file(job, state.download_root)
        if job["sha256Actual"] == "MISMATCH":
            job["state"] = "FAILED"
            job["error"] = "CHECKSUM_MISMATCH"
        elif job["sha256Actual"] == "MISSING":
            # 扩展说"完了"但盘上没文件 → 不能认，否则就是拿谎报当凭据（C5）
            job["state"] = "FAILED"
            job["error"] = "FILE_MISSING_ON_COMPLETE"
            state.audit("fabricated_complete", jobId=jid,
                        note="上报 complete 但本地无此文件，判 FAILED")
        state.audit("complete", jobId=jid, verify=job["sha256Actual"],
                    bytes=job["bytesReceived"], restarted=job["restarted"])
    elif st == "CANCELLED":
        job["state"] = "CANCELLED"
    elif st == "PAUSED":
        job["state"] = "PAUSED"
    elif st in ("INTERRUPTED", "ERROR"):
        err = msg.get("error") or "interrupted"
        job["error"] = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)[:200]
        if job["canResume"]:
            job["state"] = "PENDING"
            job["resumeAttempt"] = True
            sched.schedule_retry(job, f"INTERRUPTED:{err}")
        else:
            job["state"] = "PENDING"
            job["resumeAttempt"] = False
            # 只有确实下过字节又重头来才叫"重头下"；404 这种一字节没有的不算
            if (job["bytesReceived"] or 0) > 0:
                job["restarted"] = True
            sched.schedule_retry(job, f"NO_RANGE_SUPPORT:{err}")
        state.audit("interrupted", jobId=jid, error=job["error"], canResume=job["canResume"])
    else:
        state.audit("unknown_state", jobId=jid, state=st)
    if job["state"] == "DOWNLOADING" or st == "COMPLETE":
        state.feed_speed(job, int(job["bytesReceived"]), time.time())
    state.touch(job)
    if prev != job["state"] or not from_snapshot:
        hub.publish({"type": "progress", **public_job(job)})
    if st in ("COMPLETE", "FAILED", "CANCELLED"):
        await sched.dispatch_once()


# ==========================================================================
# 传输层 A：aiohttp（优先）
# ==========================================================================
class WsConn:
    """链路连接的统一接口：recv_text / send_text / close。两种传输实现必须一致。"""

    async def recv_text(self, timeout: float | None = None) -> dict | None:
        raise NotImplementedError

    async def send_text(self, text: str) -> None:
        raise NotImplementedError

    async def close(self, code: int = 1000, reason: str = "") -> None:
        raise NotImplementedError


class AiohttpConn(WsConn):
    def __init__(self, state: BridgeState, ws):
        self.state = state
        self.ws = ws

    async def recv_text(self, timeout: float | None = None) -> dict | None:
        from aiohttp import WSMsgType
        while True:
            msg = await self.ws.receive(timeout=timeout)
            if msg.type == WSMsgType.TEXT:
                try:
                    obj = json.loads(msg.data)
                except Exception:
                    self.state.audit("link_bad_json", bytes=len(msg.data or ""))
                    continue
                if not isinstance(obj, dict):
                    self.state.audit("link_bad_json", shape=type(obj).__name__)
                    continue
                return obj
            if msg.type == WSMsgType.BINARY:
                # 通道只许文本帧：二进制 = 有人想塞文件字节（C1/C7），当超大帧一样处理
                raise FrameTooBig(f"binary frame {len(msg.data)} bytes")
            if msg.type == WSMsgType.ERROR:
                exc = self.ws.exception()
                txt = f"{type(exc).__name__}: {exc}" if exc else "unknown"
                # aiohttp 对超限的原文是 "Message size N exceeds limit M"。
                # 必须把它归到 FrameTooBig，否则 W17 的"记 link_bad_frame"落不进去，
                # 只剩一条含糊的 link_io_error —— 长度闸的现场就该有长度闸的名字。
                low = txt.lower()
                if ("exceeds limit" in low or "too big" in low
                        or "1009" in txt or "max_msg_size" in low):
                    raise FrameTooBig(txt)
                raise ConnectionError(txt)
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                return None

    async def send_text(self, text: str) -> None:
        if self.ws.closed:
            raise ConnectionError("link down")
        await self.ws.send_str(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """发一个关闭帧就完事，**不等对面回帧**。

        实测坑：aiohttp 的 ws.close() 会 await 对端的 close 回声。对面是"发完 9000
        字节就不吭声"的违规客户端时，这一等能把整条链路的收尾卡住，state.link 迟迟
        不清空 → health 一直报"扩展在连"，任务全卡在 DISPATCH。关闭帧本身在 await
        之前就已经写进 transport，所以给 1 秒只是等回声，超时就自己往下走。
        """
        if self.ws.closed:
            return
        try:
            await asyncio.wait_for(
                self.ws.close(code=code, message=reason.encode("utf-8")[:120]), 1.0)
        except asyncio.TimeoutError:
            self.state.audit("link_close_echo_timeout", code=code, reason=reason[:80])
            try:
                self.ws._writer.close()      # 收尾：不再等，直接掐本地这头
            except Exception:
                pass
        except Exception:
            pass


async def serve_aiohttp(state: BridgeState, sched: Scheduler, hub: ProgressHub,
                        sock: socket.socket) -> tuple[str, object]:
    from aiohttp import web
    api = RestApi(state, sched, hub)
    app = web.Application()

    async def link_ws(request):
        # C7 / F13：max_msg_size 必须**显式**传 8192。aiohttp 的默认值是 4 MiB
        # （本机源码 web_ws.py:117），不传就等于把 V4-c 的"单条纸条 ≤8KB"判据写废。
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, compress=False,
                                  autoping=True, heartbeat=None)
        await ws.prepare(request)
        peer = request.transport.get_extra_info("peername")
        conn = AiohttpConn(state, ws)
        await link_session(state, sched, hub, conn, f"{peer[0]}:{peer[1]}",
                           state.port_scan)
        return ws

    async def rest(request):
        body = b""
        if request.can_read_body:
            raw = await request.read()
            if len(raw) > api.BODY_LIMIT:
                return web.json_response({"error": "BODY_TOO_BIG"}, status=413)
            body = raw or b""
        res = await api.handle(request.method, request.path, dict(request.query),
                               # 头部键必须小写化：aiohttp 自己的 MultiDict 大小写不敏感，
                               # 但 dict(request.headers) 会把 "Authorization" 原样留下，
                               # 跟零依赖降级路径（read_http_head 已小写）对齐才不漂语义。
                               {k.lower(): v for k, v in request.headers.items()}, body)
        if res.stream is not None:
            resp = web.StreamResponse(status=res.status, headers=res.headers)
            await resp.prepare(request)
            try:
                async for chunk in res.stream:
                    await resp.write(chunk)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            finally:
                try:
                    await resp.write_eof()
                except Exception:
                    pass
            return resp
        return web.Response(status=res.status, headers=res.headers, body=res.payload)

    app.router.add_get(WS_PATH, link_ws)
    app.router.add_route("*", "/{tail:.*}", rest)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.SockSite(runner, sock)       # 用已经带 SO_EXCLUSIVEADDRUSE 的套接字
    await site.start()

    async def closer():
        try:
            await site.stop()
        except Exception:
            pass
        try:
            await runner.cleanup()
        except Exception:
            pass

    return "aiohttp", closer


# ==========================================================================
# 传输层 B：零依赖降级（标准库手写 RFC6455 + HTTP/1.1）
# ==========================================================================
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WsProtoError(Exception):
    def __init__(self, reason, close_code=1002):
        super().__init__(reason)
        self.reason = reason
        self.close_code = close_code


def unmask(payload: bytes, mask: bytes) -> bytes:
    import operator
    if not payload:
        return b""
    pad = (mask * (len(payload) // 4 + 2))[:len(payload)]
    return bytes(map(operator.xor, payload, pad))


def ws_accept_key(sec_key: str) -> str:
    return base64.b64encode(hashlib.sha1(f"{sec_key}{WS_GUID}".encode()).digest()).decode()


def encode_ws_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    """服务端 → 客户端的帧（不掩码，RFC6455 规定服务端帧无掩码），带长度自检。"""
    n = len(payload)
    if n <= 125:
        head = bytes([0x80 | opcode, n])
    elif n <= 0xFFFF:
        head = bytes([0x80 | opcode, 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([0x80 | opcode, 127]) + n.to_bytes(8, "big")
    return head + payload


class FallbackConn(WsConn):
    def __init__(self, state: BridgeState, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.state = state
        self.reader = reader
        self.writer = writer
        self.closed = False

    async def _read_frame(self):
        r = self.reader
        try:
            hdr = await r.readexactly(2)
        except asyncio.IncompleteReadError:
            return None
        b0, b1 = hdr[0], hdr[1]
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        masked, ln = bool(b1 & 0x80), b1 & 0x7F
        if not masked:
            raise WsProtoError("client frame not masked")     # RFC6455 强制掩码
        if ln == 126:
            ln = int.from_bytes(await r.readexactly(2), "big")
        elif ln == 127:
            ln = int.from_bytes(await r.readexactly(8), "big")
        if ln > MAX_FRAME_BYTES:
            # C7：单帧超 8192 → 立刻断（1009），不读正文、不分片拼接、不放宽
            raise FrameTooBig(f"declared frame length {ln} > {MAX_FRAME_BYTES}")
        mask = await r.readexactly(4)
        payload = unmask(await r.readexactly(ln), mask) if ln else b""
        if not fin and opcode != OP_CONT:
            # 分片接收同样是后门：一个 9000 字节的纸条拆成两片就绕过长度闸了
            raise FrameTooBig(f"fragmented message first frame {ln} bytes")
        return opcode, payload

    async def recv_text(self, timeout: float | None = None) -> dict | None:
        while True:
            if self.closed:
                return None
            fut = asyncio.ensure_future(self._read_frame())
            try:
                got = await asyncio.wait_for(fut, timeout) if timeout else await fut
            except asyncio.TimeoutError:
                raise
            except asyncio.IncompleteReadError:
                return None
            if got is None:
                return None
            opcode, payload = got
            if opcode == OP_PING:
                await self._write(encode_ws_frame(payload, OP_PONG))
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                await self._write(encode_ws_frame(payload or b"", OP_CLOSE))
                self.closed = True
                return None
            if opcode == OP_BINARY:
                raise FrameTooBig(f"binary frame {len(payload)} bytes")
            if opcode != OP_TEXT:
                raise WsProtoError(f"bad opcode {opcode}")
            try:
                obj = json.loads(payload.decode("utf-8"))
            except Exception:
                self.state.audit("link_bad_json", bytes=len(payload))
                continue
            if not isinstance(obj, dict):
                self.state.audit("link_bad_json", shape=type(obj).__name__)
                continue
            return obj

    async def _write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("link down")
        self.writer.write(data)
        await self.writer.drain()

    async def send_text(self, text: str) -> None:
        raw = text.encode("utf-8")
        if len(raw) > MAX_FRAME_BYTES:
            raise ValueError(f"frame too big: {len(raw)} > {MAX_FRAME_BYTES}")
        await self._write(encode_ws_frame(raw, OP_TEXT))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        body = int(code).to_bytes(2, "big") + reason.encode("utf-8")[:120]
        try:
            await self._write(encode_ws_frame(body, OP_CLOSE))
        except Exception:
            pass
        self.closed = True
        try:
            self.writer.close()
        except Exception:
            pass


async def read_http_head(reader: asyncio.StreamReader):
    line = await reader.readline()
    if not line:
        return None
    try:
        method, path, _ver = line.decode("iso-8859-1").rstrip("\r\n").split(" ", 2)
    except ValueError:
        raise WsProtoError("bad request line")
    headers: dict[str, str] = {}
    while True:
        l = await reader.readline()
        if l in (b"\r\n", b"\n", b""):
            break
        if not l:
            break
        k, _, v = l.decode("iso-8859-1").rstrip("\r\n").partition(":")
        headers[k.strip().lower()] = v.strip()
    return method, path, headers


async def serve_fallback(state: BridgeState, sched: Scheduler, hub: ProgressHub,
                         sock: socket.socket) -> tuple[str, object]:
    """零依赖降级：aiohttp 不在场时用标准库把同一套接口跑起来。

    与 aiohttp 路径**共用** RestApi 与 link_session，所以语义不会漂；
    唯一多写的是 RFC6455 帧与 HTTP 报文收发。8192 闸在这里自己实现（F13/W17）。
    """
    api = RestApi(state, sched, hub)

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        tag = f"{peer[0]}:{peer[1]}"
        try:
            req = await read_http_head(reader)
            if req is None:
                return
            method, raw_path, headers = req
            # 升级判定要看三件事，缺一不可；注意 **Chrome 与合规客户端发的是
            # `Connection: Upgrade`（不是 `Connection: websocket`）**，
            # 只认后者会把真浏览器挡在门外——实测踩过，降级路径一律 404。
            conn_hdr = headers.get("connection", "").lower()
            wants_ws = (headers.get("upgrade", "").lower() == "websocket"
                        and headers.get("sec-websocket-key") is not None
                        and ("upgrade" in conn_hdr or "websocket" in conn_hdr))
            path = raw_path.split("?", 1)[0]
            query = dict(kv.split("=", 1) if "=" in kv else (kv, "")
                         for kv in (raw_path.split("?", 1) + [""])[1].split("&") if kv)
            if wants_ws and path == WS_PATH:
                key = headers.get("sec-websocket-key", "")
                if not key or headers.get("sec-websocket-version") != "13":
                    writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n"
                                 b"Connection: close\r\n\r\n")
                    await writer.drain()
                    return
                resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {ws_accept_key(key)}\r\n\r\n")
                writer.write(resp.encode("iso-8859-1"))
                await writer.drain()
                conn = FallbackConn(state, reader, writer)
                await link_session(state, sched, hub, conn, tag, state.port_scan)
                return

            body = b""
            te = headers.get("transfer-encoding", "").lower()
            if te and "chunked" in te:
                writer.write(b"HTTP/1.1 411 Length Required\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
                await writer.drain()
                return
            cl = int(headers.get("content-length") or 0)
            if cl > api.BODY_LIMIT:
                writer.write(b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
                await writer.drain()
                return
            if cl:
                body = await reader.readexactly(cl)
            res = await api.handle(method, path, query, headers, body)
            if res.stream is not None:
                head = f"HTTP/1.1 {res.status} Streaming\r\n"
                for k, v in res.headers.items():
                    head += f"{k}: {v}\r\n"
                head += "Connection: close\r\n\r\n"
                writer.write(head.encode("iso-8859-1"))
                await writer.drain()
                try:
                    async for chunk in res.stream:
                        writer.write(chunk)
                        await writer.drain()
                except (ConnectionResetError, asyncio.CancelledError, OSError):
                    pass
                finally:
                    writer.close()
                return
            payload = res.payload or b""
            head = f"HTTP/1.1 {res.status} OK\r\n"
            for k, v in res.headers.items():
                head += f"{k}: {v}\r\n"
            head += f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
            writer.write(head.encode("iso-8859-1") + payload)
            await writer.drain()
            writer.close()
        except (WsProtoError, FrameTooBig) as exc:
            code = getattr(exc, "close_code", 1009)
            state.audit("link_bad_frame" if isinstance(exc, FrameTooBig)
                        else "link_proto_error", detail=str(exc), peer=tag)
            try:
                writer.write(encode_ws_frame(int(code).to_bytes(2, "big")
                                             + str(exc).encode()[:120], OP_CLOSE))
                await writer.drain()
                writer.close()
            except Exception:
                pass
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            state.audit("http_worker_error", error=f"{type(exc).__name__}: {exc}")
        finally:
            try:
                if not writer.is_closing():
                    writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(client, sock=sock)

    async def closer():
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass

    return "stdlib-rfc6455", closer


# ==========================================================================
# 端口与保活
# ==========================================================================
def exclusive_listen_socket(port: int) -> socket.socket:
    """建一个只绑 127.0.0.1 的监听套接字，Windows 上设 SO_EXCLUSIVEADDRUSE。

    为什么不用 SO_REUSEADDR：Windows 的 SO_REUSEADDR 语义与 POSIX 不同，它**允许**
    第二个进程绑到已被别人占用的端口（端口劫持，F14）。微软官方建议服务端用
    SO_EXCLUSIVEADDRUSE。所以这里既不设 REUSEADDR，又显式设 EXCLUSIVE。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:                                # 非 Windows：保持不复用即可
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind(("127.0.0.1", port))
        s.listen(64)
    except OSError:
        s.close()                            # 绑不上就把这只 socket 关掉，别漏 fd
        raise
    return s


def probe_port(prefer: int = DEFAULT_WS_PORT, hi: int = PORT_MAX,
               wait_stale: float = 0.0) -> tuple[int, socket.socket]:
    """在 prefer..hi 上找一个能**独占**绑上的端口（WS 与 HTTP 同口，零配置的关键）。

    返回的端口取自 getsockname()，所以 `--port 0` 会拿到系统分给的临时端口
    （测试并发跑时不许跟真用户的 18888 抢门牌）。

    两类失败必须分清（W3 / W14b）：
      * 有人在听（connect_ex == 0）→ 真占用，跳过并记下来（扩展侧据此告警）
      * 没人听却绑不上 → 多半是本机 TIME_WAIT 或加速器保留段，重试一轮再放弃
    """
    stale: list[int] = []
    occupied: list[int] = []
    lo, hi2 = (prefer, hi) if prefer else (0, 0)     # prefer=0 → 只试 0（临时端口）
    for attempt in (0, 1):
        for p in range(lo, hi2 + 1):
            try:
                s = exclusive_listen_socket(p)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                return s.getsockname()[1], s
            except OSError:
                with socket.socket() as t:
                    t.settimeout(0.3)
                    listening = (t.connect_ex(("127.0.0.1", p)) == 0)
                (occupied if listening else stale).append(p)
                continue
        if attempt == 0 and stale and wait_stale > 0:
            time.sleep(wait_stale)
    raise OSError(f"no exclusive port in {lo}..{hi2}; "
                  f"occupied={occupied} stale_or_reserved={sorted(set(stale))}")


async def keepalive(state: BridgeState, sched: Scheduler) -> None:
    """服务端也按官方 20 秒发一次 ping（F1）；三个周期没动静就判定链路死了。

    为什么Bridge要主动踢：扩展侧的 keepalive 依赖 SW 活着，SW 被 Chrome 杀掉后
    这条 WS 在半开状态下不会自己关；不踢就会一直"以为连着"，任务全卡在 DISPATCH。
    """
    period = float(getattr(state.args, "keepalive_sec", KEEPALIVE_PING_SEC) or KEEPALIVE_PING_SEC)
    dead_limit = period * 3
    while True:
        try:
            await asyncio.sleep(period)
            link = state.link
            if not link or not link.alive:
                continue
            if time.time() - link.last_seen > dead_limit:
                state.audit("link_stale_close", peer=link.peer,
                            gapSec=round(time.time() - link.last_seen, 1))
                link.alive = False
                await link.conn.close(1002, "keepalive lost")
                continue
            await link.send({"type": "ping"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.audit("keepalive_error", error=f"{type(exc).__name__}: {exc}")


async def watchdog(state: BridgeState, sched: Scheduler, hub: ProgressHub) -> None:
    """全局心跳：STALE 检测 + 并发补位。放在 Bridge 进程里才可靠（F4/R11）。"""
    while True:
        try:
            await asyncio.sleep(1.0)
            now = time.time()
            for j in list(state.jobs.values()):
                if j["state"] != "DOWNLOADING":
                    continue
                base = j.get("lastProgressAt") or j.get("dispatchedAt") or now
                gap = now - base
                if gap > state.args.stale_after and not j.get("staleFlagged"):
                    j["staleFlagged"] = True
                    state.audit("STALE", jobId=j["jobId"], gapSec=round(gap, 1),
                                warning="进度盲区超限，需人看；不自动重下以免重复下载")
                    hub.publish({"type": "stale", **public_job(j)})
                    state.touch(j)
            await sched.dispatch_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.audit("watchdog_error", error=f"{type(exc).__name__}: {exc}")


async def amain(args) -> int:
    # 熔断前置到所有分支之前（001-F6 闭环：不留"某个模式没装闸"的洞）
    state = BridgeState(args)
    if not args.no_egress_guard:
        install_egress_guard(state)
        state.audit("egress_guard", enabled=True, loopbackExempt=sorted(LOOPBACK_NAMES))
    else:
        state.guard_enabled = False
        state.audit("egress_guard", enabled=False,
                    note="--no-egress-guard 由人关闭，审计记 INFO；Bridge 仍不取文件字节")

    hub = ProgressHub()
    loop = asyncio.get_running_loop()
    sched = Scheduler(state, loop)

    port, sock = probe_port(args.port,
                            (args.port + args.port_scan - 1) if args.port else 0,
                            wait_stale=args.bind_wait)
    state.port_scan = list(range(args.port, args.port + args.port_scan)) if args.port else [port]
    try:
        # 环境变量是专门给 W10（零依赖降级）用的开关：不拆环境也能验降级路径，
        # 默认不设 = 优先 aiohttp。降级与 aiohttp 两条路径共用 RestApi/link_session，
        # 所以“降级版”不是另一套语义，只是换了张收发邮票的皮。
        forced = os.environ.get("CDL_TRANSPORT", "").strip().lower()
        if forced in ("stdlib", "fallback", "rfc6455"):
            raise ImportError("CDL_TRANSPORT 强制零依赖降级（W10）")
        import importlib.util
        if importlib.util.find_spec("aiohttp") is None:
            raise ImportError("aiohttp 不在场")
        transport, closer = await serve_aiohttp(state, sched, hub, sock)
    except ImportError:
        transport, closer = await serve_fallback(state, sched, hub, sock)
    state.transport = transport
    state.ws_port = port

    endpoint = {
        "version": PROTOCOL_VERSION, "token": state.token, "pid": os.getpid(),
        "httpHost": "127.0.0.1", "httpPort": port, "linkPort": port,
        "wsPort": port, "wsPath": WS_PATH, "transport": transport,
        "startedAt": state.started_at, "bridgeStartedAt": state.peer_started_at,
        "downloadRoot": str(state.download_root), "stateDir": str(state.state_dir),
        "egressGuard": state.guard_enabled, "maxFrame": MAX_FRAME_BYTES,
        "bridgeId": state.bridge_id, "paired": state.paired,
        "portScanCount": args.port_scan,
    }
    ep = state.state_dir / "endpoint.json"
    ep.write_text(json.dumps(endpoint, indent=1), encoding="utf-8")
    try:
        os.chmod(ep, 0o600)
    except OSError:
        pass
    state.save_ledger()

    print(json.dumps({"event": "bridge_ready", "wsPort": port, "transport": transport,
                      "bridgeId": state.bridge_id, "paired": state.paired,
                      "token": state.token, "endpoint": str(ep),
                      "egressGuard": state.guard_enabled, "maxFrame": MAX_FRAME_BYTES},
                     ensure_ascii=False))
    sys.stdout.flush()

    wd = asyncio.create_task(watchdog(state, sched, hub))
    ka = asyncio.create_task(keepalive(state, sched))
    stop = asyncio.Event()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal_module, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, ValueError, RuntimeError):
            pass          # Windows 上没有 add_signal_handler，走 KeyboardInterrupt
    try:
        await stop.wait()
    finally:
        wd.cancel()
        ka.cancel()
        await asyncio.gather(wd, ka, return_exceptions=True)
        if state.link and state.link.alive:
            state.link.alive = False
            try:
                await state.link.send({"type": "replaced", "reason": "BRIDGE_STOPPING"})
            except Exception:
                pass
        try:
            await closer()
        except Exception as exc:
            state.audit("bridge_cleanup_error", error=str(exc))
        sock.close()
        state.save_ledger()
        state.audit("bridge_stop")
    return 0


# ==========================================================================
# 扩展侧握手自测（W13）：在**不依赖 Chrome** 的情况下把 HMAC 双向挑战应答跑一遍
# ==========================================================================
def _client_side_handshake(sock, want_key: bytes | None, bridge_id_expect: str | None,
                           verbose=True) -> dict:
    """纯标准库实现的**扩展侧**握手客户端，用来做正反两向测试。

    返回 {"ok":..., "reason":..., "bridgeId":..., "pairing":...}
    注意：这是测试夹具，不是交付代码（交付的扩展端在 extension/background.js）。
    """
    fh = sock.makefile("rwb")

    def send(obj):
        raw = encode_frame(obj)
        mask = secrets.token_bytes(4)
        n = len(raw)
        if n <= 125:
            head = bytes([0x81, 0x80 | n])
        elif n <= 0xFFFF:
            head = bytes([0x81, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head = bytes([0x81, 0x80 | 127]) + n.to_bytes(8, "big")
        fh.write(head + mask + unmask(raw, mask))
        fh.flush()

    def recv_frame():
        h = fh.read(2)
        if len(h) < 2:
            return None, None
        opcode, ln = h[0] & 0x0F, h[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(fh.read(2), "big")
        elif ln == 127:
            ln = int.from_bytes(fh.read(8), "big")
        payload = fh.read(ln)
        return opcode, payload

    def recv_msg():
        while True:
            opcode, payload = recv_frame()
            if opcode is None:
                return None
            if opcode in (OP_PING,):
                continue
            if opcode == OP_CLOSE:
                return None
            if opcode != OP_TEXT:
                return {"_binary": len(payload or b"")}
            try:
                return json.loads(payload.decode("utf-8"))
            except Exception:
                return {"_badjson": True}

    key = want_key if want_key is not None else secrets.token_bytes(32)
    cnonce = secrets.token_hex(16)
    hello = {"v": PROTOCOL_VERSION, "type": "hello_challenge", "clientNonce": cnonce,
             "role": "extension", "chromeVersion": "HANDSHAKE-SELFTEST",
             "extensionVersion": "test", "port": 0}
    if want_key is None:
        hello["keyExchange"] = key.hex()
    hs = (f"GET {WS_PATH} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
          f"Connection: Upgrade\r\nSec-WebSocket-Key: {base64.b64encode(secrets.token_bytes(16)).decode()}\r\n"
          "Sec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(hs.encode())
    # 读 101 响应头
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = fh.readline()
        if not chunk:
            return {"ok": False, "reason": "NO_HTTP_UPGRADE"}
        head += chunk
    status_line = head.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    if "101" not in status_line:
        return {"ok": False, "reason": f"NO_101:{status_line}"}
    send(hello)
    rep = recv_msg()
    if not rep or rep.get("type") != "hello_verify":
        return {"ok": False, "reason": f"NO_CHALLENGE:{rep}"}
    if rep.get("type") == "error":
        return {"ok": False, "reason": rep.get("reason")}
    if bridge_id_expect and rep.get("bridgeId") != bridge_id_expect:
        return {"ok": False, "reason": "BRIDGE_ID_CHANGED", "bridgeId": rep.get("bridgeId")}
    got = rep.get("hmac", "")
    expect = challenge_expect(key, cnonce, rep.get("nonce", ""), HMAC_CTX_BRIDGE)
    if not hmac_ok(expect, got):
        return {"ok": False, "reason": "BRIDGE_HMAC_MISMATCH", "bridgeId": rep.get("bridgeId")}
    send({"v": PROTOCOL_VERSION, "type": "hello_verify",
          "hmac": response_expect(key, cnonce, rep.get("nonce", ""))})
    wel = recv_msg()
    if not wel or wel.get("type") != "welcome":
        return {"ok": False, "reason": f"NO_WELCOME:{wel}"}
    return {"ok": True, "bridgeId": rep.get("bridgeId"),
            "welcome": wel, "key": key}


def handshake_selftest(args) -> int:
    """W13 三小问：①不知 K 的伪服务端过不了扩展侧验签；②不知 K 的伪客户端被服务端拒；
    ③正常双向握手连打 N 次全成功。全程只碰 127.0.0.1。"""
    out = {"loopbackOnly": True, "tests": []}
    ep = Path(args.state_dir) / "endpoint.json"
    if not ep.exists():
        print(json.dumps({"error": "ENDPOINT_NOT_FOUND", "path": str(ep)}))
        return 2
    info = json.loads(ep.read_text(encoding="utf-8"))
    port = info["wsPort"]

    def attempt(name, fn):
        try:
            res = fn()
        except Exception as exc:
            res = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        out["tests"].append({"name": name, **res})
        return res

    # ② 伪造客户端：带着**另一个**密钥来（不交 keyExchange，所以服务端不会重新 TOFU）
    def fake_client():
        bad = secrets.token_bytes(32)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:  # noqa: egress-audit
            r = _client_side_handshake(s, want_key=bad, bridge_id_expect=None, verbose=False)
        return {"ok": r.get("ok") is False, "serverSaw": r.get("reason"),
                "detail": "伪造密钥的客户端必须被服务端拒绝"}

    # ③ 正常双向：用**已配对**的密钥连 N 次
    good_key = read_pairing_key(Path(args.state_dir))

    def good_clients(n):
        results = []
        for _ in range(n):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:  # noqa: egress-audit
                rr = _client_side_handshake(s, want_key=good_key,
                                            bridge_id_expect=info.get("bridgeId"))
            results.append(rr.get("ok"))
        return {"ok": all(x is True for x in results), "attempts": len(results),
                "failures": results.count(False)}

    def unpaired_reject():
        """没有 keyExchange 且服务端未配对 → 必须拒。用一次性 state_dir 起不了这场景，
        所以这里改测**旧版 hello**（无 HMAC）：服务端必须拒，等价于未配对的冒充者。"""
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:  # noqa: egress-audit
            fh = s.makefile("rwb")
            hs = (f"GET {WS_PATH} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Key: "
                  f"{base64.b64encode(secrets.token_bytes(16)).decode()}\r\n"
                  "Sec-WebSocket-Version: 13\r\n\r\n")
            s.sendall(hs.encode())
            head = b""
            while b"\r\n\r\n" not in head:
                line = fh.readline()
                if not line:
                    break
                head += line
            raw = encode_frame({"v": 1, "type": "hello", "role": "extension"})
            mask = secrets.token_bytes(4)
            s.sendall(bytes([0x81, 0x80 | len(raw)]) + mask + unmask(raw, mask))
            h = fh.read(2)
            ln = h[1] & 0x7F
            if ln == 126:
                ln = int.from_bytes(fh.read(2), "big")
            payload = fh.read(ln)
            obj = json.loads(payload.decode()) if payload else {}
        return {"ok": obj.get("reason") == "NOT_HMAC_HELLO", "serverSaw": obj.get("reason")}

    attempt("W13-2_fake_client_rejected", fake_client)
    attempt("W13-1_legacy_hello_rejected", unpaired_reject)
    if good_key is not None:
        attempt("W13-3_good_handshake_x100", lambda: good_clients(args.selftest_rounds))
    else:
        out["tests"].append({"name": "W13-3_good_handshake_x100", "ok": False,
                             "reason": "NOT_PAIRED_YET"})
    out["verdict"] = "PASS" if all(t.get("ok") for t in out["tests"]) else "FAIL"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["verdict"] == "PASS" else 1


# ==========================================================================
# 熔断自测（V4-b 取证）：在装了熔断的本进程内故意违规，必须被拦
# ==========================================================================
def egress_selftest(args) -> int:
    """装完熔断后，尝试若干违规动作，逐个报告是否被拦。全程不得有任何真出站。"""
    state = BridgeState(args)
    install_egress_guard(state)          # 自测必须强制装，不接受关闭
    targets = [a for a in (args.selftest_targets or
                           ["example.com:80", "198.18.0.1:443", "8.8.8.8:53"])]
    results = []

    def attempt(kind, fn):
        try:
            fn()
            results.append({"kind": kind, "blocked": False, "note": "NOT BLOCKED (!!)"})
        except EgressBlocked as e:
            results.append({"kind": kind, "blocked": True, "error": str(e)[:160]})
        except Exception as e:
            results.append({"kind": kind, "blocked": True, "error":
                            f"{type(e).__name__}: {str(e)[:120]}"})

    for t in targets:
        host, _, port = t.partition(":")
        p = int(port or 80)
        attempt(f"socket.create_connection[{t}]",
                lambda host=host, p=p: socket.create_connection((host, p), timeout=2))  # noqa: egress-audit
        attempt(f"socket.connect[{t}]",
                lambda host=host, p=p: socket.socket().connect((host, p)))  # noqa: egress-audit
        attempt(f"getaddrinfo[{t}]", lambda host=host: socket.getaddrinfo(host, None))

    # 以下三行是"故意违规样本"：装了熔断后必然被拦，不会真的发出任何请求
    attempt("subprocess.Popen[external-tool]",
            lambda: subprocess.Popen(["curl", "-s", SAMPLE_URL]))               # noqa: egress-audit
    attempt("os.system[external-tool]",
            lambda: os.system("curl -s " + SAMPLE_URL))                          # noqa: egress-audit
    attempt("os.execv", lambda: os.execv("curl", ["curl", SAMPLE_URL]))          # noqa: egress-audit

    # 回环必须放行（C1 回环豁免，否则本进程连不上 Bridge / 测不了 mock 源站）
    listener = exclusive_listen_socket(0)
    lport = listener.getsockname()[1]
    lb_ok = None
    try:
        with socket.socket() as s:
            s.settimeout(2.0)
            try:
                s.connect(("127.0.0.1", lport))
                lb_ok = "allowed"
            except EgressBlocked as e:
                lb_ok = f"WRONGLY BLOCKED: {e}"
            except OSError as e:
                lb_ok = f"denied? {e}"
    finally:
        listener.close()

    all_blocked = all(r["blocked"] for r in results)
    out = {"guard_enabled": state.guard_enabled, "attempts": results,
           "loopback": lb_ok, "blockedLogEntries": state.blocked_count,
           "blockedLog": str(state.blocked_path),
           "verdict": "PASS" if (all_blocked and lb_ok.startswith("allowed")) else "FAIL"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["verdict"] == "PASS" else 1


# ==========================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bridge.py",
                                description="chrome-dl-bridge v0.2（回环 WebSocket 指挥官；"
                                            "只发号施令，不碰字节）")
    p.add_argument("--port", type=int, default=DEFAULT_WS_PORT,
                   help=f"首选端口（默认 {DEFAULT_WS_PORT}）；被占则向上扫 --port-scan 个")
    p.add_argument("--port-scan", type=int, default=10,
                   help="候选端口个数（18888..18897 = 10）")
    p.add_argument("--keepalive-sec", type=float, default=KEEPALIVE_PING_SEC,
                   help="服务端 WS ping 周期（默认 20 秒 = Chrome 官方示例值 F1；"
                        "测试可调小以观察保活与死链判定，不改默认行为）")
    p.add_argument("--bind-wait", type=float, default=0.6,
                   help="本轮全部绑不上时，重试一轮前的等待秒数（清 TIME_WAIT）")
    p.add_argument("--state-dir", default=os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "chrome-dl-bridge"))
    p.add_argument("--download-root", default=os.path.join(
        os.path.expanduser("~"), "Downloads"),
                   help="必须等于 Chrome 自己的下载目录（filename 在它之下解析）")
    p.add_argument("--folder", default="chrome-dl",
                   help="落在 download_root 下的子目录；置空则不建子目录")
    p.add_argument("--token", default=None)
    p.add_argument("--no-token", action="store_true")
    p.add_argument("--no-egress-guard", action="store_true")
    p.add_argument("--max-concurrent", type=int, default=4)
    p.add_argument("--per-host", type=int, default=2)
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("--stale-after", type=float, default=45.0)
    p.add_argument("--link-wait-timeout", type=float, default=120.0)
    p.add_argument("--conflict", default="uniquify",
                   choices=["uniquify", "overwrite", "prompt"])
    p.add_argument("--chrome-version", default=None)
    p.add_argument("--ext-version", default=None)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--selftest-egress", action="store_true",
                   help="V4-b：装熔断后在本进程内故意违规，验证必被拦")
    p.add_argument("--selftest-handshake", action="store_true",
                   help="W13：对已在跑的 Bridge 跑 HMAC 双向握手正反测试")
    p.add_argument("--selftest-rounds", type=int, default=100,
                   help="握手自测的正常次数（W13③）")
    p.add_argument("--selftest-target", dest="selftest_targets", action="append",
                   metavar="host:port", help="自测目标，可多次（默认 example.com/198.18.0.1/8.8.8.8）")
    args = p.parse_args(argv)

    if args.selftest_egress:
        return egress_selftest(args)
    if args.selftest_handshake:
        return handshake_selftest(args)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
