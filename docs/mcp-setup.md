# MCP 客户端接法（三家配置形状）

服务端的凭据与规范原文在 `docs/mcp-spec-archive.md`（STOP-12 要求的硬产出）。
本文只讲"怎么把它接到客户端里"，不重述规范。

## 共同点

命令：`chrome-dl-mcp`（或 `python -m chrome_downloader.mcp_server`）
传输：**stdio**。服务端往 stdout 只写 JSON-RPC 单行消息，日志一律走 stderr。
支持的两个协议时代：`2026-07-28`（现代，无握手）与 `2025-11-25`（旧时代，`initialize` 握手）。

工具 8 个（顺序固定）：`chrome_download_status` / `chrome_download` / `chrome_download_list` /
`chrome_download_progress` / `chrome_download_wait` / `chrome_download_pause` /
`chrome_download_resume` / `chrome_download_cancel`。

⚠️ 每个工具的 description 都写着"插件冷启动最长约 30 秒"。那不是装饰：
Chrome 官方把扩展后台定时器下限钳在 30 秒，提交后没动静是正常现象，重复提交只会多下一份。

---

## 1 · Claude Code

```bash
claude mcp add chrome-downloader -- chrome-dl-mcp
```

核对（应当看到 `chrome-downloader` 处于 connected，并列出 8 个工具）：

```bash
claude mcp list
```

- 本机实测版本 `claude --version` → **2.1.299**（计划 §0.1 实测）。
- **未在本轮验证的**：2.1.292+ 的默认协商档位与 `MCP_PROTOCOL_NEGOTIATION=legacy` 回退，
  本轮**没有**抓到 changelog 原文（清单里没有那条 URL），标 **UNVERIFIED**。
  判据 M8（真接本机 Claude Code 跑一次真下载）属**人在场**的活，见本文末。

## 2 · Antigravity IDE

配置面是 `mcp_config.json`（与 Claude Desktop 同形状）。工具 → Model Context Protocol →
Configure servers，或直接编辑：

```json
{
  "mcpServers": {
    "chrome-downloader": {
      "command": "chrome-dl-mcp",
      "args": []
    }
  }
}
```

想用源码树跑（没 pip 安装）就把 command 换成 python：

```json
{
  "mcpServers": {
    "chrome-downloader": {
      "command": "python",
      "args": ["-m", "chrome_downloader.mcp_server"],
      "env": {"PYTHONPATH": "D:/article4/scratch/chrome_downloader_pkg/src",
              "CHROME_DL_STATE_DIR": "C:/Users/you/AppData/Local/chrome-dl-bridge"}
    }
  }
}
```

- **Antigravity 的配置页 URL 本轮未重抓**（计划 §13 标 ⚠️ 待复检）→ 上面这段按
  "Claude Desktop 同形状"给出，形状对不对**属 UNVERIFIED**，接之前自己核对一次。

## 3 · Cursor

```json
{
  "mcpServers": {
    "chrome-downloader": { "command": "chrome-dl-mcp", "args": [] }
  }
}
```

- **本机未装 Cursor**（`where cursor` 空）→ 本文这一段整体 **UNVERIFIED**：
  配置文件的确切路径与键名我没法在这台机器上核。计划 §8.7 早已把 Cursor 列为
  UNVERIFIED，本轮没有改变这一点，我不拿"常见形状"冒充实测。

---

## 状态目录怎么选

| 场景 | 怎么指定 | 说明 |
|---|---|---|
| 默认 | 不设 | `LOCALAPPDATA\chrome-dl-bridge`，与 v0.2 逐字符相同（C4 兼容） |
| 环境变量 | `CHROME_DL_STATE_DIR=<目录>` | 优先级：显式参数 > 环境变量 > 默认 |
| 命令行 | `chrome-dl --state-dir <目录> ...` | 最高 |

多用户机器上默认值本身就按登录用户分目录；`handshake.key` / `token` 落盘时尽力 chmod 600
（**诚实边界**：600 只防别的登录用户，不防同一个账号下的恶意程序，这是零安装架构的固有上限，
README 第九节原话，没粉饰）。

## 排障三步

1. `chrome-dl daemon status` → 看 `readyLevels` 的 L1/L2/L3 哪个是 false。
2. L3=false 且 `extensionConnected=false` → Chrome 里扩展没装/没醒/没配对：
   点一下扩展图标，等约 30 秒。
3. 客户端起不来服务端 → 看它捕获的 stderr（我们只往 stderr 写日志），
   或临时把 `chrome-dl-mcp` 换成 `python -m chrome_downloader.mcp_server` 手跑一遍。

## 必须由人做的两项（STOP-7 会拦发布）

- **M8**：真接本机 Claude Code 2.1.299 跑通一次真下载（半自动，人守）。
- **T9 真机验收**：真 Chrome 加载扩展 → 跑 `tests/ws_probe_readme.md` 的 T0/T0.5 →
  填报告。WS-T0/T0.5/T0e、W4/W4b/W5/W8/W18/W19、F5c 到现在仍是 **UNVERIFIED**，
  打包与自动拉起**不为其中任何一项背书**（§8.7）。
