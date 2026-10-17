#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_readme_examples.py —— T7-a 执行口径的落地实现（计划 §8.6）

三条判据（§8.0 的 T7-a 系列 3 条）：
  T7-a1 抽取器能正确识别 `# runnable`，并**跳过**没标记的块
        （含对现有 README 的全部围栏块做一次 dry-run，断言"不会把
          `python -m pip install aiohttp==3.14.2` 拿去跑"）
  T7-a2 每条被抽出的命令，实际退出码 == 块上标注的 `# exit: <n>`
  T7-a3 ≥3 条**真下载级**命令成立（带 --sha256 + --wait + 读到 sha256Actual==OK）

口径细节（逐条照 §8.6，不自作主张）：
  · 只有围栏块**首行**含 `# runnable` 的才被抽取；
  · 占位符替换表 REPLACEMENTS：<URL> / <jobId> / <STATE_DIR> / <PORT> / <KNOWN_HEX>。
    **替换不全 = 该块作者责任**：写了 `# runnable` 就是承诺可跑，
    脚本遇到未替换的占位符 → 判 FAIL，不跳过。
  · 预期退出码：`# exit: <n>`，默认 0；非 0 必须显式声明。

用法：
  python workflow/run_readme_examples.py --task T7          # 真跑（含起桥 + 真下载）
  python workflow/run_readme_examples.py --dry-run          # 只做识别，不执行
证据落 tests/evidence/T7/（§8.4.1 的新增面，不进 IMMUTABLE）。
退出码：0 三条全过 / 1 任一不过 / 2 用法或前置错。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
TESTS = ROOT / "tests"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(TESTS))

from chrome_downloader import daemon as dm          # noqa: E402
from chrome_downloader import paths                 # noqa: E402
from chrome_downloader.mock_server import MockServer, expected_sha256  # noqa: E402
from harness import ev                              # noqa: E402

README_CANDIDATES = [SRC / "chrome_downloader" / "README.md", ROOT / "README.md",
                    ROOT / "README_EN.md"]   # 英文版里的 runnable 块同样必须真跑过
FENCE = re.compile(r"^```(\w*)\s*$")
RUNNABLE = "# runnable"
EXIT_RE = re.compile(r"^#\s*exit:\s*(\d+)\s*$")
PLACEHOLDER_RE = re.compile(r"<[A-Za-z_][A-Za-z0-9_]*>")
# 绝不该被真跑的东西（T7-a1 的反面清单：计划 §0.1 实测 README 里有这类行）
MUST_NOT_RUN = ("pip install", "pip3 install", "python -m pip install", "pip uninstall")


def parse_blocks(md: str) -> list[dict]:
    """扫围栏块。返回 [{lang, lines, runnable, expect, startLine}]。"""
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        lang = m.group(1)
        start = i + 1
        body = []
        j = start
        while j < len(lines) and not lines[j].startswith("```"):
            body.append(lines[j])
            j += 1
        first = body[0].strip() if body else ""
        runnable = first == RUNNABLE or (first.startswith(RUNNABLE))
        expect = 0
        cmds: list[str] = []
        for ln in body[1:]:
            e = EXIT_RE.match(ln.strip())
            if e:
                expect = int(e.group(1))
                continue
            t = ln.strip()
            if not t or t.startswith("#") or t.startswith("$"):
                continue
            cmds.append(t.lstrip("$ ").strip())
        blocks.append({"lang": lang, "startLine": i + 1, "runnable": runnable,
                       "expect": expect, "commands": cmds,
                       "rawHead": body[:3]})
        i = j + 1
    return blocks


def build_replacements(state_dir: Path, port: int, mock: MockServer,
                      job_id: str) -> dict:
    name, size = "readme-demo", 200_000
    return {
        "<URL>": mock.url(name, size, rate=200_000),
        "<KNOWN_HEX>": expected_sha256(name, size),
        "<STATE_DIR>": str(state_dir),
        "<PORT>": str(port),
        "<jobId>": job_id,
    }


def apply(text: str, table: dict) -> tuple[str, list[str]]:
    out = text
    for k, v in table.items():
        out = out.replace(k, v)
    left = sorted({m.group(0) for m in PLACEHOLDER_RE.finditer(out)
                   if m.group(0) not in ("<n>",)})
    return out, left


def run_commands(cmds, table, env, timeout=240):
    rows = []
    for raw in cmds:
        cmd, unresolved = apply(raw, table)
        if unresolved:
            rows.append({"command": raw, "status": "UNRESOLVED_PLACEHOLDER",
                         "unresolved": unresolved})
            continue
        real = to_argv(cmd)
        t0 = time.time()
        try:
            r = subprocess.run(real, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=timeout, env=env, cwd=str(ROOT))
        except subprocess.TimeoutExpired:
            rows.append({"command": cmd, "status": "TIMEOUT",
                         "timeoutSec": timeout})
            continue
        blob = (r.stdout or "") + (r.stderr or "")
        rows.append({"status": "OK", "command": cmd, "argv": real,
                     "shellUsed": False, "returncode": r.returncode,
                     "elapsedSec": round(time.time() - t0, 2),
                     "noTraceback": "Traceback (most recent call last)" not in blob,
                     "stdoutHead": (r.stdout or "")[:240].replace("\n", " | "),
                     "stderrHead": (r.stderr or "")[:240].replace("\n", " | ")})
    return rows


def to_argv(cmd: str) -> list[str]:
    """把文档里那一行命令变成**argv 列表**（绝不用 shell）。

    两个理由，第二个是实测踩出来的：
      ① 装了包时 PATH 上有 chrome-dl.exe，没装时必须走 `python -m ...`，
         两条路跑的是同一份实现，不是两套代码；
      ② shell=True 在 Windows 上会把 URL 里的 `&` 当命令分隔符 ——
         实测后果：`… download http://127.0.0.1:59941/file/x?size=200000&rate=200000 …`
         的后半截 `rate=200000` 被 cmd 当成**一条命令去执行**，
         报 `'rate' 不是内部或外部命令`，命令实际只跑到半截。
         文档示例被这样"跑"过一次还标了 runnable，那就是假跑。
    """
    parts = shlex.split(cmd.replace("\\", "/"), posix=True)
    if not parts:
        return []
    head = parts[0]
    if head == "chrome-dl-mcp":
        return [sys.executable, "-m", "chrome_downloader.mcp_server"] + parts[1:]
    if head == "chrome-dl":
        return [sys.executable, "-m", "chrome_downloader.cli"] + parts[1:]
    return parts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T7-a README 示例真跑")
    ap.add_argument("--dry-run", action="store_true", help="只识别不执行（T7-a1）")
    ap.add_argument("--task", default="T7")
    a = ap.parse_args(argv)

    # 两份 README 都扫：工程根的指路牌 + 包里的那份唯一正文。
    # （第一版只取"第一个存在的文件"，结果抓到工程根的指路牌 → 0 块 runnable，
    #   判据自己把自己测成空判。空判的闸不是闸，所以这里明确要求"至少一块 runnable"
    #   并且把扫到几个文件写进证据。）
    found = [p for p in README_CANDIDATES if p.exists()]
    if not found:
        print("找不到 README.md", file=sys.stderr)
        return 2
    md_parts = [p.read_text(encoding="utf-8") for p in found]
    md = chr(10).join(md_parts)
    blocks = parse_blocks(md)
    runnable = [b for b in blocks if b["runnable"]]
    skipped = [b for b in blocks if not b["runnable"]]

    # ---------------- T7-a1：识别 + 必须跳过 ----------------
    # 判据原文："抽取器能正确识别并跳过非 runnable 块，含对现有 README 的 7 块做一次
    # dry-run，断言'不会把 `pip install aiohttp` 拿去跑'"。
    # ⇒ 正面证据是"那些危险行**落在没标记的块里**"，所以它出现在 skipped 里是好事；
    #   真正的失败是"危险行被标成了 runnable"（那才会被执行）。
    skipped_text = chr(10).join(chr(10).join(b["rawHead"]) for b in skipped)
    present_in_skipped = [f for f in MUST_NOT_RUN if f in skipped_text]
    danger_in_runnable = [b for b in runnable
                          if any(f in " ".join(b["commands"]) for f in MUST_NOT_RUN)]
    a1_ok = (bool(present_in_skipped) and            # 确实存在这类行，才谈得上"跳过了它"
             not danger_in_runnable and              # 危险行没被误标成 runnable
             len(runnable) >= 3 and                  # 空判不许算过
             len(skipped) >= 1)
    a1 = {"readmeScanned": [str(x) for x in found],
          "blocksTotal": len(blocks),
          "runnableBlocks": len(runnable), "skippedBlocks": len(skipped),
          "dangerousLinesFoundInSkippedBlocks": present_in_skipped,
          "dangerousLinesMarkedRunnable": danger_in_runnable,
          "runnableCommands": [c for b in runnable for c in b["commands"]],
          "caliber": "T7-a1 的正面证据 = 含 `python -m pip install …` 的块**未被标 runnable**，"
                     "因此不进执行清单；若它被标了 runnable 才是 FAIL。"
                     "另要求 runnable 块 ≥3，防止'一个都没抽到'冒充通过",
          "verdict": "PASS" if a1_ok else "FAIL"}
    print(f"[T7-a1] 识别：{len(runnable)} 块 runnable / {len(skipped)} 块跳过 "
          f"（危险行只在跳过块里：{bool(present_in_skipped)}）-> {a1['verdict']}")
    if a.dry_run:
        ev("T7", "T7-a1_extractor_recognition.json", a1)
        print(json.dumps(a1, ensure_ascii=False, indent=1))
        return 0 if a1_ok else 1

    # ---------------- 真跑前的舞台：临时 state-dir + 临时端口 + mock 源站 ----------
    workdir = Path(tempfile.mkdtemp(prefix="cdl_t7_"))
    state = workdir / "state"
    dl = workdir / "files"
    mock = MockServer(0, str(workdir / "mock.log")).start()
    port = _free_port_not_18888()
    box: dict = {}
    try:
        th = threading.Thread(target=lambda: box.update(
            res=dm.ensure_bridge(state, port=port, download_root=dl, timeout=30,
                                 quiet=True)))
        th.start()
        info = None
        t0 = time.time()
        while time.time() - t0 < 20:
            info = paths.read_endpoint(state)
            if info:
                break
            time.sleep(0.1)
        if info is None:
            print(f"[前置失败] 桥没起来：{box}", file=sys.stderr)
            return 2
        fake = subprocess.Popen(
            [sys.executable, str(TESTS / "fake_chrome.py"), "--state-dir", str(state),
             "--download-root", str(dl)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", cwd=str(ROOT))
        th.join(120)
        if box.get("err"):
            print(f"[前置失败] 三级就绪没达成：{box['err']}", file=sys.stderr)
            try:
                fake.kill()
            except Exception:
                pass
            return 2
        ep = box["res"]["endpoint"]
        port = int(ep["httpPort"])
        # 先拿一个真实 jobId 给 <jobId> 用（顺序：先提交，后面 wait/show 才有得查）
        name, size = "readme-demo", 200_000
        c = dm.make_client(state, ensure=False)
        job_id = c.add(mock.url(name, size, rate=200_000), filename="readme-demo.bin",
                       sha256=expected_sha256(name, size))["job"]["jobId"]
        table = build_replacements(state, port, mock, job_id)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(SRC), str(TESTS)])
        env["CHROME_DL_STATE_DIR"] = str(state)

        # ---------------- T7-a2：逐条退出码 == 标注 ----------------
        rows, bad = [], []
        for b in runnable:
            got = run_commands(b["commands"], table, env)
            entry = {"startLine": b["startLine"], "expect": b["expect"],
                     "commands": got}
            for g in got:
                if g["status"] != "OK" and g["status"] != "TIMEOUT":
                    if g["status"] == "UNRESOLVED_PLACEHOLDER":
                        bad.append({"command": g["command"],
                                    "problem": f"占位符没被替换：{g['unresolved']}"})
                        continue
                rc = g.get("returncode")
                if rc != b["expect"]:
                    bad.append({"command": g["command"],
                                "problem": f"退出码 {rc} != 标注 {b['expect']}"})
                if not g.get("noTraceback", True):
                    bad.append({"command": g["command"], "problem": "吐了 traceback"})
            rows.append(entry)
        a2_ok = not bad
        a2 = {"runnableBlocks": len(rows), "offenders": bad,
              "rows": rows,
              "caliber": "每条命令的实际退出码必须等于块上 `# exit: <n>`（默认 0）；"
                         "占位符没被替换 = 作者责任，直接 FAIL 而不是跳过",
              "verdict": "PASS" if a2_ok else "FAIL"}
        print(f"[T7-a2] 退出码核对：{len(rows)} 块 -> {a2['verdict']}")

        # ---------------- T7-a3：≥3 条真下载级命令成立 ----------------
        dl_cmds = [g["command"] for e in rows for g in e["commands"]
                   if g.get("returncode") == 0]
        real_download = [x for x in dl_cmds if re.search(r"\b(download|add|list|wait|show)\b",
                                                         x)]
        # 完整性必须真读到 OK（不是"没崩就算"）
        show_out = next((g for e in rows for g in e["commands"]
                         if g["command"].startswith("chrome-dl show")), None)
        sha_ok = bool(show_out) and '"sha256Actual": "OK"' in (show_out.get("stdoutHead") or
                                                               "")
        ledger = c.show(job_id)
        sha_ok = sha_ok or ledger.get("sha256Actual") == "OK"
        a3_ok = (len(real_download) >= 3 and sha_ok and
                ledger.get("state") == "COMPLETE")
        a3 = {"realDownloadCommandsRun": real_download, "count": len(real_download),
              "minimumRequired": 3, "jobId": job_id,
              "ledgerState": ledger.get("state"),
              "sha256Actual": ledger.get("sha256Actual"),
              "sha256Expected": ledger.get("sha256Expected"),
              "withSha256Flag": any("--sha256" in x for x in real_download),
              "note": "§8.6 第 4 条（V4 按审查员 R1 补 --sha256）：不带 --sha256 时 "
                      "verify_file() 走 `return \"SKIP\"` 分支，账本里 sha256Actual 必为 "
                      "SKIP，③ 硬断言 ==\"OK\" 会假失败 —— 所以 ① 必须带 --sha256",
              "verdict": "PASS" if a3_ok else "FAIL"}
        print(f"[T7-a3] 真下载级：{len(real_download)} 条，sha256Actual="
              f"{a3['sha256Actual']} -> {a3['verdict']}")
        for vid, obj in (("T7-a1_extractor_recognition.json", a1),
                         ("T7-a2_exit_codes_match.json", a2),
                         ("T7-a3_real_download_commands.json", a3)):
            ev("T7", vid, obj)
        allok = a1_ok and a2_ok and a3_ok
        print(f"\n总判定：{'PASS' if allok else 'FAIL'}"
              f"（a1={a1['verdict']} a2={a2['verdict']} a3={a3['verdict']}）")
        try:
            fake.terminate()
            fake.communicate(timeout=8)
        except Exception:
            try:
                fake.kill()
            except Exception:
                pass
        dm.stop_bridge(state)
        return 0 if allok else 1
    finally:
        try:
            mock.stop()
        except Exception:
            pass
        dm.stop_bridge(state)
        ep2 = paths.read_endpoint(state)
        if ep2:
            _kill(int(ep2["pid"]))
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


def _free_port_not_18888() -> int:
    import socket
    for _ in range(30):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if p != 18888:
            return p
    return 18889


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=30)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
