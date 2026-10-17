#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
t6_snapshot.py —— PLAN-CHROME-DOWNLOADER-003 V4 §8.4 的基线/末态快照与四断言判定器

口径**逐条照计划实现**，不自作主张：
  §8.4   第 1/2 条：五元组 (relpath, sha256, top-level verdict|null, decisive{}, immutable)
  §8.4.0 步骤 0：T6 前 18888 必须空闲（本脚本 --check-port 只做"报告"，kill 由人/入口做）
  §8.4.1 IMMUTABLE = 37 个目录（V4：38 去掉 SUMMARY，见 §8.4.4）；NOT-IMMUTABLE = D*/M*/L*/B*/V4b2/…
  §8.4.2 DECISIVE_KEYS（**V4 已删 otherStateDir**，§8.4.2.4）+ 9 个噪声键排除
  §8.4.2.1 规范化口径 **D**（V4 采纳，替换 V3 原文）：
        None/int/float → "<NUM>"；bool 原样保留；字符串按序掩 <HEX>/<PATH>/<LOOP>/<FLOAT>/<NUM>
        保留键名、字典结构、数组长度
  §8.4.2.2 两条硬约束：**None 与数值必须同类**；**不许为抓小整数放宽掩码**
  §8.4.2.3 (b2) 责任边界：D 口径是"内容形态闸"，不背常量回归（由 W1/W17 兜底）
  §8.4.3 搬运豁免：路径被掩成 <PATH> 自然吸收；verdict / decisive 仍逐条核
  §8.5   ERROR/FAIL 二分

用法：
  python t6_snapshot.py snapshot <evidence_root> <out.json>
  python t6_snapshot.py compare <baseline.json> <final.json> <out_verdict.json>
  python t6_snapshot.py pairs <root1> <root2> ...        # STOP-14：两两算 D 口径漂移
  python t6_snapshot.py check-port [18888]               # §8.4.0 步骤 0 的前置检查
退出码：0 判定通过 / 1 判定不通过（含 STOP-5 / STOP-14 条件）/ 2 用法或路径错。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import socket
import sys
from pathlib import Path

# ---------------------------------------------------------------- §8.4.1
IMMUTABLE_DIRS = {
    "C1", "F8", "TE1x", "UNVERIFIED",
    "V1", "V2", "V3", "V4a", "V4b", "V4c", "V4e", "V5", "V6", "V7", "V7x", "V8", "V8p",
    "V9", "V10", "V11", "V1p",
    "W1", "W3", "W4", "W5", "W6", "W9", "W10", "W11", "W13", "W14", "W14b", "W15",
    "W16", "W17", "W19", "W20",
}                                  # 37 个（V4：SUMMARY 已移出，§8.4.4）
EXCLUDE_DIRS = {"_archive_001", "_archive_001_nm"}
NOT_IMMUTABLE_PREFIX = ("D", "M", "L", "B")
SELF_FILES = {"_baseline_T1.json", "_final_T6.json"}

# ---------------------------------------------------------------- §8.4.2
DECISIVE_KEYS = {
    "rotates", "oldProofReplayedAccepted", "linkGone", "auditHasBadFrame", "dropped",
    "blocked", "restarted", "sha256Actual", "state", "finalState",
    "beforeEqualsAfterByteForByte", "nonTerminalJobs", "reloadedPendingReconcile",
    "auditHasFabricated", "auditHasReplace", "newKeyDiffers",
    "serverTotalServed", "fileSize", "bytesReceived",
    "proofOfWorkOK", "nonLoopbackUrlCount", "failures", "ok",
}                                  # ← V4：otherStateDir 已删（§8.4.2.4）
DECISIVE_PATH_KEYS = {"egressGuard.enabled"}
NOISE_KEYS = {"ts", "timestamp", "generatedAt", "createdAt", "updatedAt",
              "auditTail", "audit", "job", "health"}
SCALARS = (bool, int, float, str)

# ---------------------------------------------------------------- §8.4.2.1 口径 D
RE_HEX = re.compile(r"[0-9a-fA-F]{16,}")
RE_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\u4e00-\u9fff]*")
RE_POSIX_PATH = re.compile(r"/(?:tmp|var|usr|home|opt|proc)/[^\s\"']*")
RE_LOOP = re.compile(r"127\.0\.0\.1:\d+")
RE_FLOAT = re.compile(r"\d+\.\d+")
RE_INT = re.compile(r"\d+")

HEX_MASK, PATH_MASK, LOOP_MASK, NUM_MASK = "<HEX>", "<PATH>", "<LOOP>", "<NUM>"


def mask_str(s: str) -> str:
    """字符串按 §8.4.2.1 的**顺序**替换（顺序会影响结果，不许打乱）。"""
    s = RE_HEX.sub(HEX_MASK, s)
    s = RE_WIN_PATH.sub(PATH_MASK, s)
    s = RE_POSIX_PATH.sub(PATH_MASK, s)
    s = RE_LOOP.sub(LOOP_MASK, s)
    s = RE_FLOAT.sub(NUM_MASK, s)
    s = RE_INT.sub(NUM_MASK, s)
    return s


def mask_scalar(v):
    if v is None or isinstance(v, (int, float)) and not isinstance(v, bool):
        return NUM_MASK                      # §8.4.2.2 硬约束 1：None 与数值同类
    if isinstance(v, bool):
        return v                             # bool 原样保留（判定力主要来源）
    if isinstance(v, str):
        return mask_str(v)
    return v


def normalize(obj):
    """递归：剔噪声键 + 掩随机值，**保留键名、字典结构与数组长度**。"""
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in sorted(obj.items()) if k not in NOISE_KEYS}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    return mask_scalar(obj)


def canon_bytes(obj) -> bytes:
    return json.dumps(normalize(obj), sort_keys=True, ensure_ascii=False).encode("utf-8")


def d_sha256(obj) -> str:
    return hashlib.sha256(canon_bytes(obj)).hexdigest()


# ---------------------------------------------------------------- DECISIVE 抽取
def decisive_of(obj) -> dict:
    """顶层 + 一层嵌套内，字段名 ∈ DECISIVE_KEYS 且值为标量 → 收集（§8.4.2）。

    ⚠️ decisive 的**值不掩码**（V4 采用"删 otherStateDir"而不是"掩 decisive"，
       §8.4.2.4 明文：掩码方案只记录备查）。
    """
    out: dict[str, object] = {}

    def scan(d, prefix, depth):
        if not isinstance(d, dict):
            return
        for k, v in d.items():
            path = f"{prefix}{k}"
            if isinstance(v, SCALARS) and k in DECISIVE_KEYS:
                out[path] = v
            if depth < 1:
                if isinstance(v, dict):
                    scan(v, path + ".", depth + 1)
                elif isinstance(v, list):
                    for i, it in enumerate(v):
                        if isinstance(it, dict):
                            scan(it, f"{path}[{i}].", depth + 1)

    scan(obj, "", 0)
    for p in sorted(DECISIVE_PATH_KEYS):          # egressGuard.enabled 这类带点路径
        cur = obj
        for part in p.split("."):
            if not isinstance(cur, dict) or part not in cur:
                break
            cur = cur[part]
        else:
            if isinstance(cur, SCALARS):
                out[p] = cur
    return dict(sorted(out.items()))


def immutable_for(rel: str) -> bool:
    first = rel.split("/")[0]
    if first in EXCLUDE_DIRS or first in IMMUTABLE_DIRS:
        return first in IMMUTABLE_DIRS
    if first in ("SUMMARY", "TIMEOUT"):
        return False
    if first.startswith(NOT_IMMUTABLE_PREFIX) or first == "V4b2":
        return False
    return False


def iter_evidence(root: Path):
    for p in sorted(root.rglob("*.json")):
        rel = p.relative_to(root).as_posix()
        first = rel.split("/")[0]
        if first in EXCLUDE_DIRS or p.name in SELF_FILES:
            continue
        yield rel, p


def snapshot(root: Path) -> dict:
    recs: dict[str, dict] = {}
    parse_errors: dict[str, str] = {}
    for rel, p in iter_evidence(root):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            parse_errors[rel] = f"{type(exc).__name__}: {exc}"
            recs[rel] = {"sha256": None, "verdict": None, "decisive": {},
                         "immutable": immutable_for(rel), "rawBytes": p.stat().st_size,
                         "parseError": str(exc)[:160]}
            continue
        verdict = obj.get("verdict") if isinstance(obj, dict) else None
        recs[rel] = {
            "sha256": d_sha256(obj),
            "verdict": verdict if verdict in ("PASS", "FAIL") else None,
            "decisive": decisive_of(obj),
            "immutable": immutable_for(rel),
            "rawBytes": p.stat().st_size,
        }
    return {
        "meta": {
            "generatedAtUtc": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generatorVersion": "t6_snapshot.py v1 (PLAN-CHROME-DOWNLOADER-003 V4)",
            "sourceRoot": str(root),
            "excludeRules": ["_archive_001/", "_archive_001_nm/",
                             "_baseline_T1.json", "_final_T6.json"],
            "normalizeRule": "§8.4.2.1 口径 D：剔 9 噪声键 → None/int/float=<NUM>、"
                             "bool 原样、字符串按序掩 <HEX>/<PATH>/<LOOP>/<FLOAT>/<NUM>，"
                             "保留键名/字典结构/数组长度 → "
                             "json.dumps(sort_keys=True, ensure_ascii=False) utf-8 取 sha256",
            "immutableRule": "§8.4.1：第一段目录名 ∈ 37 目录集合（V4 已移出 SUMMARY）",
            "decisiveRule": "§8.4.2 + §8.4.2.4：顶层+一层嵌套，字段名 ∈ DECISIVE_KEYS"
                            "（已删 otherStateDir），值不掩码",
            "responsibilityBoundary": "§8.4.2.3：(b2) 是内容形态闸，不背常量回归"
                                      "（minimum_chrome_version / MAX_FRAME_BYTES 由 W1/W17 兜底）",
        },
        "counts": {
            "files": len(recs),
            "pass": sum(1 for r in recs.values() if r["verdict"] == "PASS"),
            "null": sum(1 for r in recs.values() if r["verdict"] is None),
            "fail": sum(1 for r in recs.values() if r["verdict"] == "FAIL"),
            "immutableFiles": sum(1 for r in recs.values() if r["immutable"]),
            "immutableDirs": len({k.split("/")[0] for k, r in recs.items() if r["immutable"]}),
            "decisiveEmpty": sum(1 for r in recs.values() if not r["decisive"]),
            "parseErrors": len(parse_errors),
        },
        "files": recs,
    }


RANK = {"PASS": 2, "FAIL": 0}


def rank(v):
    return RANK.get(v, 1)


def compare(base: dict, final: dict) -> dict:
    bk, fk = set(base["files"]), set(final["files"])
    a_ok = bk <= fk
    missing = sorted(bk - fk)
    b_bad = [(k, base["files"][k]["verdict"], final["files"][k]["verdict"])
             for k in sorted(bk & fk)
             if rank(final["files"][k]["verdict"]) < rank(base["files"][k]["verdict"])]
    b2_bad = []
    for k in sorted(bk & fk):
        rb, rf = base["files"][k], final["files"][k]
        if rb["immutable"] and rf["immutable"] and rb["sha256"] != rf["sha256"]:
            b2_bad.append({"file": k, "baseline": rb["sha256"], "final": rf["sha256"]})
    b3_bad = []
    for k in sorted(bk & fk):
        db, df = base["files"][k]["decisive"], final["files"][k]["decisive"]
        if db != df:
            b3_bad.append({"file": k, "diff": {p: [db.get(p), df.get(p)]
                                               for p in set(db) | set(df)
                                               if db.get(p) != df.get(p)}})
    # (c) 派生量只记录，不作门槛
    changed = [k for k in sorted(bk & fk)
               if base["files"][k]["sha256"] != final["files"][k]["sha256"]
               and not base["files"][k]["immutable"]]
    added = sorted(fk - bk)
    allok = a_ok and not b_bad and not b2_bad and not b3_bad
    return {
        "assertions": {
            "(a)_keys_only_grow": {"ok": a_ok, "missingFromFinal": missing},
            "(b)_verdict_not_downgraded": {"ok": not b_bad, "violations": b_bad},
            "(b2)_immutable_normalized_sha_stable":
                {"ok": not b2_bad, "violations": b2_bad,
                 "scope": "IMMUTABLE ∩ baseline ∩ final（§8.4.2.3：内容形态闸，不背常量回归）"},
            "(b3)_decisive_stable": {"ok": not b3_bad, "violations": b3_bad,
                                     "note": "§8.4.2.4：decisive{} 为空的记录是恒真空判，"
                                             "覆盖由 (b2) 兜底"},
        },
        "derived_only_(c)": {
            "filesBaseline": len(bk), "filesFinal": len(fk),
            "addedKeys": added, "changedOutsideImmutable": changed,
            "passFinal": sum(1 for r in final["files"].values() if r["verdict"] == "PASS"),
            "immutableDirsFinal": len({k.split("/")[0] for k, r in final["files"].items()
                                       if r["immutable"]}),
        },
        "verdict": "PASS" if allok else "FAIL",
        "stopTriggered": None if allok else "STOP-5",
    }


def pairs(roots: list[Path]) -> dict:
    """STOP-14 复验：N 份快照两两比 D 口径 sha256，IMMUTABLE 上的漂移必须为 0。

    口径：§8.4.2.1 说的就是"**58 个 IMMUTABLE 文件**"（V4 按 §8.4.4 把 SUMMARY
    移出 IMMUTABLE 后为 **57 个**）。SUMMARY 是派生视图（内嵌 mtime/bytes），
    逐次必变属**预期**，所以它只记入 infoOnlyDrift，不计入漂移判定。
    """
    snaps: dict[str, dict[str, str]] = {}
    immut: dict[str, bool] = {}
    for i, r in enumerate(roots):
        tag = f"#{i}:{r.name}"
        vals = {}
        for rel, p in iter_evidence(r):
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            immut.setdefault(rel, immutable_for(rel))
            vals[f"{tag}|{rel}"] = d_sha256(obj)
        snaps[tag] = vals
    names = list(snaps)
    rels = sorted({k.split("|", 1)[1] for v in snaps.values() for k in v})
    rows = []
    for a, b in itertools.combinations(names, 2):
        shared = [r for r in rels if f"{a}|{r}" in snaps[a] and f"{b}|{r}" in snaps[b]]
        imm_shared = [r for r in shared if immut.get(r)]
        drift = [r for r in imm_shared if snaps[a][f"{a}|{r}"] != snaps[b][f"{b}|{r}"]]
        info_only = [r for r in shared if not immut.get(r)
                     and snaps[a][f"{a}|{r}"] != snaps[b][f"{b}|{r}"]]
        only = [r for r in rels if (f"{a}|{r}" in snaps[a]) != (f"{b}|{r}" in snaps[b])]
        rows.append({"pair": f"{a} ↔ {b}", "comparedImmutable": len(imm_shared),
                     "drift": len(drift), "driftFiles": drift[:20],
                     "infoOnlyDrift": info_only[:20],
                     "keySetsDiffer": only[:20]})
    bad = [r for r in rows if r["drift"] > 0]
    return {"scope": "IMMUTABLE（§8.4.1 的 37 目录 / 57 文件）；SUMMARY 只记不拦（§8.4.4）",
            "snapshots": {k: len(v) for k, v in snaps.items()},
            "universalKeys": len(rels),
            "universalImmutableKeys": sum(1 for r in rels if immut.get(r)),
            "pairCount": len(rows), "pairs": rows,
            "verdict": "PASS" if not bad else "FAIL",
            "stopTriggered": None if not bad else "STOP-14"}


# ---------------------------------------------------------------- STOP-14 判别力
# 计划 §8.4.2.1 的 5 类"真退化"：注入后规范化 sha256 **必须变**。
# 抓不到任何一类 → STOP-14 触发，且**不许放宽掩码**（§8.4.2.2 已证放宽会把漂移抬到 7/9/22）。
MUTATIONS = [
    ("V4a/V4a_static_scan.json", "V4a 新增一条未豁免的取字节/connect 命中",
     lambda d: dict(d, hits=[{"file": "daemon.py", "line": 7, "rule": "unexempt-connect",
                              "text": "sock.connect(target)"}] + list(d.get("hits") or []))),
    ("W13/W13_nonce_replay.json", "W13 老证明被接受（布尔翻转）",
     lambda d: {k: ((not v) if isinstance(v, bool) else v) for k, v in d.items()}),
    ("V4b/V4b_egress_selftest.json", "V4b 有一次出站没被拦（blocked=false）",
     lambda d: dict(d, attempts=[dict(d["attempts"][0], blocked=False)] +
                              list(d["attempts"][1:])) if d.get("attempts") else None),
    ("W14b/W14b_impostor_vs_real_bridge.json", "W14b 冒充者拿到控制面",
     lambda d: dict(d, impostorGotControlPlane=True)),
    ("C1/C1_extension_network_surface.json", "C1 扩展多了外网 fetch 目标",
     lambda d: dict(d, fetchTargets=sorted(set(d.get("fetchTargets") or []) |
                                          {"https://evil.example/x"}))),
]


def discriminate(root: Path) -> dict:
    rows = []
    for rel, label, fn in MUTATIONS:
        p = root / rel
        if not p.exists():
            rows.append({"case": label, "file": rel, "detected": None,
                         "note": "证据文件不在场，无法注入"})
            continue
        try:
            base = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            rows.append({"case": label, "file": rel, "detected": None,
                         "note": f"读不动：{type(exc).__name__}"})
            continue
        mut = fn(base)
        if mut is None:
            rows.append({"case": label, "file": rel, "detected": None,
                         "note": "注入前置字段缺失"})
            continue
        h0, h1 = d_sha256(base), d_sha256(mut)
        rows.append({"case": label, "file": rel, "detected": h0 != h1,
                     "baselineSha": h0[:16], "mutatedSha": h1[:16],
                     "verdictAlsoChanged": (isinstance(mut, dict) and
                                            mut.get("verdict") != base.get("verdict"))})
    undetected = [r for r in rows if r.get("detected") is False]
    untestable = [r for r in rows if r.get("detected") is None]
    return {"root": str(root), "cases": rows,
            "detected": sum(1 for r in rows if r.get("detected") is True),
            "total": len(rows), "undetected": [r["case"] for r in undetected],
            "untestable": [r["case"] for r in untestable],
            "verdict": "PASS" if not undetected and not untestable else "FAIL",
            "stopTriggered": None if not undetected and not untestable else "STOP-14"}


def check_port(port: int = 18888) -> dict:
    """§8.4.0 步骤 0：只**报告**，不 kill。结束进程归人或入口做（STOP-13）。"""
    out = {"port": port, "listening": None, "ownerPid": None,
           "netstatLines": [], "precondition": "UNKNOWN"}
    with socket.socket() as s:
        s.settimeout(0.4)
        try:
            out["listening"] = (s.connect_ex(("127.0.0.1", port)) == 0)
        except OSError as exc:
            out["listening"] = f"probe-error:{type(exc).__name__}"
    if not out["listening"]:
        # 再确认一次：连不上不等于没人占（TIME_WAIT 也可能），用 netstat 看 LISTENING
        import subprocess
        r = subprocess.run(f'netstat -ano | findstr :{port}', shell=True,
                           capture_output=True, text=True, timeout=30)
        lines = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
        out["netstatLines"] = lines[:10]
        listening = [l for l in lines if "LISTENING" in l.upper()]
        out["ownerPid"] = listening[0].split()[-1] if listening else None
        out["precondition"] = "FREE" if not listening else "OCCUPIED"
    else:
        out["precondition"] = "OCCUPIED"
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    op = argv[0]
    if op == "snapshot" and len(argv) == 3:
        root = Path(argv[1])
        if not root.is_dir():
            print(f"evidence root 不存在：{root}", file=sys.stderr)
            return 2
        snap = snapshot(root)
        out = Path(argv[2])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(snap["counts"], ensure_ascii=False))
        return 0
    if op == "compare" and len(argv) == 4:
        base = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        final = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        rep = compare(base, final)
        rep["baseline"] = {"sourceRoot": base["meta"]["sourceRoot"],
                           "generatedAtUtc": base["meta"]["generatedAtUtc"],
                           "counts": base["counts"]}
        rep["final"] = {"sourceRoot": final["meta"]["sourceRoot"],
                        "generatedAtUtc": final["meta"]["generatedAtUtc"],
                        "counts": final["counts"]}
        Path(argv[3]).parent.mkdir(parents=True, exist_ok=True)
        Path(argv[3]).write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        print(json.dumps({k: v["ok"] for k, v in rep["assertions"].items()},
                         ensure_ascii=False), "->", rep["verdict"],
              rep["stopTriggered"] or "")
        return 0 if rep["verdict"] == "PASS" else 1
    if op == "pairs":
        roots = [Path(a) for a in argv[1:]]
        for r in roots:
            if not r.is_dir():
                print(f"快照目录不存在：{r}", file=sys.stderr)
                return 2
        rep = pairs(roots)
        if len(argv[1:]) >= 2 and Path(argv[-1]).suffix == ".json":
            pass
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep["verdict"] == "PASS" else 1
    if op == "pairs-json" and len(argv) >= 3:
        roots = [Path(a) for a in argv[1:-1]]
        rep = pairs(roots)
        Path(argv[-1]).write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(json.dumps({r["pair"]: r["drift"] for r in rep["pairs"]}, ensure_ascii=False))
        return 0 if rep["verdict"] == "PASS" else 1
    if op == "discriminate" and len(argv) >= 2:
        rep = discriminate(Path(argv[1]))
        if len(argv) == 3:
            Path(argv[2]).write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        print(json.dumps({"detected": f"{rep['detected']}/{rep['total']}",
                          "undetected": rep["undetected"],
                          "untestable": rep["untestable"],
                          "verdict": rep["verdict"]}, ensure_ascii=False))
        return 0 if rep["verdict"] == "PASS" else 1
    if op == "check-port":
        port = int(argv[1]) if len(argv) > 1 else 18888
        rep = check_port(port)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep["precondition"] == "FREE" else 1
    print(f"用法错：{argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
