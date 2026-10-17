# 旧"传话筒"（Native Messaging）移除登记

这份文件是**唯一**允许留下那些字样的地方（计划 §10.1：对确定性 grep 走白名单）。
其余任何文件里出现下列特征词，都算 v0.2 的残余，判 FAIL。

## 移除清单（v0.1 → v0.2）

| 旧物 | 处置 | 现在由什么替代 |
|---|---|---|
| `install_host.py`（注册表/宿主清单安装器，331 行） | **已删除** | 无（零注册：装完扩展即可用） |
| `start_bridge.bat`（双击常驻启动脚本） | **已删除** | 一行 `python bridge.py`；无常驻要求 |
| 宿主清单 JSON（`com.` + `article4.` + `chrome_dl`） | **已删除** | 端点写 `endpoint.json`，扩展只连固定门牌 |
| `HKCU\Software\Google\Chrome\` + `Native` + `MessagingHosts` 注册键 | **已删除**（不再有代码写它） | 无 |
| `allowed_` + `origins` 单扩展 ID 白名单 | **已删除** | HMAC 双向挑战应答 + bridgeId 钉扎 + 一键撤销 |
| 用户复制 32 位扩展 ID 这一步 | **已删除** | 无 |
| `chrome.` + `runtime.connectNative(...)` | **已删除** | `new WebSocket('ws://127.0.0.1:18888/link')` |
| `native` + `Messaging` manifest 权限 | **已删除** | 权限只剩 `downloads / alarms / storage` |
| 4 字节本机字节序长度帧（`struct.pack("@I", …)`） | **已删除** | WS 文本帧 + 8192 显式上限 |
| `nm_relay()` / `--mode nm` / `autospawn_serve()` / `LINK_MAGIC` | **已删除** | WS 服务端单进程；熔断前置到所有分支之前 |
| `nm_trace.jsonl` | **改名** | `ws_trace.jsonl` |
| `tests/test_nm_host.py` + `tests/evidence/NM/*` | **归档** | `tests/evidence/_archive_001_nm/`（只读历史，不参与判定） |
| 001 版全部 V 证据 + `acceptance_report.md` | **归档** | `tests/evidence/_archive_001/` |

## 复验命令（任何人可跑，期望：除本文件外 0 命中）

```
grep -rniE "connectNative|nativeMessaging|NativeMessagingHosts|install_host|allowed_origins|EXT_ID_RE|com\.article4\.chrome_dl" \
     --include=*.py --include=*.js --include=*.json --include=*.bat --include=*.md . \
     | grep -v "_archive" | grep -v "NM_REMOVAL_NOTE"
```

自动版判据在 `test_downloader.py`：`EgressGuardTests.test_V4a_no_native_messaging_anywhere`（W2）。

## 为什么这些字样不能一并不许提

删掉的东西得有人说明"删了什么、换成什么、怎么复验"，否则下一个人只会重新发明一遍。
所以历史账进 `tests/evidence/_archive_001*`（归档区不参与扫描），登记面集中在这一个文件里。
