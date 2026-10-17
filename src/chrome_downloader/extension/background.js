// chrome-dl-bridge 扩展端（Manifest V3, Service Worker, v0.2 纯 WebSocket 版）
//
// 分工：Agent 下命令 → Bridge 记账 → **本文件叫 Chrome 干活** → 字节只由 Chrome 网络栈取。
// 本文件与 Bridge 之间只有"纸条"（WS 文本帧，单条 ≤ 8192 字节），绝不让文件字节过通道。
//
// v0.2 换掉的东西（其余下载逻辑与 v0.1 一致）：
//   链路：OS 注册的宿主进程 stdio 管道  →  new WebSocket('ws://127.0.0.1:18888/link')
//   配对：注册表 + 单扩展 ID 白名单     →  HMAC-SHA256 双向挑战应答（TOFU）
//   保活：持有宿主端口                 →  20s WS ping（Chrome 116+ 官方机制）+ alarms 30s 兜底
//   （旧链路与旧配对方式在本仓已彻底移除，逐字残留检查见 tests 的 W2 判据与 NM_REMOVAL_NOTE.md）
//
// 五条设计约束（来自计划 §5 / F1 / F2 / C3 / C7）：
//   1. SW 随时会被 Chrome 杀掉。任何"必须由定时器完成的事"都要有 chrome.alarms 心跳兜底；
//      setTimeout 只在"当前这次活着"的窗口里当加速器用，不作为唯一依赖（C3）。
//      官方下限 30 秒（0.5 分钟）：delayInMinutes / periodInMinutes / when 三者都不能更早，
//      所以这里**不写** <0.5 的 alarm，也不拿 when 装成"秒级"（F2）。
//   2. 进度主路径 = chrome.downloads.search() 轮询（F7）；onChanged 里顺带接收 delta，
//      两者喂同一个状态机并单调化。
//   3. 续传 = downloads.resume()。若字节数打回接近 0 → 如实标 restarted=true（重头下），
//      绝不把"重头下"写成"续传"。
//   4. 发送前自检 ≤8192 字节：超长 = 设计违约，宁可丢这一条也不拆成两帧
//      （拆帧等于给"绕过长度闸"开门，C7/W17）。
//   5. 未经 HMAC 双向验证的连接**一个命令都不接**（§5.5 L2）：宁可连不上。
'use strict';

const MAX_FRAME = 8192;                 // C7：与 bridge.py 的 MAX_FRAME_BYTES 同值
const POLL_MS = 500;                    // SW 存活期间的轮询间隔
const KEEPALIVE_MS = 20000;             // F1：官方示例就是 20 秒一次
const ALARM_TICK = 'tick';
const ALARM_PERIOD_MIN = 0.5;           // F2：官方下限 30 秒，再小会被钳并告警
const BACKOFF_SEC = [1, 2, 4, 8, 16];   // SW 仍存活时的进程内退避（A 档唯一来源）
const WS_CONNECT_TIMEOUT_MS = 3000;     // 单个端口的连接尝试上限
const HANDSHAKE_TIMEOUT_MS = 8000;
const PREF_PORT = 18888;
const PORT_MIN = 18888, PORT_MAX = 18897;
const WS_PATHNAME = '/link';
const HOST = '127.0.0.1';
const HANDSHAKE_FAIL_LIMIT = 3;         // 恢复路径二：连续 3 次 → 主动提示并暂停自动重连

const STATE_KEY = 'cdl_state';           // 任务快照（SW 重启后靠它恢复）
const CFG_KEY = 'cdl_link';              // 链路配置：密钥 / bridgeId 钉扎 / 端口 / 黑名单
const NOTICE_KEY = 'cdl_notice';         // 给人看的告警（popup 第一屏）

const HMAC_CTX_BRIDGE = 'bridge';        // 与 bridge.py HMAC_CTX_BRIDGE 逐字一致
const HMAC_CTX_EXTENSION = 'extension';  // 与 bridge.py HMAC_CTX_EXTENSION 逐字一致

const NON_RESUMABLE = ['NETWORK_CHANGED', 'CONTENT_LENGTH_CHANGED', 'FILE_ERROR',
                       'FILE_SECURITY_ERROR', 'SERVER_NO_RANGE'];

// ---- 运行态（内存；SW 被杀就没了，所以一切都有一份持久副本）----
let ws = null;
let wsPort = null;                 // 当前这条连接实际用的端口
let connecting = false;
let keepaliveTimer = null;
let connectTimer = null;
let pollTimer = null;
let handshakeFailures = 0;
let backoffIndex = 0;
let lastSent = new Map();          // jobId -> {bytes, state, t}  去抖
let idToJob = new Map();           // chrome downloadId -> jobId
let linkEpoch = 0;                 // 每次成功握手 +1，popup 用来判断"是不是同一条链路"

// ============================================================
// 0. 持久配置：TOFU 密钥、bridgeId 钉扎、端口与黑名单（§5.5 L3/L4）
// ============================================================
async function loadCfg() {
  const o = await chrome.storage.local.get(CFG_KEY);
  const c = o[CFG_KEY] || {};
  return {
    keyHex: c.keyHex || null,            // 32 字节随机密钥（hex），首次连接时生成
    bridgeId: c.bridgeId || null,        // 钉扎的对端身份
    preferredPort: c.preferredPort || PREF_PORT,
    blacklist: c.blacklist || [],        // 疑似冒充的端口，本轮不再自动重试
    lastPeerPid: c.lastPeerPid || null,
    lastPeerStartedAt: c.lastPeerStartedAt || null,
  };
}
async function saveCfg(patch) {
  const cur = await loadCfg();
  const next = Object.assign(cur, patch || {});
  await chrome.storage.local.set({ [CFG_KEY]: next });
  return next;
}

async function loadState() {
  const o = await chrome.storage.local.get(STATE_KEY);
  return o[STATE_KEY] || { tasks: {} };
}
async function saveState(s) {
  await chrome.storage.local.set({ [STATE_KEY]: s });
}
async function updateTask(jobId, patch) {
  const s = await loadState();
  s.tasks[jobId] = Object.assign({}, s.tasks[jobId] || {}, patch);
  await saveState(s);
  return s.tasks[jobId];
}

// ---- 给人看的告警（popup 第一屏；同时写 chrome.storage 让 popup 读得到）----
async function setNotice(kind, detail) {
  const rec = { kind, detail: detail || null, at: Date.now() };
  await chrome.storage.local.set({ [NOTICE_KEY]: rec });
  try {
    // 徽章：红色感叹号 = 需要人看一眼；正常态清空
    await chrome.action.setBadgeText({ text: kind ? '!' : '' });
    await chrome.action.setBadgeBackgroundColor({ color: kind === 'silent' ? '#8a8a8a' : '#d93025' });
  } catch { /* 老版本没有 action API，不拦主流程 */ }
  return rec;
}
async function clearNotice() {
  await chrome.storage.local.set({ [NOTICE_KEY]: { kind: null, detail: null, at: Date.now() } });
  try { await chrome.action.setBadgeText({ text: '' }); } catch {}
}

// ---- 小工具 ----
function nowSec() { return Date.now() / 1000; }
function hostOf(url) { try { return new URL(url).host; } catch { return '?'; } }
function toHex(buf) {
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('');
}
function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}
function randomHex(nbytes) {
  const b = new Uint8Array(nbytes);
  crypto.getRandomValues(b);
  return toHex(b.buffer);
}

// 发送前的硬闸（W17③）：>8192 一律不发并点名。返回值 false = 已丢弃。
function send(obj) {
  if (!ws || ws.readyState !== 1 /* OPEN */) return false;
  let text;
  try { text = JSON.stringify(Object.assign({ v: 2, ts: Math.round(Date.now() / 10) / 100 }, obj)); }
  catch (e) { return false; }
  const bytes = new TextEncoder().encode(text).byteLength;
  if (bytes > MAX_FRAME) {
    // 宁可丢也不拆字节：拆帧 = 让"单帧 ≤8KB"这条判据形同虚设
    console.warn('[cdl] 纸条超长已丢弃：', obj && obj.type, bytes, '>', MAX_FRAME);
    return false;
  }
  try { ws.send(text); return true; } catch (e) { return false; }
}

// ============================================================
// 1. HMAC-SHA256 双向挑战应答（§5.5 L2）
// ============================================================
async function hmacSha256(keyHex, msg) {
  const key = await crypto.subtle.importKey(
    'raw', hexToBytes(keyHex), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(msg));
  return toHex(sig);
}

// 与 bridge.py 的 challenge_expect / response_expect 逐字对齐（换行符是 \n，不是空格）
function bridgeProofExpect(keyHex, clientNonce, serverNonce) {
  return hmacSha256(keyHex, `${clientNonce}\n${serverNonce}\n${HMAC_CTX_BRIDGE}`);
}
function extensionProof(keyHex, clientNonce, serverNonce) {
  return hmacSha256(keyHex, `${serverNonce}\n${clientNonce}\n${HMAC_CTX_EXTENSION}`);
}
function constantTimeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ============================================================
// 2. 链路（回环 WebSocket）
// ============================================================
function candidatePorts(cfg) {
  // L1 固定端点优先：先试上次成功的端口 → 18888 → 18889..18897；黑名单端口跳过
  const seen = new Set();
  const out = [];
  const push = (p) => {
    if (typeof p !== 'number' || p < PORT_MIN || p > PORT_MAX) return;
    if (cfg.blacklist.includes(p) || seen.has(p)) return;
    seen.add(p); out.push(p);
  };
  push(cfg.preferredPort); push(PREF_PORT);
  for (let p = PORT_MIN; p <= PORT_MAX; p++) push(p);
  return out;
}

function wsUrl(port) { return `ws://${HOST}:${port}${WS_PATHNAME}`; }

// 用一次 no-cors fetch 判断"这个端口上有没有别的东西在听"。
// 为什么绕这一趟：扩展侧读不到 TCP 错误码（WebSocket 只会告诉你"失败了"），
// 分不清"没人"和"有人但不是我们的桥"。fetch 的 resolve/reject 能把这两类分开：
//   resolve（哪怕 opaque）→ 有人监听，但它没按我们的 WS 协议应答 → 无关软件占用
//   reject                → 连不上 → 无服务（正常态，静默）
// 这一发同时也是 LNA 提示的补触发尝试（§5.6 观测项 e，我们的假设，非官方路径）。
async function portHasForeignListener(port) {
  try {
    await fetch(`http://${HOST}:${port}/v1/health`, { mode: 'no-cors', cache: 'no-store' });
    return true;
  } catch {
    return false;
  }
}

async function ensureLink(reason) {
  if (connecting) return false;
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return true;   // 已在连/已连
  const cfg = await loadCfg();
  if (cfg.noticePaused === true) return false;    // 恢复路径二：等人处置前不再自动撞墙
  connecting = true;
  try {
    const ports = candidatePorts(cfg);
    for (const port of ports) {
      const r = await tryConnect(port);
      if (r.ok) { connecting = false; return true; }
      if (r.fatal) { connecting = false; return false; }    // 疑似冒充 → 停手等人
    }
    // 全都没成：区分"无服务（静默）"与"被别的软件占着（一次性提示）"
    let foreign = null;
    if (await portHasForeignListener(PREF_PORT)) foreign = PREF_PORT;
    connecting = false;
    if (foreign) {
      await setNotice('foreign_port', { port: foreign,
        text: `${foreign} 这个门牌被别的软件占了，换一个门牌就好。` });
      await scheduleReconnect(); // 仍按退避继续（换端口由人/Agent 决定，不自作主张）
    } else {
      await scheduleReconnect(); // 无服务 = 正常态：静默退避，console 不刷
    }
    return false;
  } catch (e) {
    connecting = false;
    return false;
  }
}

async function tryConnect(port) {
  const cfg = await loadCfg();
  let sock;
  try {
    sock = new WebSocket(wsUrl(port));
  } catch (e) {
    return { ok: false, fatal: false };
  }
  const opened = await new Promise((resolve) => {
    let done = false;
    const t = setTimeout(() => { if (!done) { done = true; resolve(false); } }, WS_CONNECT_TIMEOUT_MS);
    sock.onopen = () => { if (!done) { done = true; clearTimeout(t); resolve(true); } };
    sock.onerror = () => { if (!done) { done = true; clearTimeout(t); resolve(false); } };
    sock.onclose = () => { if (!done) { done = true; clearTimeout(t); resolve(false); } };
  });
  if (!opened) {
    try { sock.close(); } catch {}
    return { ok: false, fatal: false };
  }

  // ---- 握手：扩展先出题，再验服务端的答卷，最后交自己的答卷 ----
  const clientNonce = randomHex(16);
  let keyHex = cfg.keyHex;
  const hello = { type: 'hello_challenge', role: 'extension', clientNonce,
                  chromeVersion: chromeVersion(), port,
                  extensionVersion: chrome.runtime.getManifest().version };
  if (!keyHex) {
    // L3 TOFU：本地没密钥 → 现生成一个随机 K，随第一条纸条发过去（仅此一次）
    keyHex = randomHex(32);
    hello.keyExchange = keyHex;
  }
  let peer;
  try {
    sock.send(JSON.stringify(Object.assign({ v: 2, ts: Math.round(Date.now() / 10) / 100 }, hello)));
    peer = await recvOne(sock, HANDSHAKE_TIMEOUT_MS, (m) => m.type === 'hello_verify' || m.type === 'error');
  } catch (e) {
    try { sock.close(); } catch {}
    return { ok: false, fatal: false };
  }
  if (!peer) {
    try { sock.close(); } catch {}
    return { ok: false, fatal: false };
  }
  if (peer.type === 'error') {
    // 服务端不认我们（多半是本地 state_dir 被清/换过 → 它手里的 K 变了）
    await noteHandshakeFailure(port, peer.reason || 'SERVER_REJECTED', cfg);
    try { sock.close(); } catch {}
    return { ok: false, fatal: true };
  }
  const expect = await bridgeProofExpect(keyHex, clientNonce, peer.nonce || '');
  if (!constantTimeEqual(expect, peer.hmac || '')) {
    // 对面不知道 K = 冒充 Bridge 的程序。红线处置：拉黑这个端口 + 红色告警 + 停手（W13①/W14b）
    const blacklisted = Array.from(new Set((cfg.blacklist || []).concat([port])));
    await saveCfg({ blacklist: blacklisted });
    await setNotice('hijack', {
      port, peerBridgeId: peer.bridgeId || null, seenAt: new Date().toISOString(),
      text: `注意：${port} 门牌上站着一个不认识的程序，我没让它进来。要不要把它拉黑？` });
    try { sock.close(1008, 'bridge hmac mismatch'); } catch {}
    return { ok: false, fatal: true };
  }
  // 交自己的答卷，让服务端也验一次扩展身份（双向）
  sock.send(JSON.stringify({ v: 2, type: 'hello_verify', ts: Date.now(),
                             hmac: await extensionProof(keyHex, clientNonce, peer.nonce || '') }));
  let welcome;
  try {
    welcome = await recvOne(sock, HANDSHAKE_TIMEOUT_MS, (m) => m.type === 'welcome' || m.type === 'error');
  } catch (e) { welcome = null; }
  if (!welcome || welcome.type !== 'welcome') {
    await noteHandshakeFailure(port, 'NO_WELCOME', cfg);
    try { sock.close(); } catch {}
    return { ok: false, fatal: false };
  }

  // ---- 身份钉扎（L4 / W15）----
  let idChanged = false;
  if (cfg.bridgeId && welcome.bridgeId && cfg.bridgeId !== welcome.bridgeId) idChanged = true;
  await saveCfg({ keyHex, bridgeId: welcome.bridgeId || null, preferredPort: port,
                  lastPeerPid: welcome.peerPid || null,
                  lastPeerStartedAt: welcome.peerStartedAt || null });
  handshakeFailures = 0;
  backoffIndex = 0;
  ws = sock; wsPort = port; linkEpoch += 1;
  wireSocket(sock);
  startKeepalive();
  startPolling();
  await clearNotice();
  if (idChanged) {
    await setNotice('bridge_id_changed', { old: cfg.bridgeId, now: welcome.bridgeId,
      text: '连不上——电脑上的那个程序好像换了一个（或者不是原来那个）。要重新认一下吗？' });
  }
  await driveRetryQueue();
  await sendSnapshot();
  return { ok: true, fatal: false };
}

function recvOne(sock, timeoutMs, pred) {
  return new Promise((resolve, reject) => {
    let done = false;
    const t = setTimeout(() => { if (!done) { done = true; sock.onmessage = null; resolve(null); } },
                         timeoutMs);
    const onMsg = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }        // 不像我们协议的报文：继续等
      if (m && typeof m === 'object' && pred(m)) {
        if (done) return;
        done = true; clearTimeout(t); sock.onmessage = null; resolve(m);
      }
    };
    sock.onmessage = onMsg;
    sock.onclose = () => { if (!done) { done = true; clearTimeout(t); resolve(null); } };
  });
}

function wireSocket(sock) {
  sock.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg && typeof msg === 'object') onBridgeMessage(msg).catch(() => {});
  };
  sock.onclose = async () => {
    const wasUs = (ws === sock);
    if (wasUs) { ws = null; stopKeepalive(); stopPolling(); }
    // 断线不等于任务失败：账本在 Bridge，重连后靠 snapshot 对账
    if (wasUs) await scheduleReconnect();
  };
  sock.onerror = () => { /* onclose 会跟着来，这里不重复处置，免得刷屏 */ };
}

function chromeVersion() {
  try {
    const m = /Chrome\/([\d.]+)/.exec(navigator.userAgent || '');
    return m ? m[1] : 'unknown';
  } catch { return 'unknown'; }
}

// ---- 保活：连上之后每 20 秒一发 ping（F1 官方机制，不是我们自创的技巧）----
function startKeepalive() {
  stopKeepalive();
  keepaliveTimer = setInterval(() => {
    if (ws && ws.readyState === 1) send({ type: 'ping' });
  }, KEEPALIVE_MS);
}
function stopKeepalive() {
  if (keepaliveTimer) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
}

// ---- 重连：SW 还活着 → setTimeout 退避（1/2/4/8/16 秒）；SW 被杀 → 交给 30 秒 alarm ----
// 这两档的分工是 F2 的直接后果：alarm 最快也要 30 秒，任何 alarm 参数都绕不开，
// 所以"秒级"只能来自进程内 setTimeout —— 前提是这次 SW 还没被销毁。
async function scheduleReconnect() {
  if (connectTimer) return;
  const delay = BACKOFF_SEC[Math.min(backoffIndex, BACKOFF_SEC.length - 1)] * 1000;
  backoffIndex = Math.min(backoffIndex + 1, BACKOFF_SEC.length - 1);
  connectTimer = setTimeout(async () => {
    connectTimer = null;
    await ensureLink('backoff');
  }, delay);
}

// 握手连续失败的处置（恢复路径二 / W19）：满 3 次就停止自动撞墙，主动请人拍板
async function noteHandshakeFailure(port, reason, cfg) {
  handshakeFailures += 1;
  if (handshakeFailures < HANDSHAKE_FAIL_LIMIT) {
    await setNotice('handshake_fail', { port, reason, attempt: handshakeFailures,
      text: '连不上——电脑上的那个程序好像换了一个（或者不是原来那个）。要重新认一下吗？' });
    return;
  }
  const next = await saveCfg({ noticePaused: true });
  await setNotice('key_mismatch', {
    port, reason, attempts: handshakeFailures, bridgeIdPinned: cfg.bridgeId || null,
    paused: true,
    text: '连不上——电脑上的那个程序好像换了一个（或者不是原来那个）。要重新认一下吗？' });
  return next;
}

// ============================================================
// 3. 命令处理（与 v0.1 同语义，只是承载换成 WS）
// ============================================================
async function onBridgeMessage(msg) {
  if (!msg || typeof msg !== 'object') return;
  const { type, jobId } = msg;
  try {
    if (type === 'pong') return;
    if (type === 'ping') { send({ type: 'pong' }); return; }      // 服务端也在保活（F1）
    if (type === 'welcome') {
      await driveRetryQueue();
      await sendSnapshot();
      return;
    }
    if (type === 'start') return await startJob(msg);
    // 能力最小化（§5.5 L5）：没列进白名单的指令一律不执行、不代传。
    // Bridge 只会下发 start/pause/resume/cancel/ping + welcome/replaced/error/snapshot；
    // 其余的一律当噪声丢弃（冒充者拿到这条链路也指挥不动 chrome.downloads）。
    if (type === 'download' || type === 'open' || type === 'removeFile') {
      send({ type: 'progress', jobId, state: 'interrupted',
             error: 'CAPABILITY_NOT_ALLOWED:' + type, canResume: false });
      return;
    }
    if (type === 'pause') return await controlJob(msg, 'pause');
    if (type === 'resume') return await controlJob(msg, 'resume');
    if (type === 'cancel') return await controlJob(msg, 'cancel');
    if (type === 'replaced') {           // 这条链路被更新的连接顶替
      if (ws) { try { ws.close(); } catch {} ws = null; }
      stopKeepalive(); stopPolling();
      return;
    }
    if (type === 'error') {              // 服务端拒绝：按原因决定要不要停手
      await setNotice('server_error', { reason: msg.reason, text:
        '对面的程序没认出我（原因：' + String(msg.reason || '未知') + '）。' });
      return;
    }
  } catch (e) {
    send({ type: 'progress', jobId, state: 'interrupted',
           error: String((e && e.message) || e), canResume: false });
  }
}

// ============================================================
// 4. 下载控制（v0.1 逻辑原样保留 —— 这次改造不许动下载语义）
// ============================================================
async function startJob(msg) {
  const { jobId, url, filename, conflictAction, sha256 } = msg;

  // 续传路径：Bridge 的退避定时器会发 start + resume:true + downloadId。
  // 这里必须走 downloads.resume(id)，不能重新下单（重下单 = 重头下 + 可能多下一份）。
  if (msg.resume && msg.downloadId != null) {
    const it0 = (await chrome.downloads.search({ id: msg.downloadId })) || [];
    // 只有"确实能续"才走 resume；服务器不支持 byte range 时 resume 是空动作，
    // 必须落到下面重开一单，并如实标 restarted（R4）。
    if (it0[0] && it0[0].state !== 'complete' && it0[0].canResume) {
      idToJob.set(msg.downloadId, jobId);
      await updateTask(jobId, { url, filename, downloadId: msg.downloadId,
                               resumedFrom: it0[0].bytesReceived || 0 });
      try {
        await chrome.downloads.resume(msg.downloadId);
        send({ type: 'ack', jobId, downloadId: msg.downloadId, state: 'in_progress' });
      } catch (e) {
        send({ type: 'progress', jobId, downloadId: msg.downloadId, state: 'interrupted',
               error: 'RESUME_REJECTED:' + String((e && e.message) || e), canResume: false });
      }
      const found = await chrome.downloads.search({ id: msg.downloadId });
      if (found && found[0]) await emitProgress(jobId, found[0]);
      return;
    }
    // 找不到那个 downloadId，或它续不了 → 落到下面开新单，并如实算"重头下"
    await updateTask(jobId, { restarted: true, resumedFrom: null, baselineBytes: 0 });
    msg = Object.assign({}, msg, { conflictAction: 'overwrite' });
  }

  await updateTask(jobId, { url, filename, sha256: sha256 || null,
                            baselineBytes: 0, resumes: 0, startedAt: nowSec() });
  let downloadId;
  try {
    downloadId = await chrome.downloads.download({
      url,
      filename,                                  // 相对路径，落在 Chrome 当前默认下载目录
      conflictAction: msg.conflictAction || conflictAction || 'uniquify',
      saveAs: false,
      // 不塞自造 Range 头：chrome.downloads 的 headers 受 XHR 限制，分片不走这条路
    });
  } catch (e) {
    const message = String((e && e.message) || e);
    send({ type: 'progress', jobId, state: 'interrupted',
           error: 'DOWNLOAD_REJECTED:' + message, canResume: false });
    return;
  }
  idToJob.set(downloadId, jobId);
  await updateTask(jobId, { downloadId });
  send({ type: 'ack', jobId, downloadId, state: 'in_progress' });
  const found = await chrome.downloads.search({ id: downloadId });
  if (found && found[0]) await emitProgress(jobId, found[0]);
}

async function controlJob(msg, what) {
  const { jobId } = msg;
  const t = (await loadState()).tasks[jobId] || {};
  const id = msg.downloadId ?? t.downloadId;
  if (id == null) {
    send({ type: 'progress', jobId, state: 'interrupted', error: 'NO_DOWNLOAD_ID', canResume: false });
    return;
  }
  try {
    if (what === 'pause') await chrome.downloads.pause(id);
    else if (what === 'resume') await chrome.downloads.resume(id);
    else if (what === 'cancel') await chrome.downloads.cancel(id);
    send({ type: 'ack', jobId, downloadId: id,
           state: what === 'cancel' ? 'cancelled' : what + 'd' });
    if (what === 'resume') await updateTask(jobId, { resumedFrom: t.baselineBytes || 0 });
    setTimeout(async () => {
      const found = await chrome.downloads.search({ id });
      if (found && found[0]) await emitProgress(jobId, found[0]);
    }, 300);
  } catch (e) {
    // pause 在部分 Chrome 上对不可暂停项会抛错；如实上报，不假装成功
    send({ type: 'progress', jobId, downloadId: id, state: 'interrupted',
           error: 'CONTROL_FAILED:' + what + ':' + String((e && e.message) || e), canResume: true });
  }
}

// ============================================================
// 5. 进度：search() 轮询为主，onChanged 为辅（双保险）
// ============================================================
function startPolling() { stopPolling(); pollTimer = setInterval(pollOnce, POLL_MS); pollOnce(); }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

async function pollOnce() {
  if (!ws || ws.readyState !== 1) return;
  const s = await loadState();
  const ids = Object.values(s.tasks).map((t) => t.downloadId).filter((x) => x != null);
  if (!ids.length) return;
  const items = await chrome.downloads.search({ limit: 1000, ordered: false });
  const want = new Set(ids);
  for (const it of items) {
    if (!want.has(it.id)) continue;
    const jobId = idToJob.get(it.id) || jobForDownload(s, it.id);
    if (jobId) await emitProgress(jobId, it);
  }
}

function jobOfDownload(id) { return idToJob.get(id) || null; }

function jobForDownload(stateObj, downloadId) {
  for (const [jobId, t] of Object.entries((stateObj && stateObj.tasks) || {})) {
    if (t.downloadId === downloadId) { idToJob.set(downloadId, jobId); return jobId; }
  }
  return null;
}

async function emitProgress(jobId, it) {
  const t = (await loadState()).tasks[jobId] || {};
  const bytes = Math.max(0, it.bytesReceived || 0);
  // 单调化 + "重头下"检测：resume 成功后 Chrome 接着涨；不支持 Range 会打回接近 0
  const prevBase = t.baselineBytes || 0;
  let restarted = !!t.restarted;
  if (t.resumedFrom != null && bytes < t.resumedFrom - 1024 && it.state === 'in_progress') {
    restarted = true;
  }
  if (restarted) t.resumedFrom = null;
  const monotone = Math.max(prevBase, bytes);
  await updateTask(jobId, { baselineBytes: monotone, restarted,
                            resumedFrom: t.resumedFrom ?? null, state: it.state });
  const last = lastSent.get(jobId) || {};
  const movedEnough = Math.abs(monotone - (last.bytes || 0)) >= 16384 ||
                      it.state !== last.state ||
                      nowSec() - (last.t || 0) >= 1;      // 至少 1 条/秒（SW 存活段）
  if (!movedEnough) return;
  lastSent.set(jobId, { bytes: monotone, state: it.state, t: nowSec() });
  const total = it.totalBytes && it.totalBytes > 0 ? it.totalBytes : null;
  const speed = computeSpeed(jobId, monotone);
  // 错误类型属于"续不了"的，强制 canResume=false，让 Bridge 走 RESTARTED 而不是假装能续
  const errName = String(it.error || (it.interruptReason && it.interruptReason.reason) || '');
  const resumable = !!it.canResume && !NON_RESUMABLE.some((k) => errName.toUpperCase().includes(k));
  send({
    type: 'progress', jobId, downloadId: it.id,
    state: it.state, bytesReceived: monotone, totalBytes: total, speedBps: speed,
    canResume: resumable, restarted,
    resumedFrom: t.resumedFrom || 0,
    error: it.error || it.interruptReason || null,
  });
}

// 速度：本地滑窗（Δbytes/Δt），Bridge 也会独立算一份，两者对不上就是问题（V2）
const windows = new Map();
function computeSpeed(jobId, bytes) {
  const w = windows.get(jobId) || [];
  const t = nowSec();
  w.push([t, bytes]);
  while (w.length && t - w[0][0] > 5) w.shift();
  windows.set(jobId, w);
  if (w.length < 2) return 0;
  const dt = w[w.length - 1][0] - w[0][0];
  return dt > 0 ? Math.max(0, Math.round((w[w.length - 1][1] - w[0][1]) / dt)) : 0;
}

// 重连后对账：把本地每个还在跟的任务真实状态发回去（W6/W7 的收敛路径）
async function sendSnapshot() {
  const s = await loadState();
  const items = [];
  for (const [jobId, t] of Object.entries(s.tasks)) {
    if (t.downloadId == null) continue;
    const found = await chrome.downloads.search({ id: t.downloadId });
    if (!found || !found[0]) continue;
    idToJob.set(t.downloadId, jobId);
    items.push({ jobId, downloadId: t.downloadId, state: found[0].state,
                 bytesReceived: Math.max(t.baselineBytes || 0, found[0].bytesReceived || 0),
                 totalBytes: found[0].totalBytes || null,
                 canResume: !!found[0].canResume, restarted: !!t.restarted });
  }
  // 8192 硬闸优先于"一次发完"：装不下就切成多条 snapshot（每条仍然是一张小纸条，
  // 拆的是任务列表，永远不是文件字节）
  let batch = [];
  const flush = async () => {
    if (!batch.length) return;
    if (!send({ type: 'snapshot', items: batch })) {
      for (const it of batch) await emitProgress(it.jobId, { id: it.downloadId, state: it.state,
        bytesReceived: it.bytesReceived, totalBytes: it.totalBytes, canResume: it.canResume });
    }
    batch = [];
  };
  for (const it of items) {
    const probe = batch.concat([it]);
    const size = new TextEncoder().encode(JSON.stringify({ v: 2, type: 'snapshot', items: probe }))
      .byteLength;
    if (size > MAX_FRAME && batch.length) { await flush(); batch = [it]; } else batch = probe;
  }
  await flush();
}

// onChanged：主路径仍是 search 轮询；这里顺手接收 delta 里可能存在的 bytesReceived
chrome.downloads.onChanged.addListener(async (delta) => {
  const jobId = jobOfDownload(delta.id) || jobForDownload(await loadState(), delta.id);
  if (!jobId) return;
  const found = await chrome.downloads.search({ id: delta.id });
  if (found && found[0]) return emitProgress(jobId, found[0]);
  const st = delta.state && delta.state.current;
  if (st) {
    send({ type: 'progress', jobId, downloadId: delta.id, state: st,
           bytesReceived: delta.bytesReceived && delta.bytesReceived.current,
           canResume: !!(delta.canResume && delta.canResume.current) });
  }
});

// ============================================================
// 6. 中断上报（重试决策归 Bridge：它常驻、定时器可靠）
// ============================================================
// 为什么扩展不再自己退避重试：两个大脑各自 1/2/4/8/16s 退避会撞车 ——
// 实测下就是"同一任务被下令两次"。Bridge 是账本拥有者，扩展只负责如实上报。
async function driveRetryQueue() {
  const s = await loadState();
  for (const [jobId, t] of Object.entries(s.tasks)) {
    if (t.downloadId == null) continue;
    if (t.state === 'complete' || t.state === 'cancelled') continue;
    const items = await chrome.downloads.search({ id: t.downloadId });
    const it = items && items[0];
    if (!it) continue;
    if (!idToJob.has(t.downloadId)) idToJob.set(t.downloadId, jobId);
    if (it.state === 'interrupted' || it.paused) await emitProgress(jobId, it);
  }
}

// ============================================================
// 7. 生命周期：每次 SW 启动都重建 alarm（§5.1 官方建议）
// ============================================================
function ensureAlarm() {
  // F2：periodInMinutes/delayInMinutes/when 三者都不能早于 30 秒；这里用合法最小值 0.5。
  // 商店版被钳到 ≥30 秒属预期，不许对外承诺秒级（§5.4）。
  const spec = { periodInMinutes: ALARM_PERIOD_MIN, delayInMinutes: ALARM_PERIOD_MIN };
  try {
    chrome.alarms.create(ALARM_TICK, Object.assign({ persistAcrossSessions: true }, spec));
  } catch {
    chrome.alarms.create(ALARM_TICK, spec);   // 老版本不认 persistAcrossSessions
  }
}

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== ALARM_TICK) return;
  // 醒来第一件事：把链路接上（连上之后 20 秒 ping 会把 SW 活动窗口续住，F1）
  await ensureLink('alarm');
  await driveRetryQueue();
  await pollOnce();
});

chrome.runtime.onStartup.addListener(() => { ensureAlarm(); ensureLink('startup'); });
chrome.runtime.onInstalled.addListener(() => { ensureAlarm(); ensureLink('installed'); });

// popup 的指令通道（不新增任何安装步骤：都是用户主动点一下的加速/处置）
chrome.runtime.onMessage.addListener((req, _sender, respond) => {
  (async () => {
    const t = req && req.type;
    if (t === 'status') {
      const cfg = await loadCfg();
      const o = await chrome.storage.local.get(NOTICE_KEY);
      const s = await loadState();
      respond({
        connected: !!(ws && ws.readyState === 1), port: wsPort, epoch: linkEpoch,
        transport: 'ws', path: WS_PATHNAME,
        paired: !!cfg.keyHex, bridgeId: cfg.bridgeId, preferredPort: cfg.preferredPort,
        blacklist: cfg.blacklist, paused: cfg.noticePaused === true,
        peerPid: cfg.lastPeerPid, peerStartedAt: cfg.lastPeerStartedAt,
        chromeVersion: chromeVersion(), notice: o[NOTICE_KEY] || null,
        handshakes: handshakeFailures, tasks: Object.keys(s.tasks).length,
      });
      return;
    }
    if (t === 'hold') {
      // "先别连"：保留告警、挂起自动重连（不抹平问题，也不继续撞墙）
      await saveCfg({ noticePaused: true });
      if (ws) { try { ws.close(); } catch {} ws = null; }
      respond({ ok: true, paused: true });
      return;
    }
    if (t === 'setPort') {
      const p = Number(req.port);
      if (!(p >= PORT_MIN && p <= PORT_MAX)) {
        respond({ ok: false, error: 'PORT_OUT_OF_RANGE', port: p }); return;
      }
      const cfg = await loadCfg();
      await saveCfg({ preferredPort: p, blacklist: cfg.blacklist.filter((x) => x !== p) });
      respond({ ok: true, preferredPort: p });
      return;
    }
    if (t === 'reconnect') {
      await saveCfg({ noticePaused: false });
      if (ws) { try { ws.close(); } catch {} ws = null; }
      backoffIndex = 0; handshakeFailures = 0;
      respond({ ok: await ensureLink('popup'), port: wsPort });
      return;
    }
    if (t === 'forget') {
      // 一键"断开并忘记此程序"（L5 可撤销 / W16/W18）：清 K、清钉扎、清黑名单
      if (ws) { try { ws.close(); } catch {} ws = null; }
      await chrome.storage.local.remove([CFG_KEY, NOTICE_KEY]);
      handshakeFailures = 0; backoffIndex = 0;
      await clearNotice();
      respond({ ok: true, next: '下一次连接重新 TOFU' });
      return;
    }
    if (t === 'unblacklist') {
      const cfg = await loadCfg();
      await saveCfg({ blacklist: [], noticePaused: false });
      if (req.port) { /* 用户看过详情后允许再试这个端口 */ }
      respond({ ok: true, cleared: cfg.blacklist });
      return;
    }
    respond({ ok: false, error: 'UNKNOWN_REQUEST', got: t });
  })();
  return true;                          // 异步应答：必须返回 true
});

// 入口：SW 每次被唤起都会跑到这里
ensureAlarm();
ensureLink('boot');

// ---- 供 Node 侧测试夹具使用（真实 Chrome 里 chrome/WebSocket 都已就位，不导出）----
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    onBridgeMessage, startJob, controlJob, emitProgress, pollOnce, send, ensureLink,
    tryConnect, candidatePorts, sendSnapshot, driveRetryQueue, setNotice, clearNotice,
    loadCfg, saveCfg, hmacSha256, bridgeProofExpect, extensionProof, constantTimeEqual,
    randomHex, portHasForeignListener, scheduleReconnect,
    _t: { hostOf, computeSpeed, MAX_FRAME, PREF_PORT, PORT_MIN, PORT_MAX, WS_PATHNAME,
          KEEPALIVE_MS, ALARM_PERIOD_MIN, BACKOFF_SEC, HMAC_CTX_BRIDGE, HMAC_CTX_EXTENSION,
          HANDSHAKE_FAIL_LIMIT },
  };
}
