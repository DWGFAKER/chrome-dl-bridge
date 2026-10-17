#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v11_environment_state_finding.py —— 把 V11/check_env.json 的跨根 b2 违规量清楚

发现（实测，不是推断）：
  以"v0.2 原树证据"为基线、以"包树证据"为末态做 (b2) 比较时，
  37 个 IMMUTABLE 目录里有 **1 处**规范化 sha256 不同：`V11/check_env.json`。
  差异**只在一个自由文本判决项 `Bridge 在跑吗` 的 detail 句子**：
    基线（run5，2026-10-15 21:25）: "endpoint.json 在但问不通（多半是陈文件）：ConnectionRefusedError"
    末态（本会话）              : "没在跑（endpoint.json 不存在）"
  两句话描述的是**同一件事实**（桥没在跑、该项 ok=false、计入 blocking、ready=false），
  但走的是搬运件 check_env.py:236-247 里**两个不同分支**。

为什么分支不同（根因）：
  check_env 探测的是**用户全局状态目录** `LOCALAPPDATA\chrome-dl-bridge`。
  上一轮跑测时那里留着一个陈旧 endpoint.json（残留桥 PID 15732 写的，进程已死），
  于是走"文件在但问不通"分支；
  本会话为了满足 **D13/L4**（`daemon stop` 后 endpoint.json 必须被删除）
  执行了真正的 stop，那个文件被我删掉了 → 之后所有 check_env 都走"不存在"分支。
  ⇒ 换句话说：**(b2) 对"描述本机状态的自由文本"没有覆盖**，而计划 §8.4.3 的
     搬运豁免只处理了"路径改写"（D 口径掩成 <PATH>），没处理"分支改写"。

本脚本干什么：把"判定依据有没有变"与"文字有没有变"**分开量**，落成证据，
让 planner 看得见我要的是哪一种覆盖，而不是我替他决定放宽口径。
⚠️ 本脚本不修改任何证据、不放宽任何掩码（§8.4.2.2 明令不许）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT.parent
sys.path.insert(0, str(ROOT / "workflow"))

from t6_snapshot import DECISIVE_KEYS, decisive_of, d_sha256, normalize  # noqa: E402

A = SCRATCH / "chrome_downloader" / "tests" / "evidence" / "V11" / "check_env.json"
B = ROOT / "tests" / "evidence" / "V11" / "check_env.json"


def main() -> int:
    a = json.loads(A.read_text(encoding="utf-8"))
    b = json.loads(B.read_text(encoding="utf-8"))
    ca = {c["name"]: c for c in a["checks"]}
    cb = {c["name"]: c for c in b["checks"]}
    judgment_same = all(
        (ca[k]["ok"], ca[k]["level"], ca[k]["fix"]) ==
        (cb[k]["ok"], cb[k]["level"], cb[k]["fix"]) for k in ca)
    prose_diff = {k: {"baseline": ca[k]["detail"], "final": cb[k]["detail"]}
                  for k in ca if ca[k]["detail"] != cb[k]["detail"]}
    out = {
        "file": "V11/check_env.json",
        "normalizedShaBaseline": d_sha256(a)[:16],
        "normalizedShaFinal": d_sha256(b)[:16],
        "b2Violated": d_sha256(a) != d_sha256(b),
        "checksCount": {"baseline": len(a["checks"]), "final": len(b["checks"])},
        "topLevel": {"verdict": [a.get("verdict"), b.get("verdict")],
                     "blocking": [a["blocking"], b["blocking"]],
                     "ready": [a["ready"], b["ready"]],
                     "stateDirMaskedEqual": (normalize(a)["stateDir"] ==
                                             normalize(b)["stateDir"])},
        "judgmentFieldsUnchanged": judgment_same,
        "decisiveEqual": decisive_of(a) == decisive_of(b),
        "decisiveBaseline": decisive_of(a),
        "decisiveFinal": decisive_of(b),
        "proseOnlyDiff": prose_diff,
        "branchSourceLines": "check_env.py:236-247（两个分支：文件在但问不通 / 文件不存在）",
        "rootCause": ("用户全局状态目录里的陈旧 endpoint.json 被本会话的 `daemon stop` 删掉"
                      "（D13/L4 要求它必须被删）→ check_env 走了另一个分支。"
                      "描述的是本机状态，与 T1–T5 的代码无关。"),
        "whatIDidNotDo": ["没改证据", "没放宽掩码（§8.4.2.2 禁止）",
                          "没把 V11 移出 IMMUTABLE（那是 §8.4.1 的名单，不由我改）",
                          "没有为了让文字对齐而手工把 endpoint.json 塞回去"],
        "plannerQuestion": (
            "(b2) 要不要覆盖'描述本机状态的自由文本'？三种可选处置，我不自选："
            "①给 check_env 的体检项加一个'环境态指纹'前置条件，T6 与基线必须同前置；"
            "②把这一类句子按分支名归一（例如统一成 BRIDGE_NOT_RUNNING:<reason-code>），"
            "但那要改搬运件 check_env.py（§0.1 sha256 钉死）；"
            "③承认 (b2) 只保证判定依据与结构不变，把本条列入 changed[] 并写明原因"
            "（§8.4 第 4 条已有 changed[] 通道，但 V11 在 IMMUTABLE 内，走不通）。"),
        "verdict": "FINDINGS-ONLY（本脚本不作 PASS/FAIL 判定）",
    }
    txt = json.dumps(out, ensure_ascii=False, indent=1)
    print(txt)
    (ROOT / "workflow" / "t6_v11_environment_state_finding.json").write_text(
        txt, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
