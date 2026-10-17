#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_downloader.py（**包内的转发壳**，不是测试本体）

测试本体在工程根的 `tests/test_downloader.py`（计划 §3.1 的目录裁定）。
这里为什么还要留一个同名文件：搬运件 check_env.py 的第 9 项体检逐条检查
交付面上这些文件在不在，其中一条是 `HERE / "test_downloader.py"`
（HERE = 本模块所在目录）。check_env.py 是 v0.2 搬运件，计划 §0.1 用 sha256
把它逐字节钉死，**不许我改它的期望清单**；而 §3.1 又把测试搬到了 tests/。
两边一撞，体检就会报"文件缺失"，进而把证据 V11/check_env.json 里的一个
布尔位从 true 翻成 false —— 那是 §8.4 (b3) 明令不许出现的"判定依据漂移"。

所以正确的解法不是改判据、也不是改搬运件，而是**在这里放一个真的能用的壳**：
它把 `python -m chrome_downloader.test_downloader` 转发到 tests/ 里的本体，
名字与行为都对得上，不存在假装存在的空文件。

用法：
    python -m chrome_downloader.test_downloader            # 全量（等价于跑 tests 本体）
    python -m chrome_downloader.test_downloader UnitTests  # 只跑某个 TestCase
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

# tests/ 相对本文件的位置：装了包 → <pkg>/../tests（源码树布局）；
# 只查包目录的父级，查不到就如实报错，不去猜绝对路径。
_CANDIDATES = [Path(__file__).resolve().parent.parent.parent / "tests",   # src/chrome_downloader → 工程根/tests
               Path(__file__).resolve().parent.parent / "tests"]


def _suite_path() -> Path:
    for d in _CANDIDATES:
        f = d / "test_downloader.py"
        if f.exists():
            return f
    raise FileNotFoundError(
        "找不到测试本体 tests/test_downloader.py。已试过：\n  " +
        "\n  ".join(str(d / "test_downloader.py") for d in _CANDIDATES) +
        "\n→ 从包工程根跑：python tests/test_downloader.py")


def main(argv=None) -> int:
    target = _suite_path()
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.argv = [str(target)] + argv            # 本体按 unittest 的 argv 习惯解析
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
