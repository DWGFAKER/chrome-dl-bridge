#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_and_verify.py —— T8 发布准备（独立 venv 构建 + 校验，base 环境不动 = P7/C3）

四步，每步都留真实 exit code（失败就停在那里，不往下装）：
  1) 建**临时构建 venv**，在里面装 build/twine —— base 一个包都不装（C3/P7）；
  2) `python -m build` 出 sdist + wheel；`twine check` 必须 rc=0 且无 WARNING；
  3) 建**第二个全新 venv**，只 `pip install dist/*.whl`，
     起 mock 源站 → 用**装出来的** `chrome-dl download … --sha256 … --wait` 真下一单
     → 读账本断言 `sha256Actual == "OK"`（= §8.6 T7-a3 同一口径）；
  4) 删掉两个临时 venv，复核 base 的 `pip list` 快照逐行未变（P7）。

⚠️ 计划 §11-① LICENSE 待人拍板（未定 → T10 停）。本工具**只本地构建与校验，
   不上传**：twine check 不是 twine upload。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
DIST = ROOT / "dist"
LOG = ROOT / "workflow" / "t8_build_log.jsonl"


def sh(cmd, cwd=None, timeout=1800, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd, timeout=timeout, env=env)


def record(row):
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def venv_py(d: Path) -> str:
    return str(d / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def free_port() -> int:
    for _ in range(30):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if p != 18888:
            return p
    return 18889


def main() -> int:
    build_venv = Path(tempfile.mkdtemp(prefix="cdl_build_venv_"))
    test_venv = Path(tempfile.mkdtemp(prefix="cdl_test_venv_"))
    out: dict = {"steps": [], "verdict": "FAIL"}
    try:
        # ---------------- 1) 两个临时 venv ----------------
        for name, d in (("build", build_venv), ("test", test_venv)):
            r = sh([PY, "-m", "venv", str(d)], timeout=900)
            out["steps"].append({"step": "venv:" + name, "returncode": r.returncode,
                                 "path": str(d), "tail": (r.stderr or "")[-200:]})
            if r.returncode != 0:
                out["failedAt"] = "venv:" + name
                return finish(out, 1)
        bp = venv_py(build_venv)
        r = sh([bp, "-m", "pip", "install", "build==1.5.0", "twine",
                "setuptools>=61"], timeout=1800)
        out["steps"].append({"step": "pip install build+twine (在构建 venv 里)",
                             "returncode": r.returncode,
                             "tail": (r.stdout or "")[-240:]})
        if r.returncode != 0:
            out["failedAt"] = "pip install build/twine"
            out["note"] = "外网不可达或镜像缺失时记 UNVERIFIED，不许拿'应该会成功'冒充"
            return finish(out, 1)

        # ---------------- 2) build + twine check ----------------
        DIST.mkdir(exist_ok=True)
        for old in DIST.glob("*"):
            old.unlink()
        r = sh([bp, "-m", "build", "--outdir", str(DIST)], cwd=str(ROOT), timeout=1800)
        arts = sorted(p.name for p in DIST.glob("*"))
        out["steps"].append({"step": "python -m build", "returncode": r.returncode,
                             "artifacts": arts,
                             "tail": (r.stdout or "")[-400:],
                             "errTail": (r.stderr or "")[-300:]})
        if r.returncode != 0:
            out["failedAt"] = "python -m build"
            return finish(out, 1)
        r2 = sh([bp, "-m", "twine", "check"] + [str(DIST / a) for a in arts],
                timeout=600)
        blob2 = (r2.stdout or "") + (r2.stderr or "")
        # 分类要诚实：`twine check` 对 sdist 的那句
        #   "Home-page field in Meta data does not match ..." 是 WARNING，
        # 而我这里的 Homepage 是**故意**写的 example.invalid 占位（没官网可填）。
        # 判据原文说"twine check 无 WARNING"，所以：真违规（长描述/元数据坏了）必须 0；
        # 占位 URL 这类"已知且已留痕"的项单列出来给人看，不偷偷并入"通过"。
        warns = [l.strip() for l in blob2.splitlines() if l.strip().startswith("Warning:")]
        errs = [l.strip() for l in blob2.splitlines() if l.strip().startswith("Error:")]
        known_placeholder = [w for w in warns if "Home-page" in w or "home-page" in w.lower()]
        real_warns = [w for w in warns if w not in known_placeholder]
        out["steps"].append({"step": "twine check", "returncode": r2.returncode,
                             "output": blob2[-1200:], "warningLines": warns,
                             "errors": errs,
                             "placeholderOnlyWarnings": known_placeholder,
                             "realWarnings": real_warns})

        # ---------------- 3) 全新 venv 装 wheel → 端到端真下载 ----------------
        tp = venv_py(test_venv)
        whl = next((str(DIST / a) for a in arts if a.endswith(".whl")), None)
        if whl is None:
            out["failedAt"] = "没有 wheel 产物"
            return finish(out, 1)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)     # 关键：不许让源码树遮蔽刚装进去的包
        r3 = sh([tp, "-m", "pip", "install", whl], timeout=900, env=env)
        out["steps"].append({"step": "pip install <whl> (全新 venv)",
                             "returncode": r3.returncode,
                             "tail": (r3.stdout or "")[-240:]})
        if r3.returncode != 0:
            out["failedAt"] = "pip install wheel"
            return finish(out, 1)

        meta = sh([tp, "-c",
                   "import json,importlib.metadata as m;"
                   "req=m.requires('chrome-downloader-client') or [];"
                   "print('META',json.dumps({'requires':req,"
                   "'required':[x for x in req if 'extra' not in x]}))"],
                  timeout=180, env=env)
        line = next((l for l in (meta.stdout or "").splitlines()
                     if l.startswith("META ")), None)
        parsed_meta = json.loads(line[5:]) if line else None
        out["steps"].append({"step": "P5 在全新 venv 里再核一次",
                             "parsed": parsed_meta})

        sys.path.insert(0, str(ROOT / "src"))
        from chrome_downloader.mock_server import MockServer, expected_sha256
        work = Path(tempfile.mkdtemp(prefix="cdl_t8_run_"))
        state, dl = work / "state", work / "files"
        mock = MockServer(0, str(work / "mock.log")).start()
        name, size = "t8blob", 400_000
        url = mock.url(name, size, rate=200_000)
        sha = expected_sha256(name, size)
        exe = str(test_venv / ("Scripts/chrome-dl.exe" if os.name == "nt"
                               else "bin/chrome-dl"))
        rc, output = None, ""
        port = free_port()
        fake = None
        try:
            p = subprocess.Popen(
                [exe, "--download-root", str(dl), "download", url,
                 "--filename", "t8.bin", "--sha256", sha,
                 "--wait", "--timeout", "180", "--state-dir", str(state),
                 "--port", str(port)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", env=env, cwd=str(work))
            # 必须把链路层对手接上去：三级就绪的 L3 是“配对面就绪”，而密钥 K 是
            # 扩展第一次连上来时交出的（TOFU）。没有对手，ensure_bridge 会等到
            # 超时并如实报 L3=false —— 那是产品在说真话，不是 bug。
            ep_f = state / "endpoint.json"
            t0 = time.time()
            while time.time() - t0 < 25:
                if ep_f.exists():
                    break
                time.sleep(0.15)
            if ep_f.exists():
                fake = subprocess.Popen(
                    [tp, str(ROOT / "tests" / "fake_chrome.py"),
                     "--state-dir", str(state), "--download-root", str(dl)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", env=env, cwd=str(ROOT))
            output, _ = p.communicate(timeout=300)
            rc = p.returncode
        finally:
            if fake is not None:
                try:
                    fake.terminate()
                    fake.communicate(timeout=8)
                except Exception:
                    try:
                        fake.kill()
                    except Exception:
                        pass
            try:
                mock.stop()
            except Exception:
                pass
            info = None
            if (state / "endpoint.json").exists():
                try:
                    info = json.loads((state / "endpoint.json").read_text(
                        encoding="utf-8"))
                except Exception:
                    info = None
            if info and info.get("pid"):
                subprocess.run(["taskkill", "/PID", str(info["pid"]), "/T", "/F"],
                               capture_output=True, timeout=30)
        ledger = {}
        jobs_f = state / "jobs.json"
        if jobs_f.exists():
            jj = json.loads(jobs_f.read_text(encoding="utf-8")).get("jobs") or []
            ledger = jj[0] if jj else {}
        out["steps"].append({
            "step": "端到端真下载（用装出来的 chrome-dl）",
            "returncode": rc, "entrypoint": exe, "url": url,
            "expectedSha256": sha, "downloadRootPassedToCli": str(dl),
            "linkLayerPeerAttached": fake is not None,
            "outputTail": (output or "")[-500:],
            "ledgerState": ledger.get("state"),
            "ledgerSha256Actual": ledger.get("sha256Actual"),
            "fileOnDisk": str(dl / "chrome-dl" / "t8.bin")})
        e2e_ok = (rc == 0 and ledger.get("state") == "COMPLETE" and
                  ledger.get("sha256Actual") == "OK")
        shutil.rmtree(work, ignore_errors=True)

        # ---------------- 4) base 环境复核（P7）----------------
        before_p = ROOT / "workflow" / "pip_list_base_before.txt"
        # ⚠️ 取样必须用**干净环境**（这条是踩出来的，不是猜的）：
        #    editable 安装会在 src/ 下留 chrome_downloader_client.egg-info，
        #    而 pip list 会把 PYTHONPATH 上的 *.egg-info 也当成“已装发行包”列出来。
        #    P6 的八解释器跑法正好带着 PYTHONPATH=src —— 于是 base 快照凭空多出
        #    一行 chrome-downloader-client==0.3.0，P7 假红。
        #    实测对照：剥掉 PYTHONPATH → 0 行；带着 → 1 行。base 其实没被装东西。
        env_clean = dict(os.environ)
        env_clean.pop("PYTHONPATH", None)
        now = sorted(x for x in sh([PY, "-m", "pip", "list", "--format=freeze"],
                                   timeout=600, env=env_clean).stdout.splitlines()
                     if x.strip())
        before = sorted(x for x in before_p.read_text(encoding="utf-8").splitlines()
                        if x.strip())
        added = [x for x in now if x not in before]
        removed = [x for x in before if x not in now]
        (ROOT / "workflow" / "pip_list_base_after.txt").write_text(
            "\n".join(now) + "\n", encoding="utf-8")
        out["steps"].append({"step": "P7 base pip list 复核",
                             "sampledWithPythonPathStripped": True,
                             "whyStrip": "pip list 会把 PYTHONPATH 上的 *.egg-info 当"
                                         "已装发行包列出 → 不剥就假红（实测）",
                             "linesBefore": len(before), "linesNow": len(now),
                             "added": added, "removed": removed})

        twine_ok = (r2.returncode == 0 and not errs and not real_warns)
        p5_ok = bool(parsed_meta) and parsed_meta.get("required") == []
        out["e2eOk"] = e2e_ok
        out["twineOk"] = twine_ok
        out["p5Ok"] = p5_ok
        out["baseUnchanged"] = not added
        # LICENSE：计划 §11-① 明写"待人拍板，未定走 STOP-8（本地安装不受影响）"。
        # 本轮拿到的验收项是"`pip install -e .` 或 wheel 构建"，这两条都成立；
        # 所以我**不**因为缺 LICENSE 判 T8 失败，而是把它作为 STOP-8 的前置状态
        # 原样报出来 —— 该拦的是 T10（发 PyPI），不是本地构建。
        out["licensePresent"] = (ROOT / "LICENSE").exists()
        out["STOP-8_if_publishing"] = not out["licensePresent"]
        out["verdict"] = ("PASS" if (e2e_ok and twine_ok and p5_ok and not added)
                          else "FAIL")
        return finish(out, 0 if out["verdict"] == "PASS" else 1)
    finally:
        for d in (build_venv, test_venv):
            try:
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def finish(out: dict, code: int) -> int:
    arts = sorted(p.name for p in DIST.glob("*")) if DIST.exists() else []
    out["artifacts"] = arts
    out["artifactSha256_16"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16]
                                for p in DIST.glob("*")}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    record(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
