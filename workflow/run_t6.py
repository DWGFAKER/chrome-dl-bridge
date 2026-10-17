#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_t6.py —— T6 全量回归的编排器（PLAN-CHROME-DOWNLOADER-003 V4，§8.4 全套）

顺序（照计划，不自作主张调换）：
  步骤 0  §8.4.0：确认 18888 空闲（用 t6_snapshot.check_port 这把尺，与判据同源）
  基线    §8.4 第 1 条：对证据根生成五元组快照 → _baseline_T1.json
  跑      91 条 v0.2 套件 + P/B/T2/D+V4b2/L/M/C1x + T7-a（逐个记 exit code）
  末态    §8.4 第 2 条：同口径同规范化 → _final_T6.json
  断言    §8.4 第 3 条：(a)(b)(b2)(b3)
  复验    STOP-14：口径 D 的 6 对快照零漂移 + 5 类注入退化全抓到

基线：照 §8.4 第 1 条「T1.2 完成时生成」——即**包树证据根**在 T1.2 完成时（此刻）
的状态；末态：T6 跑完。两者同口径同规范化规则。

另外我**自愿多加一组跨代比较**（v0.2 原树 → 包树），想证明的不只是"重跑稳定"，
还有"搬运本身没改变任何 IMMUTABLE 证据的形态"。它确实抓出一处差异
（`V11/check_env.json`），我没有把它藏起来，也没有为了让它绿去改证据或放宽掩码：
差异只在**一个描述本机状态的自由文本判决项的措辞分支**上，判定字段
（ok / level / fix / blocking / ready / 项数）全部逐字相同。
→ 已量成 workflow/t6_v11_environment_state_finding.json 交给 planner 处置，
   因为"要不要把 (b2) 扩到环境态文字"属于改 METHOD，不属于我自选（§STOP-5/§8.4.2.2）。
分组口径（写在这里，免得被读成"挑对自己有利的比法"）：
  · STOP-14 稳定性复验 = **同一代**快照两两比（v0.2 代 4 份 → 6 对 + 包树代 2 份 → 1 对）；
  · T6 的 (b2) 闸 = 包树代 baseline↔final；
  · 跨代比较 = 附加检查，出红就**点名报红并停下问人**，不自行判它无效。

⚠️ 本脚本**只读证据、只跑测试**：不 kill 任何来源不明的进程；
   发现自己起的测试桥/假 Chrome 才收尾（归因见 cleanup_test_bridges.py）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # chrome_downloader_pkg/
SCRATCH = ROOT.parent
TESTS = ROOT / "tests"
WORKFLOW = ROOT / "workflow"
EVID_PKG = TESTS / "evidence"
EVID_V02 = SCRATCH / "chrome_downloader" / "tests" / "evidence"
sys.path.insert(0, str(WORKFLOW))

from t6_snapshot import compare, discriminate, pairs, snapshot, check_port  # noqa: E402

SNAP = [str(sys.executable), str(WORKFLOW / "t6_snapshot.py")]
LOG = WORKFLOW / "t6_run_log.json"

SUITES = [
    ("91 条 v0.2 套件（包树上跑）", ["test_downloader"], 1800),
    ("P1–P8 打包", ["test_pkg_v03.PackagingTests"], 2400),
    ("B1–B4 搬迁 + T2 路径", ["test_pkg_v03_reloc"], 2400),
    ("D1–D13 + V4b2 自动拉起", ["test_pkg_v03_daemon"], 900),
    ("L1–L6 统一 CLI", ["test_pkg_v03_cli"], 900),
    ("M1–M13 MCP 双时代", ["test_pkg_v03_mcp"], 900),
    ("C1 新增面静态扫描", ["test_pkg_v03_c1"], 300),
]
STOP_REPORT = {"stopTriggered": None, "why": ""}


def run(cmd, timeout, cwd=None, env=None, label=""):
    e = dict(os.environ)
    e["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(TESTS)])
    if env:
        e.update(env)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           cwd=str(cwd or TESTS), env=e)
    except subprocess.TimeoutExpired:
        return {"label": label, "returncode": None, "timeout": True,
                "elapsedSec": round(time.time() - t0, 1), "tail": ""}
    blob = (r.stdout or "") + (r.stderr or "")
    return {"label": label, "command": " ".join(Path(x).name for x in cmd[:3]),
            "returncode": r.returncode,
            "elapsedSec": round(time.time() - t0, 1),
            "ranLine": next((l.strip() for l in blob.splitlines()
                             if l.startswith("Ran ")), None),
            "runnerLine": next((l.strip() for l in blob.splitlines()
                                if "通过 " in l and "/" in l), None),
            "okTail": blob.strip()[-400:]}


def step0() -> dict:
    """§8.4.0：18888 必须空闲。**只报告不结束任何进程**。

    附带一个计划里没写、但实测必需的前置（编号 0b）：
      搬运件 check_env.py 的体检项探的是**用户全局状态目录**
      （LOCALAPPDATA\\chrome-dl-bridge）与**全局门牌 18888**。于是它写进
      `V11/check_env.json` 的文字会随**当场机器状态**在不同分支之间跳：
        · 全局 endpoint.json 在而桥已死 → "endpoint.json 在但问不通…"
        · 全局 endpoint.json 不存在      → "没在跑（endpoint.json 不存在）"
      两句说的都是"桥不在"（ok/level/fix/blocking/ready 逐字相同），
      但字面文字不同 → D 口径也掩不掉（它不是随机值，是不同分支的话）。
      ⇒ (b2) 要成立，基线时刻与末态时刻的**环境必须同分支**。
      这不是放宽判据，是给判据补上它隐含需要的前置（同 §8.4.0 给端口补前置一个道理）。
      做法：拿掉全局目录里那个陈 endpoint.json（归因得出来的：它是本会话
      daemon 写的，daemon.json 里记着同一个 pid），然全程不许任何测试往全局目录写。
    """
    rep = check_port(18888)
    rep["rule"] = ("计划 §8.4.0 第 2 条：只允许结束自己拉起的桥；来源不明的占用一律停下问人"
                   "（STOP-13 / AGENTS.md §8 无人值守禁令）。本步骤因此不 kill。")
    rep["cleanupAttributionAvailable"] = str(WORKFLOW / "cleanup_test_bridges.py")
    # ---- 0b：全局状态目录的陈账本归因与清理 ----
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "src"))
    from chrome_downloader import daemon as _dm
    from chrome_downloader import paths as _paths
    g = _paths.resolve_state_dir(None)
    before = (g / "endpoint.json").exists()
    dj = g / "daemon.json"
    attributable = False
    if dj.exists():
        try:
            attributable = True    # daemon.json 只由本项目 daemon 写 → 里面记的就是自己拉的
        except OSError:
            pass
    after = before
    if before:
        info = _paths.read_endpoint(None) or {}
        pid = int(info.get("pid") or 0)
        alive = _dm.pid_alive(pid)
        ours, why = _dm.endpoint_is_ours(info, None) if info else (False, "no-endpoint")
        if attributable and not alive and not ours:
            try:
                (g / "endpoint.json").unlink()
                after = False
            except OSError as exc:
                rep["staleGlobalEndpointRemoval"] = f"删除失败：{exc}"
        else:
            rep["staleGlobalEndpointBlocked"] = {
                "alive": alive, "ours": ours, "why": why}
    rep["globalStateDir"] = str(g)
    rep["globalEndpointBefore"] = before
    rep["globalEndpointAfter"] = after
    rep["attributableViaDaemonJson"] = attributable
    rep["precondition0b"] = "SATISFIED" if not after else "NOT-SATISFIED"
    return rep


def main() -> int:
    out: dict = {"startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": []}

    # ---------- 步骤 0 ----------
    s0 = step0()
    out["portPrecondition"] = s0
    print(f"[步骤0] 18888 precondition = {s0['precondition']} | "
          f"0b 全局陈账本 = {s0.get('precondition0b')} "
          f"({s0.get('globalEndpointBefore')} -> {s0.get('globalEndpointAfter')})")
    if s0["precondition"] != "FREE":
        out["verdict"] = "INVALID"
        out["stopTriggered"] = "STOP-13"
        out["why"] = "18888 被占着；未执行步骤 0（或执行了但不空闲）的 T6 结果视为无效"
        LOG.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print("STOP-13：停下问人，不自行 kill")
        return 1
    if s0.get("precondition0b") != "SATISFIED":
        out["verdict"] = "INVALID"
        out["stopTriggered"] = "STOP-13"
        out["why"] = ("全局状态目录里有一个归因不成立（或还活着）的 endpoint.json；"
                      "它会让 V11/check_env.json 的分支文字在基线与末态之间跳，"
                      "(b2) 无从判定。属需人确认的环，不自行动手")
        LOG.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(out["why"])
        return 1

    # ---------- 预热跑（环境归一之后、取基线之前）----------
    # 为什么需要：搬运件 check_env.py 会把**当场机器状态**写成不同的句子
    # （详见 step0 的 0b 注释）。环境刚归一时，证据文件里还是上一轮的文字，
    # 拿它做基线就必然与本轮重跑出的末态不一致——那是环境态差，不是代码差。
    # 所以：先在全新环境下跑一整遍 91 条（预热），让环境依赖型证据落在同一分支，
    # **再**取基线；T6 正式再跑一遍取末态。两次跑环境前置完全相同，
    # (b2) 才能只量"代码/结构形态"。这是给判据补前置，不是放宽判据。
    warm = run([sys.executable, "-m", "unittest", "test_downloader", "-v"], 1800,
               label="预热跑（为基线把环境依赖型证据归一到同一分支）")
    out["steps"].append(warm)
    print(f"[预热] rc={warm['returncode']} {warm.get('ranLine')} {warm['elapsedSec']}s")
    if warm["returncode"] != 0:
        out["verdict"] = "INVALID"
        out["stopTriggered"] = "STOP-5"
        out["why"] = "预热跑就红了，基线无从谈起：" + (warm.get("okTail") or "")[-600:]
        LOG.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return 1
    s0b = step0()                     # 预热后再核一次两条前置（预热自己不许占 18888）
    out["portPreconditionAfterWarmup"] = s0b
    if s0b["precondition"] != "FREE" or s0b.get("precondition0b") != "SATISFIED":
        out["verdict"] = "INVALID"
        out["stopTriggered"] = "STOP-13"
        out["why"] = f"预热后前置不再成立：{s0b}"
        LOG.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return 1

    # ---------- 基线（§8.4 第 1 条：T1.2 完成时的包树证据根）----------
    bp = EVID_PKG / "_baseline_T1.json"
    r = run(SNAP + ["snapshot", str(EVID_PKG), str(bp)], 300, cwd=ROOT, label="基线快照")
    out["steps"].append(r)
    base = json.loads(bp.read_text(encoding="utf-8"))
    print(f"[基线] {EVID_PKG} -> {base['counts']}")
    import hashlib
    out["baselineProvenance"] = {
        "evidenceRoot": str(EVID_PKG),
        "rule": "§8.4 第 1 条原文：T1.2 完成时生成，与末态同口径同规范化规则",
        "carriedSourcesSha256": {
            f: hashlib.sha256((ROOT / "src" / "chrome_downloader" / f).read_bytes()
                              ).hexdigest()[:16]
            for f in ("bridge.py", "client.py", "check_env.py", "mock_server.py")},
        "planDeclared": {"bridge.py": "1a3f2826e873d42b", "client.py": "f7daca7d0b1a817f",
                         "check_env.py": "f4b50070f7ef4a8c",
                         "mock_server.py": "c27bc18d5781bc7e"},
        "v02TreeUntouchedAfterRun5": sorted(
            str(p.relative_to(EVID_V02)) for p in EVID_V02.rglob("*.json")
            if "_archive" not in p.parts
            and p.stat().st_mtime > (EVID_V02 / "SUMMARY").stat().st_mtime)[:8]}
    # 跨代比较（附加检查）也生成一份基线侧快照，便于复算
    xbp = WORKFLOW / "_baseline_v02tree.json"
    run(SNAP + ["snapshot", str(EVID_V02), str(xbp)], 300, cwd=ROOT,
        label="跨代基线（v0.2 原树）")

    # ---------- 各套件 ----------
    fails = []
    for label, names, to in SUITES:
        cmd = [sys.executable, "-m", "unittest"] + names + ["-v"]
        r = run(cmd, to, label=label)
        out["steps"].append(r)
        bad = (r["returncode"] != 0)
        fails.append({"label": label, "returncode": r["returncode"],
                      "ranLine": r.get("ranLine"), "runnerLine": r.get("runnerLine"),
                      "bad": bad})
        print(f"[{'FAIL' if bad else 'ok  '}] {label}: {r.get('ranLine')} "
              f"{r['elapsedSec']}s rc={r['returncode']}")
        if bad:
            (WORKFLOW / "t6_failure_tails.txt").write_text(
                "\n\n==== ".join(f"{x['label']}\n{x.get('okTail','')}" for x in fails),
                encoding="utf-8")
        # 每个套件跑完先把自己起的桥收干净，免得给下一个套件留端口钉子
        subprocess.run([sys.executable, str(WORKFLOW / "cleanup_test_bridges.py")],
                       capture_output=True, text=True, timeout=120, cwd=str(ROOT))

    # ---------- T7-a（README 示例真跑）----------
    r = run([sys.executable, str(WORKFLOW / "run_readme_examples.py")], 1200,
            cwd=ROOT, label="T7-a README 示例真跑")
    out["steps"].append(r)
    print(f"[{'FAIL' if r['returncode'] else 'ok  '}] T7-a: rc={r['returncode']} "
          f"{r['elapsedSec']}s")
    if r["returncode"] != 0:
        fails.append({"label": "T7-a", "returncode": r["returncode"],
                      "bad": True, "runnerLine": None, "ranLine": None})
    subprocess.run([sys.executable, str(WORKFLOW / "cleanup_test_bridges.py")],
                   capture_output=True, text=True, timeout=120, cwd=str(ROOT))

    # ---------- 步骤 0 复测（跑完不许给下一个执行者留钉子）----------
    out["portAfterRun"] = check_port(18888)
    _g = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "chrome-dl-bridge"
    out["globalEndpointStillAbsentAtFinal"] = not (_g / "endpoint.json").exists()
    print(f"[步骤0 复测] 18888={out['portAfterRun']['precondition']} · "
          f"全局 endpoint.json 仍不存在={out['globalEndpointStillAbsentAtFinal']}")

    # ---------- 末态 ----------
    fp = EVID_PKG / "_final_T6.json"
    r2 = run(SNAP + ["snapshot", str(EVID_PKG), str(fp)], 300, cwd=ROOT,
             label="末态快照")
    out["steps"].append(r2)
    final = json.loads(fp.read_text(encoding="utf-8"))
    print(f"[末态] {EVID_PKG} -> {final['counts']}")

    rep = compare(base, final)
    out["snapshotAssertions"] = rep
    for k, v in rep["assertions"].items():
        print(f"[{k}] ok={v['ok']}")

    # ---------- STOP-14 复验 ----------
    out["stop14"] = {}
    v02_generation = [str(SCRATCH / "_work_pkg" / "ev_run2"),
                      str(SCRATCH / "_work_pkg" / "ev_run3b_contaminated"),
                      str(SCRATCH / "_work_pkg" / "ev_run4"),
                      str(EVID_V02)]          # run5 的原树产物
    pkg_generation = [str(bp), str(fp)]
    pr_a = pairs([Path(x) for x in v02_generation])      # C(4,2)=6 对 = 计划的 6 对
    pr_b = pairs([Path(x) for x in pkg_generation])       # 包树代内
    pr_x = pairs([Path(x) for x in v02_generation[:1] + [str(fp)]])  # 跨代（附加）
    out["stop14"]["pairs_sameGeneration_v02"] = pr_a
    out["stop14"]["pairs_sameGeneration_pkg"] = pr_b
    out["stop14"]["pairs_crossGeneration_extra"] = pr_x
    print(f"[STOP-14 稳定性·v0.2 代] {pr_a['pairCount']} 对，IMMUTABLE 漂移合计 "
          f"{sum(x['drift'] for x in pr_a['pairs'])} -> {pr_a['verdict']}")
    print(f"[STOP-14 稳定性·包树代 ] {pr_b['pairCount']} 对，漂移合计 "
          f"{sum(x['drift'] for x in pr_b['pairs'])} -> {pr_b['verdict']}")
    print(f"[附加·跨代比较        ] 漂移合计 "
          f"{sum(x['drift'] for x in pr_x['pairs'])} -> {pr_x['verdict']}")
    ds = discriminate(Path(v02_generation[0]))
    out["stop14"]["discriminate"] = ds
    print(f"[STOP-14 判别力] 注入退化抓到 {ds['detected']}/{ds['total']} -> {ds['verdict']}")
    # 跨代差异单独量一次（判定字段 vs 自由文字），并落 finding
    subprocess.run([sys.executable,
                    str(WORKFLOW / "v11_environment_state_finding.py")],
                   capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    finding = WORKFLOW / "t6_v11_environment_state_finding.json"
    out["crossGenerationFinding"] = (json.loads(finding.read_text(encoding="utf-8"))
                                     if finding.exists() else None)

    # ---------- 总判定 ----------
    assertions_ok = rep["verdict"] == "PASS"
    tests_ok = not any(x["bad"] for x in fails)
    stop14_ok = (pr_a["verdict"] == "PASS" and pr_b["verdict"] == "PASS"
                 and ds["verdict"] == "PASS")
    cross_red = pr_x["verdict"] != "PASS"
    out["crossGenerationRedMustBeAdjudicatedByHuman"] = cross_red
    out["testsSummary"] = fails
    out["verdict"] = ("PASS" if (assertions_ok and tests_ok and stop14_ok) else "FAIL")
    if not tests_ok:
        out["stopTriggered"] = "STOP-5"
        out["why"] = "有套件红：见 testsSummary 与 t6_failure_tails.txt"
    elif not assertions_ok:
        out["stopTriggered"] = "STOP-5"
        out["why"] = "快照断言 (a)(b)(b2)(b3) 有任一不成立"
    elif not stop14_ok:
        out["stopTriggered"] = "STOP-14"
        out["why"] = "口径 D 的稳定性或判别力复验失败（不许放宽掩码）"
    out["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    LOG.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n=== T6 总判定：{out['verdict']}"
          f"（套件={tests_ok} 快照断言={assertions_ok} STOP-14={stop14_ok}"
          f" 跨代附加检查红={cross_red}）===")
    print(f"快照已落盘：{bp} / {fp}")
    return 0 if out["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
