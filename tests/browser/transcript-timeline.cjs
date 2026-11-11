/* 正式资源：逐句展示、实际状态更新、分页、失败保留、窄屏与旧请求拒绝。 */
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.OOPTRA_PLAYWRIGHT_MODULE || 'playwright');
const assets = path.resolve(__dirname, '../../src/webui/assets');
const artifacts = process.env.OOPTRA_BROWSER_ARTIFACTS || path.resolve(__dirname, '../../assets/acceptance/transcript-timeline');
const server = http.createServer((req, res) => {
  const file = req.url.startsWith('/assets/') ? path.basename(req.url) : 'index.html';
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : 'text/html');
  res.end(fs.readFileSync(path.join(assets, file)));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, executablePath: process.env.OOPTRA_BROWSER_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {if (message.type() === 'error') errors.push(message.text());});
    let completed = false, fail = false, hold = false, releaseOld, oldStarted;
    const transcriptRequests = [];
    let oldPending = new Promise(resolve => {oldStarted = resolve;});
    const rows = Array.from({length: 52}, (_, i) => {
      const sequence = 52 - i;
      return {sequence, recorded_at: 1794140000 + sequence, updated_at: Date.now()/1000, area: 'a', channel: 'b',
        session_id: 'session-a', input_ref: 'input-' + sequence, user_key: 'oopz:user-' + sequence,
        speaker_name: sequence === 52 ? '小陈' : sequence === 51 ? '小林' : '频道成员',
        recognition_status: sequence === 48 ? 'failed' : 'recognized',
        text: sequence === 48 ? '' : sequence === 52 ? '大肥鱼，你在吗？' : sequence === 51 ? '我先自己算一下。' : sequence === 50 ? '' : sequence === 49 ? '<img src=x onerror="window.injected=1">' : '第 ' + sequence + ' 句：关于反常积分收敛性的推导。',
        text_final: true, stage: sequence === 1 ? 'deciding' : 'finished', decision: sequence === 52 ? 'speak' : 'silent',
        reason: sequence === 48 ? 'recognition_failed' : sequence === 52 ? 'keyword' : sequence === 50 ? 'unselected_or_expired' : 'semantic_silent',
        outcome: sequence === 52 ? 'completed' : sequence === 50 ? 'not_executed' : 'semantic_silent', elapsed_ms: 800};
    });
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      let result = {};
      if (url.pathname === '/api/auth/status') result = {configured: true, authenticated: true};
      if (url.pathname === '/api/credentials') result = {credentials: {has_password: true}};
      if (url.pathname === '/api/voice/status') result = {status: {joined: true, enabled: true, backend: 'aliyun_deepseek', area: 'a', channel: 'b', voice_observation: {recent_decisions: []}}};
      if (url.pathname === '/api/voice/transcripts') {
        transcriptRequests.push(url);
        if (fail) { await route.fulfill({status: 503, json: {ok: false, error: '暂时读取失败'}}); return; }
        if (hold && url.searchParams.get('area') === 'a') {
          hold = false; oldStarted();
          await new Promise(resolve => {releaseOld = resolve;});
        }
        const before = Number(url.searchParams.get('before') || 10000);
        let filtered = rows.filter(row => row.sequence < before).map(row => ({...row}));
        if (!completed && before > 52) filtered[0] = {...filtered[0], stage: 'deciding', outcome: 'pending'};
        if (!url.searchParams.has('area')) filtered.unshift({...rows[0], sequence: 9999, speaker_name: '另一频道成员', area: 'elsewhere', channel: 'other', text: '其他频道记录'});
        result = {rows: filtered.slice(0, 50), next_before: filtered.length > 50 ? filtered[49].sequence : null, total: rows.length + (url.searchParams.has('area') ? 0 : 1)};
      }
      await route.fulfill({json: {ok: true, ...result}});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/voice`);
    const list = page.locator('#voice-trace-list');
    await page.waitForFunction(() => document.querySelectorAll('#voice-trace-list > li').length === 50);
    assert.match(await list.textContent(), /小陈/);
    assert.match(await list.textContent(), /小林/);
    assert.match(await list.textContent(), /大肥鱼，你在吗/);
    assert.match(await list.textContent(), /模型判断应继续倾听/);
    assert.match(await list.textContent(), /语音识别失败，未取得最终转写/);
    assert.match(await list.textContent(), /被后续发言替换或等待超时/);
    assert.equal(await list.locator('img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    await page.locator('#voice-trace-title').scrollIntoViewIfNeeded();
    fs.mkdirSync(artifacts, {recursive: true});
    completed = true;
    await page.locator('#voice-trace-refresh').click();
    await page.waitForFunction(() => document.querySelector('#voice-trace-list li').textContent.includes('已回复'));
    assert.equal(await list.locator('li').count(), 50);
    await page.locator('.voice-trace-card').screenshot({path: path.join(artifacts, '01-sentences.png')});
    await page.locator('#voice-trace-more').click();
    await page.waitForFunction(() => document.querySelectorAll('#voice-trace-list > li').length === 52);
    assert(await page.locator('#voice-trace-more').isHidden());
    rows.unshift(...Array.from({length: 60}, (_, i) => ({...rows[0], sequence: 112 - i,
      input_ref: 'new-' + (112 - i), text: '新到达的第 ' + (112 - i) + ' 句'})));
    await page.locator('#voice-trace-refresh').click();
    await page.waitForFunction(() => document.querySelector('#voice-trace-status').textContent.includes('/ 112'));
    assert.equal(await list.locator('li').count(), 102);
    assert(await page.locator('#voice-trace-more').isVisible(), 'newest page must retain a cursor for the ten missing sentences');
    await page.locator('.voice-trace-card').screenshot({path: path.join(artifacts, '03-gap.png')});
    await page.locator('#voice-trace-more').click();
    await page.waitForFunction(() => document.querySelectorAll('#voice-trace-list > li').length === 112);
    assert(await page.locator('#voice-trace-more').isHidden());
    await page.locator('.voice-trace-card').screenshot({path: path.join(artifacts, '04-gap-filled.png')});
    const oldest = list.locator('li').last();
    assert.match(await oldest.textContent(), /处理中/);
    rows.find(row => row.sequence === 1).stage = 'finished';
    const requestsBeforeFinal = transcriptRequests.length;
    await page.locator('#voice-trace-refresh').click();
    await page.waitForFunction(() => document.querySelector('#voice-trace-list li:last-child').textContent.includes('保持沉默'));
    assert.equal(await list.locator('li').count(), 112);
    const finalRequests = transcriptRequests.slice(requestsBeforeFinal);
    assert.equal(finalRequests.length, 2, 'refresh must issue only newest plus one pending page');
    assert.equal(finalRequests[1].searchParams.get('before'), '2');
    await oldest.scrollIntoViewIfNeeded();
    await page.locator('.voice-trace-card').screenshot({path: path.join(artifacts, '05-old-final.png')});
    fail = true;
    await page.locator('#voice-trace-refresh').click();
    await page.waitForFunction(() => document.querySelector('#voice-trace-status').textContent.includes('读取失败'));
    assert.equal(await list.locator('li').count(), 112);
    fail = false;
    // 切换范围后，迟到的当前频道请求不能覆盖全部频道。
    hold = true;
    await page.locator('#voice-trace-refresh').click();
    await oldPending;
    await page.locator('#voice-trace-scope').selectOption('all');
    await page.waitForFunction(() => document.querySelector('#voice-trace-list').textContent.includes('其他频道记录'));
    releaseOld();
    await page.locator('#voice-trace-refresh').click();
    await page.waitForFunction(() => !document.querySelector('#voice-trace-refresh').disabled);
    assert.match(await list.locator('li').first().textContent(), /另一频道成员/);
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#voice-trace-title').scrollIntoViewIfNeeded();
    await page.locator('.voice-trace-card').screenshot({path: path.join(artifacts, '02-mobile.png')});
    assert.deepEqual(errors, ['Failed to load resource: the server responded with a status of 503 (Service Unavailable)']); // 仅允许本测试主动注入的一次503；任何额外页面/脚本错误均失败。
    console.log('Transcript timeline: speakers, text, reasons, final playback, pagination, failure recovery, stale requests, XSS and mobile passed.');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => {console.error(error); process.exitCode = 1;});
