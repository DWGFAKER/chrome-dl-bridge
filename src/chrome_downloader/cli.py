#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cli.py —— 统一命令树 `chrome-dl`（PLAN-CHROME-DOWNLOADER-003 · T4）

v0.2 的 CLI 长在 client.py 里（`python client.py add <url>`，12 个动词）。
本文件**不重写那 12 个动词**：它调 client.main(argv) 复用，只在外面加
`download` / `env` / `extension-path` / `daemon` 四块新面。为什么不另写一份：
  另写 = 两份口径 = 早晚漂移；那 12 个动词还挂着 CliTests（V11）的实测断言。
  而且 client.py 是搬运件，§0.1 用 sha256 逐字节钉着，**一个字都不能改**。

动词表（§4 T4）：
  add / batch / list / show / progress / watch / pause / resume / cancel / wait /
  stats / health                                  ← v0.2 既有，委托 client.main()
  download <url> [--filename] [--sha256] [--wait] [--timeout]   ← add 的别名（L5）
  env / extension-path                            ← 路径体检（T2）
  daemon start|stop|status|restart                ← 自动拉起（T3）

自动拉起在**这里**发生（不在 client.py 里）：先 ensure_bridge 拿到 port+token，
再显式传给 client.main —— 这正是 §3.2.3 红线 1 要求的"拉起代码跑在未装熔断的进程"。
`--no-auto-spawn` 退回 v0.2 行为（D8）。

判据对应：
  L1 每个子命令 --help exit 0
  L2 未知子命令 exit≠0 + 人话 + 无 traceback
  L3 daemon status 输出可 json.loads 且字段固定
  L4 daemon stop 后 D13 三文件状态成立
  L5 download 与 add 等价（同参数同 job 结果）
  L6 任一路径不吐 traceback（stdout 与 stderr 都不许）

退出码：0 成功 / 1 失败（含未到终态）/ 2 用法错 / 130 Ctrl-C。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import paths
from .common import humanize, say

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

# v0.2 既有动词 = client.py 的 add_parser 全集（实测集合，§8.6 也引它）
LEGACY_VERBS = ["add", "batch", "list", "show", "progress", "watch",
                "pause", "resume", "cancel", "wait", "stats", "health"]
NEW_VERBS = ["download", "env", "extension-path", "daemon"]
ALL_VERBS = LEGACY_VERBS + NEW_VERBS


# ==========================================================================
# 复用 v0.2 的实现
# ==========================================================================
def _run_legacy(argv: list[str]) -> int:
    from .client import main as legacy_main
    return int(legacy_main(argv) or 0)


def _global_flags(args, *, port=None, token=None) -> list[str]:
    """拼出 v0.2 认识的 argv 前缀。ensure 成功后一定带 --port/--token（零配置面已解析）。"""
    out: list[str] = []
    host = args.host or "127.0.0.1"
    out += ["--host", host]
    if port is not None:
        out += ["--port", str(port)]
    if token:
        out += ["--token", token]
    if args.state_dir:
        out += ["--state-dir", str(args.state_dir)]
    if args.transport and args.transport != "sse":
        out += ["--transport", args.transport]
    return out


def _connect(args) -> tuple[list[str], dict | None]:
    """返回 (argv 前缀, endpoint)。--no-auto-spawn / 自动拉起被关 → 走 v0.2 老路。

    ⚠️ `--download-root` 必须**真的传给被拉起的桥**（实测踩到的真 bug）：
       以前这里只传 state-dir/port，`--download-root` 被静默丢掉，
       于是桥按自己的默认根（~/Downloads/chrome-dl）落盘，
       而扩展/搬运工按调用方指定的根写文件 → 桥在校验那一步找不到文件，
       账本里 `sha256Actual` 变成 `MISSING`、任务判 FAILED。
       表现是"下载失败了"，病根却是"旗标没转发"。宁可直接报错，也不静默丢旗标。
    """
    from . import daemon as dm
    if args.no_auto_spawn:
        return _global_flags(args), paths.read_endpoint(args.state_dir)
    try:
        ep = dm.ensure_bridge(args.state_dir, port=args.port,
                              download_root=args.download_root,
                              quiet=True)["endpoint"]
    except dm.DaemonError as exc:
        # D5：人话 + 非 0 + 无 traceback（调用方 main() 里统一捕获 DaemonError）
        raise exc from None
    return _global_flags(args, port=ep.get("httpPort"), token=ep.get("token")), ep


# ==========================================================================
# download（add 别名）—— §4 明文要求 --wait / --timeout 挂在它上面
# ==========================================================================
def cmd_download(args, rest: list[str], url: str | None) -> int:
    """内部**转成 add** 走同一条 /v1/jobs：--filename→--name、--sha256→--sha256、--wait→--wait。

    只有 --timeout 是 add 没有的（v0.2 的 add --wait 用 SDK 默认 1800s）。
    带 --timeout 时不转 add，直接用 SDK 的 wait(timeout=)，两者打的是同一个面。
    """
    from . import daemon as dm
    url = url or args.url
    if not url:
        say("download 需要一个 URL。→ chrome-dl download <网址> [--sha256 HEX] [--wait]",
            err=True)
        return EXIT_USAGE
    prefix, _ep = _connect(args)
    if not args.wait or args.timeout is None:
        sub = ["add", url]
        name = args.filename or (rest[0] if rest and not rest[0].startswith("-") else None)
        if name:
            sub += ["--name", name]
        if args.sha256:
            sub += ["--sha256", args.sha256]
        if args.wait:
            sub.append("--wait")
        return _run_legacy(prefix + sub)
    # 带 --timeout 的等待：同一个 add 面提交，再用 SDK 的 wait 收
    from .client import Client
    c = Client(host=args.host or "127.0.0.1",
               port=_ep.get("httpPort") if _ep else None,
               token=_ep.get("token") if _ep else None,
               state_dir=args.state_dir)
    r = c.add(url, args.filename, args.sha256)
    job = r["job"]
    print(f"已提交 jobId={job['jobId']}（{'复用旧任务' if r.get('reused') else '新任务'}）")
    res = c.wait([job["jobId"]], timeout=args.timeout)
    print(json.dumps(res["jobs"][0], ensure_ascii=False, indent=2))
    if not res["ok"]:
        say(f"超时（{args.timeout:.0f}s）没到终态", err=True)
        return EXIT_FAIL
    return EXIT_OK if res["jobs"][0]["state"] == "COMPLETE" else EXIT_FAIL


def cmd_env(args, rest: list[str]) -> int:
    sd = paths.resolve_state_dir(args.state_dir)
    root = paths.download_root(args.download_root)
    info = {
        "stateDir": str(sd),
        "stateDirExists": sd.exists(),
        "endpoint": str(paths.endpoint_path(sd)),
        "endpointExists": paths.endpoint_path(sd).exists(),
        "downloadRoot": str(root),
        "downloadRootExists": root.exists(),
        "chromeDownloadDir": paths.chrome_download_dir(),
        "extensionDir": str(paths.extension_dir()),
        "extensionDirExists": paths.extension_dir().exists(),
        "extensionViaResources": str(paths.extension_dir_resource()),
        "packageDir": str(paths.package_dir()),
        "bridgePort": paths.BRIDGE_PORT,
        "lockStaleSec": paths.lock_stale_sec(),
        "noAutospawn": paths.no_autospawn(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"状态目录       : {info['stateDir']}"
          f"  {'[在]' if info['stateDirExists'] else '[还没建，第一次用会自动建]'}")
    print(f"门牌记账本     : {info['endpoint']}"
          f"  {'[在]' if info['endpointExists'] else '[还没有]'}")
    print(f"下载落盘目录   : {info['downloadRoot']}"
          f"  {'[在]' if info['downloadRootExists'] else '[不存在：先去 Chrome 设下载目录]'}")
    print(f"Chrome 下载目录: {info['chromeDownloadDir'] or '读不到（按默认处理）'}")
    print(f"扩展目录       : {info['extensionDir']}"
          f"  {'[在]' if info['extensionDirExists'] else '[缺失]'}")
    print(f"自动拉起       : "
          f"{'已关（CHROME_DL_NO_AUTOSPAWN=1）' if info['noAutospawn'] else '开着'}"
          f"，锁陈旧阈值 {info['lockStaleSec']:.0f}s")
    return EXIT_OK


def cmd_extension_path(args, rest: list[str]) -> int:
    d = paths.extension_dir_resource()
    missing = [f for f in paths.extension_files() if not (d / f).exists()]
    if args.json:
        print(json.dumps({"extensionDir": str(d), "missing": missing,
                          "verdict": "PASS" if not missing else "FAIL"},
                         ensure_ascii=False, indent=2))
    else:
        print(str(d))
        if missing:
            say(f"缺文件：{', '.join(missing)}", err=True)
    return EXIT_OK if not missing else EXIT_FAIL


def cmd_daemon(args, rest: list[str]) -> int:
    from . import daemon as dm
    action = args.action or (rest[0] if rest else None)
    if action not in ("start", "stop", "status", "restart"):
        say("daemon 要带动作：start / stop / status / restart\n"
            "→ 例：chrome-dl daemon status", err=True)
        return EXIT_USAGE
    if action == "status":                                   # L3
        print(json.dumps(dm.status(args.state_dir), ensure_ascii=False, indent=2))
        return EXIT_OK
    if action == "stop":                                     # L4 / D13
        out = dm.stop_bridge(args.state_dir)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if out.get("refused"):
            say(f"⚠️ {out['refused']}", err=True)
            return EXIT_FAIL
        return EXIT_OK
    try:
        if action == "restart":
            dm.stop_bridge(args.state_dir)
        res = dm.ensure_bridge(args.state_dir, port=args.port,
                               download_root=args.download_root, spawn=True,
                               quiet=args.quiet)
        ep = res["endpoint"]
        print(json.dumps({"pid": ep.get("pid"), "port": ep.get("httpPort"),
                          "transport": ep.get("transport"),
                          "spawned": bool(res.get("spawned")),
                          "reason": res.get("reason"),
                          "readyLevels": (ep.get("readyLevels") or {}).get("detail", {})},
                         ensure_ascii=False, indent=2))
        return EXIT_OK
    except dm.DaemonError as exc:
        say(str(exc), err=True)
        return EXIT_FAIL


# ==========================================================================
# 参数树
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chrome-dl",
        description="让 Chrome 替你下载：本机命令 → 桥 → 浏览器扩展取字节"
                    "（CLI 本身不碰任何文件字节）")
    p.add_argument("--host", default=None, help="桥地址（默认从 endpoint.json 读，只许回环）")
    p.add_argument("--port", type=int, default=None, help="桥端口（默认 18888）")
    p.add_argument("--token", default=None, help="凭证（默认从 endpoint.json 读）")
    p.add_argument("--state-dir", dest="state_dir", default=None,
                   help=f"状态目录（默认 {paths.default_state_dir()}）")
    p.add_argument("--download-root", dest="download_root", default=None,
                   help="下载落盘根（默认探测到的 Chrome 下载目录）")
    p.add_argument("--transport", default="sse", choices=["sse", "ws", "auto"],
                   help="v0.2 兼容位：进度统一走 SSE")
    p.add_argument("--no-auto-spawn", dest="no_auto_spawn", action="store_true",
                   help="不自动拉起桥（等同 v0.2 行为，D8）")
    p.add_argument("--json", action="store_true", help="机器可读输出（env / extension-path）")
    p.add_argument("--quiet", action="store_true", help="少说话")
    p.add_argument("--version", action="store_true", help="打印包版本与协议版本")
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("download", help="提交下载（add 的别名）")
    d.add_argument("url", nargs="?", default=None)
    d.add_argument("--filename", default=None, help="落盘文件名（= add 的 --name）")
    d.add_argument("--sha256", default=None, help="期望哈希（不填则跳过完整性校验）")
    d.add_argument("--wait", action="store_true", help="阻塞到终态")
    d.add_argument("--timeout", type=float, default=None, help="--wait 的超时秒")

    e = sub.add_parser("env", help="打印路径与配置（状态目录 / 下载根 / 扩展目录）")

    x = sub.add_parser("extension-path", help="打印扩展目录（给 Chrome「加载已解压」用）")

    dmn = sub.add_parser("daemon", help="后台桥：start / stop / status / restart")
    dmn.add_argument("action", nargs="?", default=None,
                     choices=["start", "stop", "status", "restart"])

    for verb in LEGACY_VERBS:                  # 只登记"存在"，参数表交给 client.main()
        sub.add_parser(verb, add_help=False)
    return p


# 全局旗标（可以写在动词前也可以写在动词后，抽取器会归位）
GLOBAL_FLAGS = {"--host", "--port", "--token", "--state-dir", "--download-root",
                "--transport", "--no-auto-spawn", "--json", "--quiet", "--version"}
GLOBAL_FLAGS_WITH_VALUE = {"--host", "--port", "--token", "--state-dir",
                           "--download-root", "--transport"}


def _hoist_global_flags(argv: list[str]) -> list[str]:
    """把写在**动词后面**的全局旗标提到 argv 前部（一次预归位，不重解析）。

    为什么必须做这件事（实测踩出来的新 bug）：
      `chrome-dl list --state-dir X --port 53273` 里那两个旗标原本被 argparse 丢进
      parse_known_args 的 rest，再原样转给搬运件 client.main() —— 结果要么
      client.main() 报 usage（rc=2），要么**默默改用默认门牌 18888**。
      后者最坏：看起来连的是你指定的桥，其实连的是别的桥。
      提到前面不新增任何旗标，argparse 顶层本来就认识它们，只是把位置摆对。

    ⚠️ 为什么不做"解析后再重解析一次"：试过了，`daemon status --state-dir X` 重解析时
       会把子命令的位置参数 action 丢掉（status 不见了），当场变成
       "daemon 要带动作"。所以归位必须发生在**第一次解析之前**。
    """
    head: list[str] = []
    body: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in GLOBAL_FLAGS_WITH_VALUE and i + 1 < len(argv):
            head += [tok, argv[i + 1]]
            i += 2
            continue
        if tok in GLOBAL_FLAGS:
            head.append(tok)
            i += 1
            continue
        body.append(tok)
        i += 1
    return head + body


def _usage_error(msg: str) -> int:
    """L2：未知子命令 → 人话 + exit≠0 + 无 traceback。"""
    say(f"没听懂这个动词：{msg}", err=True)
    say("能用的动词：" + ", ".join(ALL_VERBS), err=True)
    say("看细节：chrome-dl --help", err=True)
    return EXIT_USAGE


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V"):
        from . import PROTOCOL_VERSION, __version__
        print(json.dumps({"package": __version__, "protocol": PROTOCOL_VERSION,
                          "python": sys.version.split()[0],
                          "cli": "chrome-dl"}, ensure_ascii=False))
        return EXIT_OK
    try:
        parser = build_parser()
        args, rest = parser.parse_known_args(_hoist_global_flags(argv))
    except SystemExit as exc:                   # argparse 自己已经打过帮助/用法
        return int(exc.code or 0)
    if args.cmd is None:
        return _usage_error(" ".join(rest)) if rest else (
            say("没给动词。→ chrome-dl --help", err=True) or EXIT_USAGE)
    if args.cmd not in ALL_VERBS:
        return _usage_error(args.cmd)
    from .client import BridgeError
    from .daemon import DaemonError
    try:
        if args.cmd == "download":
            return cmd_download(args, rest, args.url)
        if args.cmd == "env":
            return cmd_env(args, rest)
        if args.cmd == "extension-path":
            return cmd_extension_path(args, rest)
        if args.cmd == "daemon":
            return cmd_daemon(args, rest)
        # ---- v0.2 既有 12 个动词 ----
        fwd = [args.cmd] + [r for r in rest]
        if args.cmd == "add":
            fwd = _rename_filename(fwd)
        # `<动词> --help` 必须**先于**任何连接动作处理（L1）。
        # 踩过的坑：这些动词的参数表交给搬运件 client.main() 解析，所以这里若不先截住
        # --help，就会先走 _connect() 自动拉起、拉不起来就报"没就绪"，
        # 于是 `chrome-dl add --help` 返回 1 —— 帮助页变成要连上网才给看，等于没有。
        if "--help" in rest or "-h" in rest:
            return _run_legacy([a for a in fwd])
        prefix, _ep = _connect(args)
        return _run_legacy(prefix + fwd)
    except (DaemonError, BridgeError) as exc:
        say(str(exc), err=True)                 # D5 / L6：人话，不甩栈
        return EXIT_FAIL
    except KeyboardInterrupt:
        say("（已取消）", err=True)
        return 130
    except Exception as exc:
        if os.environ.get("CHROME_DL_DEBUG") == "1":
            raise
        say(humanize(exc, prefix="出错了：") + "\n→ 先看一眼：chrome-dl daemon status",
            err=True)
        return EXIT_FAIL


def _rename_filename(fwd: list[str]) -> list[str]:
    """add 也接受 --filename（与 download 同义），转成 v0.2 的 --name。"""
    out: list[str] = []
    i = 0
    while i < len(fwd):
        if fwd[i] in ("--filename", "--file-name") and i + 1 < len(fwd):
            out += ["--name", fwd[i + 1]]
            i += 2
            continue
        out.append(fwd[i])
        i += 1
    return out


if __name__ == "__main__":
    sys.exit(main())
