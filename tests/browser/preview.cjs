/* Open the committed single file, without a backend or internet access. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.OOPTRA_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.OOPTRA_BROWSER_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage();
    const errors = [], remote = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(/^https?:/, route => {remote.push(route.request().url()); return route.abort();});
    await page.goto(pathToFileURL(path.resolve(__dirname, '../../webui_preview.html')).href);
    await page.locator('#screen-login').waitFor({state: 'visible'});
    assert.match(await page.locator('.preview-banner').textContent(), /demo/);
    if (process.env.OOPTRA_BROWSER_ARTIFACTS) {
      fs.mkdirSync(process.env.OOPTRA_BROWSER_ARTIFACTS, {recursive: true});
      await page.screenshot({path: path.join(process.env.OOPTRA_BROWSER_ARTIFACTS, 'preview-login.png'), fullPage: true});
    }
    await page.locator('#login-token').fill('wrong');
    await page.locator('#login-form').evaluate(form => form.requestSubmit());
    await page.waitForFunction(() => /不正确/.test(document.querySelector('#login-msg').textContent));
    await page.locator('#login-token').fill('demo');
    await page.locator('#login-form').evaluate(form => form.requestSubmit());
    await page.locator('#screen-login').waitFor({state: 'hidden'});
    await page.locator('#page-overview').waitFor({state: 'visible'});
    await page.locator('[data-theme-toggle]:visible').click();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await page.locator('.nav-item[data-page="logs"]').click();
    await page.waitForFunction(() => document.querySelector('#log-view').textContent.includes('离线演示'));
    assert.equal(await page.locator('#log-wrap').isChecked(), true);
    await page.locator('.nav-item[data-page="voice"]').click();
    await page.waitForFunction(() => document.querySelectorAll('#channel-grid .channel-card').length >= 2);
    await page.locator('#voice-join').click();
    await page.waitForFunction(() => document.querySelector('#v-joined').textContent === '手动会话' && !document.querySelector('#voice-leave').disabled);
    await page.locator('#voice-mic-toggle').click();
    await page.waitForFunction(() => document.querySelector('#voice-mic-toggle').getAttribute('aria-pressed') === 'true' && !document.querySelector('#voice-mic-toggle').disabled);
    await page.locator('#voice-output-volume').evaluate(el => {el.value = 40; el.dispatchEvent(new Event('input', {bubbles: true}));});
    await page.locator('#voice-volume-save').click();
    await page.waitForFunction(() => document.querySelector('#voice-mic-toggle').getAttribute('aria-pressed') === 'false' && !document.querySelector('#voice-mic-toggle').disabled);
    assert.equal(await page.locator('#voice-output-volume').inputValue(), '40');
    await page.locator('#voice-mic-toggle').click();
    await page.waitForFunction(() => document.querySelector('#voice-mic-toggle').getAttribute('aria-pressed') === 'true');
    if (process.env.OOPTRA_BROWSER_ARTIFACTS) await page.screenshot({path: path.join(process.env.OOPTRA_BROWSER_ARTIFACTS, 'preview-voice-controls.png'), fullPage: true});
    await page.locator('#voice-leave').click();
    await page.waitForFunction(() => document.querySelector('#v-joined').textContent === '不在房');
    assert.equal(await page.locator('#voice-mic-toggle').getAttribute('aria-pressed'), 'false');
    assert(await page.locator('#voice-mic-toggle').isDisabled());
    await page.locator('.nav-item[data-page="config"]').click();
    await page.waitForFunction(() => document.querySelectorAll('#config-groups .cfg-group').length > 0);
    await page.locator('#config-tabs [data-tab="voice"]').click();
    const backend = page.locator('[data-group="voice"][data-field="backend"] select');
    await backend.selectOption('aliyun_deepseek');
    const effort = page.locator('[data-group="voice"][data-field="aliyun_deepseek.reasoning_effort"] select');
    assert.equal(await effort.inputValue(), 'low');
    assert.deepEqual(await effort.locator('option').evaluateAll(options => options.map(option => option.value)), ['none', 'low', 'high', 'max']);
    for (const value of ['high', 'max', 'none', 'low']) {
      await effort.selectOption(value);
      await page.locator('#config-save').click();
      await page.waitForFunction(() => !document.querySelector('#config-save').disabled);
      await page.locator('#tool-reload').click();
      await page.waitForFunction(value => document.querySelector('[data-field="aliyun_deepseek.reasoning_effort"] select').value === value, value);
      if (process.env.OOPTRA_BROWSER_ARTIFACTS) await page.screenshot({path: path.join(process.env.OOPTRA_BROWSER_ARTIFACTS, `thinking-${value}.png`), fullPage: true});
    }
    const voiceVolume = page.locator('[data-group="voice"][data-field="output_volume"] input');
    assert.equal(await voiceVolume.inputValue(), '40');
    await voiceVolume.fill('25');
    await page.locator('#config-save').click();
    await page.waitForFunction(() => !document.querySelector('#config-save').disabled);
    await page.locator('#tool-reload').click();
    await page.waitForFunction(() => document.querySelector('[data-field="output_volume"] input').value === '25');
    if (process.env.OOPTRA_BROWSER_ARTIFACTS) await page.screenshot({path: path.join(process.env.OOPTRA_BROWSER_ARTIFACTS, 'preview-voice-volume.png'), fullPage: true});
    for (const width of [390, 320]) {
      await page.setViewportSize({width, height: 844});
      assert(await voiceVolume.isVisible(), 'voice volume option must be visible on mobile');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'voice volume config must fit mobile viewport');
    }
    await page.setViewportSize({width: 1280, height: 720});
    await page.locator('#config-tabs [data-tab="system"]').click();
    assert.equal(await page.locator('[data-field="token"]').count(), 1);
    await page.locator('.nav-item[data-page="account"]').click();
    await page.waitForFunction(() => document.querySelector('#cred-list').textContent.includes('演示账号'));
    await page.locator('.nav-item[data-page="maintenance"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.backup-row').length === 1);
    await page.locator('#maintenance-backup').click();
    await page.waitForFunction(() => document.querySelectorAll('.backup-row').length === 2);
    await page.locator('.backup-delete').first().click();
    await page.locator('#dialog-yes').click();
    await page.waitForFunction(() => document.querySelectorAll('.backup-row').length === 1);
    await page.locator('#maintenance-check').click();
    await page.waitForFunction(() => !document.querySelector('#maintenance-update').disabled);
    await page.locator('#maintenance-update').click();
    await page.locator('#dialog-yes').click();
    await page.waitForFunction(() => document.querySelector('#maintenance-job').dataset.phase === 'preparing');
    assert.equal(await page.locator('progress#maintenance-progress').count(), 1);
    await page.waitForFunction(() => document.querySelector('#maintenance-job').dataset.phase === 'complete', {timeout: 30000});
    assert.equal(await page.locator('#maintenance-progress').getAttribute('value'), '100');
    if (process.env.OOPTRA_BROWSER_ARTIFACTS) await page.screenshot({path: path.join(process.env.OOPTRA_BROWSER_ARTIFACTS, 'preview-maintenance.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'mobile preview must fit viewport');
    if (process.env.OOPTRA_BROWSER_ARTIFACTS) await page.screenshot({path: path.join(process.env.OOPTRA_BROWSER_ARTIFACTS, 'preview-mobile.png'), fullPage: true});
    await page.setViewportSize({width: 1280, height: 720});
    await page.locator('.rail-utilities > summary').click();
    await page.locator('#btn-logout').click();
    await page.locator('#dialog-yes').click();
    await page.locator('#screen-login').waitFor({state: 'visible'});
    assert.equal(await page.locator('#login-token').inputValue(), '');
    await page.reload();
    await page.locator('#screen-login').waitFor({state: 'visible'});
    assert.equal(await page.locator('#login-token').inputValue(), '');
    assert.deepEqual(errors, []);
    assert.deepEqual(remote, []);
    console.log('Standalone offline preview: login, theme, logs, voice, config, account, backups, update progress and logout passed.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
