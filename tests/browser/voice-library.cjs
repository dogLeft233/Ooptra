/* 正式页面上的音色创建、试听、切换、失败与移动端检查；供应商/配置 API 使用隔离夹具。 */
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.OOPTRA_PLAYWRIGHT_MODULE || 'playwright');
const assets = path.resolve(__dirname, '../../src/webui/assets');
const artifacts = process.env.OOPTRA_BROWSER_ARTIFACTS || path.resolve(__dirname, '../../assets/acceptance/voice-library');
const model = 'qwen-audio-3.1-tts-flash';
const custom = model + '-girl-fixture';
const wav = Buffer.alloc(48044);
wav.write('RIFF'); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8); wav.writeUInt32LE(16, 16);
wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22); wav.writeUInt32LE(24000, 24);
wav.writeUInt32LE(48000, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36); wav.writeUInt32LE(48000, 40);
const server = http.createServer((req, res) => {
  const name = req.url.startsWith('/assets/') ? path.basename(req.url) : 'index.html';
  res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : name.endsWith('.svg') ? 'image/svg+xml' : 'text/html');
  res.end(fs.readFileSync(path.join(assets, name)));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, executablePath: process.env.OOPTRA_BROWSER_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
    const errors = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {if (message.type() === 'error') errors.push(message.text());});
    let selected = 'longanhuan_v3.1', created = false, failApply = false;
    await page.route('**/api/**', async route => {
      const request = route.request();
      const endpoint = new URL(request.url()).pathname;
      const body = request.headers()['content-type']?.includes('application/json') ? request.postDataJSON() : {};
      let result = {};
      if (endpoint === '/api/credentials') result = {credentials: {has_password: true}};
      if (endpoint === '/api/auth/status') result = {configured: true, authenticated: true};
      if (endpoint === '/api/voice/voices') {
        if (request.method() === 'POST') {
          assert(request.postData().includes('name="prefix"'));
          assert(request.postData().includes('girl'));
          assert(request.postData().includes('sample.wav'));
          created = true; result = {voice: {voice_id: custom, status: 'DEPLOYING'}};
        } else result = {current_voice: selected, model, page: 0, has_more: false, voices: [
          {voice_id: 'longanhuan_v3.1', kind: 'system', status: 'OK'},
          ...(created ? [{voice_id: custom, kind: 'custom', status: 'OK'}] : []),
          {voice_id: model + '-pending', kind: 'custom', status: 'DEPLOYING'},
        ]};
      }
      if (endpoint === '/api/config') {
        if (request.method() === 'POST') {
          writes.push(body); selected = body.updates.voice['aliyun_deepseek.tts_voice'];
          result = {hot_reload_failed: failApply, application_state: failApply ? 'partial' : 'applied', notes: failApply ? ['测试应用失败'] : []};
        } else result = {revision: 'fixture-revision', groups: {voice: {fields: {
          backend: {value: 'aliyun_deepseek'}, 'aliyun_deepseek.tts_model': {value: model},
        }}}};
      }
      if (endpoint === '/api/voice/preview') {
        assert.equal(body.voice, custom);
        assert.equal(selected, 'longanhuan_v3.1');
        result = {wav_base64: wav.toString('base64')};
      }
      await route.fulfill({json: {ok: true, ...result}});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/voice`);
    await page.locator('#screen-app').waitFor({state: 'visible'});
    await page.locator('[data-tab="voices"]').click();
    await page.waitForFunction(() => document.querySelector('#voices-current').textContent.includes('longanhuan'));
    fs.mkdirSync(artifacts, {recursive: true});
    await page.screenshot({path: path.join(artifacts, '01-library.png'), fullPage: true});
    await page.locator('#voices-prefix').fill('girl');
    await page.locator('#voices-file').setInputFiles({name: 'sample.wav', mimeType: 'audio/wav', buffer: wav});
    await page.locator('#voices-create').click();
    await page.waitForFunction(() => document.querySelector('#voices-created').textContent.includes('-girl-fixture'));
    await page.screenshot({path: path.join(artifacts, '02-created.png'), fullPage: true});
    await page.locator('#voices-refresh').click();
    await page.locator('#voices-select option').filter({hasText: custom}).waitFor({state: 'attached'});
    await page.locator('#voices-select').selectOption(custom);
    await page.locator('#voices-preview').click();
    await page.locator('#voices-player').waitFor({state: 'visible'});
    await page.screenshot({path: path.join(artifacts, '03-preview.png'), fullPage: true});
    await page.locator('#voices-use').click();
    await page.waitForFunction(() => document.querySelector('#voices-feedback').textContent.startsWith('已切换'));
    assert.equal(writes[0].expected_revision, 'fixture-revision');
    assert.equal(writes[0].updates.voice['aliyun_deepseek.tts_voice'], custom);
    await page.screenshot({path: path.join(artifacts, '04-selected.png'), fullPage: true});
    failApply = true;
    await page.locator('#voices-select').selectOption('longanhuan_v3.1');
    await page.locator('#voices-use').click();
    await page.waitForFunction(() => document.querySelector('#voices-feedback').textContent.includes('运行应用未完成'));
    await page.screenshot({path: path.join(artifacts, '05-apply-failed.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: path.join(artifacts, '06-mobile.png'), fullPage: true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert.deepEqual(errors, []);
    console.log('Voice library: create, refresh, preview, persistent selection, failed application and mobile layout passed.');
  } finally {await browser.close(); server.close();}
})().catch(error => {console.error(error); server.close(); process.exitCode = 1;});
