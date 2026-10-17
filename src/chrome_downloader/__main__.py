#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`python -m chrome_downloader` → 与 `chrome-dl` 同一个 CLI 面。

不带参数时打印版本与用法（不静默退出 0，避免"看起来跑了其实什么都没做"）。
"""
from __future__ import annotations

import sys


def _usage() -> int:
    from . import PROTOCOL_VERSION, __version__
    print(f"chrome-downloader-client {__version__}（纸条协议 v{PROTOCOL_VERSION}）")
    print()
    print("用法：  python -m chrome_downloader <动词> [参数]")
    print("        或直接用安装出来的命令： chrome-dl <动词>")
    print()
    print("常用三步：")
    print("  1) Chrome 里加载扩展（开发者模式 → 加载已解压 → 选扩展目录）")
    print("     目录在哪： python -m chrome_downloader extension-path")
    print("  2) 起后台桥： python -m chrome_downloader daemon start")
    print("  3) 下东西：   python -m chrome_downloader download <网址> --wait")
    print()
    print("看路径与配置： python -m chrome_downloader env")
    print("看桥的状态：   python -m chrome_downloader daemon status")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _usage()
    from .cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
