// Browser check for the clip picker over tests/ui/mock_bridge.js (no Python, no network): asserts the
// behaviour and saves screenshots to the folder given. Needs Playwright with Chromium installed:
//   $env:PLAYWRIGHT = '<path to node_modules/playwright>'   # or have `playwright` resolvable
//   node tests/ui/picker_check.js <screenshot dir>
const { chromium } = require(process.env.PLAYWRIGHT || 'playwright');
const fs = require('fs'), path = require('path'), assert = require('assert');
const repo = path.resolve(__dirname, '..', '..').split(path.sep).join('/');
const out = process.argv[2];
const mock = fs.readFileSync(repo + '/tests/ui/mock_bridge.js', 'utf8');

async function page(browser, mockOpt, scheme = 'dark', size = [1320, 860]) {
  const ctx = await browser.newContext({ viewport: { width: size[0], height: size[1] }, colorScheme: scheme });
  const p = await ctx.newPage(); const errors = [];
  p.on('pageerror', (e) => errors.push(e.message)); p.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await p.addInitScript(`window.__MOCK = ${JSON.stringify(mockOpt)};\n${mock}`);
  await p.goto('file:///' + repo + '/clipcompiler/ui/web/index.html'); await p.waitForTimeout(900);
  return { p, ctx, errors };
}
const text = (p, sel) => p.$eval(sel, (e) => e.textContent.trim());
const settle = (p) => p.waitForTimeout(350);

(async () => {
  const browser = await chromium.launch();
  const { p, ctx, errors } = await page(browser, { smallScan: true });
  // Home: all clips in range, picker link visible.
  assert.strictEqual(await text(p, '#pickInfo'), 'All 26 clips in range');
  await p.waitForFunction(() => document.querySelector('#sClips').textContent === '26', null, { timeout: 5000 });
  await p.click('#pickClips'); await settle(p);
  assert.strictEqual(await p.$eval('.screen.on', (e) => e.dataset.screen), 'pick');
  assert.strictEqual((await p.$$('.pick-clip')).length, 26);
  assert.strictEqual((await p.$$('.pick-day')).length, 3);
  const squashed = await p.$$eval('.pick-day', (days) => days.filter((d) => d.scrollHeight > d.clientHeight + 1).length);
  assert.strictEqual(squashed, 0, 'day boxes must not clip their clips');
  assert.strictEqual(await text(p, '#pkCount'), '26');
  // Untick everything shown, then tick two clips by clicking their rows.
  await p.click('#pickNone'); await settle(p);
  assert.strictEqual(await text(p, '#pkCount'), '0');
  assert.strictEqual(await p.$eval('#pickDone', (b) => b.disabled), true);
  await p.click('.pick-clip[data-i="1"] .name'); await p.click('.pick-clip[data-i="2"] input'); await settle(p);
  assert.strictEqual(await text(p, '#pkCount'), '2');
  assert.strictEqual(await p.$eval('.pick-clip[data-i="2"] input', (c) => c.checked), true);
  // The play button opens the clip and does not change the selection.
  await p.click('.pick-clip[data-i="5"] .play'); await settle(p);
  assert.strictEqual(await text(p, '#pkCount'), '2');
  // Day header ticks the whole evening (9 clips), and shows a partial state before that.
  assert.strictEqual(await p.$eval('.pick-day header input', (c) => c.indeterminate), true);
  await p.click('.pick-day header b'); await settle(p);
  assert.strictEqual(await text(p, '#pkCount'), '9');
  // Filters: "Not judged" hides judged clips; "Tick shown" only ticks what is visible.
  await p.click('#pickFilter button[data-v="new"]'); await settle(p);
  const visible = await p.$$eval('.pick-clip', (rows) => rows.filter((r) => !r.hidden).length);
  assert.ok(visible > 0 && visible < 26, 'filter hides judged clips');
  await p.click('#pickFilter button[data-v="all"]');
  await p.fill('#pickSearch', 'zzz-no-match'); await settle(p);
  assert.strictEqual(await p.$$eval('.pick-clip', (rows) => rows.filter((r) => !r.hidden).length), 0);
  await p.fill('#pickSearch', ''); await settle(p);
  // Footer cost reflects only the ticked clips.
  const cost = await text(p, '#pkCost'); assert.ok(cost.startsWith('$'));
  await p.waitForTimeout(300);
  await p.screenshot({ path: path.join(out, 'pick-dark-1320.png') });
  // Done: back home with "9 of 26 clips chosen" and the stats for 9.
  await p.click('#pickDone'); await p.waitForTimeout(600);
  assert.strictEqual(await p.$eval('.screen.on', (e) => e.dataset.screen), 'home');
  assert.strictEqual(await text(p, '#pickInfo'), '9 of 26 clips chosen');
  assert.strictEqual(await text(p, '#pickClips'), 'Change');
  await p.waitForFunction(() => document.querySelector('#sClips').textContent === '9', null, { timeout: 5000 });
  assert.ok((await text(p, '#startBtn')).includes('clips'), 'start button counts the chosen clips');
  await p.screenshot({ path: path.join(out, 'home-picked-dark-1320.png') });
  // Reopen, change, cancel: the earlier choice comes back.
  await p.click('#pickClips'); await settle(p);
  assert.strictEqual(await text(p, '#pkCount'), '9');
  await p.click('#pickAll'); await settle(p); await p.waitForTimeout(250);
  assert.strictEqual(await text(p, '#pkCount'), '26');
  await p.click('#pickCancel'); await p.waitForTimeout(600);
  assert.strictEqual(await text(p, '#pickInfo'), '9 of 26 clips chosen');
  // Ticking everything again means "all clips" (no selection).
  await p.click('#pickClips'); await settle(p); await p.click('#pickAll'); await settle(p);
  await p.click('#pickDone'); await p.waitForTimeout(600);
  assert.strictEqual(await text(p, '#pickInfo'), 'All 26 clips in range');
  assert.deepStrictEqual(errors, []);
  await ctx.close();
  // Light theme and the smaller window, for the eye check.
  for (const [scheme, size] of [['light', [1320, 860]], ['dark', [1100, 720]], ['light', [1100, 720]]]) {
    const v = await page(browser, { smallScan: true, picked: 9 }, scheme, size);
    await v.p.click('#pickClips'); await v.p.waitForTimeout(700);
    await v.p.screenshot({ path: path.join(out, `pick-${scheme}-${size[0]}.png`) });
    assert.deepStrictEqual(v.errors, []); await v.ctx.close();
  }
  await browser.close();
  console.log('picker UI checks passed');
})().catch((e) => { console.error('FAILED:', e.message); process.exit(1); });
