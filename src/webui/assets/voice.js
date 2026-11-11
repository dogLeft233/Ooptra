/* Voice sessions, room targets, persona and auto-visit settings. */
'use strict';

/* ───────── 语音台 ───────── */

let voiceReady = false;
let voicePoll = null;
let voiceAreaNames = {};
let voiceChannelNames = {};
let voiceDefaultArea = '';
let voiceDefaultChannel = '';
let voiceOutputState = null;
let voiceVolumeDraft = null;
let voiceOutputBusy = false;
let voiceOutputRevision = 0;
let voiceOutputOperation = 0;
let voiceTraceRows = new Map();
let voiceTraceNext = null;
let voiceTraceGeneration = 0;
let voiceTraceBusy = false;
let voiceTraceGap = null;
let voiceTraceTarget = '';
let voiceTraceStatus = null;

function resetVoiceTraces() {
  voiceTraceGeneration++;
  voiceTraceRows.clear(); voiceTraceNext = null;
  voiceTraceBusy = false; voiceTraceGap = null;
  const list = $('voice-trace-list');
  if (list) list.replaceChildren();
}

function voiceTraceReason(row) {
  const reasons = {
    keyword: '转写命中呼叫关键词，优先回应', semantic_addressed: '模型判断你正在呼叫或追问 Bot',
    semantic_contribute: '模型判断适合提供帮助或接话', semantic_silent: '模型判断应继续倾听',
    semantic_invalid: '模型判定格式无效，普通聊天保持沉默',
    semantic_unavailable: '模型判定超时或不可用，普通聊天保持沉默',
    policy_silence: '未命中关键词或窗口，概率未选中', reply: '接话策略允许回复',
    too_short: '语音过短，未进入普通接话', noise: '音量过低或噪声，未取得转写',
    no_speech: '没有识别到有效文字', failed: '语音识别失败，未取得最终文字',
    recognition_failed: '语音识别失败', recognizing: '正在接收语音或等待识别',
    awaiting_decision: '识别完成，等待接话判断或调度',
    unselected_or_expired: '冷却或忙碌期间未被选中，输入已被后续发言替换或等待超时',
    unexecuted: '输入已选中，但本轮未执行（可能正在播报或会话已变化）',
    input_gap: '音频不连续或房间重置，识别已中断', input_changed: '音频来源或采样率变化，识别已中断',
    asr_start_failed: '无法启动语音识别', asr_input_failed: '识别音频输入失败',
    limit_reached: '本次会话自动接话尝试已达上限', stale_attempt: '输入许可已失效',
    expired_attempt: '等待过久，输入许可已过期', superseded: '房间或会话已变化，拒绝旧结果',
    interrupted: '本轮被打断', error: '处理失败，请结合耗时和日志排查', control: '识别为语音控制指令',
    classification_invalid: '控制指令解析无效，未执行', classification_unavailable: '控制指令判断不可用，未执行',
  };
  return reasons[row.reason] || row.reason || '尚无最终决定';
}

function renderVoiceTraces() {
  const host = $('voice-trace-list');
  if (!host) return;
  host.replaceChildren();
  const rows = [...voiceTraceRows.values()].sort((a, b) => b.sequence - a.sequence);
  if (!rows.length) {
    const empty = document.createElement('li'); empty.className = 'empty';
    empty.textContent = '暂无逐句记录'; host.append(empty);
  }
  for (const row of rows) {
    const item = document.createElement('li'); item.className = 'voice-trace-item';
    const heading = document.createElement('div'); heading.className = 'voice-trace-heading';
    const who = document.createElement('b'); who.textContent = row.speaker_name || row.user_key || '说话者未知';
    const timestamp = document.createElement('time');
    timestamp.textContent = new Date(row.recorded_at * 1000).toLocaleString('zh-CN', {hour12: false});
    const badge = document.createElement('span'); badge.className = 'chip';
    badge.textContent = row.stage !== 'finished' ? (row.stage === 'recognizing' ? '识别中' : '处理中')
      : row.decision === 'speak' ? (row.outcome === 'completed' ? '已回复' : row.outcome === 'pending' ? '准备回复' : '回复未完成')
      : row.decision === 'control' ? '控制指令' : row.decision === 'silent' && ['semantic_silent', 'policy_silence', 'too_short'].includes(row.reason) ? '保持沉默' : '未回复';
    heading.append(who, timestamp, badge);
    const transcript = document.createElement('p'); transcript.className = 'voice-trace-text';
    transcript.textContent = row.text || (row.recognition_status === 'failed' ? '语音识别失败，未取得最终转写' : row.recognition_status === 'no_speech' ? '识别结束，没有有效语音文字' : row.stage === 'recognizing' ? '等待语音识别结果…' : '未取得最终转写');
    if (row.text && !row.text_final) transcript.textContent += '（临时转写，尚未最终确认）';
    const reason = document.createElement('p'); reason.className = 'hint'; reason.textContent = voiceTraceReason(row);
    if (row.decision === 'speak' && row.outcome && row.outcome !== 'completed') reason.textContent += '；' + ({pending: '等待播放完成', interrupted: '播放已中断', cancelled: '播放已取消', failed: '生成或播放失败', rejected: '播放请求被拒绝'}[row.outcome] || ('执行结果：' + row.outcome));
    if (row.stage !== 'finished' && Date.now() / 1000 - row.updated_at > 90) reason.textContent += '；长时间未更新，可能已因进程中断而停止';
    const details = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = '查看关联信息';
    const meta = document.createElement('p'); meta.className = 'hint';
    meta.textContent = '成员：' + (row.user_key || '未知') + ' · 输入：' + row.input_ref + ' · 会话：' + row.session_id + ' · 频道：' + row.area + '/' + row.channel + (row.elapsed_ms ? ' · 处理 ' + Math.round(row.elapsed_ms) + ' ms' : '');
    details.append(summary, meta); item.append(heading, transcript, reason, details); host.append(item);
  }
  const more = $('voice-trace-more');
  if (more) { more.hidden = (voiceTraceGap?.before ?? voiceTraceNext) == null; more.disabled = voiceTraceBusy; }
}

async function refreshVoiceTraces(st, {older = false} = {}) {
  if (!$('voice-trace-list')) return;
  if (st) voiceTraceStatus = st;
  const scope = $('voice-trace-scope')?.value || 'current';
  const state = voiceTraceStatus || {};
  const target = scope === 'all' ? 'all' : (state.area && state.channel ? state.area + '/' + state.channel : 'none');
  if (target !== voiceTraceTarget) { resetVoiceTraces(); voiceTraceTarget = target; }
  if (target === 'none') { text('voice-trace-status', '当前没有频道，请选择全部频道查看历史'); renderVoiceTraces(); return; }
  if (voiceTraceBusy || (older && (voiceTraceGap?.before ?? voiceTraceNext) == null)) return;
  const generation = voiceTraceGeneration;
  voiceTraceBusy = true;
  const refresh = $('voice-trace-refresh'), more = $('voice-trace-more');
  if (refresh) refresh.disabled = true;
  if (more) more.disabled = true;
  const query = new URLSearchParams({limit: '50'});
  if (target !== 'all') { query.set('area', state.area); query.set('channel', state.channel); }
  const fillingGap = older ? voiceTraceGap : null;
  if (older) query.set('before', String(fillingGap?.before ?? voiceTraceNext));
  let newestKnown = 0;
  for (const sequence of voiceTraceRows.keys()) newestKnown = Math.max(newestKnown, sequence);
  try {
    const data = await api('/api/voice/transcripts?' + query);
    if (generation !== voiceTraceGeneration) return;
    const rows = data.rows || [];
    for (const row of rows) voiceTraceRows.set(row.sequence, row);
    if (fillingGap) {
      if (rows.some(row => row.sequence <= fillingGap.until) || data.next_before == null) voiceTraceGap = null;
      else fillingGap.before = data.next_before;
    } else if (older || !newestKnown) voiceTraceNext = data.next_before ?? null;
    else if (rows.length && rows.every(row => row.sequence > newestKnown) && data.next_before != null) {
      // Keep the earlier gap's lower boundary when multiple suspended refreshes
      // create disjoint loaded pages; the ordinary older-history cursor is separate.
      voiceTraceGap = {before: data.next_before, until: voiceTraceGap?.until ?? newestKnown};
    }
    text('voice-trace-status', '已载入 ' + voiceTraceRows.size + ' / ' + (data.total || 0) + ' 句 · 自动更新');
    renderVoiceTraces();
  } catch (error) {
    if (generation === voiceTraceGeneration) text('voice-trace-status', '读取失败：' + error.message + '；可点击刷新重试，已有记录保留');
  } finally {
    if (generation === voiceTraceGeneration) {
      voiceTraceBusy = false;
      if (refresh) refresh.disabled = false;
      if (more) more.disabled = false;
    }
  }
}

function resetVoiceOutput() {
  resetVoiceTraces(); voiceTraceTarget = ''; voiceTraceStatus = null;
  voiceOutputOperation++;
  voiceOutputRevision++;
  voiceOutputState = null;
  voiceVolumeDraft = null;
  voiceOutputBusy = false;
  renderVoiceOutput();
}

function renderVoiceOutput(st) {
  if (st) voiceOutputState = st;
  const state = voiceOutputState;
  const mic = $('voice-mic-toggle');
  const slider = $('voice-output-volume');
  const save = $('voice-volume-save');
  if (!mic || !slider || !save) return;
  const available = state && Number.isInteger(state.output_volume);
  const muted = !!state?.output_muted;
  text('voice-output-state', !available ? '状态暂不可用' : !state.joined ? '未进房' : muted ? '麦克风关闭 · 仍在接听' : '麦克风打开');
  mic.textContent = muted ? '打开麦克风' : '关闭麦克风';
  mic.setAttribute('aria-pressed', String(muted));
  mic.disabled = voiceOutputBusy || !available || !state.joined;
  slider.disabled = voiceOutputBusy || !available;
  slider.value = voiceVolumeDraft ?? state?.output_volume ?? 50;
  text('voice-volume-value', slider.value + '%');
  save.disabled = voiceOutputBusy || !available || voiceVolumeDraft === null || (voiceVolumeDraft === state.output_volume && !muted);
  save.textContent = voiceOutputBusy ? '应用中…' : '保存音量';
}

async function changeVoiceOutput(body) {
  if (voiceOutputBusy) return;
  const operation = ++voiceOutputOperation;
  voiceOutputRevision++;
  voiceOutputBusy = true;
  renderVoiceOutput();
  try {
    const result = await api('/api/voice/output', {method: 'POST', body});
    if (operation !== voiceOutputOperation) return;
    voiceOutputRevision++;
    if (body.volume !== undefined) {
      voiceVolumeDraft = null;
      // A second editor may contain unsaved fields; refresh its volume only when clean.
      if (configSchema?.voice?.fields?.output_volume) {
        configSchema.voice.fields.output_volume.value = result.volume;
        if (!draftCount()) renderConfig();
      }
    }
    renderVoiceOutput({...voiceOutputState, output_volume: result.volume, output_muted: result.muted});
    toast('已应用', result.message, 'ok');
  } catch (err) {
    if (operation !== voiceOutputOperation) return;
    voiceOutputRevision++;
    toast('语音输出设置失败', err.message, 'err');
    // Re-read the actual state, including a persisted volume whose live application failed.
    try {
      const data = await api('/api/voice/status');
      if (operation === voiceOutputOperation) renderVoiceOutput(data.status || {});
    } catch (_) {
      if (operation === voiceOutputOperation) {voiceOutputState = null; renderVoiceOutput();}
    }
  } finally {
    if (operation === voiceOutputOperation) {
      voiceOutputRevision++;
      voiceOutputBusy = false;
      renderVoiceOutput();
    }
  }
}

function currentJoinArea() {
  const el = $('join-area');
  return (el && el.value) || '';
}

function currentJoinChannel() {
  const el = $('join-channel');
  return (el && el.value) || '';
}

function updateDefaultHint() {
  const hint = $('default-target-hint');
  if (!hint) return;
  const areaName = voiceDefaultArea ? (voiceAreaNames[voiceDefaultArea] || voiceDefaultArea) : '';
  const channelName = voiceDefaultChannel
    ? (voiceChannelNames[voiceDefaultChannel] || voiceDefaultChannel)
    : '';
  if (voiceDefaultArea && voiceDefaultChannel) {
    hint.textContent = '默认目标：' + areaName + ' / ' + channelName + '。插件不带参数进房时用它；一个 QQ 群绑了多个域时也按默认域查询。';
  } else if (voiceDefaultArea) {
    hint.textContent = '默认域：' + areaName + '（未设默认频道）。一个 QQ 群绑了多个域时按默认域查询；插件进房仍需默认频道。';
  } else {
    hint.textContent = '默认目标：未设置。选中域与频道后点「设为默认」，作为插件 /进语音 的默认目标。';
  }
  document.querySelectorAll('#area-tabs .chip-tab').forEach((el) => {
    const id = el.dataset.area || '';
    el.classList.toggle('is-default', !!id && id === voiceDefaultArea);
  });
  document.querySelectorAll('#channel-grid .channel-card').forEach((el) => {
    const id = el.dataset.channel || '';
    el.classList.toggle('is-default', !!id && id === voiceDefaultChannel);
  });
  const btn = $('voice-set-default');
  if (btn) {
    const area = currentJoinArea();
    const channel = currentJoinChannel();
    btn.disabled = !area;
    btn.title = area
      ? ('把 ' + (voiceAreaNames[area] || area) + (channel ? ' / ' + (voiceChannelNames[channel] || channel) : '') + ' 写为默认')
      : '先点选一个域标签';
  }
}

async function setDefaultTarget() {
  const area = currentJoinArea();
  if (!area) {
    toast('请先选域', '点上方域标签选中目标域', 'warn');
    return;
  }
  const channel = currentJoinChannel();
  const btn = $('voice-set-default');
  if (btn) btn.disabled = true;
  try {
    await api('/api/config', {
      method: 'POST',
      body: { updates: { oopz: { default_area: area, default_channel: channel } } },
    });
    voiceDefaultArea = area;
    voiceDefaultChannel = channel;
    updateDefaultHint();
    toast('已设为默认', (voiceAreaNames[area] || area) + (channel ? ' / ' + (voiceChannelNames[channel] || channel) : ''), 'ok');
  } catch (err) {
    toast('设置失败', err.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
    updateDefaultHint();
  }
}

function updateBindBar() {
  const areaId = currentJoinArea();
  const channelId = currentJoinChannel();
  const areaShown = areaId || '<域ID>';
  const channelShown = channelId || '<频道ID>';
  const cmd = $('bind-cmd');
  if (cmd) {
    cmd.textContent = '/语音绑定 ' + areaShown + (channelId ? ' ' + channelShown : '');
    cmd.title = areaId
      ? ('域 ID：' + areaId + (channelId ? '\n频道 ID：' + channelId : '\n尚未选择频道'))
      : '尚未选择域，将用配置默认域';
  }
  const areaBtn = $('copy-area-id');
  if (areaBtn) {
    areaBtn.disabled = !areaId;
    areaBtn.title = areaId ? ('复制域 ID：' + areaId) : '先点选一个域标签';
  }
  const channelBtn = $('copy-channel-id');
  if (channelBtn) {
    channelBtn.disabled = !channelId;
    channelBtn.title = channelId ? ('复制频道 ID：' + channelId) : '先点选一个频道卡片';
  }
  const bindBtn = $('copy-bind-cmd');
  if (bindBtn) {
    bindBtn.disabled = !areaId;
    bindBtn.title = areaId ? '复制完整 /语音绑定 指令' : '先点选一个域标签';
  }
  updateDefaultHint();
}

function wireBindBar() {
  const areaBtn = $('copy-area-id');
  const channelBtn = $('copy-channel-id');
  const bindBtn = $('copy-bind-cmd');
  const cmd = $('bind-cmd');
  if (areaBtn) areaBtn.addEventListener('click', () => {
    const id = currentJoinArea();
    if (id) copyText(id);
  });
  if (channelBtn) channelBtn.addEventListener('click', () => {
    const id = currentJoinChannel();
    if (id) copyText(id);
  });
  if (bindBtn) bindBtn.addEventListener('click', () => {
    const areaId = currentJoinArea();
    if (!areaId) return;
    const channelId = currentJoinChannel();
    copyText('/语音绑定 ' + areaId + (channelId ? ' ' + channelId : ''));
  });
  if (cmd) cmd.addEventListener('click', () => {
    const areaId = currentJoinArea();
    if (!areaId) return;
    const channelId = currentJoinChannel();
    copyText('/语音绑定 ' + areaId + (channelId ? ' ' + channelId : ''));
  });
  updateBindBar();
}

function switchVoiceTab(tab) {
  const name = tab || 'session';
  document.querySelectorAll('#voice-tabs .subtab').forEach((el) => {
    el.classList.toggle('is-active', el.dataset.tab === name);
    el.setAttribute('aria-pressed', String(el.dataset.tab === name));
  });
  document.querySelectorAll('.vpane').forEach((el) => {
    el.classList.toggle('is-active', el.id === 'vpane-' + name);
  });
  if (name === 'members') refreshMembers();
  if (name === 'bindings') loadBindings();
  if (name === 'auto-visit') refreshAutoVisit(true);
  if (name === 'persona') {
    loadPersona();
    loadMemory();
  }
  if (name === 'tools' && window.loadPreviewPrompts) window.loadPreviewPrompts();
}

function setupVoice() {
  if (voiceReady) return;
  voiceReady = true;
  $('voice-mic-toggle').onclick = () => changeVoiceOutput({muted: !voiceOutputState?.output_muted});
  $('voice-output-volume').addEventListener('input', event => {
    voiceVolumeDraft = Number(event.target.value);
    renderVoiceOutput();
  });
  $('voice-volume-save').onclick = () => changeVoiceOutput({volume: voiceVolumeDraft});
  const tabs = $('voice-tabs');
  if (tabs) {
    tabs.addEventListener('click', (event) => {
      const btn = event.target.closest('.subtab');
      if (btn) switchVoiceTab(btn.dataset.tab);
    });
  }
  const join = $('voice-join');
  const leave = $('voice-leave');
  const speak = $('speak-btn');
  const memRefresh = $('memory-refresh');
  const memClear = $('memory-clear');
  const personaSave = $('persona-save');
  const memberRefresh = $('member-refresh');
  const targetsRefresh = $('voice-targets-refresh');
  if (join) join.onclick = () => voiceAction('join');
  if (leave) leave.onclick = () => voiceAction('leave');
  if ($('voice-stop')) $('voice-stop').onclick = () => voiceAction('stop');
  if (targetsRefresh) targetsRefresh.onclick = () => loadVoiceTargets(true);
  const setDefault = $('voice-set-default');
  if (setDefault) setDefault.onclick = setDefaultTarget;
  wireBindBar();
  setupAutoVisit();
  const areaTabs = $('area-tabs');
  if (areaTabs) {
    areaTabs.addEventListener('click', (event) => {
      const btn = event.target.closest('.chip-tab');
      if (!btn) return;
      const area = btn.dataset.area || '';
      localStorage.setItem('oopz.webui.voice.area', area);
      setJoinArea(area);
      loadChannels(area, localStorage.getItem('oopz.webui.voice.channel') || '');
      syncMemberArea(area);
    });
  }
  const channelGrid = $('channel-grid');
  if (channelGrid) {
    channelGrid.addEventListener('click', (event) => {
      const btn = event.target.closest('.channel-card');
      if (!btn) return;
      const channel = btn.dataset.channel || '';
      localStorage.setItem('oopz.webui.voice.channel', channel);
      setJoinChannel(channel);
    });
  }
  if (speak) speak.onclick = voiceSpeak;
  if (memRefresh) memRefresh.onclick = loadMemory;
  if (memClear) memClear.onclick = clearMemory;
  if (personaSave) personaSave.onclick = savePersona;
  $('astrbot-context-refresh').onclick = async (event) => {
    const button = event.currentTarget; button.disabled = true;
    try { await api('/api/voice/astrbot/refresh', {method: 'POST', body: {}}); await loadPersona(); }
    catch (error) { text('astrbot-context-status', error.message); }
    finally { button.disabled = false; }
  };
  if (memberRefresh) memberRefresh.onclick = refreshMembers;
  const traceRefresh = $('voice-trace-refresh'), traceMore = $('voice-trace-more'), traceScope = $('voice-trace-scope');
  if (traceRefresh) traceRefresh.onclick = () => refreshVoiceTraces();
  if (traceMore) traceMore.onclick = () => refreshVoiceTraces(null, {older: true});
  if (traceScope) traceScope.onchange = () => { resetVoiceTraces(); voiceTraceTarget = ''; refreshVoiceTraces(); };
}

async function voiceAction(kind) {
  const controls = ['voice-join', 'voice-leave', 'voice-stop'].map($).filter(Boolean);
  if (controls.some(button => button.disabled)) return;
  controls.forEach(button => { button.disabled = true; button.setAttribute('aria-busy', 'true'); });
  try {
    const body = {};
    if (kind === 'join') {
      const areaSel = $('join-area');
      const channelSel = $('join-channel');
      const area = areaSel ? areaSel.value.trim() : '';
      const channel = channelSel ? channelSel.value.trim() : '';
      if (area) body.area = area;
      if (channel) body.channel = channel;
    }
    const data = await api('/api/voice/' + kind, {
      method: 'POST',
      body,
    });
    toast(kind === 'join' ? '已进房' : kind === 'stop' ? '已停止回复' : '已退房', (data.area || '') + ' ' + (data.channel || ''), 'ok');
    await refreshVoiceStatus();
  } catch (err) {
    toast('语音操作失败', err.message, 'err');
  } finally { controls.forEach(button => { button.disabled = false; button.removeAttribute('aria-busy'); }); }
}

async function voiceSpeak() {
  const input = $('speak-text');
  const status = $('speak-status');
  const value = input ? input.value.trim() : '';
  if (!value) {
    if (status) status.textContent = '请输入要发送的内容';
    return;
  }
  if ($('speak-btn').disabled) return;
  $('speak-btn').disabled = true;
  $('speak-btn').setAttribute('aria-busy', 'true');
  try {
    if (status) status.textContent = '发送中…';
    const data = await api('/api/voice/speak', { method: 'POST', body: { text: value } });
    if (status) {
      // Live 模式是「交给模型，由它开口」，没有本地合成的字节数 —— 照级联那套
      // 写「已推送（0 bytes）」会让人以为失败了。
      status.textContent = data.mode === 'live'
        ? '已发送，等 AI 开口…'
        : '已朗读（' + (data.pcm_bytes || 0) + ' bytes）';
    }
    toast(data.mode === 'live' ? '已发送' : '已朗读', value.slice(0, 24), 'ok');
  } catch (err) {
    if (status) status.textContent = err.message;
    toast('发送失败', err.message, 'err');
  } finally { $('speak-btn').disabled = false; $('speak-btn').removeAttribute('aria-busy'); }
}

async function refreshVoiceStatus() {
  setupVoice();
  const outputRevision = voiceOutputRevision;
  try {
    const data = await api('/api/voice/status');
    const st = data.status || {};
    if (outputRevision === voiceOutputRevision && !voiceOutputBusy) renderVoiceOutput(st);
    const joined = !!st.joined;
    const live = String(st.backend || '').includes('gemini');
    text('speak-title', live ? 'AI 语音回答' : '直接朗读');
    text('speak-mode', live ? '交给 AI 回答' : '文字转语音');
    text('speak-btn', live ? '让 AI 回答' : '朗读');
    text('speak-hint', live ? '输入一句话，AI 在当前语音房生成语音回答；需要已经进房。' : '把输入文字直接读给当前语音房，不经过对话模型；需要已经进房。');
    const empty = (data.auto_visit || {}).empty_room || {};
    text('v-empty', !joined ? '未在房间' : empty.state === 'alone' ? Math.ceil(empty.remaining_seconds ?? 30) + ' 秒后退出' : empty.state === 'occupied' ? '房间有人' : '等待确认');
    text('v-empty-sub', empty.error || (empty.state === 'alone' ? '独处 30 秒后复查并静默退房' : empty.last_exit_reason === 'alone_30_seconds' && !joined ? '上次因独处 30 秒退出' : '手动与自动进房均适用'));
    text('v-backend', st.backend || '—');
    text('v-backend-sub', (st.enabled ? '已启用' : '未启用') + ' · 对话后端');
    if (st.model_connection) {
      const conn = st.model_connection;
      text('v-backend-sub', conn.state === 'ready' ? '模型已连接' : conn.state === 'reconnecting' ? '正在重连（' + conn.attempts + '/3）' : conn.state === 'failed' ? conn.error + '；可退房后重新加入重试' : '模型连接：' + conn.state);
    }
    const source = st.join_source || st.source || st.session_source || '';
    const automatic = source === 'auto' || source === 'auto_visit';
    text('v-joined', joined ? (automatic ? '自动停留' : (source ? '手动会话' : '在房')) : '不在房');
    text('visit-current-target', joined
      ? '当前' + (automatic ? '自动停留' : (source ? '手动会话' : '语音会话')) + '：' + (voiceAreaNames[st.area] || st.area || '当前域') + ' / ' + (voiceChannelNames[st.channel] || st.channel || '当前房间')
      : '当前语音会话：未在房间');
    text('v-target', joined ? ((st.area || '') + ' / ' + (st.channel || '')) : '未绑定频道');
    text('v-turns', String(st.turns || 0));
    text('v-speaking', st.speaking ? '正在说话' : '空闲');
    const title = $('voice-verdict-title');
    const detail = $('voice-verdict-detail');
    if (title) title.textContent = joined ? '语音会话进行中' : (st.enabled ? '语音 Agent 就绪' : '语音 Agent 未启用');
    if (detail) {
      detail.textContent = st.last_reply
        ? '最近回复：' + String(st.last_reply).slice(0, 80)
        : (joined ? '可对语音房说话，或使用「快捷开口」' : '点「进房」开始语音对话');
    }
    renderTranscript(st);
    void refreshVoiceTraces(st);
    renderVoiceDecisions(st.voice_observation || data.voice_observation);
  } catch (err) {
    if (outputRevision === voiceOutputRevision && !voiceOutputBusy) {
      voiceOutputState = null;
      renderVoiceOutput();
    }
    const detail = $('voice-verdict-detail');
    if (detail) detail.textContent = err.message;
  }
}

function renderVoiceDecisions(observation) {
  const host = $('voice-decision-list');
  if (!host) return;
  const reasons = {probability: '概率跳过', keyword: '关键词触发', window: '连续对话窗口', short: '短句跳过', empty: '无识别内容', error: '处理失败', reply: '普通回复', control: '会话控制', explicit: '手动开口', interrupted: '回复已停止'};
  const outcomes = {replied: '已回复', reply: '已回复', skipped: '已跳过', skip: '已跳过', error: '失败', leave: '退房', control: '控制'};
  const stages = {asr: '识别', intent: '意图', chat: '聊天', tts: '合成'};
  const rows = (observation?.recent_decisions || []).slice(-10).reverse();
  host.replaceChildren();
  if (!rows.length) { host.innerHTML = '<li class="empty">暂无决策记录</li>'; return; }
  for (const row of rows) {
    const item = document.createElement('li');
    item.className = 'check-row';
    const info = document.createElement('div');
    const title = document.createElement('b');
    title.textContent = (reasons[row.reason] || row.reason || '决策') + ' · ' + (outcomes[row.outcome] || row.outcome || '—');
    const detail = document.createElement('p');
    const timing = Object.entries(stages).map(([key, label]) => label + ' ' + (Number.isFinite(row.timings_ms?.[key]) ? Math.round(row.timings_ms[key]) + ' ms' : '—')).join(' / ');
    const mode = row.timing_mode === 'live_overlap' ? '（实时阶段重叠）' : row.timing_mode === 'combined_intent_chat' ? '（意图与聊天共用一次请求）' : '';
    const timestamp = typeof row.timestamp === 'string' ? Date.parse(row.timestamp) / 1000 : row.timestamp;
    detail.textContent = fmtClock(Number.isFinite(timestamp) ? timestamp : 0) + ' · ' + timing + mode + (Number.isFinite(row.elapsed_ms) ? ' · 总计 ' + Math.round(row.elapsed_ms) + ' ms' : '');
    info.append(title, detail); item.append(info); host.append(item);
  }
}

function renderTranscript(st) {
  const host = $('voice-transcript');
  if (!host) return;
  const rows = [];
  if (st.last_user_text) rows.push(['user', st.last_user_text]);
  if (st.last_reply) rows.push(['assistant', st.last_reply]);
  if (!rows.length) {
    host.innerHTML = '<li class="muted">暂无对话</li>';
    return;
  }
  host.innerHTML = rows.map(([role, content]) =>
    '<li class="feed-item"><span class="chip">' + (role === 'user' ? '用户' : 'Bot') +
    '</span><span>' + escapeHtml(content) + '</span></li>'
  ).join('');
}

async function refreshMembers() {
  const status = $('member-status');
  const areaInput = $('member-area');
  let area = areaInput ? areaInput.value.trim() : '';
  let channel = '';
  try {
    if (status) status.textContent = '加载中…';
    // 静音状态只有**同一个 Agora 房间里**的人会广播。接口不传 channel 时会把整个
    // 域下所有语音房的成员汇总过来，那些房的广播我们收不到，整张表就全是「未知」。
    // 所以先问 /voice/status 拿 bot 此刻在哪个房，只查那一个。
    const st = ((await api('/api/voice/status')).status) || {};
    if (!area) area = st.area || '';
    if (st.joined && st.channel) channel = st.channel;
    let qs = '?area=' + encodeURIComponent(area);
    if (channel) qs += '&channel=' + encodeURIComponent(channel);
    const data = await api('/api/voice/members' + qs);
    const body = $('member-body');
    const rows = data.members || [];
    text('member-count', String(data.count || rows.length));
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="4" class="muted">暂无成员（确认已进房 / 域 ID 正确）</td></tr>';
    } else {
      // 静音状态只有两个来源：成员自己用 Agora stream message 广播的实时状态
      // （row.live=true），或 REST 自带字段（当前 Oopz 不返回）。都没有就不能伪造成
      // 「开麦」——但也不能写「未知」，那看着像我们没收到；是对方还没广播过。
      const muteCell = (flag, onText, offText) => {
        if (flag === null || flag === undefined) {
          return '<span class="badge" title="该成员进房后还没广播过静音状态">未广播</span>';
        }
        return '<span class="badge ' + (flag ? 'warn' : 'ok') + '">' +
          (flag ? onText : offText) + '</span>';
      };
      body.innerHTML = rows.map((row) =>
        '<tr><td class="mono">' + escapeHtml(row.uid || '—') + '</td>' +
        '<td>' + escapeHtml(row.name || '—') + '</td>' +
        '<td>' + muteCell(row.mic_muted, '已闭麦', '开麦') + '</td>' +
        '<td>' + muteCell(row.speaker_muted, '已闭听', '正常') + '</td></tr>'
      ).join('');
    }
    // 广播是「谁进房/改状态谁发一条」，一次只报一个人，不是整房快照。所以能报出
    // 「本房 N 人里拿到 M 人」，而不是含糊的「收到 X 条」。
    const total = rows.length;
    const got = Number(data.live_members || 0);
    if (status) {
      if (!channel) {
        status.textContent = '已更新（bot 未在语音房，拿不到实时状态）';
      } else if (got > 0) {
        status.textContent = '已更新（本房实时状态 ' + got + '/' + total +
          ' 人；对端只在进房和改状态时广播）';
      } else {
        status.textContent = '已更新（本房还没收到任何静音状态广播，这两列显示「未广播」）';
      }
    }
  } catch (err) {
    if (status) status.textContent = err.message;
  }
}

async function loadPersona() {
  try {
    const voice = await api('/api/voice/status');
    const shared = voice.astrbot_context || {};
    text('astrbot-context-status', shared.active
      ? `人格来源 ${shared.persona_umo || shared.group_id} · 人格 ${shared.persona_id || '未设置'} · 记忆 ${shared.provider || '未接入'}（${(shared.memory_umos || []).length} 个查询／写回会话）· 待写 ${shared.pending_writes || 0} 条。${shared.error || shared.memory_error || ''}`
      : `使用本地设置。${shared.error || ''}`);
    const data = await api('/api/persona');
    const ta = $('persona-text');
    const inherited = data.astrbot_context?.active === true;
    if (ta) {ta.value = inherited ? data.effective_persona || '' : data.persona || ''; ta.readOnly = inherited;}
    const save = $('persona-save');
    if (save) save.disabled = inherited;
    const status = $('persona-status');
    if (status) status.textContent = '';
  } catch (err) {
    const status = $('persona-status');
    if (status) status.textContent = err.message;
  }
}

async function savePersona() {
  const ta = $('persona-text');
  const status = $('persona-status');
  try {
    const data = await api('/api/persona', {
      method: 'PUT',
      body: { persona: ta ? ta.value : '' },
    });
    if (status) status.textContent = '已保存';
    toast('人格已保存', '长度 ' + String((data.persona || '').length), 'ok');
  } catch (err) {
    if (status) status.textContent = err.message;
    toast('保存失败', err.message, 'err');
  }
}

async function loadMemory() {
  try {
    const data = await api('/api/memory');
    const host = $('memory-list');
    const rows = data.messages || [];
    if (!rows.length) {
      host.innerHTML = '<li class="muted">暂无记忆</li>';
      return;
    }
    host.innerHTML = rows.slice(-20).map((row) =>
      '<li class="feed-item"><span class="chip">' + escapeHtml(row.role || 'user') +
      '</span><span>' + escapeHtml(row.content || '') + '</span></li>'
    ).join('');
  } catch (err) {
    toast('记忆读取失败', err.message, 'err');
  }
}

async function clearMemory() {
  const yes = await confirmDialog('清空共享记忆', '将删除所有语音对话记忆，且不可恢复。', '清空');
  if (!yes) return;
  try {
    await api('/api/memory', { method: 'DELETE', body: {} });
    toast('已清空', '', 'ok');
    loadMemory();
  } catch (err) {
    toast('清空失败', err.message, 'err');
  }
}

/* ───────── 域 / 频道选择（标签 + 卡片） ───────── */

function setJoinArea(area) {
  const hidden = $('join-area');
  if (hidden) hidden.value = area || '';
  document.querySelectorAll('#area-tabs .chip-tab').forEach((el) => {
    el.classList.toggle('is-active', (el.dataset.area || '') === (area || ''));
  });
  const label = $('channel-area-label');
  if (label) {
    const tab = document.querySelector('#area-tabs .chip-tab.is-active');
    const name = (tab && tab.textContent.trim()) || '默认域';
    label.textContent = '频道 · ' + name;
    if (area) label.title = '域 ID：' + area;
  }
  updateBindBar();
}

function setJoinChannel(channel) {
  const hidden = $('join-channel');
  if (hidden) hidden.value = channel || '';
  document.querySelectorAll('#channel-grid .channel-card').forEach((el) => {
    el.classList.toggle('is-active', (el.dataset.channel || '') === (channel || ''));
  });
  updateBindBar();
}

function syncMemberArea(area) {
  const memberArea = $('member-area');
  if (!memberArea) return;
  // 成员页若仍是 select，保持同步；若是隐藏/显示，只写 dataset
  if (memberArea.tagName === 'SELECT') {
    if (area && Array.from(memberArea.options).some((o) => o.value === area)) {
      memberArea.value = area;
    }
  } else {
    memberArea.dataset.area = area || '';
  }
}

async function loadVoiceTargets(force) {
  const areaTabs = $('area-tabs');
  const hint = $('join-hint');
  if (!areaTabs) return;
  if (!force && areaTabs.childElementCount > 1) {
    const current = localStorage.getItem('oopz.webui.voice.area') || '';
    setJoinArea(current);
    await loadChannels(current, localStorage.getItem('oopz.webui.voice.channel') || '');
    return;
  }
  try {
    if (hint) hint.textContent = '加载域列表…';
    const data = await api('/api/oopz/areas');
    const areas = data.areas || [];
    voiceAreaNames = {};
    areas.forEach((row) => { if (row.id) voiceAreaNames[row.id] = row.name || row.id; });
    voiceDefaultArea = data.default_area || '';
    voiceDefaultChannel = data.default_channel || '';
    const savedArea = localStorage.getItem('oopz.webui.voice.area') || data.default_area || '';
    areaTabs.innerHTML = '';
    // 默认域占位
    const def = document.createElement('button');
    def.type = 'button';
    def.className = 'chip-tab';
    def.dataset.area = '';
    def.textContent = '默认域';
    def.title = '使用配置里的默认域';
    areaTabs.appendChild(def);
    areas.forEach((row) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip-tab';
      btn.dataset.area = row.id || '';
      btn.textContent = row.name || row.id || '未命名';
      btn.title = '域 ID：' + (row.id || '') + '\n点击切换，ID 见下方绑定栏';
      areaTabs.appendChild(btn);
    });
    // 成员页域下拉同步
    const memberArea = $('member-area');
    if (memberArea && memberArea.tagName === 'SELECT') {
      memberArea.innerHTML = '<option value="">（当前会话）</option>' + areas.map((row) =>
        '<option value="' + escapeHtml(row.id) + '">' + escapeHtml(row.name || row.id) + '</option>'
      ).join('');
      if (savedArea) memberArea.value = savedArea;
    }
    setJoinArea(savedArea);
    const savedChannel = localStorage.getItem('oopz.webui.voice.channel') || data.default_channel || '';
    await loadChannels(savedArea, savedChannel);
    if (hint) hint.textContent = '点域标签切换、点频道卡片选中；下方可复制域/频道 ID 用于 /语音绑定';
  } catch (err) {
    if (hint) hint.textContent = '域列表加载失败：' + err.message;
    areaTabs.innerHTML = '<button type="button" class="chip-tab is-active" data-area="">默认域</button>';
    updateBindBar();
  }
}

async function loadChannels(area, preferred) {
  const grid = $('channel-grid');
  const hint = $('join-hint');
  if (!grid) return;
  voiceChannelNames = {};
  // 先放默认频道卡
  grid.innerHTML = '';
  const def = document.createElement('button');
  def.type = 'button';
  def.className = 'channel-card';
  def.dataset.channel = '';
  def.innerHTML = '<span class="ch-name">默认频道</span><span class="ch-sub">config 默认</span>';
  grid.appendChild(def);

  if (!area) {
    setJoinChannel(preferred || '');
    if (hint) hint.textContent = '未选域时用配置默认频道；点「默认域」以外的标签可列频道';
    return;
  }
  try {
    if (hint) hint.textContent = '加载频道…';
    const data = await api('/api/oopz/channels?area=' + encodeURIComponent(area));
    const channels = data.channels || [];
    channels.forEach((row) => {
      if (row.id) voiceChannelNames[row.id] = row.name || row.id;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'channel-card';
      btn.dataset.channel = row.id || '';
      btn.title = '频道 ID：' + (row.id || '') + '\n点击选中，ID 见下方绑定栏';
      btn.innerHTML =
        '<span class="ch-name">' + escapeHtml(row.name || row.id) + '</span>' +
        '<span class="ch-sub"><span class="ch-id">' + escapeHtml(row.id || '—') + '</span></span>';
      grid.appendChild(btn);
    });
    const pick = preferred || '';
    setJoinChannel(pick);
    if (hint) hint.textContent = channels.length
      ? ('该域共 ' + channels.length + ' 个语音频道，点卡片选中并复制 ID')
      : '该域下未发现语音频道，将使用默认';
  } catch (err) {
    if (hint) hint.textContent = '频道加载失败：' + err.message;
    setJoinChannel('');
  }
}

// 进入语音台时预加载目标
const _origRefreshVoiceStatus = typeof refreshVoiceStatus === 'function' ? refreshVoiceStatus : null;
refreshVoiceStatus = async function () {
  if (_origRefreshVoiceStatus) await _origRefreshVoiceStatus();
  await loadVoiceTargets(false);
};

/* ───────── 自动串门：读取状态与字段级配置合并 ───────── */
const VISIT_FIELDS = [
  { key: 'join_probability', label: '进房概率 / %', kind: 'probability', help: '每轮只抽签一次；未命中就等下一轮' },
  { key: 'stay_minutes', label: '自动停留 / 分钟', kind: 'range', help: '已开始的停留不会因修改范围而重置' },
  { key: 'auto_cooldown_minutes', label: '自动退房后的全局冷却 / 分钟', kind: 'range', help: '这段时间内所有域暂停自动进房' },
  { key: 'manual_cooldown_minutes', label: '手动退房后的域内冷却 / 分钟', kind: 'range', help: '仅休息刚退出的域，其他域仍可串门' },
  { key: 'enter_prompts', label: '进房表达意图', kind: 'prompts', help: '每行一条，随机选取并由当前 AI 改写；留空表示静默' },
  { key: 'leave_prompts', label: '告别表达意图', kind: 'prompts', help: '每行一条，随机选取并由当前 AI 改写；留空表示静默' },
];
const VISIT_PHASES = { waiting: '等待下一轮检查', checking: '正在寻找有人的房间', joining: '正在进入房间', greeting: '正在打招呼', active: '自动停留中', waiting_reply: '等待当前回复结束', farewell: '正在告别', leaving: '正在退出房间', cooldown: '全局冷却中', paused: '自动串门已暂停', stopped: '自动串门未运行', idle: '等待就绪' };
let visitConfig = null;
let visitStatus = null;
let visitAreas = [];
let visitSelectedArea = '';
let visitFetchPending = false;
let visitGlobalDirty = new Set();
let visitAreaDirty = new Set();

function visitFieldMarkup(scope, field) {
  const id = 'visit-' + scope + '-' + field.key;
  const inherit = scope === 'area' ? '<label class="visit-override"><input type="checkbox" data-override="' + field.key + '" />单独设置 <span class="visit-inherit-state">继承全局默认</span></label>' : '';
  let control;
  if (field.kind === 'range') {
    control = '<div class="range-input"><input class="control" id="' + id + '-min" aria-label="' + field.label + '最小值" type="number" min="1" max="10080" step="any" required /><span>至</span><input class="control" id="' + id + '-max" aria-label="' + field.label + '最大值" type="number" min="1" max="10080" step="any" required /></div>';
  } else if (field.kind === 'probability') {
    control = '<input class="control" id="' + id + '" aria-label="' + field.label + '" type="number" min="0" max="100" step="any" required />';
  } else {
    control = '<textarea class="control" id="' + id + '" aria-label="' + field.label + '" rows="3" placeholder="留空表示不说话"></textarea>';
  }
  return '<div class="visit-field' + (field.kind === 'prompts' ? ' visit-wide' : '') + '" data-visit-field="' + field.key + '"><span>' + field.label + '</span>' + inherit + control + '<small>' + field.help + '</small></div>';
}

function setVisitField(scope, field, value, inherited) {
  const id = 'visit-' + scope + '-' + field.key;
  if (field.kind === 'range') {
    $(id + '-min').value = value?.[0] ?? '';
    $(id + '-max').value = value?.[1] ?? '';
  } else $(id).value = field.kind === 'prompts' ? (value || []).join('\n') : Number(value || 0) * 100;
  if (scope === 'area') {
    const row = $('visit-area-fields').querySelector('[data-visit-field="' + field.key + '"]');
    row.querySelector('[data-override]').checked = !inherited;
    row.querySelectorAll('.control').forEach((input) => { input.disabled = inherited; });
    row.querySelector('.visit-inherit-state').textContent = inherited ? '继承全局默认' : '覆盖全局默认';
    row.classList.toggle('is-inherited', inherited);
  }
}

function readVisitField(scope, field) {
  const id = 'visit-' + scope + '-' + field.key;
  if (field.kind === 'range') {
    const range = [Number($(id + '-min').value), Number($(id + '-max').value)];
    if (range[0] > range[1]) throw new Error(field.label + '：最小值不能大于最大值');
    return range;
  }
  if (field.kind === 'probability') return Number($(id).value) / 100;
  const prompts = $(id).value.split('\n').map((line) => line.trim()).filter(Boolean);
  if (prompts.length > 50 || prompts.some((line) => line.length > 500)) throw new Error(field.label + '：最多 50 条，每条最多 500 字');
  return prompts;
}

function setupAutoVisit() {
  $('visit-default-fields').innerHTML = VISIT_FIELDS.map((field) => visitFieldMarkup('default', field)).join('');
  $('visit-area-fields').innerHTML = VISIT_FIELDS.map((field) => visitFieldMarkup('area', field)).join('');
  $('visit-global-form').addEventListener('input', (event) => {
    const field = event.target.closest('[data-visit-field]');
    visitGlobalDirty.add(field ? 'defaults.' + field.dataset.visitField : (event.target.id === 'visit-global-limit' ? 'daily_limit' : 'check_interval_minutes'));
    text('visit-global-feedback', '有未保存的改动');
  });
  $('visit-area-form').addEventListener('input', (event) => {
    const field = event.target.closest('[data-visit-field]');
    visitAreaDirty.add(field ? field.dataset.visitField : (event.target.id === 'visit-area-enabled' ? 'enabled' : 'daily_limit'));
    text('visit-area-feedback', '有未保存的改动');
  });
  $('visit-area-fields').addEventListener('change', (event) => {
    const key = event.target.dataset.override;
    if (!key) return;
    const row = event.target.closest('[data-visit-field]');
    const inherited = !event.target.checked;
    row.querySelectorAll('.control').forEach((input) => { input.disabled = inherited; });
    row.querySelector('.visit-inherit-state').textContent = inherited ? '继承全局默认' : '覆盖全局默认';
    row.classList.toggle('is-inherited', inherited);
    if (inherited) setVisitField('area', VISIT_FIELDS.find((field) => field.key === key), visitConfig.defaults[key], true);
  });
  $('visit-area-select').addEventListener('change', async (event) => {
    const next = event.target.value;
    if (visitAreaDirty.size && !await confirmDialog('切换域', '这个域有未保存的修改。放弃修改并切换域？', '放弃并切换')) {
      event.target.value = visitSelectedArea;
      return;
    }
    visitSelectedArea = next;
    visitAreaDirty.clear();
    renderVisitArea();
  });
  $('visit-refresh').onclick = () => refreshAutoVisit(true);
  $('visit-pause').onclick = async () => {
    const button = $('visit-pause');
    button.disabled = true;
    try {
      const data = await api('/api/voice/auto-visit/' + (visitStatus?.paused ? 'resume' : 'pause'), { method: 'POST' });
      visitStatus = data.status;
      renderVisitStatus();
      toast(visitStatus.paused ? '已暂停串门' : '已恢复串门', '今日次数与冷却保持有效', 'ok');
    } catch (err) { toast('操作失败', err.message, 'err'); }
    finally { button.disabled = !visitStatus; }
  };
  $('visit-global-form').onsubmit = (event) => { event.preventDefault(); saveVisitConfig('global'); };
  $('visit-area-form').onsubmit = (event) => { event.preventDefault(); saveVisitConfig('area'); };
}

async function refreshAutoVisit(loadAreas) {
  if (visitFetchPending) return;
  visitFetchPending = true;
  try {
    const data = await api('/api/voice/auto-visit');
    visitConfig = data.config;
    visitStatus = data.status;
    if (!visitConfig || !visitStatus) throw new Error('自动串门接口未返回配置或状态');
    if (loadAreas || !visitAreas.length) {
      try { visitAreas = (await api('/api/oopz/areas')).areas || []; }
      catch (err) { text('visit-area-empty', '域列表读取失败：' + err.message + '。请检查桥接连接后点刷新。'); }
    }
    renderVisitStatus();
    if (!visitGlobalDirty.size) renderVisitGlobal();
    renderVisitAreaSelector();
    if (!visitAreaDirty.size) renderVisitArea();
    else renderVisitAreaStatus();
  } catch (err) {
    text('visit-phase', '自动串门状态读取失败');
    text('visit-error', err.message + '。请检查语音服务后点刷新。');
  } finally { visitFetchPending = false; }
}

function visitTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '—';
  const seconds = Math.ceil((date.getTime() - Date.now()) / 1000);
  return seconds > 0 ? fmtDuration(seconds) + ' 后' : '已到时间';
}

function renderVisitStatus() {
  if (!visitStatus) return;
  const st = visitStatus;
  const phase = String(st.phase || 'waiting').toLowerCase();
  text('visit-phase', st.paused ? '自动串门已暂停' : (VISIT_PHASES[phase] || st.phase));
  text('visit-reason', st.pause_reason || '只进入已开启的域，先选有人的域，再随机选择房间。');
  text('visit-count', (st.daily_count || 0) + ' / ' + (st.daily_limit ? st.daily_limit + ' 次' : '不限'));
  $('visit-count').title = '北京时间 ' + (st.day || '今日');
  text('visit-next', visitTime(st.next_check_at));
  text('visit-leave', visitTime(st.leave_at));
  text('visit-cooldown', visitTime(st.global_cooldown_until));
  const action = st.last_action;
  $('visit-action').textContent = typeof action === 'string' ? action : (action?.reason || action?.message || action?.kind || '');
  $('visit-error').textContent = typeof st.last_error === 'string' ? st.last_error : (st.last_error?.message || '');
  $('visit-pause').textContent = st.paused ? '恢复串门' : '暂停串门';
  $('visit-pause').disabled = false;
  text('visit-enabled-count', Object.values(visitConfig.areas || {}).filter((area) => area.enabled).length + ' 个域已开启');
  renderVisitAreaStatus();
}

function renderVisitGlobal() {
  $('visit-check-min').value = visitConfig.check_interval_minutes[0];
  $('visit-check-max').value = visitConfig.check_interval_minutes[1];
  $('visit-global-limit').value = visitConfig.daily_limit;
  VISIT_FIELDS.forEach((field) => setVisitField('default', field, visitConfig.defaults[field.key]));
  $('visit-global-save').disabled = false;
}

function renderVisitAreaSelector() {
  const select = $('visit-area-select');
  const ids = new Set(visitAreas.map((row) => row.id).filter(Boolean));
  Object.keys(visitConfig.areas || {}).forEach((id) => ids.add(id));
  const options = [...ids].map((id) => [id, visitAreas.find((row) => row.id === id)?.name || id]);
  if (!ids.has(visitSelectedArea)) visitSelectedArea = options[0]?.[0] || '';
  select.innerHTML = options.length ? options.map(([id, name]) => '<option value="' + escapeHtml(id) + '">' + escapeHtml(name) + '</option>').join('') : '<option value="">暂无可用域</option>';
  select.value = visitSelectedArea;
  select.disabled = !options.length;
  show($('visit-area-form'), !!options.length);
  show($('visit-area-empty'), !options.length);
  if (!options.length) text('visit-area-empty', '尚未读取到已加入的域。请先连接 Oopz 并加入一个域，然后点刷新。');
}

function renderVisitArea() {
  if (!visitSelectedArea) return;
  const area = visitConfig.areas?.[visitSelectedArea] || {};
  $('visit-area-enabled').checked = !!area.enabled;
  $('visit-area-limit').value = area.daily_limit || 0;
  VISIT_FIELDS.forEach((field) => {
    const overridden = Object.prototype.hasOwnProperty.call(area.overrides || {}, field.key);
    setVisitField('area', field, overridden ? area.overrides[field.key] : visitConfig.defaults[field.key], !overridden);
  });
  $('visit-area-feedback').textContent = '';
  renderVisitAreaStatus();
}

function renderVisitAreaStatus() {
  if (!visitSelectedArea || !visitStatus) return;
  const st = visitStatus.areas?.[visitSelectedArea] || {};
  text('visit-area-status', '今日 ' + (st.daily_count || 0) + ' 次自动进房 · 域内手动冷却：' + (st.manual_cooldown_until ? visitTime(st.manual_cooldown_until) : '无'));
}

async function saveVisitConfig(scope) {
  const form = $('visit-' + scope + '-form');
  const feedback = $('visit-' + scope + '-feedback');
  const changed = scope === 'global' ? visitGlobalDirty : visitAreaDirty;
  if (!changed.size) { feedback.textContent = '设置没有变化'; return; }
  try {
    const updates = {};
    if (scope === 'global') {
      if (changed.has('check_interval_minutes')) {
        const range = [Number($('visit-check-min').value), Number($('visit-check-max').value)];
        if (range[0] > range[1]) throw new Error('检查间隔：最小值不能大于最大值');
        updates.check_interval_minutes = range;
      }
      if (changed.has('daily_limit')) updates.daily_limit = Number($('visit-global-limit').value);
      VISIT_FIELDS.forEach((field) => {
        if (changed.has('defaults.' + field.key)) {
          updates.defaults = updates.defaults || {};
          updates.defaults[field.key] = readVisitField('default', field);
        }
      });
    } else {
      const area = {};
      if (changed.has('enabled')) area.enabled = $('visit-area-enabled').checked;
      if (changed.has('daily_limit')) area.daily_limit = Number($('visit-area-limit').value);
      VISIT_FIELDS.forEach((field) => {
        if (!changed.has(field.key)) return;
        area.overrides = area.overrides || {};
        const override = $('visit-area-fields').querySelector('[data-override="' + field.key + '"]');
        area.overrides[field.key] = override.checked ? readVisitField('area', field) : null;
      });
      updates.areas = { [visitSelectedArea]: area };
    }
    form.inert = true;
    feedback.textContent = '正在保存…';
    const data = await api('/api/voice/auto-visit/config', { method: 'POST', body: { updates } });
    changed.clear();
    if (data.config) visitConfig = data.config;
    if (data.status) visitStatus = data.status;
    await refreshAutoVisit(false);
    feedback.textContent = '已保存并生效';
    toast(scope === 'global' ? '全局规则已保存' : '这个域已保存', '后台已应用新设置', 'ok');
  } catch (err) { feedback.textContent = '保存失败：' + err.message; toast('保存失败', err.message, 'err'); }
  finally { form.inert = false; }
}
