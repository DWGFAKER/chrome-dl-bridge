# 真机探针操作单（WS-T0 / T0.5 / T0e / W4b / W5 / W8 / W18 / W19 / Z1）

**为什么要有这份文件**：v0.2 剩下的判据全部要求"真实 Chrome + 人手动加载扩展"。
本环境的自动化跑不了浏览器（也不许装 Playwright/Selenium，违反 C2），
所以这些项**一律标 UNVERIFIED**，不许拿推断或假 chrome 桩的结果冒充实测（铁律 5 / 001-F8）。

下面每一步做完，把观察到的原文写进 `tests/ws_probe_report.md`（同一目录，照下面的表填），
入口再把结论落进 claims 与审计。**测不到就写"测不到"，不要填空。**

预计耗时：一次会话约 25 分钟。建议一次做完，Chrome 版本中途别升级。

---

## 0. 先记下现场（30 秒）

```
chrome://version                     ← 抄 ProductVersion（本机应约 154/155）
python -c "import platform;print(platform.release())"
python check_env.py --json           ← 存一份，作为"开工前状态"
```

---

## 1. 起服务端（不要常驻，测的就是"随用随启"）

```
python bridge.py --port 18888
```

看到 `{"event": "bridge_ready", ...}` 就行。**窗口保持开着但不用管**。

---

## 2. WS-T0：装了就能连上（无 host_permissions）

1. Chrome 地址栏 `chrome://extensions` → 开「开发者模式」
2. 「加载已解压的扩展程序」→ 选本目录的 `extension` 文件夹
3. **只此一步，不做别的事**（不复制 ID、不改注册表、不去任何设置页授权）
4. 点开扩展图标 → 看「接上了（内部门牌 18888）」还是别的
5. 打开该扩展的 Service Worker 控制台（点图标卡片上的 "Service Worker" 链接），
   把 **Console 里的第一行报错原文** 抄下来（如果有）

记录（`ws_probe_report.md` §1）：
- 站点设置页里本扩展的「本地网络访问 / Local network access」一项显示什么：**Allow / Ask / Block**，
  以及它在**本机 Chrome 上的实际名称**（用来回填或证伪 F5c：Chrome 145 是否把它拆成
  `local-network` 与 `loopback-network` 两项。F5c 目前 **UNVERIFIED**，不要写成事实）
- 有没有出现「允许访问本地网络」的弹窗？（有 = ②档；没有且连上了 = ①档）
- 连不上时，SW 控制台原文是什么？（重点看有没有 `address space` / `Local Network Access` 字样）

> **判据 WS-T0**：连上 = ①档，G1 成立；弹提示 = ②档 → **STOP-8 报人**；
> 连不上又没提示 = ③档 → 继续第 3 步，**必须再分 ③-a / ③-b**。

---

## 3. WS-T0.5 观测项 e：popup 能不能把授权提示"钓"出来

⚠️ 这一招是**我们自己的工程假设**，不是官方给的路（官方只说"worker 触发提示的能力尚在开发中"，
crbug 404887282）。所以它只有"实测证实 / 实测证伪"两种结果，**不许假设它成立就往下走**。

如果第 2 步落在 ③ 档（连不上且无提示）：
1. 关掉扩展图标弹窗，再**重新点开**它（让 popup 上下文活着）
2. 同时看一眼 Chrome 右上有没有冒出授权提示
3. 记下：提示出现了 = **③-a**；仍然没有 = **③-b**

记录：`③-a` / `③-b` / 不适用（第 2 步已落在 ① 或 ② 档）。

---

## 4. WS-T0 ③ + W4：20 秒 keepalive 是否真让 SW 活过 5 分钟

1. 在扩展的 SW 控制台里执行：`document.visibilityState`（确认你连的是 SW 而不是别的目标）
2. 提交一个慢任务让它保持活跃（另开终端）：
   ```
   python mock_server.py --port 18099          # 另开一个窗口
   python client.py --port 18888 --token <endpoint.json 里的 token> ^
       add "http://127.0.0.1:18099/file/probe?size=20000000&rate=200000" --name probe.bin
   ```
3. 让它跑 6 分钟。期间每隔 1 分钟在 `chrome://extensions` 看那张卡片：
   **"Service Worker (Inactive)" 里的 Inactive 有没有出现过**
   （出现过 = SW 被杀过，keepalive 没兜住 = **STOP-2**）
4. 同时在 SW 控制台看 `setInterval` 的 ping 有没有连续断档

记录：SW 被杀次数 / 第一次变 Inactive 的时刻 / 断档秒数。

---

## 5. W4b：alarm 钳制的反向对照（**这项的目的是堵死"用 when 提速"这条错路**）

⚠️ 必须用**打包安装**的扩展测，未打包（开发者模式）版官方明确"no limit"，测出来不算数。
没有打包版就在报告里写"未做，需打包版"，**不要拿开发版的秒级当结论**。

在 SW 控制台执行，观察下一次 `[tick]` 日志的实际间隔：

```js
chrome.alarms.clearAll();
chrome.alarms.create('probeA', {delayInMinutes: 0.01});      // 期望：被钳到 ≥30 秒 + 一条 warning
chrome.alarms.create('probeB', {when: Date.now() + 3000});    // 期望：同样 ≥30 秒才响
let t0 = performance.now();
chrome.alarms.onAlarm.addListener(a => console.log('ALARM', a.name, Math.round(performance.now()-t0)));
```

记录：`probeA` 与 `probeB` 各自的实际触发毫秒数、有没有 warning 文本。
**结论必须写成"SW 一旦被销毁，唤醒必然 ≥30 秒"**；如果谁比 30 秒早，说明本机 Chrome
行为与官方文档不符，把原文贴出来交 Reviewer。

---

## 6. W5：无服务时的安静程度

1. 关掉 `bridge.py` 窗口（模拟机器人不在）
2. 让 Chrome 保持开着 10 分钟，期间别管扩展
3. SW 控制台数一数：`[cdl]` 开头的行有几条？CPU 有没有可观测的增长？

判据：console 噪音 ≤3 条，无刷屏。**超出就把原文条数与样例贴上来。**

---

## 7. W8 / W18 / W19 / W20：popup 的三条文案与按钮

| 场景 | 怎么造 | 该看到什么（逐字） |
|---|---|---|
| 点图标加速 | 关 Bridge → 开 Bridge → 点扩展图标 | ≤1 秒内第一屏变成「接上了（内部门牌 18888）」；对端进程号有数字 |
| 密钥不一致 | 删 `%LOCALAPPDATA%\chrome-dl-bridge\handshake.key` 后重启 Bridge，重复点图标 3 次 | 「连不上——电脑上的那个程序好像换了一个（或者不是原来那个）。要重新认一下吗？」+ 〔重新认一下〕〔先别连〕 |
| 疑似劫持 | 先跑 `python tests/fake_bridge_impostor.py`（它会占 18888 并交出错的证明） | 「注意：18888 门牌上站着一个不认识的程序，我没让它进来。要不要把它拉黑？」+ 〔拉黑并断开〕〔让我看看详情〕 |
| 无关占用 | 先在 18888 上起个纯 HTTP 服务（比如 `python mock_server.py --port 18888`），再起 Bridge 让它漂到 18889 | 「18888 这个门牌被别的软件占了，换一个门牌就好。」+ 〔换个端口〕〔知道了〕 |
| W18 重配对 | 上面"疑似劫持"之后：点〔拉黑并断开〕→ 关掉冒充者 → 起真 Bridge → 点〔立即重连〕 | 图标显示新的 bridgeId 前 8 位；**全程不重装扩展、不改注册表** |

每条**截图**存 `tests/evidence/Z1/`（文件名带场景名与时间）。

---

## 8. Z1（承重）：零安装端到端实证

在**没跑过任何安装脚本、注册表无本项目键值、无常驻进程、没去过任何设置页授权**的状态下：

1. 只做一步：装扩展（第 2 步那三个点击）
2. 起 Bridge：`python bridge.py`
3. 提交一个真下载：`python client.py add <一个真实小文件 URL>`
4. 文件落盘且 sha256 一致

**同时必须证明**（截图或命令输出存证）：

```bash
python check_env.py --json --regcheck   # 注册表与进程两项都该是 true
python -c "import json;d=json.load(open(r'%LOCALAPPDATA%\chrome-dl-bridge\endpoint.json',encoding='utf-8'));print(d['transport'],d['wsPort'],d['paired'])"
```

`--regcheck` 会实际枚举 HKCU 下旧方案的宿主注册区，确认**没有本项目键值**；
`tasklist` 那一项同时确认没有本项目的常驻进程（v0.2 应当一个都没有）。
（旧方案的键名不在这张单子里直写：计划 §10.1 要求全仓 grep 包 .md 只能 0 命中，
唯一豁免面是 `NM_REMOVAL_NOTE.md`；把要查的键名交给 `check_env.py` 拼接执行。）
chrome://extensions 里本扩展的权限清单                           ← 只有三样，无"访问所有网站"类告警
该扩展的站点设置里 LNA/loopback 未被人工改动                     ← 专防 ②档蒙混过关
```

---

## 9. WS-T0e / WS-T19：API 语义与"请求确实由浏览器发出"

在 SW 控制台跑（下载进行中）：

```js
chrome.downloads.onChanged.addListener(d => console.log('DELTA', JSON.stringify(d)));
```

- 记录 delta 里**到底有没有** `bytesReceived`（001-F1 遗留；没有就写没有，别推断）
- `chrome.downloads.search({id:<n>})` 的返回全文抄一条
- 然后手动 `chrome.downloads.pause(id)` → `resume(id)`，看 resume 之后是否出现 206
  （去 `mock_server` 的访问日志里看 `status` 与 `Content-Range`）
- WS-T19：`chrome://net-export` 抓一段，证明这条请求由浏览器网络服务发出
  （导出文件路径与关键行号记下来；本机是 fake-IP/TUN 加速器，**按目标 IP 反查进程那条取证路已作废**，见 F9）

---

## 10. 跑完之后

把 `tests/ws_probe_report.md` 与截图交给入口，由入口逐条落 claims（带 `conclusion_level`）
并重跑 `rv_audit`。**这份单子里任何一项没做，对应判据在验收报告里就必须保持 UNVERIFIED**——
不允许写"应该没问题"，也不允许拿本仓自动化测试的绿勾替它背书。
