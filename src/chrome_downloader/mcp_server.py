#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_server.py —— 零第三方依赖的 MCP stdio 服务端（双时代）
（PLAN-CHROME-DOWNLOADER-003 · T5，§3.3；判据 M1–M13；规范凭据见 docs/mcp-spec-archive.md）

**双时代**是什么意思（不是我发明的兼容姿势，是规范原文）：
  · 现代（2026-07-28）：没有握手。每个请求在 `_meta.io.modelcontextprotocol/protocolVersion`
    里自带版本号，服务端逐请求接受或拒绝（§存档 2.2）。
  · 旧时代（2025-11-25 及更早）：先 `initialize` 握手，服务端回显 `protocolVersion`。
  · 双时代服务端"按客户端怎么开口来选择行为"（§存档 2.7 原文：
    "A dual-era **server** selects its behavior from how the client opens"）。

三条 stdio 硬规矩（M6/M7 就是钉这三条，规范 §存档 2.8）：
  1) 只走 `sys.stdout.buffer.write(line + b"\\n")` + `flush()`，**禁用 print**
     —— Windows 的文本模式会把 `\\n` 写成 `\\r\\n`，那是非法消息帧。
  2) `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` —— 单行内不许有裸换行。
  3) 日志/诊断只走 stderr 或 daemon.log。stdout 上"不是 MCP 消息的东西"= 协议违规。

字节预算（C6 第三段 / M11）：任何 tools/call 响应序列化后**单行 ≤8192 字节**。
超限由服务端**截断**（不发出超限行），并附 `truncated/totalCount/omitted`。

⚠️ 冷启动实话（M13，R6 缓解）：8 个工具的 description **全部含**"插件冷启动最长约 30 秒"。
   不写这句就等于把"Chrome 定时器最快 30 秒醒一次"这个已知代价藏进模型的眼罩里。

C1：本模块不取任何文件字节。所有动作都转成对**本机 Bridge** 的 HTTP 调用，
    走搬运件 client.py（内含 _assert_loopback），自己一行网络代码都不写。
"""
from __future__ import annotations

import json
import sys

from . import __version__, paths

PROTOCOL_MODERN = "2026-07-28"
PROTOCOL_LEGACY = "2025-11-25"
SUPPORTED_VERSIONS = [PROTOCOL_MODERN, PROTOCOL_LEGACY]

# 规范 §存档 2.10：本实现允许发出的 JSON-RPC 错误码（M12 的白名单）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022
ALLOWED_ERROR_CODES = [PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND,
                       INVALID_PARAMS, INTERNAL_ERROR, UNSUPPORTED_PROTOCOL_VERSION]

MAX_LINE_BYTES = 8192           # C6 第三段：与 WS 帧上限同数，故意对齐（不另设一套口径）
SERVER_INFO = {"name": "chrome-downloader-client", "version": __version__}
INSTRUCTIONS = (
    "本服务把下载交给浏览器做：所有文件字节都由 Chrome 扩展经 chrome.downloads 落盘，"
    "服务端与本机桥只发号施令、只读进度。"
    "前提：Chrome 里已加载扩展，且本机桥可用（不可用时会自动拉起）。"
    "Chrome 扩展后台休眠时，任务最长约 30 秒才开始动（官方定时器下限），"
    "不是卡死；要加速可让用户点一下扩展图标。"
)

COLD_START_TRUTH = "插件冷启动最长约 30 秒"      # M13 逐字命中串

META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"


# ==========================================================================
# 工具表（8 个，顺序固定 —— M2 要求两次 tools/list 逐字节相同）
# ==========================================================================
def tool_defs() -> list[dict]:
    def t(name, title, desc, props, required=(), read_only=False):
        schema = {"type": "object", "properties": props,
                  "required": list(required), "additionalProperties": False}
        return {"name": name, "title": title, "description": desc, "inputSchema": schema,
                "annotations": {"readOnlyHint": read_only,
                                "destructiveHint": False,
                                "idempotentHint": name in ("chrome_download_status",
                                                           "chrome_download_list",
                                                           "chrome_download_progress",
                                                           "chrome_download_wait")}}

    return [
        t("chrome_download_status", "桥与扩展状态",
          f"查本机下载桥是否可用、扩展有没有连上、出站熔断状态。"
          f"排查第一步就敲它。{COLD_START_TRUTH}（Chrome 扩展后台定时器官方下限），"
          f"任务没动静先看这里的 extensionConnected。",
          {"stateDir": {"type": "string",
                         "description": "状态目录（默认按 endpoint.json 的位置）"}},
          read_only=True),
        t("chrome_download", "提交下载（字节由 Chrome 取）",
          f"提交一个或一批 URL 给浏览器下载。**服务端不取任何字节**，只把任务交给扩展。"
          f"注意：插件冷启动最长约 30 秒才醒（官方定时器下限），所以提交后进度不立刻动是正常的；"
          f"要等结果请带 wait=true 或改用 chrome_download_wait。",
          {"urls": {"type": "array", "items": {"type": "string"},
                    "description": "要下载的 URL 列表（http/https；服务端会拒非 http(s)）"},
           "filename": {"type": "string", "description": "落盘文件名（可省，缺省由 URL 推）"},
           "sha256": {"type": "string", "description": "期望的 sha256；给了才会做完整性校验"},
           "wait": {"type": "boolean",
                    "description": "是否阻塞到终态（默认 false，与 v0.2 SDK 一致）"},
           "timeout_sec": {"type": "number",
                           "description": f"wait=true 时的超时秒（上限 600）"},
           "stateDir": {"type": "string"}},
          required=("urls",)),
        t("chrome_download_list", "任务列表",
          f"列出账本里的下载任务（含状态/进度/字节数）。"
          f"账本很大时本工具会**截断**到单行 8192 字节以内并标 truncated=true，"
          f"届时请用 jobId 精确查（chrome_download_progress）。插件冷启动最长约 30 秒。",
          {"stateDir": {"type": "string"},
           "includeTerminal": {"type": "boolean",
                               "description": "是否含终态任务（默认 true）"}},
          read_only=True),
        t("chrome_download_progress", "单个任务进度",
          f"看一个 jobId 的实时进度与网速。{COLD_START_TRUTH}，"
          f"刚提交的任务数字不动属正常，别重复提交。",
          {"jobId": {"type": "string"}, "stateDir": {"type": "string"}},
          required=("jobId",), read_only=True),
        t("chrome_download_wait", "等一批任务到终态",
          f"阻塞等到指定 jobId 全部进入终态（COMPLETE/FAILED/CANCELLED）或超时。"
          f"超时上限 600 秒；{COLD_START_TRUTH}，把 timeout 压太小会误判成没下完。",
          {"jobIds": {"type": "array", "items": {"type": "string"}},
           "timeout_sec": {"type": "number", "description": "默认 120，上限 600"},
           "stateDir": {"type": "string"}},
          read_only=True),
        t("chrome_download_pause", "暂停",
          f"暂停一个正在下的任务。{COLD_START_TRUTH}；暂停是发给扩展的指令，"
          f"扩展睡着时这条会排队到它醒来。",
          {"jobId": {"type": "string"}, "stateDir": {"type": "string"}},
          required=("jobId",)),
        t("chrome_download_resume", "继续",
          f"继续一个被暂停/中断的任务（能续传则 Range 续传，不能则整单重头下，"
          f"重头下会标 restarted=true）。{COLD_START_TRUTH}。",
          {"jobId": {"type": "string"}, "stateDir": {"type": "string"}},
          required=("jobId",)),
        t("chrome_download_cancel", "取消",
          f"取消一个任务（不可逆；要重下请新提交）。{COLD_START_TRUTH}。",
          {"jobId": {"type": "string"}, "stateDir": {"type": "string"}},
          required=("jobId",)),
    ]


# ==========================================================================
# 出站：只走本机桥
# ==========================================================================
def _bridge(state_dir=None, ensure=True):
    """拿到一个连好的 Client（必要时自动拉起桥）。"""
    from .daemon import make_client, DaemonError
    try:
        return make_client(state_dir, ensure=ensure)
    except DaemonError as exc:
        raise BridgeUnavailable(str(exc)) from exc


class BridgeUnavailable(RuntimeError):
    pass


# ==========================================================================
# 字节预算（M11）
# ==========================================================================
def _line_len(obj: dict) -> int:
    return len(_dumps(obj))


def _dumps(obj) -> bytes:
    # 单行、无裸换行、UTF-8（规范 §存档 2.8）
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


TRUNC_NOTE = "请用 jobId 精确查询（chrome_download_progress / chrome_download_wait）。"


def pack_response(resp: dict, max_bytes: int = MAX_LINE_BYTES) -> dict:
    """把一条**完整响应信封**装进单行 ≤max_bytes 字节（M11 / C6 第三段）。

    预算口径（§3.3 明文，顺序不许换）：
      1) `structuredContent` 先占位 —— 它是机器要读的数据；
      2) 剩余额度给 `content[].text`，text 超限就截断并加“…（已截断）”；
      3) 连 structuredContent 都装不下 → 从列表尾部**整条**丢弃，并如实写
         `truncated:true` + `totalCount` + `omitted`。
    绝不悄悄丢数据：丢了必须标，标了模型才会去精确查，而不是以为拿到了全集。

    ⚠️ 实现要点（第一版在这上头栽过，M11 虚绿）：裁剪循环里的试算**必须把最终
       要说的那句提示语一起算进长度**。只按 content=[] 试算会得到"刚好装下"的
       假结论，加上真实提示语就溢出，然后一路退到兜底分支，把 560 条压成
       `totalCount: 1` —— 行长虽然 ≤8192，但数据等于丢了还没说实话。
    """
    if _line_len(resp) <= max_bytes:
        return resp
    out = json.loads(_dumps(resp).decode("utf-8"))          # 深拷贝，不动调用方对象
    result = out.get("result")
    if not isinstance(result, dict):
        return _fit_error(out, max_bytes)                   # 错误响应：只削长度

    texts = result.get("content") or []

    # ---- 阶段 A：structuredContent 单独装得下 → 只削 text ----
    probe = dict(out)
    probe["result"] = dict(result, content=[])
    if _line_len(probe) <= max_bytes:
        room = max_bytes - _line_len(probe)
        kept = _fit_texts(texts, room)
        result["content"] = kept
        total_text = sum(len((t.get("text") or "").encode("utf-8")) for t in texts)
        if total_text > max(0, room):
            result["truncated"] = True
            result["totalCount"] = len(texts)
            result["omitted"] = max(0, len(texts) - len(kept))
            result["shownCount"] = len(kept)
        if _line_len(out) <= max_bytes:
            return out
        # 连一句最短的话都塞不进 → 把 text 收成最短诚实说明
        result["content"] = [_text(TRUNC_NOTE)]
        result["truncated"] = True
        result.setdefault("totalCount", len(texts))
        if _line_len(out) <= max_bytes:
            return out
        return _emergency_fit(out, max_bytes)

    # ---- 阶段 B：structuredContent 里有列表 → 从尾部整条丢 ----
    key = _list_key(result.get("structuredContent"))
    if key:
        items = list(result["structuredContent"][key])
        total = len(items)
        for k in range(total, -1, -1):
            cand_sc = dict(result["structuredContent"])
            cand_sc[key] = items[:k]
            cand_sc["count"] = k
            note = _trunc_note(total, k, max_bytes)
            trial = dict(out)
            trial["result"] = dict(result, structuredContent=cand_sc,
                                   content=[_text(note)], truncated=True,
                                   totalCount=total, omitted=total - k, shownCount=k)
            if _line_len(trial) <= max_bytes:
                return trial
        # 一条都塞不进：只留信封 + 提示，明确标 omitted=total
        note = _trunc_note(total, 0, max_bytes)
        trial = dict(out)
        trial["result"] = {"truncated": True, "totalCount": total, "omitted": total,
                           "shownCount": 0, "content": [_text(note)]}
        if isinstance(result.get("resultType"), str):
            trial["result"]["resultType"] = result["resultType"]
        if _line_len(trial) <= max_bytes:
            return trial
    return _emergency_fit(out, max_bytes)


def _trunc_note(total: int, shown: int, max_bytes: int) -> str:
    return (f"共 {total} 条，单行字节上限 {max_bytes}，已显示前 {shown} 条，"
            f"省略 {total - shown} 条。{TRUNC_NOTE}")


def _fit_texts(texts: list, room: int) -> list[dict]:
    """按整块保留；装不下的那块做字节安全截断。"""
    kept: list[dict] = []
    used = 0
    for blk in texts:
        b = (blk.get("text") or "").encode("utf-8")
        if used + len(b) <= room:
            kept.append(dict(blk))
            used += len(b)
            continue
        rest = room - used
        if rest > 16:
            s = _safe_decode(b[:rest])
            kept.append(dict(blk, text=s + "…（已截断）"))
            used = room
        if used >= room:
            break
    return kept or [{"type": "text", "text": "（内容超出单行字节上限，已截断）"}]


def _safe_decode(b: bytes) -> str:
    """逐字节切会把多字节汉字劈半 → 退到完整字符边界。"""
    while b:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            b = b[:-1]
    return ""


def _list_key(sc) -> str | None:
    if isinstance(sc, dict):
        for k in ("jobs", "results", "items"):
            if isinstance(sc.get(k), list):
                return k
    return None


def _fit_error(out: dict, max_bytes: int) -> dict:
    """错误响应超预算：逐层削 data.detail → message，保信封形状不变。"""
    err = out.get("error")
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and data.get("detail"):
            data["detail"] = _safe_decode(str(data["detail"]).encode("utf-8")[:512]) \
                + "…（详情过长已截断）"
            if _line_len(out) <= max_bytes:
                return out
        if isinstance(data, dict):
            err["data"] = {"detail": "detail omitted (over byte budget)"}
            if _line_len(out) <= max_bytes:
                return out
        err["message"] = (str(err.get("message", ""))[:120])
    return out


def _emergency_fit(out: dict, max_bytes: int) -> dict:
    """最后兜底：只留信封 + 一句人话。宁可信息少，也不许吐出一条超长的行。

    到这条路意味着上面两级削都救不回来（现实中只有畸形超大 single object 才会）；
    它必须**如实标 truncated**，不许安静地发一条不完整的数据。"""
    result = out.get("result") if isinstance(out.get("result"), dict) else {}
    out["result"] = {"truncated": True, "totalCount": 1, "omitted": 1, "shownCount": 0}
    if isinstance(result, dict) and result.get("resultType"):
        out["result"]["resultType"] = result["resultType"]
    if isinstance(result, dict) and result.get("isError"):
        out["result"]["isError"] = True
    out["result"]["content"] = [{"type": "text",
                                 "text": f"响应超出单行字节上限 {max_bytes}，已截断。"
                                         f"{TRUNC_NOTE}"}]
    if _line_len(out) <= max_bytes:
        return out
    out["result"]["content"] = [{"type": "text", "text": "truncated"}]
    return out


# ==========================================================================
# 现代/旧时代的判定（§存档 2.7）
# ==========================================================================
def meta_of(msg: dict) -> dict:
    params = msg.get("params") if isinstance(msg, dict) else None
    if not isinstance(params, dict):
        return {}
    m = params.get("_meta")
    return m if isinstance(m, dict) else {}


def requested_version(msg: dict) -> str | None:
    v = meta_of(msg).get(META_VERSION_KEY)
    return v if isinstance(v, str) and v else None


def is_modern(msg: dict) -> bool:
    """带现代 `_meta` 的请求 → 现代面。

    注意：只认 `_meta` 里的版本键。**不许**把"有 params 没版本"当成现代
    （那样旧客户端的 initialize 会被我误判成现代请求并吐出一个它看不懂的错误）。
    """
    return requested_version(msg) is not None


# ==========================================================================
# 分发
# ==========================================================================
class Session:
    """一次 stdio 会话的时代状态。

    规范说"时代是服务器的属性，客户端 SHOULD 缓存"；服务端这边按"客户端怎么开口"逐条选，
    所以这里只记"第一次见到的是哪种"，用来给旧时代会话补发 `notifications/*` 的边角。
    """

    def __init__(self):
        self.era: str | None = None          # "modern" | "legacy"
        self.legacy_initialized = False
        self.emitted_codes: set[int] = set()      # M12 要"穷举触发一次"，这里记账

    def note_error(self, code: int) -> None:
        self.emitted_codes.add(code)


def handle(msg: dict, session: Session) -> dict | None:
    """一条消息 → 响应 dict（None = 通知，不需回应）。"""
    if not isinstance(msg, dict):
        session.note_error(INVALID_REQUEST)
        return error(None, INVALID_REQUEST, "Invalid Request", "消息不是 JSON 对象")
    method = msg.get("method")
    mid = msg.get("id")
    if not isinstance(method, str):
        session.note_error(INVALID_REQUEST)
        return error(mid, INVALID_REQUEST, "Invalid Request", "缺 method")

    modern = is_modern(msg)
    # ---- 旧时代的开门：initialize ----
    if method == "initialize":
        if modern:
            # 现代面里 initialize 不是方法（握手已取消）→ 明确告诉它走哪条路
            session.note_error(METHOD_NOT_FOUND)
            return error(mid, METHOD_NOT_FOUND, "Method not found",
                         f"protocol revision {PROTOCOL_MODERN} 没有 initialize 握手；"
                         f"请改用 server/discover，或在每个请求的 _meta 里带 "
                         f"{META_VERSION_KEY}")
        if session.era is None:
            session.era = "legacy"
        session.legacy_initialized = True
        return legacy_initialize_result(msg, mid)

    # ---- 现代面：版本闸（M3）----
    if modern:
        v = requested_version(msg)
        if v not in SUPPORTED_VERSIONS:
            session.era = session.era or "modern"
            session.note_error(UNSUPPORTED_PROTOCOL_VERSION)
            # 关键：**不回退 initialize**。规范 §存档 2.11 原文：
            # "Use one of the versions in its advertised `supported` list.
            #  Do **not** fall back to `initialize`."
            return unsupported_version(mid, v)
        if session.era is None:
            session.era = "modern"

    # ---- 通知（两条路都不回）----
    if method.startswith("notifications/"):
        return None

    if method == "server/discover":
        if not modern:
            session.note_error(INVALID_REQUEST)
            return error(mid, INVALID_REQUEST, "Invalid Request",
                         f"server/discover 需要 _meta 里的 {META_VERSION_KEY}")
        return discover_result(mid)
    if method == "tools/list":
        return ok(mid, tools_list_result(modern, session))
    if method == "tools/call":
        return ok(mid, tools_call_result(msg, modern, session))
    if method in ("prompts/list", "resources/list", "resources/templates/list",
                  "logging/setLevel"):
        # 本服务端没这些能力：规范说"未知方法 -32601"；能力没声明也不该假装支持
        session.note_error(METHOD_NOT_FOUND)
        return error(mid, METHOD_NOT_FOUND, "Method not found",
                     f"本服务端只提供 tools 能力，不支持 {method}")
    session.note_error(METHOD_NOT_FOUND)
    return error(mid, METHOD_NOT_FOUND, "Method not found", f"未知方法：{method}")


def error(mid, code: int, message: str, detail: str = "") -> dict:
    err = {"code": code, "message": message}
    if detail:
        err["data"] = {"detail": detail}
    return {"jsonrpc": "2.0", "id": mid, "error": err}


def unsupported_version(mid, requested) -> dict:
    # 形状照规范原文示例（§存档 2.3），字段一个不多一个不少
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": UNSUPPORTED_PROTOCOL_VERSION,
                      "message": "Unsupported protocol version",
                      "data": {"supported": list(SUPPORTED_VERSIONS),
                               "requested": requested}}}


def ok(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


# ==========================================================================
# 结果形状
# ==========================================================================
def discover_result(mid) -> dict:
    """M1：supportedVersions / capabilities.tools / serverInfo / instructions /
    ttlMs / cacheScope / resultType="complete"。"""
    result = {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_VERSIONS),
        "capabilities": {"tools": {"listChanged": False}},
        "_meta": {META_SERVER_INFO: dict(SERVER_INFO)},
        "instructions": INSTRUCTIONS,
        "ttlMs": 3600000,
        "cacheScope": "private",
    }
    return ok(mid, result)


def tools_list_result(modern: bool, session: Session) -> dict:
    """M2：工具按固定顺序，两次调用逐字节相同（不掺时间戳、不掺随机 id）。"""
    tools = tool_defs()
    if modern:
        return {"resultType": "complete", "tools": tools, "ttlMs": 300000,
                "cacheScope": "private"}
    return {"tools": tools}                    # 旧时代 Result 没有 resultType（M4）


def legacy_initialize_result(msg: dict, mid) -> dict:
    """M4：回显 protocolVersion（钉 2025-11-25）+ capabilities.tools + serverInfo。"""
    params = msg.get("params") or {}
    asked = params.get("protocolVersion") if isinstance(params, dict) else None
    version = asked if asked in SUPPORTED_VERSIONS else PROTOCOL_LEGACY
    result = {"protocolVersion": version,
              "capabilities": {"tools": {"listChanged": False}},
              "serverInfo": dict(SERVER_INFO),
              "instructions": INSTRUCTIONS}
    return ok(mid, result)


# ==========================================================================
# tools/call 的实现
# ==========================================================================
def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def call_tool(name: str, args: dict) -> dict:
    """返回 CallToolResult（不含 resultType，由调用方按时代补）。

    工具内的错误一律走 isError=true（规范 §存档 2.9：不许报成协议级错误，
    否则模型看不见、没法自我纠正）。
    """
    if not isinstance(args, dict):
        return tool_error("参数必须是对象")
    state_dir = args.get("stateDir") or None
    if name == "chrome_download_status":
        return call_status(state_dir)
    if name == "chrome_download":
        return call_download(args, state_dir)
    if name == "chrome_download_list":
        return call_list(args, state_dir)
    if name == "chrome_download_progress":
        return call_progress(args, state_dir)
    if name == "chrome_download_wait":
        return call_wait(args, state_dir)
    if name in ("chrome_download_pause", "chrome_download_resume",
                "chrome_download_cancel"):
        return call_control(name, args, state_dir)
    return tool_error(f"未知工具：{name}")


def tool_error(msg: str) -> dict:
    return {"content": [_text(msg)], "isError": True}


def tool_result(msg: str, structured, modern: bool, truncated_meta: dict | None = None):
    r = {"content": [_text(msg)], "structuredContent": structured, "isError": False}
    if modern:
        r["resultType"] = "complete"
    if truncated_meta:
        r.update(truncated_meta)
    return r


def call_status(state_dir) -> dict:
    from . import daemon as dm
    st = dm.status(state_dir)
    lines = [f"桥进程：{'活着' if st['bridgeAlive'] else '不在'}"
             f"（pid={st['bridgePid']}）",
             f"三级就绪：L1={st['readyLevels']['L1']} "
             f"L2={st['readyLevels']['L2']} L3={st['readyLevels']['L3']}",
             f"门牌：{st['port']} · 传输：{st['transport']}",
             f"扩展连接：{st['extensionConnected']} · 已配对：{st['paired']}",
             f"出站熔断：{st['egressGuard']}",
             f"状态目录：{st['stateDir']}"]
    if not st["ownedByUs"]:
        lines.append(f"⚠️ 归因：{st['attribution']}（这个进程不是我们认的桥）")
    return {"content": [_text("\n".join(lines))], "structuredContent": st,
            "isError": False}


def _reject_non_http(url: str) -> str | None:
    """M10：非 http(s) 一律拒，且**不产生任务**。"""
    if not isinstance(url, str) or not url:
        return "URL 不能为空"
    low = url.lower().strip()
    if low.startswith(("file:", "ftp:", "data:", "blob:", "chrome:", "chrome-extension:",
                       "javascript:", "about:", "mailto:")):
        return f"不支持的协议：{url[:60]}（只许 http/https；服务端不取字节也不许代取）"
    if not (low.startswith("http://") or low.startswith("https://")):
        return f"不是 http(s) URL：{url[:60]}"
    return None


def call_download(args, state_dir) -> dict:
    urls = args.get("urls")
    if not isinstance(urls, list) or not urls:
        return tool_error("urls 必须是非空字符串数组")
    if len(urls) > 200:
        return tool_error(f"一次最多 200 个 URL（收到 {len(urls)}）")
    for u in urls:
        why = _reject_non_http(u)
        if why:
            return tool_error(why + " → 未产生任何任务")
    wait = bool(args.get("wait"))
    timeout = args.get("timeout_sec")
    if timeout is not None:
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            return tool_error("timeout_sec 必须是正数")
        if timeout > 600:
            return tool_error("timeout_sec 上限 600 秒（MCP 侧不许开长阻塞）")
    sha = args.get("sha256")
    fname = args.get("filename")
    try:
        c = _bridge(state_dir)
    except BridgeUnavailable as exc:
        return tool_error(f"本机桥不可用：{exc}")
    try:
        jobs = []
        for i, u in enumerate(urls):
            spec = {"url": u}
            # filename 只在单 URL 时生效：多 URL 共用一个名字会互相覆盖（实测踩过）
            if fname and len(urls) == 1:
                spec["filename"] = fname
            if sha and len(urls) == 1:
                spec["sha256"] = sha
            r = c.add(u, spec.get("filename"), spec.get("sha256"))
            jobs.append(r["job"])
    except Exception as exc:
        return tool_error(f"提交失败：{type(exc).__name__}: {str(exc)[:200]}")
    ids = [j["jobId"] for j in jobs]
    final = jobs
    waited = False
    if wait:
        waited = True
        try:
            res = c.wait(ids, timeout=float(timeout or 120.0))
            final = res["jobs"]
            if not res["ok"]:
                return tool_error(
                    f"等待超时（{timeout or 120}s）未到终态；{COLD_START_TRUTH}，"
                    f"可把 timeout_sec 放宽，或用 chrome_download_progress 单独查 {ids}")
        except Exception as exc:
            return tool_error(f"等待出错：{type(exc).__name__}: {str(exc)[:160]}")
    done = [j for j in final if j.get("state") == "COMPLETE"]
    bad = [j for j in final if j.get("state") in ("FAILED", "CANCELLED")]
    text = (f"已提交 {len(ids)} 个任务：" + ", ".join(i[:12] for i in ids))
    if waited:
        text += f"；完成 {len(done)}，异常 {len(bad)}"
        if bad:
            text += "（详情见 structuredContent）"
    text += f"。{COLD_START_TRUTH}，没动静先查 chrome_download_status。"
    return {"content": [_text(text)],
            "structuredContent": {"jobId": ids[0] if len(ids) == 1 else None,
                                  "jobIds": ids, "jobs": final, "waited": waited},
            "isError": bool(waited and (len(done) != len(final)))}


def call_list(args, state_dir) -> dict:
    try:
        c = _bridge(state_dir, ensure=False)
    except Exception as exc:
        return tool_error(f"连不上桥：{type(exc).__name__}: {str(exc)[:160]}")
    try:
        jobs = c.list()
    except Exception as exc:
        return tool_error(f"读账本失败：{type(exc).__name__}: {str(exc)[:160]}")
    if not args.get("includeTerminal", True):
        jobs = [j for j in jobs if j.get("state") not in
                ("COMPLETE", "FAILED", "CANCELLED")]
    slim = [{"jobId": j.get("jobId"), "filename": j.get("filename"),
             "state": j.get("state"), "bytesReceived": j.get("bytesReceived"),
             "totalBytes": j.get("totalBytes"), "speedBps": j.get("speedBps"),
             "sha256Actual": j.get("sha256Actual")} for j in jobs]
    n = len(slim)
    text = (f"共 {n} 个任务。" if n else "账本为空。") + \
           (f"其中进行中 {sum(1 for j in slim if j['state'] not in ('COMPLETE','FAILED','CANCELLED'))} 个。"
            if n else "")
    return {"content": [_text(text)],
            "structuredContent": {"jobs": slim, "count": n}, "isError": False}


def call_progress(args, state_dir) -> dict:
    jid = args.get("jobId")
    if not isinstance(jid, str) or not jid:
        return tool_error("jobId 必须是字符串")
    try:
        c = _bridge(state_dir, ensure=False)
        j = c.show(jid)
    except Exception as exc:
        return tool_error(f"查不到 {jid[:24]}：{type(exc).__name__}: {str(exc)[:160]}")
    pct = (100.0 * (j.get("bytesReceived") or 0) / j["totalBytes"]
           if j.get("totalBytes") else None)
    return {"content": [_text(f"{j.get('state')} "
                              f"{'' if pct is None else f'{pct:.1f}% '} "
                              f"{j.get('bytesReceived')}/{j.get('totalBytes')} 字节 "
                              f"{j.get('speedBps') or 0}B/s")],
            "structuredContent": j, "isError": False}


def call_wait(args, state_dir) -> dict:
    ids = args.get("jobIds") or []
    if not isinstance(ids, list) or not ids:
        return tool_error("jobIds 必须是非空字符串数组")
    timeout = float(args.get("timeout_sec") or 120.0)
    if timeout <= 0 or timeout > 600:
        return tool_error("timeout_sec 必须落在 (0, 600]")
    try:
        c = _bridge(state_dir)
    except BridgeUnavailable as exc:
        return tool_error(f"本机桥不可用：{exc}")
    try:
        r = c.wait([str(i) for i in ids], timeout=timeout)
    except Exception as exc:
        return tool_error(f"等待出错：{type(exc).__name__}: {str(exc)[:160]}")
    states = {j["jobId"]: j.get("state") for j in r["jobs"]}
    if not r["ok"]:
        return tool_error(f"超时未到终态：{states}（{COLD_START_TRUTH}）")
    bad = [k for k, v in states.items() if v != "COMPLETE"]
    return {"content": [_text(f"全部到终态：{states}")],
            "structuredContent": {"jobs": r["jobs"], "ok": not bad},
            "isError": bool(bad)}


def call_control(name: str, args, state_dir) -> dict:
    jid = args.get("jobId")
    if not isinstance(jid, str) or not jid:
        return tool_error("jobId 必须是字符串")
    verb = name.rsplit("_", 1)[-1]                    # pause / resume / cancel
    try:
        c = _bridge(state_dir, ensure=False)
        j = getattr(c, verb)(jid)
    except Exception as exc:
        return tool_error(f"{verb} 失败：{type(exc).__name__}: {str(exc)[:160]}")
    job = j.get("job") or j
    return {"content": [_text(f"{verb} 已发出，当前状态 {job.get('state')}"
                              f"（{'整单重头下' if job.get('restarted') else '续传'}）")],
            "structuredContent": job, "isError": False}


def tools_call_result(msg: dict, modern: bool, session: Session) -> dict:
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        session.note_error(INVALID_PARAMS)
        raise _ProtocolErr(INVALID_PARAMS, "Invalid params", "params 必须是对象")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        session.note_error(INVALID_PARAMS)
        raise _ProtocolErr(INVALID_PARAMS, "Invalid params", "缺 tools/call 的 name")
    known = {t["name"] for t in tool_defs()}
    if name not in known:
        # 规范 §存档 2.9：找不到工具属"exceptional condition"→ 协议级错误
        session.note_error(METHOD_NOT_FOUND)
        raise _ProtocolErr(METHOD_NOT_FOUND, "Method not found", f"未知工具：{name}")
    res = call_tool(name, params.get("arguments") or {})
    if modern:
        res.setdefault("resultType", "complete")
    return res


class _ProtocolErr(Exception):
    def __init__(self, code: int, message: str, detail: str = ""):
        super().__init__(detail or message)
        self.code, self.message, self.detail = code, message, detail


# ==========================================================================
# 主循环
# ==========================================================================
def write_line(obj: dict) -> int:
    """唯一出站口：单行进 stdout 的**二进制**通道。

    为什么强调"二进制"：Windows 文本模式会把行尾 `\\n` 写成 `\\r\\n`（M6 就是抓这个）。

    ⚠️ 这里**不做字节截断**：截断 JSON 等于吐出一条非法消息，比不发更坏。
       限额由 pack_response 在语义层做（削列表、截 text、标 truncated）；
       超限时写 stderr 留痕并照发 —— 因为到这一步的数据已经装好了，
       真装不下的那条路径（畸形 single object）也在 pack_response 里兜住了。
    """
    data = _dumps(obj)
    if len(data) > MAX_LINE_BYTES:
        log(f"OVER-BUDGET line={len(data)}B（pack_response 未兜住，属实现缺陷）")
    out = getattr(sys.stdout, "buffer", None) or sys.stdout
    out.write(data + b"\n")
    out.flush()
    return len(data)


def log(msg: str) -> None:
    """日志只走 stderr（M7）。"""
    try:
        sys.stderr.write(f"[mcp] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def dispatch(raw_line: bytes, session: Session) -> dict | None:
    if not raw_line.strip():
        return None
    try:
        msg = json.loads(raw_line.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        session.note_error(PARSE_ERROR)
        return error(None, PARSE_ERROR, "Parse error", str(exc)[:160])
    try:
        rep = handle(msg, session)
        return pack_response(rep) if rep is not None else None
    except _ProtocolErr as e:
        session.note_error(e.code)
        mid = msg.get("id") if isinstance(msg, dict) else None
        return error(mid, e.code, e.message, e.detail)
    except NotImplementedError as e:
        session.note_error(METHOD_NOT_FOUND)
        return error(msg.get("id"), METHOD_NOT_FOUND, "Method not found", str(e)[:160])
    except Exception as e:
        session.note_error(INTERNAL_ERROR)
        log(f"internal: {type(e).__name__}: {e}")
        return error(msg.get("id"), INTERNAL_ERROR, "Internal error",
                     f"{type(e).__name__}: {str(e)[:160]}")


def serve(stdin=None, stdout_check=None) -> Session:
    session = Session()
    stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
    for raw in stream:
        rep = dispatch(raw if isinstance(raw, bytes) else str(raw).encode("utf-8"),
                       session)
        if rep is not None:
            write_line(rep)
    return session


def main(argv=None) -> int:
    """chrome-dl-mcp 的入口：跑起来就是一条 stdio 服务。

    用法：chrome-dl-mcp（客户端把它当子进程起，不需要任何参数）。
    `--print-tools` 是给人/测试看的旁路：它往 **stdout** 打工具表（JSON），
    因为此时没有 MCP 会话在跑，不违反"stdout 只放 MCP 消息"。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--print-tools" in argv:
        sys.stdout.buffer.write(_dumps({"tools": tool_defs()}) + b"\n")
        return 0
    if "--version" in argv:
        sys.stdout.buffer.write(_dumps({"name": SERVER_INFO["name"],
                                        "version": SERVER_INFO["version"],
                                        "supportedVersions": SUPPORTED_VERSIONS}) + b"\n")
        return 0
    if "--default-state-dir" in argv:
        sys.stdout.buffer.write(_dumps({"stateDir": paths.default_state_dir()}) + b"\n")
        return 0
    try:
        serve()
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
