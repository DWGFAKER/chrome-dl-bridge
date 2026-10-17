# MCP 规范存档（STOP-12 / T5 硬产出）

- 课题：`PLAN-CHROME-DOWNLOADER-003` · 计划 V4 §3.3 / §8.3 / §8.7
- **抓取时间：2026-10-15T16:11:43Z ~ 16:13Z（UTC，本机实测）**
- 抓取方式：`curl -sS -L`（本机可达外网；HTTP 状态码逐条记录在下）
- 原始字节：全部落在本目录 `_spec_raw/`，SHA-256 见文末清单（执行员另跑 `sha256sum` 落盘）
- 用途：M3/M4 的判据凭据。**没有这份存档，M3/M4 的正确性只能靠信任**（审查员 §7-1 的原话），
  故计划把"先产出本文件"设为 T5 进 T6 的前置条件（STOP-12）。

> 本文件里凡标「原文」的句子都是**逐字摘录**（含 Markdown 强调符与代码块），
> 每句都给了 `_spec_raw/` 里的落盘文件名，任何人可以对着文件搜到同一串字。
> 不是原文、由我概括的句子一律标「（转述）」，两种不要混。

---

## 1 · 抓取清单（URL → 状态码 → 落盘文件）

| # | URL | HTTP | 落盘 | 大小 |
|---|---|---|---|---|
| 1 | `https://modelcontextprotocol.io/specification/versioning` | **308 → 200**（重定向后取到 HTML） | `_spec_raw/versioning.md` + `_spec_raw/h_versioning.txt`（响应头） | 292863 B |
| 2 | `https://modelcontextprotocol.io/docs/2026-07-28/learn/versioning.md` | **200** | `_spec_raw/versioning_doc.md` | 3222 B |
| 3 | `https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning.md` | **200** | `_spec_raw/specification_2026-07-28_basic_versioning.md` | 8410 B |
| 4 | `https://modelcontextprotocol.io/specification/2026-07-28/server/discover.md` | **200** | `_spec_raw/specification_2026-07-28_server_discover.md` | 3835 B |
| 5 | `https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio.md` | **200** | `_spec_raw/specification_2026-07-28_basic_transports_stdio.md` | 7352 B |
| 6 | `https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md` | **200** | `_spec_raw/specification_2026-07-28_server_tools.md` | 24314 B |
| 7 | `https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2026-07-28/schema.ts` | **200** | `_spec_raw/schema_2026-07-28.ts` | 98426 B |
| 8 | `…/schema/2026-07-28/examples/DiscoverResult/server-capabilities-discovery.json` | **200** | `_spec_raw/ex_server-capabilities-discovery.json` | 419 B |
| 9 | `…/schema/2026-07-28/examples/DiscoverRequest/server-discover-request.json` | **200** | `_spec_raw/ex_server-discover-request.json` | 349 B |
| 10 | `…/schema/2026-07-28/examples/UnsupportedProtocolVersionError/unsupported-version.json` | **200** | `_spec_raw/ex_unsupported-version.json` | 244 B |
| 11 | `https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2025-11-25/schema.ts` | **200** | `_spec_raw/schema_2025-11-25.ts` | 66671 B |
| 12 | `https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle.md` | **200** | `_spec_raw/legacy_lifecycle.md` | 9217 B |
| 13 | `https://modelcontextprotocol.io/specification/2025-11-25/basic/transports.md` | **200** | `_spec_raw/legacy_stdio.md` | 16191 B |
| — | `https://modelcontextprotocol.io/specification/2026-07-28/basic/utilities/caching.md` | **404** | `_spec_raw/specification_2026-07-28_basic_utilities_caching.md`（是 404 页，**不作任何引用**） | 682 B |
| — | `https://modelcontextprotocol.io/specification/2026-07-28/versioning.md` | **404** | `_spec_raw/versioning_spec.md`（同上，**不作引用**） | 689 B |

> 两条 404 一并留盘，不删。理由：我最初的取页路径猜错了两条，
> 留着比删掉诚实 —— 它记录了"哪些页**不存在**"，后来人不必再猜第三次。
> 引用只从 200 的那 13 份里取。

---

## 2 · 逐字引文（本实现的判据依据）

### 2.1 当前版本 = 2026-07-28

原文（`_spec_raw/versioning_doc.md`，"## Revisions" 段末）：

> The **current** protocol version is [**2026-07-28**](/specification/2026-07-28/).

原文（`_spec_raw/schema_2026-07-28.ts:30`）：

> export const LATEST_PROTOCOL_VERSION = "2026-07-28";

### 2.2 每请求声明版本、服务端逐请求接受或拒绝

原文（`_spec_raw/versioning_doc.md`，"## Negotiation"）：

> Every request declares the protocol version it is using via the
> `io.modelcontextprotocol/protocolVersion` key in its
> [`_meta`](/specification/2026-07-28/basic/index#meta) field, and the server accepts or
> rejects each request independently.

原文（`_spec_raw/schema_2026-07-28.ts`，`RequestMetaObject`）：

>   "io.modelcontextprotocol/protocolVersion": string;

（同文件注释里写明 "The MCP Protocol Version being used for this request. **Required**."）

### 2.3 不支持的版本 → `UnsupportedProtocolVersionError`（码 -32022）

原文（`_spec_raw/versioning_doc.md`）：

> If the server does not support the requested version, it responds with an
> [`UnsupportedProtocolVersionError`](/specification/2026-07-28/basic/versioning#protocol-version-negotiation)
> listing the versions it does support.

原文（`_spec_raw/specification_2026-07-28_basic_versioning.md`，"## Protocol Version Negotiation"，
紧跟的示例代码块，逐字）：

```json theme={null}
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32022,
    "message": "Unsupported protocol version",
    "data": {
      "supported": ["2026-07-28", "2025-11-25"],
      "requested": "1900-01-01"
    }
  }
}
```

原文（`_spec_raw/schema_2026-07-28.ts:450`）：

> export const UNSUPPORTED_PROTOCOL_VERSION = -32022;

→ **M3 的形状就照这段来**：`-32022` + `data.supported` + `data.requested`。
注意示例里 `supported` 正是 `["2026-07-28", "2025-11-25"]` —— 与本实现的
`supportedVersions` 取值完全一致（计划 X1 的修正点由规范自己印证）。

### 2.4 `server/discover` 是服务端必须实现的单次 RPC

原文（`_spec_raw/specification_2026-07-28_server_discover.md`，首段）：

> `server/discover` lets a client query a server's supported protocol versions,
> capabilities, and identity before sending any other requests. Servers **MUST**
> implement it.

原文（同页 "## When to Call"）：

> Calling `server/discover` is optional for clients — a client may invoke any
> RPC inline and handle
> [`UnsupportedProtocolVersionError`](/specification/2026-07-28/schema#unsupportedprotocolversionerror)
> if the server does not support the requested version.

（计划 §0.2 那句 "a **mandatory RPC** … Calling it is optional" 是**对这两段的转述**，
原文里 "mandatory" 一词没出现，出现的是 `Servers **MUST** implement it` + `optional for clients`。
此处把差别写明白，免得后来人拿转述当原文。）

原文（`_spec_raw/schema_2026-07-28.ts`，`DiscoverResult`）：

> export interface DiscoverResult extends CacheableResult {
>   supportedVersions: string[];
>   capabilities: ServerCapabilities;
>   instructions?: string;
> }

原文（`_spec_raw/schema_2026-07-28.ts`，`CacheableResult extends Result`）：

>   ttlMs: number;
>   cacheScope: "public" | "private";

→ **M1 的字段清单**：`resultType` + `supportedVersions` + `capabilities` +
`_meta.io.modelcontextprotocol/serverInfo` + `instructions` + `ttlMs` + `cacheScope`。
`serverInfo` 走 `_meta`（不是顶层字段），依据是 `_spec_raw/ex_server-capabilities-discovery.json`
与 discover 页的响应示例，两处一致。

### 2.5 `resultType` 必带

原文（`_spec_raw/schema_2026-07-28.ts`，`Result`）：

>   resultType: ResultType;

同段注释（原文）：

>   Servers implementing this protocol version MUST include this field.
>   For backward compatibility, when a client receives a result from a
>   server implementing an earlier protocol version (which does not include
>   `resultType`), the client MUST treat the absent field as `"complete"`.

（转述：**现代**响应必须带 `resultType`，**旧时代**响应不带 —— 这正是 §3.3 双时代表里
"旧时代 `tools/list`/`tools/call` 不带 `resultType`"的规范依据；缺省按 complete 处理是
客户端的义务，不是服务端可以省略的理由。）

### 2.6 legacy 时代钉在 2025-11-25（计划 X1 的修正依据）

原文（`_spec_raw/versioning_doc.md`，"## Negotiation" 末段）：

> For interoperability with servers and clients that implement the
> handshake-based protocol revisions (`2025-11-25` and earlier), see
> [Backward Compatibility](/specification/2026-07-28/basic/versioning#backward-compatibility-with-initialization-based-versions).

原文（`_spec_raw/specification_2026-07-28_basic_versioning.md`，"## Terminology"）：

> * **Modern**: protocol versions that convey version, identity, and
>   capabilities as per-request metadata (revision `2026-07-28` and later).
> * **Legacy**: protocol versions that establish a session with an
>   `initialize` handshake (`2025-11-25` and earlier).
> * **Dual-era**: an implementation that supports both modern and legacy
>   versions.

→ 计划 V1 写的 `2025-06-18` 是**错的**（X1）；本实现 legacy 钉 `2025-11-25`。
`_spec_raw/schema_2025-11-25.ts:12` 亦为原文佐证：

> export const LATEST_PROTOCOL_VERSION = "2025-11-25";

### 2.7 双时代服务端如何选择行为

原文（`_spec_raw/specification_2026-07-28_basic_versioning.md`，"### Compatibility Matrix" 之后）：

> A dual-era **server** selects its behavior from how the client opens:
>
> * A request carrying modern per-request `_meta` is served statelessly
>   according to this revision.
> * An `initialize` request selects legacy semantics, scoped to the stdio
>   process (stdio) or the session (HTTP), as specified by the negotiated
>   legacy protocol version.
>
> A dual-era server **MAY** serve both eras concurrently on the same endpoint
> or process.

→ §3.3 那张表（"收到 `server/discover`+现代 `_meta` → 现代；收到 `initialize` 且无现代
`_meta` → 旧"）**就是这段的实现**，不是我发明的启发式。

### 2.8 stdio 洁净（M6 / M7 / C7 的依据）

原文（`_spec_raw/specification_2026-07-28_basic_transports_stdio.md`，首段要点，逐字）：

> * Each message is a single JSON-RPC request, notification, or response.
> * Messages are delimited by newlines, and **MUST NOT** contain embedded newlines.
> * The server **MAY** write UTF-8 strings to `stderr` for any logging purposes
>   including informational, debug, and error messages.
> * The server **MUST NOT** write anything to its `stdout` that is not a valid MCP
>   message.

同页 "## Receiving Messages"（原文）：

> The server **MUST NOT** write JSON-RPC *requests* to `stdout`.

→ 落地三条硬规矩（§3.3）：只走 `sys.stdout.buffer.write(line + b"\n")`、禁 `print`
（Windows 会把 `\n` 变 `\r\n`，M6 断言"无 `\r`"就是拦这个）、`json.dumps(...,
ensure_ascii=False, separators=(",", ":"))` 保证单行内无换行。

旧时代同口径（`_spec_raw/legacy_stdio.md:30`，原文）：

> * Messages are delimited by newlines, and **MUST NOT** contain embedded newlines.

### 2.9 工具报错走 `isError`，不走协议级错误（M10 的依据）

原文（`_spec_raw/schema_2026-07-28.ts`，`CallToolResult.isError` 注释）：

>   Any errors that originate from the tool SHOULD be reported inside the result
>   object, with `isError` set to true, _not_ as an MCP protocol-level error
>   response. Otherwise, the LLM would not be able to see that an error occurred
>   and self-correct.

同段（原文）：

>   However, any errors in _finding_ the tool, an error indicating that the
>   server does not support tool calls, or any other exceptional conditions,
>   should be reported as an MCP error response.

### 2.10 错误码保留段（M12 白名单的依据）

原文（`_spec_raw/schema_2026-07-28.ts:312-316`）：

> export const PARSE_ERROR = -32700;
> export const INVALID_REQUEST = -32600;
> export const METHOD_NOT_FOUND = -32601;
> export const INVALID_PARAMS = -32602;
> export const INTERNAL_ERROR = -32603;

原文（同文件 `:434 / :442 / :450`）：

> export const HEADER_MISMATCH = -32020;
> export const MISSING_REQUIRED_CLIENT_CAPABILITY = -32021;
> export const UNSUPPORTED_PROTOCOL_VERSION = -32022;

（转述：本实现允许发出的码 = `{-32700, -32600, -32601, -32602, -32603, -32022}`；
`-32020/-32021` 归 HTTP/能力协商用，stdio 上不发；**不许**在 -32020..-32099 自造码。）

### 2.11 stdio 后向兼容探测（客户端义务，但决定了我的响应形状）

原文（`_spec_raw/specification_2026-07-28_basic_transports_stdio.md`，"## Backward Compatibility"）：

> * The server returns a `DiscoverResult`: the server is modern. Select a
>   mutually supported version from `supportedVersions` and continue.
> * The server returns a recognized modern JSON-RPC error such as
>   [`UnsupportedProtocolVersionError`][unsupported-version]: the server is
>   modern but does not support the requested version. Use one of the versions
>   in its advertised `supported` list. Do **not** fall back to `initialize`.
> * The server returns any other error, or does not respond within a
>   reasonable timeout: the server is legacy. Fall back to the `initialize`
>   handshake.

> The fallback **MUST NOT** be keyed to one specific error code: legacy servers
> respond to unknown pre-`initialize` requests with implementation-defined
> errors (commonly `-32601` or `-32602`) or not at all.

→ **M3 里"不回退 initialize"这句的规范根基**：现代错误是"我是现代服务器，只是不支持你
那个版本"，客户端据此换版本重试；服务端若此时假装旧时代，就等于把可诊断的失败变成不可诊断的。

### 2.12 旧时代 `initialize` 的响应形状（M4 的依据）

原文（`_spec_raw/schema_2025-11-25.ts`，`InitializeResult`）：

> export interface InitializeResult extends Result {
>   protocolVersion: string;
>   capabilities: ServerCapabilities;
>   serverInfo: Implementation;
>   instructions?: string;
> }

原文（`_spec_raw/legacy_lifecycle.md`，示例里的两行版本号）：

>     "protocolVersion": "2025-11-25",

→ M4 断言：回显 `protocolVersion`（钉 `2025-11-25`）+ `capabilities.tools` + `serverInfo`，
后续 `tools/list`/`tools/call` **不带** `resultType`（旧 Result 无此字段）。

### 2.13 Claude Code 的协商档位（M8 的真机前提）

本机 `claude --version` 实测 **2.1.299**（见计划 §0.1）。
**本节没有抓到 changelog 原文** —— 计划 §13 把它标为「⚠️ 待复检」，
我照实标 **UNVERIFIED**：`~2.1.292 起 stdio 默认协商 2026-07-28、可用
`MCP_PROTOCOL_NEGOTIATION=legacy` 回退` 这句话来自 V1 的检索记录，
本轮抓取清单（第 1 节的 13 条）里**没有** `code.claude.com/docs/en/changelog`。
⇒ M8（真接本机 Claude Code 跑一次真下载）属**人在场**的活，按计划 §8.7/STOP-7 处理，
我不拿它给协商行为作证。

---

## 3 · 实现映射（规范 → mcp_server.py 的哪一行）

| 判据 | 规范依据（上文小节） | 实现落点 |
|---|---|---|
| M1 | §2.1 §2.4 §2.5 | `discover_result()`：`supportedVersions=["2026-07-28","2025-11-25"]`、`capabilities.tools{listChanged:false}`、`_meta.serverInfo`、`instructions`、`ttlMs`、`cacheScope`、`resultType:"complete"` |
| M2 | §2.5 + §3.3 确定性排序 | `tools_list()` 按固定顺序发 8 个工具，两次逐字节相同 |
| M3 | §2.3 §2.11 | `handle()` 现代分支：版本不认识 → `-32022` + `supported`/`requested`，**不发 initialize 响应** |
| M4 | §2.6 §2.12 | `legacy_initialize()`：回显 `protocolVersion`（钉 2025-11-25）+ `capabilities.tools` + `serverInfo`；旧时代会话里 `result` 不含 `resultType` |
| M6/M7 | §2.8 | `_write_line()` 只走 `sys.stdout.buffer.write(... + b"\n")`；日志只进 stderr / daemon.log |
| M10 | §2.9 | 工具内错误 → `isError:true` 的 CallToolResult；`file://` 等坏参在提交前拒，且不产生任务 |
| M11 | §3.3 字节预算 + C6 第三段 | `_budget_pack()`：先算 `structuredContent`，余额给 `content[].text`；超限截断并附 `truncated/totalCount/omitted`，整行 ≤8192 B |
| M12 | §2.10 | `ALLOWED_ERROR_CODES` 常量 + 逐条触发一次 |
| M13 | R6 缓解（§3.3） | 8 个 description 全含"插件冷启动最长约 30 秒" |

---

## 4 · 原始字节 SHA-256（复算用）

```
（本节由执行员跑 `cd docs/_spec_raw && sha256sum *` 落盘，不手写）
```

见 `_spec_raw_hashes.txt`（同目录，由 `sha256sum *` 直接重定向生成）。
哈希由命令产出；手写哈希 = 本文件自己犯"计划批评过的那个病"（F4 的病根）。

---

## 5 · 诚实边界

1. 存档只证「规范原文是这些句子」，**不证**「我的实现符合规范」—— 后者由 M1–M13 的实测断言负责。
2. §2.13（Claude Code changelog）本轮未抓到原文，标 UNVERIFIED，不拿计划旧文冒充实证。
3. 抓取是**本机当天**的活页内容；规范若再演进（R11），本文件立即过期，
   T5 之后再改实现要先回来重抓、重算哈希。
