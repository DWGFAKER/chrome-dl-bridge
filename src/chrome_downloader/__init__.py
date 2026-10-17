#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chrome_downloader —— 让 Chrome 替你下载（v0.3 打包版）

一句话架构：**字节只由 Chrome 扩展经 chrome.downloads 落盘**，
Bridge / Client / CLI / MCP / Daemon 五方只传"纸条"（URL / 任务号 / 状态 / 字节数 / 速度），
单个文本帧超过 8192 字节立即断链；Bridge 进程启动即装"非回环出站熔断"。

模块分工（PLAN-CHROME-DOWNLOADER-003 §3.1）：
  paths.py        路径唯一解析处（状态目录 / 下载根 / 扩展目录）
  bridge.py       本机指挥官（搬运件，逐字节同 v0.2）
  client.py       Agent 侧 SDK + 旧 CLI（搬运件，逐字节同 v0.2）
  check_env.py    一键预检（搬运件）
  mock_server.py  测试用回环源站（搬运件）
  daemon.py       后台无感自动拉起 + 锁协议（新）
  cli.py          统一命令树 chrome-dl（新）
  mcp_server.py   零依赖 MCP stdio 双时代服务端（新）
  common.py       跨模块共用的小工具（错误人话化 / 证据面 / 版本）

版本号：v0.2 的扩展 manifest 里写死 "0.2.0"（W1 逐字断言），
包版本与之**分属两个面**，这里给包自己的版本，别拿它当协议版本。
"""
from __future__ import annotations

__version__ = "0.3.0"
PROTOCOL_VERSION = 2                  # 与 bridge.py:53 一致（纸条协议版本）
MAX_FRAME_BYTES = 8192                # 与 bridge.py:54 一致（C6 第一段）

__all__ = ["__version__", "PROTOCOL_VERSION", "MAX_FRAME_BYTES", "Client", "BridgeError"]


def __getattr__(name):                # 惰性导出：import 包不拖着拉起子进程
    if name in ("Client", "BridgeError", "read_endpoint"):
        from . import client as _c
        return getattr(_c, name)
    raise AttributeError(f"module 'chrome_downloader' has no attribute {name!r}")
