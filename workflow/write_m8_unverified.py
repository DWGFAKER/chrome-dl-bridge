#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
write_m8_unverified.py —— 把 M8 如实落成 UNVERIFIED 证据

M8（真接本机 Claude Code 2.1.299 跑一次真下载）需要**人在场**操作两端：
Chrome 里加载扩展 + Claude Code 里配置并发起下载。本轮没有人守，
所以按 AGENTS.md 铁律 5 标 UNVERIFIED：
  · 不拿自动化冒充实证；
  · 也不拿「链路层对手跑通」当「真客户端跑通」。
同时写清「我用什么替代证明了相邻部分」，让人看得见缺口有多宽。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from harness import ev  # noqa: E402

QUOTE = chr(34)
out = {
    "criterion": "M8",
    "what": ("真接本机 Claude Code 2.1.299 跑通一次真下载"
             "（计划 §8.3 原文标注：半自动，人守）"),
    "verdict": None,
    "status": "UNVERIFIED",
    "why": (
        "需要人手动执行三步：① 在 Chrome 里加载扩展；② 在 Claude Code 里配置 "
        "chrome-dl-mcp；③ 由模型发起一次真下载并核对落盘文件。"
        "本轮没有人在场操作这两端，按 AGENTS.md 铁律 5 标 UNVERIFIED，"
        "不拿自动化冒充实证，也不拿『链路层对手跑通』当『真客户端跑通』。"),
    "whatWasVerifiedInstead": {
        "服务端协议面": ("M1–M7、M9–M13 全 PASS（含 -32022 不回退 initialize、"
                        "stdout 逐行 JSON 且无 \r、8192 字节预算截断）"),
        "下载链路": "M5 用链路层假 Chrome 真下一单：state=COMPLETE + sha256Actual=OK",
        "包内 CLI": ("T8 的 editable 与 wheel 两条安装路径都跑通真下载"
                     "（workflow/t8_smoke_log.json）")},
    "notCoveredByTheAbove": (
        "真 MCP 客户端的协商行为、真 Chrome 的 MV3 语义（SW 生命周期 / alarms 实钳制 / "
        "downloads 真语义 / LNA 档位）—— 这两块本轮都没有一手凭据"),
    "howToClose": ("见 docs/mcp-setup.md 第 1 节（claude mcp add chrome-downloader -- "
                   "chrome-dl-mcp）与文末『必须由人做的两项』"),
    "relatedStop": ("STOP-7（T9 真机报告没做 / 关键项仍 UNVERIFIED → 不许发布；"
                    "本地安装不受影响）"),
    "planAnchor": "§8.3 M8、§8.7 诚实边界、§10 STOP-7",
}
p = ev("M", "M8_claude_code_real_client_UNVERIFIED.json", out)
print("已落盘:", p)
