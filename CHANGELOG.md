# CHANGELOG

## 0.3.0 —— 打包与无感自动拉起（PLAN-CHROME-DOWNLOADER-003）

发布形态从"一文件夹脚本"变成标准包：`pip install` 装得上、`chrome-dl` 直接能敲、
后台桥没人手点也能起来。

### 新增

- **包化**：`src/chrome_downloader/` 布局 + `pyproject.toml` 唯一打包真源（不用 setup.py）。
  发行名 `chrome-downloader-client`，导入名 `chrome_downloader`。
  入口两个：`chrome-dl`（CLI）、`chrome-dl-mcp`（MCP stdio 服务端），
  外加 `python -m chrome_downloader`（同一条 CLI 面）。
- **`paths.py`**：路径的唯一解析处。状态目录优先级
  显式 `--state-dir` > `CHROME_DL_STATE_DIR` > 默认；
  **默认值与 v0.2 逐字符相同**（C4：换了默认值就等于让升级用户读不到自己的
  `endpoint.json` / `token` / `handshake.key` / 账本）。
  下载根默认改为"探测到的 Chrome 下载目录"，探测不到才退 `~/Downloads`。
- **`daemon.py`**：后台无感自动拉起 + 锁协议。
  锁生命周期状态机（含 N4 的 `FileNotFoundError` 竞态分支与"限 1 次"语义）、
  `try/finally` + `atexit` 双保险释放、陈旧判定（pid 不在 / bootId 不符 / 锁龄超阈值）。
  就绪判定升为**三级**：endpoint 可用 ∧ health 通且 `egressGuard=true` ∧ 配对面就绪。
  可关：`CHROME_DL_NO_AUTOSPAWN=1` 或 `chrome-dl --no-auto-spawn`。
- **`cli.py`**：统一动词树。v0.2 的 12 个动词原样复用（委托 `client.main`，不另写一份），
  新增 `download`（`add` 的别名，带 `--wait/--timeout/--sha256/--filename`）、
  `env`、`extension-path`、`daemon start|stop|status|restart`。
  **不新增 `verify` 子命令**：完整性校验走既有 `show` 读账本里的 `sha256Actual`。
- **`mcp_server.py`**：零第三方依赖的 MCP stdio 服务端，**双时代**协商
  （`2026-07-28` 现代无握手 + `2025-11-25` 旧时代 `initialize`）。
  8 个工具固定顺序；`tools/call` 响应单行 ≤8192 字节，超限由服务端**截断**并如实标
  `truncated` / `totalCount` / `omitted`（C6 第三段）。
  规范原文凭据：`docs/mcp-spec-archive.md`。
- **测试搬迁**：91 条 v0.2 套件在包树上跑通，交付件取用路径全部改由
  `find_spec("chrome_downloader")` 解析（`DELIVERY_ROOT`），子进程改走 `-m` 模块。
  新增 `tests/test_pkg_v03*.py`：打包 P1–P8、搬迁 B1–B4、拉起 D1–D13 + V4b2、
  CLI L1–L6、MCP M1–M13、C1 新增面静态扫描。
- **文档**：README 第十一节"装了包之后的三步用法"，其中命令被
  `workflow/run_readme_examples.py` 逐条真跑过（`# runnable` + `# exit: <n>` 白名单制）；
  另有 `README_EN.md`、`docs/mcp-setup.md`。

### 行为变化（会被人感觉到的）

- 提交一条 URL 而桥没在跑时：**以前**报"找不到 endpoint.json，先跑 python bridge.py"；
  **现在**自动把桥拉起来并等它三级就绪，然后把任务交下去。不想要就设
  `CHROME_DL_NO_AUTOSPAWN=1`。
- 桥是**脱离父进程**的（Windows `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`，
  POSIX `start_new_session`）。它的 stdout/stderr 进 `<state>/bridge.log`，
  不混进调用方的输出。`chrome-dl daemon stop` 只结束归因得到的桥。
- 端口被**不认识的**进程占着时：不接管、不递凭证（一个 token/URL 都不发过去），
  给人话提示后非 0 退出。
- 测试期不再需要有人手点：所有新套件一律用临时 state-dir + 临时端口，
  不占 18888（那是 W14/W14b 写死的门牌）。

### 一点没动的硬约束

- **C1**：文件字节 100% 由浏览器 `chrome.downloads` 发起。Bridge / Client / CLI /
  MCP / Daemon 五方都不是字节通道；单条控制纸条 >8192 字节即断链。
  `bridge.py` / `client.py` / `check_env.py` / `mock_server.py` 四个搬运件
  **逐字节未改**（sha256 见计划 §0.1），扩展四个文件同样逐字节相同。
- **C2**：非回环出站熔断仍前置于 Bridge，打包与自动拉起都不绕过它。
  拉起代码只跑在**没装熔断**的进程里（熔正是 `raise`，不是阻塞，最坏是人话报错不会死锁）。
- **C3**：必装第三方依赖仍为 0；`aiohttp` 只在 extras；构建工具只进临时 venv。
- **C5**：扩展三权限 `{downloads, alarms, storage}`、无 `host_permissions`、
  `minimum_chrome_version ≥ 116` 一条没松。
- **C6**：8192 上限的作用域明确为三段（WS 控制帧 / 扩展发送前自检 / MCP 响应单行）。
- README 第九节"固有上限不粉饰"那段一字没改：抢先把 18888 门牌占掉的坏程序会被当成
  "第一个认识的"，这个防不住；同账号下的恶意程序读得到密钥文件，600 权限只防别的用户。

### 已知未验证（不背书）

真 Chrome 侧的行为仍未实证：WS-T0 / T0.5 / T0e、W4/W4b/W5/W8/W18/W19、F5c。
Cursor 配置形状（本机未装）、Antigravity 配置页、Claude Code changelog 的协商档位
本轮均未取到一手凭据 → 一律 **UNVERIFIED**。真机报告没做完之前不发布（STOP-7）。

## 0.2.0 —— 纯回环 WebSocket 架构

扩展侧通道从"系统注册的宿主进程"换成 `ws://127.0.0.1:18888/link`；
零安装步骤（不用复制扩展 ID、不写注册表、无常驻脚本要求）；
身份绑定改为 HMAC-SHA256 双向挑战应答（TOFU）。见 `PROTOCOL.md` 与
`NM_REMOVAL_NOTE.md`。

## 0.1.x —— Native Messaging 时代

已移除，原因与登记见 `NM_REMOVAL_NOTE.md`（该文件同时是 grep 的唯一豁免面）。
