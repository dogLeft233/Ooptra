/* Real browser decoding and production viewer lifecycle; only the external RTC SDK is replaced. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.OOPTRA_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.OOPTRA_BROWSER_CHANNEL});
  const failures = [];
  try {
    for (const scenario of ['credentials', 'close-during-replace', 'stale-frame']) {
      const page = await browser.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(pathToFileURL(path.resolve(__dirname, '../../src/oopz_sdk/assets/voice/agora_player.html')).href);
      const result = await page.evaluate(async scenario => {
        if (scenario === 'stale-frame') {
          const canvas = document.createElement('canvas');
          canvas.width = 320; canvas.height = 180;
          document.body.append(canvas);
          const paint = canvas.getContext('2d');
          paint.fillStyle = 'red'; paint.fillRect(0, 0, 320, 180);
          const track = canvas.captureStream(0).getVideoTracks()[0];
          const user = {uid: 'publisher', hasVideo: true, videoTrack: {getMediaStreamTrack: () => track}};
          eval('client = {remoteUsers: []}');
          eval('client').remoteUsers = [user];
          const painting = setInterval(() => track.requestFrame(), 20);
          const first = await window.agoraCaptureScreen();
          clearInterval(painting);
          await new Promise(resolve => setTimeout(resolve, 3200));
          const stale = await window.agoraCaptureScreen();
          const stillStale = await window.agoraCaptureScreen();
          paint.fillStyle = 'blue'; paint.fillRect(0, 0, 320, 180);
          const refreshing = setInterval(() => track.requestFrame(), 20);
          const renewed = await window.agoraCaptureScreen();
          clearInterval(refreshing);
          track.stop();
          return {first: first.status, stale: stale.status, stillStale: stillStale.status, renewed: renewed.status};
        }
        const joins = [];
        let releaseLeave, leaveStarted;
        const entered = new Promise(resolve => {leaveStarted = resolve;});
        let stall = false;
        const clients = [];
        window.AgoraRTC = {createClient: () => {
          const rtc = {connectionState: 'DISCONNECTED', remoteUsers: [{uid: 'publisher', hasVideo: true}],
            async setClientRole() {}, on() {},
            async join(...args) {joins.push(args); this.connectionState = 'CONNECTED';},
            async leave() {
              if (stall) {leaveStarted(); await new Promise(resolve => {releaseLeave = resolve;});}
              this.connectionState = 'DISCONNECTED';
            }};
          clients.push(rtc);
          return rtc;
        }};
        const base = {key: 'session:publisher', publisher: 'publisher', appId: 'app', roomId: 'room', token: 'token-1', uid: 12};
        await window.agoraWatchScreen(base);
        if (scenario === 'credentials') {
          await window.agoraWatchScreen({...base});
          await window.agoraWatchScreen({...base, token: 'token-2'});
          await window.agoraWatchScreen({...base, token: 'token-2', roomId: 'room-2'});
          return {joins};
        }
        stall = true;
        const pending = window.agoraWatchScreen({...base, key: 'replacement'});
        await entered;
        const closed = await window.agoraCloseScreen();
        stall = false;
        releaseLeave();
        const replaced = await pending;
        return {closed, replaced, active: clients.filter(rtc => rtc.connectionState === 'CONNECTED').length};
      }, scenario);
      try {
        if (scenario === 'credentials') {
          assert.deepEqual(result.joins.map(row => [row[1], row[2]]), [
            ['room', 'token-1'], ['room', 'token-2'], ['room-2', 'token-2']]);
        } else if (scenario === 'close-during-replace') {
          assert.equal(result.closed.ok, true);
          assert.equal(result.replaced.ok, false);
          assert.equal(result.active, 0, 'a completed close must invalidate pending replacement');
        } else {
          assert.equal(result.first, 'captured');
          assert.equal(result.stale, 'unavailable', 'a live track cannot prove the retained decoded frame is fresh');
          assert.equal(result.stillStale, 'unavailable', 'repeated capture must not refresh a retained image');
          assert.equal(result.renewed, 'captured');
        }
        assert.deepEqual(errors, []);
        if (process.env.OOPTRA_BROWSER_ARTIFACTS) {
          fs.mkdirSync(process.env.OOPTRA_BROWSER_ARTIFACTS, {recursive: true});
          await page.evaluate(text => {
            const report = document.createElement('pre');
            report.textContent = text;
            document.body.prepend(report);
          }, `${scenario}: passed\n${JSON.stringify(result, null, 2)}`);
          await page.screenshot({path: path.join(process.env.OOPTRA_BROWSER_ARTIFACTS, `${scenario}.png`)});
        }
        console.log(`${scenario}: passed`);
      } catch (error) {failures.push(`${scenario}: ${error.message}`);}
      await page.close();
    }
  } finally {await browser.close();}
  assert.deepEqual(failures, []);
})().catch(error => {console.error(error); process.exitCode = 1;});
