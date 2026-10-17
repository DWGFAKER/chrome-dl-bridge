#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client.py —— Agent 侧 SDK + CLI（**零第三方依赖**，只用标准库）

它只做两件事：把命令送到本机 Bridge，把进度读回来。**它不下载任何文件字节。**

v0.2 的变化只有一处：**Bridge 的扩展侧通道**从 Native Messaging 换成了回环 WebSocket。
Agent ⇄ Bridge 这一面**仍是同端口上的 HTTP + SSE**（18888），所以：
  * 接口不变：download(urls) / get_progress() / pause() / resume() / cancel() / wait()
  * 调用方式不变：`python client.py add <url>` 照旧，端口/口令仍从 endpoint.json 自读
  * 零配置：不需要复制扩展 ID、不需要写注册表、不需要常驻启动脚本

  add / batch     提交任务（批量）
  list / show     看账本
  progress        一次性查进度 + 网速
  watch           SSE 实时流式跟进度（Ctrl-C 退出）
  pause/resume/cancel
  wait            等一批任务全部到终态
  stats / health  体检（含出站熔断拦截次数、扩展是否在连、配对与 bridgeId）

transport：
  sse（默认，也是唯一）—— 标准库 http.client：命令走 POST/GET，进度走 text/event-stream。
  （--transport ws 是 0.1 留下的兼容位：现在 Bridge 只有一个端口，选什么都走 SSE，不会报错。）
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from pathlib import Path

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _assert_loopback(host: str) -> None:
    """客户端也只准连本机 Bridge。写了外网地址就地报错，不给"顺手直连"留活路。"""
    if host not in LOOPBACK and not str(host).startswith("127."):
        raise ValueError(f"[client] 只允许连回环地址，收到 {host!r}（C1：不许绕过浏览器取字节）")


class BridgeError(RuntimeError):
    pass


class Client:
    def __init__(self, host: str | None = None, port: int | None = None,
                 token: str | None = None, state_dir: str | None = None,
                 timeout: float = 30.0, transport: str = "sse"):
        # 只要给了 port+token，就不必读 endpoint.json（host 默认回环）；
        # 否则从 endpoint.json 自动补齐，让用户一行 `python client.py list` 就能用。
        if port is None or token is None:
            info = read_endpoint(state_dir)
            host = host or info["httpHost"]
            port = port or info["httpPort"]
            token = token or info["token"]
        host = host or "127.0.0.1"
        _assert_loopback(host)
        self.host, self.port, self.token, self.timeout = host, port, token, timeout
        self.transport = transport

    # ---- 底层 ----
    def _conn(self, timeout=None):
        c = http.client.HTTPConnection(self.host, self.port,
                                       timeout=self.timeout if timeout is None else timeout)
        return c

    def call(self, method: str, path: str, body=None, timeout=None) -> dict:
        c = self._conn(timeout)
        try:
            data = None if body is None else json.dumps(body).encode("utf-8")
            hdr = {"Authorization": f"Bearer {self.token}"}
            if data is not None:
                hdr["Content-Type"] = "application/json"
            c.request(method, path, body=data, headers=hdr)
            r = c.getresponse()
            raw = r.read().decode("utf-8", "replace")
            try:
                obj = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                raise BridgeError(f"Bridge 返回非 JSON (HTTP {r.status}): {raw[:200]}")
            if r.status >= 400:
                raise BridgeError(f"{method} {path} -> HTTP {r.status}: {obj}")
            # 注意：任务记录自带 error 字段（如 NO_EXTENSION_CONNECTED），
            # 那不是协议错误 —— 只有没带 jobId 的 error 才是报错。
            if obj.get("error") and "jobId" not in obj:
                raise BridgeError(f"{method} {path} -> {obj}")
            return obj
        finally:
            c.close()

    # ---- 命令 ----
    def health(self) -> dict:
        return self.call("GET", "/v1/health")

    def stats(self) -> dict:
        return self.call("GET", "/v1/stats")

    def add(self, url: str, filename: str | None = None, sha256: str | None = None) -> dict:
        spec = {"url": url}
        if filename:
            spec["filename"] = filename
        if sha256:
            spec["sha256"] = sha256
        return self.call("POST", "/v1/jobs", spec)

    def batch(self, jobs: list[dict]) -> list[dict]:
        out = self.call("POST", "/v1/jobs/batch", {"jobs": jobs})
        return out.get("results", [])

    def list(self) -> list[dict]:
        return self.call("GET", "/v1/jobs").get("jobs", [])

    def show(self, job_id: str) -> dict:
        return self.call("GET", f"/v1/jobs/{job_id}")

    def progress(self, job_id: str | None = None) -> dict:
        if job_id:
            return self.show(job_id)
        return {"jobs": self.list()}

    # ---- 计划 §5.2 的 SDK 口径：同一批实现，换一组更直观的名字（接口一致，不拆两套逻辑）----
    def download(self, urls, filename=None, sha256=None) -> list:
        """提交一个或一批 URL，返回 jobId 列表。

        urls 可以是字符串，也可以是 {url,filename,sha256} 字典（或两者混合的列表）。
        走的是同一个 /v1/jobs(/batch) 面：**本方法不取任何字节。**
        """
        specs = urls if isinstance(urls, (list, tuple)) else [urls]
        out = []
        for s in specs:
            if isinstance(s, dict):
                out.append(s)
            else:
                d = {"url": s}
                if filename:
                    d["filename"] = filename
                if sha256:
                    d["sha256"] = sha256
                out.append(d)
        if len(out) == 1:
            return [self.call("POST", "/v1/jobs", out[0])["job"]["jobId"]]
        return [r["job"]["jobId"] if r.get("ok") else None
                for r in self.batch(out)]

    def get_progress(self, job_id: str | None = None) -> dict:
        return self.progress(job_id)

    def link_status(self) -> dict:
        """扩展链路状态（v0.2 新增）：在不在连、走哪个端口、配对没有、对端是谁。"""
        h = self.health()
        return {"extensionConnected": h.get("extensionConnected"),
                "chromeVersion": h.get("chromeVersion"),
                "transport": h.get("transport"), "wsPort": h.get("httpPort"),
                "wsPath": h.get("wsPath"), "paired": h.get("paired"),
                "bridgeId": h.get("bridgeId"), "pid": h.get("pid"),
                "egressGuard": h.get("egressGuard"), "stats": h.get("stats")}

    def pause(self, job_id: str) -> dict:
        return self.call("POST", f"/v1/jobs/{job_id}/pause")

    def resume(self, job_id: str) -> dict:
        return self.call("POST", f"/v1/jobs/{job_id}/resume")

    def cancel(self, job_id: str) -> dict:
        return self.call("POST", f"/v1/jobs/{job_id}/cancel")

    def trace(self, limit: int = 100000) -> dict:
        return self.call("GET", f"/v1/trace?limit={limit}")

    # ---- 流式 ----
    def watch(self, on_event, until=None, stop_after_all: bool = False,
              timeout: float | None = None) -> dict:
        """SSE 跟进度。on_event(dict)。until=终态集合。返回每个 jobId 的最后一次快照。"""
        last: dict[str, dict] = {}
        t0 = time.time()
        c = self._conn(timeout=timeout or 300)
        c.request("GET", "/v1/stream", headers={"Authorization": f"Bearer {self.token}",
                                                "Accept": "text/event-stream"})
        r = c.getresponse()
        if r.status != 200:
            raise BridgeError(f"stream HTTP {r.status}")
        try:
            for raw in _sse_lines(r):
                if not raw.startswith("data:"):
                    continue
                try:
                    evt = json.loads(raw[5:].strip())
                except json.JSONDecodeError:
                    continue
                jid = evt.get("jobId")
                if jid:
                    last[jid] = evt
                on_event(evt)
                if stop_after_all and until and last and \
                        all(e.get("state") in until for e in last.values()) and \
                        (not until or all(v.get("state") in until for v in last.values())):
                    break
                if timeout and time.time() - t0 > timeout:
                    break
        finally:
            c.close()
        return last

    def wait(self, job_ids: list[str] | None = None, poll: float = 1.0,
             timeout: float = 1800.0) -> dict:
        """轮询等终态（不依赖 SSE，CLI 默认路径，最稳）。"""
        t0 = time.time()
        seen: dict[str, dict] = {}
        while time.time() - t0 < timeout:
            jobs = self.list()
            for j in jobs:
                if job_ids and j["jobId"] not in job_ids:
                    continue
                seen[j["jobId"]] = j
            targets = [seen.get(j) for j in (job_ids or seen)] if job_ids else list(seen.values())
            if targets and all(t and t["state"] in ("COMPLETE", "FAILED", "CANCELLED")
                               for t in targets):
                return {"ok": True, "jobs": list(seen.values()), "elapsed": round(time.time() - t0, 2)}
            time.sleep(poll)
        return {"ok": False, "timeout": True, "jobs": list(seen.values()),
                "elapsed": round(time.time() - t0, 2)}


def _sse_lines(resp):
    """从 http.client 响应里按行读 SSE（标准库，无第三方）。"""
    buf = b""
    while True:
        chunk = resp.read(1024)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", "replace").rstrip("\r")


def read_endpoint(state_dir: str | None = None) -> dict:
    sd = Path(state_dir or default_state_dir())
    ep = sd / "endpoint.json"
    if not ep.exists():
        raise BridgeError(
            f"找不到 {ep}\n→ Bridge 没在跑。先执行：  python bridge.py\n"
            f"  （这一行命令就是启动，见 README；插件那边什么都不用填）")
    info = json.loads(ep.read_text(encoding="utf-8"))
    info.setdefault("httpHost", "127.0.0.1")
    # v0.2：WS 与 HTTP 合并在同一个端口。老字段名留着，新字段兜底，避免升级时读空。
    if "httpPort" not in info and "wsPort" in info:
        info["httpPort"] = info["wsPort"]
    return info


def default_state_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "chrome-dl-bridge")


# --------------------------------------------------------------------------
def human(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def fmt_row(j: dict) -> str:
    pct = ""
    if j.get("totalBytes"):
        pct = f"{100.0 * (j.get('bytesReceived') or 0) / j['totalBytes']:5.1f}%"
    return (f"{j['jobId'][:12]} {j.get('state','?'):<11} {pct:>6} "
            f"{human(j.get('bytesReceived')):>9}/{human(j.get('totalBytes')):<9} "
            f"{human((j.get('speedBps') or 0))+'/s':>10}  "
            f"{'[重头下]' if j.get('restarted') else ''}"
            f"{' ' + str(j.get('filename'))[:40]}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="chrome-dl", description="Agent 下载指挥（不碰字节）")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--token", default=None)
    p.add_argument("--state-dir", default=None)
    p.add_argument("--transport", choices=["sse", "ws", "auto"], default="sse",
                   help="0.1 兼容位：v0.2 起 Bridge 只有一个端口，进度统一走 SSE")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="提交一个下载")
    a.add_argument("url")
    a.add_argument("--name", default=None)
    a.add_argument("--sha256", default=None)
    a.add_argument("--wait", action="store_true")

    b = sub.add_parser("batch", help="批量提交：--file jobs.json 或多个 url")
    b.add_argument("urls", nargs="*")
    b.add_argument("--file", default=None)

    sub.add_parser("list", help="列出所有任务")
    s2 = sub.add_parser("show", help="看一个任务")
    s2.add_argument("job_id")

    pr = sub.add_parser("progress", help="查进度/网速（一次性）")
    pr.add_argument("job_id", nargs="?")

    w = sub.add_parser("watch", help="实时跟进度（SSE 流）")
    w.add_argument("--timeout", type=float, default=60.0)

    for name in ("pause", "resume", "cancel"):
        c2 = sub.add_parser(name)
        c2.add_argument("job_id")

    wa = sub.add_parser("wait", help="等一批任务到终态")
    wa.add_argument("job_ids", nargs="*")
    wa.add_argument("--timeout", type=float, default=600.0)

    sub.add_parser("stats", help="汇总")
    sub.add_parser("health", help="体检（含熔断拦截数）")

    args = p.parse_args(argv)
    cli = Client(host=args.host, port=args.port, token=args.token,
                 state_dir=args.state_dir, transport=args.transport)

    if args.cmd == "add":
        r = cli.add(args.url, args.name, args.sha256)
        job = r["job"]
        print(f"已提交 jobId={job['jobId']}（{'复用旧任务' if r.get('reused') else '新任务'}）")
        if args.wait:
            res = cli.wait([job["jobId"]])
            print(json.dumps(res["jobs"][0], ensure_ascii=False, indent=2))
            return 0 if res["ok"] and res["jobs"][0]["state"] == "COMPLETE" else 1
        return 0
    if args.cmd == "batch":
        jobs = []
        if args.file:
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            jobs = data if isinstance(data, list) else data.get("jobs", [])
        jobs += [{"url": u} for u in args.urls]
        res = cli.batch(jobs)
        for r in res:
            if r.get("ok"):
                # 打完整 jobId：列表里用短显可以，但这行输出要能被用户直接复制去 wait/show
                print(f"  ok  {r['job']['jobId']} {r['job']['filename']}")
            else:
                print(f" FAIL {r}")
        bad = sum(1 for r in res if not r.get("ok"))
        return 1 if bad else 0
    if args.cmd == "list":
        rows = cli.list()
        if not rows:
            print("（账本为空）")
        for j in rows:
            print(fmt_row(j))
        return 0
    if args.cmd == "show":
        print(json.dumps(cli.show(args.job_id), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "progress":
        d = cli.progress(args.job_id)
        if "jobs" in d:
            for j in d["jobs"]:
                print(fmt_row(j))
        else:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "watch":
        def cb(evt):
            print(fmt_row(evt), flush=True)
        cli.watch(cb, until={"COMPLETE", "FAILED", "CANCELLED"}, timeout=args.timeout)
        return 0
    if args.cmd in ("pause", "resume", "cancel"):
        print(json.dumps(getattr(cli, args.cmd)(args.job_id), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "wait":
        r = cli.wait(args.job_ids or None, timeout=args.timeout)
        for j in r["jobs"]:
            print(fmt_row(j))
        print(f"→ {'全部终态' if r['ok'] else '超时'}，用时 {r['elapsed']}s")
        return 0 if r["ok"] else 1
    if args.cmd == "stats":
        print(json.dumps(cli.stats(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "health":
        print(json.dumps(cli.health(), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    # 新手面：Bridge 没跑/口令错这类错必须只给人能懂的两三行，不甩 traceback。
    # （traceback 里那行提示虽然也在，但被一堆栈帧埋着，等于没提示）
    try:
        sys.exit(main())
    except BridgeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
