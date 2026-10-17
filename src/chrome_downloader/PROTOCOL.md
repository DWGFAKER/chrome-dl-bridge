# 协议（Bridge ⇄ Chrome 扩展）v0.2 · 回环 WebSocket

**只传"纸条"，不传字节。**

```
地址：ws://127.0.0.1:18888/link          （扩展主动连；与 Agent 的 HTTP 面同一端口）
帧：  WebSocket **文本帧**，载荷 = UTF-8 JSON
上限：单帧 ≤ 8192 字节 —— **服务端显式配置**，超限立即断链
```

## 0. 帧上限的强制条款（C7 / W17，不是建议）

1. aiohttp 路径：`WebSocketResponse(max_msg_size=8192)` —— **必须显式传参**。
   aiohttp 的默认值是 **4 MiB**（本机源码 `aiohttp/web_ws.py:117`），吃默认值等于把这条判据写废。
2. 零依赖降级路径：自写 reader 一见"声明长度 > 8192"就**关闭连接（1009 Message Too Big）**
   并记 `link_bad_frame`；不读正文、不放宽。
3. **分片不算过关**：把 9000 字节拆成两帧一样拒（`fin=false` 的首帧直接断）。
4. **二进制帧 = 有人想塞文件字节**，与超大帧同处置。
5. 扩展侧发送前自检：>8192 就**丢弃并 console 告警**，宁可丢这一张也不拆两张。
6. 禁止任何 `max_msg_size=0`（无限）或 >8192 的配置；`grep max_msg_size` 双向核。

超限时**唯一**处置是断链；绝不允许"文件字节走 WS"。

## 1. 握手：HMAC-SHA256 双向挑战应答（TOFU）

```
扩展 → Bridge  {type:"hello_challenge", clientNonce:<32hex>, [keyExchange:<64hex>],
                role, chromeVersion, extensionVersion, port}
Bridge → 扩展   {type:"hello_verify", nonce:<serverNonce>, bridgeId,
                hmac = HMAC(K, clientNonce ‖ "\n" ‖ serverNonce ‖ "\n" ‖ "bridge")}
扩展 → Bridge  {type:"hello_verify", hmac = HMAC(K, serverNonce ‖ "\n" ‖ clientNonce ‖ "\n" ‖ "extension")}
Bridge → 扩展  {type:"welcome", ok, bridgeId, token, maxConcurrent, perHost, maxFrame,
                port, peerPid, peerStartedAt}
```

- `keyExchange` **只在首次（TOFU）出现**：扩展本地没密钥时生成 32 字节随机 K 并交出，
  服务端存 `state_dir/handshake.key`（`chmod 600`）；此后两端各自复用同一把 K。
- 两个方向的口径字符串**不同且不可互换**（`bridge` / `extension` 两个上下文词 + 换行分隔）。
- 服务端一律用**常量时间比较**（`hmac.compare_digest`）；扩展侧同样逐字节比对。
- 任一方向验签不过 → **立即断开**，写 `pairing_audit.jsonl`：
  `EXT_HMAC_MISMATCH`（对面不是我们的扩展）/ `BRIDGE_UNPAIRED`（没配对又不交 K）/
  `NOT_HMAC_HELLO`（旧版无 HMAC 的报文，v0.2 一律拒）。
- 扩展侧发现服务端交不出正确证明 → 判"门牌上站着不认识的程序"，把该端口进黑名单并告警。
- **固有上限（如实登记）**：抢在真 Bridge 之前占住端口者会成为"首任可信方"。
  没有系统级来源绑定的架构下这一点**不能消除**，只能靠 `SO_EXCLUSIVEADDRUSE` + 固定端点 +
  bridgeId 钉扎告警 + 一键撤销来压缩窗口并暴露异常。

## 2. 字段白名单（任何一条消息，含嵌套键，都必须落在这里）

```
v type jobId downloadId url filename bytesReceived totalBytes speedBps
state error canResume restarted resumedFrom ts role token chromeVersion
extensionVersion ok reason conflictAction items sha256 maxConcurrent
perHost host maxFrame resume
port portScan clientNonce serverNonce nonce hmac bridgeId keyExchange
pairing peerPid peerStartedAt wsPath
```

Bridge 把每条纸条原样追加到 `%LOCALAPPDATA%\chrome-dl-bridge\ws_trace.jsonl`，
测试对着这张表逐条核字段（`audit_field_closure`）。出现未知字段 → 丢弃这一条并记
`link_unknown_field`（不断链，但审计留名）。

## 3. 保活与重连

| 机制 | 参数 | 依据 |
|---|---|---|
| WS keepalive ping | 每 **20 秒**（两端都可发，收方回 pong） | Chrome 116+ 官方示例；30 秒活动窗口内互发消息可延长 SW 寿命 |
| 进程内退避重连 | 1 / 2 / 4 / 8 / 16 秒 | **只在 SW 仍存活时有效**（这是"秒级"的唯一来源） |
| `chrome.alarms` 心跳 | `periodInMinutes: 0.5` = **30 秒** | 官方下限：`delayInMinutes` / `periodInMinutes` / `when` 三者都不能早于 30 秒 |
| 无服务时 | **静默**退避，不刷 console | 没装机器人属正常态 |
| 死链判定 | 服务端连续 3 个周期收不到动静 → 主动关（1002） | SW 被杀后半开连接不会自己关，不踢会一直"以为连着" |

对外承诺：**约 30 秒起**（空闲 + 商店版）；点扩展图标可加速到 ≤1 秒。**禁止任何秒级承诺。**

## 4. 消息类型

| 方向 | type | 作用 |
|---|---|---|
| 扩展 → Bridge | `hello_challenge` | 出题（首次附 `keyExchange`） |
| Bridge → 扩展 | `hello_verify` | 服务端答卷（含 `bridgeId`） |
| 扩展 → Bridge | `hello_verify` | 扩展答卷 |
| Bridge → 扩展 | `welcome` | 握手回执（并发上限、帧上限、门牌、对端 pid） |
| 双向 | `ping` / `pong` | 保活（F1） |
| Bridge → 扩展 | `start` | 下单：`{jobId,url,filename,conflictAction,sha256}`；重试时带 `resume`+`downloadId` |
| Bridge → 扩展 | `pause/resume/cancel` | 控制 |
| 扩展 → Bridge | `ack` | 已执行（带 `downloadId`） |
| 扩展 → Bridge | `progress` | 进度：`{jobId,downloadId,state,bytesReceived,totalBytes,speedBps,canResume,restarted,resumedFrom,error}` |
| 扩展 → Bridge | `snapshot` | 重连后对账（`items` 列表，>8192 时按任务条切多条，**永不拆字节**） |
| 双向 | `replaced` | 这条链路被新连接顶替 / 指挥官正在关停 |
| Bridge → 扩展 | `error` | 拒绝握手，带 `reason` |

## 5. 状态机

`PENDING → DOWNLOADING → COMPLETE | FAILED | CANCELLED`，旁路 `PAUSED`、`INTERRUPTED`、`STALE`。
Chrome 侧的 `in_progress / complete / interrupted / paused / cancelled` 由 Bridge 归一化。
上报 `complete` 但本地查无文件 → 判 `FAILED: FILE_MISSING_ON_COMPLETE`（谎报不当凭据）。

## 6. Agent ⇄ Bridge（HTTP + SSE，与插件通道同端口）

口令在 `%LOCALAPPDATA%\chrome-dl-bridge\endpoint.json`（权限 600），
用 `Authorization: Bearer <token>` 或 `?token=`。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/v1/health` | 体检（扩展是否在连、transport、配对与 bridgeId、熔断状态与拦截次数、任务汇总） |
| POST | `/v1/jobs` | 提交一个 `{url,filename?,sha256?}` |
| POST | `/v1/jobs/batch` | 批量 `{jobs:[...]}` |
| GET | `/v1/jobs` | 列表 |
| GET | `/v1/jobs/<id>` | 单个任务 |
| POST | `/v1/jobs/<id>/pause\|resume\|cancel` | 控制 |
| GET | `/v1/stats` | 汇总 |
| GET | `/v1/stream` | **SSE** 实时进度（标准库就能读，无需第三方） |
| GET | `/v1/trace` | 读自己的纸条审计（取证用） |

校验失败返回 **HTTP 400/401 + `{"error": ...}`**；任务自己的失败原因放在 `job.error`
（如 `NO_EXTENSION_CONNECTED`），两者不混淆。

> Agent 侧接口与 v0.1 **完全一致**（同端口、同路径、同 token）——这次改的只有插件那一侧的承载。
