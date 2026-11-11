/* Real video-frame extraction; the remote provider boundary supplies a canvas MediaStream. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.OOPTRA_PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(pathToFileURL(path.resolve(__dirname, '../../src/oopz_sdk/assets/voice/agora_player.html')).href);
    const result = await page.evaluate(async () => {
      const canvas = document.createElement('canvas');
      canvas.width = 640; canvas.height = 360;
      const context = canvas.getContext('2d');
      context.fillStyle = 'red'; context.fillRect(0, 0, 640, 360);
      const stream = canvas.captureStream(30);
      const track = stream.getVideoTracks()[0];
      const user = {uid: 'screen-publisher', hasVideo: true, videoTrack: {getMediaStreamTrack: () => track}};
      // Production capture reads the SDK client lexical binding; no application function is mocked.
      eval('client = {remoteUsers: []}');
      const none = await window.agoraCaptureScreen();
      eval('client').remoteUsers = [user];
      const first = await window.agoraCaptureScreen();
      context.fillStyle = 'blue';
      const painting = setInterval(() => {context.fillRect(0, 0, 640, 360); track.requestFrame();}, 20);
      await new Promise(resolve => setTimeout(resolve, 250));
      clearInterval(painting);
      const second = await window.agoraCaptureScreen();
      eval('client').remoteUsers = [user, {...user, uid: 'other-publisher'}];
      const ambiguous = await window.agoraCaptureScreen();
      eval('client').remoteUsers = [];
      const ended = await window.agoraCaptureScreen();
      track.stop();
      return {none, first, second, ambiguous, ended};
    });
    assert.equal(result.none.status, 'no_video');
    assert.equal(result.first.status, 'captured');
    assert.equal(result.first.width, 640);
    assert.equal(result.first.height, 360);
    assert(result.first.jpeg_base64 !== result.second.jpeg_base64, 'new publisher frame must replace the old image');
    assert.equal(result.ambiguous.status, 'ambiguous_video');
    assert.equal(result.ended.status, 'no_video');
    assert(!('jpeg_base64' in result.ended));
    assert.deepEqual(errors, []);
    if (process.env.OOPTRA_BROWSER_ARTIFACTS) {
      fs.mkdirSync(process.env.OOPTRA_BROWSER_ARTIFACTS, {recursive: true});
      fs.writeFileSync(path.join(process.env.OOPTRA_BROWSER_ARTIFACTS, 'screen-frame.jpg'), Buffer.from(result.second.jpeg_base64, 'base64'));
    }
    console.log('Screen frames: real MediaStream JPEG, newest frame, ambiguous publishers and ended stream passed.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
