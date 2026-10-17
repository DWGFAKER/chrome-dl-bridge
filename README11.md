# chrome-downloader-client（v0.3 打包工程根）

**唯一的那份 README 在包里**：`src/chrome_downloader/README.md`。

为什么放包里而不是这里：搬运件 `check_env.py` 的第 9 项体检逐条检查交付面上的文件在不在，
其中一条是 `README.md`，而它的 `HERE` 就是包目录；`test_downloader.py` 的 W20/文档口径断言
也读 `DELIVERY_ROOT/README.md`。那个文件被计划 §0.1 用 sha256 逐字节钉死不可改，
所以"README 在包根"这件事只能由**把 README 放进包**来满足，不能靠改断言。

这里再抄一份 = 两个真源，早晚漂移。所以本文件只做指路牌，不复制内容。

- 安装与三步用法：`src/chrome_downloader/README.md`（含 `# runnable` 标记块，被真跑过）
- 协议：`src/chrome_downloader/PROTOCOL.md`
- MCP 接法：`docs/mcp-setup.md`；规范原文凭据：`docs/mcp-spec-archive.md`
- 旧传话筒（Native Messaging）为什么被移除：`src/chrome_downloader/NM_REMOVAL_NOTE.md`
- 测试：`python tests/test_downloader.py`（v0.2 的 91 条）+ `tests/test_pkg_v03*.py`（v0.3 新增）
- 回归快照与判据工具：`workflow/`

工程根目录结构见计划 `scratch/chrome_downloader_pkg_plan.md` §3.1。
