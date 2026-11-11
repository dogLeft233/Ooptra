/* Ooptra · Oopz ⇄ OneBot v11 桥接控制台 —— 前端逻辑（无外部依赖） */
'use strict';

const TOKEN_KEY = 'oopz.webui.token';
const SIGNED_OUT_KEY = 'oopz.webui.signedout';
const ADV_KEY = 'oopz.webui.advanced';
const POLL_MS = 3000;

const PAGE_META = {
  overview: ['总览', '一眼看清链路、语音会话、流量与最近动态'],
  logs: ['日志', '实时跟随日志文件，可过滤、换行、下载'],
  voice: ['语音台', '手动对话、自动串门、房间成员与共享记忆'],
  config: ['配置', '连接 / 语音模型 / 系统；常用项直接改'],
  account: ['账号', '查看凭据状态，或重新登录 Oopz'],
  maintenance: ['更新与备份', '准备、切换与恢复，在部署机器上完成维护'],
};
const GROUP_TITLE = { oopz: 'Oopz 账号与事件', onebot: 'OneBot v11 桥接', webui: 'Web 控制台', voice: '语音对话 Agent', voice_api: '语音 HTTP API' };

let pollTimer = null;
let logStream = null;
let loginPollTimer = null;
let configSchema = null;
let dirty = {};
let logBuffer = [];
let eventFilter = 'msg';
let showAdvanced = localStorage.getItem(ADV_KEY) === '1';

const $ = (id) => document.getElementById(id);
const show = (el, on) => { el.hidden = !on; };

/* ───────── 基础设施 ───────── */

class AuthError extends Error {}

function withToken(path) {
  return path; // Browser requests use the HttpOnly session cookie, never the shared password.
}

async function api(path, opts = {}) {
  const controller = new AbortController();
  const timeout = opts.timeout || 90000;
  const deadline = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(withToken(path), {
      method: opts.method || 'GET',
      headers: opts.body && !(opts.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {},
      body: opts.body instanceof FormData ? opts.body : opts.body ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
    });
    let payload = null;
    try { payload = await res.json(); } catch (err) {
      if (controller.signal.aborted) throw err;
      payload = null;
    }
    if (res.status === 401) {
      if (!path.startsWith('/api/auth/') && !$('screen-app').hidden) showLogin({gate: 'token'});
      throw new AuthError('请重新登录控制台');
    }
    if (!res.ok) throw new Error((payload && payload.error) || ('HTTP ' + res.status));
    return payload || {};
  } catch (err) {
    if (controller.signal.aborted) throw new Error('请求超过 ' + Math.ceil(timeout / 1000) + ' 秒，请检查连接后重试');
    throw err;
  } finally { clearTimeout(deadline); }
}

function toast(title, text, kind) {
  const host = $('toasts');
  const key = JSON.stringify([title, text || '', kind || '']);
  const existing = [...host.children].find(item => item.dataset.key === key);
  if (existing) existing.remove();
  const box = document.createElement('div');
  box.className = 'toast ' + (kind || '');
  box.dataset.key = key;
  box.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  box.innerHTML = '<b></b><span></span><button class="toast-close" type="button" aria-label="关闭通知">×</button>';
  box.querySelector('b').textContent = title;
  box.querySelector('span').textContent = text || '';
  host.appendChild(box);
  while (host.childElementCount > 3) host.firstElementChild.remove();
  host.scrollTop = host.scrollHeight;
  positionToasts();
  let timer;
  const dismiss = () => { clearTimeout(timer); box.remove(); };
  const resume = () => { clearTimeout(timer); timer = setTimeout(dismiss, kind === 'err' ? 8000 : 4000); };
  box.querySelector('button').addEventListener('click', dismiss);
  box.addEventListener('focusin', () => clearTimeout(timer));
  box.addEventListener('focusout', resume);
  resume();
}

function positionToasts() {
  const host = $('toasts');
  if (!host?.childElementCount) return;
  const bar = $('savebar');
  const bounds = bar && !bar.hidden ? bar.getBoundingClientRect() : null;
  const offset = bounds?.width && bounds.top < innerHeight ? Math.max(18, Math.ceil(innerHeight - bounds.top + 12)) : 18;
  host.style.setProperty('--toast-offset', offset + 'px');
}

function fmtDuration(seconds) {
  if (!seconds || seconds <= 0) return '—';
  const t = Math.floor(seconds);
  const d = Math.floor(t / 86400), h = Math.floor((t % 86400) / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  if (d) return d + ' 天 ' + h + ' 小时';
  if (h) return h + ' 小时 ' + m + ' 分';
  if (m) return m + ' 分 ' + s + ' 秒';
  return s + ' 秒';
}

function fmtClock(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

function fmtAgo(ts) {
  if (!ts) return '从未';
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diff < 60) return diff + ' 秒前';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
}

function text(id, value) {
  const node = $(id);
  if (node) node.textContent = value === undefined || value === null || value === '' ? '—' : String(value);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// 复制：navigator.clipboard 只在**安全上下文**（https 或 localhost）存在。
// 用局域网 IP 打开控制台时它是 undefined，旧代码在这里直接抛 TypeError，
// 连失败提示都不会弹 —— 三个「复制…」按钮点了没反应就是这个原因。
// 所以：能用就用，用不了退回 execCommand；再不行就把值弹出来让用户手抄。
function legacyCopy(value) {
  try {
    const ta = document.createElement('textarea');
    ta.value = value;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.left = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

function copyText(value) {
  const s = String(value ?? '');
  const done = () => toast('已复制', s.slice(0, 40), 'ok');
  const manual = (why) => {
    toast('复制失败', why, 'err');
    // prompt 的默认值在浏览器里是选中状态，Ctrl+C 即可
    window.prompt('请手动复制（Ctrl+C）', s);
  };
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(s).then(done, () => {
      if (legacyCopy(s)) done();
      else manual('浏览器拒绝了剪贴板访问');
    });
  }
  if (legacyCopy(s)) {
    done();
  } else {
    manual('当前地址不是安全上下文（http + 非 localhost），无法直接写剪贴板');
  }
  return Promise.resolve();
}

let dialogPending = false;
let cancelActiveDialog = null;

function confirmDialog(title, message, yesLabel) {
  if (dialogPending) return Promise.resolve(false);
  dialogPending = true;
  return new Promise((resolve) => {
    const previousFocus = document.activeElement;
    const backdrop = $('dialog');
    const panel = backdrop.querySelector('.dialog');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', 'dialog-title');
    panel.setAttribute('aria-describedby', 'dialog-text');
    const surfaces = [$('screen-app'), $('screen-login')].map(el => ({el, inert: el.inert}));
    surfaces.forEach(({el}) => { el.inert = true; });
    $('dialog-title').textContent = title;
    $('dialog-text').textContent = message;
    $('dialog-yes').textContent = yesLabel || '确定';
    show(backdrop, true);
    const done = (ok) => {
      show(backdrop, false);
      dialogPending = false;
      cancelActiveDialog = null;
      surfaces.forEach(({el, inert}) => { el.inert = inert; });
      document.removeEventListener('keydown', onKey, true);
      $('dialog-yes').onclick = $('dialog-no').onclick = null;
      if (previousFocus && previousFocus.isConnected) previousFocus.focus({preventScroll: true});
      resolve(ok);
    };
    const onKey = (event) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); done(false); }
      if (event.key === 'Tab') {
        event.preventDefault();
        const next = document.activeElement === $('dialog-no') ? $('dialog-yes') : $('dialog-no');
        next.focus();
      }
    };
    document.addEventListener('keydown', onKey, true);
    cancelActiveDialog = () => done(false);
    $('dialog-yes').onclick = () => done(true);
    $('dialog-no').onclick = () => done(false);
    $('dialog-no').focus();
  });
}

async function withButtonBusy(button, action, label) {
  if (button.disabled) return;
  const previousLabel = button.textContent;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.textContent = label;
  try { return await action(); }
  finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    button.textContent = previousLabel;
  }
}

function credsUsable(creds) {
  if (!creds) return false;
  return Boolean(String(creds.jwt_token || '').trim() || creds.has_password);
}

/* ───────── 登录闸门 ───────── */

async function tryStatus() {
  try {
    return { kind: 'ok', data: await api('/api/status') };
  } catch (err) {
    if (err instanceof AuthError) return { kind: 'auth' };
    return { kind: 'error', message: err.message };
  }
}

async function guard() {
  try {
    const auth = await api('/api/auth/status');
    if (!auth.configured) return {gate: auth.setup_allowed ? 'setup' : 'unconfigured'};
    if (!auth.authenticated) return {gate: 'token'};
  } catch (err) {
    return {gate: 'offline', message: err.message};
  }
  const probe = await tryStatus();
  if (probe.kind === 'auth') return { gate: 'token' };
  if (probe.kind === 'error') return { gate: 'offline', message: probe.message };
  state = probe.data;
  try {
    const cred = await api('/api/credentials');
    state.creds = cred.credentials || {};
  } catch (err) {
    state.creds = {};
  }
  if (!credsUsable(state.creds)) return { gate: 'oopz' };
  return { gate: null };
}

let state = {};
let loginGate = 'token';

function showLogin(result) {
  if (cancelActiveDialog) cancelActiveDialog();
  stopPolling();
  stopLogStream();
  clearTimeout(loginPollTimer);
  loginGate = result.gate;
  show($('screen-app'), false);
  show($('screen-login'), true);

  const titles = {
    token: ['登录控制台', result.message || (sessionStorage.getItem(SIGNED_OUT_KEY) === '1' ? '已退出登录，请重新输入控制台密码。' : '输入控制台密码，继续管理你的 Ooptra。')],
    setup: ['设置控制台密码', '首次使用请设置密码。插件 API Token 也使用此密码；以后在「配置 → 系统」统一修改。'],
    unconfigured: ['请先设置密码', '请在部署机器本机打开控制台设置首次密码，或在 config.py 中填写 WEBUI_CONFIG.token，然后重启。'],
    oopz: ['登录 Oopz', '桥接需要一份可用的 Oopz 凭据；登录成功后会写入 config.py 与 private_key.py 并自动重连。'],
    offline: ['连不上控制台后端', result.message || '请确认进程仍在运行。'],
  };
  const buttons = { token: '登录控制台', setup: '设置密码并进入', unconfigured: '重新检查', oopz: '登录并进入', offline: '重试' };
  const t = titles[loginGate] || titles.token;
  $('login-title').textContent = t[0];
  $('login-sub').className = loginGate === 'offline' ? 'hint err' : 'hint';
  $('login-sub').textContent = t[1];
  show($('login-token-field'), ['token', 'setup'].includes(loginGate));
  show($('login-token-confirm-field'), loginGate === 'setup');
  $('login-token').value = '';
  $('login-token-confirm').value = '';
  $('login-token').autocomplete = loginGate === 'setup' ? 'new-password' : 'current-password';
  show($('login-oopz-field'), loginGate === 'oopz');
  show($('login-alt'), loginGate === 'oopz');
  $('login-submit').textContent = buttons[loginGate] || '进入';
  $('login-msg').textContent = '';
  $('login-msg').className = 'login-msg';
  const focus = ['token', 'setup'].includes(loginGate) ? $('login-token') : (loginGate === 'oopz' ? $('login-phone') : $('login-submit'));
  if (focus) setTimeout(() => focus.focus(), 60);
}

function loginMsg(message, kind) {
  $('login-msg').textContent = message || '';
  $('login-msg').className = 'login-msg ' + (kind || '');
}

async function onLoginSubmit(event) {
  event.preventDefault();
  const button = $('login-submit');
  if (['offline', 'unconfigured'].includes(loginGate)) { const g = await guard(); return g.gate ? showLogin(g) : enterApp(); }
  if (['token', 'setup'].includes(loginGate)) {
    const value = $('login-token').value.trim();
    if (!value) return loginMsg('请填写控制台密码', 'err');
    if (loginGate === 'setup' && value !== $('login-token-confirm').value.trim()) return loginMsg('两次输入的密码不一致', 'err');
    button.disabled = true;
    loginMsg('正在验证…');
    try {
      await api('/api/auth/' + (loginGate === 'setup' ? 'setup' : 'login'), {method: 'POST', body: {password: value}});
      $('login-token').value = '';
      $('login-token-confirm').value = '';
      sessionStorage.removeItem(SIGNED_OUT_KEY);
      const g = await guard();
      return g.gate ? showLogin(g) : enterApp();
    } catch (err) {
      loginMsg(err instanceof AuthError ? '控制台密码不正确' : err.message, 'err');
    } finally { button.disabled = false; }
    return;
  }
  if (loginGate === 'oopz') {
    button.disabled = true;
    loginMsg('正在登录 Oopz…');
    try {
      await api('/api/login/api', {
        method: 'POST',
        body: { phone: $('login-phone').value, password: $('login-password').value, timeout: 60 },
      });
      loginMsg('登录成功，正在进入…', 'ok');
      $('login-password').value = '';
      const g = await guard();
      return g.gate ? showLogin(g) : enterApp();
    } catch (err) {
      loginMsg(err.message, 'err');
    } finally {
      button.disabled = false;
    }
  }
}

function enterApp() {
  sessionStorage.removeItem(SIGNED_OUT_KEY);
  show($('screen-login'), false);
  show($('screen-app'), true);
  setupNav();
  refreshCredentials();
  loadLogFiles();
  switchPage(pageFromLocation(), '', {replace: true});
  startPolling();
}

async function logout() {
  try { await api('/api/auth/logout', {method: 'POST', body: {}}); }
  catch (err) { toast('退出登录失败', err.message + '；请重试。', 'err'); return; }
  sessionStorage.setItem(SIGNED_OUT_KEY, '1');
  clearLegacyCredentials();
  stopLogStream();
  stopPolling();
  dirty = {};
  configSchema = null;
  if (window.resetVoiceOutput) window.resetVoiceOutput();
  logBuffer = [];
  state = {};
  updateSavebar();
  showLogin({gate: 'token'});
}

/* ───────── 导航 ───────── */

let navReady = false;
const PAGE_PATHS = {overview: '/overview', logs: '/logs', voice: '/voice', maintenance: '/maintenance', config: '/config', account: '/account'};
let pageHistoryIndex = Number(history.state?.ooptraPageIndex) || 0;
let restoringPageHistory = false;
let pageHistoryRequest = 0;

function pageFromLocation() {
  const path = location.protocol === 'file:' ? location.hash.replace(/^#/, '') : location.pathname;
  return Object.keys(PAGE_PATHS).find(name => PAGE_PATHS[name] === path) || 'overview';
}

function writePageLocation(name, replace) {
  const url = new URL(location.href);
  if (url.protocol === 'file:') url.hash = PAGE_PATHS[name];
  else {
    url.pathname = replace && name === 'overview' && url.pathname === '/' ? '/' : PAGE_PATHS[name];
    url.hash = '';
  }
  const same = url.href === location.href;
  if (!replace && same) return;
  if (!replace) pageHistoryIndex++;
  history[replace ? 'replaceState' : 'pushState']({...history.state, ooptraPageIndex: pageHistoryIndex}, '', url);
}

async function followPageHistory() {
  const request = ++pageHistoryRequest;
  if (restoringPageHistory) { restoringPageHistory = false; return; }
  const nextIndex = Number(history.state?.ooptraPageIndex);
  if ($('screen-app').hidden) { if (Number.isFinite(nextIndex)) pageHistoryIndex = nextIndex; return; }
  const current = document.querySelector('.page.is-active')?.id.replace('page-', '') || 'overview';
  const changed = await switchPage(pageFromLocation(), '', {fromHistory: true});
  if (request !== pageHistoryRequest) return;
  if (changed) { if (Number.isFinite(nextIndex)) pageHistoryIndex = nextIndex; }
  else if (Number.isFinite(nextIndex) && nextIndex !== pageHistoryIndex) {
    restoringPageHistory = true;
    history.go(pageHistoryIndex - nextIndex);
  } else writePageLocation(current, true);
}

function setupNav() {
  if (navReady) return;
  navReady = true;
  window.addEventListener('resize', positionToasts);
  window.addEventListener('scroll', positionToasts, {passive: true});
  window.addEventListener('popstate', followPageHistory);
  if (location.protocol === 'file:') window.addEventListener('hashchange', () => {
    // Hash edits emit popstate too; the active page check avoids opening two dialogs.
    if (!navigationPending && !isPage(pageFromLocation())) followPageHistory();
  });
  document.querySelectorAll('.nav-item[data-page]').forEach((item) => {
    item.addEventListener('click', () => switchPage(item.dataset.page, item.dataset.tab || ''));
  });
  $('ev-filter').addEventListener('click', (event) => {
    const button = event.target.closest('.seg-btn');
    if (!button) return;
    eventFilter = button.dataset.mode;
    document.querySelectorAll('#ev-filter .seg-btn').forEach((item) => item.classList.toggle('is-active', item === button));
    renderOverview();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden || $('screen-app').hidden) stopPolling(); else startPolling();
  });
}

let navigationPending = false;

async function switchPage(name, tab, options = {}) {
  if (!Object.prototype.hasOwnProperty.call(PAGE_PATHS, name)) name = 'overview';
  if (navigationPending) return false;
  if (name !== 'config' && isPage('config') && draftCount()) {
    navigationPending = true;
    const leave = await confirmDialog('配置尚未保存', '离开后草稿仍会保留在本页，返回配置可继续编辑。刷新或关闭页面前请保存。', '保留草稿并离开');
    navigationPending = false;
    if (!leave) return false;
    if (options.fromHistory && pageFromLocation() !== name) return false;
  }
  const changed = !isPage(name);
  if (!options.fromHistory) writePageLocation(name, !!options.replace);
  const meta = PAGE_META[name] || PAGE_META.overview;
  document.querySelectorAll('.nav-item[data-page]').forEach((item) => {
    const match = item.dataset.page === name && (!item.dataset.tab || item.dataset.tab === (tab || ''));
    const primary = item.dataset.page === name && !item.dataset.tab;
    item.classList.toggle('is-active', match || (primary && !tab));
  });
  document.querySelectorAll('.page').forEach((page) => {
    page.classList.toggle('is-active', page.id === 'page-' + name);
  });
  text('page-title', meta[0]);
  text('page-sub', meta[1]);
  renderPageTools(name);
  document.querySelectorAll('.nav-item[data-page]').forEach((item) => {
    const active = item.dataset.page === name;
    item.classList.toggle('is-active', active);
    if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
  });
  if (name === 'logs') startLogStream();
  else stopLogStream();
  if (name === 'config') loadConfig();
  if (name === 'account') refreshCredentials();
  if (name === 'maintenance' && window.refreshMaintenance) window.refreshMaintenance();
  if (name === 'voice') {
    if (tab) switchVoiceTab(tab);
    refreshVoiceStatus();
  }
  if (changed) window.scrollTo(0, 0);
  return true;
}

function renderPageTools(name) {
  const tools = $('page-tools');
  if (name === 'overview') {
    tools.innerHTML = '<span class="chip" id="updated-at">—</span><button class="btn ghost sm" id="tool-refresh">立即刷新</button>';
    const button = $('tool-refresh');
    button.addEventListener('click', () => withButtonBusy(button, async () => {
      if (await refreshStatus()) toast('已刷新', '状态已重新读取', 'ok');
      else if (!$('screen-app').hidden) toast('刷新失败', '状态尚未更新，请检查后端连接后重试。', 'err');
    }, '刷新中…'));
  } else if (name === 'account') {
    tools.innerHTML = '<button class="btn ghost sm" id="tool-cred">刷新凭据</button>';
    const button = $('tool-cred');
    button.addEventListener('click', () => withButtonBusy(button, refreshCredentials, '读取中…'));
  } else if (name === 'config') {
    tools.innerHTML = '<button class="btn ghost sm" id="tool-adv">高级选项</button>' +
      '<button class="btn ghost sm" id="tool-reload">重新读取</button>';
    const adv = $('tool-adv');
    adv.classList.toggle('toggle-on', showAdvanced);
    adv.setAttribute('aria-pressed', String(showAdvanced));
    adv.addEventListener('click', () => {
      showAdvanced = !showAdvanced;
      localStorage.setItem(ADV_KEY, showAdvanced ? '1' : '0');
      adv.classList.toggle('toggle-on', showAdvanced);
      adv.setAttribute('aria-pressed', String(showAdvanced));
      renderConfig();
    });
    const reload = $('tool-reload');
    reload.addEventListener('click', () => withButtonBusy(reload, async () => {
      if (await loadConfig()) toast('已重新读取', '保留当前未保存的草稿', 'ok');
    }, '读取中…'));
  } else {
    tools.innerHTML = '';
  }
}

/* ───────── 概览 ───────── */

function renderOverview() {
  const bridge = (state && state.bridge) || {};
  const rt = bridge.runtime || {};
  const oopz = bridge.oopz || {};
  const onebot = bridge.onebot || {};
  const traffic = bridge.traffic || {};
  const proc = (state && state.process) || {};

  let tone = 'ok';
  let title = '链路正常';
  let detail = 'Oopz 与 OneBot 端双向可用，事件和指令都在流动。';
  let action = false;

  if (!rt.running) {
    tone = 'err'; action = true;
    title = '桥接未运行';
    detail = rt.last_error ? '上一次错误：' + rt.last_error : 'SDK 会话没有起来，点右侧按钮重试。';
  } else if (!oopz.connected) {
    tone = 'err'; action = true;
    title = 'Oopz 未连接';
    detail = (oopz.last_error ? '最近错误：' + oopz.last_error + '。' : '') + '检查账号凭据与网络，或在账号页重新登录。';
  } else if (!onebot.connected) {
    tone = 'warn'; action = true;
    title = '等待 OneBot 端接入';
    detail = 'Oopz 侧正常，反向 WS 尚未连上 ' + (onebot.target || '目标地址') + '。确认对端已启用 OneBot v11 反向 WS。';
  }

  const verdict = $('verdict');
  verdict.dataset.tone = tone;
  text('verdict-title', title);
  text('verdict-detail', detail);
  show($('verdict-action'), action);

  setDot('tile-oopz', oopz.connected ? 'ok' : 'err');
  text('t-oopz-state', oopz.connected ? '已连接' : '未连接');
  text('t-oopz-sub', (oopz.target || '—') + ' · 持续 ' + fmtDuration(oopz.uptime_seconds) + (oopz.joined_areas ? ' · 已加入 ' + oopz.joined_areas + ' 个域' : ''));

  setDot('tile-onebot', onebot.connected ? 'ok' : (rt.running && oopz.connected ? 'warn' : 'err'));
  text('t-onebot-state', onebot.connected ? '已接入' : '等待中');
  text('t-onebot-sub', (onebot.target || '—') + ' · 重连 ' + (onebot.attempts ?? 0) + ' 次 / 断开 ' + (onebot.drops ?? 0) + ' 次');

  setDot('tile-traffic', traffic.events_total ? 'ok' : 'warn');
  text('t-total', traffic.events_total ?? 0);
  const byType = traffic.events_by_type || {};
  const countOf = (prefix) => Object.entries(byType).filter(([key]) => key.startsWith(prefix))
    .reduce((sum, [, value]) => sum + value, 0);
  text('t-types', '消息 ' + countOf('message') + ' · 通知 ' + countOf('notice') + ' · 心跳 ' + countOf('meta_event'));

  setDot('tile-process', rt.running ? 'ok' : 'err');
  text('t-uptime', fmtDuration(rt.uptime_seconds));
  text('t-process-sub', 'PID ' + (proc.pid ?? '—') + ' · Python ' + (proc.python ?? '—') + ' · 桥接重启 ' + (rt.restarts ?? 0) + ' 次');

  const account = (oopz.nickname || '') + (oopz.self_uid ? ' · ' + oopz.self_uid.slice(0, 8) : '');
  if (account) text('t-oopz-sub', account + ' · ' + (oopz.target || '—') + ' · 持续 ' + fmtDuration(oopz.uptime_seconds));

  const allEvents = bridge.recent_events || [];
  const shownEvents = allEvents.filter((item) => eventFilter === 'all' || !String(item.kind || '').startsWith('meta_event'));
  renderFeed('feed-events', 'ev-count', shownEvents, 'event',
    eventFilter === 'msg' ? '最近只有心跳，切到「全部」可以看' : '还没有事件推送给对端');
  text('ev-count', shownEvents.length === allEvents.length ? allEvents.length + ' 条' : shownEvents.length + ' / ' + allEvents.length + ' 条');
  renderFeed('feed-actions', 'ac-count', bridge.recent_actions || [], 'action', '对端还没有下发过 action');

  const last = traffic.last_event_at;
  text('updated-at', last ? '最近事件 ' + fmtAgo(last) : '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false }));
  renderRailMeta(tone, title);
}

function setDot(tileId, tone) {
  const tile = $(tileId);
  if (!tile) return;
  const dot = tile.querySelector('.dot');
  if (dot) dot.className = 'dot ' + tone;
}

function renderRailMeta(tone, title) {
  $('rail-meta').innerHTML = '<span class="dot ' + tone + '" style="display:inline-block;margin-right:6px"></span>' + escapeHtml(title);
}

function renderFeed(listId, countId, items, kind, emptyText) {
  const list = $(listId);
  text(countId, items.length + ' 条');
  if (!items.length) {
    list.innerHTML = '<li class="empty">' + escapeHtml(emptyText || '暂无数据') + '</li>';
    return;
  }
  list.innerHTML = items.slice(0, 24).map((item) => {
    if (kind === 'action') {
      const meta = [item.group_id ? '群 ' + item.group_id : '', item.user_id ? '用户 ' + item.user_id : ''].filter(Boolean).join(' · ');
      return '<li class="feed-item"><i class="kind-dot k-action"></i><div class="feed-main">' +
        '<div class="feed-top"><b>' + escapeHtml(item.action || '—') + '</b><span class="meta">' + escapeHtml(meta) + '</span></div>' +
        '<p class="feed-text">来自对端的调用</p></div><time>' + fmtClock(item.time) + '</time></li>';
    }
    const key = String(item.kind || '');
    const cls = key.startsWith('message') ? 'k-message' : (key.startsWith('notice') ? 'k-notice' : 'k-meta');
    const meta = [item.group_id ? '群 ' + item.group_id : '', item.user_id ? '用户 ' + item.user_id : ''].filter(Boolean).join(' · ');
    return '<li class="feed-item"><i class="kind-dot ' + cls + '"></i><div class="feed-main">' +
      '<div class="feed-top"><b>' + escapeHtml(key || '—') + '</b><span class="meta">' + escapeHtml(meta) + '</span></div>' +
      '<p class="feed-text">' + (escapeHtml(item.preview || '') || '<span class="muted">（无文本内容）</span>') + '</p></div><time>' + fmtClock(item.time) + '</time></li>';
  }).join('');
}

async function refreshStatus() {
  try {
    state = { ...state, ...(await api('/api/status')) };
  } catch (err) {
    if (err instanceof AuthError) { stopPolling(); showLogin({ gate: 'token' }); return false; }
    text('verdict-title', '读取状态失败');
    text('verdict-detail', err.message);
    $('verdict').dataset.tone = 'err';
    text('updated-at', '连接失败 · 显示上次状态');
    renderRailMeta('err', '状态读取失败');
    return false;
  }
  renderPageToolsIfNeeded();
  renderVersion();
  if (isPage('overview')) renderOverview();
  else {
    const b = state.bridge || {};
    const rt = b.runtime || {};
    const healthy = rt.running && (b.oopz || {}).connected && (b.onebot || {}).connected;
    renderRailMeta(healthy ? 'ok' : (rt.running ? 'warn' : 'err'), healthy ? '链路正常' : (rt.running ? '部分异常' : '桥接未运行'));
    syncConfigChips();
  }
  if (isPage('voice')) {
    await _origRefreshVoiceStatus();
    if ($('vpane-auto-visit').classList.contains('is-active')) await refreshAutoVisit(false);
  }
  if (isPage('maintenance') && window.refreshMaintenance) await window.refreshMaintenance();
  return true;
}

const isPage = (name) => document.querySelector('.page.is-active')?.id === 'page-' + name;

function renderVersion() {
  const version = (state && state.process && state.process.version) || '';
  const el = $('rail-ver');
  if (el && version && el.textContent !== 'v' + version) el.textContent = 'v' + version;
}

function renderPageToolsIfNeeded() {
  if (isPage('overview') && !$('updated-at')) renderPageTools('overview');
}

function startPolling() {
  stopPolling();
  if ($('screen-app').hidden) return;
  refreshStatus();
  pollTimer = setInterval(() => { if (!document.hidden) refreshStatus(); }, POLL_MS);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function restartBridge() {
  const ok = await confirmDialog('重新连接桥接', '会断开当前 Oopz 与 OneBot 端的连接并立即重连（通常几秒）。', '重连');
  if (!ok) return;
  try {
    const result = await api('/api/bridge/restart', { method: 'POST', body: {} });
    toast('已请求重连', result.message || '桥接正在重建连接', 'ok');
    setTimeout(refreshStatus, 1500);
  } catch (err) {
    toast('重连失败', err.message, 'err');
  }
}

/* ───────── 日志 ───────── */

const LINE_RE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[([^\]]+)\]\s+([A-Z]+):\s?([\s\S]*)$/;
const MAX_NODES = 4000;
let logTerms = [];
let logLevel = '';

function levelTone(level) {
  if (level === 'ERROR' || level === 'CRITICAL') return 'lv-err';
  if (level === 'WARNING' || level === 'WARN') return 'lv-warn';
  if (level === 'DEBUG' || level === 'TRACE') return 'lv-debug';
  return 'lv-info';
}

function passesFilter(raw) {
  if (logLevel === 'WARN' && !/(WARNING|WARN|ERROR|CRITICAL)/.test(raw)) return false;
  if (logLevel === 'ERROR' && !/(ERROR|CRITICAL)/.test(raw)) return false;
  if (!logTerms.length) return true;
  const lower = raw.toLowerCase();
  return logTerms.every((term) => lower.includes(term));
}

function highlight(escaped, terms) {
  if (!terms.length) return escaped;
  return terms.reduce((acc, term) => {
    if (!term) return acc;
    const safe = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return acc.replace(new RegExp('(' + safe + ')', 'gi'), '<mark class="hit">$1</mark>');
  }, escaped);
}

function logNode(raw) {
  const match = LINE_RE.exec(raw);
  let time = '', level = '', body = raw;
  if (match) { time = match[1].slice(11); level = match[3]; body = '[' + match[2] + '] ' + match[4]; }
  const node = document.createElement('div');
  node.className = 'ln ' + levelTone(level) + (match ? '' : ' is-plain');
  node.innerHTML = '<span class="ln-time">' + escapeHtml(time) + '</span>' +
    '<span class="ln-level">' + escapeHtml(level || '·') + '</span>' +
    '<span class="ln-body">' + highlight(escapeHtml(body), logTerms) + '</span>';
  return node;
}

async function loadLogFiles() {
  try {
    const data = await api('/api/logs');
    const select = $('log-file');
    select.innerHTML = (data.files || []).map((item) =>
      '<option value="' + escapeHtml(item.name) + '">' + escapeHtml(item.name) + ' · ' + (item.size / 1024).toFixed(0) + ' KB</option>').join('');
    if (!select.value && data.default) select.value = data.default;
  } catch (err) {
    text('log-status', err.message);
  }
}

function stopLogStream() {
  if (logStream) { logStream.close(); logStream = null; }
}

function appendLog(raw) {
  logBuffer.push(raw);
  if (logBuffer.length > MAX_NODES * 1.5) logBuffer = logBuffer.slice(-MAX_NODES);
  if (!passesFilter(raw)) return;
  const view = $('log-view');
  const stick = view.scrollTop + view.clientHeight >= view.scrollHeight - 60;
  view.appendChild(logNode(raw));
  while (view.childElementCount > MAX_NODES) view.removeChild(view.firstChild);
  if (stick) view.scrollTop = view.scrollHeight;
  else show($('log-jump'), true);
}

function rerenderLogs() {
  const view = $('log-view');
  view.innerHTML = '';
  const matched = logBuffer.filter(passesFilter).slice(-MAX_NODES);
  const fragment = document.createDocumentFragment();
  matched.forEach((raw) => fragment.appendChild(logNode(raw)));
  view.appendChild(fragment);
  view.scrollTop = view.scrollHeight;
  show($('log-jump'), false);
}

async function loadLogTail() {
  try {
    const data = await api('/api/logs/tail?lines=' + encodeURIComponent($('log-lines').value) +
      '&file=' + encodeURIComponent($('log-file').value));
    $('log-view').innerHTML = '';
    logBuffer = [];
    (data.lines || []).forEach(appendLog);
    $('log-view').scrollTop = $('log-view').scrollHeight;
    text('log-status', '已加载 ' + (data.lines || []).length + ' 行（未跟随）');
  } catch (err) {
    text('log-status', err.message);
  }
}

function startLogStream() {
  stopLogStream();
  if (!isPage('logs')) return;
  if (!$('log-follow').checked) {
    text('log-status', '读取中…');
    loadLogTail();
    return;
  }
  const file = $('log-file').value;
  text('log-status', '实时跟随中…');
  logStream = new EventSource(withToken('/api/logs/stream?lines=' + encodeURIComponent($('log-lines').value) +
    '&file=' + encodeURIComponent(file)));
  logStream.addEventListener('reset', () => { logBuffer = []; $('log-view').innerHTML = ''; });
  logStream.addEventListener('line', (event) => {
    try { appendLog(JSON.parse(event.data).line); } catch (err) { /* 忽略坏帧 */ }
  });
  logStream.onerror = () => text('log-status', '日志流已断开，正在重试…');
}

function wireLogs() {
  $('log-file').addEventListener('change', startLogStream);
  $('log-lines').addEventListener('change', startLogStream);
  $('log-follow').addEventListener('change', startLogStream);
  $('log-wrap').addEventListener('change', () => $('log-view').classList.toggle('wrap', $('log-wrap').checked));
  $('log-search').addEventListener('input', () => {
    logTerms = $('log-search').value.toLowerCase().split(/\s+/).filter(Boolean);
    rerenderLogs();
  });
  $('log-level').addEventListener('change', () => {
    logLevel = $('log-level').value;
    rerenderLogs();
  });
  $('log-clear').addEventListener('click', () => {
    logBuffer = [];
    $('log-view').innerHTML = '';
    toast('已清屏', '只影响当前页面，不会动日志文件', 'ok');
  });
  $('log-download').addEventListener('click', () => {
    window.open(withToken('/api/logs/tail?lines=20000&file=' + encodeURIComponent($('log-file').value)), '_blank');
  });
  $('log-jump').addEventListener('click', () => {
    $('log-view').scrollTop = $('log-view').scrollHeight;
    show($('log-jump'), false);
  });
  $('log-view').addEventListener('scroll', () => {
    const view = $('log-view');
    if (view.scrollTop + view.clientHeight >= view.scrollHeight - 60) show($('log-jump'), false);
  });
}

/* ───────── 账号 ───────── */

async function refreshCredentials() {
  try {
    const data = await api('/api/credentials');
    state.creds = data.credentials || {};
  } catch (err) {
    if (err instanceof AuthError) return showLogin({ gate: 'token' });
    toast('凭据读取失败', err.message, 'err');
    return;
  }
  renderCredentials();
}

function renderCredentials() {
  const c = state.creds || {};
  const remain = c.expires_in_seconds;
  const chip = $('cred-expiry-chip');
  if (remain === null || remain === undefined) {
    chip.className = 'chip'; chip.textContent = '到期未知';
  } else if (remain <= 0) {
    chip.className = 'chip err'; chip.textContent = 'JWT 已过期';
  } else if (remain < 21600) {
    chip.className = 'chip warn'; chip.textContent = '剩余 ' + fmtDuration(remain);
  } else {
    chip.className = 'chip ok'; chip.textContent = '剩余 ' + fmtDuration(remain);
  }

  const rows = [
    ['手机号', c.login_phone],
    ['UID', c.person_uid, true],
    ['device_id', c.device_id, true],
    ['JWT', c.jwt_token, true],
    ['登录密码', c.has_password ? '已保存' : '未保存', false, c.has_password ? 'good' : 'bad'],
    ['RSA 私钥', c.has_private_key ? '已保存' : '未保存', false, c.has_private_key ? 'good' : 'bad'],
    ['到期时间', c.expires_at ? new Date(c.expires_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'],
  ];
  $('cred-list').innerHTML = rows.map(([label, value, copy, tone]) => {
    const v = (value === undefined || value === null || value === '') ? '—' : String(value);
    return '<div class="kv"><dt>' + escapeHtml(label) + '</dt><dd class="' + (tone || '') + '" title="' + escapeHtml(v) + '">' +
      escapeHtml(v) + '</dd>' + (copy && v !== '—' ? '<button class="btn subtle sm" data-copy="' + escapeHtml(v) + '">复制</button>' : '') + '</div>';
  }).join('');
  $('cred-list').querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', () => copyText(btn.dataset.copy));
  });
}

async function doApiLogin(phoneId, passwordId, statusId, onSuccess) {
  const status = $(statusId);
  status.className = 'login-msg';
  status.textContent = '正在登录 Oopz…';
  try {
    await api('/api/login/api', {
      method: 'POST',
      body: { phone: $(phoneId)?.value || '', password: $(passwordId)?.value || '', timeout: 60 },
    });
    status.className = 'login-msg ok';
    status.textContent = '登录成功，凭据已写入 config.py';
    if ($(passwordId)) $(passwordId).value = '';
    toast('Oopz 登录成功', '凭据已保存并触发重连', 'ok');
    await refreshCredentials();
    if (onSuccess) await onSuccess();
  } catch (err) {
    status.className = 'login-msg err';
    status.textContent = err.message;
  }
}

async function doBrowserLogin(phone, password, headless, statusEl, onSuccess) {
  const status = $(statusEl);
  status.className = 'login-msg';
  status.textContent = '正在启动浏览器…';
  try {
    await api('/api/login/browser', { method: 'POST', body: { phone, password, headless } });
    status.textContent = '已启动，请在弹出的浏览器里完成验证（滑块 / 短信）';
    watchBrowserLogin(statusEl, onSuccess);
  } catch (err) {
    status.className = 'login-msg err';
    status.textContent = err.message;
  }
}

function watchBrowserLogin(statusEl, onSuccess) {
  clearTimeout(loginPollTimer);
  api('/api/login/browser').then(async (data) => {
    const task = data.task || {};
    const status = $(statusEl);
    if (task.state === 'running') {
      status.className = 'login-msg';
      status.textContent = task.message || '浏览器登录进行中…';
      loginPollTimer = setTimeout(() => watchBrowserLogin(statusEl, onSuccess), 2000);
    } else if (task.state === 'ok') {
      status.className = 'login-msg ok';
      status.textContent = '网页版登录成功，凭据已保存';
      toast('网页版登录成功', '凭据已写入 config.py', 'ok');
      await refreshCredentials();
      if (onSuccess) await onSuccess();
    } else if (task.state === 'failed') {
      status.className = 'login-msg err';
      status.textContent = task.message || '登录失败';
    } else if (task.state === 'cancelled') {
      status.className = 'login-msg';
      status.textContent = '已取消';
    }
  }).catch(() => { loginPollTimer = setTimeout(() => watchBrowserLogin(statusEl, onSuccess), 4000); });
}

async function cancelBrowserLogin(statusEl) {
  clearTimeout(loginPollTimer);
  try {
    const result = await api('/api/login/browser/cancel', { method: 'POST', body: {} });
    if (statusEl) $(statusEl).textContent = result.message || '已取消';
  } catch (err) {
    toast('取消失败', err.message, 'err');
  }
}

/* ───────── 装配 ───────── */

function clearLegacyCredentials() {
  localStorage.removeItem(TOKEN_KEY);
  const url = new URL(location.href);
  if (url.searchParams.has('token')) {
    url.searchParams.delete('token');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
  }
}

function wireLogin() {
  $('login-form').addEventListener('submit', onLoginSubmit);
  $('login-browser').addEventListener('click', () => {
    show($('login-cancel'), true);
    doBrowserLogin($('login-phone').value, $('login-password').value, false, 'login-msg', async () => {
      const g = await guard();
      return g.gate ? showLogin(g) : enterApp();
    });
  });
  $('login-cancel').addEventListener('click', async () => {
    await cancelBrowserLogin('login-msg');
    show($('login-cancel'), false);
  });
}

function wireAccount() {
  $('api-login').addEventListener('click', () => doApiLogin('api-phone', 'api-password', 'api-login-status'));
  $('browser-login').addEventListener('click', () => {
    doBrowserLogin($('api-phone').value, $('api-password').value, $('browser-headless').checked, 'browser-login-status');
  });
  $('browser-cancel').addEventListener('click', () => cancelBrowserLogin('browser-login-status'));
}

function wireRail() {
  $('btn-restart').addEventListener('click', restartBridge);
  $('verdict-action').addEventListener('click', restartBridge);
  $('btn-logout').addEventListener('click', async () => {
    const ok = await confirmDialog('退出登录', '将注销当前浏览器会话，再次进入需重新输入控制台密码。bot 会继续运行。', '退出');
    if (ok) logout();
  });
  const checkBtn = $('btn-check-update');
  if (checkBtn) checkBtn.addEventListener('click', checkUpdate);
  const githubBtn = $('btn-github');
  if (githubBtn) githubBtn.addEventListener('click', openGithubRepo);
}

const GITHUB_REPO_URL = 'https://github.com/Eason4869/Ooptra';

function openGithubRepo() {
  window.open(GITHUB_REPO_URL, '_blank', 'noopener');
}

async function checkUpdate() {
  await switchPage('maintenance');
  if (!isPage('maintenance')) return;
  if (window.checkMaintenanceUpdate) await window.checkMaintenanceUpdate();
  else toast('更新入口未加载', '请刷新页面后重试；检查页面脚本是否完整。', 'err');
}

async function boot() {
  clearLegacyCredentials();
  wireLogin();
  wireLogs();
  wireConfig();
  wireAccount();
  wireRail();
  const result = await guard();
  if (result.gate) showLogin(result);
  else enterApp();
}

document.addEventListener('DOMContentLoaded', () => boot().catch(err => {
  showLogin({gate: 'offline', message: '控制台页面加载失败：' + err.message + '。请刷新页面重试。'});
}));
