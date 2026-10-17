#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_install_paths.py —— T8 的两条安装路径检验（P1 的 `pip install -e .` 分支 + 端到端）

为什么单独一个脚本：build_and_verify.py 走的是 **wheel 安装**这条路，
而验收项写的是"`pip install -e .` 或 wheel 构建"。两条路的取件路径解析不一样
（editable 指向 src/，wheel 指向 site-packages/），所以两条都要真跑，不能只跑一条宣称另一条也行。

三件事，全部真跑并留 exit code：
  A) 独立 venv 里 `pip install -e .`（editable）→ 三个入口 --help → 打印的扩展目录
     **确实存在**且 4 个文件在（P3 在 editable 形态下的等价核对）；
  B) 同一 venv 里起 mock 源站 + 链路层假 Chrome，用**装出来的** chrome-dl 下一单，
     读账本断言 state=COMPLETE 且 sha256Actual=OK（§8.6 T7-a3 同一口径）；
  C) wheel 形态的对照（build_and_verify.py 已做，这里再跑一次入口 --help，
     确保两种形态下 CLI 面一致）。

诚实边界：这里的"下载"仍由 tests/fake_chrome.py 这个**链路层对手**完成取字节动作，
它证的是"包 + CLI + 桥 + 账本"这条链在装好之后真能跑通，
不替真实 Chrome 的 MV3 语义作证（§8.7）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PY = sys.executable


def sh(cmd, env=None, cwd=None, timeout=900):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(cwd or ROOT),
                          timeout=timeout)


def venv_py(d: Path) -> str:
    return str(d / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def free_port() -> int:
    for _ in range(30):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if p != 18888:                     # 不占 W14/W14b 的门牌（§8.4.0 步骤 0）
            return p
    return 18889


def clean_env(**extra) -> dict:
    e = dict(os.environ)
    e.pop("PYTHONPATH", None)              # 关键：不许让源码树遮蔽刚装进去的包
    e.update(extra)
    return e


def main() -> int:
    results = {"steps": [], "verdict": "FAIL"}
    ok_all = True
    for label, kind in (("editable", ["-e", "."]), ("wheel", ["."])):
        v = Path(tempfile.mkdtemp(prefix=f"cdl_smoke_{label}_"))
        try:
            r = sh([PY, "-m", "venv", str(v)], timeout=900)
            assert r.returncode == 0, r.stderr[-200:]
            vp = venv_py(v)
            scripts = v / ("Scripts" if os.name == "nt" else "bin")
            if kind == ["."]:
                dist = ROOT / "dist"
                whl = sorted(p for p in dist.glob("*.whl"))
                assert whl, "没有 wheel 产物，先跑 build_and_verify.py"
                target = [str(whl[-1])]
            else:
                target = ["-e", str(ROOT)]
            r2 = sh([vp, "-m", "pip", "install", "--no-build-isolation"] + target,
                    timeout=1800, env=clean_env())
            step = {"form": label, "installArgs": target,
                    "installReturncode": r2.returncode,
                    "tail": (r2.stdout or "")[-200:]}
            if r2.returncode != 0:
                step["verdict"] = "FAIL"
                results["steps"].append(step)
                ok_all = False
                continue

            # ---- 三个入口 --help ----
            exe = str(scripts / ("chrome-dl.exe" if os.name == "nt" else "chrome-dl"))
            mexe = str(scripts / ("chrome-dl-mcp.exe" if os.name == "nt"
                                  else "chrome-dl-mcp"))
            helps = {}
            for name, cmd in (("chrome-dl --help", [exe, "--help"]),
                              ("chrome-dl-mcp --version", [mexe, "--version"]),
                              ("python -m chrome_downloader --help",
                               [vp, "-m", "chrome_downloader", "--help"])):
                rr = sh(cmd, env=clean_env(), timeout=180)
                blob = (rr.stdout or "") + (rr.stderr or "")
                helps[name] = {"returncode": rr.returncode,
                               "noTraceback": "Traceback" not in blob,
                               "head": blob[:150].replace("\n", " | ")}
            step["entrypoints"] = helps

            # ---- 扩展目录真的在（P3 在该形态下的等价核对）----
            probe = sh([vp, "-c",
                        "import json;from chrome_downloader import paths;"
                        "d=paths.extension_dir_resource();"
                        "need=paths.extension_files();"
                        "miss=[f for f in need if not (d/f).exists()];"
                        "print('P',json.dumps({'dir':str(d),'missing':miss,"
                        "'origin':__import__('chrome_downloader',fromlist=['x']).__file__}))"],
                       env=clean_env(), timeout=180)
            pl = next((l for l in (probe.stdout or "").splitlines()
                       if l.startswith("P ")), None)
            step["packageOriginAndExtensionDir"] = json.loads(pl[2:]) if pl else None

            # ---- 端到端：装出来的 chrome-dl + 链路层假 Chrome 下一单 ----
            work = Path(tempfile.mkdtemp(prefix=f"cdl_smoke_run_{label}_"))
            state, dl = work / "state", work / "files"
            sys.path.insert(0, str(SRC))
            from chrome_downloader.mock_server import MockServer, expected_sha256
            mock = MockServer(0, str(work / "mock.log")).start()
            name, size = f"smoke_{label}", 350_000
            url = mock.url(name, size, rate=200_000)
            sha = expected_sha256(name, size)
            port = free_port()
            env = clean_env()
            # --download-root 必须显式传，且必须**真的转发给被拉起的桥**。
            # 这里顺带钉住刚修的那个真 bug：以前 CLI 收下 --download-root 却丢掉不传，
            # 桥就用自己默认的 ~/Downloads/chrome-dl 去找文件，
            # 而搬运工往调用方指定的目录写 → sha256Actual=MISSING、任务判 FAILED，
            # 看起来像"下载坏了"，其实是旗标没转发。
            cmd_dl = [exe, "--download-root", str(dl), "download", url,
                      "--filename", "s.bin", "--sha256", sha,
                      "--wait", "--timeout", "150", "--state-dir", str(state),
                      "--port", str(port)]
            box = {}

            def run_dl():
                box["r"] = sh(cmd_dl, env=env, timeout=300, cwd=work)
                box["rc"] = box["r"].returncode

            import threading
            th = threading.Thread(target=run_dl)
            th.start()
            fake = None
            t0 = time.time()
            info = None
            while time.time() - t0 < 25:
                f = state / "endpoint.json"
                if f.exists():
                    try:
                        info = json.loads(f.read_text(encoding="utf-8"))
                        break
                    except Exception:
                        pass
                time.sleep(0.15)
            if info:
                fake = subprocess.Popen(
                    [vp, str(ROOT / "tests" / "fake_chrome.py"),
                     "--state-dir", str(state), "--download-root", str(dl)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", cwd=str(ROOT), env=env)
            th.join(400)
            try:
                mock.stop()
            except Exception:
                pass
            ledger = {}
            jf = state / "jobs.json"
            if jf.exists():
                jj = json.loads(jf.read_text(encoding="utf-8")).get("jobs") or []
                ledger = jj[0] if jj else {}
            rc = box.get("rc")
            blob = ((box.get("r").stdout or "") + (box.get("r").stderr or "")
                    if box.get("r") else "")
            e2e_ok = (rc == 0 and ledger.get("state") == "COMPLETE" and
                      ledger.get("sha256Actual") == "OK")
            step["endToEnd"] = {"downloadRootPassedToCli": str(dl),
                                "returncode": rc, "jobState": ledger.get("state"),
                                "sha256Actual": ledger.get("sha256Actual"),
                                "outputTail": blob[-260:],
                                "transportUsed": (info or {}).get("transport"),
                                "fakeChromeUsed": "链路层对手（不替真 Chrome 作证）",
                                "ok": e2e_ok}
            if fake is not None:
                try:
                    fake.terminate()
                    fake.communicate(timeout=8)
                except Exception:
                    fake.kill()
            # 收尾：停掉这条链上的桥（只停我们自己 state-dir 里记着的那个）
            if info and info.get("pid"):
                subprocess.run(["taskkill", "/PID", str(info["pid"]), "/T", "/F"],
                               capture_output=True, timeout=30)
            shutil.rmtree(work, ignore_errors=True)
            step["verdict"] = "PASS" if (e2e_ok and step["packageOriginAndExtensionDir"]
                                         and not step["packageOriginAndExtensionDir"]["missing"]
                                         and all(h["returncode"] == 0 and h["noTraceback"]
                                                 for h in helps.values())) else "FAIL"
            ok_all = ok_all and step["verdict"] == "PASS"
            results["steps"].append(step)
        finally:
            shutil.rmtree(v, ignore_errors=True)
    results["verdict"] = "PASS" if ok_all else "FAIL"
    print(json.dumps(results, ensure_ascii=False, indent=1))
    (ROOT / "workflow" / "t8_smoke_log.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if results["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
