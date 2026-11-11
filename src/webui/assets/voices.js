/* 云端目录是音色权威；选择复用配置事务，不保存第二份当前音色。 */
(() => {
  let pageIndex = 0;
  let model = '';
  let busy = false;
  let sequence = 0;
  let audioUrl = '';
  let voices = new Map();
  const feedback = message => text('voices-feedback', message);
  const requestId = () => crypto.randomUUID ? crypto.randomUUID() :
    Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');

  function updateControls() {
    const ready = voices.get($('voices-select').value)?.status === 'OK';
    for (const id of ['voices-refresh', 'voices-create', 'voices-more', 'voices-select']) $(id).disabled = busy;
    $('voices-use').disabled = busy || !ready;
    $('voices-preview').disabled = busy || !ready;
  }

  function render(selected) {
    const select = $('voices-select');
    select.replaceChildren();
    for (const voice of voices.values()) {
      const option = document.createElement('option');
      option.value = voice.voice_id;
      option.textContent = (voice.kind === 'system' ? '系统音色 · ' : '') + voice.voice_id +
        (voice.status === 'OK' ? '' : ' · ' + voice.status);
      option.disabled = voice.status !== 'OK';
      select.appendChild(option);
    }
    if (voices.get(selected)?.status === 'OK') select.value = selected;
    updateControls();
  }

  async function load(append = false) {
    const generation = ++sequence;
    const result = await api('/api/voice/voices?page=' + (append ? pageIndex + 1 : 0));
    if (generation !== sequence) return;
    const selected = append ? $('voices-select').value : result.current_voice;
    if (!append) voices = new Map();
    for (const voice of result.voices) voices.set(voice.voice_id, voice);
    model = result.model;
    pageIndex = result.page;
    text('voices-current', '当前模型：' + model + '；当前音色：' + result.current_voice);
    $('voices-more').hidden = !result.has_more;
    render(selected);
  }

  async function run(action) {
    if (busy) return;
    busy = true;
    updateControls();
    feedback('处理中…');
    try { await action(); }
    catch (error) { feedback(error.message); }
    finally { busy = false; updateControls(); }
  }

  window.loadVoiceLibrary = () => run(async () => { await load(); feedback('音色列表已更新'); });
  $('voices-refresh').onclick = window.loadVoiceLibrary;
  $('voices-more').onclick = () => run(async () => { await load(true); feedback('已加载更多音色'); });
  $('voices-select').onchange = updateControls;

  $('voices-create').onclick = () => run(async () => {
    const file = $('voices-file').files[0];
    const prefix = $('voices-prefix').value.trim();
    if (!/^[A-Za-z0-9]{1,10}$/.test(prefix)) throw new Error('音色名称须为 1～10 个英文字母或数字');
    if (!file || !/\.(wav|mp3|m4a)$/i.test(file.name) || !file.size || file.size > 10 * 1024 * 1024) {
      throw new Error('请选择不超过 10 MB 的 WAV、MP3 或 M4A 文件');
    }
    const body = new FormData();
    body.append('prefix', prefix);
    body.append('audio', file);
    feedback('正在创建音色，请勿重复提交；超时后先刷新列表确认结果。');
    const result = await api('/api/voice/voices', {method: 'POST', body});
    text('voices-created', '已创建：' + result.voice.voice_id + '。刷新列表确认就绪后，即可试听和使用。');
    // 单独保留创建回执；后续目录查询失败不会把成功创建报成失败。
    feedback('创建成功，请刷新音色列表。');
  });

  $('voices-use').onclick = () => run(async () => {
    const voice = $('voices-select').value;
    const config = await api('/api/config');
    const fields = config.groups.voice.fields;
    if (fields.backend.value !== 'aliyun_deepseek' || fields['aliyun_deepseek.tts_model'].value !== model) {
      throw new Error('语音后端或模型已改变，请刷新音色列表后再切换');
    }
    await api('/api/voice/voices/check', {method: 'POST', body: {voice_id: voice}});
    const result = await api('/api/config', {method: 'POST', body: {
      updates: {voice: {'aliyun_deepseek.tts_voice': voice}},
      expected_revision: config.revision, request_id: requestId(),
    }});
    if (result.hot_reload_failed || !['applied', undefined].includes(result.application_state)) {
      feedback('音色已保存，但运行应用未完成：' + (result.notes || []).join('；') + '。请重试切换。');
      return;
    }
    await load();
    feedback('已切换：' + voice + '。Bot 的下一次新生成语音将使用此音色。');
  });

  $('voices-preview').onclick = () => run(async () => {
    const voice = $('voices-select').value;
    const result = await api('/api/voice/preview', {method: 'POST', body: {
      kind: 'voice', text: $('voices-text').value.trim(), voice, model,
    }});
    const bytes = Uint8Array.from(atob(result.wav_base64), char => char.charCodeAt(0));
    const player = $('voices-player');
    player.pause();
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = URL.createObjectURL(new Blob([bytes], {type: 'audio/wav'}));
    player.src = audioUrl;
    player.hidden = false;
    feedback('试听已生成，可播放对比；Bot 当前音色未改变。');
  });
})();
