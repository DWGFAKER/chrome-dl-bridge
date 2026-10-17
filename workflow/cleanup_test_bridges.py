#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理**本会话自己起的**桥（PLAN-CHROME-DOWNLOADER-003 的执行期工具）。

为什么需要这个工具，以及它第一版为什么会失手（如实留痕）：
  第一版只认"state-dir 在 Temp/cdl_* 下"。结果我探测 CLI 旗标位置时起的桥
  （--state-dir C:\\temp\\x、…\\Temp\\tmpr32x1y57\\none、\\Temp\\nonexistent_xyz）
  被它判成"别人的桥"KEEP 掉，18888 一度一直不空 → 跑 91 条套件时 W14/W14b
  当场一 FAIL 一 ERROR。归因太窄本身就是 bug。

现在的归因（四条任一成立即"我起的"）：
  ① daemon.json 里记着的 pid —— 那个文件只由本项目 daemon 写，最强的一条；
  ② state-dir 在 %TEMP% 下（真用户的默认状态目录是 LOCALAPPDATA\\chrome-dl-bridge，
     落在 TEMP 里的只可能是测试或探测起的）；
  ③ state-dir 含本会话用过的标记（cdl_ / tmpr… / nonexistent / c:/temp/x）；
  ④ 命令行里带 --port 且该端口不是默认门牌 18888，同时 state-dir 在 TEMP 下。
  以上都不成立 → **一律不动**，只列出来给人看，交人确认（STOP-13 / §8.4.0 第 2 条）。

每次判定都追加写 workflow/cleanup_log.jsonl，事后可复查我到底杀了什么。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path("D:/article4/scratch/chrome_downloader_pkg/src")))
from chrome_downloader import daemon as dm      # noqa: E402
from chrome_downloader import paths             # noqa: E402

TMP = tempfile.gettempdir().lower().replace("\\", "/")
MARKERS = ("cdl_", "nonexistent", "c:/temp/x", "tmpr", "tmp")
LOG = Path("D:/article4/scratch/chrome_downloader_pkg/workflow/cleanup_log.jsonl")


def my_daemon_pids() -> set[int]:
    out: set[int] = set()
    for f in (paths.resolve_state_dir(None) / "daemon.json",):
        if not f.exists():
            continue
        try:
            out.add(int(json.loads(f.read_text(encoding="utf-8")).get("pid") or -1))
        except (OSError, ValueError, AttributeError):
            continue
    return out


def state_dir_of(low: str) -> str:
    for m in ("--state-dir ", "--state-dir="):
        if m in low:
            rest = low.split(m, 1)[1]
            tok = rest.split(" ", 1)[0].strip('"').rstrip("/")
            return tok
    return ""


def attribution(cmd: str, daemons: set[int]) -> tuple[bool, str]:
    low = (cmd or "").lower().replace("\\", "/")
    if not ("chrome_downloader.bridge" in low or "chrome_downloader\\bridge.py" in low):
        return False, "不是本项目的桥"
    sd = state_dir_of(low)
    try:
        pid = int(low.split()[0]) if low.split() else -1
    except ValueError:
        pid = -1
    if pid in daemons:
        return True, "daemon.json 记着的正是这个 pid"
    if sd and TMP in sd:
        return True, f"state-dir 在临时目录下（{sd}）—— 真用户默认在 LOCALAPPDATA"
    if sd and any(m in sd for m in MARKERS):
        return True, f"state-dir 含本会话标记（{sd}）"
    return False, "来源不明（不动它，交人确认）"


def record(row: dict) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    daemons = my_daemon_pids()
    procs = dm._list_python_cmdlines() or []
    mine, keep = [], []
    for pid, cmd in procs:
        low = (cmd or "").lower().replace("\\", "/")
        # 只看"本项目的桥 + 本项目的假 Chrome"。别的 python 进程（ComfyUI、别人的
        # wait_for_worker……）压根不进这张表 —— 列成 KEEP 是噪音，会让人误以为
        # "有来源不明的桥占着端口"。
        is_bridge = "chrome_downloader.bridge" in low or "chrome_downloader\bridge.py" in low
        is_fake = "fake_chrome.py" in low
        if not (is_bridge or is_fake):
            continue
        owned, why = attribution(cmd, daemons)
        if not owned and pid in daemons:
            owned, why = True, "daemon.json 记着的正是这个 pid"
        if not owned and is_fake and TMP in state_dir_of(low):
            owned, why = True, "假 Chrome 且 state-dir 在临时目录下（测试夹具）"
        (mine if owned else keep).append((pid, cmd, why))
    print(f"本会话自起的桥：{len(mine)} 个")
    for pid, cmd, why in mine:
        print(f"  KILL {pid}  [{why}]  {cmd[:120]}")
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        record({"ts": round(time.time(), 3), "action": "kill", "pid": pid,
                "reason": why, "cmdline": cmd, "returncode": r.returncode})
    print(f"\n归因不成立（一律不动，只列给人看）：{len(keep)} 个")
    for pid, cmd, why in keep:
        print(f"  KEEP {pid}  [{why}]  {cmd[:120]}")
        record({"ts": round(time.time(), 3), "action": "keep", "pid": pid,
                "reason": why, "cmdline": cmd})
    left = [(p, c) for p, c in (dm._list_python_cmdlines() or [])
            if "chrome_downloader.bridge" in (c or "").lower()]
    print(f"\n清理后仍在的桥：{len(left)}")
    return 1 if keep else 0


if __name__ == "__main__":
    sys.exit(main())
