# Chrome 下载插件 · v0.2（装了就能用）

一句话：**让 Chrome 去下载，让程序当指挥官。**
程序只会喊"下这个、下到哪了、停"，一个字节都不经它的手——所以你在浏览器里装的那个加速器一定能吃到流量。

---

## 一、这次改了什么（一句话）

以前装完插件还要在电脑上再装个"传话筒"：**复制 32 位插件 ID、往 Windows 注册表里登记、让那个传话筒一直开着**，三步少一步就失联。
现在插件自己就是会主动打电话的那个人：它在电脑内部的 **18888 号门牌**（端口）上敲门。
**你要做的只有一件事：把插件装进 Chrome。**

技术侧一句：控制通道从系统注册的宿主进程换成**本机回环 WebSocket**；下载字节仍然 100% 由
`chrome.downloads` 走浏览器内核发起，这条硬约束一点没动。

---

## 二、要准备的东西（都是一次性的）

| 东西 | 要什么 | 怎么查 |
|---|---|---|
| Chrome | **≥ 116**（本机实测 154） | 地址栏 `chrome://version` |
| Python | 3.11 以上 | `python --version` |
| aiohttp | **可选**，不装也能跑 | `python -m pip install aiohttp==3.14.2` |

不确定缺什么？**先跑体检**（只看本机，不联网、不下载）：

```
python check_env.py
```

它会一行一行告诉你哪一项缺、下一步该敲什么。
其中有一项专门看**你把 Chrome 的下载目录改过没有**——改过就必须告诉指挥官，
不然它找不到文件、算不了校验（会报"没有文件"，其实文件在）。

> `aiohttp` 不在时，指挥官自动换用零依赖实现（标准库手写 RFC6455）。
> 两条路共用同一套账本与协议代码，功能一致；`--json` 体检与 `health` 里会写清用的哪条（字段 `transport`）。

---

## 三、正式安装：**一步**

1. 打开 Chrome，地址栏输入 `chrome://extensions` 回车
2. 右上角打开「**开发者模式**」开关
3. 点「**加载已解压的扩展程序**」，选中本目录里的 **`extension`** 文件夹

**完。** 不用复制 ID、不用改注册表、不用双击什么常驻、不用去设置页手动授权。

（要上架 Chrome 商店之后同理：点"添加到 Chrome"就完事。）

然后起指挥官：

```
python bridge.py
```

出现一行 `{"event": "bridge_ready", ...}` 就说明它在听了。窗口最小化即可，**不用常驻**——
它什么时候在，插件自己找得到；它不在的时候插件安静等，不刷屏。

装完了？再跑一次 `python check_env.py`。

---

## 四、怎么用

### 人用法（命令行）

```
python client.py add https://example.com/big.csv      （提交一个下载）
python client.py progress                              （看所有任务进度和网速）
python client.py watch                                 （实时刷屏跟进度，Ctrl-C 停）
python client.py pause  <任务号>                        （暂停）
python client.py resume <任务号>                        （继续）
python client.py cancel <任务号>                        （取消并删掉半成品）
python client.py batch  --file jobs.json               （批量提交）
python client.py health                                （体检：插件连没连上、用的哪条通道）
```

文件落在**你 Chrome 里设的下载目录**下的 `chrome-dl/` 子目录（默认 `C:\Users\你\Downloads\chrome-dl\`；
你在 Chrome 设置里改过下载目录，落点就跟着变）。提交时打印一个 **16 位任务号**，后面所有操作都用它。

### 机器人用法（Python 里调用，零第三方依赖）

```python
from client import Client

c = Client()                              # 自动读本机 endpoint.json，不用填端口密码

ids = c.download(["https://example.com/a.csv", "https://example.com/b.csv"])
print(ids)                                # 任务号列表

print(c.get_progress())                   # 全部进度；给任务号就只看那一个
c.pause(ids[0]); c.resume(ids[0])
r = c.wait(ids)                           # 等一批下完
for j in r["jobs"]:
    print(j["state"], j["bytesReceived"], j["speedBps"])

print(c.link_status())                    # 插件连没连上 / 门牌 / 配对与 bridgeId
```

边下边看（SSE 流，标准库就能读）：

```python
c.watch(lambda e: print(e.get("jobId"), e.get("state"),
                        e.get("bytesReceived"), e.get("speedBps")), timeout=120)
```

---

## 五、"多久能连上"——三档实话（别当 bug 报）

| 档 | 情形 | 实测承诺 |
|---|---|---|
| A | 插件这会儿**还醒着**（正在下载 / 刚被事件唤起 / WS 还开着） | **几秒**（1/2/4/8/16 秒退避重连，走进程内定时器） |
| B | 你点一下扩展图标 | **≤1 秒**（popup 打开就立刻重连一次） |
| C | 插件**睡着了**（好久没用） | **约 30 秒起**，Chrome 可能给得更久 |

C 档为什么不能更快：**Chrome 官方规定扩展的定时器最快 30 秒醒一次**
（`delayInMinutes` / `periodInMinutes` / `when` 三者都不能更早，官方原话"at most once every 30 seconds"）。
这是平台限制，我们绕不开，也不假装绕得开。所以对外承诺一律写"**最长约半分钟**"，**不写"秒级"**。

> 开发者模式加载的未打包版没有这个限制——开发时看到的秒级**不代表**商店版表现。

---

## 六、它到底"多"在哪（说人话，别误会）

- ✅ **多个文件一起下**：默认同时 4 个，同一个网站最多 2 个，谁先下完谁补位。
- ❌ **不是把一个文件切成 8 块同时下**。Chrome 的规矩是"一个文件一条线"，插件插不手。
- **断点续传**：中途断网/断电，恢复后接着下，不重头来。前提是服务器支持；服务器不支持时会重头下，
  日志里老实写「重头下」（`restarted`），不会骗你说"续传成功"。

---

## 七、不放心？自己验一遍

```
python test_downloader.py            （全套回归，真跑，约 5-10 分钟）
node tests/test_extension_logic.js   （扩展决策逻辑，**假 chrome API**）
python check_env.py --json           （预检取证）
```

测试里最关键的几条：

- **V4-b** —— 指挥官一旦想自己连外网，**当场被掐死并记日志**（运行时出站熔断）。
- **V4-c / W17** —— 指挥官和浏览器之间只传"纸条"：**每条不超过 8192 字节**，
  超了直接断链；分片、二进制帧同样拒（防止把一张纸条撕成两半绕过这道闸）。
- **W13** —— 双向对暗号：不知道密钥的程序冒充指挥官，扩展会识破并断开；
  反过来冒充扩展的程序，指挥官也会当场识破（`EXT_HMAC_MISMATCH`）。
- **V5 / V6** —— 断点续传是真的（服务器确实收到 `Range` 并回 `206`）；禁 Range 时如实标"重头下"。
- **V8′** —— **下载进行中**杀掉指挥官，重启后字节对齐、不重下、不留幽灵任务。

⚠️ **哪些是"跑不了真机所以没证"的**（不许拿上面的绿勾当这些的凭据）：
`W4 20 秒保活是否真让 SW 活过 5 分钟`、`W4b 打包版 alarm 实钳制`、`W5 扩展 console 噪音条数`、
`WS-T0.5 Chrome 154 的"本地网络"门禁落在哪一档`、`W8 popup 点一下是否 ≤1 秒连上`、
`W18/W19 popup 文案是否按预期出现` —— 这些**必须有人在真 Chrome 里手动加载扩展跑一次**，
结果落 `tests/ws_probe_report.md`；在那之前它们是 **UNVERIFIED**，本 README 不替它们认账。

---

## 八、遇到问题怎么办

| 现象 | 意思 | 这么办 |
|---|---|---|
| `找不到 ...\endpoint.json` | 指挥官没在跑 | `python bridge.py` |
| `401 BAD_TOKEN` | 口令不对 | 别自己填 token，让它自动读 `endpoint.json`；实在不行删掉 `%LOCALAPPDATA%\chrome-dl-bridge\endpoint.json` 重启 |
| 任务 `FAILED: NO_EXTENSION_CONNECTED` | Chrome 没开 / 插件没装 / 插件睡了还没醒 | 开 Chrome；等约 30 秒；点一下扩展图标可立刻加速 |
| 插件图标上有红色「!」 | **需要你看一眼**：对暗号失败 / 门牌上站着不认识的程序 | 点开图标，按它给的按钮处置（"重新认一下" 或 "拉黑并断开"） |
| `18888 这个门牌被别的软件占了` | 端口冲突 | `python bridge.py --port 18889`（插件那边点图标里的"换个端口"） |
| 进度数字隔一会儿才动 | 插件在打盹（最长约 30 秒） | 正常现象；文件仍在下，别重复提交 |
| 说"文件不存在"但其实下完了 | 你改过 Chrome 下载目录 | `python bridge.py --download-root "你的目录"` |

审计文件都在这儿，出问题发这几张给我看就行：

```
%LOCALAPPDATA%\chrome-dl-bridge\bridge_audit.jsonl      ← 指挥官干了什么
%LOCALAPPDATA%\chrome-dl-bridge\ws_trace.jsonl          ← 每张纸条的内容
%LOCALAPPDATA%\chrome-dl-bridge\pairing_audit.jsonl     ← 对暗号的成败记录（安全事件看这张）
%LOCALAPPDATA%\chrome-dl-bridge\egress_blocked.jsonl    ← 有没有被熔断拦下
%LOCALAPPDATA%\chrome-dl-bridge\jobs.json               ← 任务账本
%LOCALAPPDATA%\chrome-dl-bridge\handshake.key           ← 配对密钥（权限 600，别发给别人）
```

想彻底卸载：在 Chrome 里删掉扩展，再删 `%LOCALAPPDATA%\chrome-dl-bridge` 整个目录。**没有注册表项要清**——这一层已经不存在了（见 `NM_REMOVAL_NOTE.md`）。

---

## 九、安全边界（如实说，不粉饰）

**只监听 `127.0.0.1`**，别的电脑连不上。文件字节 100% 由 Chrome 网络栈发起：
指挥官、插件、Agent 三方**都不许当文件字节的传输通道**（回环 HTTP/SSE 与元数据请求是允许的，
这条边界要说准：管的是"字节不许过通道"，不是"程序完全不许联网"）。

**每条控制纸条 ≤8192 字节，服务端显式配置**（不是吃库的默认值——aiohttp 的默认是 4 MiB，
吃默认就等于把这条判据写废）；超限一律断链 1009，不分片、不收二进制帧。

**这一版唯一的实质性安全退让**（原来靠 Windows 注册表帮我们认人，现在没了）：

改成"**第一次连上就算认识（对暗号），以后每次都对暗号**"。所以：

- 万一有个坏程序**抢在**真指挥官之前占住 18888 门牌，它会被当成"第一个认识的"——**这个防不住**，
  这是"零安装步骤"换来的固有代价（任何没有系统级来源绑定的架构都躲不过）。
- 同用户的恶意程序也可以读走密钥文件（`chmod 600` 只防别的登录用户，不防你自己账号下的程序）。
- 坏程序能做到的**上限**：让浏览器往**你 Chrome 当前默认下载目录**下的 `chrome-dl/` 里写文件，
  文件名可以指定，同名文件会被**覆盖**（`conflictAction: overwrite`）。
  它**读不了**你的其他文件、**执行不了**代码、**传不出**数据（指令白名单 + 出站熔断 + 纸条审计三重）。
- 我们能做的：**门口换人了就告诉你**（图标红「!」+ 文案），你可以一键"**断开并忘记此程序**"重新配对；
  指挥官的"身份证号"（bridgeId）被插件钉扎，变了就告警；所有对暗号的成败单独记一张账。

**要不要接受这个代价，由你拍板。** 不接受的话，可退回"首次一次配对（你在 popup 点一次允许）"，
代价是不再"零步骤"。

---

## 十、给"老奶奶"的三句话

1. 以前装完插件还要在电脑上再装个传话筒（复制 ID、改注册表、让它常驻）；
   **现在装完插件就完事**，什么都不用再做。
2. 插件会自己在电脑内部的 18888 号小门上敲门找你的机器人。插件醒着 → **几秒钟**接上；
   插件睡了 → **至少半分钟**（Chrome 官方定的，绕不开），之后就一直连着。想快，点一下插件图标。
3. 下东西**从头到尾都是浏览器在干**，插件和机器人之间只传"网址、编号、下了多少"这种小纸条——
   所以你的加速器一定吃得到流量；这条规矩一点没变。

---

## 十一、v0.3：装了包之后的用法（三步，命令都是真跑过的）

**先装包**（这一步**不**标 runnable —— 它是环境改动，跑一次就动一次 base，
计划 §8.6 第 1 条把"不该真跑的行"明确排除在外）：

```bash
python -m pip install --no-build-isolation .
```

下面三块标了 `# runnable`，会被 `workflow/run_readme_examples.py` 抽取真跑。
占位符 `<URL>` / `<jobId>` / `<STATE_DIR>` / `<PORT>` / `<KNOWN_HEX>` 由抽取器填
（`<KNOWN_HEX>` 用 `mock_server.expected_sha256()` 现算，不手写）。

### 1) 看路径、看门牌、看扩展在哪

```bash
# runnable
# exit: 0
chrome-dl env --json
```

```bash
# runnable
# exit: 0
chrome-dl extension-path --json
```

### 2) 下一次真下载，并验完整性（三条真下载级命令，T7-a3）

```bash
# runnable
# exit: 0
chrome-dl download "<URL>" --filename readme-demo.bin --sha256 <KNOWN_HEX> --wait --timeout 180 --state-dir <STATE_DIR> --port <PORT>
```

```bash
# runnable
# exit: 0
chrome-dl list --state-dir <STATE_DIR> --port <PORT>
```

```bash
# runnable
# exit: 0
chrome-dl wait <jobId> --timeout 120 --state-dir <STATE_DIR> --port <PORT>
```

```bash
# runnable
# exit: 0
chrome-dl show <jobId> --state-dir <STATE_DIR> --port <PORT>
```

第四条把完整性说清楚：`show` 打出来的账本里 `sha256Actual` 必须是 `OK`。
**为什么第一步要带 `--sha256`**：Bridge 的校验分支是"没给期望哈希就返回 `SKIP`"，
不带 `--sha256` 的下载只证明"没崩"，证明不了"文件是那个文件"。

### 3) 后台桥与自动拉起

```bash
# runnable
# exit: 0
chrome-dl daemon status --state-dir <STATE_DIR>
```

```bash
# runnable
# exit: 2
chrome-dl bogus-verb --state-dir <STATE_DIR>
```

第二条是**故意**敲错的：`exit: 2` 明写预期非 0，抽取器会去核对它真的非 0、
真的给人话、真的没甩 traceback。报错面也要被跑过，不然没人知道它长什么样。

### 关掉自动拉起 / 不想常驻

```bash
# runnable
# exit: 0
chrome-dl --no-auto-spawn env --state-dir <STATE_DIR>
```

环境变量 `CHROME_DL_NO_AUTOSPAWN=1` 也能关；`chrome-dl daemon stop` 只结束
**本项目自己归因得到的桥**，归因不过的进程一律不动。

### MCP 客户端配置

Claude Code / Antigravity / 任何 stdio MCP 客户端的接法见 `docs/mcp-setup.md`。
