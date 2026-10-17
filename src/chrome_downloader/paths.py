#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paths.py —— 路径的唯一解析处（PLAN-CHROME-DOWNLOADER-003 · T2）

为什么要有这个文件：v0.2 把状态目录的默认值抄在了三处（bridge.py 的 --state-dir 默认、
client.py 的 default_state_dir()、check_env.py 的 STATE_DIR）。三处各写一遍 = 三处可漂移，
而 bridge.py 与 client.py 在本计划里是**逐字节不许动**的搬运件（§0.1 / V10 判据）。
所以这里只解决"新增面"（daemon/cli/mcp/测试）的口径唯一化，并给出与 v0.2 完全一致的默认值。

多用户隔离的依据（不是设计偏好，是操作系统的事实）：
  * Windows：LOCALAPPDATA 本身按登录用户分目录（C:\\Users\\<u>\\AppData\\Local），
    所以默认 state-dir 天然 per-user；跨用户读别人的 token 需要管理员权限。
  * POSIX：不假设 HOME 够用，优先 XDG（state/config 各有去处），拿不到才退 ~/.chrome-dl。
  * 同机多用户想显式隔离：CHROME_DL_STATE_DIR 或 --state-dir，优先级见 resolve_state_dir。

C4 向后兼容：Windows 下的默认值必须与 v0.2 逐字符相同，否则升级后
endpoint.json / token / handshake.key / jobs.json 全部读不到（账本与配对会丢）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---- v0.2 就有的常量（不许改，改了等于换门牌）----
BRIDGE_PORT = 18888
PORT_MIN, PORT_MAX = 18888, 18897
APP_DIR_NAME = "chrome-dl-bridge"          # state 目录名（v0.2 同名，C4）
DOWNLOAD_SUBDIR = "chrome-dl"              # download_root 下的子目录（v0.2 同名）
ENDPOINT_FILE = "endpoint.json"
TOKEN_FILE = "token"
KEY_FILE = "handshake.key"
BRIDGE_ID_FILE = "bridge_id"
LEDGER_FILE = "jobs.json"
DAEMON_LOCK_FILE = "daemon.lock"
DAEMON_STATE_FILE = "daemon.json"
DAEMON_LOG_FILE = "daemon.log"
BRIDGE_LOG_FILE = "bridge.log"

ENV_STATE_DIR = "CHROME_DL_STATE_DIR"
ENV_NO_AUTOSPAWN = "CHROME_DL_NO_AUTOSPAWN"
ENV_STALE_SEC = "CHROME_DL_LOCK_STALE_SEC"
ENV_DOWNLOAD_ROOT = "CHROME_DL_DOWNLOAD_ROOT"

DEFAULT_LOCK_STALE_SEC = 60.0
LOCK_STALE_MIN = 1.0            # 测试要把 D9 压到秒级，但不许压成 0（等于没有锁）


# ==========================================================================
# 平台基址
# ==========================================================================
def _win_localappdata() -> Path | None:
    v = os.environ.get("LOCALAPPDATA")
    return Path(v) if v else None


def xdg_base(kind: str = "state") -> Path:
    """POSIX 的 XDG 目录；kind: state | config | data。"""
    env = {"state": "XDG_STATE_HOME", "config": "XDG_CONFIG_HOME",
           "data": "XDG_DATA_HOME"}[kind]
    v = os.environ.get(env)
    if v:
        return Path(v)
    if kind == "state":
        return Path.home() / ".local" / "state"
    if kind == "config":
        return Path.home() / ".config"
    return Path.home() / ".local" / "share"


def default_state_dir() -> str:
    """**与 v0.2 逐字符相同**的默认状态目录。

    Windows / macOS / Linux 上都沿用 bridge.py:2118 的算法
    （`LOCALAPPDATA or expanduser("~")` + "chrome-dl-bridge"）：
    这不是偷懒，而是 C4 的要求 —— 换算法就会让升级用户读不到自己的 endpoint.json/token。
    XDG 只在 POSIX 上作为**补充**提供（见 posix_xdg_state_dir），不改默认，
    免得"更规范"变成"不兼容"。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME)


def posix_xdg_state_dir() -> str:
    """POSIX 上想要 XDG 规范的用户可以显式用这个（不是默认，见上）。"""
    return str(xdg_base("state") / APP_DIR_NAME)


def resolve_state_dir(explicit: str | os.PathLike | None = None) -> Path:
    """优先级：显式参数 > CHROME_DL_STATE_DIR > v0.2 默认。

    返回**绝对路径**（Path）。不隐式创建目录 —— 创建走 ensure_state_dir()，
    这样"读路径"这个动作没有副作用（体检与 CLI 的 --help 都爱踩到读路径）。
    """
    raw = explicit or os.environ.get(ENV_STATE_DIR) or default_state_dir()
    p = Path(os.path.expanduser(str(raw)))
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    else:
        p = Path(os.path.normpath(str(p)))
    return p


def ensure_state_dir(explicit: str | os.PathLike | None = None) -> Path:
    """建目录（0700 尽力而为）并返回路径。失败抛 RuntimeError 带人话。"""
    p = resolve_state_dir(explicit)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"状态目录打不开：{p}\n→ {type(exc).__name__}: {exc}\n"
                           f"  换一个可写的目录：chrome-dl --state-dir <目录> ...") from exc
    try:
        os.chmod(p, 0o700)          # 多用户机器上：token/key 不许被同机他人读
    except OSError:
        pass                         # Windows 上 chmod 语义有限，尽力即可，不因此报错
    return p


# ==========================================================================
# 派生路径（全部只读拼接，不创建）
# ==========================================================================
def endpoint_path(state_dir=None) -> Path:
    return resolve_state_dir(state_dir) / ENDPOINT_FILE


def daemon_lock_path(state_dir=None) -> Path:
    return resolve_state_dir(state_dir) / DAEMON_LOCK_FILE


def daemon_state_path(state_dir=None) -> Path:
    return resolve_state_dir(state_dir) / DAEMON_STATE_FILE


def daemon_log_path(state_dir=None) -> Path:
    return resolve_state_dir(state_dir) / DAEMON_LOG_FILE


def bridge_log_path(state_dir=None) -> Path:
    return resolve_state_dir(state_dir) / BRIDGE_LOG_FILE


def pairing_key_path(state_dir=None) -> Path:
    return resolve_state_dir(state_dir) / KEY_FILE


def ledger_path(state_dir=None) -> Path:
    return resolve_state_dir(state_dir) / LEDGER_FILE


def read_endpoint(state_dir=None) -> dict | None:
    """读 endpoint.json；不存在/坏掉 → None（不抛，让调用方决定怎么说人话）。

    兼容 v0.2 的字段边角：老版本可能只有 wsPort 没有 httpPort。
    """
    ep = endpoint_path(state_dir)
    if not ep.exists():
        return None
    try:
        info = json.loads(ep.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    info.setdefault("httpHost", "127.0.0.1")
    if "httpPort" not in info and "wsPort" in info:
        info["httpPort"] = info["wsPort"]
    return info


# ==========================================================================
# 下载根（T2：默认 = 探测到的 Chrome 下载目录，探测不到才退 ~/Downloads）
# ==========================================================================
def chrome_user_data_dir() -> Path | None:
    """Chrome 的 User Data 根（只读探测，不启动 Chrome）。"""
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA")
        if la:
            p = Path(la) / "Google" / "Chrome" / "User Data"
            return p if p.exists() else None
        return None
    home = Path.home()
    for c in (home / "Library" / "Application Support" / "Google" / "Chrome",
              home / ".config" / "google-chrome",
              home / ".config" / "chromium"):
        if c.exists():
            return c
    return None


def chrome_download_dir() -> str | None:
    """读 Chrome 当前默认下载目录（改了的话必须告诉 Bridge）。

    与 v0.2 的 check_env.chrome_download_dir() 同口径（Default + 所有 Profile *），
    搬到这里是为了"唯一解析处"；调用时读环境变量，不在导入时固化 ——
    test_check_env_catches_custom_chrome_download_dir 靠换 LOCALAPPDATA 造场景。
    """
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    if not base.exists():
        alt = chrome_user_data_dir()
        base = alt if alt is not None else base
    profs = []
    try:
        profs = [p.name for p in base.glob("Profile *")] if base.exists() else []
    except OSError:
        profs = []
    for prof in ("Default",) + tuple(sorted(profs)):
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


def default_download_root() -> Path:
    """Bridge 的 --download-root 默认值：探测到的 Chrome 目录优先，否则 ~/Downloads。

    注意：这里**只是新增面（daemon/cli/mcp）的默认值**。bridge.py 自身的 argparse 默认
    （~/Downloads）逐字节不动 —— 它总被显式传参覆盖，改它反而破坏 §0.1 的搬运判据。
    """
    cd = chrome_download_dir()
    if cd:
        p = Path(cd)
        if p.exists():
            return p
    return Path(os.path.expanduser("~")) / "Downloads"


def download_root(explicit: str | os.PathLike | None = None) -> Path:
    raw = explicit or os.environ.get(ENV_DOWNLOAD_ROOT)
    return Path(os.path.expanduser(str(raw))) if raw else default_download_root()


# ==========================================================================
# 包内资源（P3：扩展目录必须可由 importlib.resources 解析）
# ==========================================================================
def package_dir() -> Path:
    return Path(__file__).resolve().parent


def delivery_root() -> Path:
    """交付根 = 包目录（搬运件的 sha256 判据与见证文件都以此为锚）。"""
    return package_dir()


def extension_dir() -> Path:
    return package_dir() / "extension"


def extension_files() -> list[str]:
    return ["manifest.json", "background.js", "popup.html", "popup.js"]


def extension_dir_resource() -> Path:
    """优先 importlib.resources 解析（打包进 wheel 后也命中），退化为 __file__ 拼接。"""
    try:
        from importlib.resources import as_file, files      # noqa: PLC0415
    except ImportError:                                     # pragma: no cover
        return extension_dir()
    try:
        ref = files("chrome_downloader").joinpath("extension")
        # MultiplexedPath / PosixPath 都可直接转 str；zip 内的资源需要 as_file（会临时解包）
        try:
            return Path(str(ref))
        except Exception:                                   # pragma: no cover
            with as_file(ref) as p:
                return Path(str(p))
    except Exception:
        return extension_dir()


# ==========================================================================
def lock_stale_sec() -> float:
    """锁陈旧阈值：默认 60s，CHROME_DL_LOCK_STALE_SEC 可覆盖（D9 测试要压到 2s）。"""
    raw = os.environ.get(ENV_STALE_SEC)
    if not raw:
        return DEFAULT_LOCK_STALE_SEC
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_LOCK_STALE_SEC
    return max(LOCK_STALE_MIN, v)


def no_autospawn() -> bool:
    """CHROME_DL_NO_AUTOSPAWN=1 → 关掉自动拉起（C8：必须可关、必须可告知）。"""
    return os.environ.get(ENV_NO_AUTOSPAWN, "").strip() not in ("", "0", "false", "False")


if __name__ == "__main__":          # 调试入口：python -m chrome_downloader.paths
    out = {"stateDir": str(resolve_state_dir()), "endpoint": str(endpoint_path()),
           "downloadRoot": str(default_download_root()),
           "chromeDownloadDir": chrome_download_dir(),
           "extensionDir": str(extension_dir()), "extensionViaResources":
           str(extension_dir_resource()),
           "lockStaleSec": lock_stale_sec(), "noAutospawn": no_autospawn(),
           "platform": sys.platform}
    print(json.dumps(out, ensure_ascii=False, indent=2))
