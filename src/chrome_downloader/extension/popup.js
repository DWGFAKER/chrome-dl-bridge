// popup.js —— 一屏看懂 + 每个告警都给可操作按钮（W20：不许只报错不给路）
//
// 数据来源：只读 Service Worker 的账（chrome.runtime.sendMessage 问 SW，SW 读 storage）。
// popup 自己不做任何判断，也不写证据 —— 它只是把 SW 记下来的东西显示出来。
'use strict';

const $ = (id) => document.getElementById(id);

// 与 background.js 的文案一一对应（W20 逐字判据）。按 notice.kind 决定按钮。
const ACTIONS = {
  key_mismatch: [
    { label: '重新认一下', action: 'reconnect', primary: true },
    { label: '先别连', action: 'hold', primary: false },
  ],
  handshake_fail: [
    { label: '重新认一下', action: 'reconnect', primary: true },
    { label: '先别连', action: 'hold', primary: false },
  ],
  bridge_id_changed: [
    { label: '重新认一下', action: 'forget_then_reconnect', primary: true },
    { label: '先别连', action: 'hold', primary: false },
  ],
  hijack: [
    { label: '拉黑并断开', action: 'forget', primary: true },
    { label: '让我看看详情', action: 'details', primary: false },
  ],
  foreign_port: [
    { label: '换个端口', action: 'port', primary: true },
    { label: '知道了', action: 'hold', primary: false },
  ],
  server_error: [
    { label: '重新认一下', action: 'reconnect', primary: true },
    { label: '先别连', action: 'hold', primary: false },
  ],
};

function ask(type, extra) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(Object.assign({ type }, extra || {}), (resp) => {
        if (chrome.runtime.lastError) return resolve({ ok: false,
          error: chrome.runtime.lastError.message });
        resolve(resp || { ok: false, error: 'NO_REPLY' });
      });
    } catch (e) { resolve({ ok: false, error: String(e && e.message || e) }); }
  });
}

function shortHex(v, n) { return v ? String(v).slice(0, n || 12) : '—'; }

async function render() {
  const st = await ask('status');
  const connected = !!(st && st.connected);
  const bad = !!(st && st.notice && st.notice.kind);
  $('dot').className = 'dot ' + (connected ? 'on' : bad ? 'bad' : 'off');
  $('ledText').textContent = connected
    ? `接上了（内部门牌 ${st.port || '—'}）`
    : bad ? '需要你拍个板' : '还没接上（机器人那头发号施令的程序没在跑，属正常）';
  $('port').textContent = st.port || (st.preferredPort ? st.preferredPort + '（首选）' : '—');
  $('bridgeId').textContent = st.paired ? shortHex(st.bridgeId, 8) : '还没配上';
  $('peerPid').textContent = st.peerPid || '—';
  $('chrome').textContent = st.chromeVersion || '—';
  $('tasks').textContent = st.tasks != null ? st.tasks : '—';
  renderNotice(st);
}

function renderNotice(st) {
  const box = $('notice'), n = st && st.notice;
  if (!n || !n.kind) { box.hidden = true; $('noticeBtns').innerHTML = ''; return; }
  box.hidden = false;
  box.className = 'notice ' + ({ hijack: 'red', key_mismatch: 'red', bridge_id_changed: 'yellow',
    handshake_fail: 'yellow', foreign_port: 'yellow', server_error: 'yellow' }[n.kind] || 'gray');
  $('noticeText').textContent = n.detail && n.detail.text
    ? n.detail.text : '有点不对劲，看一眼下面这行。';
  const bits = [];
  if (n.detail && n.detail.port) bits.push('门牌 ' + n.detail.port);
  if (n.detail && n.detail.reason) bits.push('原因 ' + n.detail.reason);
  if (n.detail && n.detail.attempts) bits.push('已试 ' + n.detail.attempts + ' 次');
  if (n.detail && n.detail.peerBridgeId) bits.push('对面编号 ' + shortHex(n.detail.peerBridgeId, 8));
  if (n.detail && n.detail.now) bits.push('新编号 ' + shortHex(n.detail.now, 8));
  if (n.detail && n.detail.old) bits.push('旧编号 ' + shortHex(n.detail.old, 8));
  bits.push('时间 ' + new Date(n.at).toLocaleTimeString());
  $('noticeWhy').textContent = bits.join(' ｜ ');
  const btns = $('noticeBtns');
  btns.innerHTML = '';
  (ACTIONS[n.kind] || [{ label: '知道了', action: 'hold' }]).forEach((a) => {
    const b = document.createElement('button');
    b.textContent = a.label;
    if (!a.primary) b.className = 'ghost';
    b.onclick = () => doAction(a.action, n);
    btns.appendChild(b);
  });
}

async function doAction(action, n) {
  if (action === 'reconnect') {
    $('ledText').textContent = '正在重连…';
    await ask('reconnect');
  } else if (action === 'hold') {
    // "先别连"：保留告警、挂起自动重连（不再撞墙，也不假装没事）
    await ask('hold');
  } else if (action === 'forget') {
    await ask('forget');
  } else if (action === 'forget_then_reconnect') {
    await ask('forget');
    await ask('reconnect');
  } else if (action === 'unblacklist') {
    await ask('unblacklist');
  } else if (action === 'details') {
    // "让我看看详情"：就地展开全部字段（不抹平告警、不改配置）
    $('noticeWhy').textContent = JSON.stringify((n && n.detail) || {}, null, 1).replace(/\n\s*/g, ' ');
    const b = document.createElement('button');
    b.className = 'ghost'; b.textContent = '拉黑并断开';
    b.onclick = () => doAction('forget', n);
    $('noticeBtns').appendChild(b);
    return;
  } else if (action === 'port') {
    const p = prompt('换一个内部门牌（18888～18897 之间的数字）。\n' +
                     '记得同时让对面也换成同一个，例如：python bridge.py --port 18889',
                     String((n && n.detail && n.detail.port ? n.detail.port + 1 : 18889)));
    const num = parseInt(p, 10);
    if (num >= 18888 && num <= 18897) {
      await ask('setPort', { port: num });
      await ask('reconnect');
    }
  }
  await render();
}

document.addEventListener('DOMContentLoaded', () => {
  $('reconnect').onclick = async () => { await ask('reconnect'); await render(); };
  $('forget').onclick = async () => { await ask('forget'); await render(); };
  render();
  setInterval(render, 2000);            // 开着窗口时能看见状态自己变绿
});
