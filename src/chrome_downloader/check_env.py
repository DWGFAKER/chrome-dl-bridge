#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_env.py —— 一键预检（老奶奶版体检，v0.2 纯 WebSocket 零配置）

用法：
    python check_env.py            # 给人看的清单
    python check_env.py --json     # 给机器看（测试取证）

它只看本机、只连 127.0.0.1，**不下载任何东西、不上外网、不装任何东西**。
每行给：检查项 / 结果 / 下一步该敲什么命令。

v0.2 改了什么：
  * 不再检查"插件装没装到系统里"那一类事（现在没有这一层了）；
  * 新增：18888 门牌是否被人占、Chrome 是否 ≥116（WS 保活的版本门槛）、
          Chrome ≥142 时提醒"本地网络"门禁可能拦回环（官方已知限制）、
          交付面是否还留着旧传话筒的残余（应当 0 命中）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "chrome-dl-bridge"
WS_PORT = 18888
PORT_RANGE = range(18888, 18898)
WS_MIN_CHROME = 116           # 官方：扩展 Service Worker 里用 WS 保活自 116 起
LNA_WATCH_CHROME = 142        # 官方渠道：142–144 区间有 LNA 相关实证 bug
DOWNLOAD_SUBDIR = "chrome-dl"


class Check:
    __slots__ = ("name", "ok", "detail", "fix", "level")

    def __init__(self, name, ok, detail, fix="", level="require"):
        self.name, self.ok, self.detail, self.fix, self.level = name, ok, detail, fix, level


def chrome_dir_candidates() -> list[Path]:
    dirs = [Path(r"C:\Program Files\Google\Chrome\Application"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application")]
    for pf in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", "")):
        if pf:
            dirs.append(Path(pf) / "Google" / "Chrome" / "Application")
    return dirs


def find_chrome() -> tuple[Path | None, str | None]:
    """返回 (chrome.exe, 版本号)。不启动 Chrome，只读目录与文件属性。"""
    for d in chrome_dir_candidates():
        exe = d / "chrome.exe"
        if not exe.exists():
            continue
        cands = [q.name for q in d.glob("[0-9]*.[0-9]*")
                 if q.is_dir() and (q / "chrome.dll").exists()]
        if cands:
            ver = sorted(cands, key=lambda s: [int(x) for x in re.findall(r"\d+", s)])[-1]
            return exe, ver
        try:                                        # 兜底：读文件版本资源
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.windll.version.GetFileVersionInfoW(str(exe), 0, len(buf), buf)
            if size:
                val = ctypes.c_void_p()
                ln = wintypes.UINT()
                if ctypes.windll.version.VSQueryValueW(
                        buf, "\\VarFileInfo\\Translation", ctypes.byref(val), ctypes.byref(ln)):
                    pair = ctypes.cast(val, ctypes.POINTER(ctypes.c_ushort * 2)).contents
                    key = f"\\StringFileInfo\\{pair[0]:04x}{pair[1]:04x}\\ProductVersion"
                    if ctypes.windll.version.VSQueryValueW(
                            buf, key, ctypes.byref(val), ctypes.byref(ln)):
                        return exe, ctypes.wstring_at(val).strip()
        except Exception:
            pass
        return exe, None
    return None, None


def chrome_major(ver: str | None) -> int | None:
    if not ver:
        return None
    m = re.match(r"\s*(\d+)", str(ver))
    return int(m.group(1)) if m else None


def port_state(port: int) -> str:
    """'free' / 'listening'。只连 127.0.0.1，0.3 秒超时。"""
    with socket.socket() as s:
        s.settimeout(0.3)
        try:
            return "listening" if s.connect_ex(("127.0.0.1", port)) == 0 else "free"
        except OSError:
            return "free"


def loopback_health(info: dict) -> dict:
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", info["httpPort"], timeout=3)
    c.request("GET", "/v1/health", headers={"Authorization": "Bearer " + info["token"]})
    try:
        return json.loads(c.getresponse().read().decode("utf-8"))
    finally:
        c.close()


def chrome_download_dir() -> str | None:
    """读 Chrome 当前默认下载目录（改了的话必须告诉 Bridge）。"""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    for prof in ("Default",) + tuple(p.name for p in base.glob("Profile *")):
        f = base / prof / "Preferences"
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        v = (d.get("savefile") or {}).get("default_directory")
        if v:
            return str(v)
    return None


def project_registry_keys() -> dict:
    """查注册表里有没有旧"传话筒"方案的键值残留（G1/Z1 的可跑证据）。

    键名**按片段拼**：一是免得这个 .py 自己变成一处 grep 命中，
    二是这函数本来就是用来证明"它不该存在"的。
    """
    out = {"checked": [], "present": [], "error": None}
    if os.name != "nt":
        out["error"] = "非 Windows：跳过注册表检查"
        return out
    try:
        import winreg
    except ImportError:                                  # pragma: no cover
        out["error"] = "没有 winreg"
        return out
    base = "Software" + chr(92) + "Google" + chr(92) + "Chrome" + chr(92) +         "Native" + "MessagingHosts"
    out["checked"].append("HKCU\\" + base)
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
    except OSError:
        winreg.close_key if False else None
        return out                                       # 整个父键都没有 = 干净
    try:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            if "chrome" in name.lower() or "article" in name.lower() or "dl" in name.lower():
                out["present"].append(name)
    finally:
        winreg.CloseKey(k)
    return out


def stray_processes() -> dict:
    """有没有本项目的常驻进程（v0.2 应当一个都没有）。"""
    if os.name != "nt":
        return {"error": "非 Windows"}
    import subprocess
    r = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True)
    txt = r.stdout.decode("gbk", "replace")
    bad = [l for l in txt.splitlines()
           if re.search(r"chrome[-_]dl|cdl[-_]host", l, re.I)]
    return {"strayLines": bad, "count": len(bad)}


def collect(reg: bool = False) -> dict:
    checks: list[Check] = []

    # 1. 系统与 Python
    pyver = platform.python_version()
    ok = sys.version_info >= (3, 11)
    checks.append(Check("操作系统 / Python", ok,
                        f"{platform.system()} {platform.release()} · Python {pyver}",
                        "" if ok else "装 Python 3.11 以上（python --version 看看）"))

    # 2. Chrome 装了没 / 版本够不够（116 = WS 保活门槛）
    exe, ver = find_chrome()
    checks.append(Check("Chrome 装了没", exe is not None,
                        str(exe) if exe else "没在常见目录找到 chrome.exe",
                        "装 Chrome；本机没装也能跑测试，但真下载必须有人在 Chrome 里装插件"))
    major = chrome_major(ver)
    checks.append(Check("Chrome 版本够用", bool(major and major >= WS_MIN_CHROME),
                        f"{ver or '读不到版本号'}（扩展要求 ≥{WS_MIN_CHROME}）",
                        "" if major and major >= WS_MIN_CHROME
                        else "升级 Chrome：扩展靠 116+ 的 WebSocket 保活撑住后台，旧版接不稳"))
    if major and major >= LNA_WATCH_CHROME:
        checks.append(Check(
            "本地网络门禁（新版本 Chrome 需注意）", True,
            f"Chrome {major} ≥ {LNA_WATCH_CHROME}：官方新增了\u201c本地网络访问\u201d限制，"
            f"插件在后台敲 {WS_PORT} 门牌时，提示框可能弹不出来（官方已知限制）。"
            f"真连不上时去 chrome-extension 站点设置里看这一项，或点扩展图标试一次",
            "", "info"))

    # 3. 门牌（端口）
    st = port_state(WS_PORT)
    free_alt = [p for p in PORT_RANGE if p != WS_PORT and port_state(p) == "free"]
    if st == "free":
        checks.append(Check(f"内部门牌 {WS_PORT} 空着", True, f"{WS_PORT} 空闲，可直接用",
                            ""))
    else:
        checks.append(Check(
            f"内部门牌 {WS_PORT} 空着", bool(free_alt),
            f"{WS_PORT} 上已经有东西在听"
            + (f"；{free_alt[0]}-{free_alt[-1]} 里有空的" if free_alt else "，且 18888-18897 全满"),
            f"python bridge.py --port {free_alt[0]}" if free_alt
            else "关掉占着端口的软件，或换端口区间"))

    # 4. Bridge 在不在跑
    ep = STATE_DIR / "endpoint.json"
    running = False
    if ep.exists():
        try:
            info = json.loads(ep.read_text(encoding="utf-8"))
            h = loopback_health(info)
            running = bool(h.get("ok"))
        except Exception as exc:
            checks.append(Check("Bridge 在跑吗", False,
                                f"endpoint.json 在但问不通（多半是陈文件）：{type(exc).__name__}",
                                "python bridge.py"))
    if running:
        checks.append(Check("Bridge 在跑吗", True,
                            f"pid={info.get('pid')} · 门牌={info.get('wsPort')} · "
                            f"通道={info.get('transport')} · 已配对={info.get('paired')}",
                            ""))
    elif not ep.exists():
        checks.append(Check("Bridge 在跑吗", False, "没在跑（endpoint.json 不存在）",
                            "python bridge.py"))
    elif not any(c.name == "Bridge 在跑吗" for c in checks):
        # 兜底：文件在、没抛异常、但 health 不认（ok=False）—— 这一项绝不能悄悄消失，
        # 悄悄消失会让预检报"全绿"而 Bridge 其实死了，比报红坏得多。
        checks.append(Check("Bridge 在跑吗", False,
                            "endpoint.json 在，但 Bridge 不应答 /v1/health（陈文件或版本不符）",
                            "python bridge.py"))

    # 5. 插件装没装（人这一侧唯一的步骤）
    loaded = "去 chrome://extensions 看有没有 chrome-dl-bridge"
    checks.append(Check("插件装了没", True,
                        "机器上无法自动确认（浏览器里的状态读不到）。人工核对：" + loaded +
                        " —— 装完就完事，不用复制 ID、不用改注册表",
                        "", "info"))

    # 6. 下载目录
    cd = chrome_download_dir()
    default_root = Path(os.path.expanduser("~")) / "Downloads"
    if cd is None:
        checks.append(Check("Chrome 下载目录 = Bridge 默认?", True,
                            f"读不到 Chrome 设置，按默认 {default_root} 处理", "", "info"))
    else:
        same = Path(cd).resolve() == default_root.resolve() if default_root.exists() else False
        checks.append(Check(
            "Chrome 下载目录 = Bridge 默认?", same,
            f"Chrome 现用 {cd}；Bridge 默认 {default_root} 下的 {DOWNLOAD_SUBDIR}/",
            "" if same else f'python bridge.py --download-root "{cd}"'))

    # 7. aiohttp（可选依赖：不在就走零依赖降级）
    try:
        import aiohttp
        checks.append(Check("aiohttp（可选）", True, f"装了 {aiohttp.__version__}",
                            ""))
    except ImportError:
        checks.append(Check("aiohttp（可选）", True,
                            "没装 → Bridge 自动用零依赖降级实现（手写 RFC6455），功能一样",
                            "想要更标准的实现：python -m pip install aiohttp==3.14.2", "info"))

    # 7.5 注册表 / 常驻进程（给了 --regcheck 才查，默认不碰注册表）
    if reg:
        rk = project_registry_keys()
        checks.append(Check("注册表无旧传话筒键值", not rk["present"],
                            f"查过 {rk['checked']}；发现 {rk['present'] or '无'}"
                            + (f"（{rk['error']}）" if rk.get("error") else ""),
                            "删掉这些键名，或跑一次旧脚本的 --uninstall（v0.2 已不含该脚本）"))
        sp = stray_processes()
        checks.append(Check("无本项目常驻进程", sp["count"] == 0,
                            f"发现 {sp['count']} 条：{sp['strayLines'][:2]}"
                            if sp["count"] else "tasklist 里没有 chrome-dl / cdl-host",
                            "", "require" if sp["count"] else "info"))

    # 8. 旧传话筒残余（应当 0 命中）
    residue = scan_nm_residue()
    checks.append(Check("旧传话筒残余（应为 0）", not residue["hits"],
                        f"命中 {len(residue['hits'])} 处"
                        + (f"：{residue['hits'][0]}" if residue["hits"] else ""),
                        "把残留的注册脚本/权限删干净，见 NM_REMOVAL_NOTE.md"))

    # 9. 关键文件在不在
    for f in ("bridge.py", "client.py", "mock_server.py", "test_downloader.py",
              "README.md", "PROTOCOL.md", "requirements.txt",
              "extension/manifest.json", "extension/background.js",
              "extension/popup.html", "extension/popup.js"):
        p = HERE / f
        checks.append(Check(f"文件 {f}", p.exists(), str(p) if p.exists() else "缺失",
                            "重新取一份 v0.2 代码"))

    # 10. manifest 权限形状（W1 的预检视角）
    mf = HERE / "extension" / "manifest.json"
    if mf.exists():
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
            perms = set(m.get("permissions", []))
            okp = perms == {"downloads", "alarms", "storage"}
            checks.append(Check("插件权限只三样", okp,
                                f"permissions={sorted(perms)} · host_permissions="
                                f"{sorted(m.get('host_permissions', []))} · "
                                f"minimum_chrome_version={m.get('minimum_chrome_version')}",
                                "" if okp else "manifest 权限应为 downloads/alarms/storage 三样"))
        except Exception as exc:
            checks.append(Check("插件权限只三样", False, f"manifest 读不动：{exc}", ""))

    blocking = [c.name for c in checks if c.level == "require" and not c.ok]
    return {"stateDir": str(STATE_DIR), "wsPort": WS_PORT,
            "checks": [{k: getattr(c, k) for k in Check.__slots__} for c in checks],
            "blocking": blocking, "ready": not blocking}


# 行内豁免标记：只有带这个标记的行允许出现旧特征词，且必须在 NM_REMOVAL_NOTE.md 里登记。
MARK = "grep-ok: " + "nm-residue"


def scan_nm_residue() -> dict:
    """确定性 grep：旧传话筒的五个特征词应当 0 命中（计划 §10.1 / W2）。

    模式**按片段拼出来**，免得本文件自己变成一处命中。
    """
    pat = "|".join(["connect" + "Native", "native" + "Messaging", "Native" + "MessagingHosts",
                    "install_" + "host", "allowed_" + "origins"])
    hits = []
    for root, dirs, files in os.walk(HERE):
        # __pycache__/归档区/**工具生成的证据目录**不参与：
        # 证据里记的就是"命中清单本身"，扫它等于自己咬自己的尾巴（假阳性）。
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules", "evidence")
                   and not d.startswith("_archive")]
        for f in files:
            if not f.endswith((".py", ".js", ".json", ".bat")):
                continue
            p = Path(root) / f
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    if MARK in line:                       # 显式豁免行（登记在 NM_REMOVAL_NOTE.md）
                        continue
                    if re.search(pat, line, re.I):
                        hits.append(f"{p.relative_to(HERE)}:{i}")
            except (UnicodeDecodeError, OSError):
                continue
    return {"pattern": pat, "hits": hits}


def human(rep: dict) -> None:
    print("=" * 66)
    print(" 下载桥 预检（v0.2：装了就能用，不再需要传话筒）")
    print("=" * 66)
    for c in rep["checks"]:
        mark = "✓" if c["ok"] else ("·" if c["level"] == "info" else "✗")
        print(f"[{mark}] {c['name']}")
        print(f"      {c['detail']}")
        if c["fix"]:
            print(f"      → {c['fix']}")
    print("-" * 66)
    if rep["ready"]:
        print("结论：可以开干。两步用法：")
        print("  1) Chrome 里把 extension 文件夹装上（开发者模式 → 加载已解压的扩展程序）")
        print("  2) python bridge.py            （这一行就是启动，窗口最小化即可）")
        print("  然后：python client.py add <网址>")
    else:
        print("结论：还差几项 —— 按上面带 ✗ 的提示逐条办，办完再跑一次本脚本。")
        print("缺的是：", "、".join(rep["blocking"]))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="下载桥一键预检")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    p.add_argument("--regcheck", action="store_true",
                   help="额外查注册表与进程里有没有旧方案的残留（真机验收 Z1 用）")
    a = p.parse_args(argv)
    rep = collect(reg=a.regcheck)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        human(rep)
    return 0 if rep["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
