# chrome-downloader 🚀

> **把下载交给 Chrome 浏览器做，让程序与 AI Agent 当指挥官。**  
> 100% 流量走浏览器网络栈（自动继承 Cookie、登录态、系统代理与加速器），免人工手动开服，支持命令行 CLI 与标准 MCP 协议。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Chrome Extension](https://img.shields.io/badge/Chrome-Manifest_V3-green.svg)](https://developer.chrome.com/docs/extensions/mv3/intro/)
[![Protocol](https://img.shields.io/badge/MCP-2026--07--28%20%7C%202025--11--25-purple.svg)](https://modelcontextprotocol.io/)

---

## ✨ 核心特性

- **🌐 100% 走 Chrome 原生内核**：所有文件字节完全由 `chrome.downloads` 发起。自动带入浏览器里的登录 Cookie、网页会话、专属网络代理与下载加速器。
- **⚡ 零配置、免手动开服**：不需要在后台常驻服务。命令行工具或 AI Agent 发起请求时，**后台守护进程 1 秒内自动静默拉起**，无需使用者人工管理。
- **🤖 专为 AI Agent 打造**：原生支持 Model Context Protocol (MCP) 双时代协议，Claude Code、Cursor、Paseo 等大模型一句话即可调度下载。
- **🛡️ 纯本地回环与零外网偷跑**：Python 桥接服务仅负责收发调度指令，绝不经过本机网卡直接下载文件；仅监听本地 `127.0.0.1` 端口，结合 TOFU 握手密钥保护。
- **📦 零强制外部依赖**：Python 端核心库完全基于标准库实现（内置 RFC 6455 WebSocket 回退），无需繁琐装包，开箱即用。

---

## 🛠️ 两步快速开始

### 1. 安装 Chrome 扩展插件
- **方式 A（Chrome 应用商店）**：直接在 Chrome 网上应用店搜索 `chrome-dl-bridge`，点击 **「添加到 Chrome」**。
- **方式 B（开发者模式本地加载）**：
  1. 浏览器打开 `chrome://extensions/` 并开启右上角 **「开发者模式」**。
  2. 点击 **「加载已解压的扩展程序」**，选中本项目源码中的 `src/chrome_downloader/extension` 目录即可。

### 2. 安装 Python 控制端
在你的终端执行：
```bash
pip install chrome-downloader
📖 使用指南
-------

### 1\. 终端命令行使用（CLI）

**完全不需要提前启动后台！** 直接敲命令，后台服务自动随需启动：

```bash
# 提交下载并等待完成（自动打印落盘路径与 SHA256 校验和）
chrome-dl download "https://example.com/data.zip" --wait

# 查看所有正在进行的任务与实时下载网速
chrome-dl progress

# 任务控制
chrome-dl pause <任务号>     # 暂停
chrome-dl resume <任务号>    # 继续
chrome-dl cancel <任务号>    # 取消并清理未完成文件

# 查看服务与插件连接健康状态
chrome-dl health
```

> **文件保存在哪？**  
> 默认保存在你 Chrome 浏览器配置的下载目录下的 `chrome-dl/` 文件夹中。

* * *

### 2\. AI Agent 智能体调用（MCP 协议接入）

本项目原生支持 **Model Context Protocol (MCP)**。只需在你的 AI 客户端配置中添加：

#### 接入 Claude Desktop / Claude Code

在 `claude_desktop_config.json` 中配置：

```json
{
  "mcpServers": {
    "chrome-downloader": {
      "command": "chrome-dl-mcp"
    }
  }
}
```

#### 接入 Cursor / Paseo / 其它 Agent

直接配置命令为 `chrome-dl-mcp` 即可。

**接入后效果**：  
你只需对 AI 说：

> _“帮我用 Chrome 插件把这个链接的数据集下载下来并告诉我保存路径。”_

AI 将自动调用 `download_file` 原生工具完成调度，全程无需人工干预！

* * *

### 3\. Python 脚本中调用（SDK）

如果你要在自己的 Python 脚本或自动化流水线中使用：

```python
from chrome_downloader.client import Client

# 自动定位本地端点并连接（免配端口与密钥）
client = Client()

# 提交下载任务
job_ids = client.download(["https://example.com/sample.csv"])
print("任务已提交:", job_ids)

# 阻塞等待下载完成
result = client.wait(job_ids)
print("下载完成:", result)
```

* * *

🔒 安全与设计哲学
----------

1. **流量零越界（Zero-Egress Guard）**： 桥接程序和控制端在架构级别被限制为指令通道，数据帧限制在 8192 字节以内，从物理上阻止通过 Python 脚本直连外网下载文件，确保流量合规。
2. **信任初建（TOFU 机制）**： 扩展程序首次与本地桥连接时生成 32 字节高强度配对密钥，拒绝外部未授权来源劫持控制。
3. **资源友好**： 扩展基于 Chrome Manifest V3 标准开发，空闲时自动进入休眠状态，不占用系统 CPU 与多余内存。

* * *

📄 开源许可证
--------

本项目基于 [MIT License](LICENSE) 开源发布，欢迎自由使用、集成与贡献代码。
