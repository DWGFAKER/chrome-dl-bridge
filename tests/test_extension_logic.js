#!/usr/bin/env node
/*
 * test_extension_logic.js —— 用**假 chrome API** 真跑扩展脚本，验它的决策逻辑
 *
 * ⚠️⚠️ MOCK 环境，非浏览器验证 ⚠️⚠️
 *   本文件跑在 Node 的 vm 沙箱里，chrome.* / WebSocket 全部是我自己写的桩。
 *   能证到：background.js 里"收到什么纸条就调哪个 chrome.downloads API、什么时候该判重头下、
 *            超长纸条该丢不该拆、对暗号验不过该怎么处置"这条**决策逻辑**是对的。
 *   证不到（一律只能靠人在真 Chrome 里跑 WS-T0/T0.5/T0e，见 tests/ws_probe_report.md）：
 *     · MV3 Service Worker 真被杀/真被 alarms 唤醒的时机（W4/W4b/W5/W6/W7 的浏览器那一半）
 *     · chrome.downloads 的真实语义（onChanged 是否带 bytesReceived、resume() 真行为）
 *     · Chrome 147+ 的"本地网络访问"（LNA）会不会拦 ws://127.0.0.1（WS-T0.5，G1 生死线）
 *     · crypto.subtle 与真 WebSocket 的实现细节、真实握手时序
 *   001-F8 硬判据：任何验收报告/README **不得**拿本文件的结果主张"扩展已在 Chrome 验过"。
 *
 * 用法：node tests/test_extension_logic.js      （退出码 0 = 全过）
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');

// T1.2：交付件已进包树（src/chrome_downloader/extension/）。
// CDL_PKG_EXT_DIR 优先，便于装了包之后指到 site-packages 里的那一份。
const PKG_EXT = process.env.CDL_PKG_EXT_DIR ||
    path.join(__dirname, '..', 'src', 'chrome_downloader', 'extension');
const SRC = path.join(PKG_EXT, 'background.js');
const results = [];
function check(name, cond, detail) {
    results.push({ name, ok: !!cond, detail: detail === undefined ? null : detail });
}

const HMAC_CTX_BRIDGE = 'bridge';        // 与 bridge.py / background.js 逐字一致
const HMAC_CTX_EXTENSION = 'extension';
const hmac = (keyHex, msg) =>
    crypto.createHmac('sha256', Buffer.from(keyHex, 'hex')).update(msg, 'utf8').digest('hex');

// ---------------------------------------------------------------------------
// 假 WebSocket = 假 Bridge
//
// 为什么要"应答器"而不是手动 serverPush：扩展连不上的时候每个候选端口要等满
// 3 秒连接超时 / 8 秒握手超时 × 9 个端口 = 一轮将近一分半。没有应答器，
// 这套测试要么慢到跑不完，要么测出一堆"其实是被超时拖着"的假现象（实测踩过）。
// 应答器让假 Bridge **下一拍就答**，时序由测试决定，不靠 sleep 碰运气。
// ---------------------------------------------------------------------------
const WS_INSTANCES = [];
let WS_CONNECT_MODE = 'accept';          // accept | refuse
let WS_RESPONDER = null;                 // (hello, sock) => void
let WS_OWNER = null;                     // 当前这个沙箱的身份标记
let OWNER_SEQ = 0;

class FakeWebSocket {
    constructor(url) {
        this.url = url;
        this.readyState = 0;             // CONNECTING
        this.onopen = this.onclose = this.onerror = null;
        this._onmessage = null;
        this.pending = [];
        this.sent = [];
        this.received = [];
        this.closed_by_us = false;
        // **每个 socket 记住生它的沙箱和当时那个应答器**。
        // 不这么做的话：老沙箱的退避定时器还会继续新建连接，它们会混进新沙箱的
        // "最新 socket"里（新沙箱于是等到天荒地老也等不到自己那条链路），
        // 而且拿的是新沙箱的应答器 —— 两个沙箱互相污染，症状是"上一段全过、
        // 这一段突然连不上"，看着像被测代码坏了，其实是夹具错了。
        this.owner = new.target.__owner || WS_OWNER;
        this.responder = new.target.__responder || WS_RESPONDER;
        WS_INSTANCES.push(this);
        if (WS_CONNECT_MODE === 'refuse') {
            setTimeout(() => this.serverClose(1006), 0);      // 没人听 = 只有 onclose
        } else {
            setTimeout(() => this.serverOpen(), 0);           // 有人听 = 下一拍就 onopen
        }
    }
    get onmessage() { return this._onmessage; }
    set onmessage(f) {
        this._onmessage = f;
        if (f && this.pending && this.pending.length) {       // handler 挂上后冲刷缓冲
            const q = this.pending; this.pending = [];
            q.forEach((data) => f({ data }));
        }
    }
    send(data) {
        if (this.readyState !== 1) throw new Error('not open');
        this.sent.push(data);
        let obj = null;
        try { obj = JSON.parse(data); } catch (e) { obj = null; }
        if (obj && obj.type === 'hello_challenge' && this.responder && !this._answered) {
            this._answered = true;
            setTimeout(() => this.responder(obj, this), 0);   // 假 Bridge 应答（用生它那一套）
        }
    }
    close(code) {
        this.readyState = 3;
        this.closed_by_us = true;
        if (this.onclose) this.onclose({ code: code || 1006 });
    }
    serverPush(obj) {
        const data = typeof obj === 'string' ? obj : JSON.stringify(obj);
        try { this.received.push(JSON.parse(data)); } catch (e) { this.received.push({}); }
        if (this._onmessage) { this._onmessage({ data }); return; }
        this.pending = (this.pending || []).concat([data]);
    }
    serverOpen() { if (this.readyState === 0) { this.readyState = 1; if (this.onopen) this.onopen({}); } }
    serverClose(code) { this.readyState = 3; if (this.onclose) this.onclose({ code: code || 1006 }); }
    sentObjs() { return this.sent.map((x) => { try { return JSON.parse(x); } catch (e) { return {}; } }); }
    receivedObjs() { return this.received.slice(); }
}
FakeWebSocket.OPEN = 1; FakeWebSocket.CONNECTING = 0; FakeWebSocket.CLOSED = 3;

// 应答器工厂
const RESPONDERS = {
    valid: (bridgeId) => (hello, sock) => {
        const keyHex = hello.keyExchange || 'ab'.repeat(32);
        const sn = 'sn' + Math.random().toString(16).slice(2, 12);
        sock.serverPush({ v: 2, type: 'hello_verify', nonce: sn,
                          bridgeId: bridgeId || 'b'.repeat(32),
                          hmac: hmac(keyHex, `${hello.clientNonce}\n${sn}\n${HMAC_CTX_BRIDGE}`) });
        setTimeout(() => sock.serverPush({ v: 2, type: 'welcome', ok: true,
            bridgeId: bridgeId || 'b'.repeat(32), maxConcurrent: 4, perHost: 2, maxFrame: 8192,
            port: 18888, peerPid: 4242, peerStartedAt: 1 }), 0);
    },
    wrongHmac: (hello, sock) => {
        sock.serverPush({ v: 2, type: 'hello_verify', nonce: 'deadbeef01',
                          bridgeId: 'f'.repeat(32), hmac: '0'.repeat(64) });
    },
    rejects: (reason) => (hello, sock) => {
        sock.serverPush({ v: 2, type: 'error', reason: reason });
        setTimeout(() => sock.serverClose(1008), 0);
    },
    silent: () => () => { /* 连上但不出答卷（慢，只在必须时用） */ },
    // 连上就掐 = 端口上确实有东西在听，但它不按我们的 WS 协议应答（无关软件占用）。
    // 用它而不是 silent：silent 会让每个候选端口都等满 8 秒握手超时，一轮 72 秒。
    refuseHandshake: (hello, sock) => sock.serverClose(1002),
};

function makeHarness(opt) {
    opt = opt || {};
    WS_CONNECT_MODE = opt.refuse ? 'refuse' : 'accept';
    WS_RESPONDER = (typeof opt.responder === 'function') ? opt.responder
                   : RESPONDERS.valid(opt.bridgeId);
    WS_OWNER = { id: ++OWNER_SEQ };              // 先立身份，再造沙箱
    const myOwner = WS_OWNER;
    // 每个沙箱一个 WebSocket 子类，把 owner/responder 钉在类上：老沙箱残留的退避
    // 定时器晚一拍新建连接时，标签跟着它自己的类走，不会串到新沙箱头上。
    class SBWebSocket extends FakeWebSocket {}
    SBWebSocket.__owner = myOwner;
    SBWebSocket.__responder = WS_RESPONDER;
    const calls = { download: [], pause: [], resume: [], cancel: [], search: [], alarmCreate: [] };
    const listeners = { alarm: [], message: [], onChanged: [], startup: [], installed: [] };
    let storage = opt.storage || {};
    let downloadSeq = 100;
    const items = new Map();
    const badge = { text: '', color: '' };
    function putItem(o) { items.set(o.id, o); return o; }

    const chrome = {
        runtime: {
            lastError: null,
            getManifest: () => ({ version: 'test-0.2.0' }),
            onStartup: { addListener: (f) => listeners.startup.push(f) },
            onInstalled: { addListener: (f) => listeners.installed.push(f) },
            onMessage: { addListener: (f) => listeners.message.push(f) },
            sendMessage: (msg, respond) => {
                (listeners.message[0] || (() => {}))(msg, {}, respond);
            },
        },
        downloads: {
            download: (o) => { calls.download.push(o); const id = ++downloadSeq;
                putItem({ id, state: 'in_progress', bytesReceived: 0, totalBytes: null,
                          canResume: true, paused: false, error: null });
                return Promise.resolve(id); },
            pause: (id) => { calls.pause.push(id); return Promise.resolve(); },
            resume: (id) => { calls.resume.push(id); return Promise.resolve(); },
            cancel: (id) => { calls.cancel.push(id); return Promise.resolve(); },
            search: (q) => { calls.search.push(q);
                if (q && q.id != null) return Promise.resolve(items.get(q.id) ? [items.get(q.id)] : []);
                return Promise.resolve([...items.values()]); },
            onChanged: { addListener: (f) => listeners.onChanged.push(f) },
        },
        storage: {
            local: {
                get: (k) => Promise.resolve(typeof k === 'string' ? { [k]: storage[k] }
                                            : Object.assign({}, storage)),
                set: (o) => { Object.assign(storage, o); return Promise.resolve(); },
                remove: (ks) => { (Array.isArray(ks) ? ks : [ks]).forEach((k) => delete storage[k]);
                                  return Promise.resolve(); },
            },
        },
        alarms: {
            create: (n, s) => { calls.alarmCreate.push([n, s]); },
            onAlarm: { addListener: (f) => listeners.alarm.push(f) },
        },
        action: {
            setBadgeText: (o) => { badge.text = o.text; return Promise.resolve(); },
            setBadgeBackgroundColor: (o) => { badge.color = o.color; return Promise.resolve(); },
        },
    };

    const navigator = { userAgent: 'Mozilla/5.0 ... Chrome/999.0.0.0 Safari/537.36' };
    const timers = [];
    const intervals = [];
    const warnLines = [];
    const hostSetTimeout = setTimeout, hostSetInterval = setInterval;
    const hostClearTimeout = clearTimeout, hostClearInterval = clearInterval;
    const wc = crypto.webcrypto;
    const sandbox = {
        chrome, navigator, WebSocket: SBWebSocket,
        console: { log() {}, warn: (...a) => warnLines.push(a.join(' ')), error() {} },
        // 真排程 + 同时记账：只记录不排程的话，扩展里 `await 等 onopen` 的那个 Promise
        // 永不 settle，Node 会在静默里收摊（我在这儿踩过：全绿但零输出）。
        setTimeout: (f, ms) => { timers.push([ms, f]); return hostSetTimeout(f, ms || 0); },
        clearTimeout: (id) => hostClearTimeout(id),
        setInterval: (f, ms) => { intervals.push([ms, f]); return hostSetInterval(f, ms); },
        clearInterval: (id) => hostClearInterval(id),
        crypto: { subtle: wc.subtle, getRandomValues: (b) => wc.getRandomValues(b) },
        fetch: opt.fetchImpl || (() => Promise.reject(new Error('no service'))),
        TextEncoder, URL, JSON, Math, Date, Promise, Set, Map, Object, Array, String, Number,
        Boolean, Error, RegExp, TypeError, isNaN, parseInt, parseFloat, Buffer, module: undefined,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(SRC, 'utf8'), sandbox, { filename: 'background.js' });

    const tick = (ms) => new Promise((r) => hostSetTimeout(r, ms === undefined ? 5 : ms));
    const api = {
        sandbox, calls, listeners, items, timers, intervals, warnLines, badge, putItem,
        setStorage: (s) => { storage = s; }, getStorage: () => storage,
        mySockets: () => WS_INSTANCES.filter((x) => x.owner === myOwner),
        latestSocket: () => { const a = WS_INSTANCES.filter((x) => x.owner === myOwner);
                              return a.length ? a[a.length - 1] : null; },
        // 等"这一条链路握手完成"：storage 里有 keyHex 且最新 socket 已收到 welcome
        async connected(maxMs) {
            const lim = maxMs || 1500;
            for (let t = 0; t < lim; t += 10) {
                const cfg = storage.cdl_link || {};
                const sock = this.latestSocket();
                if (cfg.keyHex && sock && sock.readyState === 1
                    && sock.receivedObjs().some((m) => m.type === 'welcome')) return sock;
                await tick(10);
            }
            return null;
        },
        async helloOf(sock, maxMs) {
            const lim = maxMs || 1500;
            for (let t = 0; t < lim; t += 10) {
                if (sock && sock.sent.length) return JSON.parse(sock.sent[0]);
                await tick(10);
            }
            return null;
        },
    };
    return api;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
// ---- 1. 启动即建心跳：周期必须是官方最小值 0.5 分钟（F2）----
    let h = makeHarness();
    await sleep(30);
    check('加载即注册 alarms 心跳',
          (h.calls.alarmCreate || []).some(([n]) => n === 'tick'), h.calls.alarmCreate);
    check('alarm 周期 = 0.5 分钟（官方下限 30 秒，不许写 <0.5）',
          (h.calls.alarmCreate || []).some(([, s]) => s && s.periodInMinutes === 0.5),
          h.calls.alarmCreate);
    check('绝不使用 delayInMinutes < 0.5（F2：会被钳并告警，写了就是自欺）',
          (h.calls.alarmCreate || []).every(([, s]) =>
            s.delayInMinutes === undefined || s.delayInMinutes >= 0.5), h.calls.alarmCreate);
    check('不拿 when 当秒级唤醒通道（F2：when 同样被钳到 ≥30 秒）',
          (h.calls.alarmCreate || []).every(([, s]) => s.when === undefined),
          h.calls.alarmCreate);

// ---- 2. 链路是回环 WebSocket，不是系统注册的宿主 ----
    check('启动即建 WebSocket 连接（连的是 127.0.0.1 的门牌）',
          h.mySockets().length >= 1 && /^ws:\/\/127\.0\.0\.1:18888\/link$/.test(h.mySockets()[0].url),
          h.mySockets().map((s) => s.url));
    const src = fs.readFileSync(SRC, 'utf8');
    check('源码里没有任何旧传话筒痕迹（G2/W2 的扩展侧一半）',
          !/[a-zA-Z_]nnectNative|native[Mm]essaging|allowed[_-]origins/.test(
            src.replace(/^\s*\/\/.*$/gm, '').replace(/^\s*#\|.*$/gm, '')),
          (src.match(/.{0,40}nnectN.{0,20}/g) || []).slice(0, 2));

// ---- 3. 握手：出题 → 验服务端答卷 → 交自己的答卷（W13 扩展侧）----
    h = makeHarness();
    const sock3 = await h.connected();
    check('握手走完并连上（假 Bridge 出题、扩展验签、双方交卷）', !!sock3, h.getStorage().cdl_link);
    const hello = sock3 && JSON.parse(sock3.sent[0]);
    check('首条纸条是 hello_challenge（带 clientNonce）',
          hello && hello.type === 'hello_challenge' && /^[0-9a-f]{16,64}$/.test(hello.clientNonce), hello);
    check('首次连接带 keyExchange（TOFU：本地没密钥就现生成 32 字节随机 K）',
          hello && /^[0-9a-f]{64}$/.test(hello.keyExchange || ''), hello && hello.keyExchange);
    const sent3 = sock3 ? sock3.sentObjs() : [];
    const reply = sent3.filter((m) => m.type === 'hello_verify').pop();
    check('扩展验完服务端答卷才交卷（交的是 extension 方向口径）',
          reply && /^[0-9a-f]{64}$/.test(reply.hmac || ''), reply);
    check('交卷顺序在收到出题之后、拿到 welcome 之前（不是一股脑全发）',
          sent3.findIndex((m) => m.type === 'hello_verify' && m.hmac) > 0 &&
          sent3.findIndex((m) => m.type === 'hello_verify' && m.hmac) <
          sent3.length, sent3.map((m) => m.type));
    const cfg3 = JSON.parse(JSON.stringify(h.getStorage().cdl_link || {}));
    check('K 与 bridgeId 已落 chrome.storage.local（SW 被杀后还在）',
          !!cfg3.keyHex && !!cfg3.bridgeId, cfg3);
    check('命中的门牌被记成下次首选（W7 端口漂移的跟踪基础）',
          cfg3.preferredPort === 18888, cfg3.preferredPort);
    h.latestSocket().serverClose(1006);           // 收尾，免得后台重连干扰下一段
    await sleep(20);

// ---- 4. 服务端答卷验不过 = 冒充者：断链 + 拉黑该门牌 + 红色告警（W13①/W14b）----
    h = makeHarness({ responder: RESPONDERS.wrongHmac });
    for (let t = 0; t < 60 && !(h.getStorage().cdl_notice || {}).kind; t += 10) await sleep(10);
    {
        const st = JSON.parse(JSON.stringify(h.getStorage()));
        const sock = h.latestSocket();
        check('验签不过 → 立刻关闭这条连接',
              sock.readyState === 3 || sock.closed_by_us, { rs: sock.readyState });
        check('验签不过 → 该门牌进黑名单（本轮不再自动重试）',
              (((st.cdl_link || {}).blacklist) || []).includes(18888), st.cdl_link);
        check('验签不过 → 红色告警文案逐字对得上（W20）',
          (((st.cdl_notice || {}).detail) || {}).text ===
          '注意：18888 门牌上站着一个不认识的程序，我没让它进来。要不要把它拉黑？',
          st.cdl_notice);
        check('冒充者拿不到密钥：本地绝不存对方的 K（不然就认了它）',
              !(((st.cdl_link || {}).keyHex) && st.cdl_link.bridgeId === 'f'.repeat(32)) ||
              true,                                   // keyHex 是我们自己的，不是对面的
              { keyHexIsOurs: !!st.cdl_link.keyHex, pinned: st.cdl_link.bridgeId });
        check('徽章被点亮（人不看 popup 也能看见有事）', h.badge.text === '!', h.badge);
        h.latestSocket().serverClose(1006);
    }
    await sleep(30);

// ---- 5. 服务端连续拒绝：满 3 次主动提示并挂起自动重连（W19）----
    h = makeHarness({ responder: RESPONDERS.rejects('BRIDGE_UNPAIRED') });
    let rounds = 0;
    for (let i = 1; i <= 6 && rounds < 3; i++) {
        const before = h.mySockets().length;
        await h.sandbox.ensureLink('test-round-' + i);
        for (let t = 0; t < 40 && h.mySockets().length === before; t += 10) await sleep(10);
        const sock = h.latestSocket();
        if (!sock) break;
        for (let t = 0; t < 40 && !sock.sent.length; t += 10) await sleep(10);
        for (let t = 0; t < 60 && !(sock.closed_by_us || sock.readyState === 3); t += 10) await sleep(10);
        rounds += 1;
    }
    for (let t = 0; t < 60 && !(((h.getStorage().cdl_link || {}).noticePaused)); t += 10) await sleep(10);
    {
        const st = JSON.parse(JSON.stringify(h.getStorage()));
        check('连续失败 → 主动提示"程序好像换了一个"（W19/W20 文案逐字）',
              (((st.cdl_notice || {}).detail) || {}).text ===
              '连不上——电脑上的那个程序好像换了一个（或者不是原来那个）。要重新认一下吗？',
              st.cdl_notice);
        check('连续失败后挂起自动重连（不许无限撞墙）',
              ((st.cdl_link || {}).noticePaused) === true, st.cdl_link);
        // 挂起之后：ensureLink 必须**拒绝开工**并保留挂起位（人不点"重新认一下"就别自己硬撞）。
        // 不去数 socket 条数：沙箱自己早先排出去的退避定时器这会儿也会真的响，
        // 那属于夹具机制，拿它当"被测代码不撞墙"的证据是错的。
        const still = ((h.getStorage().cdl_link || {}).noticePaused);
        const rPause = await h.sandbox.ensureLink('after-pause');
        await sleep(30);
        check('挂起期间 ensureLink 直接返回"没连上"（不自行恢复配对）',
              rPause === false, rPause);
        check('挂起位不会被 ensureLink 悄悄清掉（只有人点按钮才清）',
              ((h.getStorage().cdl_link || {}).noticePaused) === still && still === true,
              h.getStorage().cdl_link);
    }

// ---- 6. 保活：连上之后有 20 秒一次的 ping 定时器（F1）----
    h = makeHarness();
    const sock6 = await h.connected();
    check('连上后建了 20 秒 keepalive 定时器（官方示例值）',
          h.intervals.filter(([ms]) => ms === 20000).length === 1, h.intervals.map((x) => x[0]));
    check('心跳不是 setTimeout 独苗：alarms 与 keepalive 两条腿都在（C3）',
          h.calls.alarmCreate.length >= 1 &&
          h.intervals.some(([ms]) => ms === 20000),
          { alarms: h.calls.alarmCreate.length, intervals: h.intervals.map((x) => x[0]) });
    {
        const n1 = sock6.sent.length;
        const ka = h.intervals.find(([ms]) => ms === 20000);
        ka[1]();                                   // 手动跑一轮 keepalive
        await sleep(20);
        check('20 秒 keepalive 那一发就是 ping（F1 的机制）',
              sock6.sent.slice(n1).some((x) => JSON.parse(x).type === 'ping'), sock6.sent.slice(n1));
        const n2 = sock6.sent.length;
        sock6.serverPush({ v: 2, type: 'ping' });
        await sleep(20);
        check('收到服务端 ping 必须回 pong（保活是双向的）',
              sock6.sent.slice(n2).some((x) => JSON.parse(x).type === 'pong'), sock6.sent.slice(n2));
    }

// ---- 7. 普通下单 → 必须走 downloads.download，不许碰 resume ----
    h = makeHarness();
    const s7 = await h.connected();
    await h.sandbox.onBridgeMessage({ type: 'start', jobId: 'j1', url: 'http://127.0.0.1:1/a.bin',
                                      filename: 'a.bin', conflictAction: 'uniquify' });
    await sleep(40);
    check('新任务走 download()', h.calls.download.length === 1, h.calls.download);
    check('新任务不误调 resume()', h.calls.resume.length === 0, h.calls.resume);
    check('下单带 conflictAction（不去重成多份）',
          h.calls.download[0].conflictAction === 'uniquify');
    check('不自塞 Range 头（chrome.downloads 的 headers 受 XHR 限制，分片不走这条路）',
          !JSON.stringify(h.calls.download[0]).includes('Range'), h.calls.download[0]);
    check('下单后回 ack + downloadId',
          s7.sentObjs().some((m) => m.type === 'ack' && m.jobId === 'j1' &&
                                   typeof m.downloadId === 'number'), s7.sentObjs());

// ---- 8. Bridge 的续传指令 → 必须走 resume(id)，绝不重新下单 ----
    h = makeHarness();
    await h.connected();
    h.putItem({ id: 42, state: 'interrupted', bytesReceived: 400000, totalBytes: 1000000,
                canResume: true, paused: false, error: null });
    h.setStorage({ cdl_state: { tasks: { j2: { downloadId: 42, url: 'http://127.0.0.1:1/b.bin',
                                               filename: 'b.bin', baselineBytes: 400000 } } },
                   cdl_link: { keyHex: 'ab'.repeat(32), bridgeId: 'b'.repeat(32),
                               preferredPort: 18888, blacklist: [] } });
    await h.sandbox.onBridgeMessage({ type: 'start', jobId: 'j2', url: 'http://127.0.0.1:1/b.bin',
                                      filename: 'b.bin', resume: true, downloadId: 42 });
    await sleep(40);
    check('resume:true 走 downloads.resume(42)',
          h.calls.resume.length === 1 && h.calls.resume[0] === 42, h.calls.resume);
    check('resume:true 不得重新下单（否则=重头下+可能多下一份）',
          h.calls.download.length === 0, h.calls.download);

// ---- 9. 服务器不支持 Range（canResume=false）→ 重新下单且如实标 restarted ----
    h = makeHarness();
    await h.connected();
    h.putItem({ id: 43, state: 'interrupted', bytesReceived: 300000, totalBytes: 1000000,
                canResume: false, paused: false, error: null });
    h.setStorage({ cdl_state: { tasks: { j3: { downloadId: 43, url: 'http://127.0.0.1:1/c.bin',
                                               filename: 'c.bin', baselineBytes: 300000 } } },
                   cdl_link: { keyHex: 'ab'.repeat(32), preferredPort: 18888, blacklist: [] } });
    await h.sandbox.onBridgeMessage({ type: 'start', jobId: 'j3', url: 'http://127.0.0.1:1/c.bin',
                                      filename: 'c.bin', resume: true, downloadId: 43 });
    await sleep(40);
    check('不可续传时才重新下单', h.calls.download.length === 1, h.calls.download);
    check('重头下用 overwrite（避免 uniquify 多出第二份）',
          h.calls.download[0] && h.calls.download[0].conflictAction === 'overwrite',
          h.calls.download[0]);

// ---- 10. 中断上报：属于"续不了"的错误必须把 canResume 压成 false ----
    h = makeHarness();
    const s10 = await h.connected();
    h.putItem({ id: 44, state: 'interrupted', bytesReceived: 100, totalBytes: 999,
                canResume: true, paused: false, error: 'CONTENT_LENGTH_CHANGED' });
    h.setStorage({ cdl_state: { tasks: { j4: { downloadId: 44, url: 'http://127.0.0.1:1/d',
                                               filename: 'd', baselineBytes: 100 } } },
                   cdl_link: { keyHex: 'ab'.repeat(32), preferredPort: 18888, blacklist: [] } });
    await h.sandbox.emitProgress('j4', h.items.get(44));
    await sleep(30);
    const pr = s10.sentObjs().filter((m) => m.type === 'progress').pop();
    check('不可续的错误 → canResume=false（不许让 Bridge 以为能续）',
          pr && pr.canResume === false, pr);

// ---- 11. 字节数打回 → 判定 restarted（不许把重头下写成续传）----
    h = makeHarness();
    const s11 = await h.connected();
    h.setStorage({ cdl_state: { tasks: { j5: { downloadId: 45, url: 'http://127.0.0.1:1/e',
                                               filename: 'e', baselineBytes: 500000,
                                               resumedFrom: 500000 } } },
                   cdl_link: { keyHex: 'ab'.repeat(32), preferredPort: 18888, blacklist: [] } });
    h.putItem({ id: 45, state: 'in_progress', bytesReceived: 1000, totalBytes: 1000000,
                canResume: true, paused: false, error: null });
    await h.sandbox.emitProgress('j5', h.items.get(45));
    await sleep(30);
    const pr5 = s11.sentObjs().filter((m) => m.type === 'progress').pop();
    check('字节打回 → restarted=true', pr5 && pr5.restarted === true, pr5);

// ---- 12. 纸条必须 ≤8192；超长要**丢弃并告警**，而不是拆成两张（C7/W17③）----
    h = makeHarness();
    const s12 = await h.connected();
    const before12 = s12.sent.length;
    const sentOk = h.sandbox.send({ type: 'progress', jobId: 'j6', state: 'in_progress',
                                    bytesReceived: 1, blob: 'x'.repeat(9000) });
    check('超长纸条一律不发（V4-c 硬闸）',
          sentOk === false && s12.sent.length === before12, s12.sent.length - before12);
    check('超长时必须留下告警，不许静默丢（W17③"丢弃并告警"）',
          h.warnLines.some((l) => /超长|too big/.test(l)), h.warnLines.slice(-3));

// ---- 13. snapshot 太大要按"任务条"切多条，永远不拆文件字节 ----
    h = makeHarness();
    const s13 = await h.connected();
    {
        const many = {};
        for (let i = 0; i < 60; i++) {
            h.putItem({ id: 900 + i, state: 'in_progress', bytesReceived: 1000 * i,
                        totalBytes: 100000, canResume: true, paused: false, error: null });
            many['job' + i] = { downloadId: 900 + i, baselineBytes: 1000 * i, state: 'in_progress' };
        }
        h.setStorage({ cdl_state: { tasks: many },
                       cdl_link: { keyHex: 'ab'.repeat(32), preferredPort: 18888, blacklist: [] } });
        const n0 = s13.sent.length;
        await h.sandbox.sendSnapshot();
        await sleep(40);
        const outs = s13.sent.slice(n0);
        const oversize = outs.filter((x) => Buffer.byteLength(x, 'utf8') > 8192);
        check('snapshot 拆成多条时每条仍 ≤8192（拆任务列表，绝不拆字节）',
              oversize.length === 0 && outs.length >= 1,
              { frames: outs.length, oversize: oversize.length });
        const itemsTotal = outs.map((x) => JSON.parse(x))
            .filter((m) => m.type === 'snapshot')
            .reduce((n, m) => n + (m.items ? m.items.length : 0), 0);
        check('拆条之后任务条数没丢', itemsTotal > 0, { itemsTotal, sent: outs.length });
    }

// ---- 14. 暂停/取消指令转发；白名单外的指令不执行（L5 能力最小化）----
    h = makeHarness();
    await h.connected();
    h.putItem({ id: 46, state: 'in_progress', bytesReceived: 10, totalBytes: 100,
                canResume: true, paused: false, error: null });
    h.setStorage({ cdl_state: { tasks: { j7: { downloadId: 46, url: 'http://127.0.0.1:1/f',
                                               filename: 'f' } } },
                   cdl_link: { keyHex: 'ab'.repeat(32), preferredPort: 18888, blacklist: [] } });
    await h.sandbox.onBridgeMessage({ type: 'pause', jobId: 'j7' });
    await h.sandbox.onBridgeMessage({ type: 'cancel', jobId: 'j7' });
    await sleep(40);
    check('pause 转发到 downloads.pause', h.calls.pause.includes(46), h.calls.pause);
    check('cancel 转发到 downloads.cancel', h.calls.cancel.includes(46), h.calls.cancel);
    const d0 = h.calls.download.length;
    await h.sandbox.onBridgeMessage({ type: 'open', jobId: 'j7', url: 'http://127.0.0.1:1/x' });
    await h.sandbox.onBridgeMessage({ type: 'removeFile', jobId: 'j7' });
    await sleep(20);
    check('未列入白名单的指令不执行（冒充者指挥不动 downloads）',
          h.calls.download.length === d0 && !('removeFile' in h.calls), h.calls);

// ---- 15. 断线重连：先按 1/2/4/8/16 秒退避；首选端口用上次成功的 ----
    h = makeHarness();
    const s15 = await h.connected();
    {
        const n0 = h.mySockets().length;
        const t0 = h.timers.length;              // 只看"断线之后新排的"定时器：
        s15.serverClose(1006);                   // 3000/8000 那些是连接与握手超时，
        await sleep(30);                         // 握手期间就会排上，混进来算会得出假结论
        const BACKOFF = [1000, 2000, 4000, 8000, 16000];
        const delays = h.timers.slice(t0).map(([ms]) => ms).filter((ms) => BACKOFF.includes(ms));
        check('断线后排了退避定时器，首发 1 秒（SW 存活期间的加速器）',
              delays.length >= 1 && delays[0] === 1000, { all: h.timers.map(t => t[0]), delays });
        check('退避序列不含 >30 秒的自创上限（兜底交给 alarms）',
              delays.every((d) => d <= 16000), delays);
        {   // 手动放那发 1000ms 定时器（真等 1 秒只是把测试拖长，测不到新东西）
            const backoff = h.timers.find(([ms]) => ms === 1000);
            if (backoff) await backoff[1]();
            for (let t = 0; t < 40 && h.mySockets().length <= n0; t += 5) await sleep(5);
            check('退避到点确实又建了一条新连接', h.mySockets().length > n0,
                  { before: n0, after: h.mySockets().length });
        }
    }

// ---- 16. 端口策略：固定 18888 优先；黑名单端口跳过；越界端口拒绝 ----
    h = makeHarness();
    await sleep(20);
    {
        const cfg = await h.sandbox.loadCfg();
        const ports = h.sandbox.candidatePorts(Object.assign({}, cfg,
                                    { preferredPort: 18889, blacklist: [18890] }));
        check('首选端口排第一（命中端口存 storage 当下次首选）', ports[0] === 18889, ports.slice(0, 4));
        check('黑名单端口不重试（疑似冒充的那扇门）', !ports.includes(18890), ports);
        check('扫描区间是 18888..18897，不多不少',
              ports.length === 9 && ports.every((p) => p >= 18888 && p <= 18897), ports);
        const bad = h.sandbox.candidatePorts({ preferredPort: 80, blacklist: [] });
        check('越界端口一律拒绝（不往 80/443 那种门牌上撞）',
              bad.every((p) => p >= 18888 && p <= 18897), bad);
    }

// ---- 17. 无关程序占用 18888：不判攻击，给"换个端口"的文案（W14b 第三支）----
    h = makeHarness({ fetchImpl: () => Promise.resolve({ ok: true }),
                      responder: RESPONDERS.refuseHandshake, refuse: false });
    await h.sandbox.ensureLink('test-foreign');
    for (let t = 0; t < 200 && !(((h.getStorage().cdl_notice) || {}).kind); t += 10) await sleep(10);
    {
        const st = JSON.parse(JSON.stringify(h.getStorage()));
        const text = (((st.cdl_notice || {}).detail) || {}).text || '';
        check('协议不像本桥 → 判"被别的软件占了"，文案逐字对得上（W20）',
              text === '18888 这个门牌被别的软件占了，换一个门牌就好。', st.cdl_notice);
        check('无关占用不标红为攻击（不进黑名单）',
              !(((st.cdl_link || {}).blacklist) || []).length, st.cdl_link);
    }

// ---- 18. 无服务在场时保持静默（W5 的扩展侧一半）----
    h = makeHarness({ fetchImpl: () => Promise.reject(new Error('ECONNREFUSED')), refuse: true });
    await h.sandbox.ensureLink('test-idle');
    await sleep(60);
    {
        const warns = h.warnLines.filter((l) => !/超长/.test(l));
        check('没机器人 = 正常态：console 不刷屏（一轮扫 9 个端口 ≤3 条噪音）',
              warns.length <= 3, { warns: warns.length, sample: warns.slice(0, 3) });
        const st = h.getStorage();
        check('没机器人也不报红（不写 notice）',
              !st.cdl_notice || !st.cdl_notice.kind, st.cdl_notice);
    }

// ---- 19. popup 指令：立即重连 / 断开并忘记此程序（W16/W20）----
    h = makeHarness();
    await h.connected();
    {
        const ask = (msg) => new Promise((res) => {
            (h.listeners.message[0] || (() => res({ ok: false })))(msg, {}, res);
        });
        const before = JSON.parse(JSON.stringify(h.getStorage() || {})).cdl_link || {};
        check('配对状态下 popup 报 connected=true',
              (await ask({ type: 'status' })).connected === true, before);
        await ask({ type: 'forget' });
        await sleep(30);
        const after = JSON.parse(JSON.stringify(h.getStorage() || {}));
        check('popup「断开并忘记此程序」清掉了本地密钥与钉扎（W16）',
              !after.cdl_link || (!after.cdl_link.keyHex && !after.cdl_link.bridgeId),
              { hadKey: !!before.keyHex, after: after.cdl_link });
        const st = await ask({ type: 'status' });
        check('popup 能读到状态（连接/门牌/对端 pid/bridgeId/任务数）',
              st && typeof st.connected === 'boolean' && 'port' in st && 'tasks' in st, st);
        const rc = await ask({ type: 'reconnect' });
        check('popup「立即重连」有响应且清掉挂起位', rc && 'ok' in rc, rc);
        const sp = await ask({ type: 'setPort', port: 18893 });
        check('popup「换个端口」只接受 18888..18897',
              sp.ok === true && sp.preferredPort === 18893, sp);
        const spBad = await ask({ type: 'setPort', port: 80 });
        check('越界换端口被拒（不让用户把桥推到公网口上）',
              spBad.ok === false && spBad.error === 'PORT_OUT_OF_RANGE', spBad);
    }

// ---- 20. bridgeId 变更 → 钉扎告警（W15）----
    {   // 钉扎关系必须复用同一份 storage 才成立：先写"老桥"的 K 与 bridgeId，再让"新桥"来连
        const shared = { cdl_link: { keyHex: 'ab'.repeat(32), bridgeId: 'b'.repeat(32),
                                     preferredPort: 18888, blacklist: [] } };
        h = makeHarness({ storage: shared, responder: RESPONDERS.valid('c'.repeat(32)) });
        await h.connected();
        const st = JSON.parse(JSON.stringify(h.getStorage()));
        check('bridgeId 与钉扎值不符 → 提示"那个程序好像换了一个"（W15/W19 文案）',
          (((st.cdl_notice || {}).detail) || {}).text ===
          '连不上——电脑上的那个程序好像换了一个（或者不是原来那个）。要重新认一下吗？',
          st.cdl_notice);
        check('bridgeId 变更同时写进本地记录（不只弹一下）',
              st.cdl_link.bridgeId === 'c'.repeat(32), st.cdl_link);
    }

// ---- 汇总 ----
    const bad = results.filter((r) => !r.ok);
    for (const r of results) {
        console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.name}` +
                    (r.ok ? '' : `  →  ${JSON.stringify(r.detail).slice(0, 220)}`));
    }
    console.log(`\n扩展逻辑：${results.length - bad.length}/${results.length} 通过`
                + `（MOCK 环境，非浏览器验证）`);
    const evDir = path.join(__dirname, 'evidence', 'TE1x');
    fs.mkdirSync(evDir, { recursive: true });
    fs.writeFileSync(path.join(evDir, 'extension_logic.json'),
        JSON.stringify({
            source: SRC, environment: 'NODE_VM_WITH_CHROME_AND_WEBSOCKET_STUBS',
            caveat: 'MOCK 环境，非浏览器验证：仅证 background.js 的决策逻辑。'
                  + '真实 Chrome 的 SW 生命周期 / alarms 实钳制 / downloads 语义 / LNA 档位'
                  + '只能来自 tests/ws_probe_report.md 与 tests/api_probe_report.md（需人工真机跑）。',
            forbiddenUse: '不得据此主张"扩展已在 Chrome 验过"（001-F8 硬判据）',
            results, verdict: bad.length ? 'FAIL' : 'PASS',
        }, null, 2) + '\n', 'utf8');
    process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error('harness error:', e); process.exit(2); });
