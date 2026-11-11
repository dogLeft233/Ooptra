/* Offline API fixtures only. Production HTML/CSS/JS remain the canonical UI. */
'use strict';
(() => {
  const fixture = window.OOPTRA_PREVIEW;
  const clone = value => JSON.parse(JSON.stringify(value));
  const now = () => new Date().toISOString();
  let authenticated = false;
  let password = 'demo';
  let persona = '你是 Ooptra，一个自然、友善的语音伙伴。简短接话，尊重正在聊天的成员。';
  let messages = [{role: 'user', content: '今天一起玩什么？'}, {role: 'assistant', content: '你们想玩合作游戏吗？'}];
  const groups = clone(fixture.groups);
  const visit = clone(fixture.auto_visit);
  const visitStatus = {phase: 'waiting', paused: false, day: '演示日', daily_count: 1, daily_limit: 3,
    next_check_at: new Date(Date.now() + 720000).toISOString(), areas: {},
    empty_room: {state: 'occupied', remaining_seconds: null}};
  const voice = {enabled: true, joined: false, backend: 'mimo_cascade', area: '', channel: '',
    output_volume: 50, output_muted: false,
    turns: 2, speaking: false, last_user_text: '今天一起玩什么？', last_reply: '你们想玩合作游戏吗？',
    model_connection: {state: 'ready'}, voice_observation: {recent_decisions: []}};
  const areas = [{id: 'demo-area-1', name: '朋友的游戏小屋'}, {id: 'demo-area-2', name: '周末闲聊'}];
  const channels = [{id: 'demo-channel-1', name: '游戏语音'}, {id: 'demo-channel-2', name: '休息室'}];
  let bindings = [{group_id: 'Demo:GroupMessage:123456789', umo: 'Demo:GroupMessage:123456789', area: areas[0].id, areas: [areas[0].id], channel: '', label: '演示开黑群'}];
  let sourceRules = [];
  const logs = ['14:20:00 INFO [Preview] 离线演示已启动，没有连接真实服务。',
    '14:20:01 INFO [Bridge] 演示数据：Oopz / OneBot 已连接',
    '14:20:02 INFO [Voice] 回复概率 30%，连续对话窗口 30 秒，独处 30 秒退出'];
  const checks = [{id: 'preview', title: '离线演示环境', state: 'pass', detail: '仅模拟结果，真实部署请运行自检', elapsed_ms: 0}];
  const preflight = {supported: true, checks, detail: '演示：Git＋虚拟环境＋launcher，未执行真实检查'};
  let backups = [{id: 'a'.repeat(32), created_at: Date.now() / 1000 - 3600, size: 256000, can_delete: true}];
  let installed = 'main';
  let check = null;
  let job = null;
  let jobStart = 0;
  const storage = () => ({total_bytes: backups.length * 256000, backups_bytes: backups.length * 256000,
    backups_count: backups.length, environments_bytes: 0, browsers_bytes: 0, staging_bytes: 0});
  const backup = () => {
    const item = {id: crypto.randomUUID().replaceAll('-', ''), created_at: Date.now() / 1000,
      size: 256000, can_delete: true};
    backups.unshift(item);
    return item;
  };
  function merge(target, patch) {
    for (const [key, value] of Object.entries(patch)) {
      if (value === null) delete target[key];
      else if (value && typeof value === 'object' && !Array.isArray(value)) merge(target[key] ||= {}, value);
      else target[key] = value;
    }
  }
  function tickJob() {
    if (!job || job.phase === 'complete') return;
    const stages = ['download', 'environment', 'dependencies', 'browser', 'backup', 'awaiting_restart', 'checking', 'complete'];
    const stage = stages[Math.min(stages.length - 1, Math.floor((Date.now() - jobStart) / 1800))];
    job.phase = ['awaiting_restart', 'checking', 'complete'].includes(stage) ? stage : 'preparing';
    job.progress_stage = stage;
    job.detail = '离线演示：' + ({download: '下载代码', environment: '准备环境', dependencies: '安装依赖',
      browser: '检查浏览器', backup: '备份数据', awaiting_restart: '等待重启', checking: '健康检查', complete: '操作已完成'}[stage]);
    if (stage === 'complete') {
      installed = job.channel;
      check = null;
      for (const item of backups) { item.can_delete = true; delete item.delete_reason; }
    }
  }
  function startJob(action, channel, backupId) {
    const item = backup();
    for (const row of backups) if (row.id === backupId || row.id === item.id) {
      row.can_delete = false; row.delete_reason = '演示任务正在引用';
    }
    jobStart = Date.now();
    job = {id: crypto.randomUUID(), action, channel, phase: 'preparing', progress_stage: 'download',
      started_at: Date.now() / 1000, detail: '离线演示：准备操作'};
    return {job};
  }
  async function dispatch(endpoint, method, body, query) {
    if (endpoint === '/api/auth/status') return {configured: true, authenticated};
    if (endpoint === '/api/auth/login') {
      if (body.password !== password) throw {status: 401, error: '演示密码不正确，初始密码为 demo'};
      authenticated = true; return {};
    }
    if (endpoint === '/api/auth/logout') { authenticated = false; return {}; }
    if (!authenticated) throw {status: 401, error: '请登录离线演示'};
    if (endpoint === '/api/status') return {process: {version: fixture.version, uptime_seconds: 8640},
      bridge: {runtime: {running: true}, oopz: {connected: true, joined_areas: 2, uptime_seconds: 8640, target: '离线演示'},
        onebot: {connected: true, target: '演示反向 WS', attempts: 1, drops: 0}, traffic: {events: 128, actions: 45},
        recent_events: [{kind: 'message.group', preview: '演示消息：今晚玩什么？', time: Date.now() / 1000}],
        recent_actions: [{action: 'send_group_msg', time: Date.now() / 1000}]}};
    if (endpoint === '/api/credentials') return {credentials: {has_password: true, has_private_key: true,
      login_phone: '演示账号', person_uid: 'demo-bot', device_id: 'demo-device', jwt_token: '演示占位符', expires_in_seconds: 86400}};
    if (endpoint.startsWith('/api/login/')) throw {status: 400, error: '离线预览不登录真实 Oopz 账号，请在正式部署中操作'};
    if (endpoint === '/api/config') {
      if (method === 'POST') {
        const changed = {};
        for (const [group, fields] of Object.entries(body.updates || {})) {
          changed[group] = Object.keys(fields);
          for (const [name, value] of Object.entries(fields)) {
            const field = groups[group]?.fields[name];
            if (!field) throw {status: 400, error: '未知演示字段：' + name};
            field.value = field.sensitive ? null : value;
            if (field.sensitive) field.is_set = !!value;
          }
        }
        if (body.updates?.voice?.backend) voice.backend = body.updates.voice.backend;
        if (body.updates?.voice?.enabled !== undefined) voice.enabled = body.updates.voice.enabled;
        if (body.updates?.voice?.output_volume !== undefined) {
          voice.output_volume = body.updates.voice.output_volume;
          voice.output_muted = false;
        }
        if (body.updates?.webui?.token !== undefined) {
          if (!body.updates.webui.token) throw {status: 400, error: '演示密码不可为空'};
          password = body.updates.webui.token; authenticated = false;
          return {changed, reauth_required: true};
        }
        return {changed};
      }
      return {groups, path: '离线演示 · 配置仅保存在本页内存，刷新恢复初始值'};
    }
    if (endpoint === '/api/bridge/restart') return {message: '离线演示：桥接重启完成'};
    if (endpoint === '/api/logs') return {files: [{name: 'preview.log', size: 12000}], default: 'preview.log'};
    if (endpoint === '/api/logs/tail') return {lines: logs};
    if (endpoint === '/api/oopz/areas') return {areas, default_area: areas[0].id, default_channel: channels[0].id};
    if (endpoint === '/api/oopz/channels') return {channels};
    if (endpoint === '/api/voice/astrbot/bindings') {
      if (body.action === 'save') {
        bindings = bindings.filter(row => row.group_id !== body.group_id && row.group_id !== body.previous_key);
        bindings.push({...body, umo: body.group_id, area: body.areas[0]});
      }
      if (body.action === 'delete') bindings = bindings.filter(row => row.group_id !== body.group_id);
      if (['source-save', 'source-delete'].includes(body.action)) {
        sourceRules = sourceRules.filter(row => row.area !== body.area || row.channel !== body.channel);
        if (body.action === 'source-save') sourceRules.push(body);
      }
      if (body.action === 'source-options') return {groups: [{name: '演示开黑群', umo: 'Demo:GroupMessage:123456789', kind: 'qq'}, {name: '演示私聊', umo: 'Demo:FriendMessage:456', kind: 'session'}]};
      return {ok: true, source_management: true, bindings, context_sources: {rules: sourceRules}, platforms: [{id: 'Demo', name: '演示实例'}], context_sync: {personas: [{id: '语音伙伴', name: '语音伙伴'}]}};
    }
    if (endpoint === '/api/voice/transcripts') return {rows: [
      {sequence: 2, recorded_at: Date.now()/1000-5, updated_at: Date.now()/1000-3, speaker_name: '小林', user_key: 'oopz:demo-lin', text: '我先自己算一下。', text_final: true, stage: 'finished', decision: 'silent', reason: 'semantic_silent', outcome: 'semantic_silent', area: 'demo-area-1', channel: 'demo-channel-1', session_id: 'demo-session', input_ref: 'demo-input-2', elapsed_ms: 650},
      {sequence: 1, recorded_at: Date.now()/1000-15, updated_at: Date.now()/1000-10, speaker_name: '小陈', user_key: 'oopz:demo-chen', text: '大肥鱼，你在吗？', text_final: true, stage: 'finished', decision: 'speak', reason: 'keyword', outcome: 'completed', area: 'demo-area-1', channel: 'demo-channel-1', session_id: 'demo-session', input_ref: 'demo-input-1', elapsed_ms: 1800},
    ], total: 2, next_before: null};
    if (endpoint === '/api/voice/status') return {status: voice, auto_visit: visitStatus};
    if (endpoint === '/api/voice/output') {
      if (body.volume !== undefined) {
        if (!Number.isInteger(body.volume) || body.volume < 10 || body.volume > 100) throw {status: 400, error: '音量须为 10-100 的整数'};
        voice.output_volume = body.volume;
        voice.output_muted = false;
        groups.voice.fields.output_volume.value = body.volume;
      }
      if (body.muted !== undefined) {
        if (!voice.joined || typeof body.muted !== 'boolean') throw {status: 400, error: '进房后才能控制麦克风'};
        voice.output_muted = body.muted;
      }
      return {volume: voice.output_volume, muted: voice.output_muted, saved: body.volume !== undefined,
        message: body.muted !== undefined ? (voice.output_muted ? '麦克风已关闭，仍在接听' : '麦克风已打开') : '语音音量已保存'};
    }
    if (endpoint === '/api/voice/join') {
      Object.assign(voice, {joined: true, output_muted: false, area: body.area || areas[0].id, channel: body.channel || channels[0].id, join_source: 'manual'});
      return {status: voice};
    }
    if (endpoint === '/api/voice/leave') {Object.assign(voice, {joined: false, output_muted: false, area: '', channel: '', join_source: null}); return {status: voice};}
    if (endpoint === '/api/voice/stop') {voice.speaking = false; return {};}
    if (endpoint === '/api/voice/speak') {
      if (!voice.joined) throw {status: 400, error: '请先在演示中进房'};
      voice.last_reply = body.text; voice.turns++;
      return {mode: voice.backend === 'gemini_live' ? 'live' : 'cascade', pcm_bytes: 48000};
    }
    if (endpoint === '/api/voice/members') return {count: 2, live_members: 2, members: [
      {uid: 'demo-bot', name: 'Ooptra', mic_muted: false, speaker_muted: false, live: true},
      {uid: 'demo-friend', name: '朋友', mic_muted: false, speaker_muted: false, live: true}]};
    if (endpoint === '/api/persona') {if (method === 'PUT') persona = body.persona; return {persona};}
    if (endpoint === '/api/memory') {if (method === 'DELETE') messages = []; return {messages};}
    if (endpoint.startsWith('/api/voice/auto-visit')) {
      if (endpoint.endsWith('/pause')) visitStatus.paused = true;
      if (endpoint.endsWith('/resume')) visitStatus.paused = false;
      if (endpoint.endsWith('/config')) merge(visit, body.updates || {});
      visitStatus.daily_limit = visit.daily_limit;
      return {config: visit, status: visitStatus};
    }
    if (endpoint === '/api/voice/diagnostics') return {checks};
    if (endpoint === '/api/voice/preview/prompts') return {prompts: visit.defaults[query.get('kind') === 'leave' ? 'leave_prompts' : 'enter_prompts']};
    if (endpoint === '/api/voice/preview') throw {status: 400, error: '离线预览不调用模型或合成音频；请在正式部署中试听'};
    if (endpoint === '/api/maintenance/network/test') return {elapsed_ms: 0, detail: '离线演示，没有实际联网；正式部署将在部署机器测试'};
    if (endpoint === '/api/maintenance/preflight') return preflight;
    if (endpoint === '/api/maintenance/storage') return storage();
    if (endpoint === '/api/maintenance/check') {
      const channel = body.channel || installed;
      check = {channel, current_channel: installed, target_sha: 'b'.repeat(40), target_version: '演示更新',
        available: true, compatible: true, action: channel === installed ? 'update' : 'switch', requires_confirmation: channel !== installed};
      return check;
    }
    if (endpoint === '/api/maintenance/backups' && method === 'POST') return {backup: backup()};
    if (endpoint.startsWith('/api/maintenance/backups/') && method === 'DELETE') {
      const item = backups.find(row => row.id === endpoint.split('/').pop());
      if (!item?.can_delete) throw {status: 409, error: '演示备份不存在或受保护'};
      backups = backups.filter(row => row !== item); return {};
    }
    if (endpoint === '/api/maintenance/update') return startJob('update', body.channel || installed);
    if (endpoint === '/api/maintenance/restore') return startJob('restore', installed, body.backup_id);
    if (endpoint === '/api/maintenance/cleanup/preview') return {token: 'demo', items: [], total_bytes: 0};
    if (endpoint === '/api/maintenance/cleanup/apply') return {removed: []};
    if (endpoint === '/api/maintenance') {
      tickJob();
      const page = Math.max(1, Number(query.get('page')) || 1);
      const pages = Math.max(1, Math.ceil(backups.length / 10));
      const selected = Math.min(page, pages);
      return {preflight, restore_supported: true, health: {checks}, storage: storage(),
        backups: backups.slice((selected - 1) * 10, selected * 10), backup_page: {page: selected, pages, total: backups.length, page_size: 10},
        update: {version: fixture.version, current_channel: installed, channel: installed, current_sha: 'a'.repeat(40)}, check, job};
    }
    throw {status: 404, error: '此操作未提供离线演示，请在正式部署中使用'};
  }
  window.fetch = async (input, options = {}) => {
    const url = new URL(String(input), 'https://preview.invalid');
    let status = 200, result;
    try {
      if (!url.pathname.startsWith('/api/') || url.origin !== 'https://preview.invalid') throw {status: 400, error: '离线预览禁止真实 API 请求'};
      if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      const body = options.body ? JSON.parse(options.body) : {};
      result = {ok: true, ...clone(await dispatch(url.pathname, options.method || 'GET', body, url.searchParams))};
    } catch (err) {status = err.status || 400; result = {ok: false, error: err.error || err.message};}
    return new Response(JSON.stringify(result), {status, headers: {'Content-Type': 'application/json'}});
  };
  window.EventSource = class extends EventTarget {
    constructor() {
      super();
      this.timer = setTimeout(() => logs.forEach(line => this.dispatchEvent(new MessageEvent('line', {data: JSON.stringify({line})}))), 50);
    }
    close() {clearTimeout(this.timer);}
  };
  function offlineDownload() {
    if (typeof toast === 'function') toast('离线交互预览', '演示没有真实备份或日志文件；请在部署机器的 WebUI 下载。', 'ok');
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (link?.getAttribute('href')?.startsWith('/api/')) {event.preventDefault(); offlineDownload();}
  });
  const nativeOpen = window.open.bind(window);
  window.open = (url, ...args) => String(url).startsWith('/api/') ? (offlineDownload(), null) : nativeOpen(url, ...args);
})();
