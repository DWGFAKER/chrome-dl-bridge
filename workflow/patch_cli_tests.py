#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 L2/L5 两处判据写法改准（一次性的补丁脚本，跑完即弃）。"""
import pathlib
import sys

p = pathlib.Path("D:/article4/scratch/chrome_downloader_pkg/tests/test_pkg_v03_cli.py")
s = p.read_text(encoding="utf-8")
n = 0


def rep(old, new, where, expect=1):
    global s, n
    c = s.count(old)
    if c != expect:
        print(f"[FAIL] {where}: 命中 {c}（期望 {expect}）")
        sys.exit(1)
    s = s.replace(old, new)
    n += 1


# ---- L2：把"人话"写成它本来的意思 ----
rep('''                         "listsVerbs": "能用的动词" in blob or "usage" in blob.lower(),''',
    '''                         # "人话"的口径 = 告诉调用方能用什么（动词清单 / usage 行 /
                         # 三个动作名都算），不是死抠某一句话术。
                         "listsVerbs": ("能用的动词" in blob or
                                         "usage" in blob.lower() or
                                         ("start" in blob and "stop" in blob and
                                          "status" in blob)),''', "L2 actionable")

# ---- live_bridge 支持挂链路层对手 ----
rep('''    def live_bridge(self):
        """起一个可用的桥（配对面用预置 handshake.key 达等价状态）。"""
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "handshake.key").write_text("ab" * 32, encoding="ascii")''',
    '''    def live_bridge(self, with_fake: bool = False):
        """起一个可用的桥。

        with_fake=False：只预置 handshake.key（配对面就绪），够 status/stop/动词面用。
        with_fake=True ：再挂上 tests/fake_chrome.py，任务才**真能下完**。
          不挂对手时，桥会按设计在 link_wait_timeout 之后把任务判成
          FAILED: NO_EXTENSION_CONNECTED —— 那是产品的正确行为，不是 bug；
          所以 L5 这种"要看 COMPLETE 与 sha256Actual"的用例必须带对手。
        """
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "handshake.key").write_text("ab" * 32, encoding="ascii")''',
    "live_bridge doc")

rep('''        th = threading.Thread(target=go)
        th.start()
        th.join(60)
        assert "res" in box, f"桥起不来：{box.get('err')}"''',
    '''        th = threading.Thread(target=go)
        th.start()
        if with_fake:
            t0 = time.time()
            info = None
            while time.time() - t0 < 20:
                info = paths.read_endpoint(self.state)
                if info:
                    break
                time.sleep(0.1)
            if info:
                self.fake = H.start_fake_chrome(self.state, self.dl)
        th.join(60)
        assert "res" in box, f"桥起不来：{box.get('err')}"''', "live_bridge fake hook")

rep('''        self.live_bridge()
        name, size = "l5blob", 300_000''',
    '''        self.live_bridge(with_fake=True)
        name, size = "l5blob", 300_000''', "L5 use fake")

p.write_text(s, encoding="utf-8")
print(f"ok，{n} 处")
